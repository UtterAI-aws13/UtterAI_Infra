variable "project_name" {
  type = string
}

variable "environment" {
  type = string
}

variable "cluster_name" {
  type = string
}

variable "oidc_provider_arn" {
  type = string
}

variable "oidc_provider_url" {
  type = string
}

variable "aws_account_id" {
  type = string
}

variable "aws_region" {
  type = string
}

variable "raw_audio_bucket_arn" {
  type = string
}

variable "processed_audio_bucket_arn" {
  type = string
}

variable "documents_bucket_arn" {
  type = string
}

variable "reports_bucket_arn" {
  type = string
}

variable "artifacts_bucket_arn" {
  type = string
}

variable "frontend_bucket_arn" {
  type = string
}

variable "cpu_analysis_queue_arn" {
  type = string
}

variable "audio_ml_queue_arn" {
  type = string
}

variable "llm_queue_arn" {
  type = string
}

variable "cpu_analysis_dlq_arn" {
  type = string
}

variable "private_app_subnet_ids" {
  type = list(string)
}

variable "node_security_group_id" {
  type = string
}
