data "terraform_remote_state" "network" {
  backend = "s3"
  config = {
    bucket = "utterai-prod-terraform-state"
    key    = "prod/network/terraform.tfstate"
    region = "ap-northeast-2"
  }
}

data "terraform_remote_state" "eks" {
  backend = "s3"
  config = {
    bucket = "utterai-prod-terraform-state"
    key    = "prod/platform/terraform.tfstate"
    region = "ap-northeast-2"
  }
}

data "aws_caller_identity" "current" {}

# ── RDS ──────────────────────────────────────────────────────────────────────

module "rds" {
  source = "../../../modules/rds"

  project_name   = var.project_name
  environment    = var.environment
  instance_class = var.rds_instance_class

  vpc_id                    = data.terraform_remote_state.network.outputs.vpc_id
  private_data_subnet_ids   = data.terraform_remote_state.network.outputs.private_data_subnet_ids
  allowed_security_group_id = data.terraform_remote_state.eks.outputs.node_security_group_id
  cluster_security_group_id = data.terraform_remote_state.eks.outputs.cluster_security_group_id

  skip_final_snapshot = false
  deletion_protection = true
}

# ── Redis ─────────────────────────────────────────────────────────────────────

module "redis" {
  source = "../../../modules/redis"

  project_name    = var.project_name
  environment     = var.environment
  node_type       = var.redis_node_type
  num_cache_nodes = var.redis_num_cache_nodes

  vpc_id                    = data.terraform_remote_state.network.outputs.vpc_id
  private_data_subnet_ids   = data.terraform_remote_state.network.outputs.private_data_subnet_ids
  allowed_security_group_id = data.terraform_remote_state.eks.outputs.node_security_group_id
  cluster_security_group_id = data.terraform_remote_state.eks.outputs.cluster_security_group_id
}

# ── S3 ───────────────────────────────────────────────────────────────────────

module "s3" {
  source = "../../../modules/s3"

  project_name          = var.project_name
  environment           = var.environment
  frontend_domain       = var.frontend_domain
  allowed_extra_origins = var.allowed_extra_origins
}

# ── SQS ──────────────────────────────────────────────────────────────────────

module "sqs" {
  source = "../../../modules/sqs"

  project_name = var.project_name
  environment  = var.environment

  audio_preprocess_visibility_timeout_seconds = 900
  gpu_visibility_timeout_seconds              = 1800
  gpu_max_receive_count                       = 3
  report_visibility_timeout_seconds           = 900
  report_max_receive_count                    = 3
}

# ── Secrets Manager ──────────────────────────────────────────────────────────

module "secrets" {
  source = "../../../modules/secrets"

  project_name              = var.project_name
  environment               = var.environment
  rag_ingest_secret_enabled = true
}

# ── Karpenter Interruption Queue ─────────────────────────────────────────────

resource "aws_sqs_queue" "karpenter_interruption" {
  name                      = var.cluster_name
  message_retention_seconds = 300
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue_policy" "karpenter_interruption" {
  queue_url = aws_sqs_queue.karpenter_interruption.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = ["events.amazonaws.com", "sqs.amazonaws.com"] }
        Action    = "sqs:SendMessage"
        Resource  = aws_sqs_queue.karpenter_interruption.arn
      }
    ]
  })
}

resource "aws_cloudwatch_event_rule" "karpenter_spot_interruption" {
  name        = "${var.cluster_name}-spot-interruption"
  description = "Karpenter spot interruption"

  event_pattern = jsonencode({
    source      = ["aws.ec2"]
    detail-type = ["EC2 Spot Instance Interruption Warning", "EC2 Instance Rebalance Recommendation", "EC2 Instance State-change Notification"]
  })
}

resource "aws_cloudwatch_event_target" "karpenter_spot_interruption" {
  rule = aws_cloudwatch_event_rule.karpenter_spot_interruption.name
  arn  = aws_sqs_queue.karpenter_interruption.arn
}

# ── Lambda: 논문 수집 (월 1회 자동 실행) ─────────────────────────────────────

data "archive_file" "collect_papers" {
  type        = "zip"
  source_file = "${path.module}/../../../../../UtterAI_AI/app/lambda/collect_papers_handler.py"
  output_path = "${path.module}/collect_papers_handler.zip"
}

