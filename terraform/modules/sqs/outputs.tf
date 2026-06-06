output "audio_preprocess_queue_url" {
  value = aws_sqs_queue.audio_preprocess.url
}

output "audio_preprocess_queue_arn" {
  value = aws_sqs_queue.audio_preprocess.arn
}

output "audio_preprocess_dlq_arn" {
  value = aws_sqs_queue.audio_preprocess_dlq.arn
}

output "gpu_inference_queue_url" {
  value = aws_sqs_queue.gpu_inference.url
}

output "gpu_inference_queue_arn" {
  value = aws_sqs_queue.gpu_inference.arn
}

output "gpu_inference_dlq_arn" {
  value = aws_sqs_queue.gpu_inference_dlq.arn
}

output "report_analysis_queue_url" {
  value = aws_sqs_queue.report_analysis.url
}

output "report_analysis_queue_arn" {
  value = aws_sqs_queue.report_analysis.arn
}

output "report_analysis_dlq_arn" {
  value = aws_sqs_queue.report_analysis_dlq.arn
}

output "rag_ingest_queue_url" {
  value = aws_sqs_queue.rag_ingest.url
}

output "rag_ingest_queue_arn" {
  value = aws_sqs_queue.rag_ingest.arn
}

output "rag_ingest_dlq_arn" {
  value = aws_sqs_queue.rag_ingest_dlq.arn
}
