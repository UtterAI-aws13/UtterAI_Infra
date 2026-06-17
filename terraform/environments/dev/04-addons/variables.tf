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

variable "alertmanager_slack_enabled" {
  type        = bool
  default     = true
  description = "Enable Slack notifications from Alertmanager. Requires alertmanager-slack-webhook Secret in monitoring namespace."
}

variable "alertmanager_slack_channel" {
  type        = string
  default     = "#aws-alerts"
  description = "Slack channel used by Alertmanager when Slack notifications are enabled."
}

variable "alertmanager_slack_webhook_secret_name" {
  type        = string
  default     = "alertmanager-slack-webhook"
  description = "Kubernetes Secret name mounted into Alertmanager."
}

variable "alertmanager_slack_webhook_secret_key" {
  type        = string
  default     = "webhook-url"
  description = "Secret key containing the Slack incoming webhook URL."
}

variable "alertmanager_slack_secret_manager_name" {
  type        = string
  default     = "utterai-dev/alertmanager-slack-webhook"
  description = "AWS Secrets Manager secret name containing the Slack incoming webhook URL."
}

variable "grafana_admin_credentials_enabled" {
  type        = bool
  default     = true
  description = "Use an existing Kubernetes Secret synced from AWS Secrets Manager for Grafana admin credentials."
}

variable "grafana_admin_secret_manager_name" {
  type        = string
  default     = "utterai-dev/grafana-admin-credentials"
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
