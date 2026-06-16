output "cloudfront_distribution_id" {
  value = module.cloudfront.distribution_id
}

output "cloudfront_domain_name" {
  value = module.cloudfront.distribution_domain_name
}

output "alertmanager_slack_secret_manager_name" {
  value = module.eks_addons.alertmanager_slack_secret_manager_name
}

output "alertmanager_slack_secret_manager_arn" {
  value = module.eks_addons.alertmanager_slack_secret_manager_arn
}
