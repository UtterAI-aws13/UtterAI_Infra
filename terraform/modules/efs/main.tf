locals {
  prefix = "${var.project_name}-${var.environment}"
}

resource "aws_efs_file_system" "model_cache" {
  encrypted       = true
  throughput_mode = "bursting"

  tags = {
    Name = "${local.prefix}-model-cache"
  }
}

resource "aws_security_group" "efs" {
  name        = "${local.prefix}-efs-sg"
  description = "NFS inbound from EKS nodes for EFS model cache"
  vpc_id      = var.vpc_id

  ingress {
    from_port       = 2049
    to_port         = 2049
    protocol        = "tcp"
    security_groups = [var.node_security_group_id]
  }

  tags = {
    Name = "${local.prefix}-efs-sg"
  }
}

resource "aws_efs_mount_target" "model_cache" {
  for_each = toset(var.private_app_subnet_ids)

  file_system_id  = aws_efs_file_system.model_cache.id
  subnet_id       = each.value
  security_groups = [aws_security_group.efs.id]
}
