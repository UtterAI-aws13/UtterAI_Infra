#!/usr/bin/env bash
# k8s dev Argo CD 리소스 정리 스크립트
#
# AWS 인프라 자체를 destroy하지 않고, Argo CD Application과 dev 앱 namespace를
# 정리합니다. EKS/RDS/Redis/S3/SQS/ECR은 그대로 둡니다.

set -euo pipefail

AWS_REGION="${AWS_REGION:-ap-northeast-2}"
CLUSTER_NAME="${CLUSTER_NAME:-utterai-dev-eks}"

require_command() {
  local command_name="$1"
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "ERROR: $command_name 명령어가 필요합니다."
    exit 1
  fi
}

require_command aws
require_command kubectl

echo "== UtterAI k8s dev Argo CD delete =="

aws eks update-kubeconfig \
  --region "$AWS_REGION" \
  --name "$CLUSTER_NAME" \
  --alias "$CLUSTER_NAME"
kubectl config current-context
echo ""

echo "Argo CD Application 삭제:"
kubectl delete application utterai-ai-worker-dev -n argocd --ignore-not-found=true
kubectl delete application utterai-backend-dev -n argocd --ignore-not-found=true
kubectl delete application utterai-platform-dev -n argocd --ignore-not-found=true
echo ""

echo "dev 앱 namespace 삭제:"
kubectl delete namespace utterai-api --ignore-not-found=true
kubectl delete namespace utterai-ai-api --ignore-not-found=true
kubectl delete namespace utterai-ai-cpu --ignore-not-found=true
kubectl delete namespace utterai-ai-gpu --ignore-not-found=true
kubectl delete namespace utterai-batch --ignore-not-found=true
kubectl delete namespace utterai-observability --ignore-not-found=true
echo ""

echo "정리 완료"
