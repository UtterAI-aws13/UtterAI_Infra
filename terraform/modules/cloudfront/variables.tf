variable "project_name" {
  type = string
}

variable "environment" {
  type = string
}

variable "frontend_bucket_id" {
  type = string
}

variable "frontend_bucket_arn" {
  type = string
}

variable "alb_dns_name" {
  type        = string
  default     = ""
  description = "ALB DNS name for API proxy behavior (/api/*). Leave empty to skip."
}

variable "aliases" {
  type        = list(string)
  default     = []
  description = "Alternate domain names for the CloudFront distribution, such as app.utterai.org."
}

variable "acm_certificate_arn" {
  type        = string
  default     = ""
  description = "ACM certificate ARN in us-east-1 for CloudFront alternate domain names."
}

variable "web_acl_id" {
  type        = string
  default     = null
  description = "AWS WAFv2 Web ACL ARN to associate with this CloudFront distribution."
}
