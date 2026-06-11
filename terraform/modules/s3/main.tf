locals {
  prefix = "${var.project_name}-${var.environment}"

  buckets = {
    frontend  = "${local.prefix}-frontend"
    raw_audio = "${local.prefix}-raw-audio"
    documents = "${local.prefix}-documents"
    reports   = "${local.prefix}-reports"
  }
}

resource "aws_s3_bucket" "buckets" {
  for_each = local.buckets

  bucket = each.value

  tags = {
    Name = each.value
  }
}

resource "aws_s3_bucket_public_access_block" "buckets" {
  for_each = local.buckets

  bucket = aws_s3_bucket.buckets[each.key].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "buckets" {
  for_each = local.buckets

  bucket = aws_s3_bucket.buckets[each.key].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "raw_audio" {
  bucket = aws_s3_bucket.buckets["raw_audio"].id

  rule {
    id     = "expire-raw-audio"
    status = "Enabled"

    filter {}

    expiration {
      days = 30
    }
  }
}

resource "aws_s3_bucket_cors_configuration" "raw_audio" {
  bucket = aws_s3_bucket.buckets["raw_audio"].id

  cors_rule {
    allowed_headers = ["*"]
    allowed_methods = ["GET", "PUT", "POST"]
    allowed_origins = concat(["https://${var.frontend_domain}"], var.allowed_extra_origins)
    expose_headers  = ["ETag"]
    max_age_seconds = 3000
  }
}

resource "aws_s3_bucket_cors_configuration" "documents" {
  bucket = aws_s3_bucket.buckets["documents"].id

  cors_rule {
    allowed_headers = ["*"]
    allowed_methods = ["GET", "PUT", "POST"]
    allowed_origins = concat(["https://${var.frontend_domain}"], var.allowed_extra_origins)
    expose_headers  = ["ETag"]
    max_age_seconds = 3000
  }
}
