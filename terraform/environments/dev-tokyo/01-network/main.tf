module "vpc" {
  source = "../../../modules/vpc"

  project_name = var.project_name
  environment  = var.environment
  cluster_name = var.cluster_name
  aws_region   = var.aws_region

  vpc_cidr                  = var.vpc_cidr
  azs                       = var.azs
  public_subnet_cidrs       = var.public_subnet_cidrs
  private_app_subnet_cidrs  = var.private_app_subnet_cidrs
  private_data_subnet_cidrs = var.private_data_subnet_cidrs

  pod_cidr         = var.pod_cidr
  pod_subnet_cidrs = var.pod_subnet_cidrs
}
