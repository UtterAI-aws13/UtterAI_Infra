#!/usr/bin/env bash
# k8s-demo dev Argo CD bootstrap 스크립트
#
# 이 스크립트는 애플리케이션 manifest를 직접 kubectl apply 하지 않습니다.
# dev EKS 클러스터에 Argo CD를 준비하고, Argo CD Application만 등록합니다.
# 실제 backend/AI worker/platform 리소스 반영은 Argo CD가 Git의 k8s-demo 경로를
# 바라보고 sync하면서 수행합니다.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
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

cd "$ROOT_DIR"

echo "== UtterAI k8s-demo dev Argo CD bootstrap =="
echo "Repo root: $ROOT_DIR"
echo "AWS region: $AWS_REGION"
echo "Cluster name: $CLUSTER_NAME"
echo ""

echo "kubeconfig 갱신:"
aws eks update-kubeconfig \
  --region "$AWS_REGION" \
  --name "$CLUSTER_NAME" \
  --alias "$CLUSTER_NAME"
kubectl config current-context
kubectl get nodes
echo ""

echo "Argo CD namespace 준비:"
kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -
echo ""

echo "Argo CD 설치/업데이트:"
kubectl apply \
  -n argocd \
  -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
echo ""

echo "Argo CD rollout 대기:"
kubectl rollout status deployment/argocd-server -n argocd --timeout=300s
kubectl rollout status deployment/argocd-repo-server -n argocd --timeout=300s
kubectl rollout status statefulset/argocd-application-controller -n argocd --timeout=300s
echo ""

echo "Argo CD dev Application 등록:"
kubectl apply -f deploy/argocd/dev/platform-dev.yaml
kubectl apply -f deploy/argocd/dev/backend-dev.yaml
kubectl apply -f deploy/argocd/dev/ai-worker-dev.yaml
echo ""

echo "Argo CD Applications:"
kubectl get applications -n argocd
echo ""

echo "완료: Argo CD가 Git의 k8s-demo dev 경로를 sync합니다."
echo ""
echo "확인 명령:"
echo "  kubectl get applications -n argocd"
echo "  kubectl describe application utterai-backend-dev -n argocd"
echo "  kubectl describe application utterai-ai-worker-dev -n argocd"
echo "  kubectl describe application utterai-platform-dev -n argocd"
echo ""
echo "UI 접속:"
echo "  kubectl port-forward svc/argocd-server -n argocd 8080:443"
