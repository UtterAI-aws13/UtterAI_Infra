"""
API 부하 테스트 스크립트 (Phase 1: CA+HPA / Phase 2: Karpenter+KEDA 공용)

목적:
  Backend API HPA를 트리거하기 위한 HTTP 부하 투입.

엔드포인트 선택:
  /health     — CPU 부하 거의 없음. HPA 트리거 안 됨. 연결 확인용으로만 사용.
  /api/v1/... — 실제 DB/Redis 조회가 발생하는 엔드포인트를 사용해야 CPU 부하 발생.

사용법:
  pip install requests
  python generate_api_load.py --url http://<ALB_DNS>/api/v1/<엔드포인트> --rps 50 --duration 300
"""
import argparse
import time
import threading
import requests
from datetime import datetime


def send_request(url: str, results: list):
    try:
        start = time.time()
        resp = requests.get(url, timeout=5)
        elapsed = time.time() - start
        results.append({"status": resp.status_code, "latency": elapsed})
    except Exception as e:
        results.append({"status": "error", "latency": None, "error": str(e)})


def run_load(url: str, rps: int, duration: int):
    results = []
    interval = 1.0 / rps
    end_time = time.time() + duration

    print(f"[{datetime.now()}] 부하 시작: {rps} RPS, {duration}초")
    print(f"대상: {url}\n")

    while time.time() < end_time:
        t = threading.Thread(target=send_request, args=(url, results))
        t.daemon = True
        t.start()
        time.sleep(interval)

    time.sleep(2)

    success = [r for r in results if isinstance(r["status"], int) and r["status"] < 500]
    errors = [r for r in results if r["status"] == "error"]
    latencies = [r["latency"] for r in success if r["latency"]]

    print(f"\n[{datetime.now()}] 부하 완료")
    print(f"총 요청: {len(results)}")
    print(f"성공: {len(success)} / 오류: {len(errors)}")
    if latencies:
        print(f"평균 레이턴시: {sum(latencies)/len(latencies)*1000:.1f}ms")
        print(f"최대 레이턴시: {max(latencies)*1000:.1f}ms")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="API 엔드포인트 URL (CPU 부하가 발생하는 실제 엔드포인트 권장)")
    parser.add_argument("--rps", type=int, default=50, help="초당 요청 수 (기본: 50)")
    parser.add_argument("--duration", type=int, default=300, help="테스트 시간(초) (기본: 300)")
    args = parser.parse_args()

    run_load(args.url, args.rps, args.duration)
