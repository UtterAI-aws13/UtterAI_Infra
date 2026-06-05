output "analysis_queue_url" {
  value = aws_sqs_queue.analysis.url
}

output "analysis_queue_arn" {
  value = aws_sqs_queue.analysis.arn
}

output "dlq_url" {
  value = aws_sqs_queue.dlq.url
}

output "dlq_arn" {
  value = aws_sqs_queue.dlq.arn
}
