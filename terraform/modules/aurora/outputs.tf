output "writer_endpoint" {
  value = aws_rds_cluster.this.endpoint
}

output "reader_endpoint" {
  value = aws_rds_cluster.this.reader_endpoint
}

output "cluster_id" {
  value = aws_rds_cluster.this.cluster_identifier
}

output "security_group_id" {
  value = aws_security_group.aurora.id
}

output "database_name" {
  value = aws_rds_cluster.this.database_name
}

# manage_master_user_password=true 시 AWS가 자동 생성하는 Secrets Manager 시크릿 ARN
# 이름이 rds!cluster-xxx 형태라 예측 불가 → output으로 노출해서 사용
output "db_secret_arn" {
  value = aws_rds_cluster.this.master_user_secret[0].secret_arn
}
