variable "project_name" {
  type = string
}

variable "environment" {
  type = string
}

variable "frontend_domain" {
  type = string
}

variable "allowed_extra_origins" {
  type    = list(string)
  default = []
}
