# Real AWS. Reviewed with `terraform validate`/`plan` only; never applied without explicit
# approval of account, region, budget and Slack app. Differences from envs/local are intended:
# real credentials, externally managed secret values, reserved Lambda concurrency, ECS task.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"
    }
  }
  # backend "s3" { ... }  # configure per account before any apply
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]

  default_tags {
    tags = { project = "slack-llm-worker", env = "aws" }
  }
}

module "slack_worker" {
  source = "../../modules/slack_worker"

  name                         = var.name
  lambda_zip_path              = var.lambda_zip_path
  signing_secret_arn           = var.signing_secret_arn
  bot_secret_arn               = var.bot_secret_arn
  slack                        = var.slack
  ingress_reserved_concurrency = 2
  create_worker_task           = true
  worker_image                 = var.worker_image
  bedrock_model_arns           = var.bedrock_model_arns
  worker_env = {
    MODEL_PROVIDER   = "bedrock"
    BEDROCK_MODEL_ID = var.bedrock_model_id
  }
}

output "function_url" {
  value = module.slack_worker.function_url
}

output "worker_task_definition_arn" {
  value = module.slack_worker.worker_task_definition_arn
}
