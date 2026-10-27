# SQSQueueBacklog 대응 런북

대상 알림: SQSQueueBacklog, QueueOldestMessageAge (stt-worker)

1. 큐 적체는 처리량 부족 신호다. 워커를 재시작하면 처리 중이던 메시지가 다시 보이게 될 뿐 적체는 줄지 않으므로 재시작하지 않는다.
2. KEDA ScaledObject의 현재 replica와 maxReplicaCount를 확인한다. 상한에 닿았다면 상한 조정을 검토한다.
3. Karpenter가 노드를 만들지 못하고 있다면 Pending Pod와 NodeClaim 이벤트를 확인한다.
4. DLQ에 메시지가 쌓이기 시작하면 실패 원인부터 확인하고, 원인 수정 전에는 redrive하지 않는다.
