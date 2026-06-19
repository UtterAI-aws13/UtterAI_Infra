output "cloudfront_distribution_id" {
  value = module.cloudfront.distribution_id
}

output "cloudfront_domain_name" {
  value = module.cloudfront.distribution_domain_name
}

output "cloudfront_custom_domains" {
  value = var.cloudfront_aliases
}

output "alertmanager_slack_secret_manager_name" {
  value = module.eks_addons.alertmanager_slack_secret_manager_name
}

output "alertmanager_slack_secret_manager_arn" {
  value = module.eks_addons.alertmanager_slack_secret_manager_arn
}

output "kubecost_release_name" {
  value = module.eks_addons.kubecost_release_name
}
