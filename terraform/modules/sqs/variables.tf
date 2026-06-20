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

variable "audio_preprocess_visibility_timeout_seconds" {
  type    = number
  default = 900
}

variable "message_retention_seconds" {
  type    = number
  default = 345600 # 4일
}

variable "max_receive_count" {
  type    = number
  default = 3
}

variable "gpu_visibility_timeout_seconds" {
  type    = number
  default = 1800
}

variable "gpu_max_receive_count" {
  type    = number
  default = 3
}

variable "report_visibility_timeout_seconds" {
  type    = number
  default = 900
}

variable "report_max_receive_count" {
  type    = number
  default = 3
}
