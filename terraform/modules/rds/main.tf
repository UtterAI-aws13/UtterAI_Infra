locals {
  prefix = "${var.project_name}-${var.environment}"
}

resource "aws_security_group" "rds" {
  name        = "${local.prefix}-rds-sg"
  description = "Security group for RDS PostgreSQL"
  vpc_id      = var.vpc_id

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [var.allowed_security_group_id]
  }

  tags = {
    Name = "${local.prefix}-rds-sg"
  }
}

resource "aws_db_subnet_group" "this" {
  name       = "${local.prefix}-rds-subnet-group"
  subnet_ids = var.private_data_subnet_ids

  tags = {
    Name = "${local.prefix}-rds-subnet-group"
  }
}

resource "aws_db_parameter_group" "this" {
  name   = "${local.prefix}-rds-pg16"
  family = "postgres16"

  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }
}

resource "aws_db_instance" "this" {
  identifier     = "${local.prefix}-rds"
  engine         = "postgres"
  engine_version = "16.4"
  instance_class = var.instance_class

  db_name                     = var.database_name
  username                    = var.master_username
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  parameter_group_name   = aws_db_parameter_group.this.name

  allocated_storage     = 20
  max_allocated_storage = 100
  storage_type          = "gp3"
  storage_encrypted     = true

  backup_retention_period    = var.backup_retention
  backup_window              = "03:00-04:00"
  maintenance_window         = "sun:04:00-sun:05:00"
  skip_final_snapshot        = true
  deletion_protection        = false
  apply_immediately          = true
  auto_minor_version_upgrade = true

  tags = {
    Name = "${local.prefix}-rds"
  }
}
