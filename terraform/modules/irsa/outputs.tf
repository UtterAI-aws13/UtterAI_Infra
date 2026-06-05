output "lbc_role_arn" {
  value = aws_iam_role.lbc.arn
}

output "cluster_autoscaler_role_arn" {
  value = aws_iam_role.cluster_autoscaler.arn
}

output "ai_api_role_arn" {
  value = aws_iam_role.ai_api.arn
}

output "api_role_arn" {
  value = aws_iam_role.api.arn
}

output "ai_cpu_role_arn" {
  value = aws_iam_role.ai_cpu.arn
}

output "ai_ml_gpu_role_arn" {
  value = aws_iam_role.ai_ml_gpu.arn
}

output "ai_llm_gpu_role_arn" {
  value = aws_iam_role.ai_llm_gpu.arn
}

output "batch_role_arn" {
  value = aws_iam_role.batch.arn
}
