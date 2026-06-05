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

# ── Network ───────────────────────────────────────────────────────────────────

variable "vpc_cidr" {
  type = string
}

variable "azs" {
  type = list(string)
}

variable "public_subnet_cidrs" {
  type = list(string)
}

variable "private_app_subnet_cidrs" {
  type = list(string)
}

variable "private_data_subnet_cidrs" {
  type = list(string)
}

# ── EKS ──────────────────────────────────────────────────────────────────────

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

# ── Aurora ───────────────────────────────────────────────────────────────────

variable "aurora_instance_class" {
  type = string
}

variable "aurora_database_name" {
  type    = string
  default = "utterai"
}

variable "aurora_master_username" {
  type    = string
  default = "utterai_app"
}

variable "aurora_backup_retention" {
  type    = number
  default = 3
}

# ── Redis ─────────────────────────────────────────────────────────────────────

variable "redis_node_type" {
  type = string
}

variable "redis_num_cache_nodes" {
  type    = number
  default = 1
}
