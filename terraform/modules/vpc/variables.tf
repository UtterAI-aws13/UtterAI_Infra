variable "project_name" {
  type = string
}

variable "environment" {
  type = string
}

variable "cluster_name" {
  type = string
}

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

variable "pod_cidr" {
  type        = string
  description = "Secondary CIDR block for pod IPs (e.g. 100.64.0.0/16)"
  default     = null
}

variable "pod_subnet_cidrs" {
  type        = list(string)
  description = "Per-AZ CIDR blocks carved from pod_cidr for pod-only ENIs"
  default     = []
}
