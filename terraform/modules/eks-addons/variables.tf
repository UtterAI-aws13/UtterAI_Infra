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

variable "kubecost_enabled" {
  type        = bool
  default     = true
  description = "Install Kubecost cost-analyzer for Kubernetes cost visibility."
}

variable "kubecost_chart_version" {
  type        = string
  default     = "2.8.6"
  description = "Kubecost cost-analyzer Helm chart version."
}

variable "kubecost_namespace" {
  type        = string
  default     = "kubecost"
  description = "Namespace where Kubecost is installed."
}

variable "kubecost_persistent_volume_enabled" {
  type        = bool
  default     = true
  description = "Enable Kubecost PVC storage to keep cost history."
}

variable "kubecost_persistent_volume_size" {
  type        = string
  default     = "10Gi"
  description = "Kubecost PVC size when persistent storage is enabled."
}

variable "kubecost_etl_daily_store_duration_days" {
  type        = number
  default     = 14
  description = "Number of days Kubecost keeps daily ETL data."
}

variable "kubecost_etl_hourly_store_duration_hours" {
  type        = number
  default     = 49
  description = "Number of hours Kubecost keeps hourly ETL data. Must stay below Prometheus retention."
}

variable "kubecost_s3_bucket_name" {
  type        = string
  default     = ""
  description = "S3 bucket name for Kubecost Thanos storage."
}

variable "kubecost_irsa_role_arn" {
  type        = string
  default     = ""
  description = "IAM Role ARN for Kubecost Service Account."
}

variable "loki_s3_bucket_name" {
  type        = string
  default     = ""
  description = "S3 bucket name for Loki storage."
}

variable "loki_irsa_role_arn" {
  type        = string
  default     = ""
  description = "IAM Role ARN for Loki Service Account."
}

variable "loki_retention_period" {
  type        = string
  default     = "168h"
  description = "Loki log retention period. Use Go duration format such as 168h or 336h."
}

variable "tempo_chart_version" {
  type        = string
  default     = "1.19.0"
  description = "Grafana Tempo Helm chart version."
}

variable "tempo_s3_bucket_name" {
  type        = string
  default     = ""
  description = "S3 bucket name for Tempo trace storage."
}

variable "tempo_irsa_role_arn" {
  type        = string
  default     = ""
  description = "IAM Role ARN for Tempo Service Account."
}

variable "tempo_retention_period" {
  type        = string
  default     = "24h"
  description = "Tempo trace retention period. Use Go duration format such as 24h or 72h."
}

variable "cluster_autoscaler_enabled" {
  type        = bool
  default     = true
  description = "Install Cluster Autoscaler. Set to false when switching to Karpenter."
}

variable "keda_enabled" {
  type        = bool
  default     = false
  description = "Install KEDA for SQS-based pod autoscaling."
}

variable "keda_irsa_role_arn" {
  type        = string
  default     = ""
  description = "IAM Role ARN for the KEDA operator Service Account."
}

variable "karpenter_enabled" {
  type        = bool
  default     = false
  description = "Install Karpenter for node autoscaling. Requires karpenter_irsa_role_arn."
}

variable "karpenter_irsa_role_arn" {
  type        = string
  default     = ""
  description = "IAM Role ARN for the Karpenter controller Service Account."
}

variable "efs_csi_driver_irsa_role_arn" {
  type        = string
  description = "IAM Role ARN for the EFS CSI Driver controller Service Account."
}
