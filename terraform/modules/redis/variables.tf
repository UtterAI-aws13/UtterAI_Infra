variable "project_name" {
  type = string
}

variable "environment" {
  type = string
}

variable "node_type" {
  type = string
}

variable "num_cache_nodes" {
  type    = number
  default = 1
}

variable "vpc_id" {
  type = string
}

variable "private_data_subnet_ids" {
  type = list(string)
}

variable "allowed_security_group_id" {
  type = string
}
