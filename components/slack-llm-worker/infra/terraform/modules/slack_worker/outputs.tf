output "function_url" {
  value = aws_lambda_function_url.ingress.function_url
}

output "queue_url" {
  value = aws_sqs_queue.jobs.url
}

output "dlq_url" {
  value = aws_sqs_queue.dlq.url
}

output "state_table" {
  value = aws_dynamodb_table.state.name
}

output "worker_role_arn" {
  value = aws_iam_role.worker.arn
}

output "worker_task_definition_arn" {
  value = try(aws_ecs_task_definition.worker[0].arn, null)
}
