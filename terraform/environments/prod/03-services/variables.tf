variable "aws_region" {
  type    = string
  default = "ap-northeast-2"
}

variable "project_name" {
  type    = string
  default = "utterai"
}

variable "environment" {
  type    = string
  default = "prod"
}

variable "cluster_name" {
  type = string
}

variable "rds_instance_class" {
  type = string
}

variable "redis_node_type" {
  type = string
}

variable "redis_num_cache_nodes" {
  type    = number
  default = 2
}

variable "frontend_domain" {
  type        = string
  description = "CloudFront domain for S3 CORS (set after CloudFront is created)"
}

variable "allowed_extra_origins" {
  type        = list(string)
  description = "Additional allowed CORS origins (e.g. localhost for local dev)"
  default     = []
}

variable "kubecost_alb_endpoint" {
  type        = string
  description = "Internal ALB DNS for Kubecost (http://<alb-dns>). Set after ArgoCD syncs the Ingress."
  default     = ""
}

variable "spot_tracking_start_date" {
  type        = string
  description = "First production date included in cumulative Spot savings queries (YYYY-MM-DD)."
  default     = "2026-07-01"

  validation {
    condition     = can(regex("^\\d{4}-\\d{2}-\\d{2}$", var.spot_tracking_start_date))
    error_message = "spot_tracking_start_date must use YYYY-MM-DD."
  }
}
