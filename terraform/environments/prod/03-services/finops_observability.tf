# ── FinOps observability ──────────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "finops_query" {
  name              = "/aws/lambda/${local.finops_query_name}"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "finops_agent" {
  name              = "/aws/lambda/${local.finops_agent_name}"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "finops_slack" {
  name              = "/aws/lambda/${local.finops_slack_name}"
  retention_in_days = 30
}

resource "aws_cloudwatch_metric_alarm" "finops_lambda_errors" {
  for_each = toset([
    local.finops_query_name,
    local.finops_agent_name,
    local.finops_slack_name,
  ])

  alarm_name          = "${each.value}-errors"
  alarm_description   = "At least one FinOps Lambda error occurred within five minutes"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = each.value
  }
}

resource "aws_cloudwatch_metric_alarm" "finops_athena_bytes_scanned" {
  alarm_name          = "utterai-${var.environment}-finops-athena-high-scan"
  alarm_description   = "FinOps Athena queries scanned more than 500 MiB in five minutes"
  namespace           = "AWS/Athena"
  metric_name         = "DataScannedInBytes"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 524288000
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    WorkGroup = aws_athena_workgroup.finops.name
  }
}

resource "aws_cloudwatch_metric_alarm" "finops_spot_query_errors" {
  alarm_name          = "utterai-${var.environment}-finops-spot-query-errors"
  alarm_description   = "Athena-backed Spot savings query failed"
  namespace           = "UtterAI/FinOps"
  metric_name         = "SpotSavingsQueryErrors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
}
