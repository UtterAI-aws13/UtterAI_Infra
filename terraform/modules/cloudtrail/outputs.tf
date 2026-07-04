output "trail_arn" {
  value = length(aws_cloudtrail.sqs_forensics) > 0 ? aws_cloudtrail.sqs_forensics[0].arn : null
}

output "log_bucket_name" {
  value = length(aws_s3_bucket.trail) > 0 ? aws_s3_bucket.trail[0].id : null
}
