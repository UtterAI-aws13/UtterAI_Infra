"""
SQS 메시지 투입 스크립트 (Phase 1: CA+HPA 기준)

CA+HPA 환경에서는 SQS 큐 적체가 발생해도 Pod가 즉시 스케일되지 않고,
CPU 임계값에 도달해야 HPA가 반응함 → 이 지연을 측정하는 것이 목적

사용법:
  pip install boto3
  python send_sqs_messages.py --queue-url <SQS_URL> --count 100 --region ap-northeast-2
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
    print("※ Phase 1 (HPA): SQS 적체에 반응하지 않고 CPU 임계값 도달 시에만 스케일됩니다.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue-url", required=True, help="SQS 큐 URL")
    parser.add_argument("--count", type=int, default=100, help="전송할 메시지 수 (기본: 100)")
    parser.add_argument("--region", default="ap-northeast-2", help="AWS 리전")
    args = parser.parse_args()

    send_messages(args.queue_url, args.count, args.region)
