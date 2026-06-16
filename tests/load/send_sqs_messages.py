"""
SQS 메시지 투입 스크립트 (Phase 1: CA+HPA / Phase 2: Karpenter+KEDA 공용)

큐별 역할 및 스케일 트리거:
  audio-preprocess-queue  → cpu-worker   | Phase 1: CPU > 70%       | Phase 2: KEDA queueLength=5
  report-analysis-queue   → cpu-worker   | Phase 1: CPU > 70%       | Phase 2: KEDA queueLength=5
  rag-ingest-queue        → batch-worker | Phase 1: CPU > 70%       | Phase 2: KEDA queueLength=3
  gpu-inference-queue     → gpu-worker   | Phase 1: CPU > 70%       | Phase 2: KEDA queueLength=1

Phase 1 (CA+HPA):
  큐 적체가 쌓여도 워커 CPU가 70%에 달하기 전까지 HPA가 반응하지 않는다.
  워커가 메시지를 빠르게 소비하면 CPU 부하가 발생하지 않아 HPA 무반응이 예상된다.
  → 이 무반응 또는 지연이 Phase 1의 한계이며, Phase 2 KEDA와의 핵심 비교 지점이다.
  → measure_scale_time.sh 를 병행 실행해 스케일 지연(또는 미발생)을 기록한다.

Phase 2 (Karpenter+KEDA):
  KEDA가 큐 깊이를 직접 관찰하므로 메시지 도착 후 ~30초 내에 Pod scale 요청이 발생한다.
  투입 완료 후 자동으로 큐 깊이를 30초 간격으로 폴링해 KEDA 반응을 관찰한다.

사용법:
  pip install boto3
  ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

  # cpu-worker 스케일 테스트
  python send_sqs_messages.py \\
    --queue-url "https://sqs.ap-northeast-2.amazonaws.com/${ACCOUNT}/utterai-dev-audio-preprocess-queue" \\
    --count 100 --rate 10 --phase 1

  # batch-worker 스케일 테스트 (동시 실행 권장)
  python send_sqs_messages.py \\
    --queue-url "https://sqs.ap-northeast-2.amazonaws.com/${ACCOUNT}/utterai-dev-rag-ingest-queue" \\
    --count 50 --rate 5 --phase 1
"""
import argparse
import json
import time
import uuid
from datetime import datetime

import boto3

_PHASE_HEADER = {
    1: (
        "[ Phase 1: CA + HPA ]\n"
        "  예상 동작: 워커 CPU > 70% 달성 시 HPA 반응 (큐 깊이 단독으로는 스케일 안 됨)\n"
        "  HPA 무반응이 관찰되면 → Phase 2 KEDA와의 핵심 대비 지점으로 기록\n"
        "  병행 실행:  ./tests/observe/watch_scaling.sh | tee results/phase1_sqs.log\n"
        "             ./tests/observe/measure_scale_time.sh <namespace> <deployment>"
    ),
    2: (
        "[ Phase 2: Karpenter + KEDA ]\n"
        "  예상 동작: 큐 깊이 임계값 도달 후 ~30s 내 KEDA가 Pod scale 요청\n"
        "  cpu-worker:   queueLength=5  → 100개 투입 시 최대 20 Pod 요청\n"
        "  batch-worker: queueLength=3  →  50개 투입 시 최대 17 Pod 요청\n"
        "  gpu-worker:   queueLength=1  →   1개 투입 시 즉시 Pod 요청\n"
        "  병행 실행:  ./tests/observe/watch_scaling.sh | tee results/phase2_sqs.log\n"
        "             ./tests/observe/measure_scale_time.sh <namespace> <deployment>"
    ),
}


def _queue_depth(sqs, queue_url: str) -> int:
    resp = sqs.get_queue_attributes(
        QueueUrl=queue_url,
        AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
    )
    attrs = resp["Attributes"]
    return (
        int(attrs.get("ApproximateNumberOfMessages", 0))
        + int(attrs.get("ApproximateNumberOfMessagesNotVisible", 0))
    )


