variable "project_name" {
  type = string
}

variable "environment" {
  type = string
}

variable "visibility_timeout_seconds" {
  type    = number
  default = 300
}

variable "message_retention_seconds" {
  type    = number
  default = 345600 # 4일
}

variable "max_receive_count" {
  type    = number
  default = 3
}
