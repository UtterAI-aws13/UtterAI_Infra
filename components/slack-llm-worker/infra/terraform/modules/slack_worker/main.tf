terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.40"
    }
  }
}

data "aws_region" "current" {}

locals {
  slack_env = {
    SLACK_APP_ID   = var.slack.app_id
    SLACK_TEAM_ID  = var.slack.team_id
    SLACK_CHANNELS = join(",", var.slack.channels)
    SLACK_USERS    = join(",", var.slack.users)
    SLACK_BOT_ID   = var.slack.bot_id
  }
}

# ── Queue ────────────────────────────────────────────────────────────────────

resource "aws_sqs_queue" "dlq" {
  name                      = "${var.name}-dlq"
  sqs_managed_sse_enabled   = true
  message_retention_seconds = 1209600
  tags                      = var.tags
}

resource "aws_sqs_queue" "jobs" {
  name                       = "${var.name}-jobs"
  sqs_managed_sse_enabled    = true
  message_retention_seconds  = 345600
  visibility_timeout_seconds = 90
  receive_wait_time_seconds  = 20
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = var.max_receive_count
  })
  tags = var.tags
}

# ── Durable dedup / lease / result state ─────────────────────────────────────

resource "aws_dynamodb_table" "state" {
  name         = "${var.name}-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  ttl {
    attribute_name = "expires"
    enabled        = true
  }

  server_side_encryption {
    enabled = true
  }

  tags = var.tags
}

# ── Ingress: signed Slack request → SQS, ACK only after SendMessage ──────────

resource "aws_cloudwatch_log_group" "ingress" {
  name              = "/aws/lambda/${var.name}-ingress"
  retention_in_days = 14
  tags              = var.tags
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ingress" {
  name               = "${var.name}-ingress"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = var.tags
}

data "aws_iam_policy_document" "ingress" {
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.jobs.arn]
  }
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.signing_secret_arn]
  }
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.ingress.arn}:*"]
  }
}

resource "aws_iam_role_policy" "ingress" {
  name   = "scoped-access"
  role   = aws_iam_role.ingress.id
  policy = data.aws_iam_policy_document.ingress.json
}

resource "aws_lambda_function" "ingress" {
  function_name                  = "${var.name}-ingress"
  role                           = aws_iam_role.ingress.arn
  runtime                        = "python3.12"
  handler                        = "slack_worker.runtime.lambda_handler"
  filename                       = var.lambda_zip_path
  source_code_hash               = filebase64sha256(var.lambda_zip_path)
  timeout                        = 3
  memory_size                    = 256
  reserved_concurrent_executions = var.ingress_reserved_concurrency

  environment {
    variables = merge(local.slack_env, {
      SLACK_SIGNING_SECRET_ARN = var.signing_secret_arn
      QUEUE_URL                = aws_sqs_queue.jobs.url
    })
  }

  depends_on = [aws_cloudwatch_log_group.ingress, aws_iam_role_policy.ingress]
  tags       = var.tags
}

resource "aws_lambda_function_url" "ingress" {
  function_name      = aws_lambda_function.ingress.function_name
  authorization_type = "NONE" # Slack cannot sign SigV4; the handler verifies the Slack HMAC signature.
}

# Public URLs need both statements since Oct 2025; LocalStack does not enforce them.
resource "aws_lambda_permission" "url_invoke" {
  statement_id           = "SlackPublicUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.ingress.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "url_function_invoke" {
  statement_id             = "SlackPublicUrlInvoke"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.ingress.function_name
  principal                = "*"
  invoked_via_function_url = true
}

# ── Worker task (ECS Fargate in AWS; host process in local emulation) ────────

resource "aws_cloudwatch_log_group" "worker" {
  count             = var.create_worker_task ? 1 : 0
  name              = "/ecs/${var.name}-worker"
  retention_in_days = 14
  tags              = var.tags
}

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "worker" {
  name               = "${var.name}-worker"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
  tags               = var.tags
}

data "aws_iam_policy_document" "worker" {
  statement {
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility"]
    resources = [aws_sqs_queue.jobs.arn]
  }
  statement {
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem"]
    resources = [aws_dynamodb_table.state.arn]
  }
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.bot_secret_arn]
  }
  dynamic "statement" {
    for_each = length(var.bedrock_model_arns) > 0 ? [1] : []
    content {
      actions   = ["bedrock:InvokeModel"]
      resources = var.bedrock_model_arns
    }
  }
}

resource "aws_iam_role_policy" "worker" {
  name   = "scoped-access"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker.json
}

resource "aws_iam_role" "execution" {
  count              = var.create_worker_task ? 1 : 0
  name               = "${var.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "execution" {
  count      = var.create_worker_task ? 1 : 0
  role       = aws_iam_role.execution[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_ecs_task_definition" "worker" {
  count                    = var.create_worker_task ? 1 : 0
  family                   = "${var.name}-worker"
  cpu                      = "256"
  memory                   = "512"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  task_role_arn            = aws_iam_role.worker.arn
  execution_role_arn       = aws_iam_role.execution[0].arn

  container_definitions = jsonencode([{
    name                   = "worker"
    image                  = var.worker_image
    essential              = true
    stopTimeout            = 120
    readonlyRootFilesystem = true
    environment = [for k, v in merge(local.slack_env, {
      AWS_DEFAULT_REGION   = data.aws_region.current.region
      QUEUE_URL            = aws_sqs_queue.jobs.url
      STATE_TABLE          = aws_dynamodb_table.state.name
      SLACK_BOT_SECRET_ARN = var.bot_secret_arn
      WORKER_CONCURRENCY   = "2"
      ENABLE_PAID_MODEL    = "disabled" # flipped only through a reviewed change after budget approval
    }, var.worker_env) : { name = k, value = v }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.worker[0].name
        awslogs-region        = data.aws_region.current.region
        awslogs-stream-prefix = "worker"
      }
    }
  }])

  lifecycle {
    precondition {
      condition     = var.worker_image != ""
      error_message = "worker_image is required when create_worker_task is true."
    }
  }

  tags = var.tags
}

# ── Alarms ───────────────────────────────────────────────────────────────────

resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  alarm_name          = "${var.name}-dlq-not-empty"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.dlq.name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  tags                = var.tags
}

resource "aws_cloudwatch_metric_alarm" "oldest_message_age" {
  alarm_name          = "${var.name}-oldest-message-age"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  dimensions          = { QueueName = aws_sqs_queue.jobs.name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 180
  treat_missing_data  = "notBreaching"
  tags                = var.tags
}
