variable "project_name" {
  type = string
}

variable "environment" {
  type = string
}

variable "frontend_bucket_id" {
  type = string
}

variable "frontend_bucket_arn" {
  type = string
}

variable "alb_dns_name" {
  type        = string
  default     = ""
  description = "ALB DNS name for API proxy behavior (/api/*). Leave empty to skip."
}
