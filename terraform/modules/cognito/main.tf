locals {
  prefix = "${var.project_name}-${var.environment}"
}

resource "aws_cognito_user_pool" "this" {
  name = "${local.prefix}-user-pool"

  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  password_policy {
    minimum_length    = 8
    require_lowercase = true
    require_numbers   = true
    require_symbols   = false
    require_uppercase = true
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  email_configuration {
    email_sending_account = "COGNITO_DEFAULT"
  }

  tags = {
    Name = "${local.prefix}-user-pool"
  }
}

resource "aws_cognito_user_pool_client" "web" {
  name         = "${local.prefix}-web-client"
  user_pool_id = aws_cognito_user_pool.this.id

  explicit_auth_flows = [
    "ALLOW_USER_PASSWORD_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
    "ALLOW_USER_SRP_AUTH",
  ]

  callback_urls = [var.callback_url]
  logout_urls   = [var.logout_url]

  token_validity_units {
    access_token  = "hours"
    refresh_token = "days"
  }

  access_token_validity  = 1
  refresh_token_validity = 30

  prevent_user_existence_errors = "ENABLED"
}
