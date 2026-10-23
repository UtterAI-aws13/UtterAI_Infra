#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
mkdir -p tests/capacity-policy/results
export CAPACITY_LAB_KUBECONFIG="${CAPACITY_LAB_KUBECONFIG:-/private/tmp/utterai-capacity-kubeconfig}"
export HELM_CONFIG_HOME=/private/tmp/utterai-helm-config
export HELM_CACHE_HOME=/private/tmp/utterai-helm-cache
export HELM_DATA_HOME=/private/tmp/utterai-helm-data
# An existing cluster with this task-specific name is reused, never a user's EKS context.
if ! kind get clusters | rg -qx utterai-capacity-lab; then
  kind create cluster --name utterai-capacity-lab --image kindest/node:v1.31.9 \
    --config scripts/capacity-policy/kind.yaml --kubeconfig "$CAPACITY_LAB_KUBECONFIG" --wait 120s
fi
docker build -t utterai-capacity-policy:lab components/capacity-policy
kind load docker-image utterai-capacity-policy:lab --name utterai-capacity-lab
python3 scripts/capacity-policy/local_cluster.py
K=(kubectl --kubeconfig "$CAPACITY_LAB_KUBECONFIG" --context kind-utterai-capacity-lab)
"${K[@]}" delete scaledobject demo -n capacity-test --ignore-not-found 2>/dev/null || true
"${K[@]}" delete deployment demo -n capacity-test --ignore-not-found
"${K[@]}" rollout restart deployment/capacity-policy -n capacity-system
"${K[@]}" rollout status deployment/capacity-policy -n capacity-system --timeout=120s
python3 -m unittest discover -s tests/capacity-policy -v
python3 tests/capacity-policy/integration.py
helm upgrade --install keda keda --repo https://kedacore.github.io/charts --version 2.16.1 \
  --namespace keda --create-namespace --kubeconfig "$CAPACITY_LAB_KUBECONFIG" \
  --kube-context kind-utterai-capacity-lab --wait --timeout 180s
"${K[@]}" apply -f tests/capacity-policy/elasticmq.yaml
"${K[@]}" rollout status deployment/elasticmq -n capacity-test --timeout=120s
python3 tests/capacity-policy/keda_integration.py
python3 scripts/capacity-policy/validate_schema.py
python3 tests/capacity-policy/visibility_integration.py
python3 scripts/capacity-policy/measure_local.py
# Cleanup is explicit so the caller can inspect the cluster before deleting it.
printf '%s\n' 'Local validation complete. Cleanup:' \
  "kind delete cluster --name utterai-capacity-lab --kubeconfig $CAPACITY_LAB_KUBECONFIG"
