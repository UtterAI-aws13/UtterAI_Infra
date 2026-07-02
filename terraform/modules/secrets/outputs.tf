output "backend_api_secret_arn" {
  value = aws_secretsmanager_secret.backend_api.arn
}

output "ai_worker_secret_arn" {
  value = aws_secretsmanager_secret.ai_worker.arn
}

output "cpu_worker_secret_arn" {
  value = aws_secretsmanager_secret.cpu_worker.arn
}

output "rag_ingest_secret_arn" {
  value = try(aws_secretsmanager_secret.rag_ingest[0].arn, null)
}

output "gpu_worker_secret_arn" {
  value = aws_secretsmanager_secret.gpu_worker.arn
}

output "collect_papers_secret_arn" {
  value = aws_secretsmanager_secret.collect_papers.arn
}
