#!/usr/bin/env python3
"""Export repeated measurements to CSV and append evidence to the design doc."""

import argparse
import csv
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
LABELS = {
    "scale_out_2_to_8": "Pod 확장 2→8 (직접 scale 요청)",
    "scale_in_8_to_2": "축소 8→2 판정 (컨테이너 종료 완료 제외)",
    "rolling_update_8_pods": "8개 Pod 버전 교체",
    "base_pod_replacement": "Base Pod 1개 삭제 후 Ready 2개 복구",
    "policy_label_repair": "분류 손상 후 base 2개 정책 복구",
    "webhook_outage_create_4": "웹훅 중단 중 새 Pod 4개 준비",
    "webhook_restore_and_adopt": "웹훅 재기동 요청→기존 Pod base 편입",
    "sqs_6_messages_scale_2_to_6": "SQS 메시지 6개→Pod 2→6",
    "queue_completed_returns_to_scheduled_2": "메시지 완료→예약 최소 2개 복귀",
    "off_hours_message_wakes_1": "업무 외 메시지→Pod 0→1",
    "off_hours_empty_queue_returns_to_0": "업무 외 작업 완료→Pod 1→0",
}
START = "<!-- REPEATED_MEASUREMENTS_START -->"
END = "<!-- REPEATED_MEASUREMENTS_END -->"


def export(directory):
    data = json.loads((directory / "summary.json").read_text())
    if not data["completed"]:
        raise ValueError("measurement is incomplete: " + str(directory))
    with (directory / "summary.csv").open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "scenario",
                "trials",
                "passed",
                "failed",
                "p50_seconds",
                "p95_seconds",
                "max_seconds",
                "min_observed_ready_base",
                "min_observed_ready_on_demand",
            ]
        )
        for name, item in data["scenarios"].items():
            latency = item["latency_seconds_successful_trials"]
            writer.writerow(
                [
                    name,
                    item["trials"],
                    item["passed"],
                    item["failed"],
                    latency["p50"],
                    latency["p95_nearest_rank"],
                    latency["max"],
                    item["min_observed_ready_base"],
                    item["min_observed_ready_on_demand"],
                ]
            )
    return data


def table(data):
    rows = [
        "| 항목 | 성공/시도 | p50(초) | p95(초) | 최대(초) |",
        "|---|---:|---:|---:|---:|",
    ]
    for name in LABELS:
        if name not in data["scenarios"]:
            continue
        item = data["scenarios"][name]
        latency = item["latency_seconds_successful_trials"]
        numbers = [
            f"{latency[k]:.2f}" if latency[k] is not None else "미측정"
            for k in ["p50", "p95_nearest_rank", "max"]
        ]
        rows.append(
            "| "
            + LABELS[name]
            + f" | {item['passed']}/{item['trials']} | "
            + " | ".join(numbers)
            + " |"
        )
    return "\n".join(rows)


