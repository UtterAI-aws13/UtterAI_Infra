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
# Evidence Research Agent(app.agents.report_generation.evidence_research)의 7개
# tool을 전부 이 Gateway에 등록한다. 7개 target 전부 kure_retriever Lambda
# 하나를 가리키며, 어떤 tool이 호출됐는지는 Lambda가 context.client_context.custom
# .bedrockAgentCoreToolName으로 구분해 분기한다(lambda/kure_retriever/handler.py).
# 멀티 에이전트 설계에서 Evidence Research Agent가 여러 검색 "전략" 중 하나를
# 스스로 고르는 agentic한 tool 선택을 그대로 보여주기 위해 7개를 유지하고 하나로
# 합치지 않았다.

locals {
  gateway_name = "utterai-${var.environment}-report-evidence-gateway"
}

data "aws_region" "current" {}

data "aws_iam_policy_document" "report_evidence_gateway_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }

    # AWS의 confused-deputy 방지 요구사항 - 이 조건이 없으면 Gateway가 실제로
    # target을 만들 때 "not authorized to perform AssumeRole" 에러가 난다
    # (역할 자체는 만들어지지만 AssumeRole 시점에 검증에서 막힘, 실제 apply로 확인함).
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values = [
        "arn:aws:bedrock-agentcore:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:gateway/${local.gateway_name}*"
      ]
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

resource "aws_bedrockagentcore_gateway_target" "search_korean_evidence" {
  name               = "search-korean-evidence"
  gateway_identifier = aws_bedrockagentcore_gateway.report_evidence.gateway_id
  description        = "국내 언어재활 논문/자료를 한국어 쿼리로 검색한다 (Kiwi+ontology+필터+벡터검색)"

  credential_provider_configuration {
    gateway_iam_role {}
  }

  target_configuration {
    mcp {
      lambda {
        lambda_arn = data.terraform_remote_state.services.outputs.kure_retriever_lambda_arn

        tool_schema {
          inline_payload {
            name        = "search_korean_evidence"
            description = "국내 언어재활 논문/자료를 한국어 쿼리로 검색한다."

            input_schema {
              type = "object"

              property {
                name        = "query_ko"
                type        = "string"
                description = "한국어 자연어 쿼리"
                required    = true
              }

              property {
                name        = "top_k"
                type        = "number"
                description = "반환할 최대 결과 수 (기본 10)"
              }
            }
          }
        }
      }
    }
  }
}

resource "aws_bedrockagentcore_gateway_target" "search_international_evidence" {
  name               = "search-international-evidence"
  gateway_identifier = aws_bedrockagentcore_gateway.report_evidence.gateway_id
  description        = "해외 언어재활 논문을 검색한다 (현재는 한국어 corpus와 동일 인덱스)"

  credential_provider_configuration {
    gateway_iam_role {}
  }

  target_configuration {
    mcp {
      lambda {
        lambda_arn = data.terraform_remote_state.services.outputs.kure_retriever_lambda_arn

        tool_schema {
          inline_payload {
            name        = "search_international_evidence"
            description = "해외 언어재활 논문을 검색한다. KURE-v1은 한국어 특화 모델이라 search_korean_evidence와 동일 corpus를 검색한다."

            input_schema {
              type = "object"

              property {
                name        = "query_en"
                type        = "string"
                description = "영어 자연어 쿼리"
                required    = true
              }

              property {
                name        = "top_k"
                type        = "number"
                description = "반환할 최대 결과 수 (기본 10)"
              }
            }
          }
        }
      }
    }
  }
}

resource "aws_bedrockagentcore_gateway_target" "search_clinical_guidelines" {
  name               = "search-clinical-guidelines"
  gateway_identifier = aws_bedrockagentcore_gateway.report_evidence.gateway_id
  description        = "임상 가이드(source_type=clinical_guide) 문서만 검색한다"

  credential_provider_configuration {
    gateway_iam_role {}
  }

  target_configuration {
    mcp {
      lambda {
        lambda_arn = data.terraform_remote_state.services.outputs.kure_retriever_lambda_arn

        tool_schema {
          inline_payload {
            name        = "search_clinical_guidelines"
            description = "임상 가이드(source_type=clinical_guide) 문서만 검색한다."

            input_schema {
              type = "object"

              property {
                name        = "query"
                type        = "string"
                description = "한국어 자연어 쿼리"
                required    = true
              }

              property {
                name        = "top_k"
                type        = "number"
                description = "반환할 최대 결과 수 (기본 10)"
              }
            }
          }
        }
      }
    }
  }
}

