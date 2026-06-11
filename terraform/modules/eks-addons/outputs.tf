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

output "external_secrets_release_name" {
  value = helm_release.external_secrets.name
}
