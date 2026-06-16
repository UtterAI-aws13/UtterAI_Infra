output "lbc_release_name" {
  value = helm_release.aws_load_balancer_controller.name
}

output "cluster_autoscaler_release_name" {
  value = helm_release.cluster_autoscaler.name
}

output "metrics_server_release_name" {
  value = helm_release.metrics_server.name
}

output "kube_prometheus_stack_release_name" {
  value = helm_release.kube_prometheus_stack.name
}

output "loki_release_name" {
  value = helm_release.loki.name
}

output "promtail_release_name" {
  value = helm_release.promtail.name
}

output "alertmanager_slack_secret_manager_name" {
  value = aws_secretsmanager_secret.alertmanager_slack_webhook.name
}

output "alertmanager_slack_secret_manager_arn" {
  value = aws_secretsmanager_secret.alertmanager_slack_webhook.arn
}

output "grafana_admin_secret_manager_name" {
  value = aws_secretsmanager_secret.grafana_admin_credentials.name
}

output "grafana_admin_secret_manager_arn" {
  value = aws_secretsmanager_secret.grafana_admin_credentials.arn
}

output "external_secrets_release_name" {
  value = helm_release.external_secrets.name
}
