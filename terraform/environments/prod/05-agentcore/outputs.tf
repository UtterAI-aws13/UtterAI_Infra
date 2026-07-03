output "gateway_id" {
  description = "AgentCore Gateway 식별자 (Runtime 코드에서 tool 호출 시 참조)"
  value       = aws_bedrockagentcore_gateway.report_evidence.gateway_id
}

output "gateway_arn" {
  description = "AgentCore Gateway ARN"
  value       = aws_bedrockagentcore_gateway.report_evidence.gateway_arn
}

output "gateway_url" {
  description = "AgentCore Gateway MCP 엔드포인트 URL"
  value       = aws_bedrockagentcore_gateway.report_evidence.gateway_url
}
