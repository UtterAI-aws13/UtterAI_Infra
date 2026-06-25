data "aws_caller_identity" "current" {}

data "aws_kms_key" "ecr" {
  key_id = "alias/aws/ecr"
}

# ── GitHub Actions AI ECR Deploy Role ────────────────────────────────────────
# ECR repos (utterai-ai-cpu, utterai-ai-gpu)가 KMS 암호화 적용되어 있어
# AmazonEC2ContainerRegistryPowerUser 외에 kms:GenerateDataKey + kms:Decrypt 필요.

resource "aws_iam_role" "github_actions_ai" {
  name        = "utterai-dev-github-actions-ai-deploy-role"
  description = "utterai-dev-github-actions-ai-deploy-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/token.actions.githubusercontent.com"
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        StringLike = {
          "token.actions.githubusercontent.com:sub" = "repo:UtterAI-aws13/UtterAI_AI:*"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "github_actions_ai_ecr" {
  role       = aws_iam_role.github_actions_ai.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryPowerUser"
}

resource "aws_iam_role_policy" "github_actions_ai_kms" {
  name = "ecr-kms-access"
  role = aws_iam_role.github_actions_ai.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "ECRKMSAccess"
      Effect   = "Allow"
      Action   = ["kms:GenerateDataKey", "kms:Decrypt"]
      Resource = data.aws_kms_key.ecr.arn
    }]
  })
}
