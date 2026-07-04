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

variable "template_bucket_arn" {
  type = string
}

variable "rag_ingest_bucket_arn" {
  type = string
}

variable "reports_bucket_arn" {
  type = string
}

variable "transcripts_bucket_arn" {
  type = string
}

variable "frontend_bucket_arn" {
  type = string
}

variable "audio_preprocess_queue_arn" {
  type = string
}

variable "gpu_inference_queue_arn" {
  type = string
}

variable "report_analysis_queue_arn" {
  type = string
}

variable "report_generation_queue_arn" {
  type = string
}

variable "report_generation_gateway_arn_pattern" {
  type        = string
  description = "Evidence Research Agent가 호출하는 AgentCore Gateway ARN 패턴 (wildcard)"
}

variable "audio_preprocess_dlq_arn" {
  type = string
}

variable "rag_ingest_queue_arn" {
  type = string
}

variable "rag_ingest_dlq_arn" {
  type = string
}

variable "private_app_subnet_ids" {
  type = list(string)
}

variable "node_security_group_id" {
  type = string
}

variable "api_namespace" {
  type        = string
  description = "K8s namespace where the API service account lives"
}

variable "kubecost_bucket_arn" {
  type = string
}

variable "loki_bucket_arn" {
  type = string
}

variable "tempo_bucket_arn" {
  type = string
}

variable "tempo_enabled" {
  type        = bool
  description = "Whether to create Tempo IRSA resources."
  default     = true
}
