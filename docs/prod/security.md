# UtterAI Prod 환경 — 보안 적용 계획

> 작성일: 2026-06-15 / 최종 업데이트: 2026-06-16
> **이 문서는 Prod 전환 시 적용해야 할 보안 항목 계획이다.**
> Dev 보안 문서(`security-overview`, `security-gaps`, `security-hardening`)를 기반으로
> Dev에서 허용한 것 중 Prod에서 강화해야 할 항목을 정리한다.
>
> **2026-06-16 적용 완료**: NetworkPolicy (AWS VPC CNI native), PodDisruptionBudget  
> **2026-06-22 적용 확인**: ALB ACM 인증서, ArgoCD admin bcrypt 비밀번호, PSA 레이블, SecurityContext 기본값 (allowPrivilegeEscalation/capabilities.drop)

---

## 목차

1. [Dev vs Prod 보안 계획 비교](#1-dev-vs-prod-보안-계획-비교)
2. [Terraform — Prod 전환 시 적용 항목](#2-terraform--prod-전환-시-적용-항목)
3. [Kubernetes — Prod 전환 시 적용 항목](#3-kubernetes--prod-전환-시-적용-항목)
4. [우선순위별 TODO 목록](#4-우선순위별-todo-목록)

---

## 1. Dev vs Prod 보안 계획 비교

> **범례**: Dev 현재 상태 vs Prod에서 적용 예정인 것. Prod 열의 내용은 **모두 미적용 / 적용 예정**.

| 보안 영역 | Dev 현재 상태 | Prod 적용 예정 |
|----------|-------------|--------------|
| **EKS API Endpoint** | Public (0.0.0.0/0) | Private only (`endpoint_public_access = false`) |
| **EKS etcd KMS** | 미설정 | CMK 봉투 암호화 |
| **EKS Node SG** | Control Plane SG만 허용 (수정 완료) | 동일 |
| **VPC NAT** | 1개 (공유) | 3개 (AZ별) |
| **VPC Flow Logs** | 미설정 | CloudWatch Logs로 전송 |
| **WAF** | 없음 | CloudFront + ALB 연결 |
| **RDS 종류** | Single Instance | Aurora Multi-AZ |
| **RDS deletion_protection** | false | true |
| **RDS skip_final_snapshot** | true | false |
| **RDS KMS** | AWS 기본 키 | CMK (`prod-aurora-kms-key`) |
| **Redis 리소스 타입** | `aws_elasticache_replication_group` (TLS 적용 완료) | 동일 모듈 사용 (변경 없음) |
| **Redis TLS** | `transit_encryption_enabled = true` (적용 완료) | 동일 |
| **Redis Auth Token** | `random_password` → Secrets Manager 저장 중 | 동일 (개선 예정: §2-E) |
| **Redis tfstate 노출** | `random_password` 결과 → .tfstate 평문 저장 | Terraform 1.10+ ephemeral 전환 |
| **S3 암호화** | SSE-S3 (AES256) | SSE-KMS (CMK) |
| **S3 버전 관리** | 없음 | raw-audio, reports 활성화 |
| **S3 액세스 로깅** | 없음 | 활성화 |
| **SQS 암호화** | Managed SSE | CMK |
| **Secrets Manager KMS** | AWS 기본 키 | CMK (`prod-secrets-kms-key`) |
| **Secrets Manager 교체** | 없음 | DB 비밀번호 90일 자동 교체 |
| **ALB HTTPS** | dev overlay에서 설정 예정 | ✅ HTTP→HTTPS 리다이렉트 + ACM ARN 주입 완료 (2026-06-22 확인) |
| **ALB WAF 연결** | 없음 | `wafv2-acl-arn` annotation — **미적용** |
| **Pod SecurityContext** | Prod overlay에 부분 적용 | ✅ `allowPrivilegeEscalation: false`, `capabilities.drop: ALL` 전 워크로드 적용 / `runAsNonRoot`, `readOnlyRootFilesystem`, `seccompProfile` **미적용** (§3-B) |
| **readOnlyRootFilesystem** | 없음 | **미적용** — Prod overlay에 추가 필요 |
| **seccompProfile** | 없음 | **미적용** — `enforce: restricted` 네임스페이스에서 신규 배포 시 rejection 위험 |
| **PSA 레이블** | 없음 | ✅ 적용 완료 — `utterai-prod-api`: enforce restricted / `utterai-ai-*`: enforce baseline (2026-06-22 확인) |
| **Kyverno** | 없음 | 미설치 — ClusterPolicy 4종 설치 예정 |
| **NetworkPolicy** | 없음 | ✅ AWS VPC CNI native NetworkPolicy 활성화 + 네임스페이스별 deny-all + 명시적 허용 정책 적용 완료 (2026-06-16) |
| **ClusterSecretStore** | 클러스터 전체 공유 | **미적용** — 여전히 ClusterSecretStore 단일 사용 중. 네임스페이스별 SecretStore 분리 필요 |
| **이미지 태그** | mutable tag 허용 | **미적용** — git SHA 고정 + Kyverno latest 차단 예정 |
| **ECR Immutability** | MUTABLE | **미적용** — 3개 레포 모두 MUTABLE 상태 |
| **PodDisruptionBudget** | 없음 | ✅ backend blue/green `minAvailable: 1`, cpu-worker `minAvailable: 1` 적용 완료 (2026-06-16) |
| **podAntiAffinity** | 없음 | **미적용** — backend api 다른 노드 분산 예정 |
| **ArgoCD 인증** | Helm 기본 admin (초기값) | ✅ bcrypt 비밀번호 주입 완료 (2026-06-22 확인) |
| **배포 방식** | envsubst + 수동 스크립트 | Kustomize + ArgoCD GitOps |
| **Cognito MFA** | 없음 | TOTP 지원 + 고급 보안 |
| **CloudWatch 알람** | 없음 | 10종 알람 + Discord 연결 |
| **VPC Endpoint 추가** | S3/SQS/SM/ECR | + STS, KMS Interface |

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

### 3-A. ALB ACM 인증서 + WAF 연결

현재 `k8s/apps/backend/overlays/prod/patch-ingress.yaml`의 `certificate-arn`이 `TODO` 상태.

```yaml
annotations:
  alb.ingress.kubernetes.io/listen-ports: '[{"HTTP":80},{"HTTPS":443}]'
  alb.ingress.kubernetes.io/ssl-redirect: "443"
  alb.ingress.kubernetes.io/certificate-arn: arn:aws:acm:ap-northeast-2:<PROD_ACCOUNT_ID>:certificate/<CERT_ID>
  alb.ingress.kubernetes.io/wafv2-acl-arn: arn:aws:wafv2:ap-northeast-2:<PROD_ACCOUNT_ID>:regional/webacl/<NAME>/<ID>
```

---

### 3-B. readOnlyRootFilesystem + seccompProfile + ml-gpu-worker securityContext 누락

현재 prod overlay 상태:
- `backend` (blue/green): `runAsNonRoot`, `allowPrivilegeEscalation: false`, `capabilities.drop` 있음 / `readOnlyRootFilesystem`, `seccompProfile` **없음**
- `ai-api`, `cpu-worker`: 동일하게 `readOnlyRootFilesystem`, `seccompProfile` **없음**
- **`ml-gpu-worker`: securityContext 자체가 patch에 없음** (`k8s/apps/ai-worker/overlays/prod/patch-deployment.yaml`에서 `replicas: 1`만 정의)

PSS `restricted` 레이블 적용 시 `seccompProfile`은 자동 요건이 되므로 반드시 추가해야 배포가 통과된다.

```yaml
# k8s/apps/backend/overlays/prod/patch-deployment.yaml
containers:
  - name: api
    securityContext:
      allowPrivilegeEscalation: false
      capabilities:
        drop: ["ALL"]
      readOnlyRootFilesystem: true
      runAsNonRoot: true
      runAsUser: 1000
      seccompProfile:
        type: RuntimeDefault
```

CPU/GPU Worker는 `HF_HOME: /tmp/huggingface` 사용으로 인해 `/tmp`를 emptyDir로 마운트 필요:

```yaml
volumes:
  - name: hf-cache
    emptyDir: {}
volumeMounts:
  - name: hf-cache
    mountPath: /tmp/huggingface
```

---

### 3-C. ClusterSecretStore → Per-Namespace SecretStore

Dev는 `ClusterSecretStore` 하나가 클러스터 전체를 커버한다. 악의적 사용자가 새 네임스페이스에 `ExternalSecret`을 만들면 `utterai-prod/*` 전체를 꺼낼 수 있다.

```yaml
# k8s/apps/backend/overlays/prod/secret-store.yaml
apiVersion: external-secrets.io/v1beta1
kind: SecretStore
metadata:
  name: aws-secrets-manager
  namespace: utterai-prod-api          # 실제 prod 네임스페이스명
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

생성 대상 네임스페이스:
- `utterai-prod-api` (backend)
- `utterai-prod-ai-worker` (ai-api, cpu-worker, ml-gpu-worker, batch-worker 통합 — prod overlay 단일 네임스페이스)

각 `ExternalSecret`의 `secretStoreRef.kind`를 `ClusterSecretStore` → `SecretStore`로 변경.

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
| `allow-egress-ai-api` | backend api | utterai-prod-ai-worker ns → 8080 허용 |

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
| `utterai-ai-api` | 1 | prod replicas: 2 |
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

### 3-G. Namespace PSA 레이블 — k8s prod overlay에 적용 완료

`k8s/apps/backend/overlays/prod/namespace.yaml`과 `k8s/apps/ai-worker/overlays/prod/namespace.yaml`에 이미 적용되어 있다.

```yaml
# k8s/apps/backend/overlays/prod/namespace.yaml (utterai-prod-api) — 적용 완료
labels:
  pod-security.kubernetes.io/enforce: restricted
  pod-security.kubernetes.io/warn: restricted
  pod-security.kubernetes.io/audit: restricted

# k8s/apps/ai-worker/overlays/prod/namespace.yaml (utterai-prod-ai-worker) — 적용 완료
# GPU NVIDIA Device Plugin 특성상 restricted 적용 시 스케줄링 실패 가능 → baseline
labels:
  pod-security.kubernetes.io/enforce: baseline
  pod-security.kubernetes.io/warn: restricted
  pod-security.kubernetes.io/audit: restricted
```

> `k8s-legacy/namespaces/namespaces.yaml` (Dev base 네임스페이스)에는 PSA 레이블 없음 — Dev는 의도적으로 미적용.

---

## 4. 우선순위별 TODO 목록

### 적용 완료

| 항목 | 확인일 | 파일 |
|------|--------|------|
| NetworkPolicy — 네임스페이스별 deny-all + 명시적 허용 정책 | 2026-06-16 | `terraform/modules/eks/main.tf`, `k8s/apps/*/overlays/prod/network-policy.yaml` |
| PodDisruptionBudget — backend blue/green + cpu-worker `minAvailable: 1` | 2026-06-16 | `k8s/apps/*/overlays/prod/pdb.yaml` |
| ALB ACM 인증서 ARN 주입 | 2026-06-22 | `k8s/apps/backend/overlays/prod/patch-ingress.yaml` |
| ArgoCD admin bcrypt 비밀번호 주입 | 2026-06-22 | `terraform/modules/eks-addons/main.tf` |
| PSA 레이블 — `utterai-prod-api`: restricted / `utterai-ai-*`: baseline | 2026-06-22 | `k8s/apps/*/overlays/prod/namespace.yaml` |
| SecurityContext 기본값 — `allowPrivilegeEscalation: false`, `capabilities.drop: ALL` 전 워크로드 | 2026-06-22 | `k8s/apps/*/overlays/prod/patch-deployment.yaml` |

### K8s 미적용 — 우선순위 순

| # | 항목 | 비고 |
|---|------|------|
| 1 | `seccompProfile: RuntimeDefault` 추가 | `enforce: restricted` 네임스페이스에서 신규 배포 시 rejection 위험 |
| 2 | `runAsNonRoot: true` container-level 명시 | 현재 pod-level 미확인, 명시적 선언 필요 |
| 3 | WAF 연결 (ALB + CloudFront) | WAF WebACL 미생성 상태 |
| 4 | `readOnlyRootFilesystem: true` + `/tmp` emptyDir | cpu/gpu-worker는 HF_HOME `/tmp` 마운트 병행 필요 |
| 5 | ECR imageTagMutability IMMUTABLE | 3개 레포 모두 MUTABLE |
| 6 | Per-Namespace SecretStore 분리 | 현재 ClusterSecretStore 단일 사용 중 |
| 7 | podAntiAffinity — backend api 다중 노드 분산 | — |

### Terraform 미적용 — 우선순위 순 (K8s 외)

| # | 항목 | 파일 |
|---|------|------|
| 1 | EKS etcd KMS 봉투 암호화 | `terraform/environments/prod/02-eks/main.tf` (신규) |
| 2 | VPC Flow Logs 활성화 | `terraform/environments/prod/01-network/` (신규) |
| 3 | Redis tfstate 토큰 노출 해소 (Terraform 1.10+) | `terraform/modules/redis/main.tf` |
| 4 | Redis Prod: num_cache_nodes=2, multi-AZ, Prod CMK | `terraform/environments/prod/03-services/main.tf` |

---

## 관련 문서

- [Dev 보안 전체 현황](../dev/security/overview.md)
- [Dev 보안 미비점 상세](../dev/security/gaps.md)
- [Dev 보안 하드닝 이력](../dev/security/hardening.md)
- [Prod 인프라 가이드](./README.md) — §21(PSS+Kyverno 계획), §22(Cilium 계획), §23(Secrets 계획)
- [Prod 전환 체크리스트](./migration-checklist.md)
