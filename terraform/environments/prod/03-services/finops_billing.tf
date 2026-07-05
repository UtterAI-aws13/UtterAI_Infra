# ── FinOps billing data foundation ────────────────────────────────────────────
# CUR 2.0 is exported as hourly Parquet. Athena reads only the /data/ prefix;
# Data Export manifests are kept separately under /metadata/.

locals {
  finops_cur_export_name  = "utterai-${var.environment}-cur-2-0"
  finops_cur_prefix       = "finops"
  finops_cur_bucket_name  = "utterai-${var.environment}-finops-cur-${data.aws_caller_identity.current.account_id}"
  finops_query_bucket     = "utterai-${var.environment}-finops-athena-${data.aws_caller_identity.current.account_id}"
  finops_athena_database  = "utterai_${replace(var.environment, "-", "_")}_finops"
  finops_athena_table     = "cur2_spot_costs"
  finops_athena_workgroup = "utterai-${var.environment}-finops"
}

resource "aws_s3_bucket" "finops_cur" {
  bucket = local.finops_cur_bucket_name
}

resource "aws_s3_bucket_ownership_controls" "finops_cur" {
  bucket = aws_s3_bucket.finops_cur.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "finops_cur" {
  bucket = aws_s3_bucket.finops_cur.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "finops_cur" {
  bucket = aws_s3_bucket.finops_cur.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "finops_cur" {
  bucket = aws_s3_bucket.finops_cur.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "finops_cur" {
  bucket = aws_s3_bucket.finops_cur.id

  depends_on = [aws_s3_bucket_versioning.finops_cur]

  rule {
    id     = "retain-billing-data-400-days"
    status = "Enabled"

    filter {}

    expiration {
      days = 400
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

data "aws_iam_policy_document" "finops_cur_delivery" {
  statement {
    sid     = "EnableAWSDataExportsToWriteToS3"
    effect  = "Allow"
    actions = ["s3:PutObject"]

    principals {
      type        = "Service"
      identifiers = ["bcm-data-exports.amazonaws.com"]
    }

    resources = ["${aws_s3_bucket.finops_cur.arn}/*"]

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:bcm-data-exports:us-east-1:${data.aws_caller_identity.current.account_id}:export/*"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_s3_bucket_policy" "finops_cur" {
  bucket = aws_s3_bucket.finops_cur.id
  policy = data.aws_iam_policy_document.finops_cur_delivery.json

  depends_on = [
    aws_s3_bucket_ownership_controls.finops_cur,
    aws_s3_bucket_public_access_block.finops_cur,
    aws_s3_bucket_server_side_encryption_configuration.finops_cur,
  ]
}

resource "aws_s3_bucket" "finops_athena_results" {
  bucket = local.finops_query_bucket
}

resource "aws_s3_bucket_ownership_controls" "finops_athena_results" {
  bucket = aws_s3_bucket.finops_athena_results.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "finops_athena_results" {
  bucket = aws_s3_bucket.finops_athena_results.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "finops_athena_results" {
  bucket = aws_s3_bucket.finops_athena_results.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "finops_athena_results" {
  bucket = aws_s3_bucket.finops_athena_results.id

  rule {
    id     = "expire-query-results"
    status = "Enabled"

    filter {}

    expiration {
      days = 30
    }
  }
}

resource "aws_bcmdataexports_export" "finops_cur" {
  provider = aws.us_east_1

  export {
    name        = local.finops_cur_export_name
    description = "Hourly CUR 2.0 data for UtterAI Spot savings calculations"

    data_query {
      query_statement = <<-SQL
        SELECT identity_line_item_id, bill_billing_period_start_date, line_item_usage_start_date, line_item_usage_end_date, line_item_product_code, line_item_usage_type, line_item_operation, line_item_resource_id, line_item_line_item_type, line_item_unblended_cost, pricing_public_on_demand_cost, product, resource_tags FROM COST_AND_USAGE_REPORT
      SQL

      table_configurations = {
        COST_AND_USAGE_REPORT = {
          BILLING_VIEW_ARN                      = "arn:aws:billing::${data.aws_caller_identity.current.account_id}:billingview/primary"
          TIME_GRANULARITY                      = "HOURLY"
          INCLUDE_RESOURCES                     = "TRUE"
          INCLUDE_SPLIT_COST_ALLOCATION_DATA    = "FALSE"
          INCLUDE_MANUAL_DISCOUNT_COMPATIBILITY = "FALSE"
        }
      }
    }

    destination_configurations {
      s3_destination {
        s3_bucket = aws_s3_bucket.finops_cur.bucket
        s3_prefix = local.finops_cur_prefix
        s3_region = var.aws_region

        s3_output_configurations {
          compression = "PARQUET"
          format      = "PARQUET"
          output_type = "CUSTOM"
          overwrite   = "OVERWRITE_REPORT"
        }
      }
    }

    refresh_cadence {
      frequency = "SYNCHRONOUS"
    }
  }

  depends_on = [
    aws_s3_bucket_policy.finops_cur,
    aws_s3_bucket_versioning.finops_cur,
    aws_s3_bucket_lifecycle_configuration.finops_cur,
  ]
}

resource "aws_glue_catalog_database" "finops" {
  name        = local.finops_athena_database
  description = "CUR 2.0 catalog for FinOps queries"
}

resource "aws_glue_catalog_table" "finops_cur" {
  name          = local.finops_athena_table
  database_name = aws_glue_catalog_database.finops.name
  table_type    = "EXTERNAL_TABLE"

  parameters = {
    EXTERNAL                                  = "TRUE"
    "projection.enabled"                      = "true"
    "projection.billing_period.type"          = "date"
    "projection.billing_period.format"        = "yyyy-MM"
    "projection.billing_period.interval"      = "1"
    "projection.billing_period.interval.unit" = "MONTHS"
    "projection.billing_period.range"         = "2026-07,NOW"
    "storage.location.template"               = "s3://${aws_s3_bucket.finops_cur.bucket}/${local.finops_cur_prefix}/${local.finops_cur_export_name}/data/BILLING_PERIOD=$${billing_period}"
    "parquet.compression"                     = "SNAPPY"
  }

  partition_keys {
    name = "billing_period"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.finops_cur.bucket}/${local.finops_cur_prefix}/${local.finops_cur_export_name}/data/"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
    }

    columns {
      name = "identity_line_item_id"
      type = "string"
    }
    columns {
      name = "bill_billing_period_start_date"
      type = "timestamp"
    }
    columns {
      name = "line_item_usage_start_date"
      type = "timestamp"
    }
    columns {
      name = "line_item_usage_end_date"
      type = "timestamp"
    }
    columns {
      name = "line_item_product_code"
      type = "string"
    }
    columns {
      name = "line_item_usage_type"
      type = "string"
    }
    columns {
      name = "line_item_operation"
      type = "string"
    }
    columns {
      name = "line_item_resource_id"
      type = "string"
    }
    columns {
      name = "line_item_line_item_type"
      type = "string"
    }
    columns {
      name = "line_item_unblended_cost"
      type = "double"
    }
    columns {
      name = "pricing_public_on_demand_cost"
      type = "double"
    }
    columns {
      name = "product"
      type = "map<string,string>"
    }
    columns {
      name = "resource_tags"
      type = "map<string,string>"
    }
  }
}

resource "aws_athena_workgroup" "finops" {
  name        = local.finops_athena_workgroup
  description = "Bounded CUR queries for the FinOps Lambda"
  state       = "ENABLED"

  configuration {
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = true
    bytes_scanned_cutoff_per_query     = 1073741824

    result_configuration {
      output_location = "s3://${aws_s3_bucket.finops_athena_results.bucket}/results/"

      encryption_configuration {
        encryption_option = "SSE_S3"
      }
    }
  }

  depends_on = [
    aws_s3_bucket_ownership_controls.finops_athena_results,
    aws_s3_bucket_public_access_block.finops_athena_results,
    aws_s3_bucket_server_side_encryption_configuration.finops_athena_results,
    aws_s3_bucket_lifecycle_configuration.finops_athena_results,
  ]
}

resource "aws_iam_role_policy" "finops_query_billing_data" {
  role = aws_iam_role.finops_query.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "AthenaQuery"
        Effect   = "Allow"
        Action   = ["athena:StartQueryExecution", "athena:GetQueryExecution", "athena:GetQueryResults", "athena:StopQueryExecution"]
        Resource = aws_athena_workgroup.finops.arn
      },
      {
        Sid    = "GlueCatalogRead"
        Effect = "Allow"
        Action = ["glue:GetDatabase", "glue:GetTable", "glue:GetPartitions"]
        Resource = [
          "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:catalog",
          aws_glue_catalog_database.finops.arn,
          aws_glue_catalog_table.finops_cur.arn,
        ]
      },
      {
        Sid      = "ReadCURData"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = ["${aws_s3_bucket.finops_cur.arn}/${local.finops_cur_prefix}/*"]
      },
      {
        Sid      = "ListCURData"
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:GetBucketLocation"]
        Resource = [aws_s3_bucket.finops_cur.arn]
      },
      {
        Sid      = "AthenaQueryResults"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload"]
        Resource = ["${aws_s3_bucket.finops_athena_results.arn}/results/*"]
      },
      {
        Sid      = "AthenaResultsBucket"
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:GetBucketLocation"]
        Resource = [aws_s3_bucket.finops_athena_results.arn]
      },
      {
        Sid      = "PublishFinOpsMetrics"
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "cloudwatch:namespace" = "UtterAI/FinOps"
          }
        }
      },
    ]
  })
}
