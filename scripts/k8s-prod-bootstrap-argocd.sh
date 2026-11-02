#!/usr/bin/env bash
# Adopt existing prod Applications. Explicit context; Root remains manual-sync.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/gitops/bootstrap.py" --environment prod "$@"
