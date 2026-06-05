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

