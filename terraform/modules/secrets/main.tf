locals {
  prefix = "${var.project_name}-${var.environment}"
}

# ── Backend API Secret ────────────────────────────────────────────────────────
# Keys: DB_PASSWORD, JWT_SECRET_KEY, INTERNAL_CALLBACK_TOKEN, INTERNAL_CALLBACK_HMAC_SECRET

resource "aws_secretsmanager_secret" "backend_api" {
  name                    = "${local.prefix}/backend-api-secret"
  recovery_window_in_days = 0
}

# ── AI Worker Secret ──────────────────────────────────────────────────────────
# Keys: DATABASE_URL

resource "aws_secretsmanager_secret" "ai_worker" {
  name                    = "${local.prefix}/ai-worker-secret"
  recovery_window_in_days = 0
}

# ── GPU Worker Secret ─────────────────────────────────────────────────────────
# Keys: HF_TOKEN

resource "aws_secretsmanager_secret" "gpu_worker" {
  name                    = "${local.prefix}/gpu-worker-secret"
  recovery_window_in_days = 0
}
