output "lbc_role_arn" {
  value = aws_iam_role.lbc.arn
}

output "cluster_autoscaler_role_arn" {
  value = aws_iam_role.cluster_autoscaler.arn
}

output "api_role_arn" {
  value = aws_iam_role.api.arn
}

output "ai_cpu_role_arn" {
  value = aws_iam_role.ai_cpu.arn
}

output "ai_gpu_role_arn" {
  value = aws_iam_role.ai_gpu.arn
}

output "batch_role_arn" {
  value = aws_iam_role.batch.arn
}
