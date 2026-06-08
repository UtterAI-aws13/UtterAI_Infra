# Dev 환경 배포 가이드

> **전제 조건**
> - AWS CLI 설치 및 `UtterAI-dohyun` 계정 로그인 (`aws sts get-caller-identity` 로 확인)
> - Terraform >= 1.6, kubectl 설치
> - S3 state 버킷 `utterai-dev-terraform-state` 존재 확인

---

## 전체 배포 순서

```
1. Terraform apply          → AWS 인프라 생성
2. GitHub Environments 설정 → CI/CD 변수 입력 (1회)
3. Secrets Manager 초기화   → 앱 시크릿 값 입력 (1회)
4. kubectl 연결             → EKS 클러스터 연결
5. k8s 매니페스트 배포      → 워크로드 기동
6. 앱 배포 (CI 자동)        → 각 레포 dev 브랜치 push 시 자동 처리
```

> **앱 이미지 빌드 & 프론트엔드 배포는 수동 불필요.**
> FE/BE/AI 각 레포의 GitHub Actions가 `dev` 브랜치 push 시 자동으로 ECR push / S3 배포 / CloudFront 무효화까지 처리한다.

---

## 1. Terraform — AWS 인프라 배포

> **주의: 반드시 2단계로 나눠서 apply해야 한다.**
> EKS 클러스터가 생성된 직후 helm provider가 연결을 잡지 못하는 타이밍 문제로,
> 한 번에 apply하면 `eks_addons` (LBC, metrics-server 등)가 실패한다.

### 1단계 — EKS 및 기반 인프라 생성

```bash
cd terraform/environments/dev

terraform init

terraform apply \
  -target=module.vpc \
  -target=module.eks \
  -target=module.irsa \
  -target=module.s3 \
  -target=module.sqs \
  -target=module.rds \
  -target=module.redis \
  -target=module.secrets \
  -target=module.ecr \
  -target=module.cloudfront
```

### 1단계 완료 후 — kubeconfig 업데이트

```bash
aws eks update-kubeconfig \
  --region ap-northeast-2 \
  --name utterai-dev-eks

kubectl get nodes  # 노드 Ready 확인
```

### 2단계 — EKS Addons 설치 (helm)

```bash
terraform apply
```

완료 후 output 확인:

```bash
terraform output
```

주요 output:

| output | 용도 |
|--------|------|
| `cluster_name` | kubectl 연결 |
| `rds_endpoint` | DB 연결 주소 |
| `rds_db_secret_arn` | RDS 자동 생성 비밀번호 조회용 ARN |
| `redis_endpoint` | Redis 연결 주소 |
| `cloudfront_domain_name` | 프론트엔드 접속 주소 |
| `cloudfront_distribution_id` | GitHub Actions 환경 변수 설정 시 사용 |

> **GPU 노드**: dev 환경은 vCPU 한도 문제로 `gpu_node_min_size = 0`.
> GPU 워커가 필요하면 AWS 콘솔에서 G 인스턴스 vCPU 한도 증가 요청 후 `tfvars` 수정.

---

## 2. GitHub Environments 설정 (1회)

각 레포의 GitHub → Settings → Environments → `dev` 환경에 아래 변수를 등록한다.

### UtterAI_FE

| 변수명 | 값 |
|--------|-----|
| `AWS_REGION` | `ap-northeast-2` |
| `AWS_ROLE_ARN` | GitHub Actions용 IAM Role ARN |
| `FRONTEND_S3_BUCKET` | `utterai-dev-frontend` |
| `CLOUDFRONT_DISTRIBUTION_ID` | `terraform output cloudfront_distribution_id` 값 |
| `VITE_API_BASE_URL` | ALB DNS (kubectl get ingress 로 확인) |

### UtterAI_BE

| 변수명 | 값 |
|--------|-----|
| `AWS_REGION` | `ap-northeast-2` |
| `AWS_ROLE_ARN` | GitHub Actions용 IAM Role ARN |
| `ECR_BACKEND_REPOSITORY` | `utterai-backend` |

### UtterAI_AI

| 변수명 | 값 |
|--------|-----|
| `AWS_REGION` | `ap-northeast-2` |
| `AWS_ROLE_ARN` | GitHub Actions용 IAM Role ARN |
| `ECR_AI_CPU_REPOSITORY` | `utterai-ai-cpu` |
| `ECR_AI_GPU_REPOSITORY` | `utterai-ai-gpu` |

---

## 3. Secrets Manager — 초기 시크릿 값 입력 (1회)

