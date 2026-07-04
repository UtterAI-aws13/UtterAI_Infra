# SQS data-event 전용 CloudTrail
#
# 목적: gpu-inference-queue 등에서 애플리케이션이 아닌 정체불명의 주체가
# ReceiveMessage/DeleteMessage를 호출해 메시지를 가로채는 정황이 CloudWatch
# 카운터 지표(NumberOfMessages*)로만 확인되고 호출자(IAM principal/source IP)를
# 특정할 방법이 없어서 추가한다. management event 전용 트레일은 SQS data event를
# 기록하지 않으므로 별도 트레일이 필요하다.
#
# 비용 절감을 위해 이 계정에 이미 관리형 트레일이 있을 가능성을 고려하지 않고
# 단일 리전·타깃 큐 한정 data event만 기록한다 (전체 SQS data event는 비용이 큼).

locals {
  prefix      = "${var.project_name}-${var.environment}"
  bucket_name = "${local.prefix}-cloudtrail-logs"
}

resource "aws_s3_bucket" "trail" {
  count  = length(var.sqs_data_event_queue_arns) > 0 ? 1 : 0
  bucket = local.bucket_name

  tags = {
    Name = local.bucket_name
  }
}

resource "aws_s3_bucket_public_access_block" "trail" {
  count  = length(var.sqs_data_event_queue_arns) > 0 ? 1 : 0
  bucket = aws_s3_bucket.trail[0].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "trail" {
  count  = length(var.sqs_data_event_queue_arns) > 0 ? 1 : 0
  bucket = aws_s3_bucket.trail[0].id

  rule {
    id     = "expire-logs"
    status = "Enabled"

    filter {}

    expiration {
      days = var.log_retention_days
    }
  }
}

data "aws_iam_policy_document" "trail_bucket" {
  count = length(var.sqs_data_event_queue_arns) > 0 ? 1 : 0

  statement {
    sid    = "AWSCloudTrailAclCheck"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    actions   = ["s3:GetBucketAcl"]
    resources = [aws_s3_bucket.trail[0].arn]
  }

  statement {
    sid    = "AWSCloudTrailWrite"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.trail[0].arn}/AWSLogs/${var.aws_account_id}/*"]
    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }
  }
}

resource "aws_s3_bucket_policy" "trail" {
  count  = length(var.sqs_data_event_queue_arns) > 0 ? 1 : 0
  bucket = aws_s3_bucket.trail[0].id
  policy = data.aws_iam_policy_document.trail_bucket[0].json
}

resource "aws_cloudtrail" "sqs_forensics" {
  count = length(var.sqs_data_event_queue_arns) > 0 ? 1 : 0

  name                          = "${local.prefix}-sqs-forensics-trail"
  s3_bucket_name                = aws_s3_bucket.trail[0].id
  is_multi_region_trail         = false
  include_global_service_events = false
  enable_logging                = true

  # SQS data event는 classic event_selector가 지원하지 않고
  # advanced_event_selector로만 설정 가능하다.
  advanced_event_selector {
    name = "SQS data events for forensics targets"

    field_selector {
      field  = "eventCategory"
      equals = ["Data"]
    }

    field_selector {
      field  = "resources.type"
      equals = ["AWS::SQS::Queue"]
    }

    field_selector {
      field  = "resources.ARN"
      equals = var.sqs_data_event_queue_arns
    }
  }

  depends_on = [aws_s3_bucket_policy.trail]

  tags = {
    Name = "${local.prefix}-sqs-forensics-trail"
  }
}
