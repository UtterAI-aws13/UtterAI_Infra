terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }

  backend "s3" {
    bucket         = "utterai-dev-terraform-state"
    key            = "dev/terraform.tfstate"
    region         = "ap-northeast-2"
    dynamodb_table = "utterai-dev-terraform-lock"
    encrypt        = true
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

provider "kubernetes" {
  host                   = module.eks.cluster_endpoint
  cluster_ca_certificate = base64decode(module.eks.cluster_ca_certificate)
  token                  = module.eks.cluster_token
}

provider "helm" {
  kubernetes {
    host                   = module.eks.cluster_endpoint
    cluster_ca_certificate = base64decode(module.eks.cluster_ca_certificate)
    token                  = module.eks.cluster_token
  }
}

# ── VPC ──────────────────────────────────────────────────────────────────────

module "vpc" {
  source = "../../modules/vpc"

  project_name = var.project_name
  environment  = var.environment
  vpc_cidr     = var.vpc_cidr
  azs          = var.azs

  public_subnet_cidrs       = var.public_subnet_cidrs
  private_app_subnet_cidrs  = var.private_app_subnet_cidrs
  private_data_subnet_cidrs = var.private_data_subnet_cidrs

  cluster_name = var.cluster_name
}

# ── EKS ──────────────────────────────────────────────────────────────────────

module "eks" {
  source = "../../modules/eks"

  project_name       = var.project_name
  environment        = var.environment
  cluster_name       = var.cluster_name
  kubernetes_version = var.kubernetes_version

  vpc_id                 = module.vpc.vpc_id
  private_app_subnet_ids = module.vpc.private_app_subnet_ids

  system_node_instance_type = var.system_node_instance_type
  system_node_desired_size  = var.system_node_desired_size
  system_node_min_size      = var.system_node_min_size
  system_node_max_size      = var.system_node_max_size

  api_node_instance_type = var.api_node_instance_type
  api_node_desired_size  = var.api_node_desired_size
  api_node_min_size      = var.api_node_min_size
  api_node_max_size      = var.api_node_max_size

}

# ── IRSA ─────────────────────────────────────────────────────────────────────

module "irsa" {
  source = "../../modules/irsa"

  project_name      = var.project_name
  environment       = var.environment
  cluster_name      = var.cluster_name
  oidc_provider_arn = module.eks.oidc_provider_arn
  oidc_provider_url = module.eks.oidc_provider_url
  aws_account_id    = data.aws_caller_identity.current.account_id
  aws_region        = var.aws_region

  raw_audio_bucket_arn       = module.s3.raw_audio_bucket_arn
  processed_audio_bucket_arn = module.s3.processed_audio_bucket_arn
  documents_bucket_arn       = module.s3.documents_bucket_arn
  reports_bucket_arn         = module.s3.reports_bucket_arn
  artifacts_bucket_arn       = module.s3.artifacts_bucket_arn
  frontend_bucket_arn        = module.s3.frontend_bucket_arn

  analysis_queue_arn = module.sqs.analysis_queue_arn
  dlq_arn            = module.sqs.dlq_arn

  private_app_subnet_ids = module.vpc.private_app_subnet_ids
  node_security_group_id = module.eks.node_security_group_id
}

# ── EKS Add-ons ──────────────────────────────────────────────────────────────

module "eks_addons" {
  source = "../../modules/eks-addons"

  cluster_name     = var.cluster_name
  cluster_endpoint = module.eks.cluster_endpoint
  aws_region       = var.aws_region

  lbc_irsa_role_arn       = module.irsa.lbc_role_arn
  karpenter_irsa_role_arn = module.irsa.karpenter_role_arn
  keda_irsa_role_arn      = module.irsa.keda_role_arn

  karpenter_node_role_name = module.irsa.karpenter_node_role_name
  karpenter_sqs_queue_url  = module.irsa.karpenter_sqs_queue_url
  karpenter_sqs_queue_arn  = module.irsa.karpenter_sqs_queue_arn

  depends_on = [module.eks]
}

# ── Aurora ───────────────────────────────────────────────────────────────────

module "aurora" {
  source = "../../modules/aurora"

  project_name = var.project_name
  environment  = var.environment

  instance_class   = var.aurora_instance_class
  database_name    = var.aurora_database_name
  master_username  = var.aurora_master_username
  backup_retention = var.aurora_backup_retention

  vpc_id                    = module.vpc.vpc_id
  private_data_subnet_ids   = module.vpc.private_data_subnet_ids
  allowed_security_group_id = module.eks.node_security_group_id
}

# ── Redis ─────────────────────────────────────────────────────────────────────

module "redis" {
  source = "../../modules/redis"

  project_name = var.project_name
  environment  = var.environment

  node_type       = var.redis_node_type
  num_cache_nodes = var.redis_num_cache_nodes

  vpc_id                    = module.vpc.vpc_id
  private_data_subnet_ids   = module.vpc.private_data_subnet_ids
  allowed_security_group_id = module.eks.node_security_group_id
}

# ── S3 ───────────────────────────────────────────────────────────────────────

module "s3" {
  source = "../../modules/s3"

  project_name    = var.project_name
  environment     = var.environment
  frontend_domain = "dev.utterai.com"
}

# ── SQS ──────────────────────────────────────────────────────────────────────

module "sqs" {
  source = "../../modules/sqs"

  project_name = var.project_name
  environment  = var.environment
}

# ── Cognito ──────────────────────────────────────────────────────────────────

module "cognito" {
  source = "../../modules/cognito"

  project_name = var.project_name
  environment  = var.environment
  callback_url = "https://dev.utterai.com/auth/callback"
  logout_url   = "https://dev.utterai.com/logout"
}

# ── Data sources ─────────────────────────────────────────────────────────────

data "aws_caller_identity" "current" {}
