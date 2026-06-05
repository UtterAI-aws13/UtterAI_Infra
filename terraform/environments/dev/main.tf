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
    bucket       = "utterai-dev-terraform-state"
    key          = "dev/terraform.tfstate"
    region       = "ap-northeast-2"
    use_lockfile = true
    encrypt      = true
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

  cpu_analysis_queue_arn = module.sqs.cpu_analysis_queue_arn
  audio_ml_queue_arn     = module.sqs.audio_ml_queue_arn
  llm_queue_arn          = module.sqs.llm_queue_arn
  cpu_analysis_dlq_arn   = module.sqs.cpu_analysis_dlq_arn

  private_app_subnet_ids = module.vpc.private_app_subnet_ids
  node_security_group_id = module.eks.node_security_group_id
}

# ── EKS Add-ons ──────────────────────────────────────────────────────────────

module "eks_addons" {
  source = "../../modules/eks-addons"

  cluster_name     = var.cluster_name
  cluster_endpoint = module.eks.cluster_endpoint
  aws_region       = var.aws_region

  lbc_irsa_role_arn                = module.irsa.lbc_role_arn
  cluster_autoscaler_irsa_role_arn = module.irsa.cluster_autoscaler_role_arn

  depends_on = [module.eks]
}

# ── RDS ──────────────────────────────────────────────────────────────────────

module "rds" {
  source = "../../modules/rds"

  project_name = var.project_name
  environment  = var.environment

  instance_class = var.rds_instance_class

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

# ── ECR ──────────────────────────────────────────────────────────────────────

module "ecr" {
  source = "../../modules/ecr"

  repository_names = ["utterai-backend", "utterai-ai-cpu", "utterai-ai-gpu"]
}

# ── Data sources ─────────────────────────────────────────────────────────────

data "aws_caller_identity" "current" {}
