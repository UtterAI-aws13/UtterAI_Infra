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

variable "rds_instance_class" {
  type = string
}

variable "redis_node_type" {
  type = string
}

variable "redis_num_cache_nodes" {
  type    = number
  default = 1
}
