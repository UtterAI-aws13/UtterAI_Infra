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

  alertmanager_slack_enabled             = var.alertmanager_slack_enabled
  alertmanager_slack_channel             = var.alertmanager_slack_channel
  alertmanager_slack_webhook_secret_name = var.alertmanager_slack_webhook_secret_name
  alertmanager_slack_webhook_secret_key  = var.alertmanager_slack_webhook_secret_key
  alertmanager_slack_secret_manager_name = var.alertmanager_slack_secret_manager_name

  grafana_admin_credentials_enabled    = var.grafana_admin_credentials_enabled
  grafana_admin_secret_manager_name    = var.grafana_admin_secret_manager_name
  grafana_admin_kubernetes_secret_name = var.grafana_admin_kubernetes_secret_name
  grafana_admin_user_key               = var.grafana_admin_user_key
  grafana_admin_password_key           = var.grafana_admin_password_key
}

# ── ALB DNS 자동 조회 ─────────────────────────────────────────────────────────

data "aws_lb" "api" {
  tags = {
    "elbv2.k8s.aws/cluster" = data.terraform_remote_state.eks.outputs.cluster_name
    "ingress.k8s.aws/stack" = "utterai-prod"
  }
}

# ── CloudFront ────────────────────────────────────────────────────────────────

module "cloudfront" {
  source = "../../../modules/cloudfront"

  project_name        = var.project_name
  environment         = var.environment
  frontend_bucket_id  = data.terraform_remote_state.services.outputs.frontend_bucket_name
  frontend_bucket_arn = data.terraform_remote_state.services.outputs.frontend_bucket_arn
  alb_dns_name        = data.aws_lb.api.dns_name
}