Terraform이 시크릿 껍데기를 생성하지만 값은 수동으로 입력해야 한다.

### RDS 자동 생성 비밀번호 확인

```bash
aws secretsmanager get-secret-value \
  --secret-id $(terraform output -raw rds_db_secret_arn) \
  --region ap-northeast-2 \
  --query SecretString \
  --output text
```

### backend-api-secret

```bash
aws secretsmanager put-secret-value \
  --secret-id "utterai-dev/backend-api-secret" \
  --region ap-northeast-2 \
  --secret-string '{
    "DB_PASSWORD": "<RDS 비밀번호>",
    "JWT_SECRET_KEY": "<랜덤 32자 이상>",
    "INTERNAL_CALLBACK_TOKEN": "<랜덤 32자 이상>",
    "INTERNAL_CALLBACK_HMAC_SECRET": "<랜덤 32자 이상>"
  }'
```

### ai-worker-secret

```bash
aws secretsmanager put-secret-value \
  --secret-id "utterai-dev/ai-worker-secret" \
  --region ap-northeast-2 \
  --secret-string '{
    "DATABASE_URL": "postgresql+psycopg://utterai_app:<RDS 비밀번호>@<rds_endpoint>:5432/utterai"
  }'
```

### gpu-worker-secret

```bash
aws secretsmanager put-secret-value \
  --secret-id "utterai-dev/gpu-worker-secret" \
  --region ap-northeast-2 \
  --secret-string '{
    "HF_TOKEN": "<Hugging Face 토큰>"
  }'
```

---

## 4. kubectl — EKS 클러스터 연결

```bash
aws eks update-kubeconfig \
  --region ap-northeast-2 \
  --name utterai-dev-eks

kubectl get nodes
```

---

## 5. k8s 매니페스트 배포

인프라 레포 루트에서 실행한다.
AWS 로그인 컨텍스트에서 계정 ID와 ECR 최신 이미지 태그를 자동 조회해 주입한다.

```bash
bash scripts/k8s-deploy.sh
```

배포 순서:
1. Namespaces
2. RBAC (ServiceAccount, RoleBinding)
3. External Secrets (ClusterSecretStore → ExternalSecret)
4. Workloads + HPA
5. Ingress

---

## 6. 앱 배포 (CI 자동)

**각 레포의 `dev` 브랜치에 push하면 CI가 자동으로 처리한다.**

| 레포 | 트리거 | CI 동작 |
|------|--------|---------|
| UtterAI_BE | `dev` push (app/**, Dockerfile 변경) | `dev-{sha}` 태그로 ECR push |
| UtterAI_AI | `dev` push (app/**, Dockerfile* 변경) | CPU/GPU 이미지 각각 ECR push |
| UtterAI_FE | `dev` push (src/**, public/** 등 변경) | S3 빌드 배포 + CloudFront 무효화 |

CI 완료 후 k8s 워크로드에 새 이미지 반영:

```bash
# 새 이미지 태그로 재배포
bash scripts/k8s-deploy.sh
```

> `k8s-deploy.sh`는 ECR에서 가장 최근 push된 태그를 자동 조회해 적용한다.

---

## 7. 배포 확인

```bash
# 파드 전체 상태 (모두 Running이어야 함)
kubectl get pods -A

# ExternalSecret 동기화 확인 (STATUS: SecretSynced)
kubectl get externalsecret -A

# ALB DNS 확인 (1~3분 소요)
kubectl get ingress -A

# HPA 상태
kubectl get hpa -A

# API health check
curl http://<ALB_DNS>/health
```

---

## 트러블슈팅

| 증상 | 원인 | 해결 |
|------|------|------|
| `helm_release` — cluster unreachable | EKS 토큰 만료 | `terraform apply` 재실행 |
| ExternalSecret — SecretSyncedError | Secrets Manager 값 미입력 | 3단계 값 입력 후 `kubectl annotate externalsecret <name> force-sync=$(date +%s) -n <ns>` |
| 파드 ImagePullBackOff | ECR 이미지 없음 | 해당 앱 레포 dev 브랜치에 push해 CI 실행 |
| ALB 프로비저닝 안 됨 | aws-load-balancer-controller 미기동 | `kubectl get pods -n ingress-system` 확인 |
| RDS/Redis endpoint null | 리소스 생성 실패 | `terraform apply` 재실행 후 에러 확인 |
| FE 배포 후 구 버전 노출 | CloudFront 캐시 | CI가 자동 무효화, 수동 시 `aws cloudfront create-invalidation --distribution-id <id> --paths "/*"` |
