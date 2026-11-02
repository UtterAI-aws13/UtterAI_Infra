#!/usr/bin/env bash
# Adopt existing dev Applications. Explicit context; never changes current kubeconfig.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/gitops/bootstrap.py" --environment dev "$@"
