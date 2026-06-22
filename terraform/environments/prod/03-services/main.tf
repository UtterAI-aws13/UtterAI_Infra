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

  project_name    = var.project_name
  environment     = var.environment
  frontend_domain = var.frontend_domain
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
}
