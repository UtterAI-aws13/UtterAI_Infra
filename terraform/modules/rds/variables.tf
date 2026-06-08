variable "project_name" {
  type = string
}

variable "environment" {
  type = string
}

variable "instance_class" {
  type = string
}

variable "database_name" {
  type    = string
  default = "utterai"
}

variable "master_username" {
  type    = string
  default = "utterai_app"
}

variable "backup_retention" {
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

variable "cluster_security_group_id" {
  type = string
}
