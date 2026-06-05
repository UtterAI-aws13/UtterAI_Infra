variable "cluster_name" {
  type = string
}

variable "cluster_endpoint" {
  type = string
}

variable "aws_region" {
  type = string
}

variable "lbc_irsa_role_arn" {
  type = string
}

variable "cluster_autoscaler_irsa_role_arn" {
  type = string
}
