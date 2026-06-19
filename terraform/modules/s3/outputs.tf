output "frontend_bucket_name" {
  value = aws_s3_bucket.buckets["frontend"].id
}

output "frontend_bucket_arn" {
  value = aws_s3_bucket.buckets["frontend"].arn
}

output "raw_audio_bucket_name" {
  value = aws_s3_bucket.buckets["raw_audio"].id
}

output "raw_audio_bucket_arn" {
  value = aws_s3_bucket.buckets["raw_audio"].arn
}

output "template_bucket_name" {
  value = aws_s3_bucket.buckets["template"].id
}

output "template_bucket_arn" {
  value = aws_s3_bucket.buckets["template"].arn
}

output "rag_ingest_bucket_name" {
  value = aws_s3_bucket.buckets["rag_ingest"].id
}

output "rag_ingest_bucket_arn" {
  value = aws_s3_bucket.buckets["rag_ingest"].arn
}

output "reports_bucket_name" {
  value = aws_s3_bucket.buckets["reports"].id
}

output "reports_bucket_arn" {
  value = aws_s3_bucket.buckets["reports"].arn
}

output "kubecost_bucket_name" {
  value = aws_s3_bucket.buckets["kubecost"].id
}

output "kubecost_bucket_arn" {
  value = aws_s3_bucket.buckets["kubecost"].arn
}

output "loki_bucket_name" {
  value = aws_s3_bucket.buckets["loki"].id
}

output "loki_bucket_arn" {
  value = aws_s3_bucket.buckets["loki"].arn
}

output "tempo_bucket_name" {
  value = try(aws_s3_bucket.buckets["tempo"].id, "")
}

output "tempo_bucket_arn" {
  value = try(aws_s3_bucket.buckets["tempo"].arn, "")
}
