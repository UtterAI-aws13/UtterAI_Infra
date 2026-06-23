output "vpc_id" {
  value = module.vpc.vpc_id
}

output "public_subnet_ids" {
  value = module.vpc.public_subnet_ids
}

output "private_app_subnet_ids" {
  value = module.vpc.private_app_subnet_ids
}

output "private_data_subnet_ids" {
  value = module.vpc.private_data_subnet_ids
}

output "nat_gateway_id" {
  value = module.vpc.nat_gateway_id
}

output "pod_subnet_ids" {
  value = module.vpc.pod_subnet_ids
}

output "pod_subnet_az_map" {
  value = module.vpc.pod_subnet_az_map
}

output "client_vpn_endpoint_id" {
  value       = aws_ec2_client_vpn_endpoint.this.id
  description = "Client VPN endpoint ID — .ovpn 파일 생성 시 사용"
}