def _watch_queue(sqs, queue_url: str, post_send_depth: int, timeout: int = 180) -> None:
    """Phase 2 전용: 투입 후 큐 깊이를 30초 간격으로 폴링해 KEDA 반응을 관찰한다."""
    print(f"\nKEDA 반응 관찰 시작 (30초 간격, 최대 {timeout}초, Ctrl+C로 중단)\n")
    start = time.time()
    try:
        while time.time() - start < timeout:
            depth = _queue_depth(sqs, queue_url)
            elapsed = int(time.time() - start)
            drain = post_send_depth - depth
            print(f"  [+{elapsed:>3d}s] 큐 깊이: {depth:>4d}개  (소비됨: {drain:>4d}개)")
            if depth == 0:
                print(f"\n  큐 완전 소진 (+{elapsed}s) — KEDA Pod가 메시지를 모두 소비했습니다.")
                break
            time.sleep(30)
        else:
            print(f"\n  관찰 종료 (타임아웃 {timeout}s 초과)")
    except KeyboardInterrupt:
        print("\n  관찰 중단")


def send_messages(queue_url: str, count: int, rate: int, region: str, phase: int) -> None:
    sqs = boto3.client("sqs", region_name=region)

    # SQS send_message_batch 최대 배치 크기는 10
    batch_size = min(10, rate)
    interval = batch_size / rate  # 배치 투입 간격 (초)

    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] SQS 메시지 투입 시작")
    print(f"큐:     {queue_url}")
    print(f"설정:   총 {count}개 | 속도 {rate}개/초 | 배치 {batch_size}개\n")
    print(_PHASE_HEADER[phase])
    print()

    before_depth = _queue_depth(sqs, queue_url)
    print(f"투입 전 큐 깊이: {before_depth}개\n")

    sent = 0
    failed_total = 0
    batches = (count + batch_size - 1) // batch_size
    start_time = time.time()

    for i in range(batches):
        batch_count = min(batch_size, count - sent)
        entries = [
            {
                "Id": str(j),
                "MessageBody": json.dumps({
                    "job_id": str(uuid.uuid4()),
                    "s3_bucket": "utterai-dev-raw-audio",
                    "s3_key": f"load-test/{uuid.uuid4()}.wav",
                    "user_id": f"load-test-user-{i % 10}",
                    "type": "audio_preprocess",
                    "timestamp": datetime.now().isoformat(),
                }),
            }
            for j in range(batch_count)
        ]

        resp = sqs.send_message_batch(QueueUrl=queue_url, Entries=entries)
        ok = len(resp.get("Successful", []))
        ng = len(resp.get("Failed", []))
        sent += ok
        failed_total += ng

        print(f"  배치 {i+1:>3d}/{batches} — {ok}개 전송 | 누적: {sent}개", end="")
        if ng:
            print(f" | 실패: {ng}개", end="")
        print()

        if i < batches - 1:
            time.sleep(interval)

    elapsed = time.time() - start_time
    after_depth = _queue_depth(sqs, queue_url)

    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 투입 완료")
    print(f"전송: {sent}개 | 실패: {failed_total}개 | 소요: {elapsed:.1f}s")
    print(f"투입 전 큐 깊이: {before_depth}개  →  투입 후: {after_depth}개\n")

    if phase == 2:
        _watch_queue(sqs, queue_url, post_send_depth=after_depth)
    else:
        print("[ Phase 1 확인 포인트 ]")
        print("  - watch_scaling.sh 로그에서 HPA TARGETS 컬럼(현재 CPU%)을 확인하세요.")
        print("  - CPU 70% 미달 시 HPA 미반응 → measure_scale_time.sh 에 '스케일 미발생' 기록")
        print("  - 이 결과가 Phase 2 KEDA와의 비교 기준값이 됩니다.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="SQS 메시지 투입 (Phase 1: CA+HPA / Phase 2: Karpenter+KEDA)"
    )
    parser.add_argument("--queue-url", required=True, help="SQS 큐 URL")
    parser.add_argument("--count", type=int, default=100, help="전송할 메시지 수 (기본: 100)")
    parser.add_argument("--rate", type=int, default=10,
                        help="초당 투입 속도 (기본: 10개/초, 최대 배치 단위 10)")
    parser.add_argument("--region", default="ap-northeast-2", help="AWS 리전 (기본: ap-northeast-2)")
    parser.add_argument("--phase", type=int, choices=[1, 2], default=1,
                        help="1=CA+HPA  2=Karpenter+KEDA (기본: 1)")
    args = parser.parse_args()

    send_messages(args.queue_url, args.count, args.rate, args.region, args.phase)
