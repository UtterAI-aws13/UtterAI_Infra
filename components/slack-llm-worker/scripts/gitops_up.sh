#!/usr/bin/env bash
# GitOps lab on top of scripts/kind_up.sh and scripts/local_up.sh: Argo CD (core) in the kind
# cluster, a Gitea server on the kind Docker network as the deploy repository, and two builds
# of a stand-in batch worker (one healthy, one that exits on startup).
set -euo pipefail
cd "$(dirname "$0")/.."

ARGOCD_VERSION=v2.14.11
GITEA_IMAGE=gitea/gitea:1.22.6
export KUBECONFIG="$PWD/e2e/.kube.local/kubeconfig"

docker build -q -t utterai/batch-worker:prod-aa314fc e2e/gitops/images/good >/dev/null
docker build -q -t utterai/batch-worker:prod-6e134f1 e2e/gitops/images/bad >/dev/null
docker build -q -t utterai/batch-worker:prod-79e4740 e2e/gitops/images/hotfix >/dev/null
kind load docker-image utterai/batch-worker:prod-aa314fc utterai/batch-worker:prod-6e134f1 \
  utterai/batch-worker:prod-79e4740 --name slack-ops >/dev/null

kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl apply -n argocd --server-side --force-conflicts \
  -f "https://raw.githubusercontent.com/argoproj/argo-cd/${ARGOCD_VERSION}/manifests/core-install.yaml" >/dev/null
# Poll Git every 15s instead of 3 min so a merged PR reaches the cluster quickly in the lab.
kubectl -n argocd patch configmap argocd-cm --type merge -p '{"data":{"timeout.reconciliation":"15s"}}' >/dev/null
kubectl -n argocd rollout status statefulset/argocd-application-controller --timeout=180s >/dev/null
kubectl -n argocd rollout status deploy/argocd-repo-server --timeout=180s >/dev/null

# Gitea delivers webhooks to the LocalStack Function URL, whose hostname resolves to 127.0.0.1;
# inside the container that would be the container itself, so map it to the Docker host.
FN_HOST=$(terraform -chdir=infra/terraform/envs/local output -raw function_url | sed -E 's#^https?://([^:/]+).*#\1#')
docker rm -f slack-ops-gitea >/dev/null 2>&1 || true
docker run -d --name slack-ops-gitea --network kind -p 127.0.0.1:3300:3000 \
  --add-host "${FN_HOST}:host-gateway" \
  -e GITEA__security__INSTALL_LOCK=true \
  -e GITEA__server__ROOT_URL=http://slack-ops-gitea:3000/ \
  -e GITEA__database__DB_TYPE=sqlite3 \
  -e GITEA__webhook__ALLOWED_HOST_LIST='*' \
  -e GITEA__service__DISABLE_REGISTRATION=true \
  "$GITEA_IMAGE" >/dev/null
for _ in $(seq 1 60); do curl -sf http://127.0.0.1:3300/api/healthz >/dev/null && break; sleep 2; done
docker exec -u git slack-ops-gitea gitea admin user create --admin --username ops-admin \
  --password 'local-admin-test-only' --email admin@example.test --must-change-password=false >/dev/null

.venv/bin/python e2e/gitops_bootstrap.py
