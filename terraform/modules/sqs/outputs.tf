output "cpu_analysis_queue_url" {
  value = aws_sqs_queue.cpu_analysis.url
}

output "cpu_analysis_queue_arn" {
  value = aws_sqs_queue.cpu_analysis.arn
}

output "cpu_analysis_dlq_arn" {
  value = aws_sqs_queue.cpu_analysis_dlq.arn
}

output "audio_ml_queue_url" {
  value = aws_sqs_queue.audio_ml.url
}

output "audio_ml_queue_arn" {
  value = aws_sqs_queue.audio_ml.arn
}

output "llm_queue_url" {
  value = aws_sqs_queue.llm.url
}

output "llm_queue_arn" {
  value = aws_sqs_queue.llm.arn
}
