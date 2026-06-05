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
  type = string
}

variable "master_username" {
  type = string
}

variable "backup_retention" {
  type = number
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
