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

variable "cloudfront_aliases" {
  type        = list(string)
  default     = []
  description = "Custom domain names for the prod CloudFront distribution, for example app.utterai.org."
}

variable "cloudfront_acm_certificate_arn" {
  type        = string
  default     = ""
  description = "Existing us-east-1 ACM certificate ARN for CloudFront. Leave empty to create and validate one with Route53."
}

variable "route53_hosted_zone_id" {
  type        = string
  default     = ""
  description = "Public Route53 Hosted Zone ID used for ACM DNS validation and CloudFront alias records."
}

variable "grafana_admin_credentials_enabled" {
  type        = bool
  default     = true
  description = "Use an existing Kubernetes Secret synced from AWS Secrets Manager for Grafana admin credentials."
}

variable "grafana_admin_secret_manager_name" {
  type        = string
  default     = "utterai-prod/grafana-admin-credentials"
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
  description = "Install Kubecost cost-analyzer for prod cost visibility."
}

variable "kubecost_persistent_volume_enabled" {
  type        = bool
  default     = true
  description = "Enable Kubecost PVC storage in prod so cost data survives pod restarts."
}
