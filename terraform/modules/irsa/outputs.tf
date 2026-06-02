output "lbc_role_arn" {
  value = aws_iam_role.lbc.arn
}

output "karpenter_role_arn" {
  value = aws_iam_role.karpenter_controller.arn
}

output "karpenter_node_role_name" {
  value = aws_iam_role.karpenter_node.name
}

output "karpenter_sqs_queue_url" {
  value = aws_sqs_queue.karpenter_interruption.url
}

output "karpenter_sqs_queue_arn" {
  value = aws_sqs_queue.karpenter_interruption.arn
}

output "keda_role_arn" {
  value = aws_iam_role.keda.arn
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
