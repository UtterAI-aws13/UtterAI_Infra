variable "project_name" {
  type = string
}

variable "environment" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "private_app_subnet_ids" {
  type        = list(string)
  description = "EFS 마운트 타깃을 생성할 서브넷 목록 (EKS 노드가 위치한 private_app 서브넷)"
}

variable "node_security_group_id" {
  type        = string
  description = "EKS 노드 SG — EFS SG의 NFS 인바운드 허용 대상"
}
