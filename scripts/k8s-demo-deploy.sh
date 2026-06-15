#!/usr/bin/env bash
# 호환용 wrapper입니다.
# k8s-demo dev는 직접 kubectl apply 하지 않고 Argo CD bootstrap만 수행합니다.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$ROOT_DIR/scripts/k8s-demo-dev-bootstrap-argocd.sh"
