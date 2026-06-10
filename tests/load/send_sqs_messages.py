"""
SQS 메시지 투입 스크립트 (Phase 1: CA+HPA / Phase 2: Karpenter+KEDA 공용)

큐별 역할 및 사용 시나리오:
  audio-preprocess-queue  → cpu-worker HPA/KEDA 트리거  (시나리오 B 핵심)
  gpu-inference-queue     → ml-gpu-worker 스케일 테스트 (cpu-worker가 채워줌, 직접 투입 시 주의)
  report-analysis-queue   → cpu-worker Bedrock 단계     (gpu-worker가 채워줌)
  rag-ingest-queue        → batch-worker HPA/KEDA 트리거 (시나리오 B 병렬 실행)

Phase 1 (CA+HPA):
  SQS 적체가 쌓여도 워커 CPU가 70%에 달하기 전까지는 HPA가 반응하지 않는다.
  → 이 지연 시간을 measure_scale_time.sh로 측정하는 것이 Phase 1의 핵심.

Phase 2 (Karpenter+KEDA):
  KEDA가 큐 깊이를 직접 관찰하므로 메시지 도착 후 ~30초 내에 Pod scale 요청이 발생한다.
  → 동일한 스크립트로 두 Phase를 비교 측정.

사용법:
  pip install boto3
  ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

  # cpu-worker 스케일 테스트
  python send_sqs_messages.py \\
    --queue-url "https://sqs.ap-northeast-2.amazonaws.com/${ACCOUNT}/utterai-dev-audio-preprocess-queue" \\
    --count 100

  # batch-worker 스케일 테스트 (동시 실행 권장)
  python send_sqs_messages.py \\
    --queue-url "https://sqs.ap-northeast-2.amazonaws.com/${ACCOUNT}/utterai-dev-rag-ingest-queue" \\
    --count 200
"""
import argparse
import json
import time
import boto3
from datetime import datetime


def send_messages(queue_url: str, count: int, region: str, batch_size: int = 10):
    sqs = boto3.client("sqs", region_name=region)

    print(f"[{datetime.now()}] SQS 메시지 투입 시작")
    print(f"큐: {queue_url}")
    print(f"총 메시지: {count}개 (배치: {batch_size}개씩)\n")

    sent = 0
    batches = (count + batch_size - 1) // batch_size

    for i in range(batches):
        batch_count = min(batch_size, count - sent)
        entries = [
            {
                "Id": str(j),
                "MessageBody": json.dumps({
                    "job_id": f"test-{i}-{j}",
                    "type": "analysis",
                    "timestamp": datetime.now().isoformat(),
                }),
            }
            for j in range(batch_count)
        ]

        resp = sqs.send_message_batch(QueueUrl=queue_url, Entries=entries)
        sent += len(resp.get("Successful", []))
        failed = len(resp.get("Failed", []))

        print(f"배치 {i+1}/{batches}: {len(resp.get('Successful', []))}개 전송 완료 (누적: {sent})", end="")
        if failed:
            print(f" / 실패: {failed}개", end="")
        print()

        time.sleep(0.1)

    print(f"\n[{datetime.now()}] 완료: 총 {sent}개 전송")
    print("※ Phase 1 (CA+HPA): CPU 임계값 도달 시에만 HPA가 반응합니다. 큐 적체만으로는 스케일 안 됨.")
    print("※ Phase 2 (KEDA):   큐 깊이 >= 임계값 시 ~30초 내 KEDA가 Pod scale을 요청합니다.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue-url", required=True, help="SQS 큐 URL")
    parser.add_argument("--count", type=int, default=100, help="전송할 메시지 수 (기본: 100)")
    parser.add_argument("--region", default="ap-northeast-2", help="AWS 리전")
    args = parser.parse_args()

    send_messages(args.queue_url, args.count, args.region)
