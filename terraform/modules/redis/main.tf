locals {
  prefix = "${var.project_name}-${var.environment}"
}

resource "random_password" "redis_auth" {
  length  = 32
  special = false
}

resource "aws_secretsmanager_secret" "redis_auth" {
  name                    = "${local.prefix}/redis-auth-token"
  recovery_window_in_days = 0

  tags = {
    Name = "${local.prefix}-redis-auth-token"
  }
}

resource "aws_secretsmanager_secret_version" "redis_auth" {
  secret_id     = aws_secretsmanager_secret.redis_auth.id
  secret_string = jsonencode({ REDIS_AUTH_TOKEN = random_password.redis_auth.result })
}

resource "aws_security_group" "redis" {
  name        = "${local.prefix}-redis-sg"
  description = "Security group for ElastiCache Redis"
  vpc_id      = var.vpc_id

  ingress {
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [var.allowed_security_group_id, var.cluster_security_group_id]
  }

  tags = {
    Name = "${local.prefix}-redis-sg"
  }
}

resource "aws_elasticache_subnet_group" "this" {
  name       = "${local.prefix}-redis-subnet-group"
  subnet_ids = var.private_data_subnet_ids

  tags = {
    Name = "${local.prefix}-redis-subnet-group"
  }
}

resource "aws_elasticache_parameter_group" "this" {
  name   = "${local.prefix}-redis7"
  family = "redis7"
}

resource "aws_elasticache_replication_group" "this" {
  replication_group_id       = "${local.prefix}-redis"
  description                = "Redis for ${local.prefix}"
  engine                     = "redis"
  engine_version             = "7.1"
  node_type                  = var.node_type
  num_cache_clusters         = var.num_cache_nodes
  parameter_group_name       = aws_elasticache_parameter_group.this.name
  subnet_group_name          = aws_elasticache_subnet_group.this.name
  security_group_ids         = [aws_security_group.redis.id]
  port                       = 6379
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  auth_token                 = random_password.redis_auth.result

  tags = {
    Name = "${local.prefix}-redis"
  }
}
