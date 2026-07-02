data "aws_caller_identity" "current" {}

data "terraform_remote_state" "services" {
  backend = "s3"
  config = {
    bucket = "utterai-dev-terraform-state"
    key    = "dev/services/terraform.tfstate"
    region = "ap-northeast-2"
  }
}

# ── AgentCore Gateway: 리포트 생성 근거 검색 tool 모음 ────────────────────────
# 지금은 KURE-v1 pgvector 검색(search_evidence) tool 하나만 등록한다.
# 향후 5-Agent 리포트 생성 설계(Evidence Research Agent)에서 국내/해외 문헌 검색,
# 임상 가이드 검색, hybrid search, rerank 등을 별도 tool로 추가할 때는
# 이 Gateway에 aws_bedrockagentcore_gateway_target 리소스만 추가하면 된다.
# Gateway 자체를 다시 만들 필요는 없다.

locals {
  gateway_name = "utterai-${var.environment}-report-evidence-gateway"
}

data "aws_iam_policy_document" "report_evidence_gateway_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "report_evidence_gateway" {
  name               = "${local.gateway_name}-role"
  assume_role_policy = data.aws_iam_policy_document.report_evidence_gateway_assume.json
}

# gateway_iam_role 인증 방식 사용 시, Gateway가 대상 Lambda를 직접 invoke할 수 있어야 한다.
resource "aws_iam_role_policy" "report_evidence_gateway_invoke_targets" {
  name = "invoke-lambda-targets"
  role = aws_iam_role.report_evidence_gateway.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "InvokeKureRetriever"
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = data.terraform_remote_state.services.outputs.kure_retriever_lambda_arn
      },
    ]
  })
}

resource "aws_bedrockagentcore_gateway" "report_evidence" {
  name        = local.gateway_name
  description = "리포트 생성 Agent가 사용하는 근거 검색 tool 모음 (KURE-v1 pgvector 등)"
  role_arn    = aws_iam_role.report_evidence_gateway.arn

  # 우리 자체 Strands/AgentCore Runtime이 호출하므로 IAM SigV4 인증을 쓴다.
  # 외부 클라이언트가 직접 호출해야 하는 상황이 되면 CUSTOM_JWT(Cognito 등)로 전환한다.
  authorizer_type = "AWS_IAM"
  protocol_type   = "MCP"
}

resource "aws_bedrockagentcore_gateway_target" "search_evidence" {
  name               = "search-evidence"
  gateway_identifier = aws_bedrockagentcore_gateway.report_evidence.gateway_id
  description        = "KURE-v1 임베딩 + pgvector 검색으로 논문/가이드 근거를 반환한다"

  credential_provider_configuration {
    gateway_iam_role {}
  }

  target_configuration {
    mcp {
      lambda {
        lambda_arn = data.terraform_remote_state.services.outputs.kure_retriever_lambda_arn

        tool_schema {
          inline_payload {
            name        = "search_evidence"
            description = "국내외 언어재활 논문, 임상 가이드에서 쿼리와 관련된 근거 chunk를 검색한다."

            input_schema {
              type        = "object"
              description = "근거 검색 쿼리"

              property {
                name        = "query"
                type        = "string"
                description = "검색할 자연어 쿼리"
                required    = true
              }

              property {
                name        = "top_k"
                type        = "number"
                description = "반환할 최대 결과 수 (기본 5)"
              }
            }
          }
        }
      }
    }
  }
}
