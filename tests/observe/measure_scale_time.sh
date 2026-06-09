#!/bin/bash
# 스케일링 소요 시간 측정 스크립트
#
# 부하 투입 시점부터 다음 단계까지 시간을 측정:
#   1) Pod Pending 최초 감지
#   2) 신규 노드 Ready
#   3) Pending Pod → Running
#
# 사용법:
#   ./measure_scale_time.sh --namespace utterai-batch --deployment utterai-batch-worker

NAMESPACE=${1:-utterai-batch}
DEPLOYMENT=${2:-utterai-batch-worker}

START=$(date +%s)
echo "[$(date)] 측정 시작 — $NAMESPACE/$DEPLOYMENT"
echo "초기 노드 수: $(kubectl get nodes --no-headers | wc -l)"
echo "초기 파드 수: $(kubectl get pods -n "$NAMESPACE" --no-headers | wc -l)"
echo ""

PENDING_TIME=""
NODE_READY_TIME=""
POD_RUNNING_TIME=""

while true; do
  NOW=$(date +%s)
  ELAPSED=$((NOW - START))

  # Pod Pending 감지
  if [ -z "$PENDING_TIME" ]; then
    PENDING=$(kubectl get pods -n "$NAMESPACE" --no-headers 2>/dev/null | grep -c "Pending")
    if [ "$PENDING" -gt 0 ]; then
      PENDING_TIME=$ELAPSED
      echo "[+${ELAPSED}s] Pod Pending 감지: ${PENDING}개"
    fi
  fi

  # 새 노드 Ready 감지 (초기 노드보다 증가)
  if [ -z "$NODE_READY_TIME" ] && [ -n "$PENDING_TIME" ]; then
    READY_NODES=$(kubectl get nodes --no-headers 2>/dev/null | grep -c " Ready")
    INITIAL_NODES=$(kubectl get nodes --no-headers 2>/dev/null | wc -l)
    if [ "$READY_NODES" -gt "$INITIAL_NODES" ] 2>/dev/null; then
      NODE_READY_TIME=$ELAPSED
      echo "[+${ELAPSED}s] 신규 노드 Ready (노드 추가 소요: $((ELAPSED - PENDING_TIME))초)"
    fi
  fi

  # 모든 Pod Running 확인
  if [ -z "$POD_RUNNING_TIME" ] && [ -n "$PENDING_TIME" ]; then
    STILL_PENDING=$(kubectl get pods -n "$NAMESPACE" --no-headers 2>/dev/null | grep -c "Pending")
    if [ "$STILL_PENDING" -eq 0 ]; then
      POD_RUNNING_TIME=$ELAPSED
      echo "[+${ELAPSED}s] 모든 Pod Running (Pending 해소 소요: $((ELAPSED - PENDING_TIME))초)"
    fi
  fi

  if [ -n "$PENDING_TIME" ] && [ -n "$POD_RUNNING_TIME" ]; then
    echo ""
    echo "===== 측정 결과 ====="
    echo "Pod Pending 감지:       +${PENDING_TIME}s"
    echo "노드 Ready:             +${NODE_READY_TIME:-미감지}s"
    echo "Pod Running:            +${POD_RUNNING_TIME}s"
    echo "전체 스케일아웃 시간:   $((POD_RUNNING_TIME - PENDING_TIME))초"
    break
  fi

  sleep 5
done
