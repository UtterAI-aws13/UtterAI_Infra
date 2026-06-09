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

output "raw_audio_bucket_arn" {
  value = module.s3.raw_audio_bucket_arn
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

output "frontend_bucket_arn" {
  value = module.s3.frontend_bucket_arn
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

# ── Secrets ───────────────────────────────────────────────────────────────────

output "backend_api_secret_arn" {
  value = module.secrets.backend_api_secret_arn
}

output "ai_worker_secret_arn" {
  value = module.secrets.ai_worker_secret_arn
}

output "gpu_worker_secret_arn" {
  value = module.secrets.gpu_worker_secret_arn
}

# ── ECR ──────────────────────────────────────────────────────────────────────

output "ecr_repository_urls" {
  value = module.ecr.repository_urls
}

# ── IRSA ─────────────────────────────────────────────────────────────────────

output "backend_api_role_arn" {
  value = module.irsa.api_role_arn
}

output "ai_api_role_arn" {
  value = module.irsa.ai_api_role_arn
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

output "lbc_role_arn" {
  value = module.irsa.lbc_role_arn
}

output "cluster_autoscaler_role_arn" {
  value = module.irsa.cluster_autoscaler_role_arn
}

output "eso_role_arn" {
  value = module.irsa.eso_role_arn
}
