# ── VPC ──────────────────────────────────────────────────────────────────────

output "vpc_id" {
  value = module.vpc.vpc_id
}

output "public_subnet_ids" {
  value = module.vpc.public_subnet_ids
}

output "private_app_subnet_ids" {
  value = module.vpc.private_app_subnet_ids
}

output "private_data_subnet_ids" {
  value = module.vpc.private_data_subnet_ids
}

# ── EKS ──────────────────────────────────────────────────────────────────────

output "cluster_name" {
  value = module.eks.cluster_name
}

output "cluster_endpoint" {
  value = module.eks.cluster_endpoint
}

output "cluster_ca_certificate" {
  value     = module.eks.cluster_ca_certificate
  sensitive = true
}

output "oidc_provider_arn" {
  value = module.eks.oidc_provider_arn
}

# ── IRSA ─────────────────────────────────────────────────────────────────────

output "backend_api_role_arn" {
  value = module.irsa.api_role_arn
}

output "ai_cpu_worker_role_arn" {
  value = module.irsa.ai_cpu_role_arn
}

output "ai_ml_gpu_worker_role_arn" {
  value = module.irsa.ai_ml_gpu_role_arn
}

output "ai_llm_gpu_worker_role_arn" {
  value = module.irsa.ai_llm_gpu_role_arn
}

output "batch_worker_role_arn" {
  value = module.irsa.batch_role_arn
}

# ── RDS ──────────────────────────────────────────────────────────────────────

output "rds_endpoint" {
  value = module.rds.endpoint
}

output "rds_db_secret_arn" {
  value = module.rds.db_secret_arn
}

# ── Redis ─────────────────────────────────────────────────────────────────────

output "redis_endpoint" {
  value = module.redis.primary_endpoint
}

# ── S3 ───────────────────────────────────────────────────────────────────────

output "raw_audio_bucket_name" {
  value = module.s3.raw_audio_bucket_name
}

output "documents_bucket_name" {
  value = module.s3.documents_bucket_name
}

output "reports_bucket_name" {
  value = module.s3.reports_bucket_name
}

output "frontend_bucket_name" {
  value = module.s3.frontend_bucket_name
}

output "cloudfront_domain_name" {
  value = module.cloudfront.distribution_domain_name
}

output "cloudfront_distribution_id" {
  value = module.cloudfront.distribution_id
}

# ── SQS ──────────────────────────────────────────────────────────────────────

output "audio_preprocess_queue_url" {
  value = module.sqs.audio_preprocess_queue_url
}

output "gpu_inference_queue_url" {
  value = module.sqs.gpu_inference_queue_url
}

output "report_analysis_queue_url" {
  value = module.sqs.report_analysis_queue_url
}

output "rag_ingest_queue_url" {
  value = module.sqs.rag_ingest_queue_url
}

