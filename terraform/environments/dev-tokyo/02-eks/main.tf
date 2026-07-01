data "terraform_remote_state" "network" {
  backend = "s3"
  config = {
    bucket = "utterai-dev-terraform-state"
    key    = "dev-tokyo/network/terraform.tfstate"
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

  # dev-tokyo는 우선 Node Group을 안정적으로 띄우는 것이 목표입니다.
  # Custom Networking은 ENIConfig 생성 순서가 맞지 않으면 노드가 Ready로 합류하지 못할 수 있어
  # Agent Lab 초기 구축 단계에서는 비활성화합니다.
  vpc_cni_custom_networking_enabled = false

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

  api_node_group_enabled    = var.api_node_group_enabled
  worker_node_group_enabled = var.worker_node_group_enabled
  gpu_node_group_enabled    = var.gpu_node_group_enabled
}
