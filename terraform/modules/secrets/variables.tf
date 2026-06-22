variable "project_name" {
  type = string
}

variable "environment" {
  type = string
}

variable "rag_ingest_secret_enabled" {
  type        = bool
  description = "Create the RAG ingest worker secret placeholder in Secrets Manager."
  default     = false
}
