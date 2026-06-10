locals {
  prefix = "${var.project_name}-${var.environment}"
}

resource "aws_security_group" "aurora" {
  name        = "${local.prefix}-aurora-sg"
  description = "Security group for Aurora PostgreSQL"
  vpc_id      = var.vpc_id

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [var.allowed_security_group_id]
  }

  tags = {
    Name = "${local.prefix}-aurora-sg"
  }
}

resource "aws_db_subnet_group" "this" {
  name       = "${local.prefix}-aurora-subnet-group"
  subnet_ids = var.private_data_subnet_ids

  tags = {
    Name = "${local.prefix}-aurora-subnet-group"
  }
}

resource "aws_rds_cluster_parameter_group" "this" {
  name   = "${local.prefix}-aurora-pg16"
  family = "aurora-postgresql16"

  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }
}

resource "aws_rds_cluster" "this" {
  cluster_identifier          = "${local.prefix}-aurora"
  engine                      = "aurora-postgresql"
  engine_version              = "16.4"
  database_name               = var.database_name
  master_username             = var.master_username
  manage_master_user_password = true

  db_subnet_group_name            = aws_db_subnet_group.this.name
  vpc_security_group_ids          = [aws_security_group.aurora.id]
  db_cluster_parameter_group_name = aws_rds_cluster_parameter_group.this.name

  backup_retention_period      = var.backup_retention
  preferred_backup_window      = "03:00-04:00"
  preferred_maintenance_window = "sun:04:00-sun:05:00"
  skip_final_snapshot          = var.environment == "dev" ? true : false
  final_snapshot_identifier    = "${local.prefix}-aurora-final"
  deletion_protection          = var.environment == "prod" ? true : false

  apply_immediately = var.environment == "dev" ? true : false

  tags = {
    Name = "${local.prefix}-aurora"
  }
}

resource "aws_rds_cluster_instance" "writer" {
  identifier         = "${local.prefix}-aurora-writer"
  cluster_identifier = aws_rds_cluster.this.id
  instance_class     = var.instance_class
  engine             = aws_rds_cluster.this.engine
  engine_version     = aws_rds_cluster.this.engine_version

  db_subnet_group_name       = aws_db_subnet_group.this.name
  auto_minor_version_upgrade = var.environment == "dev" ? true : false

  tags = {
    Name = "${local.prefix}-aurora-writer"
  }
}
