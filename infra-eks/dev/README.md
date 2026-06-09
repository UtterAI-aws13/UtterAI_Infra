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
                                               ├── API Pod          (api NodePool, MNG + HPA)
                                               ├── AI API Pod       (ai-api NodePool, 내부 전용)
                                               ├── CPU AI Worker    (ai-cpu NodePool, HPA)
                                               ├── GPU AI Worker    (ai-gpu NodePool, HPA)
                                               └── Batch Worker     (utterai-batch, HPA)

SQS 파이프라인:
  audio-preprocess-queue → gpu-inference-queue → report-analysis-queue
  rag-ingest-queue (RAG 문서 ingest 전용)
```

---

## 2. 구현 현황

### 2.1 Terraform 모듈 — 완료

| 모듈 | 경로 | 주요 리소스 |
|---|---|---|
| `vpc` | `terraform/modules/vpc/` | VPC, Public/Private-App/Private-Data 서브넷(2AZ), NAT GW 1개, VPC Endpoint 5개 |
| `eks` | `terraform/modules/eks/` | EKS 클러스터, system/api Managed NodeGroup, OIDC Provider, Cluster Addons |
| `eks-addons` | `terraform/modules/eks-addons/` | LBC, Karpenter, KEDA, metrics-server, nvidia-device-plugin (Helm) |
| `irsa` | `terraform/modules/irsa/` | IAM Role 7개 (LBC, Karpenter, ESO, backend, ai-worker, gpu-worker, batch-worker) |
| `rds` | `terraform/modules/rds/` | RDS PostgreSQL 16, Single-AZ, 비밀번호 Secrets Manager 자동 관리 |
| `redis` | `terraform/modules/redis/` | ElastiCache Redis 7, 단일 노드, TLS 활성화 |
| `s3` | `terraform/modules/s3/` | 버킷 6개 (frontend, raw-audio, processed-audio, documents, reports, artifacts) |
| `sqs` | `terraform/modules/sqs/` | 큐 4개 + DLQ 4개: audio-preprocess, gpu-inference, report-analysis, rag-ingest |
| `secrets` | `terraform/modules/secrets/` | Secrets Manager 정책 및 접근 설정 |
| `cloudfront` | `terraform/modules/cloudfront/` | 프론트엔드 S3 배포 |
| `ecr` | `terraform/modules/ecr/` | ECR 리포지토리 |

> **VPC는 별도 사전 작업 없음.** `terraform apply` 한 번으로 VPC부터 EKS까지 모두 생성된다.

### 2.2 Kubernetes Manifest — 완료

| 디렉토리 | 파일 | 내용 |
|---|---|---|
| `k8s/namespaces/` | `namespaces.yaml` | utterai-api, utterai-ai-api, utterai-ai-cpu, utterai-ai-gpu, utterai-batch, utterai-observability |
| `k8s/rbac/` | `serviceaccounts.yaml` | SA 5개 + IRSA role-arn annotation (`${AWS_ACCOUNT_ID}` 치환 필요) |
| `k8s/rbac/` | `rolebindings.yaml` | 각 SA에 K8s 내부 최소 권한 |
| `k8s/secrets/` | `cluster-secret-store.yaml` | ClusterSecretStore: `aws-secrets-manager` |
| `k8s/secrets/` | `backend-api-external-secret.yaml` | utterai-api 네임스페이스 — DB_PASSWORD, JWT_SECRET_KEY, INTERNAL_CALLBACK_TOKEN, INTERNAL_CALLBACK_HMAC_SECRET |
| `k8s/secrets/` | `ai-worker-external-secret.yaml` | utterai-ai-gpu + utterai-batch — DB_USER/PASSWORD/HOST/PORT/NAME |
| `k8s/secrets/` | `gpu-worker-external-secret.yaml` | utterai-ai-gpu — HF_TOKEN |
| `k8s/ingress/` | `api-ingress.yaml` | ALB internet-facing, HTTPS 리다이렉트 |
| `k8s/workloads/` | `api-deployment.yaml` | 메인 백엔드 API Deployment + Service |
| `k8s/workloads/` | `ai-api-deployment.yaml` | AI 내부 API Deployment + Service |
| `k8s/workloads/` | `cpu-worker-deployment.yaml` | CPU 음성 전처리 워커 |
| `k8s/workloads/` | `llm-gpu-worker-deployment.yaml` | LLM 추론 GPU 워커 (report-analysis-queue 소비) |
| `k8s/workloads/` | `ml-gpu-worker-deployment.yaml` | ML STT/화자분리 GPU 워커 (gpu-inference-queue 소비) |
| `k8s/workloads/` | `batch-worker-deployment.yaml` | RAG ingest 배치 워커 |
| `k8s/workloads/` | `hpa-api.yaml` | API HPA (CPU 70%) |
| `k8s/workloads/` | `hpa-cpu-worker.yaml` | CPU 워커 HPA |
| `k8s/workloads/` | `hpa-llm-gpu-worker.yaml` | LLM GPU 워커 HPA (maxReplicas: 2) |
| `k8s/workloads/` | `hpa-ml-gpu-worker.yaml` | ML GPU 워커 HPA (maxReplicas: 2) |
| `k8s/workloads/` | `hpa-batch-worker.yaml` | 배치 워커 HPA |
| `k8s/apps/backend/` | `base/` + `overlays/dev/` | Kustomize 구조 (별도 배포 — `kubectl apply -k`) |

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
│       ├── rds/
│       ├── redis/
│       ├── s3/
│       ├── sqs/
│       ├── secrets/
│       ├── cloudfront/
│       └── ecr/
│
├── k8s/
│   ├── namespaces/     ← 네임스페이스 정의
│   ├── rbac/           ← ServiceAccount + RoleBinding
│   ├── secrets/        ← ClusterSecretStore + ExternalSecret
│   ├── ingress/        ← ALB Ingress
│   ├── workloads/      ← Deployment + HPA
│   └── apps/
│       └── backend/    ← Kustomize base/overlays (별도 배포)
│
└── scripts/
    └── k8s-deploy.sh   ← 워크로드 배포 자동화 (namespaces ~ ingress 일괄 적용)
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
for repo in utterai-backend utterai-ai-cpu utterai-ai-gpu; do
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

**Workload 배포(STEP 5) 전까지 완료**되어야 한다. 이미지가 없으면 Pod가 `ImagePullBackOff` 상태가 된다.

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
```

