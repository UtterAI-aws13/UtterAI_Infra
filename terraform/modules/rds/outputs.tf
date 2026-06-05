output "endpoint" {
  value = aws_db_instance.this.address
}

output "port" {
  value = aws_db_instance.this.port
}

output "security_group_id" {
  value = aws_security_group.rds.id
}

output "db_secret_arn" {
  value = aws_db_instance.this.master_user_secret[0].secret_arn
}
