#!/bin/bash
# 스케일링 관찰 스크립트 (Phase 1: CA+HPA / Phase 2: Karpenter+KEDA 공용)
#
# 10초마다 노드/파드 상태와 HPA(또는 ScaledObject) 상태를 타임스탬프와 함께 기록.
# Phase 1과 Phase 2 모두 동일 스크립트로 실행해 결과를 비교한다.
#
# 사용법:
#   chmod +x watch_scaling.sh
#   # Phase 1
#   ./watch_scaling.sh 2>&1 | tee docs/dev/results/phase1_$(date +%Y%m%d_%H%M%S).log
#   # Phase 2 (전환 후)
#   ./watch_scaling.sh 2>&1 | tee docs/dev/results/phase2_$(date +%Y%m%d_%H%M%S).log

INTERVAL=10
mkdir -p docs/dev/results

echo "===== 스케일링 관찰 시작: $(date) ====="
echo "기록 주기: ${INTERVAL}초"
echo "Ctrl+C로 종료"
echo ""

while true; do
  TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

  echo "━━━━━━ $TIMESTAMP ━━━━━━"

  echo "[노드]"
  kubectl get nodes -o wide --no-headers 2>/dev/null \
    | awk '{printf "  %-40s %-10s %-8s %s\n", $1, $2, $5, $6}'

  echo "[파드 — Pending/ContainerCreating/Terminating]"
  kubectl get pods -A --no-headers 2>/dev/null \
    | grep -E "Pending|ContainerCreating|Terminating" \
    | awk '{printf "  %-30s %-40s %s\n", $1, $2, $4}'

  echo "[HPA 상태]"
  kubectl get hpa -A --no-headers 2>/dev/null \
    | awk '{printf "  %-20s %-35s %s/%s (현재: %s)\n", $1, $2, $6, $7, $8}'

  # Phase 2 전환 후: KEDA ScaledObject 상태 추가 관찰
  KEDA_EXISTS=$(kubectl get scaledobject -A --no-headers 2>/dev/null | wc -l)
  if [ "$KEDA_EXISTS" -gt 0 ]; then
    echo "[KEDA ScaledObject 상태]"
    kubectl get scaledobject -A --no-headers 2>/dev/null \
      | awk '{printf "  %-20s %-35s ready=%s active=%s\n", $1, $2, $4, $5}'
  fi

  echo ""
  sleep "$INTERVAL"
done
