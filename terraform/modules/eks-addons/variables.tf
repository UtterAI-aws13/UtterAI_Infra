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

variable "karpenter_irsa_role_arn" {
  type = string
}

variable "keda_irsa_role_arn" {
  type = string
}

variable "karpenter_node_role_name" {
  type = string
}

variable "karpenter_sqs_queue_url" {
  type = string
}

variable "karpenter_sqs_queue_arn" {
  type = string
}
