# UtterAI Prod 환경 — 보안 현황

> 작성일: 2026-06-15 / 최종 업데이트: 2026-06-24
> **이 문서는 Prod 보안 항목의 실제 현황을 코드 기준으로 기록한다.**
> Dev 보안 문서(`security-overview`, `security-gaps`, `security-hardening`)를 기반으로
> Dev에서 허용한 것 중 Prod에서 강화해야 할 항목을 정리한다.
>
> **2026-06-16 적용 완료**: NetworkPolicy (AWS VPC CNI native), PodDisruptionBudget  
> **2026-06-22 적용 완료**: ALB ACM 인증서, ArgoCD admin bcrypt 비밀번호, PSA 레이블, SecurityContext 기본값 (allowPrivilegeEscalation/capabilities.drop/runAsNonRoot/seccompProfile), podAntiAffinity  
> **2026-06-23 적용 완료**: readOnlyRootFilesystem (전 워크로드 + /tmp emptyDir 마운트), ai-api dead reference 제거, ECR imageTagMutability IMMUTABLE
> **2026-06-24 적용 완료**: Promtail 로그 redaction, OpenTelemetry Collector trace/log attribute redaction, metric label 민감정보 금지 규칙 문서화

---

## 목차

1. [Dev vs Prod 보안 계획 비교](#1-dev-vs-prod-보안-계획-비교)
2. [Terraform — Prod 전환 시 적용 항목](#2-terraform--prod-전환-시-적용-항목)
3. [Kubernetes — Prod 전환 시 적용 항목](#3-kubernetes--prod-전환-시-적용-항목)
4. [우선순위별 TODO 목록](#4-우선순위별-todo-목록)

---

## 1. Dev vs Prod 보안 계획 비교

> **범례**: ✅ = 코드에서 확인된 실제 완료 / ⚠️ = 부분 적용 또는 불일치 / **미적용** = 코드에 없음. 2026-06-23 기준.

| 보안 영역 | Dev 현재 상태 | Prod 실제 상태 |
|----------|-------------|--------------|
| **EKS API Endpoint** | Public (0.0.0.0/0) | ⚠️ 여전히 Public (`endpoint_public_access = true`) — VPN 구축 완료 (PR #289), `false` 적용 가능 상태. `modules/eks/main.tf:70` |
| **EKS etcd KMS** | 미설정 | **미적용** — `encryption_config` 블록 없음 |
| **EKS Node SG** | Control Plane SG만 허용 (수정 완료) | ✅ 동일 + Custom Networking용 cluster↔node SG 상호 허용 규칙 추가 |
| **VPC NAT** | 1개 (공유) | 확인 필요 |
| **VPC Flow Logs** | 미설정 | **미적용** — `01-network/main.tf`에 `aws_flow_log` 없음 |
| **WAF** | 없음 | **미적용** — WebACL 미생성. `app.utterai.org` CloudFront WAF v1 규칙 선택은 [`cloudfront-waf-adr.md`](./cloudfront-waf-adr.md) 참고 |
| **RDS 종류** | Single Instance | Single Instance (Aurora 미전환 — 계획은 migration-checklist 참고) |
| **RDS deletion_protection** | false | ✅ `deletion_protection = true` |
| **RDS skip_final_snapshot** | true | ✅ `skip_final_snapshot = false` |
| **RDS KMS** | AWS 기본 키 | ✅ `storage_encrypted = true` (AWS 기본 키, CMK 미전환) |
| **Redis 리소스 타입** | `aws_elasticache_replication_group` | ✅ 동일 (`modules/redis/main.tf`) |
| **Redis TLS** | `transit_encryption_enabled = true` | ✅ 동일 |
| **Redis 노드 수** | 1 | ✅ `num_cache_nodes = 2` (default) — `automatic_failover_enabled` / `multi_az_enabled` **미설정** |
| **Redis Auth Token** | `random_password` → Secrets Manager | ✅ 동일 (`utterai-prod/redis-auth-token`) |
| **Redis tfstate 노출** | `random_password` → .tfstate 평문 | **미해소** — Terraform 1.10+ ephemeral 전환 필요 (§2-E) |
| **S3 암호화** | SSE-S3 (AES256) | ⚠️ SSE-S3 (AES256) — CMK 미전환 (`modules/s3/main.tf:50`) |
| **S3 버전 관리** | 없음 | **미적용** — 모듈에 versioning 블록 없음 |
| **S3 액세스 로깅** | 없음 | **미적용** |
| **SQS 암호화** | Managed SSE | ⚠️ Managed SSE (`sqs_managed_sse_enabled = true`) — CMK 미전환 |
| **Secrets Manager KMS** | AWS 기본 키 | AWS 기본 키 (CMK 미전환) |
| **Secrets Manager 교체** | 없음 | **미적용** |
| **ALB HTTPS** | 미설정 | ✅ HTTP→HTTPS 리다이렉트 + ACM ARN 주입 완료 (`patch-ingress.yaml`) |
| **ALB WAF 연결** | 없음 | **미적용** — `wafv2-acl-arn` annotation 없음 |
| **Pod SecurityContext** | 부분 적용 | ✅ `allowPrivilegeEscalation: false` + `capabilities.drop: ALL` + `runAsNonRoot: true` (pod 레벨) + `seccompProfile: RuntimeDefault` (pod 레벨) — 전 워크로드 적용 |
| **readOnlyRootFilesystem** | 없음 | ✅ `readOnlyRootFilesystem: true` + `/tmp` emptyDir 마운트 — 전 워크로드 적용. cpu/gpu-worker는 `HF_HOME=/tmp/huggingface` 명시 |
| **PSA 레이블** | 없음 | ✅ `utterai-prod-api`: enforce restricted / `utterai-ai-api|cpu|gpu`, `utterai-batch`: enforce baseline (`namespace.yaml`) |
| **Kyverno** | 없음 | **미설치** |
| **NetworkPolicy** | 없음 | ✅ VPC CNI native NetworkPolicy 활성화 + 네임스페이스별 deny-all + 명시적 허용 정책 (`network-policy.yaml`) |
| **ClusterSecretStore** | 클러스터 전체 공유 | **미분리** — base ExternalSecret 전체가 `ClusterSecretStore` 사용 중 |
| **ECR Immutability** | MUTABLE | ✅ `image_tag_mutability = "IMMUTABLE"` — `modules/ecr/variables.tf` default 변경 |
| **PodDisruptionBudget** | 없음 | ✅ backend blue/green + ai-api `minAvailable: 1` (`pdb.yaml`) |
| **podAntiAffinity** | 없음 | ✅ backend blue/green `requiredDuringSchedulingIgnoredDuringExecution` 적용 (`deployment-blue/green.yaml`, PR #262) |
| **ArgoCD 인증** | Helm 기본 admin | ✅ bcrypt 비밀번호 주입 완료 |
| **배포 방식** | 수동 스크립트 | ✅ Kustomize + ArgoCD GitOps |
| **Observability 민감정보 마스킹** | 없음 | ✅ Promtail 로그 redaction + OTel Collector trace/log attribute 삭제 적용. Metric label 민감정보 금지 규칙 문서화 |
| **Cognito MFA** | 없음 | **미적용** |
| **CloudWatch 알람** | 없음 | **미적용** |
| **VPC Endpoint** | S3/SQS/SM/ECR | ✅ 기존 4종 유지 (STS/KMS Interface 미추가) |

---

## 2. Terraform — Prod 전환 시 적용 항목

### 2-A. EKS etcd KMS 봉투 암호화

etcd에 저장되는 Kubernetes Secret을 CMK로 봉투 암호화. AWS 계정 레벨 침해 시에도 Secret 보호.

```hcl
# terraform/environments/prod/02-eks/main.tf
resource "aws_eks_cluster" "this" {
  ...
  encryption_config {
    resources = ["secrets"]
    provider {
      key_arn = module.kms_eks.key_arn   # alias/prod-eks-secrets-kms-key
    }
  }
  vpc_config {
    endpoint_private_access = true
    endpoint_public_access  = false   # Dev는 true
  }
}
```

> 기존 클러스터에 적용 시 Secret 전체 재암호화 발생. 신규 클러스터 생성 시 처음부터 포함할 것.

---

### 2-B. VPC Flow Logs

네트워크 이상 트래픽 감지 및 사후 분석용. Dev는 미설정.

```hcl
resource "aws_flow_log" "this" {
  iam_role_arn    = aws_iam_role.flow_logs.arn
  log_destination = aws_cloudwatch_log_group.flow_logs.arn
  traffic_type    = "ALL"
  vpc_id          = module.vpc.vpc_id
}

resource "aws_cloudwatch_log_group" "flow_logs" {
  name              = "/aws/vpc/utterai-prod-flow-logs"
  retention_in_days = 30
  kms_key_id        = module.kms_logs.key_arn
}
```

---

### 2-C. WAF 연결

`migration-checklist.md §1-B`에 ALB WAF 코드가 있다.
CloudFront WAF는 `us-east-1`에 `scope = "CLOUDFRONT"`로 별도 WebACL 생성 필요.
`app.utterai.org` CloudFront만 보호하는 v1 결정은 [`cloudfront-waf-adr.md`](./cloudfront-waf-adr.md)에 정리한다.

```hcl
# CloudFront WAF (us-east-1 provider 필요)
resource "aws_wafv2_web_acl" "cloudfront" {
  provider = aws.us_east_1
  scope    = "CLOUDFRONT"
  ...
}
```

---

### 2-D. Redis — 현재 상태 확인 (이미 완료)

`terraform/modules/redis/main.tf`는 이미 `aws_elasticache_replication_group`을 사용하며 TLS와 at-rest 암호화가 적용되어 있다.

```hcl
# 현재 redis/main.tf — 이미 적용됨
resource "aws_elasticache_replication_group" "this" {
  ...
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  auth_token                 = random_password.redis_auth.result
}
```

**남은 문제**: `random_password` 결과가 `.tfstate`에 평문으로 저장된다. → §2-E에서 해결.

Prod에서 추가로 필요한 항목:
- `num_cache_clusters = 2` (Primary + Replica, 현재 dev는 `num_cache_nodes = 1`)
- `automatic_failover_enabled = true`, `multi_az_enabled = true`
- `kms_key_id` — Prod CMK로 at-rest 암호화 키 교체 (현재 AWS 관리형 키)

백엔드 Pod 환경변수 (이미 base ConfigMap에 있어야 함):
```env
REDIS_TLS_ENABLED=true
REDIS_AUTH_TOKEN=${from_secrets_manager}
```

---

### 2-E. Terraform State Redis 토큰 노출 해소

현재 `random_password` 결과가 S3 tfstate에 **평문** 저장된다. Terraform 1.10+에서 ephemeral 리소스로 전환.

```hcl
# 개선 방향 (Terraform 1.10+)
ephemeral "aws_secretsmanager_secret_version" "redis_auth" {
  secret_id = aws_secretsmanager_secret.redis_auth.id
}
resource "aws_elasticache_replication_group" "this" {
  auth_token = ephemeral.aws_secretsmanager_secret_version.redis_auth.secret_string
  # state에 저장되지 않음
}
```

상세: `prod/README.md §23`

---

### 2-F. ECR 이미지 태그 Immutability

같은 태그로 악성 이미지를 덮어쓰는 것을 차단.

```hcl
resource "aws_ecr_repository" "this" {
  name                 = var.name
  image_tag_mutability = var.env == "prod" ? "IMMUTABLE" : "MUTABLE"
}
```

---

## 3. Kubernetes — Prod 전환 시 적용 항목

### 3-A. ALB ACM 인증서 ✅ / WAF 연결 미적용

`k8s/apps/backend/overlays/prod/patch-ingress.yaml` 현재 상태:

```yaml
annotations:
  alb.ingress.kubernetes.io/certificate-arn: arn:aws:acm:ap-northeast-2:032886669461:certificate/ee72a793-6b79-4560-8f5e-7faf88aad699
  alb.ingress.kubernetes.io/listen-ports: '[{"HTTP":80,"HTTPS":443}]'
  alb.ingress.kubernetes.io/ssl-redirect: '443'
  # wafv2-acl-arn: 미적용 — WAF WebACL 미생성 상태
```

WAF 연결을 위해서는 `04-addons/main.tf`에서 `aws_wafv2_web_acl` 생성 후 ARN을 annotation에 주입해야 한다.

---

### 3-B. SecurityContext 현황

**적용 완료** (`patch-deployment.yaml` 코드 확인 기준):

| 항목 | backend (blue/green) | cpu-worker | ml-gpu-worker | batch-worker |
|------|---------------------|------------|---------------|--------------|
| `runAsNonRoot: true` | ✅ pod 레벨 | ✅ pod 레벨 | ✅ pod 레벨 | ✅ pod 레벨 |
| `seccompProfile: RuntimeDefault` | ✅ pod 레벨 | ✅ pod 레벨 | ✅ pod 레벨 | ✅ pod 레벨 |
| `allowPrivilegeEscalation: false` | ✅ container 레벨 | ✅ container 레벨 | ✅ container 레벨 | ✅ container 레벨 |
| `capabilities.drop: ALL` | ✅ container 레벨 | ✅ container 레벨 | ✅ container 레벨 | ✅ container 레벨 |
| `readOnlyRootFilesystem: true` | ✅ container 레벨 | ✅ container 레벨 | ✅ container 레벨 | ✅ container 레벨 |

**적용 완료 (2026-06-23)** — 전 워크로드에 `readOnlyRootFilesystem: true` + `/tmp` emptyDir 마운트 적용. cpu/gpu-worker는 `HF_HOME=/tmp/huggingface` 환경변수 명시.

> GPU worker에서 CUDA 커널 캐시(`~/.nv`)를 `/tmp` 외 경로에 쓰는 경우 `Read-only file system` 에러가 발생할 수 있다. 발생 시 해당 경로에 별도 emptyDir를 추가할 것.

---

### 3-C. ClusterSecretStore → Per-Namespace SecretStore

**현재 상태**: `k8s/apps/backend/base/external-secret.yaml` 및 `ai-worker/base/*-external-secret.yaml` 전체가 `ClusterSecretStore`를 사용 중. 악의적 사용자가 새 네임스페이스에 `ExternalSecret`을 만들면 `utterai-prod/*` 전체를 꺼낼 수 있다.

**prod 실제 네임스페이스 구조** (단일 ai-worker 네임스페이스가 아닌 4개 분리):
- `utterai-prod-api` — backend API
- `utterai-ai-api` — AI REST API
- `utterai-ai-cpu` — CPU worker
- `utterai-ai-gpu` — ML GPU worker
- `utterai-batch` — RAG ingest batch worker

적용 방법: 네임스페이스별 `SecretStore` 생성 후 각 `ExternalSecret`의 `secretStoreRef.kind`를 `ClusterSecretStore` → `SecretStore`로 변경.

```yaml
# 예시: k8s/apps/backend/overlays/prod/secret-store.yaml
apiVersion: external-secrets.io/v1beta1
kind: SecretStore
metadata:
  name: aws-secrets-manager
  namespace: utterai-prod-api
spec:
  provider:
    aws:
      service: SecretsManager
      region: ap-northeast-2
      auth:
        jwt:
          serviceAccountRef:
            name: utterai-api-sa       # 이 SA의 IRSA 범위 내 시크릿만 접근 가능
```

---

### 3-D. NetworkPolicy — AWS VPC CNI native ✅ 적용 완료 (2026-06-16)

Cilium 미사용 결정에 따라 AWS VPC CNI 내장 NetworkPolicy로 대체.

**Terraform**: `terraform/modules/eks/main.tf` vpc-cni addon에 `ENABLE_NETWORK_POLICY = "true"` 추가.

**k8s 정책 구조** (네임스페이스별 동일 패턴):

| 정책 | 대상 | 내용 |
|------|------|------|
| `default-deny-all` | 전체 Pod | ingress + egress 전면 차단 |
| `allow-ingress-alb` | backend api | ALB → 8080 허용 |
| `allow-ingress-from-backend` | ai-api | utterai-prod-api ns → 8080 허용 |
| `allow-ingress-prometheus` | api, ai-api | monitoring ns → 8080 허용 (scrape) |
| `allow-egress-dns` | 전체 Pod | 53/UDP, 53/TCP 허용 |
| `allow-egress-aws` | 전체 Pod | 443 (SQS/S3/Bedrock/SM VPC Endpoint) |
| `allow-egress-aws` | backend | + 5432 (RDS), 6379 (Redis) |

**적용 파일**:
- `k8s/apps/backend/overlays/prod/network-policy.yaml`
- `k8s/apps/ai-worker/overlays/prod/network-policy.yaml`

---

### 3-E. PodDisruptionBudget ✅ 적용 완료 (2026-06-16)

유지보수(노드 drain, 업그레이드) 중 최소 가용 Pod 수 보장.

| 대상 | `minAvailable` | 이유 |
|------|---------------|------|
| `utterai-api-blue` | 1 | prod replicas: 2 |
| `utterai-api-green` | 1 | prod replicas: 2 |
| cpu-worker, ml-gpu-worker, batch-worker | 미적용 | KEDA 0-scale 허용 필요 |

**적용 파일**:
- `k8s/apps/backend/overlays/prod/pdb.yaml`
- `k8s/apps/ai-worker/overlays/prod/pdb.yaml`

---

### 3-F. ArgoCD Admin 자격증명

Helm 기본 배포 시 `admin` 초기 비밀번호가 Pod 이름 기반으로 자동 생성되고 영구 유지될 수 있다.

**단기**: Secrets Manager에서 bcrypt 해시된 비밀번호 주입

```hcl
resource "helm_release" "argocd" {
  values = [yamlencode({
    configs = {
      secret = {
        argocdServerAdminPassword = var.argocd_admin_password_bcrypt
      }
    }
  })]
}
```

**장기**: Cognito OIDC를 ArgoCD SSO로 연결해 `admin` 계정 비활성화

---

### 3-G. Namespace PSA 레이블 ✅ 적용 완료

`k8s/apps/backend/overlays/prod/namespace.yaml`과 `k8s/apps/ai-worker/overlays/prod/namespace.yaml`에 적용 완료.

```yaml
# utterai-prod-api — enforce: restricted
labels:
  pod-security.kubernetes.io/enforce: restricted
  pod-security.kubernetes.io/warn: restricted
  pod-security.kubernetes.io/audit: restricted

# utterai-ai-cpu, utterai-ai-gpu, utterai-batch — enforce: baseline
# GPU NVIDIA Device Plugin 특성상 restricted 시 스케줄링 실패 가능 → 전체 baseline
labels:
  pod-security.kubernetes.io/enforce: baseline
  pod-security.kubernetes.io/warn: restricted
  pod-security.kubernetes.io/audit: restricted
```

> `utterai-prod-api`가 `enforce: restricted`이므로 `seccompProfile`이 없는 Pod는 배포 자체가 거부된다. 현재 patch-deployment.yaml에 pod 레벨 `seccompProfile: RuntimeDefault`가 적용되어 있어 통과.  
> Dev 네임스페이스에는 PSA 레이블 없음 — 의도적 미적용.

---

## 4. 보안 TODO — 적용 현황 및 우선순위

### 적용 완료

| 항목 | 확인일 | 파일 |
|------|--------|------|
| NetworkPolicy — 네임스페이스별 deny-all + 명시적 허용 정책 | 2026-06-16 | `terraform/modules/eks/main.tf`, `k8s/apps/*/overlays/prod/network-policy.yaml` |
| PodDisruptionBudget — backend blue/green `minAvailable: 1` | 2026-06-16 | `k8s/apps/*/overlays/prod/pdb.yaml` |
| ALB ACM 인증서 ARN 주입 + HTTP→HTTPS 리다이렉트 | 2026-06-22 | `k8s/apps/backend/overlays/prod/patch-ingress.yaml` |
| ArgoCD admin bcrypt 비밀번호 주입 | 2026-06-22 | `terraform/modules/eks-addons/main.tf` |
| PSA 레이블 — `utterai-prod-api`: restricted / `utterai-ai-*`, `utterai-batch`: baseline | 2026-06-22 | `k8s/apps/*/overlays/prod/namespace.yaml` |
| SecurityContext — `allowPrivilegeEscalation: false`, `capabilities.drop: ALL`, `runAsNonRoot: true`, `seccompProfile: RuntimeDefault` 전 워크로드 | 2026-06-22 | `k8s/apps/*/overlays/prod/patch-deployment.yaml` |
| podAntiAffinity — backend blue/green `requiredDuringSchedulingIgnoredDuringExecution` | 2026-06-22 | `k8s/apps/backend/overlays/prod/deployment-blue/green.yaml` |
| ai-api 게이트웨이 dead reference 제거 — ConfigMap, Namespace, AI_SERVICE_BASE_URL, allow-egress-ai-api NetworkPolicy | 2026-06-23 | `k8s/apps/ai-worker/base/configmap.yaml`, `k8s/apps/ai-worker/overlays/prod/namespace.yaml`, `k8s/apps/backend/base/configmap.yaml`, `k8s/apps/backend/overlays/prod/network-policy.yaml` |
| `readOnlyRootFilesystem: true` — 전 워크로드 container 레벨 적용. `/tmp` emptyDir 마운트. cpu/gpu-worker `HF_HOME=/tmp/huggingface` 명시 | 2026-06-23 | `k8s/apps/backend/overlays/prod/patch-deployment.yaml`, `k8s/apps/ai-worker/overlays/prod/patch-deployment.yaml` |
| ECR `image_tag_mutability = "IMMUTABLE"` — 모듈 variable 추가 (default IMMUTABLE). 기존 호출부 수정 불필요 | 2026-06-23 | `terraform/modules/ecr/variables.tf`, `terraform/modules/ecr/main.tf` |
| Observability 민감정보 redaction — Promtail 로그 redaction + OTel Collector trace/log attribute 삭제 | 2026-06-24 | `terraform/modules/eks-addons/main.tf`, `k8s/platform/observability/base/otel-collector.yaml` |

---

### 미적용 항목 — 리스크/난이도별 우선순위

#### 즉시 적용 가능 (운영 영향 없음, ArgoCD sync 또는 terraform apply로 반영)

| 순위 | 항목 | 작업 위치 | 비고 |
|------|------|----------|------|
| 1 | S3 버전 관리 (`raw-audio`, `reports`) + 액세스 로깅 | Terraform — `modules/s3/main.tf` | 기존 데이터 영향 없음, 스토리지 비용 소폭 증가 |

#### 중간 난이도 (신규 리소스 생성 또는 다수 파일 수정, 기존 서비스 영향 없음)

| 순위 | 항목 | 작업 위치 | 비고 |
|------|------|----------|------|
| 4 | WAF 연결 — ALB regional WebACL + CloudFront WebACL | Terraform — `04-addons/main.tf` 신규 + `patch-ingress.yaml` ARN 주입 | CloudFront WAF는 `us-east-1` provider 필요. AWS Managed Rule Group으로 시작 권장 |
| 5 | Per-Namespace SecretStore 분리 | K8s — 네임스페이스별 `SecretStore` yaml 추가 + ExternalSecret 5개 `secretStoreRef` 수정 | 현재 `ClusterSecretStore` 1개가 클러스터 전체 커버 중. 대상: `utterai-prod-api`, `utterai-ai-gpu`, `utterai-ai-cpu`, `utterai-batch` |
| 6 | Redis `automatic_failover_enabled = true` + `multi_az_enabled = true` | Terraform — `modules/redis/main.tf` 2줄 추가 | `num_cache_clusters=2`이나 failover 미설정 상태. `terraform apply` 중 ElastiCache 수분 downtime 가능 |
| 7 | S3/SQS/Secrets Manager KMS CMK 전환 | Terraform — `modules/s3`, `modules/sqs`, `modules/secrets` | 현재 SSE-S3/Managed SSE/AWS 기본 키 사용 중. 기존 버킷/큐는 설정 변경만으로 가능 (데이터 이동 없음) |

#### 신중하게 계획 필요 (운영 영향 또는 불가역적 변경)

| 순위 | 항목 | 작업 위치 | 주의사항 |
|------|------|----------|---------|
| 8 | EKS `endpoint_public_access = false` | Terraform — `modules/eks/main.tf:70` | VPN 구축 완료 (PR #289). GitHub Actions는 ArgoCD GitOps 구조라 EKS API 직접 호출 없음 — 영향 없음. 팀원 전체 `.ovpn` 배포 및 `kubectl get nodes` 확인 후 즉시 적용 가능. 상세: `eks-private-endpoint.md §8` |
| 9 | VPC Flow Logs 활성화 | Terraform — `01-network/main.tf` 신규 | CloudWatch Logs 비용 발생 (트래픽량에 따라 월 수만~수십만원). 보존 기간 30일 권장 |
| 10 | EKS etcd KMS 봉투 암호화 | Terraform — `modules/eks/main.tf` `encryption_config` 블록 추가 | 기존 클러스터 적용 시 클러스터 내 모든 Secret 전체 재암호화 발생. 신규 클러스터 생성 시 처음부터 포함하는 것이 안전 |
| 11 | Redis tfstate 토큰 노출 해소 | Terraform — `modules/redis/main.tf` | Terraform 1.10+ `ephemeral` 리소스 필요. 현재 `random_password` 결과가 S3 tfstate에 평문 저장 중 |

---

## 관련 문서

- [Dev 보안 전체 현황](../dev/security/overview.md)
- [Dev 보안 미비점 상세](../dev/security/gaps.md)
- [Dev 보안 하드닝 이력](../dev/security/hardening.md)
- [Prod 인프라 가이드](./README.md) — §21(PSS+Kyverno 계획), §22(Cilium 계획), §23(Secrets 계획)
- [Prod 전환 체크리스트](./migration-checklist.md)
- [EKS Private Endpoint 전환 가이드](./eks-private-endpoint.md) — 왜 하는지, 워크플로우 변화, SSM/VPN 접근 수단 구성
