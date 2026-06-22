variable "project_name" {
  type = string
}

variable "environment" {
  type = string
}

variable "frontend_domain" {
  type = string
  validation {
    condition     = var.frontend_domain != ""
    error_message = "frontend_domain must not be empty (e.g. app.utterai.org). Empty value generates invalid CORS origin 'https://'."
  }
}

variable "allowed_extra_origins" {
  type    = list(string)
  default = []
}

variable "tempo_bucket_enabled" {
  type        = bool
  default     = true
  description = "Whether to create the Tempo trace storage bucket."
}
