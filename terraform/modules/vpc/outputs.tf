output "vpc_id" {
  value = aws_vpc.this.id
}

output "public_subnet_ids" {
  value = aws_subnet.public[*].id
}

output "private_app_subnet_ids" {
  value = aws_subnet.private_app[*].id
}

output "private_data_subnet_ids" {
  value = aws_subnet.private_data[*].id
}

output "nat_gateway_id" {
  value = aws_nat_gateway.this.id
}

output "pod_subnet_ids" {
  value = aws_subnet.pod[*].id
}

# AZ → subnet ID 매핑 (ENIConfig 생성에 사용)
output "pod_subnet_az_map" {
  value = { for i, az in var.azs : az => aws_subnet.pod[i].id if i < length(var.pod_subnet_cidrs) }
}
