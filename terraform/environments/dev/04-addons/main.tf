data "terraform_remote_state" "network" {
  backend = "s3"
  config = {
    bucket = "utterai-dev-terraform-state"
    key    = "dev/network/terraform.tfstate"
    region = "ap-northeast-2"
  }
}

data "terraform_remote_state" "eks" {
  backend = "s3"
  config = {
    bucket = "utterai-dev-terraform-state"
    key    = "dev/platform/terraform.tfstate"
    region = "ap-northeast-2"
  }
}

data "terraform_remote_state" "services" {
  backend = "s3"
  config = {
    bucket = "utterai-dev-terraform-state"
    key    = "dev/services/terraform.tfstate"
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
}

# ── CloudFront ────────────────────────────────────────────────────────────────

module "cloudfront" {
  source = "../../../modules/cloudfront"

  project_name        = var.project_name
  environment         = var.environment
  frontend_bucket_id  = data.terraform_remote_state.services.outputs.frontend_bucket_name
  frontend_bucket_arn = data.terraform_remote_state.services.outputs.frontend_bucket_arn
  alb_dns_name        = var.alb_dns_name
}
