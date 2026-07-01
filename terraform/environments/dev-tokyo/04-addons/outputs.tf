output "cloudfront_distribution_id" {
  value = try(module.cloudfront[0].distribution_id, null)
}

output "cloudfront_domain_name" {
  value = try(module.cloudfront[0].distribution_domain_name, null)
}

output "grafana_admin_secret_manager_name" {
  value = module.eks_addons.grafana_admin_secret_manager_name
}

output "grafana_admin_secret_manager_arn" {
  value = module.eks_addons.grafana_admin_secret_manager_arn
}

output "kubecost_release_name" {
  value = module.eks_addons.kubecost_release_name
}