def render(policy_dir, keda_dir=None):
    policy = export(policy_dir)
    environment = json.loads((policy_dir / "environment.json").read_text())
    d = environment["docker"]
    normal = ["scale_out_2_to_8", "scale_in_8_to_2", "rolling_update_8_pods"]
    floors = "\n".join(
        f"- {LABELS[name]}: {policy['scenarios'][name]['passed']}/{policy['scenarios'][name]['trials']} 성공, 관측한 Ready base 최솟값 {policy['scenarios'][name]['min_observed_ready_base']}개."
        for name in normal
    )
    admission = policy["admission"]
    lines = [
        "### 15.5 반복 측정: 기본 용량과 복구시간",
        "",
        f"정책 측정 원본: `tests/capacity-policy/results/repeated/{policy_dir.name}/`. 각 시나리오 {policy['requested_trials_per_scenario']}회, 고정 순서로 실행했다. 실패한 시도가 있다면 분모와 raw 기록에 남기며 p95는 성공 시도의 관측 시간에 대한 nearest-rank 값이다. 초기 시도를 사후 제거하지 않았다.",
        "",
        f"전체 반복 실행은 {policy['wall_seconds'] / 60:.1f}분 걸렸다. 시도 사이의 웹훅 종료·재기동, 준비·정리 대기도 포함한 실행 시간으로, 아래 시나리오별 완료 시간과 구분한다.",
        "",
        "`trials.jsonl`은 시도별 성공 여부·종료 조건·시간, `observations.jsonl`은 Pod UID와 실제 Node 라벨을 포함한 관측값, `admission.json`은 중복 제거한 웹훅 로그다. `summary.json`과 `summary.csv`는 이 기록의 집계다. `environment.json` 및 `effective-policy.json`에 실행 환경·실제 웹훅 spec·컨테이너 imageID·소스 해시를 기록했다.",
        "",
        f"환경은 kind Kubernetes 1.31.9, 고정 Node 3개, Docker CPU {d['NCPU']}개·메모리 {d['MemTotal'] / 1024**3:.2f}GiB, 경량 Python HTTP Pod다. 이미지는 사전 로드했고 실제 Node 공급·GPU 모델 로딩·음성 추론은 실행하지 않았다. 이미지/모델의 cold start가 포함된 값이 아니다. 모의 base Pod는 같은 On-Demand 라벨 Node에 함께 배치될 수 있어 Node 장애에 대한 분산 배치·고가용성 검증에 해당하지 않는다.",
        "",
        table(policy),
        "",
        "기본 용량 유지 관측:",
        "",
        floors,
        "",
        "위 최솟값은 250ms sleep과 Kubernetes API 조회 사이의 이산 관측값이다. 관측 사이의 모든 순간이나 실제 서비스 가용성 SLA를 보장하지 않는다. 모의 Pod의 readiness 주기는 1초, 종료 유예는 1초다. 웹훅의 자원 제한·준비 검사 주기와 컨트롤러의 5초 대조 주기는 제품 설정을 유지했다. 매 시도마다 새 Deployment를 사용하여 제품의 30분 복구 cooldown을 강제로 해제하지 않았다.",
        "",
        "시간은 API 변경 명령 또는 메시지 전송 직전부터 완료 조건을 처음 관측할 때까지이며 클라이언트·API 왕복과 관측 지연을 포함한다. 별도 setup/cleanup은 제외했다. 특히 축소 시간은 남은 활성 Pod가 2개임을 확인한 시간이며, 종료 중인 컨테이너와 Node의 자원·과금 종료 시간은 아니다.",
        "",
        "Base Pod 삭제는 기존 Node 위에서 ReplicaSet이 대체 Pod를 준비하는 실험이다. 분류 손상은 실제 On-Demand Pod의 tier 라벨만 훼손한 정책 불일치 실험으로 Node 장애·Spot 회수와 다르다. 웹훅 중단 실험은 새로 생성한 4개 Pod가 모두 On-Demand 모의 라벨 Node에서 Ready가 되는지 확인했다. 기존 사용자 HTTP 요청 성공률을 측정한 실험은 아니다.",
        "",
        f"웹훅 Admission 로그 {admission['samples']}개를 복제본별 UID와 로그 순번으로 중복 제거해 수집했다. 관측 처리시간 p50 {admission['p50_ms']:.2f}ms, p95 {admission['p95_ms']:.2f}ms, 최대 {admission['max_ms']:.2f}ms다. 웹훅 미사용 대비 추가 지연을 비교한 값은 아니며 전체 사용자 요청 지연과도 다르다. 서버 핸들러 내부의 시작부터 응답 처리까지의 로그로, API 서버와 웹훅 사이의 전체 네트워크 왕복 시간은 포함하지 않는다.",
    ]
    if keda_dir:
        queue = export(keda_dir)
        counts = queue["requested_trials_per_scenario"]
        lines += [
            "",
            "### 15.6 반복 측정: 실제 KEDA/HPA와 로컬 SQS",
            "",
            f"원본: `tests/capacity-policy/results/keda-repeated/{keda_dir.name}/`. KEDA 2.16.1과 ElasticMQ 1.6.9를 사용했다. 예약 최소 수량 2개에서 확장 {counts['sqs_6_messages_scale_2_to_6']}회·복귀 {counts['queue_completed_returns_to_scheduled_2']}회, 업무 외 0→1은 {counts['off_hours_message_wakes_1']}회·1→0은 {counts['off_hours_empty_queue_returns_to_0']}회다.",
            "",
            "집계는 `summary.json`·`summary.csv`, 시도별 기록은 `trials.jsonl`, Pod 관측은 `observations.jsonl`, 테스트 설정과 소스 해시는 `environment.json`에 남겼다. 정책 자체의 반복 측정 이후 KEDA를 설치하여 두 실험의 클러스터 부하 조건을 구분했다.",
            "",
            table(queue),
            "",
            "큐 트리거는 visible+in-flight를 포함하며 queueLength=1이다. Pod는 추론을 수행하지 않고, 측정 도구가 모의 메시지를 수신·삭제해 완료를 모사한다. 따라서 처리량이나 실제 치료 음성 완료시간 수치로 사용할 수 없다. 실제 AWS SQS 속성 갱신 지연이나 EKS↔SQS 네트워크 지연을 재현한 값도 아니다.",
            "",
            "테스트 polling=2초, cooldown=5초, scale-down stabilization=0으로 설정했다. 제품 GPU의 cooldown·안정화 600초와 다르다. 이 결과를 운영 GPU의 야간 축소·기동 시간으로 인용하면 안 된다. 기본 수량을 유지하는 구간의 Ready base 관측과 업무 외 0개 허용 구간을 구분한다. 공통 raw 필드 observed_deficit_seconds는 Ready base 2개 미만의 관측 시간이므로, 목표가 0 또는 1인 업무 외 시나리오의 장애시간으로 해석하지 않는다.",
        ]
    lines += [
        "",
        "재현:",
        "",
        "```bash",
        "python3 tests/capacity-policy/repeated_measurement.py --trials 30",
        "# 첫 측정이 끝난 뒤 KEDA/ElasticMQ를 설치하고 실행",
        "python3 tests/capacity-policy/repeated_keda.py --trials 20 --off-hours 10",
        "```",
        "",
        "단일 로컬 호스트의 통제된 실험에서 얻은 표본이다. 고정 실행 순서와 reconcile/HPA 주기 사이의 상관관계, 호스트 부하, 작은 표본 수의 영향을 받는다. 성공 횟수와 관측 범위를 함께 제시하고 프로덕션 가용성·처리용량·장애 복구 SLO로 일반화하지 않는다.",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=pathlib.Path, required=True)
    parser.add_argument("--keda", type=pathlib.Path)
    args = parser.parse_args()
    text = render(args.policy, args.keda)
    doc = ROOT / "docs/design/karpenter-base-burst.md"
    if not doc.exists():
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text(START + "\n" + text + "\n" + END + "\n")
        print(text)
        raise SystemExit(0)
    old = doc.read_text()
    if START in old:
        a = old.index(START)
        b = old.index(END, a) + len(END)
        old = old[:a] + START + "\n" + text + "\n" + END + old[b:]
    else:
        old = old.replace(
            "## 16. 테스트에서 발견한 문제와 수정",
            START
            + "\n"
            + text
            + "\n"
            + END
            + "\n\n## 16. 테스트에서 발견한 문제와 수정",
        )
    doc.write_text(old)
    print(text)