> **GPU 이미지 주의**: CUDA, PyTorch, 화자 분리 모델 포함 시 이미지가 수 GB에 달할 수 있다.  
> 모델 파일을 이미지에 포함할지 S3에서 런타임에 내려받을지 AI팀과 먼저 협의한다.

### 4.6 Secrets Manager 수동 시크릿 생성

`terraform apply` 후에 생성한다. 아래 3개 시크릿을 수동으로 만들어야 한다.

```bash
# 1. 백엔드 API 시크릿 — JWT 키, 내부 콜백 토큰
# DB_PASSWORD: RDS 마스터 비밀번호 (terraform output rds_db_secret_arn으로 확인 가능)
aws secretsmanager create-secret \
  --name "utterai-dev/backend-api-secret" \
  --secret-string '{
    "DB_PASSWORD": "<RDS 마스터 비밀번호>",
    "JWT_SECRET_KEY": "<강력한_랜덤_문자열>",
    "INTERNAL_CALLBACK_TOKEN": "<강력한_랜덤_문자열>",
    "INTERNAL_CALLBACK_HMAC_SECRET": "<강력한_랜덤_문자열>"
  }' \
  --region ap-northeast-2

# 2. AI/배치 워커 DB 접속 정보 — GPU/배치 워커가 DB에 직접 접속할 때 사용
export RDS_ENDPOINT=$(cd terraform/environments/dev && terraform output -raw rds_endpoint)
aws secretsmanager create-secret \
  --name "utterai-dev/ai-worker-secret" \
  --secret-string "{
    \"DB_USER\": \"utterai\",
    \"DB_PASSWORD\": \"<AI 워커용 DB 비밀번호>\",
    \"DB_HOST\": \"${RDS_ENDPOINT}\",
    \"DB_PORT\": \"5432\",
    \"DB_NAME\": \"utterai\"
  }" \
  --region ap-northeast-2

# 3. GPU 워커 HuggingFace 토큰
aws secretsmanager create-secret \
  --name "utterai-dev/gpu-worker-secret" \
  --secret-string '{"HF_TOKEN": "<HuggingFace Access Token>"}' \
  --region ap-northeast-2
```

> **RDS 마스터 비밀번호 확인 방법**:  
> Terraform `rds` 모듈의 `manage_master_user_password = true` 설정으로 RDS가 자동 생성한 시크릿(ARN: `terraform output -raw rds_db_secret_arn`)에서 확인한다.

```bash
# 생성 확인
aws secretsmanager list-secrets --region ap-northeast-2 \
  --query 'SecretList[?starts_with(Name, `utterai-dev`)].Name'
```

---

## 5. 적용 순서

