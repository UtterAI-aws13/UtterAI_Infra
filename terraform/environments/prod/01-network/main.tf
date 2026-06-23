module "vpc" {
  source = "../../../modules/vpc"

  project_name = var.project_name
  environment  = var.environment
  cluster_name = var.cluster_name

  vpc_cidr                  = var.vpc_cidr
  azs                       = var.azs
  public_subnet_cidrs       = var.public_subnet_cidrs
  private_app_subnet_cidrs  = var.private_app_subnet_cidrs
  private_data_subnet_cidrs = var.private_data_subnet_cidrs

  pod_cidr         = var.pod_cidr
  pod_subnet_cidrs = var.pod_subnet_cidrs
}

# ── Client VPN ────────────────────────────────────────────────────────────────

locals {
  prefix = "${var.project_name}-${var.environment}"
}

resource "aws_security_group" "vpn" {
  name        = "${local.prefix}-vpn-sg"
  description = "Security group for Client VPN endpoint"
  vpc_id      = module.vpc.vpc_id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${local.prefix}-vpn-sg"
  }
}

resource "aws_ec2_client_vpn_endpoint" "this" {
  description            = "${local.prefix}-client-vpn"
  server_certificate_arn = var.vpn_server_certificate_arn
  client_cidr_block      = "172.16.0.0/22"
  vpc_id                 = module.vpc.vpc_id
  security_group_ids     = [aws_security_group.vpn.id]

  authentication_options {
    type                       = "certificate-authentication"
    root_certificate_chain_arn = var.vpn_ca_certificate_arn
  }

  connection_log_options {
    enabled = false
  }

  tags = {
    Name = "${local.prefix}-client-vpn"
  }
}

resource "aws_ec2_client_vpn_network_association" "this" {
  client_vpn_endpoint_id = aws_ec2_client_vpn_endpoint.this.id
  subnet_id              = module.vpc.private_app_subnet_ids[0]
}

resource "aws_ec2_client_vpn_authorization_rule" "vpc" {
  client_vpn_endpoint_id = aws_ec2_client_vpn_endpoint.this.id
  target_network_cidr    = var.vpc_cidr
  authorize_all_groups   = true
}
