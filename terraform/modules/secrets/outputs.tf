output "backend_api_secret_arn" {
  value = aws_secretsmanager_secret.backend_api.arn
}

output "ai_worker_secret_arn" {
  value = aws_secretsmanager_secret.ai_worker.arn
}

output "gpu_worker_secret_arn" {
  value = aws_secretsmanager_secret.gpu_worker.arn
}
