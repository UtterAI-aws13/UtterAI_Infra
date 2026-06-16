variable "cluster_name" {
  type = string
}

variable "cluster_endpoint" {
  type = string
}

variable "aws_region" {
  type = string
}

variable "lbc_irsa_role_arn" {
  type = string
}

variable "cluster_autoscaler_irsa_role_arn" {
  type = string
}

variable "eso_irsa_role_arn" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "alertmanager_slack_enabled" {
  type        = bool
  default     = false
  description = "Enable Slack notifications from Alertmanager. Requires a Kubernetes Secret in the monitoring namespace."
}

variable "alertmanager_slack_channel" {
  type        = string
  default     = "#aws-alerts"
  description = "Slack channel used by Alertmanager when Slack notifications are enabled."
}

variable "alertmanager_slack_webhook_secret_name" {
  type        = string
  default     = "alertmanager-slack-webhook"
  description = "Kubernetes Secret name mounted into Alertmanager. The Secret must exist in the monitoring namespace."
}

variable "alertmanager_slack_webhook_secret_key" {
  type        = string
  default     = "webhook-url"
  description = "Secret key containing the Slack incoming webhook URL."
}

variable "alertmanager_slack_secret_manager_name" {
  type        = string
  description = "AWS Secrets Manager secret name containing the Slack incoming webhook URL as a plaintext secret string."
}

variable "grafana_admin_credentials_enabled" {
  type        = bool
  default     = false
  description = "Use an existing Kubernetes Secret synced from AWS Secrets Manager for Grafana admin credentials."
}

variable "grafana_admin_secret_manager_name" {
  type        = string
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

variable "external_secrets_cluster_store_name" {
  type        = string
  default     = "aws-secrets-manager"
  description = "Existing ClusterSecretStore name used by ExternalSecret resources."
}
