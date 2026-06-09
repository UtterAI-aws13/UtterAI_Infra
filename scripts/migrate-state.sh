#!/usr/bin/env bash
# Terraform state migration: single root → 4 layered roots
# Run from repo root: bash scripts/migrate-state.sh
# Prerequisites: AWS credentials configured, terraform installed

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEV_DIR="$REPO_ROOT/terraform/environments/dev"
WORK_STATE="/tmp/utterai-dev-work.tfstate"

echo "=== Terraform State Migration: Single Root → 4 Layers ==="
echo ""

# ── Step 0: Backup current state ────────────────────────────────────────────

echo "[0/5] Pulling current state from S3..."
cd "$DEV_DIR"
terraform state pull > "${WORK_STATE}.bak"
cp "${WORK_STATE}.bak" "$WORK_STATE"
echo "      Backup saved to: ${WORK_STATE}.bak"
echo ""

# Move a module only if it exists in the source state
# Args: <source-state> <dest-state> <module-name>
move_if_exists() {
  local src="$1"
  local dst="$2"
  local mod="$3"

  if grep -q "\"module\": \"${mod}\"" "$src" 2>/dev/null; then
    terraform state mv -state="$src" -state-out="$dst" "$mod" "$mod"
    echo "      Moved: $mod"
  else
    echo "      Skipped (not yet deployed): $mod"
  fi
}

# ── Step 1: 01-network ───────────────────────────────────────────────────────

echo "[1/5] Initializing 01-network..."
cd "$DEV_DIR/01-network"
terraform init -reconfigure

move_if_exists "$WORK_STATE" ./terraform.tfstate module.vpc

if [ -f ./terraform.tfstate ]; then
  terraform state push ./terraform.tfstate
  rm -f ./terraform.tfstate
  echo "      01-network state pushed to S3."
else
  echo "      No existing resources to migrate for 01-network (fresh deploy)."
fi
echo ""

# ── Step 2: 02-eks ───────────────────────────────────────────────────────────

echo "[2/5] Initializing 02-eks..."
cd "$DEV_DIR/02-eks"
terraform init -reconfigure

move_if_exists "$WORK_STATE" ./terraform.tfstate module.eks

if [ -f ./terraform.tfstate ]; then
  terraform state push ./terraform.tfstate
  rm -f ./terraform.tfstate
  echo "      02-eks state pushed to S3."
else
  echo "      No existing resources to migrate for 02-eks (fresh deploy)."
fi
echo ""

# ── Step 3: 03-services ──────────────────────────────────────────────────────

echo "[3/5] Migrating 03-services (rds, redis, s3, sqs, secrets, ecr, irsa)..."
cd "$DEV_DIR/03-services"
terraform init -reconfigure

for mod in module.rds module.redis module.s3 module.sqs module.secrets module.ecr module.irsa; do
  move_if_exists "$WORK_STATE" ./terraform.tfstate "$mod"
done

if [ -f ./terraform.tfstate ]; then
  terraform state push ./terraform.tfstate
  rm -f ./terraform.tfstate
  echo "      03-services state pushed to S3."
else
  echo "      No existing resources to migrate for 03-services (fresh deploy)."
fi
echo ""

# ── Step 4: 04-addons ────────────────────────────────────────────────────────

echo "[4/5] Initializing 04-addons..."
cd "$DEV_DIR/04-addons"
terraform init -reconfigure

for mod in module.eks_addons module.cloudfront; do
  move_if_exists "$WORK_STATE" ./terraform.tfstate "$mod"
done

if [ -f ./terraform.tfstate ]; then
  terraform state push ./terraform.tfstate
  rm -f ./terraform.tfstate
  echo "      04-addons state pushed to S3."
else
  echo "      No existing resources to migrate for 04-addons (fresh deploy)."
fi
echo ""

# ── Step 5: Summary ──────────────────────────────────────────────────────────

echo "[5/5] Migration complete."
echo ""
echo "State migration done. Remaining resources in old state (if any):"
REMAINING=$(grep -o '"module": "[^"]*"' "$WORK_STATE" 2>/dev/null | sort -u | wc -l | tr -d '[:space:]')
if [ "${REMAINING:-0}" -gt 0 ]; then
  echo "  WARNING: Some modules were not migrated:"
  grep -o '"module": "[^"]*"' "$WORK_STATE" | sort -u
else
  echo "  All resources migrated."
fi

echo ""
echo "Next steps (in order):"
echo "  1. Deploy base infra first:"
echo "     cd terraform/environments/dev/01-network && terraform apply"
echo "     cd terraform/environments/dev/02-eks     && terraform apply"
echo "     cd terraform/environments/dev/03-services && terraform apply"
echo "  2. Deploy addons after EKS is ready:"
echo "     cd terraform/environments/dev/04-addons  && terraform apply"
echo ""
echo "  After all layers verified:"
echo "  rm $DEV_DIR/main.tf $DEV_DIR/variables.tf $DEV_DIR/outputs.tf"
echo "  rm $DEV_DIR/terraform.tf $DEV_DIR/terraform.tfvars $DEV_DIR/.terraform.lock.hcl"
echo "  (old S3 state at dev/terraform.tfstate is preserved as rollback)"
