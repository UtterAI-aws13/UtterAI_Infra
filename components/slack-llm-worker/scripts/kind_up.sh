#!/usr/bin/env bash
# Local Kubernetes target for the ChatOps scenarios. Uses its own kubeconfig so the
# current kubectl context (for example an EKS cluster) is never switched or modified.
set -euo pipefail
cd "$(dirname "$0")/.."

CLUSTER=slack-ops
OUT=e2e/.kube.local
mkdir -p "$OUT" && chmod 700 "$OUT"
export KUBECONFIG="$PWD/$OUT/kubeconfig"

kind get clusters | grep -qx "$CLUSTER" || kind create cluster --name "$CLUSTER" --kubeconfig "$KUBECONFIG" --wait 120s
kind export kubeconfig --name "$CLUSTER" --kubeconfig "$KUBECONFIG" >/dev/null

kubectl apply -f e2e/kind/manifests.yaml >/dev/null
kubectl -n utterai rollout status deploy/api --timeout=120s >/dev/null
kubectl -n utterai rollout status deploy/db --timeout=120s >/dev/null

# What the worker gets: API address, cluster CA and a short-lived ServiceAccount token.
# Not the admin kubeconfig above.
kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.server}' > "$OUT/server"
kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' | base64 -d > "$OUT/ca.crt"
kubectl -n utterai create token slack-ops --duration=2h > "$OUT/token"
chmod 600 "$OUT"/*
echo "kind cluster '$CLUSTER' ready; worker credentials in $OUT (gitignored)"
