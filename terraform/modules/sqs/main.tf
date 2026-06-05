locals {
  prefix = "${var.project_name}-${var.environment}"
}

# ── CPU Analysis Queue ────────────────────────────────────────────────────────

resource "aws_sqs_queue" "cpu_analysis_dlq" {
  name                      = "${local.prefix}-cpu-analysis-dlq"
  message_retention_seconds = 604800
  sqs_managed_sse_enabled   = true

  tags = {
    Name = "${local.prefix}-cpu-analysis-dlq"
  }
}

resource "aws_sqs_queue" "cpu_analysis" {
  name                       = "${local.prefix}-cpu-analysis-queue"
  visibility_timeout_seconds = var.visibility_timeout_seconds
  message_retention_seconds  = var.message_retention_seconds
  max_message_size           = 262144
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.cpu_analysis_dlq.arn
    maxReceiveCount     = var.max_receive_count
  })

  tags = {
    Name = "${local.prefix}-cpu-analysis-queue"
  }
}

# ── Audio ML Queue ────────────────────────────────────────────────────────────

resource "aws_sqs_queue" "audio_ml_dlq" {
  name                      = "${local.prefix}-audio-ml-dlq"
  message_retention_seconds = 604800
  sqs_managed_sse_enabled   = true

  tags = {
    Name = "${local.prefix}-audio-ml-dlq"
  }
}

resource "aws_sqs_queue" "audio_ml" {
  name                       = "${local.prefix}-audio-ml-queue"
  visibility_timeout_seconds = 900
  message_retention_seconds  = var.message_retention_seconds
  max_message_size           = 262144
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.audio_ml_dlq.arn
    maxReceiveCount     = var.max_receive_count
  })

  tags = {
    Name = "${local.prefix}-audio-ml-queue"
  }
}

# ── Analysis Queue (batch-worker / RAG ingest) ───────────────────────────────

resource "aws_sqs_queue" "analysis_dlq" {
  name                      = "${local.prefix}-analysis-dlq"
  message_retention_seconds = 604800
  sqs_managed_sse_enabled   = true

  tags = {
    Name = "${local.prefix}-analysis-dlq"
  }
}

resource "aws_sqs_queue" "analysis" {
  name                       = "${local.prefix}-analysis-queue"
  visibility_timeout_seconds = var.visibility_timeout_seconds
  message_retention_seconds  = var.message_retention_seconds
  max_message_size           = 262144
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.analysis_dlq.arn
    maxReceiveCount     = var.max_receive_count
  })

  tags = {
    Name = "${local.prefix}-analysis-queue"
  }
}

# ── LLM Queue ─────────────────────────────────────────────────────────────────

resource "aws_sqs_queue" "llm_dlq" {
  name                      = "${local.prefix}-llm-dlq"
  message_retention_seconds = 604800
  sqs_managed_sse_enabled   = true

  tags = {
    Name = "${local.prefix}-llm-dlq"
  }
}

resource "aws_sqs_queue" "llm" {
  name                       = "${local.prefix}-llm-queue"
  visibility_timeout_seconds = 900
  message_retention_seconds  = var.message_retention_seconds
  max_message_size           = 262144
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.llm_dlq.arn
    maxReceiveCount     = var.max_receive_count
  })

  tags = {
    Name = "${local.prefix}-llm-queue"
  }
}
