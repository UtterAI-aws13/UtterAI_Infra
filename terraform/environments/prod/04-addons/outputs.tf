output "cloudfront_distribution_id" {
  value = module.cloudfront.distribution_id
}

output "cloudfront_domain_name" {
  value = module.cloudfront.distribution_domain_name
}

output "cloudfront_distribution_hosted_zone_id" {
  value = module.cloudfront.distribution_hosted_zone_id
}

output "cloudfront_custom_domains" {
  value = var.cloudfront_aliases
}

output "frontend_url" {
  value = local.cloudfront_custom_domain_enabled ? "https://${var.cloudfront_aliases[0]}" : "https://${module.cloudfront.distribution_domain_name}"
}

output "cloudfront_acm_certificate_arn" {
  value = local.cloudfront_custom_domain_enabled ? local.cloudfront_certificate_arn : null
}

output "cloudfront_certificate_source" {
  value = local.cloudfront_custom_domain_enabled ? (var.cloudfront_acm_certificate_arn != "" ? "provided" : "terraform_managed") : "cloudfront_default"
}

output "cloudfront_route53_alias_records" {
  value = {
    a    = [for record in aws_route53_record.cloudfront_alias_a : record.fqdn]
    aaaa = [for record in aws_route53_record.cloudfront_alias_aaaa : record.fqdn]
  }
}

output "cloudfront_acm_dns_validation_records" {
  value = {
    for domain, record in aws_route53_record.cloudfront_certificate_validation :
    domain => {
      name    = record.name
      type    = record.type
      records = record.records
    }
  }
}

output "cloudfront_origin_alb_dns_name" {
  value = local.alb_dns_name
}

output "route53_hosted_zone_id" {
  value = var.route53_hosted_zone_id
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

output "alb_acm_certificate_arn" {
  description = "ap-northeast-2 ACM 인증서 ARN — patch-ingress.yaml의 certificate-arn에 사용"
  value       = aws_acm_certificate_validation.alb.certificate_arn
}
