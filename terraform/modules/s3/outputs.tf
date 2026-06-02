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

output "processed_audio_bucket_name" {
  value = aws_s3_bucket.buckets["processed_audio"].id
}

output "processed_audio_bucket_arn" {
  value = aws_s3_bucket.buckets["processed_audio"].arn
}

output "documents_bucket_name" {
  value = aws_s3_bucket.buckets["documents"].id
}

output "documents_bucket_arn" {
  value = aws_s3_bucket.buckets["documents"].arn
}

output "reports_bucket_name" {
  value = aws_s3_bucket.buckets["reports"].id
}

output "reports_bucket_arn" {
  value = aws_s3_bucket.buckets["reports"].arn
}

output "artifacts_bucket_name" {
  value = aws_s3_bucket.buckets["artifacts"].id
}

output "artifacts_bucket_arn" {
  value = aws_s3_bucket.buckets["artifacts"].arn
}
