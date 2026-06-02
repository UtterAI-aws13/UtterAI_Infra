# UtterAI Dev 환경 — 구현 현황 및 적용 가이드

> AWS ap-northeast-2 | EKS 1.31 | Terraform + Kubernetes

---

## 목차

1. [환경 개요](#1-환경-개요)
2. [구현 현황](#2-구현-현황)
3. [디렉토리 구조](#3-디렉토리-구조)
4. [사전 준비 — 전체 목록](#4-사전-준비--전체-목록)
5. [적용 순서](#5-적용-순서)
6. [플레이스홀더 치환 목록](#6-플레이스홀더-치환-목록)
7. [검증 명령어](#7-검증-명령어)
8. [남은 작업](#8-남은-작업)

---

## 1. 환경 개요

| 항목 | 값 |
|---|---|
| 환경 | `dev` |
| AWS Region | `ap-northeast-2` |
| VPC CIDR | `10.10.0.0/16` |
| AZ | `ap-northeast-2a`, `ap-northeast-2c` (2개) |
| EKS 클러스터 이름 | `utterai-dev-eks` |
| 리소스 Prefix | `utterai-dev-` |
| 도메인 | `dev.utterai.com` / `api.dev.utterai.com` |
| Terraform State | S3 `utterai-dev-terraform-state` |

### 아키텍처 요약

```text
User → Route 53 → CloudFront → ALB Ingress → EKS
                                               ├── API Pod         (api NodePool, MNG + HPA)
                                               ├── CPU AI Worker   (ai-cpu NodePool, KEDA)
                                               ├── GPU AI Worker   (ai-gpu NodePool, KEDA)
                                               └── Batch Worker    (spot-batch NodePool, KEDA)
```

---

## 2. 구현 현황

### 2.1 Terraform 모듈 — 완료

| 모듈 | 경로 | 주요 리소스 |
|---|---|---|
| `vpc` | `terraform/modules/vpc/` | VPC, Public/Private-App/Private-Data 서브넷(2AZ), NAT GW 1개, VPC Endpoint 5개 |
| `eks` | `terraform/modules/eks/` | EKS 클러스터, system/api Managed NodeGroup, OIDC Provider, Cluster Addons |
| `eks-addons` | `terraform/modules/eks-addons/` | LBC, Karpenter, KEDA, metrics-server, nvidia-device-plugin (Helm) |
| `irsa` | `terraform/modules/irsa/` | IAM Role 7개 + Karpenter Interruption Queue(SQS) |
| `aurora` | `terraform/modules/aurora/` | Aurora PostgreSQL 16, Single-AZ, Writer 1개, 비밀번호 Secrets Manager 자동 관리 |
| `redis` | `terraform/modules/redis/` | ElastiCache Redis 7, 단일 노드, TLS 활성화 |
| `s3` | `terraform/modules/s3/` | 버킷 6개 (frontend, raw-audio, processed-audio, documents, reports, artifacts) |
| `sqs` | `terraform/modules/sqs/` | analysis-queue + DLQ |
| `cognito` | `terraform/modules/cognito/` | User Pool + App Client |

> **VPC는 별도 사전 작업 없음.** `terraform apply` 한 번으로 VPC부터 EKS까지 모두 생성된다.

### 2.2 Kubernetes Manifest — 완료

| 디렉토리 | 파일 | 내용 |
|---|---|---|
| `k8s/namespaces/` | `namespaces.yaml` | utter-api, utter-ai-cpu, utter-ai-gpu, utter-batch, utter-observability |
| `k8s/nodepools/` | `ec2nodeclass-default.yaml` | general/api/ai-cpu/batch 공용 EC2NodeClass |
| `k8s/nodepools/` | `ec2nodeclass-gpu.yaml` | ai-gpu 전용 EC2NodeClass (100GB 볼륨) |
| `k8s/nodepools/` | `nodepool-general.yaml` | On-Demand+Spot, m6i/c6i |
| `k8s/nodepools/` | `nodepool-api.yaml` | On-Demand, taint: dedicated=api |
| `k8s/nodepools/` | `nodepool-ai-cpu.yaml` | On-Demand, m/c/r 계열 |
| `k8s/nodepools/` | `nodepool-ai-gpu.yaml` | Spot 허용(dev), g4dn.xlarge |
| `k8s/nodepools/` | `nodepool-spot-batch.yaml` | Spot only |
| `k8s/rbac/` | `serviceaccounts.yaml` | SA 4개 + IRSA role-arn annotation |
| `k8s/rbac/` | `rolebindings.yaml` | 각 SA에 K8s 내부 최소 권한 |
| `k8s/ingress/` | `api-ingress.yaml` | ALB internet-facing, HTTPS 리다이렉트 |
| `k8s/workloads/` | `api-deployment.yaml` | Deployment + Service + HPA + ConfigMap |
| `k8s/workloads/` | `cpu-worker-deployment.yaml` | replicas: 0, KEDA 제어 |
| `k8s/workloads/` | `gpu-worker-deployment.yaml` | nvidia.com/gpu: 1, replicas: 0 |
| `k8s/workloads/` | `batch-worker-deployment.yaml` | Spot 노드 대상, replicas: 0 |
| `k8s/workloads/` | `keda-scaledobject-cpu.yaml` | queueLength: 5, min: 0, max: 2 |
| `k8s/workloads/` | `keda-scaledobject-gpu.yaml` | queueLength: 1, min: 0, max: 1 |
| `k8s/workloads/` | `keda-scaledobject-batch.yaml` | queueLength: 10, min: 0, max: 5 |

---

## 3. 디렉토리 구조

```
UtterAI_Infra/
├── terraform/
│   ├── environments/
│   │   └── dev/
│   │       ├── main.tf           ← 모듈 조합, backend, provider
│   │       ├── variables.tf
│   │       ├── outputs.tf
│   │       └── terraform.tfvars  ← dev 실제 값 (CIDR, 인스턴스 타입 등)
│   └── modules/
│       ├── vpc/
│       ├── eks/
│       ├── eks-addons/
│       ├── irsa/
│       │   └── policies/lbc-policy.json
│       ├── aurora/
│       ├── redis/
│       ├── s3/
│       ├── sqs/
│       └── cognito/
│
└── k8s/
    ├── namespaces/
    ├── nodepools/     ← Karpenter EC2NodeClass + NodePool
    ├── rbac/          ← ServiceAccount + RoleBinding
    ├── ingress/       ← ALB Ingress
    └── workloads/     ← Deployment + KEDA ScaledObject
```

---

## 4. 사전 준비 — 전체 목록

적용 순서에 들어가기 전에 아래 항목을 모두 완료해야 한다.

### 4.1 필수 도구 설치

```bash
# 버전 확인
terraform --version   # >= 1.6
kubectl version --client
helm version          # >= 3
aws --version         # v2
envsubst --version    # apt: gettext / brew: gettext
```

### 4.2 Terraform Backend 수동 생성 (terraform init 전)

Terraform은 자신의 state를 저장할 S3 버킷이 이미 존재해야 init을 할 수 있다.

```bash
# S3 버킷 생성
aws s3api create-bucket \
  --bucket utterai-dev-terraform-state \
  --region ap-northeast-2 \
  --create-bucket-configuration LocationConstraint=ap-northeast-2

aws s3api put-bucket-versioning \
  --bucket utterai-dev-terraform-state \
  --versioning-configuration Status=Enabled

aws s3api put-bucket-encryption \
  --bucket utterai-dev-terraform-state \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

# DynamoDB lock 테이블
aws dynamodb create-table \
  --table-name utterai-dev-terraform-lock \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region ap-northeast-2
```

### 4.3 ECR 리포지토리 생성

Docker 이미지를 올릴 리포지토리를 미리 만든다. 이미지 이름은 팀과 협의 후 확정.

```bash
# 현재 임시 이름. 팀 협의 후 수정 가능
for repo in utterai-backend utterai-ai-cpu utterai-ai-gpu utterai-batch; do
  aws ecr create-repository \
    --repository-name $repo \
    --region ap-northeast-2 \
    --image-scanning-configuration scanOnPush=true
done

# 생성 확인
aws ecr describe-repositories --region ap-northeast-2 --query 'repositories[].repositoryName'
```

### 4.4 ACM 인증서 발급 (ALB HTTPS용)

ALB에 붙일 인증서를 발급한다. DNS 검증 완료까지 수 분이 걸리므로 Terraform apply 전에 먼저 진행한다.

```bash
aws acm request-certificate \
  --domain-name "*.dev.utterai.com" \
  --validation-method DNS \
  --region ap-northeast-2
```

Route 53에 DNS 검증 레코드를 추가하고 상태가 `Issued`가 되면 ARN을 기록해 둔다.

```bash
# ARN 확인
aws acm list-certificates --region ap-northeast-2 \
  --query 'CertificateSummaryList[?DomainName==`*.dev.utterai.com`].CertificateArn' \
  --output text
```

### 4.5 Docker 이미지 빌드 및 ECR 푸시

**Workload 배포(STEP 9) 전까지 완료**되어야 한다. 이미지가 없으면 Pod가 `ImagePullBackOff` 상태가 된다.

```bash
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export ECR_REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.ap-northeast-2.amazonaws.com"
export GIT_SHA=$(git rev-parse --short HEAD)

# ECR 로그인
aws ecr get-login-password --region ap-northeast-2 \
  | docker login --username AWS --password-stdin ${ECR_REGISTRY}

# 백엔드 API 이미지 (백엔드팀 담당)
docker build -t ${ECR_REGISTRY}/utterai-backend:${GIT_SHA} <백엔드 repo 경로>
docker push ${ECR_REGISTRY}/utterai-backend:${GIT_SHA}
docker tag  ${ECR_REGISTRY}/utterai-backend:${GIT_SHA} \
            ${ECR_REGISTRY}/utterai-backend:latest
docker push ${ECR_REGISTRY}/utterai-backend:latest

# AI CPU Worker 이미지 (AI팀 담당)
docker build -t ${ECR_REGISTRY}/utterai-ai-cpu:${GIT_SHA} <AI CPU repo 경로>
docker push ${ECR_REGISTRY}/utterai-ai-cpu:${GIT_SHA}
docker push ${ECR_REGISTRY}/utterai-ai-cpu:latest

# AI GPU Worker 이미지 (AI팀 담당)
# GPU 이미지는 크기가 클 수 있음 (수 GB). 빌드 + 푸시 시간 여유있게 확보
docker build -t ${ECR_REGISTRY}/utterai-ai-gpu:${GIT_SHA} <AI GPU repo 경로>
docker push ${ECR_REGISTRY}/utterai-ai-gpu:${GIT_SHA}
docker push ${ECR_REGISTRY}/utterai-ai-gpu:latest

# Batch Worker 이미지
docker build -t ${ECR_REGISTRY}/utterai-batch:${GIT_SHA} <Batch repo 경로>
docker push ${ECR_REGISTRY}/utterai-batch:${GIT_SHA}
docker push ${ECR_REGISTRY}/utterai-batch:latest
```

> **GPU 이미지 주의**: CUDA, PyTorch, 화자 분리 모델 포함 시 이미지가 수 GB에 달할 수 있다.  
> 모델 파일을 이미지에 포함할지 S3에서 런타임에 내려받을지 AI팀과 먼저 협의한다.

### 4.6 Secrets Manager 수동 시크릿 생성

`terraform apply` 후에 생성한다. Aurora DB 비밀번호는 Terraform이 자동 생성하지만, 나머지 두 개는 수동으로 만들어야 한다.

```bash
# Redis AUTH 토큰 (임의의 강력한 문자열 사용)
aws secretsmanager create-secret \
  --name "utterai-dev/redis-auth-token" \
  --secret-string '{"REDIS_AUTH_TOKEN":"<강력한_랜덤_문자열>"}' \
  --region ap-northeast-2

# 백엔드-AI 서버 간 내부 통신 토큰
aws secretsmanager create-secret \
  --name "utterai-dev/internal-service-token" \
  --secret-string '{"INTERNAL_SERVICE_TOKEN":"<강력한_랜덤_문자열>"}' \
  --region ap-northeast-2
```

> Aurora DB 비밀번호(`utterai-dev/db-password`)는 Terraform aurora 모듈의  
> `manage_master_user_password = true` 설정으로 **자동 생성**된다.

---

## 5. 적용 순서

### STEP 1 — Terraform apply (VPC · EKS · 전체 AWS 리소스)

VPC, 서브넷, EKS 클러스터, Aurora, Redis, S3, SQS, Cognito, IAM Role 등  
**모든 AWS 리소스가 이 한 번의 apply로 생성**된다.

```bash
cd terraform/environments/dev

terraform init
terraform plan   # 변경 내용 꼭 확인
terraform apply  # 완료까지 약 20~30분 소요
```

완료 후 output을 환경변수로 저장한다.

```bash
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export AURORA_WRITER_ENDPOINT=$(terraform output -raw aurora_writer_endpoint)
export AURORA_DB_SECRET_ARN=$(terraform output -raw aurora_db_secret_arn)
export REDIS_ENDPOINT=$(terraform output -raw redis_endpoint)
export ACM_CERTIFICATE_ARN="<4.4에서 발급한 ARN>"
```

### STEP 2 — Secrets Manager 시크릿 생성

`terraform apply` 후 Aurora가 준비되면 수동 시크릿을 생성한다. (사전 준비 4.6 참고)

```bash
# 생성 확인
aws secretsmanager list-secrets --region ap-northeast-2 \
  --query 'SecretList[?starts_with(Name, `utterai-dev`)].Name'
```

### STEP 3 — kubeconfig 업데이트

```bash
aws eks update-kubeconfig \
  --name utterai-dev-eks \
  --region ap-northeast-2

kubectl get nodes  # system 노드 2개 이상 Ready 확인
kubectl get pods -n kube-system  # CoreDNS, kube-proxy 정상 확인
```

### STEP 4 — Namespace

```bash
kubectl apply -f k8s/namespaces/namespaces.yaml
kubectl get ns | grep utter
```

### STEP 5 — RBAC (ServiceAccount + RoleBinding)

```bash
# ${AWS_ACCOUNT_ID} 치환 후 적용
envsubst < k8s/rbac/serviceaccounts.yaml | kubectl apply -f -
kubectl apply -f k8s/rbac/rolebindings.yaml

# 확인
kubectl get sa -A | grep -E "utter|ai-|batch"
```

### STEP 6 — External Secrets Operator 설치

Workload의 Deployment가 Secrets Manager에서 값을 가져오는 K8s Secret을 참조한다.  
**이 단계가 없으면 STEP 9에서 Pod가 시작되지 않는다.**

```bash
# Helm으로 설치
helm repo add external-secrets https://charts.external-secrets.io
helm repo update

helm install external-secrets external-secrets/external-secrets \
  --namespace external-secrets \
  --create-namespace \
  --set installCRDs=true

# 설치 확인
kubectl get pods -n external-secrets
```

설치 후 ClusterSecretStore와 ExternalSecret을 생성한다.

```bash
# ClusterSecretStore — Secrets Manager 연결 설정
# IRSA를 사용하므로 별도 Access Key 불필요
cat <<EOF | kubectl apply -f -
apiVersion: external-secrets.io/v1beta1
kind: ClusterSecretStore
metadata:
  name: aws-secrets-store
spec:
  provider:
    aws:
      service: SecretsManager
      region: ap-northeast-2
      auth:
        jwt:
          serviceAccountRef:
            name: external-secrets
            namespace: external-secrets
EOF

# ExternalSecret — utter-api 네임스페이스용
#
# DB 비밀번호는 Aurora가 manage_master_user_password=true로 자동 생성한 시크릿을 사용.
# 이름이 "rds!cluster-xxx" 형태라 예측 불가 → terraform output aurora_db_secret_arn 사용.
# 시크릿 구조: {"username":"...", "password":"...", "host":"...", "port":5432, ...}
cat <<EOF | kubectl apply -f -
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: backend-api-secret
  namespace: utter-api
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: aws-secrets-store
    kind: ClusterSecretStore
  target:
    name: backend-api-secret
  data:
    - secretKey: DB_PASSWORD
      remoteRef:
        key: "${AURORA_DB_SECRET_ARN}"
        property: password
    - secretKey: INTERNAL_SERVICE_TOKEN
      remoteRef:
        key: utterai-dev/internal-service-token
        property: INTERNAL_SERVICE_TOKEN
    - secretKey: REDIS_AUTH_TOKEN
      remoteRef:
        key: utterai-dev/redis-auth-token
        property: REDIS_AUTH_TOKEN
EOF

# Secret 생성 확인
kubectl get secret backend-api-secret -n utter-api
```

### STEP 7 — Karpenter NodePool

Terraform이 생성한 `utterai-dev-karpenter-node-role`과 서브넷 태그가 있어야 한다.

```bash
# EC2NodeClass 먼저 (NodePool이 참조함)
kubectl apply -f k8s/nodepools/ec2nodeclass-default.yaml
kubectl apply -f k8s/nodepools/ec2nodeclass-gpu.yaml

# NodePool 5개
kubectl apply -f k8s/nodepools/nodepool-general.yaml
kubectl apply -f k8s/nodepools/nodepool-api.yaml
kubectl apply -f k8s/nodepools/nodepool-ai-cpu.yaml
kubectl apply -f k8s/nodepools/nodepool-ai-gpu.yaml
kubectl apply -f k8s/nodepools/nodepool-spot-batch.yaml

# 확인
kubectl get nodepools
kubectl get ec2nodeclasses
```

### STEP 8 — Ingress

ALB가 생성되려면 AWS Load Balancer Controller가 karpenter namespace에서 정상 실행 중이어야 한다.

```bash
# LBC 정상 확인
kubectl get pods -n ingress-system

# Ingress 적용
envsubst < k8s/ingress/api-ingress.yaml | kubectl apply -f -

# ALB 생성 확인 (1~3분 소요)
kubectl get ingress -n utter-api
# ADDRESS 컬럼에 ALB DNS가 채워지면 완료

# ALB DNS 저장
export ALB_DNS=$(kubectl get ingress utter-api-ingress -n utter-api \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')
echo "ALB DNS: ${ALB_DNS}"
```

### STEP 9 — Route 53 레코드 생성

ALB DNS가 확인되면 즉시 등록한다. 이 단계가 없으면 `api.dev.utterai.com`으로 접근 불가.

```bash
# Hosted Zone ID 확인
export HOSTED_ZONE_ID=$(aws route53 list-hosted-zones \
  --query "HostedZones[?Name=='dev.utterai.com.'].Id" \
  --output text | sed 's|/hostedzone/||')

# A 레코드 (Alias) 생성
# ALB의 Hosted Zone ID는 ap-northeast-2 기준 Z35SXDOTRQ7X7K
aws route53 change-resource-record-sets \
  --hosted-zone-id ${HOSTED_ZONE_ID} \
  --change-batch "{
    \"Changes\": [{
      \"Action\": \"UPSERT\",
      \"ResourceRecordSet\": {
        \"Name\": \"api.dev.utterai.com\",
        \"Type\": \"A\",
        \"AliasTarget\": {
          \"HostedZoneId\": \"Z35SXDOTRQ7X7K\",
          \"DNSName\": \"${ALB_DNS}\",
          \"EvaluateTargetHealth\": true
        }
      }
    }]
  }"
```

### STEP 10 — Workload 배포

**4.5 이미지 빌드 완료 후** 진행한다.

```bash
# API Deployment + Service + HPA + ConfigMap
envsubst < k8s/workloads/api-deployment.yaml | kubectl apply -f -

# Worker Deployment (replicas: 0 → KEDA가 제어)
envsubst < k8s/workloads/cpu-worker-deployment.yaml | kubectl apply -f -
envsubst < k8s/workloads/gpu-worker-deployment.yaml | kubectl apply -f -
envsubst < k8s/workloads/batch-worker-deployment.yaml | kubectl apply -f -

# API Pod 기동 확인
kubectl get pods -n utter-api -w
# STATUS가 Running이 되면 Ctrl+C
```

### STEP 11 — KEDA ScaledObject

```bash
envsubst < k8s/workloads/keda-scaledobject-cpu.yaml | kubectl apply -f -
envsubst < k8s/workloads/keda-scaledobject-gpu.yaml | kubectl apply -f -
envsubst < k8s/workloads/keda-scaledobject-batch.yaml | kubectl apply -f -

kubectl get scaledobject -A
# READY가 True이면 정상
```

---

## 6. 플레이스홀더 치환 목록

`envsubst`로 치환 후 적용한다. `export` 로 환경변수를 먼저 설정해야 한다.

| 변수 | 사용 위치 | 값 출처 |
|---|---|---|
| `${AWS_ACCOUNT_ID}` | serviceaccounts.yaml, 모든 Deployment, KEDA | `aws sts get-caller-identity --query Account --output text` |
| `${ACM_CERTIFICATE_ARN}` | api-ingress.yaml | `aws acm list-certificates` |
| `${AURORA_WRITER_ENDPOINT}` | api-deployment.yaml ConfigMap | `terraform output -raw aurora_writer_endpoint` |
| `${AURORA_DB_SECRET_ARN}` | ExternalSecret (STEP 6) | `terraform output -raw aurora_db_secret_arn` |
| `${REDIS_ENDPOINT}` | api-deployment.yaml ConfigMap | `terraform output -raw redis_endpoint` |

---

## 7. 검증 명령어

### EKS 기본

```bash
# 노드 상태 및 label 확인
kubectl get nodes -L workload,karpenter.sh/capacity-type,node.kubernetes.io/instance-type

# 시스템 컴포넌트 전체 확인
kubectl get pods -n kube-system
kubectl get pods -n karpenter
kubectl get pods -n keda
kubectl get pods -n ingress-system
kubectl get pods -n external-secrets
```

### NodePool 배치 테스트

```bash
# api 노드에 nginx Pod가 정상 배치되는지 확인
kubectl run test-api --image=nginx \
  --overrides='{
    "spec":{
      "nodeSelector":{"workload":"api"},
      "tolerations":[{"key":"dedicated","operator":"Equal","value":"api","effect":"NoSchedule"}]
    }
  }' \
  --namespace utter-api

kubectl get pod test-api -n utter-api -o wide  # NODE 컬럼이 api 노드여야 함
kubectl delete pod test-api -n utter-api
```

### API 엔드포인트

```bash
# Pod 상태
kubectl get pods -n utter-api

# 헬스체크 (Route 53 등록 후)
curl https://api.dev.utterai.com/health/ready
curl https://api.dev.utterai.com/health/live
```

### KEDA 스케일 테스트

```bash
# SQS 메시지 전송
aws sqs send-message \
  --queue-url $(cd terraform/environments/dev && terraform output -raw analysis_queue_url) \
  --message-body '{"job_id":"test-001","type":"cpu"}' \
  --region ap-northeast-2

# CPU Worker Pod scale-out 감시
kubectl get pods -n utter-ai-cpu -w

# Karpenter 노드 생성 감시
kubectl get nodes -L workload -w
```

### GPU

```bash
# GPU 노드의 nvidia.com/gpu 리소스 등록 확인
kubectl get nodes -o custom-columns=\
NAME:.metadata.name,\
GPU:.status.allocatable."nvidia\.com/gpu",\
TYPE:.metadata.labels."node\.kubernetes\.io/instance-type"

# NVIDIA Device Plugin DaemonSet 확인
kubectl get daemonset -n kube-system | grep nvidia
```

### Secret 주입 확인

```bash
# ExternalSecret 상태
kubectl get externalsecret -A

# Secret 내용 확인 (디버깅용)
kubectl get secret backend-api-secret -n utter-api -o jsonpath='{.data.DB_PASSWORD}' | base64 -d
```

---

## 8. 남은 작업

### 8.1 미구현 — 우선순위 높음

| 항목 | 이유 |
|---|---|
| **External Secrets IRSA** | ESO가 Secrets Manager에 접근하려면 별도 IAM Role 필요. 현재 Terraform irsa 모듈에 미포함 |
| **Bastion Host** | Dev DB 직접 접근용. sg-dev-bastion SG + EC2 생성 필요 |
| **CloudWatch 알람** | `dev-backend-5xx-rate`, `dev-aurora-cpu`, `dev-sqs-dlq-count` |

### 8.2 CI/CD — 미구현

| 항목 | 설명 |
|---|---|
| GitHub Actions `dev-deploy.yaml` | `develop` 브랜치 push → Docker 빌드 → ECR push → ConfigMap 이미지 태그 업데이트 |
| ArgoCD Application | dev 클러스터 Auto-Sync 설정 (Shared Tooling Account에서 구성) |

### 8.3 선택 구현

| 항목 | 설명 |
|---|---|
| CloudFront | 프론트엔드 S3 연결 (프론트엔드팀 준비 후) |
| Observability | OpenTelemetry Collector, Prometheus/Grafana 기본 구성 |
| NetworkPolicy | Namespace 간 트래픽 격리 |
| GPU warm-up 전략 | 데모 전 GPU Node 1개 미리 기동 |

### 8.4 팀 협의 필요 (Workload 배포 전 필수)

| 항목 | 협의 대상 | 현재 설정값 |
|---|---|---|
| API container port | 백엔드팀 | `8080` (임시) |
| health check path | 백엔드팀 | `/health/ready`, `/health/live` (임시) |
| ECR 리포지토리 이름 확정 | 전체 팀 | `utterai-backend` 등 (임시) |
| SQS 메시지 payload 구조 | AI팀 + 백엔드팀 | 미정 |
| GPU 이미지 모델 포함 여부 | AI팀 | 미정 (이미지 크기에 큰 영향) |
| GPU Diarization 전용 큐 분리 여부 | AI팀 | 현재 analysis-queue 공용 |

---

## 참고

- EKS 아키텍처 상세 설계: [`infra-eks/eks-architecture-flow.md`](../eks-architecture-flow.md)
- Dev vs Prod 환경 비교: [`infra-eks/README.md`](../README.md)
- 브랜치/커밋 규칙: [`CONTRIBUTING.md`](../../CONTRIBUTING.md)
