# LocalStack only. allowed_account_ids pins the emulator's fixed account, so a misrouted
# credential or endpoint fails at plan time instead of creating real AWS resources.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"
    }
  }
}

locals {
  endpoint = "http://localhost:4566"
  slack = {
    app_id   = "A_LOCAL"
    team_id  = "T_LOCAL"
    channels = ["C_LOCAL1", "C_LOCAL2", "C_LOCAL3", "C_LOCAL4", "C_LOCAL5"]
    users    = ["U_LOCAL"]
    bot_id   = "B_LOCAL"
  }
  # Test-only values for the emulator. Real secrets are never written by Terraform.
  signing_secret = "local-test-signing-secret"
  bot_token      = "xoxb-local-test-only"
}

provider "aws" {
  region                      = "ap-northeast-2"
  access_key                  = "test"
  secret_key                  = "test"
  allowed_account_ids         = ["000000000000"]
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  s3_use_path_style           = true

  endpoints {
    cloudwatch     = local.endpoint
    dynamodb       = local.endpoint
    ecs            = local.endpoint
    iam            = local.endpoint
    lambda         = local.endpoint
    logs           = local.endpoint
    s3             = local.endpoint
    secretsmanager = local.endpoint
    sqs            = local.endpoint
    sts            = local.endpoint
  }

  default_tags {
    tags = { project = "slack-llm-worker", env = "local" }
  }
}

resource "aws_secretsmanager_secret" "signing" {
  name                    = "slack-worker-local/signing"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "signing" {
  secret_id     = aws_secretsmanager_secret.signing.id
  secret_string = jsonencode({ signing_secret = local.signing_secret })
}

resource "aws_secretsmanager_secret" "bot" {
  name                    = "slack-worker-local/bot"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "bot" {
  secret_id     = aws_secretsmanager_secret.bot.id
  secret_string = jsonencode({ bot_token = local.bot_token })
}

module "slack_worker" {
  source = "../../modules/slack_worker"

  name               = "slack-worker-local"
  lambda_zip_path    = "${path.module}/../../../../dist/lambda.zip"
  signing_secret_arn = aws_secretsmanager_secret.signing.arn
  bot_secret_arn     = aws_secretsmanager_secret.bot.arn
  slack              = local.slack
  create_worker_task = false

  depends_on = [aws_secretsmanager_secret_version.signing]
}

output "function_url" {
  value = module.slack_worker.function_url
}

output "queue_url" {
  value = module.slack_worker.queue_url
}

output "dlq_url" {
  value = module.slack_worker.dlq_url
}

output "state_table" {
  value = module.slack_worker.state_table
}

output "bot_secret_arn" {
  value = aws_secretsmanager_secret.bot.arn
}

output "slack" {
  value = local.slack
}

output "signing_secret" {
  value     = local.signing_secret
  sensitive = true
}