resource "aws_bedrockagentcore_gateway_target" "hybrid_search" {
  name               = "hybrid-search"
  gateway_identifier = aws_bedrockagentcore_gateway.report_evidence.gateway_id
  description        = "벡터 검색 + Kiwi 키워드 기반 ontology 확장/메타데이터 필터를 결합한 검색"

  credential_provider_configuration {
    gateway_iam_role {}
  }

  target_configuration {
    mcp {
      lambda {
        lambda_arn = data.terraform_remote_state.services.outputs.kure_retriever_lambda_arn

        tool_schema {
          inline_payload {
            name        = "hybrid_search"
            description = "벡터 검색 + Kiwi 키워드 기반 ontology 확장/메타데이터 필터를 결합한 검색."

            input_schema {
              type = "object"

              property {
                name        = "query"
                type        = "string"
                description = "한국어 자연어 쿼리"
                required    = true
              }

              property {
                name        = "top_k"
                type        = "number"
                description = "반환할 최대 결과 수 (기본 10)"
              }
            }
          }
        }
      }
    }
  }
}

resource "aws_bedrockagentcore_gateway_target" "rerank_evidence" {
  name               = "rerank-evidence"
  gateway_identifier = aws_bedrockagentcore_gateway.report_evidence.gateway_id
  description        = "검색 후보를 query와의 관련도 기준으로 재정렬한다"

  credential_provider_configuration {
    gateway_iam_role {}
  }

  target_configuration {
    mcp {
      lambda {
        lambda_arn = data.terraform_remote_state.services.outputs.kure_retriever_lambda_arn

        tool_schema {
          inline_payload {
            name        = "rerank_evidence"
            description = "search_* tool이 반환한 근거 후보 목록을 query와의 관련도로 재정렬한다."

            input_schema {
              type = "object"

              property {
                name        = "candidates"
                type        = "array"
                description = "search_* tool이 반환한 근거 후보 목록"
                required    = true

                items {
                  type = "object"

                  property {
                    name = "evidence_id"
                    type = "string"
                  }

                  property {
                    name = "source_id"
                    type = "string"
                  }

                  property {
                    name = "chunk_id"
                    type = "string"
                  }

                  property {
                    name = "text"
                    type = "string"
                  }

                  property {
                    name = "retrieval_score"
                    type = "number"
                  }
                }
              }

              property {
                name        = "query"
                type        = "string"
                description = "원 쿼리 텍스트"
                required    = true
              }
            }
          }
        }
      }
    }
  }
}

resource "aws_bedrockagentcore_gateway_target" "fetch_document_section" {
  name               = "fetch-document-section"
  gateway_identifier = aws_bedrockagentcore_gateway.report_evidence.gateway_id
  description        = "chunk_id로 원문 청크 전체를 조회한다 (인용 범위 확인용)"

  credential_provider_configuration {
    gateway_iam_role {}
  }

  target_configuration {
    mcp {
      lambda {
        lambda_arn = data.terraform_remote_state.services.outputs.kure_retriever_lambda_arn

        tool_schema {
          inline_payload {
            name        = "fetch_document_section"
            description = "chunk_id로 원문 청크 전체를 조회한다 (인용 범위 확인용)."

            input_schema {
              type = "object"

              property {
                name        = "chunk_id"
                type        = "string"
                description = "search_* tool이 반환한 chunk_id"
                required    = true
              }
            }
          }
        }
      }
    }
  }
}

resource "aws_bedrockagentcore_gateway_target" "resolve_ontology_synonyms" {
  name               = "resolve-ontology-synonyms"
  gateway_identifier = aws_bedrockagentcore_gateway.report_evidence.gateway_id
  description        = "ontology.yaml 기반으로 term과 연관된 동의어/관련어를 확장한다"

  credential_provider_configuration {
    gateway_iam_role {}
  }

  target_configuration {
    mcp {
      lambda {
        lambda_arn = data.terraform_remote_state.services.outputs.kure_retriever_lambda_arn

        tool_schema {
          inline_payload {
            name        = "resolve_ontology_synonyms"
            description = "ontology.yaml 기반으로 term과 연관된 동의어/관련어를 확장한다."

            input_schema {
              type = "object"

              property {
                name        = "term"
                type        = "string"
                description = "확장할 임상 개념 또는 지표명 (예: 'MLU', '음운 인식')"
                required    = true
              }
            }
          }
        }
      }
    }
  }
}
