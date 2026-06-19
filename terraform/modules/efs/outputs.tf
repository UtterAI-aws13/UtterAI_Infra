output "file_system_id" {
  value       = aws_efs_file_system.model_cache.id
  description = "StorageClass의 fileSystemId 파라미터에 사용"
}

output "file_system_arn" {
  value = aws_efs_file_system.model_cache.arn
}
