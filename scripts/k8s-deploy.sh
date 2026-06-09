#!/bin/bash
# k8s 매니페스트 배포 스크립트
# AWS 로그인 컨텍스트에서 계정 ID와 ECR 최신 이미지 태그를 자동 주입

set -euo pipefail

export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export AWS_REGION=${AWS_REGION:-ap-northeast-2}

echo "Account: $AWS_ACCOUNT_ID / Region: $AWS_REGION"

# ECR에서 가장 최근 push된 이미지 태그 조회
latest_tag() {
  local repo=$1
  aws ecr describe-images \
    --repository-name "$repo" \
    --region "$AWS_REGION" \
    --query 'sort_by(imageDetails, &imagePushedAt)[-1].imageTags[0]' \
    --output text
}

export BACKEND_TAG=$(latest_tag "utterai-backend")
export AI_CPU_TAG=$(latest_tag "utterai-ai-cpu")
export AI_GPU_TAG=$(latest_tag "utterai-ai-gpu")

# Terraform output에서 RDS/Redis 엔드포인트 조회
tf_output() {
  terraform -chdir=terraform/environments/dev output -raw "$1"
}

export RDS_ENDPOINT=$(tf_output "rds_endpoint")
export REDIS_ENDPOINT=$(tf_output "redis_endpoint")

echo "utterai-backend  : $BACKEND_TAG"
echo "utterai-ai-cpu   : $AI_CPU_TAG"
echo "utterai-ai-gpu   : $AI_GPU_TAG"
echo "RDS endpoint     : $RDS_ENDPOINT"
echo "Redis endpoint   : $REDIS_ENDPOINT"

apply() {
  envsubst < "$1" | kubectl apply -f -
}

# 1. 네임스페이스
kubectl apply -f k8s/namespaces/

# 2. RBAC (serviceaccounts.yaml에 ${AWS_ACCOUNT_ID} 플레이스홀더가 있으므로 envsubst 필요)
for f in k8s/rbac/*.yaml; do
  apply "$f"
done

# 3. External Secrets
kubectl apply -f k8s/secrets/cluster-secret-store.yaml
for f in k8s/secrets/*.yaml; do
  kubectl apply -f "$f"
done

# 4. 워크로드 (AWS_ACCOUNT_ID + 각 이미지 태그 치환)
for f in k8s/workloads/*.yaml; do
  apply "$f"
done

# 5. Ingress
kubectl apply -f k8s/ingress/

echo "배포 완료"
