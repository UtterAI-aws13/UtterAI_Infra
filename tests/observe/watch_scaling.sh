#!/bin/bash
# 스케일링 관찰 스크립트 — Phase 1 (CA + HPA)
#
# 10초마다 노드/파드 상태와 HPA 상태를 타임스탬프와 함께 기록
# 두 Phase 모두 동일 스크립트로 실행해 결과를 비교
#
# 사용법:
#   chmod +x watch_scaling.sh
#   ./watch_scaling.sh 2>&1 | tee results/phase1_$(date +%Y%m%d_%H%M%S).log

INTERVAL=10
LOG_FILE="results/scaling_$(date +%Y%m%d_%H%M%S).log"
mkdir -p results

echo "===== 스케일링 관찰 시작: $(date) =====" | tee "$LOG_FILE"
echo "Phase 1: Cluster Autoscaler + HPA" | tee -a "$LOG_FILE"
echo "기록 주기: ${INTERVAL}초" | tee -a "$LOG_FILE"
echo "Ctrl+C로 종료" | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"

while true; do
  TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

  echo "━━━━━━ $TIMESTAMP ━━━━━━" | tee -a "$LOG_FILE"

  echo "[노드]" | tee -a "$LOG_FILE"
  kubectl get nodes -o wide --no-headers 2>/dev/null \
    | awk '{printf "  %-40s %-10s %-8s %s\n", $1, $2, $5, $6}' \
    | tee -a "$LOG_FILE"

  echo "[파드 — Pending/ContainerCreating]" | tee -a "$LOG_FILE"
  kubectl get pods -A --no-headers 2>/dev/null \
    | grep -E "Pending|ContainerCreating|Terminating" \
    | awk '{printf "  %-30s %-40s %s\n", $1, $2, $4}' \
    | tee -a "$LOG_FILE"

  echo "[HPA 상태]" | tee -a "$LOG_FILE"
  kubectl get hpa -A --no-headers 2>/dev/null \
    | awk '{printf "  %-20s %-35s %s/%s (현재: %s)\n", $1, $2, $6, $7, $8}' \
    | tee -a "$LOG_FILE"

  echo "" | tee -a "$LOG_FILE"
  sleep "$INTERVAL"
done