resource "aws_iam_role" "collect_papers_lambda" {
  name = "utterai-${var.environment}-collect-papers-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "collect_papers_basic" {
  role       = aws_iam_role.collect_papers_lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "collect_papers_permissions" {
  role = aws_iam_role.collect_papers_lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "SecretsManager"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = module.secrets.collect_papers_secret_arn
      },
      {
        Sid      = "S3RagBucket"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${module.s3.rag_ingest_bucket_arn}/*"
      },
      {
        Sid      = "SqsIngest"
        Effect   = "Allow"
        Action   = "sqs:SendMessage"
        Resource = module.sqs.rag_ingest_queue_arn
      },
      {
        Sid      = "Bedrock"
        Effect   = "Allow"
        Action   = "bedrock:InvokeModel"
        Resource = "arn:aws:bedrock:${var.aws_region}::foundation-model/*"
      },
    ]
  })
}

resource "aws_lambda_function" "collect_papers" {
  function_name    = "utterai-${var.environment}-collect-papers"
  role             = aws_iam_role.collect_papers_lambda.arn
  filename         = data.archive_file.collect_papers.output_path
  source_code_hash = data.archive_file.collect_papers.output_base64sha256
  handler          = "collect_papers_handler.handler"
  runtime          = "python3.12"
  timeout          = 900

  environment {
    variables = {
      SECRET_ID                = "utterai-${var.environment}/collect-papers-secret"
      S3_BUCKET_RAG            = "utterai-${var.environment}-rag-ingest"
      SQS_RAG_INGEST_QUEUE_URL = module.sqs.rag_ingest_queue_url
      AWS_REGION_NAME          = var.aws_region
      BEDROCK_REGION           = var.aws_region
      PAPERS_LIMIT             = "20"
    }
  }
}

resource "aws_cloudwatch_event_rule" "collect_papers_monthly" {
  name                = "utterai-${var.environment}-collect-papers-monthly"
  description         = "월 1회 논문 수집 Lambda 트리거 (매월 1일 오전 9시 KST = UTC 00:00)"
  schedule_expression = "cron(0 0 1 * ? *)"
}

resource "aws_cloudwatch_event_target" "collect_papers" {
  rule = aws_cloudwatch_event_rule.collect_papers_monthly.name
  arn  = aws_lambda_function.collect_papers.arn
}

resource "aws_lambda_permission" "collect_papers_eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.collect_papers.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.collect_papers_monthly.arn
}

# ── Lambda: KURE-v1 검색 (AgentCore search_evidence tool) ────────────────────
# AgentCore Gateway → Lambda → KURE-v1 임베딩 → pgvector 검색 → 근거 반환
# 핸들러 소스: UtterAI_AI/lambda/kure_retriever/handler.py
# ECR 레포는 dev/03-services에서 관리한다 (계정 공유 리소스).

locals {
  kure_lambda_name         = "utterai-${var.environment}-kure-retriever"
  kure_retriever_image_uri = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com/utterai-kure-retriever:latest"
}

resource "aws_security_group" "kure_retriever_lambda" {
  name        = "${local.kure_lambda_name}-sg"
  description = "KURE retriever Lambda - pgvector RDS access"
  vpc_id      = data.terraform_remote_state.network.outputs.vpc_id

  egress {
    description = "RDS PostgreSQL"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "HTTPS (Secrets Manager, ECR image pull)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${local.kure_lambda_name}-sg"
  }
}

# Lambda SG에서 RDS SG로 인그레스 허용
resource "aws_security_group_rule" "rds_allow_kure_lambda" {
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  security_group_id        = module.rds.security_group_id
  source_security_group_id = aws_security_group.kure_retriever_lambda.id
  description              = "KURE retriever Lambda to RDS"
}

resource "aws_iam_role" "kure_retriever_lambda" {
  name = "${local.kure_lambda_name}-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "kure_retriever_vpc_execution" {
  role       = aws_iam_role.kure_retriever_lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_role_policy" "kure_retriever_permissions" {
  role = aws_iam_role.kure_retriever_lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "RdsSecret"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = module.rds.db_secret_arn
      },
      {
        Sid      = "EcrImagePull"
        Effect   = "Allow"
        Action   = ["ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage", "ecr:GetAuthorizationToken"]
        Resource = "*"
      },
    ]
  })
}

resource "aws_lambda_function" "kure_retriever" {
  function_name = local.kure_lambda_name
  role          = aws_iam_role.kure_retriever_lambda.arn
  package_type  = "Image"
  image_uri     = local.kure_retriever_image_uri

  memory_size = 3008
  # 실제 검색(모델 로딩 + Kiwi/ontology + pgvector 쿼리)이 콜드 상태에서 15~47초
  # 걸리는 것을 실측해 30초보다 여유 있게 60초로 잡았다. AgentCore Gateway 자체의
  # tool 호출 타임아웃(~30초)은 이 값과 별개이며 Lambda 쪽에서 조정 불가하다 -
  # CloudWatch warmup(5분 간격)으로 컨테이너를 최대한 웜 상태로 유지해 완화한다.
  timeout = 60

  vpc_config {
    subnet_ids         = data.terraform_remote_state.network.outputs.private_app_subnet_ids
    security_group_ids = [aws_security_group.kure_retriever_lambda.id]
  }

  environment {
    variables = {
      DB_HOST        = module.rds.endpoint
      DB_PORT        = "5432"
      DB_NAME        = "utterai"
      RDS_SECRET_ARN = module.rds.db_secret_arn
      # 없으면 app.config.Settings.app_env 기본값("local")으로 떨어져 DB 연결이
      # sslmode=disable로 시도되고, RDS가 "no pg_hba.conf entry ... no encryption"로
      # 거부한다 - 실제 호출로 확인한 진짜 원인 (모델 로딩 속도 문제가 아니었다).
      APP_ENV = var.environment
    }
  }

  lifecycle {
    ignore_changes = [image_uri]
  }
}

resource "aws_cloudwatch_event_rule" "kure_retriever_warmup" {
  name                = "${local.kure_lambda_name}-warmup"
  description         = "KURE retriever Lambda warmup - prevent cold start (every 5 min)"
  schedule_expression = "rate(5 minutes)"
}

resource "aws_cloudwatch_event_target" "kure_retriever_warmup" {
  rule  = aws_cloudwatch_event_rule.kure_retriever_warmup.name
  arn   = aws_lambda_function.kure_retriever.arn
  input = jsonencode({ query = "ping", top_k = 1 })
}

resource "aws_lambda_permission" "kure_retriever_warmup" {
  statement_id  = "AllowWarmupEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.kure_retriever.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.kure_retriever_warmup.arn
}

resource "aws_lambda_permission" "kure_retriever_agentcore" {
  statement_id  = "AllowAgentCoreGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.kure_retriever.function_name
  principal     = "bedrock.amazonaws.com"
}

# ── Lambda: FinOps Agent ──────────────────────────────────────────────────────
# finops-query  : Cost Explorer tool dispatcher (no VPC, public endpoint)
# finops-agent  : Claude agentic loop — receives natural-language question,
#                 calls finops-query via tool_use, returns Korean answer

locals {
  finops_query_name = "utterai-${var.environment}-finops-query"
  finops_agent_name = "utterai-${var.environment}-finops-agent"
}

# ── finops-query ──────────────────────────────────────────────────────────────

data "archive_file" "finops_query" {
  type        = "zip"
  source_dir  = "${path.module}/../../../../lambda/finops_query"
  output_path = "${path.module}/finops_query.zip"
}

resource "aws_iam_role" "finops_query" {
  name = "${local.finops_query_name}-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_security_group" "finops_query" {
  name        = "${local.finops_query_name}-sg"
  description = "FinOps query Lambda - Kubecost ALB + AWS APIs"
  vpc_id      = data.terraform_remote_state.network.outputs.vpc_id

  egress {
    description = "Kubecost internal ALB"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "HTTPS (Cost Explorer, Secrets Manager)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${local.finops_query_name}-sg"
  }
}

resource "aws_iam_role_policy_attachment" "finops_query_basic" {
  role       = aws_iam_role.finops_query.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "finops_query_vpc" {
  role       = aws_iam_role.finops_query.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_role_policy" "finops_query_ce" {
  role = aws_iam_role.finops_query.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "CostExplorer"
      Effect = "Allow"
      Action = [
        "ce:GetCostAndUsage",
        "ce:GetCostForecast",
        "ce:GetDimensionValues",
        "ce:GetTags",
      ]
      Resource = "*"
    }]
  })
}

resource "aws_lambda_function" "finops_query" {
  function_name    = local.finops_query_name
  role             = aws_iam_role.finops_query.arn
  filename         = data.archive_file.finops_query.output_path
  source_code_hash = data.archive_file.finops_query.output_base64sha256
  handler          = "handler.handler"
  runtime          = "python3.12"
  timeout          = 30
  memory_size      = 256

  vpc_config {
    subnet_ids         = data.terraform_remote_state.network.outputs.private_app_subnet_ids
    security_group_ids = [aws_security_group.finops_query.id]
  }

  environment {
    variables = {
      KUBECOST_ENDPOINT        = var.kubecost_alb_endpoint
      ATHENA_DATABASE          = aws_glue_catalog_database.finops.name
      ATHENA_TABLE             = aws_glue_catalog_table.finops_cur.name
      ATHENA_WORKGROUP         = aws_athena_workgroup.finops.name
      SPOT_TRACKING_START_DATE = var.spot_tracking_start_date
    }
  }
}

# ── finops-agent ──────────────────────────────────────────────────────────────

data "archive_file" "finops_agent" {
  type        = "zip"
  source_file = "${path.module}/../../../../lambda/finops_agent/handler.py"
  output_path = "${path.module}/finops_agent.zip"
}

resource "aws_iam_role" "finops_agent" {
  name = "${local.finops_agent_name}-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "finops_agent_basic" {
  role       = aws_iam_role.finops_agent.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "finops_agent_permissions" {
  role = aws_iam_role.finops_agent.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "BedrockInvoke"
        Effect = "Allow"
        Action = "bedrock:InvokeModel"
        Resource = [
          "arn:aws:bedrock:*::foundation-model/*",
          "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*",
        ]
      },
      {
        Sid      = "InvokeFinopsQuery"
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = aws_lambda_function.finops_query.arn
      },
    ]
  })
}

resource "aws_lambda_function" "finops_agent" {
  function_name    = local.finops_agent_name
  role             = aws_iam_role.finops_agent.arn
  filename         = data.archive_file.finops_agent.output_path
  source_code_hash = data.archive_file.finops_agent.output_base64sha256
  handler          = "handler.handler"
  runtime          = "python3.12"
  timeout          = 60
  memory_size      = 512

  environment {
    variables = {
      FINOPS_QUERY_LAMBDA_ARN = aws_lambda_function.finops_query.arn
      BEDROCK_REGION          = var.aws_region
      BEDROCK_MODEL_ID        = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
    }
  }
}

# ── Lambda: finops-slack (Slack slash command → finops-agent 비동기 호출) ─────

locals {
  finops_slack_name = "utterai-${var.environment}-finops-slack"
}

data "archive_file" "finops_slack" {
  type        = "zip"
  source_file = "${path.module}/../../../../lambda/finops_slack/handler.py"
  output_path = "${path.module}/finops_slack.zip"
}

resource "aws_iam_role" "finops_slack" {
  name = "${local.finops_slack_name}-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "finops_slack_basic" {
  role       = aws_iam_role.finops_slack.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "finops_slack_permissions" {
  role = aws_iam_role.finops_slack.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "GetSlackSecret"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = "arn:aws:secretsmanager:ap-northeast-2:${data.aws_caller_identity.current.account_id}:secret:utterai-prod/finops-slack*"
      },
      {
        Sid      = "InvokeFinopsAgent"
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = aws_lambda_function.finops_agent.arn
      },
    ]
  })
}

resource "aws_lambda_function" "finops_slack" {
  function_name    = local.finops_slack_name
  role             = aws_iam_role.finops_slack.arn
  filename         = data.archive_file.finops_slack.output_path
  source_code_hash = data.archive_file.finops_slack.output_base64sha256
  handler          = "handler.handler"
  runtime          = "python3.12"
  timeout          = 10
  memory_size      = 256

  environment {
    variables = {
      FINOPS_AGENT_LAMBDA_ARN = aws_lambda_function.finops_agent.arn
      SLACK_SECRET_ID         = "utterai-prod/finops-slack"
    }
  }
}

resource "aws_lambda_function_url" "finops_slack" {
  function_name      = aws_lambda_function.finops_slack.function_name
  authorization_type = "NONE"
}

resource "aws_lambda_permission" "finops_slack_public" {
  statement_id           = "AllowPublicFunctionURL"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.finops_slack.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

# ── IRSA ─────────────────────────────────────────────────────────────────────

module "irsa" {
  source = "../../../modules/irsa"

  project_name      = var.project_name
  environment       = var.environment
  cluster_name      = var.cluster_name
  oidc_provider_arn = data.terraform_remote_state.eks.outputs.oidc_provider_arn
  oidc_provider_url = data.terraform_remote_state.eks.outputs.oidc_provider_url
  aws_account_id    = data.aws_caller_identity.current.account_id
  aws_region        = var.aws_region

  raw_audio_bucket_arn   = module.s3.raw_audio_bucket_arn
  template_bucket_arn    = module.s3.template_bucket_arn
  rag_ingest_bucket_arn  = module.s3.rag_ingest_bucket_arn
  reports_bucket_arn     = module.s3.reports_bucket_arn
  transcripts_bucket_arn = module.s3.transcripts_bucket_arn
  frontend_bucket_arn    = module.s3.frontend_bucket_arn
  kubecost_bucket_arn    = module.s3.kubecost_bucket_arn
  loki_bucket_arn        = module.s3.loki_bucket_arn
  tempo_bucket_arn       = module.s3.tempo_bucket_arn

  audio_preprocess_queue_arn = module.sqs.audio_preprocess_queue_arn
  gpu_inference_queue_arn    = module.sqs.gpu_inference_queue_arn
  report_analysis_queue_arn  = module.sqs.report_analysis_queue_arn
  audio_preprocess_dlq_arn   = module.sqs.audio_preprocess_dlq_arn
  rag_ingest_queue_arn       = module.sqs.rag_ingest_queue_arn
  rag_ingest_dlq_arn         = module.sqs.rag_ingest_dlq_arn

  private_app_subnet_ids = data.terraform_remote_state.network.outputs.private_app_subnet_ids
  node_security_group_id = data.terraform_remote_state.eks.outputs.node_security_group_id

  api_namespace = "utterai-prod-api"
}
