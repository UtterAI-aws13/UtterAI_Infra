variable "account_id" {
  description = "The only AWS account this configuration may touch."
  type        = string
}

variable "region" {
  type    = string
  default = "ap-northeast-2"
}

variable "name" {
  type    = string
  default = "slack-worker"
}

variable "lambda_zip_path" {
  type    = string
  default = "../../../../dist/lambda.zip"
}

variable "signing_secret_arn" {
  description = "Created and populated outside Terraform so the value never enters state."
  type        = string
}

variable "bot_secret_arn" {
  type = string
}

variable "slack" {
  type = object({
    app_id   = string
    team_id  = string
    channels = list(string)
    users    = list(string)
    bot_id   = string
  })
}

variable "worker_image" {
  type = string
}

variable "bedrock_model_id" {
  type = string
}

variable "bedrock_model_arns" {
  type = list(string)
}
