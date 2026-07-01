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
  default = "dev"
}

variable "alb_dns_name" {
  type        = string
  default     = ""
  description = "ALB DNS name for CloudFront API proxy. Set after ALB is created."
}

variable "cloudfront_enabled" {
  type        = bool
  default     = false
  description = "Enable CloudFront after the backend Ingress creates an ALB. Keep false during the first addon apply."
}

variable "grafana_admin_credentials_enabled" {
  type        = bool
  default     = false
  description = "Use an existing Kubernetes Secret synced from AWS Secrets Manager for Grafana admin credentials."
}

variable "grafana_admin_secret_manager_name" {
  type        = string
  default     = "utterai-dev-tokyo/grafana-admin-credentials"
  description = "AWS Secrets Manager secret name containing Grafana admin credentials as a JSON object."
}

variable "grafana_admin_kubernetes_secret_name" {
  type        = string
  default     = "grafana-admin-credentials"
  description = "Kubernetes Secret name used by Grafana for admin credentials."
}

variable "grafana_admin_user_key" {
  type        = string
  default     = "admin-user"
  description = "Secret key containing the Grafana admin username."
}

variable "grafana_admin_password_key" {
  type        = string
  default     = "admin-password"
  description = "Secret key containing the Grafana admin password."
}

variable "kubecost_enabled" {
  type        = bool
  default     = true
  description = "Install Kubecost cost-analyzer for dev cost visibility."
}

variable "kubecost_persistent_volume_enabled" {
  type        = bool
  default     = false
  description = "Enable Kubecost PVC storage to keep cost history. Disabled when S3/Thanos is used."
}
