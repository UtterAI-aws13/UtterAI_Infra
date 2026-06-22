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

# ── RAG Ingest Secret ─────────────────────────────────────────────────────────
# Keys: DB_USER, DB_PASSWORD, DB_HOST, DB_PORT, DB_NAME

resource "aws_secretsmanager_secret" "rag_ingest" {
  count = var.rag_ingest_secret_enabled ? 1 : 0

  name                    = "${local.prefix}/rag-ingest-secret"
  recovery_window_in_days = 0
}

# ── GPU Worker Secret ─────────────────────────────────────────────────────────
# Keys: HF_TOKEN

resource "aws_secretsmanager_secret" "gpu_worker" {
  name                    = "${local.prefix}/gpu-worker-secret"
  recovery_window_in_days = 0
}

# ── Collect Papers Lambda Secret ──────────────────────────────────────────────
# Keys: PUBMED_EMAIL, PUBMED_API_KEY, SEMANTIC_SCHOLAR_API_KEY

resource "aws_secretsmanager_secret" "collect_papers" {
  name                    = "${local.prefix}/collect-papers-secret"
  recovery_window_in_days = 0
}
