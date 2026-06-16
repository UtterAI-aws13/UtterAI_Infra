data "terraform_remote_state" "network" {
  backend = "s3"
  config = {
    bucket = "utterai-prod-terraform-state"
    key    = "prod/network/terraform.tfstate"
    region = "ap-northeast-2"
  }
}

module "eks" {
  source = "../../../modules/eks"

  project_name       = var.project_name
  environment        = var.environment
  cluster_name       = var.cluster_name
  kubernetes_version = var.kubernetes_version

  vpc_id                 = data.terraform_remote_state.network.outputs.vpc_id
  private_app_subnet_ids = data.terraform_remote_state.network.outputs.private_app_subnet_ids

  system_node_instance_type = var.system_node_instance_type
  system_node_desired_size  = var.system_node_desired_size
  system_node_min_size      = var.system_node_min_size
  system_node_max_size      = var.system_node_max_size

  api_node_instance_type = var.api_node_instance_type
  api_node_desired_size  = var.api_node_desired_size
  api_node_min_size      = var.api_node_min_size
  api_node_max_size      = var.api_node_max_size

  worker_node_instance_type = var.worker_node_instance_type
  worker_node_disk_size     = var.worker_node_disk_size

  gpu_node_desired_size = var.gpu_node_desired_size
  gpu_node_min_size     = var.gpu_node_min_size
}
