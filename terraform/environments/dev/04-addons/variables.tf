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

variable "alb_dns_name" {
  type        = string
  default     = ""
  description = "ALB DNS name for CloudFront API proxy. Set after ALB is created."
}
