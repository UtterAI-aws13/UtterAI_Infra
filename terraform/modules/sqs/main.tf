locals {
  prefix = "${var.project_name}-${var.environment}"
}

resource "aws_sqs_queue" "dlq" {
  name                      = "${local.prefix}-analysis-dlq"
  message_retention_seconds = 604800 # 7일
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
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = var.max_receive_count
  })

  tags = {
    Name = "${local.prefix}-analysis-queue"
  }
}