### STEP 1 — Terraform apply (VPC · EKS · 전체 AWS 리소스)

VPC, 서브넷, EKS 클러스터, RDS, Redis, S3, SQS, IAM Role 등  
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
export RDS_ENDPOINT=$(terraform output -raw rds_endpoint)
export RDS_DB_SECRET_ARN=$(terraform output -raw rds_db_secret_arn)
export REDIS_ENDPOINT=$(terraform output -raw redis_endpoint)
export ACM_CERTIFICATE_ARN="<4.4에서 발급한 ARN>"

# SQS 큐 URL 확인 (참고용)
terraform output audio_preprocess_queue_url
terraform output gpu_inference_queue_url
terraform output report_analysis_queue_url
terraform output rag_ingest_queue_url
```

### STEP 2 — Secrets Manager 시크릿 생성

`terraform apply` 후 RDS가 준비되면 수동 시크릿을 생성한다. (사전 준비 4.6 참고)

```bash
# 생성 확인
aws secretsmanager list-secrets --region ap-northeast-2 \
  --query 'SecretList[?starts_with(Name, `utterai-dev`)].Name'
```

다음 3개가 모두 있어야 한다:
- `utterai-dev/backend-api-secret`
- `utterai-dev/ai-worker-secret`
- `utterai-dev/gpu-worker-secret`

### STEP 3 — kubeconfig 업데이트

```bash
aws eks update-kubeconfig \
  --name utterai-dev-eks \
  --region ap-northeast-2

kubectl get nodes  # system 노드 2개 이상 Ready 확인
kubectl get pods -n kube-system  # CoreDNS, kube-proxy 정상 확인
```

### STEP 4 — External Secrets Operator 설치

Workload의 Deployment가 Secrets Manager에서 값을 가져오는 K8s Secret을 참조한다.  
**이 단계가 없으면 STEP 5에서 Pod가 시작되지 않는다.**

```bash
helm repo add external-secrets https://charts.external-secrets.io
helm repo update

helm install external-secrets external-secrets/external-secrets \
  --namespace external-secrets \
  --create-namespace \
  --set installCRDs=true

# 설치 확인 (pod가 Running이 될 때까지 대기)
kubectl get pods -n external-secrets -w
```

### STEP 5 — k8s 워크로드 일괄 배포

`scripts/k8s-deploy.sh`가 다음 작업을 자동으로 수행한다:

1. AWS 계정 ID 및 ECR 최신 이미지 태그 조회 (`BACKEND_TAG`, `AI_CPU_TAG`, `AI_GPU_TAG`)
2. Terraform output에서 `RDS_ENDPOINT`, `REDIS_ENDPOINT` 자동 조회
3. `k8s/namespaces/`, `k8s/rbac/`, `k8s/secrets/`, `k8s/workloads/`, `k8s/ingress/` 순서로 `envsubst | kubectl apply`

```bash
# 프로젝트 루트에서 실행
export ACM_CERTIFICATE_ARN="<4.4에서 발급한 ARN>"
bash scripts/k8s-deploy.sh
```

배포 후 확인:

```bash
# 네임스페이스
kubectl get ns | grep utterai

# Pod 상태
kubectl get pods -n utterai-api -w
kubectl get pods -n utterai-ai-cpu
kubectl get pods -n utterai-ai-gpu

# ExternalSecret 동기화 상태
kubectl get externalsecret -A

