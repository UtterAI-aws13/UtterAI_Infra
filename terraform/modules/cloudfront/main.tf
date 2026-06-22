locals {
  prefix = "${var.project_name}-${var.environment}"
}

# www → app 리다이렉트 (viewer-request)
resource "aws_cloudfront_function" "www_redirect" {
  name    = "${local.prefix}-www-redirect"
  runtime = "cloudfront-js-2.0"
  publish = true
  code    = <<-EOF
    function handler(event) {
      var request = event.request;
      var host    = request.headers.host ? request.headers.host.value : '';

      if (host === 'www.utterai.org') {
        return {
          statusCode: 301,
          statusDescription: 'Moved Permanently',
          headers: {
            location: { value: 'https://app.utterai.org' + request.uri }
          }
        };
      }

      return request;
    }
  EOF
}

# CloudFront → ALB 구간은 HTTP only이므로 FastAPI의 307 redirect가 http:// 절대 URL로 생성됨.
# viewer response에서 절대 URL을 상대 경로로 바꿔 브라우저가 CloudFront HTTPS URL로 따라가도록 함.
resource "aws_cloudfront_function" "redirect_rewrite" {
  name    = "${local.prefix}-redirect-rewrite"
  runtime = "cloudfront-js-2.0"
  publish = true
  code    = <<-EOF
    function handler(event) {
      var response = event.response;
      var headers  = response.headers;

      if (headers.location) {
        var loc = headers.location.value;
        if (loc.indexOf('http://') === 0 || loc.indexOf('https://') === 0) {
          var afterProto = loc.indexOf('//') + 2;
          var pathStart  = loc.indexOf('/', afterProto);
          headers.location = { value: pathStart >= 0 ? loc.slice(pathStart) : '/' };
        }
      }

      return response;
    }
  EOF
}

resource "aws_cloudfront_origin_access_control" "frontend" {
  name                              = "${local.prefix}-frontend-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "frontend" {
  enabled             = true
  default_root_object = "index.html"
  price_class         = "PriceClass_200"
  aliases             = var.aliases

  origin {
    domain_name              = "${var.frontend_bucket_id}.s3.ap-northeast-2.amazonaws.com"
    origin_id                = "S3-${var.frontend_bucket_id}"
    origin_access_control_id = aws_cloudfront_origin_access_control.frontend.id
    origin_path              = "/current"
  }

  # ALB API origin — alb_dns_name이 설정된 경우에만 생성
  dynamic "origin" {
    for_each = var.alb_dns_name != "" ? [var.alb_dns_name] : []
    content {
      domain_name = origin.value
      origin_id   = "ALB-API"
      custom_origin_config {
        http_port              = 80
        https_port             = 443
        origin_protocol_policy = "http-only"
        origin_ssl_protocols   = ["TLSv1.2"]
      }
    }
  }

  default_cache_behavior {
    target_origin_id       = "S3-${var.frontend_bucket_id}"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true

    forwarded_values {
      query_string = false
      cookies {
        forward = "none"
      }
    }

    min_ttl     = 0
    default_ttl = 3600
    max_ttl     = 86400

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.www_redirect.arn
    }
  }

  # /api/* 요청을 ALB로 프록시 — alb_dns_name이 설정된 경우에만 생성
  dynamic "ordered_cache_behavior" {
    for_each = var.alb_dns_name != "" ? [var.alb_dns_name] : []
    content {
      path_pattern           = "/api/*"
      target_origin_id       = "ALB-API"
      viewer_protocol_policy = "redirect-to-https"
      allowed_methods        = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
      cached_methods         = ["GET", "HEAD"]
      compress               = false

      # API 응답은 캐시하지 않음
      min_ttl     = 0
      default_ttl = 0
      max_ttl     = 0

      forwarded_values {
        # Authorization, Content-Type 등 API 헤더 전달
        headers      = ["Authorization", "Content-Type", "Accept", "Origin", "X-Requested-With"]
        query_string = true
        cookies {
          forward = "all"
        }
      }

      function_association {
        event_type   = "viewer-response"
        function_arn = aws_cloudfront_function.redirect_rewrite.arn
      }
    }
  }

  # SPA 라우팅 — 404를 index.html로 처리
  custom_error_response {
    error_code            = 403
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 0
  }

  custom_error_response {
    error_code            = 404
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 0
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = var.acm_certificate_arn == ""
    acm_certificate_arn            = var.acm_certificate_arn != "" ? var.acm_certificate_arn : null
    ssl_support_method             = var.acm_certificate_arn != "" ? "sni-only" : null
    minimum_protocol_version       = var.acm_certificate_arn != "" ? "TLSv1.2_2021" : null
  }

  tags = {
    Name = "${local.prefix}-frontend-cdn"
  }
}

resource "aws_s3_bucket_policy" "frontend_cloudfront" {
  bucket = var.frontend_bucket_id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "AllowCloudFrontOAC"
      Effect = "Allow"
      Principal = {
        Service = "cloudfront.amazonaws.com"
      }
      Action   = "s3:GetObject"
      Resource = "${var.frontend_bucket_arn}/*"
      Condition = {
        StringEquals = {
          "AWS:SourceArn" = aws_cloudfront_distribution.frontend.arn
        }
      }
    }]
  })
}
