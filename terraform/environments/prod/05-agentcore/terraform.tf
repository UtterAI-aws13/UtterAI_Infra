terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # aws_bedrockagentcore_gateway / aws_bedrockagentcore_gateway_target는
      # provider 6.x부터 제공된다. 다른 스택(00-iam/01-network/02-eks/03-services/04-addons)은
      # ~> 5.0에 고정되어 있으므로, breaking change 영향을 격리하기 위해
      # 이 스택만 별도로 6.x를 사용한다.
      version = "~> 6.0"
    }
  }

  backend "s3" {
    bucket       = "utterai-prod-terraform-state"
    key          = "prod/agentcore/terraform.tfstate"
    region       = "ap-northeast-2"
    use_lockfile = true
    encrypt      = true
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
