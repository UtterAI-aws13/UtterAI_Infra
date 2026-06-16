# UtterAI Dev 환경 — 구현 현황 및 적용 가이드

> AWS ap-northeast-2 | EKS 1.31 | Terraform + Kubernetes

---

## 목차

1. [환경 개요](#1-환경-개요)
2. [구현 현황](#2-구현-현황)
3. [디렉토리 구조](#3-디렉토리-구조)
4. [State 레이어 구조](#4-state-레이어-구조)
5. [사전 준비](#5-사전-준비)
   - [5.6 EKS 접근 권한 및 kubeconfig 설정](#56-eks-접근-권한-및-kubeconfig-설정)
6. [적용 순서](#6-적용-순서)
   - [기존 단일 State에서 마이그레이션하는 경우](#61-기존-단일-state에서-마이그레이션하는-경우)
   - [처음부터 새로 배포하는 경우](#62-처음부터-새로-배포하는-경우)
7. [K8s 매니페스트 배포](#7-k8s-매니페스트-배포)
8. [플레이스홀더 치환 목록](#8-플레이스홀더-치환-목록)
9. [검증 명령어](#9-검증-명령어)
10. [알려진 이슈 및 트러블슈팅](#10-알려진-이슈-및-트러블슈팅)
11. [남은 작업](#11-남은-작업)

---

## 관련 Runbook

- [Monitoring Runbook](../shared/monitoring-runbook.md): Grafana 접속, Prometheus/Loki 확인, port-forward, 기본 트러블슈팅
- [Troubleshooting](./troubleshooting/2026-06-16.md): EKS/Terraform/Observability 적용 중 반복될 수 있는 문제와 해결 패턴

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
| Terraform State S3 버킷 | `utterai-dev-terraform-state` |

### 아키텍처 요약

```
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
| `eks-addons` | `terraform/modules/eks-addons/` | LBC, Cluster Autoscaler, Metrics Server, External Secrets Operator, NVIDIA Device Plugin, ArgoCD (Helm) |
| `irsa` | `terraform/modules/irsa/` | IAM Role 7개 (LBC, Cluster Autoscaler, ESO, backend, ai-worker, gpu-worker, batch-worker) |
| `rds` | `terraform/modules/rds/` | RDS PostgreSQL 16, Single-AZ, 비밀번호 Secrets Manager 자동 관리 |
| `redis` | `terraform/modules/redis/` | ElastiCache Redis 7, 단일 노드, TLS 활성화 |
| `s3` | `terraform/modules/s3/` | 버킷 6개 (frontend, raw-audio, processed-audio, documents, reports, artifacts) |
| `sqs` | `terraform/modules/sqs/` | 큐 4개 + DLQ 4개: audio-preprocess, gpu-inference, report-analysis, rag-ingest |
| `secrets` | `terraform/modules/secrets/` | Secrets Manager 정책 및 접근 설정 |
| `cloudfront` | `terraform/modules/cloudfront/` | 프론트엔드 S3 배포 |
| `ecr` | `terraform/modules/ecr/` | ECR 리포지토리 |

### 2.2 Kubernetes Manifest — 완료

| 디렉토리 | 파일 | 내용 |
|---|---|---|
| `k8s-legacy/namespaces/` | `namespaces.yaml` | utterai-api, utterai-ai-api, utterai-ai-cpu, utterai-ai-gpu, utterai-batch, utterai-observability |
| `k8s-legacy/rbac/` | `serviceaccounts.yaml` | SA 5개 + IRSA role-arn annotation |
| `k8s-legacy/rbac/` | `rolebindings.yaml` | 각 SA에 K8s 내부 최소 권한 |
| `k8s-legacy/secrets/` | `cluster-secret-store.yaml` | ClusterSecretStore: `aws-secrets-manager` |
| `k8s-legacy/secrets/` | `backend-api-external-secret.yaml` | utterai-api — DB_PASSWORD, JWT_SECRET_KEY 등 |
| `k8s-legacy/secrets/` | `ai-worker-external-secret.yaml` | utterai-ai-gpu + utterai-batch — DB 접속 정보 |
| `k8s-legacy/secrets/` | `gpu-worker-external-secret.yaml` | utterai-ai-gpu — HF_TOKEN |
| `k8s-legacy/ingress/` | `api-ingress.yaml` | ALB internet-facing, HTTPS 리다이렉트 |
| `k8s-legacy/workloads/` | `*-deployment.yaml` | API, AI API, CPU Worker, ML GPU Worker, Batch Worker |
| `k8s-legacy/workloads/` | `hpa-*.yaml` | 각 워크로드 HPA |
| `k8s/apps/backend/` | `base/` + `overlays/dev/` | Kustomize 구조 (별도 배포) |

---

## 3. 디렉토리 구조

```
UtterAI_Infra/
├── terraform/
│   ├── environments/
│   │   └── dev/
│   │       ├── 01-network/       ← VPC, 서브넷 (State: dev/network)
│   │       │   ├── terraform.tf
│   │       │   ├── main.tf
│   │       │   ├── variables.tf
│   │       │   └── outputs.tf
│   │       ├── 02-eks/           ← EKS 클러스터, NodeGroup (State: dev/platform)
│   │       ├── 03-services/      ← RDS, Redis, S3, SQS, IRSA, ECR (State: dev/services)
│   │       └── 04-addons/        ← Helm 애드온, CloudFront (State: dev/addons)
│   └── modules/
│       ├── vpc/
│       ├── eks/
│       ├── eks-addons/
│       ├── irsa/
│       ├── rds/
│       ├── redis/
│       ├── s3/
│       ├── sqs/
│       ├── secrets/
│       ├── cloudfront/
│       └── ecr/
│
├── k8s-legacy/
│   ├── namespaces/
│   ├── rbac/
│   ├── secrets/
│   ├── ingress/
│   ├── workloads/
│   └── apps/backend/             ← Kustomize base/overlays (별도 배포)
│
└── scripts/
    ├── k8s-deploy-legacy.sh             ← 워크로드 배포 자동화
    └── migrate-state.sh          ← State 마이그레이션 (단일 루트 → 4 레이어)
```

---

## 4. State 레이어 구조

기존 단일 루트(`dev/terraform.tfstate`)에서 **4개 레이어로 분리**된 구조로 전환됐다.  
레이어 간 값 전달은 `data.terraform_remote_state`로 처리한다.

```
S3 버킷: utterai-dev-terraform-state
│
├── dev/network/terraform.tfstate    ← 01-network (VPC, 서브넷, 라우팅)
├── dev/platform/terraform.tfstate   ← 02-eks (EKS 클러스터, NodeGroup, OIDC)
├── dev/services/terraform.tfstate   ← 03-services (RDS, Redis, S3, SQS, IRSA, ECR, Secrets)
└── dev/addons/terraform.tfstate     ← 04-addons (Helm 릴리스, CloudFront)
```

### 레이어 간 의존 관계

```
01-network ──→ 02-eks ──→ 03-services ──→ 04-addons
                │                              ↑
                └──────── (vpc_id) ────────────┘
```

`04-addons`는 세 레이어의 output을 모두 읽는다:

```hcl
data "terraform_remote_state" "network"  { key = "dev/network/terraform.tfstate" }
data "terraform_remote_state" "eks"      { key = "dev/platform/terraform.tfstate" }
data "terraform_remote_state" "services" { key = "dev/services/terraform.tfstate" }
```

### 분리 이유

| 문제 | 해결 |
|---|---|
| EKS 재생성 시 RDS/S3도 plan에 포함되어 위험 | 레이어별 독립 plan/apply |
| Helm Provider가 EKS endpoint를 참조 → 단일 root에서 초기화 불가 | 04-addons만 helm/kubernetes provider 사용 |
| `terraform plan` 시간 단축 | 변경 레이어만 plan |

---

## 5. 사전 준비

### 5.1 필수 도구 설치

```bash
terraform --version   # >= 1.6
kubectl version --client
aws --version         # v2
envsubst --version    # apt: gettext / brew: gettext
```

> `helm` CLI는 로컬에 없어도 된다. EKS 애드온(LBC, ArgoCD 등)은 `04-addons` Terraform이 Helm Provider로 설치한다.

### 5.2 Terraform Backend 수동 생성 (terraform init 전)

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
```

> DynamoDB lock 테이블은 불필요하다. `use_lockfile = true`로 S3 네이티브 락을 사용한다.

### 5.3 ACM 인증서 발급 (ALB HTTPS용)

Terraform apply 전에 미리 발급한다 (DNS 검증에 수 분 소요).

```bash
aws acm request-certificate \
  --domain-name "*.dev.utterai.com" \
  --validation-method DNS \
  --region ap-northeast-2

# ARN 확인 (Issued 상태 확인 후)
aws acm list-certificates --region ap-northeast-2 \
  --query 'CertificateSummaryList[?DomainName==`*.dev.utterai.com`].CertificateArn' \
  --output text
```

### 5.4 Docker 이미지 빌드 및 ECR 푸시 (워크로드 배포 전 완료)

```bash
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export ECR_REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.ap-northeast-2.amazonaws.com"
export GIT_SHA=$(git rev-parse --short HEAD)

aws ecr get-login-password --region ap-northeast-2 \
  | docker login --username AWS --password-stdin ${ECR_REGISTRY}

# 각 팀이 담당 이미지를 빌드 및 푸시
docker build -t ${ECR_REGISTRY}/utterai-backend:${GIT_SHA} <백엔드 repo 경로>
docker push ${ECR_REGISTRY}/utterai-backend:${GIT_SHA}
docker tag  ${ECR_REGISTRY}/utterai-backend:${GIT_SHA} ${ECR_REGISTRY}/utterai-backend:latest
docker push ${ECR_REGISTRY}/utterai-backend:latest
```

### 5.5 Secrets Manager 수동 시크릿 생성 (03-services apply 후)

```bash
# 1. 백엔드 API 시크릿
aws secretsmanager create-secret \
  --name "utterai-dev/backend-api-secret" \
  --secret-string '{
    "DB_PASSWORD": "<RDS 마스터 비밀번호>",
    "JWT_SECRET_KEY": "<랜덤 문자열>",
    "INTERNAL_CALLBACK_TOKEN": "<랜덤 문자열>",
    "INTERNAL_CALLBACK_HMAC_SECRET": "<랜덤 문자열>"
  }' \
  --region ap-northeast-2

# 2. AI/배치 워커 DB 접속 정보
export RDS_ENDPOINT=$(cd terraform/environments/dev/03-services && terraform output -raw rds_endpoint)
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

# 확인
aws secretsmanager list-secrets --region ap-northeast-2 \
  --query 'SecretList[?starts_with(Name, `utterai-dev`)].Name'
```

### 5.6 EKS 접근 권한 및 kubeconfig 설정

> `aws configure`는 AWS CLI 자격증명만 설정한다. EKS kubectl 접근을 위해서는 **① IAM 권한 부여 → ② kubeconfig 업데이트** 두 단계가 모두 필요하다.

#### ① IAM 사용자/역할에 EKS 접근 권한 부여

EKS 1.29 이상은 **EKS Access Entry** 방식을 사용한다. 클러스터 생성자(또는 Admin)가 아래 명령으로 팀원의 IAM 사용자나 역할을 등록한다.

```bash
# 현재 등록된 Access Entry 확인
aws eks list-access-entries --cluster-name utterai-dev-eks --region ap-northeast-2

# IAM 사용자에게 클러스터 관리자 권한 부여
aws eks create-access-entry \
  --cluster-name utterai-dev-eks \
  --principal-arn arn:aws:iam::032886669461:user/<IAM-사용자명> \
  --region ap-northeast-2

aws eks associate-access-policy \
  --cluster-name utterai-dev-eks \
  --principal-arn arn:aws:iam::032886669461:user/<IAM-사용자명> \
  --policy-arn arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy \
  --access-scope type=cluster \
  --region ap-northeast-2
```

권한 수준별 정책:

| 정책 ARN | 설명 |
|---|---|
| `AmazonEKSClusterAdminPolicy` | 클러스터 전체 관리 (cluster-admin) |
| `AmazonEKSAdminPolicy` | 네임스페이스 포함 대부분의 관리 |
| `AmazonEKSEditPolicy` | 워크로드 배포/수정 (읽기+쓰기) |
| `AmazonEKSViewPolicy` | 읽기 전용 |

> **팀 합류 시**: 클러스터 생성자에게 IAM 사용자 ARN을 전달하여 Access Entry 등록을 요청한다.

---

#### ② kubeconfig 업데이트

AWS CLI 자격증명이 올바르게 설정된 후 아래 명령을 실행한다.

```bash
# 1. 현재 자격증명 확인 (이 사용자가 Access Entry에 등록되어 있어야 함)
aws sts get-caller-identity

# 2. kubeconfig 업데이트
aws eks update-kubeconfig \
  --name utterai-dev-eks \
  --region ap-northeast-2

# 3. context 전환 확인
kubectl config current-context
# 출력: arn:aws:eks:ap-northeast-2:032886669461:cluster/utterai-dev-eks

# 4. 접근 확인
kubectl get nodes
```

#### 자주 발생하는 문제

| 증상 | 원인 | 해결 |
|---|---|---|
| `kubectl`이 docker-desktop 등 로컬 클러스터를 바라봄 | kubeconfig 업데이트 미실행 | `aws eks update-kubeconfig` 실행 |
| `error: You must be logged in to the server (Unauthorized)` | IAM 사용자가 Access Entry에 없음 | 클러스터 관리자에게 등록 요청 |
| `error: no such host` | 잘못된 region 또는 cluster name | `--region ap-northeast-2 --name utterai-dev-eks` 확인 |
| `could not load credentials file` | `aws configure` 미완료 | `aws configure` 후 `aws sts get-caller-identity`로 확인 |

#### 여러 context 관리 (선택)

```bash
# 등록된 context 목록
kubectl config get-contexts

# 특정 context로 전환
kubectl config use-context arn:aws:eks:ap-northeast-2:032886669461:cluster/utterai-dev-eks

# 특정 context를 명시하여 명령 실행 (전환 없이)
kubectl get nodes --context=<context-name>
```

---

## 6. 적용 순서

### 6.1 기존 단일 State에서 마이그레이션하는 경우

이전에 `terraform/environments/dev/`(단일 루트)로 이미 apply한 리소스가 있다면, State를 4개 레이어로 이동한 뒤 각 레이어에서 apply한다.

```bash
# 프로젝트 루트에서 실행
bash scripts/migrate-state.sh
```

스크립트가 수행하는 작업:
1. 현재 단일 State를 S3에서 pull하여 백업 (`/tmp/utterai-dev-work.tfstate.bak`)
2. 각 레이어 디렉토리에서 `terraform init -reconfigure`
3. 해당 레이어의 module을 로컬 tfstate로 `terraform state mv`
4. 로컬 tfstate를 각 레이어의 S3 backend로 `terraform state push`

마이그레이션 완료 후 각 레이어를 순서대로 apply한다 → [6.2 STEP 1~4 참고](#62-처음부터-새로-배포하는-경우)

> 마이그레이션 후 기존 단일 루트의 `main.tf`, `variables.tf`, `outputs.tf`, `terraform.tf`, `terraform.tfvars` 파일은 삭제해도 된다.  
> S3의 `dev/terraform.tfstate`는 롤백을 위해 보존한다.

---

### 6.2 처음부터 새로 배포하는 경우

#### STEP 1 — 01-network (VPC)

```bash
cd terraform/environments/dev/01-network
terraform init
terraform plan
terraform apply   # 약 3~5분
```

생성 리소스: VPC, Public/Private 서브넷(6개), NAT GW, Internet GW, 라우팅 테이블, VPC Endpoint

---

#### STEP 2 — 02-eks (EKS 클러스터)

```bash
cd terraform/environments/dev/02-eks
terraform init
terraform plan
terraform apply   # 약 15~20분
```

생성 리소스: EKS 클러스터(`utterai-dev-eks`), system/api Managed NodeGroup, OIDC Provider, EKS Add-ons(CoreDNS, kube-proxy, vpc-cni)

---

#### STEP 3 — 03-services (데이터/앱 서비스)

```bash
cd terraform/environments/dev/03-services
terraform init
terraform plan
terraform apply   # 약 10~15분
```

생성 리소스: RDS PostgreSQL, ElastiCache Redis, S3 버킷(6개), SQS 큐(4개+DLQ 4개), ECR 리포지토리, IRSA IAM Role(7개), Secrets Manager 설정

apply 완료 후 Secrets Manager 시크릿을 수동 생성한다 ([5.5 참고](#55-secrets-manager-수동-시크릿-생성-03-services-apply-후)).

```bash
# output 저장 (STEP 4, 워크로드 배포에서 사용)
export RDS_ENDPOINT=$(terraform output -raw rds_endpoint)
export REDIS_ENDPOINT=$(terraform output -raw redis_endpoint)
```

---

#### STEP 4 — 04-addons (EKS 애드온 Helm 설치)

> **전제 조건**: kubeconfig가 `utterai-dev-eks` 클러스터를 가리키고 있어야 한다.

```bash
aws eks update-kubeconfig --name utterai-dev-eks --region ap-northeast-2
kubectl get nodes   # system 노드 Ready 확인 후 진행
```

```bash
cd terraform/environments/dev/04-addons
terraform init
terraform plan
terraform apply   # 약 10~15분
```

Alertmanager Slack 알림을 운영할 때는 `alertmanager_slack_enabled=true`를 함께 적용한다. Slack webhook URL은 `terraform.tfvars`에 넣지 않고 Secrets Manager에만 저장한다.

```bash
terraform plan -var='alertmanager_slack_enabled=true'
terraform apply -var='alertmanager_slack_enabled=true'
```

Grafana admin credential을 Secrets Manager/External Secrets로 관리할 때는 Secrets Manager에 JSON 값을 먼저 넣은 뒤 `grafana_admin_credentials_enabled=true`를 함께 적용한다.

```bash
aws secretsmanager put-secret-value \
  --secret-id utterai-dev/grafana-admin-credentials \
  --secret-string '{"admin_user":"admin","admin_password":"<GRAFANA_ADMIN_PASSWORD>"}'

terraform plan \
  -var='alertmanager_slack_enabled=true' \
  -var='grafana_admin_credentials_enabled=true'
terraform apply \
  -var='alertmanager_slack_enabled=true' \
  -var='grafana_admin_credentials_enabled=true'
```

반복 적용 시 로컬 전용 `terraform.tfvars`를 사용할 수 있다. 이 파일은 `.gitignore` 대상이다.

```bash
cp terraform.tfvars.example terraform.tfvars
```

설치 순서 (Terraform 내부 의존 관계에 의해 자동 제어):

```
aws-load-balancer-controller  (wait=true → pod Ready 확인 후 다음 진행)
        ↓ depends_on
cluster-autoscaler / metrics-server / external-secrets / nvidia-device-plugin / argocd
```

> **LBC webhook 이슈**: LBC는 모든 네임스페이스의 Service 생성에 개입하는 MutatingWebhook을 등록한다.  
> `wait = true`로 LBC pod가 Ready된 후 나머지 릴리스가 배포되도록 의존 관계를 설정했다.

설치 확인:

```bash
kubectl get pods -n ingress-system      # aws-load-balancer-controller
kubectl get pods -n kube-system         # cluster-autoscaler, metrics-server, nvidia-device-plugin
kubectl get pods -n external-secrets    # external-secrets
kubectl get pods -n argocd              # argo-cd-server 등
```

---

## 7. K8s 매니페스트 배포

Terraform이 인프라를 구성한 뒤, 애플리케이션 워크로드를 K8s에 배포한다.

### 7.1 전제 조건 확인

```bash
# ESO CRD 등록 확인 (external-secrets pod가 Running이어야 함)
kubectl get crd | grep external-secrets.io

# ClusterSecretStore가 없으면 STEP 4가 완료되지 않은 것
kubectl get clustersecretstore
```

### 7.2 자동 배포 스크립트 실행

`scripts/k8s-deploy-legacy.sh`가 아래 작업을 순서대로 수행한다:

1. `aws sts get-caller-identity`로 `AWS_ACCOUNT_ID` 조회
2. ECR에서 각 이미지의 최신 태그(`BACKEND_TAG`, `AI_CPU_TAG`, `AI_GPU_TAG`) 조회
3. `terraform output`에서 `RDS_ENDPOINT`, `REDIS_ENDPOINT` 자동 조회
4. `k8s-legacy/namespaces/` → `k8s-legacy/rbac/` → `k8s-legacy/secrets/` → `k8s-legacy/workloads/` → `k8s-legacy/ingress/` 순서로 `envsubst | kubectl apply`

```bash
# ACM ARN을 환경변수로 export 후 프로젝트 루트에서 실행
export ACM_CERTIFICATE_ARN="<5.3에서 발급한 ARN>"
bash scripts/k8s-deploy-legacy.sh
```

### 7.3 배포 후 확인

```bash
# 1. 네임스페이스
kubectl get ns | grep utterai

# 2. ServiceAccount + IRSA annotation 확인
kubectl get sa -n utterai-api utterai-api -o jsonpath='{.metadata.annotations}'

# 3. ExternalSecret 동기화 (READY=True 확인)
kubectl get externalsecret -A

# 4. Pod 상태
kubectl get pods -n utterai-api -w
kubectl get pods -n utterai-ai-cpu
kubectl get pods -n utterai-ai-gpu
kubectl get pods -n utterai-batch

# 5. ALB 생성 확인 (1~3분 소요)
kubectl get ingress -n utterai-api
```

### 7.4 Route 53 레코드 등록

```bash
export ALB_DNS=$(kubectl get ingress utterai-api-ingress -n utterai-api \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')

export HOSTED_ZONE_ID=$(aws route53 list-hosted-zones \
  --query "HostedZones[?Name=='dev.utterai.com.'].Id" \
  --output text | sed 's|/hostedzone/||')

# ALB Hosted Zone ID (ap-northeast-2 고정값)
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

### 7.5 Kustomize 배포 (백엔드 Kustomize 구조 사용 시)

```bash
# dev overlay 적용
kubectl apply -k k8s/apps/backend/overlays/dev
```

---

## 8. 플레이스홀더 치환 목록

### 자동 주입 (k8s-deploy-legacy.sh가 처리)

| 변수 | 사용 위치 | 값 출처 |
|---|---|---|
| `${AWS_ACCOUNT_ID}` | serviceaccounts.yaml, 모든 Deployment | `aws sts get-caller-identity` |
| `${BACKEND_TAG}` | api-deployment.yaml, ai-api-deployment.yaml | ECR 최신 이미지 태그 |
| `${AI_CPU_TAG}` | cpu-worker-deployment.yaml | ECR 최신 이미지 태그 |
| `${AI_GPU_TAG}` | ml-gpu-worker-deployment.yaml | ECR 최신 이미지 태그 |
| `${RDS_ENDPOINT}` | api-deployment.yaml ConfigMap | `terraform output -raw rds_endpoint` (03-services) |
| `${REDIS_ENDPOINT}` | api-deployment.yaml ConfigMap | `terraform output -raw redis_endpoint` (03-services) |

### 수동 export 필요

| 변수 | 사용 위치 | 값 출처 |
|---|---|---|
| `${ACM_CERTIFICATE_ARN}` | api-ingress.yaml | `aws acm list-certificates` |

---

## 9. 검증 명령어

### EKS 노드

```bash
kubectl get nodes -L workload,karpenter.sh/capacity-type,node.kubernetes.io/instance-type
kubectl get pods -n kube-system
kubectl get pods -n ingress-system
kubectl get pods -n external-secrets
kubectl get pods -n argocd
```

### API 엔드포인트

```bash
# Route 53 등록 후
curl https://api.dev.utterai.com/health/ready
curl https://api.dev.utterai.com/health/live
```

### NodePool 배치 테스트

```bash
kubectl run test-api --image=nginx \
  --overrides='{"spec":{"nodeSelector":{"workload":"api"},"tolerations":[{"key":"dedicated","operator":"Equal","value":"api","effect":"NoSchedule"}]}}' \
  --namespace utterai-api

kubectl get pod test-api -n utterai-api -o wide
kubectl delete pod test-api -n utterai-api
```

### GPU 노드

```bash
kubectl get nodes -o custom-columns=\
NAME:.metadata.name,\
GPU:.status.allocatable."nvidia\.com/gpu",\
TYPE:.metadata.labels."node\.kubernetes\.io/instance-type"

kubectl get daemonset -n kube-system | grep nvidia
```

### Secret 주입 확인

```bash
kubectl get externalsecret -A

# 디버깅용
kubectl get secret backend-api-secret -n utterai-api \
  -o jsonpath='{.data.DB_PASSWORD}' | base64 -d
```

### SQS 파이프라인 테스트

```bash
aws sqs send-message \
  --queue-url $(cd terraform/environments/dev/03-services && terraform output -raw audio_preprocess_queue_url) \
  --message-body '{"job_id":"test-001","type":"preprocess"}' \
  --region ap-northeast-2

kubectl get pods -n utterai-ai-cpu -w
kubectl get nodes -L workload -w
```

---

## 10. 알려진 이슈 및 트러블슈팅

반복될 수 있는 EKS/Terraform/Observability 문제는 [Troubleshooting](../troubleshooting.md)에 먼저 추가한다. 이 섹션에는 Dev 배포 가이드 안에서 바로 알아야 하는 대표 이슈만 남긴다.

### LBC Webhook으로 인한 Helm 릴리스 실패

**증상**: `04-addons` apply 시 external-secrets 등의 Helm 릴리스가 아래 오류로 실패한다.

```
Error: failed calling webhook "mservice.elbv2.k8s.aws": no endpoints available
```

**원인**: LBC가 등록하는 MutatingWebhook이 모든 Service 생성을 가로채는데, LBC pod가 Ready되기 전에 다른 차트가 Service를 생성하려 해서 발생한다.

**현재 대응**: `aws_load_balancer_controller`에 `wait = true`를 설정하고, 나머지 모든 Helm 릴리스에 `depends_on = [helm_release.aws_load_balancer_controller]`를 추가했다.

**이미 실패한 릴리스가 있다면** (helm CLI 없이 kubectl로 처리):

```bash
# 실패한 네임스페이스 삭제 (Helm Secret까지 함께 제거됨)
kubectl delete namespace external-secrets --ignore-not-found

# Terraform state에서 해당 리소스 제거
terraform state rm 'module.eks_addons.helm_release.external_secrets'

# 재적용
terraform apply
```

### Helm release가 "failed" 상태로 state에 남아 있을 때

```bash
# state 목록 확인
terraform state list | grep helm

# 특정 릴리스 제거 후 재적용
terraform state rm 'module.eks_addons.helm_release.<이름>'
terraform apply
```

---

## 11. 남은 작업

### 우선순위 높음

| 항목 | 설명 |
|---|---|
| **Karpenter NodePool / EC2NodeClass** | `k8s-legacy/nodepools/` 미구성. 동적 스케일링이 필요하면 추가 필요 |
| **Bastion Host** | Dev DB 직접 접근용. EC2 + SG 생성 필요 |
| **CloudWatch 알람** | `dev-backend-5xx-rate`, `dev-rds-cpu`, `dev-sqs-dlq-count` |

### CI/CD

| 항목 | 설명 |
|---|---|
| GitHub Actions `dev-deploy.yaml` | `develop` push → Docker 빌드 → ECR push → `k8s-deploy-legacy.sh` 실행 |
| ArgoCD Application 설정 | dev 클러스터 Auto-Sync (Shared Tooling Account에서 구성) |

### 선택 구현

| 항목 | 설명 |
|---|---|
| KEDA ScaledObject | SQS 큐 깊이 기반 자동 스케일링 (현재 CPU 기반 HPA로만 운영) |
| Observability | OpenTelemetry Collector, Prometheus/Grafana 기본 구성 |
| NetworkPolicy | Namespace 간 트래픽 격리 |
| GPU warm-up 전략 | 데모 전 GPU Node 1개 미리 기동 |

### 팀 협의 필요

| 항목 | 협의 대상 | 현재 설정값 |
|---|---|---|
| API container port | 백엔드팀 | `8080` (임시) |
| health check path | 백엔드팀 | `/health/ready`, `/health/live` |
| ECR 리포지토리 이름 확정 | 전체 팀 | `utterai-backend` 등 (임시) |
| SQS 메시지 payload 구조 | AI팀 + 백엔드팀 | 미정 |
| GPU 이미지 모델 포함 여부 | AI팀 | 미정 |

---

## 참고

- Terraform 레이어 의존 흐름 (output 연결, 리소스 설치 상세): [`docs/dev/terraform/dependency-flow.md`](./terraform/dependency-flow.md)
- EKS 아키텍처 상세 설계: [`docs/shared/eks-architecture-flow.md`](../shared/eks-architecture-flow.md)
- Dev vs Prod 환경 비교: [`docs/README.md`](../README.md)
- Dev 보안 전체 현황: [`docs/dev/security/overview.md`](./security/overview.md)
- Dev 보안 수정 이력: [`docs/dev/security/hardening.md`](./security/hardening.md)
- 부하 테스트 시나리오: [`docs/dev/load-test-scenarios.md`](./load-test-scenarios.md)
- 브랜치/커밋 규칙: [`CONTRIBUTING.md`](../../CONTRIBUTING.md)
