variable "project_name" {
  type = string
}

variable "environment" {
  type = string
}

variable "cluster_name" {
  type = string
}

variable "kubernetes_version" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "private_app_subnet_ids" {
  type = list(string)
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

variable "api_node_group_enabled" {
  type    = bool
  default = true
}

variable "worker_node_group_enabled" {
  type    = bool
  default = true
}

variable "gpu_node_group_enabled" {
  type    = bool
  default = true
}

variable "api_node_instance_type" {
  type    = string
  default = "t3.medium"
}

variable "api_node_desired_size" {
  type    = number
  default = 1
}

variable "api_node_min_size" {
  type    = number
  default = 1
}

variable "api_node_max_size" {
  type    = number
  default = 2
}

variable "worker_node_instance_type" {
  type    = string
  default = "m5.xlarge"
}

variable "worker_node_disk_size" {
  type    = number
  default = 50
}

variable "worker_node_desired_size" {
  type    = number
  default = 1
}

variable "worker_node_min_size" {
  type    = number
  default = 1
}

variable "worker_node_max_size" {
  type    = number
  default = 10
}

variable "gpu_node_instance_types" {
  type    = list(string)
  default = ["g4dn.xlarge"]
}

variable "gpu_node_desired_size" {
  type    = number
  default = 1
}

variable "gpu_node_min_size" {
  type    = number
  default = 1
}

variable "gpu_node_max_size" {
  type    = number
  default = 2
}
