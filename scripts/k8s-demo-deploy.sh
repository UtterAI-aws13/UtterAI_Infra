#!/bin/bash
# k8s-demo Kustomize + Argo CD dev bootstrap 스크립트
#
# 기존 scripts/k8s-deploy.sh는 k8s/ raw manifest를 직접 kubectl apply 합니다.
# 이 스크립트는 k8s-demo/ Kustomize 구조와 deploy/argocd/dev Application을 사용합니다.
#
# 전제:
# - dev EKS kubeconfig가 현재 kubectl context로 연결되어 있어야 합니다.
# - Terraform 04-addons가 적용되어 Argo CD, External Secrets Operator 등이 설치되어 있어야 합니다.
# - Argo CD Application은 GitHub main의 k8s-demo path를 바라봅니다.
#   따라서 k8s-demo 변경사항이 GitHub main에 반영된 뒤 실행해야 합니다.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "== UtterAI k8s-demo dev deploy =="
echo "Repo root: $ROOT_DIR"
echo ""

require_command() {
  local command_name="$1"
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "ERROR: $command_name 명령어가 필요합니다."
    exit 1
  fi
}

require_kustomize_render() {
  local path="$1"
  local output="$2"

  echo "Render check: $path"
  kubectl kustomize "$path" > "$output"
}

require_command kubectl

echo "Current kubectl context:"
kubectl config current-context
echo ""

echo "Cluster access check:"
kubectl get nodes
echo ""

echo "Argo CD install check:"
if ! kubectl get namespace argocd >/dev/null 2>&1; then
  echo "ERROR: argocd namespace가 없습니다."
  echo "먼저 Terraform 04-addons apply로 Argo CD를 설치해야 합니다."
  exit 1
fi

kubectl wait \
  --for=condition=available deployment/argocd-server \
  -n argocd \
  --timeout=300s
echo ""

echo "Kustomize render validation:"
require_kustomize_render "k8s-demo/platform/external-secrets/base" "/tmp/utterai-platform-external-secrets.yaml"
require_kustomize_render "k8s-demo/platform/observability/base" "/tmp/utterai-platform-observability.yaml"
require_kustomize_render "k8s-demo/apps/backend/overlays/dev" "/tmp/utterai-backend-dev.yaml"
require_kustomize_render "k8s-demo/apps/ai-worker/overlays/dev" "/tmp/utterai-ai-worker-dev.yaml"
echo "Render OK"
echo ""

echo "Apply platform resources:"
echo "- ClusterSecretStore"
kubectl apply -k k8s-demo/platform/external-secrets/base

echo "- OpenTelemetry Collector"
kubectl apply -k k8s-demo/platform/observability/base
echo ""

echo "Register Argo CD dev Applications:"
kubectl apply -f deploy/argocd/dev/backend-dev.yaml
kubectl apply -f deploy/argocd/dev/ai-worker-dev.yaml
echo ""

echo "Argo CD Applications:"
kubectl get applications -n argocd
echo ""

echo "Namespace check:"
kubectl get ns utterai-api utterai-ai-api utterai-ai-cpu utterai-ai-gpu utterai-batch utterai-observability --ignore-not-found
echo ""

echo "완료: Argo CD dev Application 등록이 끝났습니다."
echo "UI 또는 kubectl로 sync 상태를 확인하세요."
echo ""
echo "확인 명령:"
echo "  kubectl get applications -n argocd"
echo "  kubectl get pods -n utterai-api"
echo "  kubectl get pods -n utterai-ai-api"
echo "  kubectl get pods -n utterai-ai-cpu"
echo "  kubectl get pods -n utterai-ai-gpu"
echo "  kubectl get pods -n utterai-batch"
