variable "name" {
  description = "Resource name prefix."
  type        = string
}

variable "lambda_zip_path" {
  description = "Ingress Lambda package built by tools/package_lambda.py."
  type        = string
}

variable "signing_secret_arn" {
  description = "Secrets Manager secret holding {\"signing_secret\": ...}. Values are managed outside Terraform state."
  type        = string
}

variable "bot_secret_arn" {
  description = "Secrets Manager secret holding {\"bot_token\": ...}."
  type        = string
}

variable "alert_token_secret_arn" {
  description = "Secrets Manager secret holding {\"alert_token\": ...} for the Alertmanager webhook. Empty disables /alertmanager."
  type        = string
  default     = ""
}

variable "git_webhook_secret_arn" {
  description = "Secrets Manager secret holding {\"webhook_secret\": ...} for deploy-repository webhooks. Empty disables /git."
  type        = string
  default     = ""
}

variable "slack" {
  description = "Single-workspace allowlist passed to the ingress Lambda and worker."
  type = object({
    app_id   = string
    team_id  = string
    channels = list(string)
    users    = list(string)
    bot_id   = string
    # Channel that receives Alertmanager notifications; empty disables alerts.
    alert_channel = optional(string, "")
  })
}

variable "max_receive_count" {
  description = "SQS receives before DLQ. Counts receives, not failures: 429 waits and busy leases also consume it."
  type        = number
  default     = 8

  validation {
    condition     = var.max_receive_count >= 3
    error_message = "max_receive_count below 3 sends healthy bursts to the DLQ."
  }
}

variable "ingress_reserved_concurrency" {
  description = "Reserved concurrency for the ingress Lambda. null leaves it unreserved."
  type        = number
  default     = null
}

variable "create_worker_task" {
  description = "Create the ECS Fargate task definition. Local emulation runs the worker as a host process."
  type        = bool
  default     = false
}

variable "worker_image" {
  description = "Worker container image (required when create_worker_task is true)."
  type        = string
  default     = ""
}

variable "worker_env" {
  description = "Extra non-secret worker environment, e.g. MODEL_PROVIDER / MODEL_BASE_URL."
  type        = map(string)
  default     = {}
}

variable "bedrock_model_arns" {
  description = "Bedrock model/inference-profile ARNs the worker may invoke. Empty grants nothing."
  type        = list(string)
  default     = []
}

variable "pipeline_queue_arns" {
  description = "Queues (and DLQs) of the pipeline that ChatOps reports on. The worker may only read their attributes."
  type        = list(string)
  default     = []
}

variable "tags" {
  type    = map(string)
  default = {}
}
