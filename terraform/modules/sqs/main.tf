locals {
  prefix = "${var.project_name}-${var.environment}"
}

# ── Audio Preprocess Queue (cpu-worker) ───────────────────────────────────────

resource "aws_sqs_queue" "audio_preprocess_dlq" {
  name                      = "${local.prefix}-audio-preprocess-dlq"
  message_retention_seconds = 604800
  sqs_managed_sse_enabled   = true

  tags = {
    Name = "${local.prefix}-audio-preprocess-dlq"
  }
}

resource "aws_sqs_queue" "audio_preprocess" {
  name                       = "${local.prefix}-audio-preprocess-queue"
  visibility_timeout_seconds = var.audio_preprocess_visibility_timeout_seconds
  message_retention_seconds  = var.message_retention_seconds
  max_message_size           = 262144
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.audio_preprocess_dlq.arn
    maxReceiveCount     = var.max_receive_count
  })

  tags = {
    Name = "${local.prefix}-audio-preprocess-queue"
  }
}

# ── GPU Inference Queue (ml-gpu-worker) ──────────────────────────────────────

resource "aws_sqs_queue" "gpu_inference_dlq" {
  name                      = "${local.prefix}-gpu-inference-dlq"
  message_retention_seconds = 604800
  sqs_managed_sse_enabled   = true

  tags = {
    Name = "${local.prefix}-gpu-inference-dlq"
  }
}

resource "aws_sqs_queue" "gpu_inference" {
  name                       = "${local.prefix}-gpu-inference-queue"
  visibility_timeout_seconds = var.gpu_visibility_timeout_seconds
  message_retention_seconds  = var.message_retention_seconds
  max_message_size           = 262144
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.gpu_inference_dlq.arn
    maxReceiveCount     = var.gpu_max_receive_count
  })

  tags = {
    Name = "${local.prefix}-gpu-inference-queue"
  }
}

# ── Report Analysis Queue (ml-gpu-worker produce → cpu-worker consume → Bedrock) ──

resource "aws_sqs_queue" "report_analysis_dlq" {
  name                      = "${local.prefix}-report-analysis-dlq"
  message_retention_seconds = 604800
  sqs_managed_sse_enabled   = true

  tags = {
    Name = "${local.prefix}-report-analysis-dlq"
  }
}

resource "aws_sqs_queue" "report_analysis" {
  name                       = "${local.prefix}-report-analysis-queue"
  visibility_timeout_seconds = var.report_visibility_timeout_seconds
  message_retention_seconds  = var.message_retention_seconds
  max_message_size           = 262144
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.report_analysis_dlq.arn
    maxReceiveCount     = var.report_max_receive_count
  })

  tags = {
    Name = "${local.prefix}-report-analysis-queue"
  }
}

# ── Report Generation Queue (5-agent, ai-api produce → cpu-worker consume) ────
# cpu-worker의 세 번째 폴링 스레드가 이 큐를 소비한다 - 전용 워커를 새로
# 두지 않고 기존 cpu-worker pod에 얹었다 (worker 코드가 receive 시
# VisibilityTimeout=1800으로 직접 지정하고 heartbeat로 연장하므로, 아래
# 큐 레벨 기본값은 그 사이의 안전망 역할만 한다).

resource "aws_sqs_queue" "report_generation_dlq" {
  name                      = "${local.prefix}-report-generation-dlq"
  message_retention_seconds = 604800
  sqs_managed_sse_enabled   = true

  tags = {
    Name = "${local.prefix}-report-generation-dlq"
  }
}

resource "aws_sqs_queue" "report_generation" {
  name                       = "${local.prefix}-report-generation-queue"
  visibility_timeout_seconds = var.report_visibility_timeout_seconds
  message_retention_seconds  = var.message_retention_seconds
  max_message_size           = 262144
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.report_generation_dlq.arn
    maxReceiveCount     = var.report_max_receive_count
  })

  tags = {
    Name = "${local.prefix}-report-generation-queue"
  }
}

# ── RAG Ingest Queue (batch-worker) ──────────────────────────────────────────

resource "aws_sqs_queue" "rag_ingest_dlq" {
  name                      = "${local.prefix}-rag-ingest-dlq"
  message_retention_seconds = 604800
  sqs_managed_sse_enabled   = true

  tags = {
    Name = "${local.prefix}-rag-ingest-dlq"
  }
}

resource "aws_sqs_queue" "rag_ingest" {
  name                       = "${local.prefix}-rag-ingest-queue"
  visibility_timeout_seconds = var.visibility_timeout_seconds
  message_retention_seconds  = var.message_retention_seconds
  max_message_size           = 262144
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.rag_ingest_dlq.arn
    maxReceiveCount     = var.max_receive_count
  })

  tags = {
    Name = "${local.prefix}-rag-ingest-queue"
  }
}
