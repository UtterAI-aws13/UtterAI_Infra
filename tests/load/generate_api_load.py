"""
API 부하 테스트 스크립트 (Phase 1: CA+HPA / Phase 2: Karpenter+KEDA 공용)

Phase 1 (CA+HPA):
  API Pod CPU > 70% 구간이 stabilizationWindowSeconds(60s) 이상 유지될 때 HPA가 반응한다.
  Pod가 기존 노드 용량을 초과하면 CA가 노드를 추가한다 (HPA 반응 후 3~4분 추가 소요).
  → 부하를 최소 5분 이상 유지해야 HPA → CA → 노드 Ready 전 과정을 관찰할 수 있다.

Phase 2 (Karpenter+KEDA):
  API Pod 스케일링은 Phase 1과 동일하게 HPA CPU 기반으로 동작한다.
  노드가 부족해지면 CA 대신 Karpenter가 처리 → 노드 Ready 시간이 2~3분 단축된다.
  → 동일 파라미터로 두 Phase를 실행하고 measure_scale_time.sh 결과를 비교.

엔드포인트 선택:
  /health          — CPU 부하 없음. 연결 확인 전용.
  /api/v1/...      — DB/Redis 조회가 발생하는 엔드포인트를 사용해야 CPU 부하가 발생한다.

사용법:
  pip install requests
  # Phase 1 — CA+HPA 측정
  python generate_api_load.py --url http://<ALB_DNS>/api/v1/<endpoint> --rps 50 --duration 300 --phase 1

  # Phase 2 — Karpenter+KEDA 측정 (동일 파라미터, --phase만 변경)
  python generate_api_load.py --url http://<ALB_DNS>/api/v1/<endpoint> --rps 50 --duration 300 --phase 2
"""
import argparse
import statistics
import threading
import time
from datetime import datetime

import requests

_PHASE_HEADER = {
    1: (
        "[ Phase 1: CA + HPA ]\n"
        "  예상 흐름: CPU > 70% (60s 유지) → HPA Pod 증설 → 노드 부족 시 CA 노드 추가 (3~4분)\n"
        "  병행 실행: ./tests/observe/watch_scaling.sh | tee results/phase1_api.log\n"
        "            ./tests/observe/measure_scale_time.sh utterai-api utterai-api"
    ),
    2: (
        "[ Phase 2: Karpenter + KEDA ]\n"
        "  예상 흐름: CPU > 70% (60s 유지) → HPA Pod 증설 → 노드 부족 시 Karpenter 노드 추가 (~60~90s)\n"
        "  병행 실행: ./tests/observe/watch_scaling.sh | tee results/phase2_api.log\n"
        "            ./tests/observe/measure_scale_time.sh utterai-api utterai-api"
    ),
}


def _send_request(url: str, results: list, lock: threading.Lock) -> None:
    try:
        t0 = time.time()
        resp = requests.get(url, timeout=5)
        latency = time.time() - t0
        with lock:
            results.append({"status": resp.status_code, "latency": latency})
    except Exception as e:
        with lock:
            results.append({"status": "error", "latency": None, "error": str(e)})


def _print_snapshot(results: list, elapsed: int) -> None:
    if not results:
        return
    success = [r for r in results if isinstance(r["status"], int) and r["status"] < 500]
    errors = [r for r in results if r["status"] == "error"]
    latencies = sorted(r["latency"] for r in success if r["latency"])

    if latencies:
        p50 = statistics.median(latencies) * 1000
        p95 = latencies[max(0, int(len(latencies) * 0.95) - 1)] * 1000
        p99 = latencies[max(0, int(len(latencies) * 0.99) - 1)] * 1000
        avg = sum(latencies) / len(latencies) * 1000
        print(
            f"  [+{elapsed:>4d}s] 요청: {len(results):>5d} | 성공: {len(success):>5d}"
            f" | 오류: {len(errors):>4d}"
            f" | avg: {avg:>5.0f}ms  p50: {p50:>5.0f}ms  p95: {p95:>5.0f}ms  p99: {p99:>5.0f}ms"
        )
    else:
        print(f"  [+{elapsed:>4d}s] 요청: {len(results):>5d} | 성공: 0 | 오류: {len(errors):>4d}")


def run_load(url: str, rps: int, duration: int, phase: int) -> None:
    results: list = []
    lock = threading.Lock()
    interval = 1.0 / rps

    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 부하 시작  rps={rps}  duration={duration}s")
    print(f"대상: {url}\n")
    print(_PHASE_HEADER[phase])
    print()

    start = time.time()
    end_time = start + duration
    next_report = start + 30

    while time.time() < end_time:
        t = threading.Thread(target=_send_request, args=(url, results, lock))
        t.daemon = True
        t.start()
        time.sleep(interval)

        if time.time() >= next_report:
            elapsed = int(time.time() - start)
            with lock:
                snapshot = list(results)
            _print_snapshot(snapshot, elapsed)
            next_report += 30

    time.sleep(2)
    total = int(time.time() - start)

    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 부하 완료 (+{total}s)\n")
    print("===== 최종 결과 =====")
    _print_snapshot(results, total)

    if phase == 1:
        print("\n[ 다음 단계 ]")
        print("  Phase 1 측정 완료. measure_scale_time.sh 의 '전체 스케일아웃 시간'을 기록하세요.")
        print("  Karpenter+KEDA 전환 후 동일 명령을 --phase 2 로 재실행해 비교하세요.")
    else:
        print("\n[ 비교 포인트 ]")
        print("  measure_scale_time.sh 결과에서 Phase 1 대비 노드 Ready 단축 시간을 확인하세요.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="API 부하 테스트 (Phase 1: CA+HPA / Phase 2: Karpenter+KEDA)")
    parser.add_argument("--url", required=True, help="CPU 부하가 발생하는 API 엔드포인트 URL")
    parser.add_argument("--rps", type=int, default=50, help="초당 요청 수 (기본: 50)")
    parser.add_argument("--duration", type=int, default=300, help="테스트 시간(초) (기본: 300)")
    parser.add_argument("--phase", type=int, choices=[1, 2], default=1,
                        help="1=CA+HPA  2=Karpenter+KEDA (기본: 1)")
    args = parser.parse_args()

    run_load(args.url, args.rps, args.duration, args.phase)
