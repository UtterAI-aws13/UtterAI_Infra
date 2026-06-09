variable "aws_region" {
  type    = string
  default = "ap-northeast-2"
}

variable "project_name" {
  type    = string
  default = "utterai"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "cluster_name" {
  type = string
}

variable "kubernetes_version" {
  type    = string
  default = "1.31"
}

variable "system_node_instance_type" {
  type = string
}

variable "system_node_desired_size" {
  type = number
}

variable "system_node_min_size" {
  type = number
}

variable "system_node_max_size" {
  type = number
}

variable "api_node_instance_type" {
  type = string
}

variable "api_node_desired_size" {
  type = number
}

variable "api_node_min_size" {
  type = number
}

variable "api_node_max_size" {
  type = number
}

variable "gpu_node_desired_size" {
  type    = number
  default = 0
}

variable "gpu_node_min_size" {
  type    = number
  default = 0
}
