variable "project_name" {
  type = string
}

variable "environment" {
  type = string
}

variable "aws_account_id" {
  type = string
}

variable "aws_region" {
  type = string
}

variable "sqs_data_event_queue_arns" {
  type        = list(string)
  description = "SQS queue ARNs to log ReceiveMessage/DeleteMessage/SendMessage data events for (caller identity forensics)."
  default     = []
}

variable "log_retention_days" {
  type    = number
  default = 30
}