# ALB 생성 확인 (1~3분 소요)
kubectl get ingress -n utterai-api
```

### STEP 6 — Route 53 레코드 생성

ALB DNS가 확인되면 즉시 등록한다. 이 단계가 없으면 `api.dev.utterai.com`으로 접근 불가.

```bash
# ALB DNS 확인
export ALB_DNS=$(kubectl get ingress utterai-api-ingress -n utterai-api \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')
echo "ALB DNS: ${ALB_DNS}"

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

---

## 6. 플레이스홀더 치환 목록

`scripts/k8s-deploy.sh`가 자동으로 주입하는 값과 수동으로 export해야 하는 값을 구분한다.

### 자동 주입 (k8s-deploy.sh가 처리)

| 변수 | 사용 위치 | 값 출처 |
|---|---|---|
| `${AWS_ACCOUNT_ID}` | serviceaccounts.yaml, 모든 Deployment | `aws sts get-caller-identity` |
| `${BACKEND_TAG}` | api-deployment.yaml, ai-api-deployment.yaml | ECR 최신 이미지 태그 |
| `${AI_CPU_TAG}` | cpu-worker-deployment.yaml | ECR 최신 이미지 태그 |
| `${AI_GPU_TAG}` | llm-gpu-worker-deployment.yaml, ml-gpu-worker-deployment.yaml | ECR 최신 이미지 태그 |
| `${RDS_ENDPOINT}` | api-deployment.yaml ConfigMap | `terraform output -raw rds_endpoint` |
| `${REDIS_ENDPOINT}` | api-deployment.yaml ConfigMap | `terraform output -raw redis_endpoint` |

### 수동 export 필요

| 변수 | 사용 위치 | 값 출처 |
|---|---|---|
| `${ACM_CERTIFICATE_ARN}` | api-ingress.yaml | `aws acm list-certificates` |

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

### API 엔드포인트

```bash
# Pod 상태
kubectl get pods -n utterai-api
kubectl get pods -n utterai-ai-cpu
kubectl get pods -n utterai-ai-gpu
kubectl get pods -n utterai-batch

# 헬스체크 (Route 53 등록 후)
curl https://api.dev.utterai.com/health/ready
curl https://api.dev.utterai.com/health/live
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
  --namespace utterai-api

kubectl get pod test-api -n utterai-api -o wide  # NODE 컬럼이 api 노드여야 함
kubectl delete pod test-api -n utterai-api
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
# ExternalSecret 상태 (READY가 True이어야 함)
kubectl get externalsecret -A

# Secret 내용 확인 (디버깅용)
kubectl get secret backend-api-secret -n utterai-api -o jsonpath='{.data.DB_PASSWORD}' | base64 -d
kubectl get secret ai-worker-secret -n utterai-ai-gpu -o jsonpath='{.data.DB_HOST}' | base64 -d
```

### SQS 파이프라인 테스트

```bash
# audio-preprocess 큐에 테스트 메시지 전송
aws sqs send-message \
  --queue-url $(cd terraform/environments/dev && terraform output -raw audio_preprocess_queue_url) \
  --message-body '{"job_id":"test-001","type":"preprocess"}' \
  --region ap-northeast-2

# CPU Worker Pod scale-out 감시
kubectl get pods -n utterai-ai-cpu -w

# Karpenter 노드 생성 감시
kubectl get nodes -L workload -w
```

---

## 8. 남은 작업

### 8.1 미구현 — 우선순위 높음

| 항목 | 이유 |
|---|---|
| **Karpenter NodePool / EC2NodeClass** | `k8s/nodepools/` 미구성. 현재 Managed NodeGroup만 사용. Karpenter 동적 스케일링이 필요하면 EC2NodeClass + NodePool 추가 필요 |
| **Bastion Host** | Dev DB 직접 접근용. sg-dev-bastion SG + EC2 생성 필요 |
| **CloudWatch 알람** | `dev-backend-5xx-rate`, `dev-rds-cpu`, `dev-sqs-dlq-count` |

### 8.2 CI/CD — 미구현

| 항목 | 설명 |
|---|---|
| GitHub Actions `dev-deploy.yaml` | `develop` 브랜치 push → Docker 빌드 → ECR push → `scripts/k8s-deploy.sh` 실행 |
| ArgoCD Application | dev 클러스터 Auto-Sync 설정 (Shared Tooling Account에서 구성) |

### 8.3 선택 구현

| 항목 | 설명 |
|---|---|
| KEDA ScaledObject | SQS 큐 깊이 기반 자동 스케일링. 현재 CPU 기반 HPA로만 운영 중 |
| Observability | OpenTelemetry Collector, Prometheus/Grafana 기본 구성 |
| NetworkPolicy | Namespace 간 트래픽 격리 |
| GPU warm-up 전략 | 데모 전 GPU Node 1개 미리 기동 |

### 8.4 팀 협의 필요 (Workload 배포 전 필수)

| 항목 | 협의 대상 | 현재 설정값 |
|---|---|---|
| API container port | 백엔드팀 | `8080` (임시) |
| health check path | 백엔드팀 | `/health/ready`, `/health/live` |
| ECR 리포지토리 이름 확정 | 전체 팀 | `utterai-backend` 등 (임시) |
| SQS 메시지 payload 구조 | AI팀 + 백엔드팀 | 미정 |
| GPU 이미지 모델 포함 여부 | AI팀 | 미정 (이미지 크기에 큰 영향) |

---

## 참고

- EKS 아키텍처 상세 설계: [`infra-eks/eks-architecture-flow.md`](../eks-architecture-flow.md)
- Dev vs Prod 환경 비교: [`infra-eks/README.md`](../README.md)
- 브랜치/커밋 규칙: [`CONTRIBUTING.md`](../../CONTRIBUTING.md)
