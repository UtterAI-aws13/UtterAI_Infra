data "terraform_remote_state" "network" {
  backend = "s3"
  config = {
    bucket = "utterai-prod-terraform-state"
    key    = "prod/network/terraform.tfstate"
    region = "ap-northeast-2"
  }
}

data "terraform_remote_state" "eks" {
  backend = "s3"
  config = {
    bucket = "utterai-prod-terraform-state"
    key    = "prod/platform/terraform.tfstate"
    region = "ap-northeast-2"
  }
}

data "terraform_remote_state" "services" {
  backend = "s3"
  config = {
    bucket = "utterai-prod-terraform-state"
    key    = "prod/services/terraform.tfstate"
    region = "ap-northeast-2"
  }
}

locals {
  cloudfront_custom_domain_enabled = length(var.cloudfront_aliases) > 0
  create_cloudfront_certificate    = local.cloudfront_custom_domain_enabled && var.cloudfront_acm_certificate_arn == ""
  cloudfront_certificate_arn       = var.cloudfront_acm_certificate_arn != "" ? var.cloudfront_acm_certificate_arn : try(aws_acm_certificate_validation.cloudfront[0].certificate_arn, "")
  alb_dns_name                     = var.alb_dns_name != "" ? var.alb_dns_name : data.aws_lb.api[0].dns_name
}

check "cloudfront_custom_domain_inputs" {
  assert {
    condition     = !local.create_cloudfront_certificate || var.route53_hosted_zone_id != ""
    error_message = "route53_hosted_zone_id is required when cloudfront_aliases is set and cloudfront_acm_certificate_arn is empty."
  }
}

# ── EKS Add-ons ──────────────────────────────────────────────────────────────

module "eks_addons" {
  source = "../../../modules/eks-addons"

  cluster_name     = data.terraform_remote_state.eks.outputs.cluster_name
  cluster_endpoint = data.terraform_remote_state.eks.outputs.cluster_endpoint
  aws_region       = var.aws_region

  lbc_irsa_role_arn                = data.terraform_remote_state.services.outputs.lbc_role_arn
  cluster_autoscaler_irsa_role_arn = data.terraform_remote_state.services.outputs.cluster_autoscaler_role_arn
  eso_irsa_role_arn                = data.terraform_remote_state.services.outputs.eso_role_arn
  vpc_id                           = data.terraform_remote_state.network.outputs.vpc_id

  cluster_autoscaler_enabled = false
  keda_enabled               = true
  keda_irsa_role_arn         = data.terraform_remote_state.services.outputs.keda_role_arn
  karpenter_enabled          = true
  karpenter_irsa_role_arn    = data.terraform_remote_state.services.outputs.karpenter_role_arn


  grafana_admin_credentials_enabled    = var.grafana_admin_credentials_enabled
  grafana_admin_secret_manager_name    = var.grafana_admin_secret_manager_name
  grafana_admin_kubernetes_secret_name = var.grafana_admin_kubernetes_secret_name
  grafana_admin_user_key               = var.grafana_admin_user_key
  grafana_admin_password_key           = var.grafana_admin_password_key

  kubecost_enabled                   = var.kubecost_enabled
  kubecost_persistent_volume_enabled = var.kubecost_persistent_volume_enabled

  kubecost_s3_bucket_name = data.terraform_remote_state.services.outputs.kubecost_bucket_name
  kubecost_irsa_role_arn  = data.terraform_remote_state.services.outputs.kubecost_role_arn
  loki_s3_bucket_name     = data.terraform_remote_state.services.outputs.loki_bucket_name
  loki_irsa_role_arn      = data.terraform_remote_state.services.outputs.loki_role_arn
  loki_retention_period   = "336h"
  tempo_s3_bucket_name    = data.terraform_remote_state.services.outputs.tempo_bucket_name
  tempo_irsa_role_arn     = data.terraform_remote_state.services.outputs.tempo_role_arn
  tempo_retention_period  = "72h"
}

# ── ALB DNS 자동 조회 ─────────────────────────────────────────────────────────

data "aws_lb" "api" {
  count = var.alb_dns_name == "" ? 1 : 0

  tags = {
    "elbv2.k8s.aws/cluster" = data.terraform_remote_state.eks.outputs.cluster_name
    "ingress.k8s.aws/stack" = "utterai-prod"
  }
}

# ── CloudFront Custom Domain ─────────────────────────────────────────────────

resource "aws_acm_certificate" "cloudfront" {
  count = local.create_cloudfront_certificate ? 1 : 0

  provider                  = aws.us_east_1
  domain_name               = var.cloudfront_aliases[0]
  subject_alternative_names = slice(var.cloudfront_aliases, 1, length(var.cloudfront_aliases))
  validation_method         = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "cloudfront_certificate_validation" {
  for_each = local.create_cloudfront_certificate ? {
    for option in aws_acm_certificate.cloudfront[0].domain_validation_options :
    option.domain_name => {
      name   = option.resource_record_name
      record = option.resource_record_value
      type   = option.resource_record_type
    }
  } : {}

  allow_overwrite = true
  zone_id         = var.route53_hosted_zone_id
  name            = each.value.name
  type            = each.value.type
  ttl             = 60
  records         = [each.value.record]
}

resource "aws_acm_certificate_validation" "cloudfront" {
  count = local.create_cloudfront_certificate ? 1 : 0

  provider                = aws.us_east_1
  certificate_arn         = aws_acm_certificate.cloudfront[0].arn
  validation_record_fqdns = [for record in aws_route53_record.cloudfront_certificate_validation : record.fqdn]
}

# ── CloudFront ────────────────────────────────────────────────────────────────

module "cloudfront" {
  source = "../../../modules/cloudfront"

  project_name        = var.project_name
  environment         = var.environment
  frontend_bucket_id  = data.terraform_remote_state.services.outputs.frontend_bucket_name
  frontend_bucket_arn = data.terraform_remote_state.services.outputs.frontend_bucket_arn
  alb_dns_name        = local.alb_dns_name
  aliases             = local.cloudfront_custom_domain_enabled ? var.cloudfront_aliases : []
  acm_certificate_arn = local.cloudfront_custom_domain_enabled ? local.cloudfront_certificate_arn : ""
}

resource "aws_route53_record" "cloudfront_alias_a" {
  for_each = local.cloudfront_custom_domain_enabled && var.route53_hosted_zone_id != "" ? toset(var.cloudfront_aliases) : toset([])

  zone_id = var.route53_hosted_zone_id
  name    = each.value
  type    = "A"

  alias {
    name                   = module.cloudfront.distribution_domain_name
    zone_id                = module.cloudfront.distribution_hosted_zone_id
    evaluate_target_health = false
  }
}

resource "aws_route53_record" "cloudfront_alias_aaaa" {
  for_each = local.cloudfront_custom_domain_enabled && var.route53_hosted_zone_id != "" ? toset(var.cloudfront_aliases) : toset([])

  zone_id = var.route53_hosted_zone_id
  name    = each.value
  type    = "AAAA"

  alias {
    name                   = module.cloudfront.distribution_domain_name
    zone_id                = module.cloudfront.distribution_hosted_zone_id
    evaluate_target_health = false
  }
}
