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

data "aws_caller_identity" "current" {}

# ── RDS ──────────────────────────────────────────────────────────────────────

module "rds" {
  source = "../../../modules/rds"

  project_name   = var.project_name
  environment    = var.environment
  instance_class = var.rds_instance_class

  vpc_id                    = data.terraform_remote_state.network.outputs.vpc_id
  private_data_subnet_ids   = data.terraform_remote_state.network.outputs.private_data_subnet_ids
  allowed_security_group_id = data.terraform_remote_state.eks.outputs.node_security_group_id
  cluster_security_group_id = data.terraform_remote_state.eks.outputs.cluster_security_group_id

  skip_final_snapshot = false
  deletion_protection = true
}

# ── Redis ─────────────────────────────────────────────────────────────────────

module "redis" {
  source = "../../../modules/redis"

  project_name    = var.project_name
  environment     = var.environment
  node_type       = var.redis_node_type
  num_cache_nodes = var.redis_num_cache_nodes

  vpc_id                    = data.terraform_remote_state.network.outputs.vpc_id
  private_data_subnet_ids   = data.terraform_remote_state.network.outputs.private_data_subnet_ids
  allowed_security_group_id = data.terraform_remote_state.eks.outputs.node_security_group_id
  cluster_security_group_id = data.terraform_remote_state.eks.outputs.cluster_security_group_id
}

# ── S3 ───────────────────────────────────────────────────────────────────────

module "s3" {
  source = "../../../modules/s3"

  project_name    = var.project_name
  environment     = var.environment
  frontend_domain = var.frontend_domain
}

# ── SQS ──────────────────────────────────────────────────────────────────────

module "sqs" {
  source = "../../../modules/sqs"

  project_name = var.project_name
  environment  = var.environment
}

# ── Secrets Manager ──────────────────────────────────────────────────────────

module "secrets" {
  source = "../../../modules/secrets"

  project_name = var.project_name
  environment  = var.environment
}

# ── ECR ──────────────────────────────────────────────────────────────────────

module "ecr" {
  source = "../../../modules/ecr"

  repository_names = ["utterai-backend", "utterai-ai-cpu", "utterai-ai-gpu"]
}

# ── IRSA ─────────────────────────────────────────────────────────────────────

module "irsa" {
  source = "../../../modules/irsa"

  project_name      = var.project_name
  environment       = var.environment
  cluster_name      = var.cluster_name
  oidc_provider_arn = data.terraform_remote_state.eks.outputs.oidc_provider_arn
  oidc_provider_url = data.terraform_remote_state.eks.outputs.oidc_provider_url
  aws_account_id    = data.aws_caller_identity.current.account_id
  aws_region        = var.aws_region

  raw_audio_bucket_arn  = module.s3.raw_audio_bucket_arn
  template_bucket_arn   = module.s3.template_bucket_arn
  rag_ingest_bucket_arn = module.s3.rag_ingest_bucket_arn
  reports_bucket_arn    = module.s3.reports_bucket_arn
  frontend_bucket_arn   = module.s3.frontend_bucket_arn

  audio_preprocess_queue_arn = module.sqs.audio_preprocess_queue_arn
  gpu_inference_queue_arn    = module.sqs.gpu_inference_queue_arn
  report_analysis_queue_arn  = module.sqs.report_analysis_queue_arn
  audio_preprocess_dlq_arn   = module.sqs.audio_preprocess_dlq_arn
  rag_ingest_queue_arn       = module.sqs.rag_ingest_queue_arn
  rag_ingest_dlq_arn         = module.sqs.rag_ingest_dlq_arn

  private_app_subnet_ids = data.terraform_remote_state.network.outputs.private_app_subnet_ids
  node_security_group_id = data.terraform_remote_state.eks.outputs.node_security_group_id
}
