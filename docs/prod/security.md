# UtterAI Prod 환경 — 보안 체크리스트

> 작성일: 2026-06-15
> 기준: `docs/dev/security-overview.md`, `docs/dev/security-gaps.md`, `docs/dev/security-hardening.md`
> Dev에서 허용한 항목 중 Prod에서 반드시 강화해야 할 것들을 중심으로 정리한다.

---

## 목차

1. [Dev vs Prod 보안 비교 전체 요약](#1-dev-vs-prod-보안-비교-전체-요약)
2. [Terraform — Prod 필수 변경](#2-terraform--prod-필수-변경)
3. [Kubernetes — Prod 필수 변경](#3-kubernetes--prod-필수-변경)
4. [미구현 → Prod 배포 전 필수 완료 항목](#4-미구현--prod-배포-전-필수-완료-항목)
5. [이미 Prod 문서에 포함된 항목](#5-이미-prod-문서에-포함된-항목)

---

## 1. Dev vs Prod 보안 비교 전체 요약

| 보안 영역 | Dev | Prod | 상태 |
|----------|-----|------|------|
| **EKS API Endpoint** | Public (0.0.0.0/0) | **Private only** | ✅ prod/README §4.1 |
| **EKS etcd KMS** | 미설정 | CMK 봉투 암호화 | ⚠️ [§2-A](#2-a-eks-etcd-kms-봉투-암호화) |
| **EKS Node SG** | 이미 수정됨 (Control Plane SG만 허용) | 동일 | ✅ |
| **VPC NAT** | 1개 (공유) | 3개 (AZ별) | ✅ prod/README §3 |
| **VPC Flow Logs** | 미설정 | 활성화 | ⚠️ [§2-B](#2-b-vpc-flow-logs) |
| **WAF** | 없음 | CloudFront + ALB 연결 | ⚠️ [§2-C](#2-c-waf-연결-확인) |
| **RDS 종류** | Single Instance | Aurora Multi-AZ | ✅ prod/README §5 |
| **RDS deletion_protection** | false | **true** | ✅ prod-migration-checklist §2-B |
| **RDS skip_final_snapshot** | true | **false** | ✅ prod-migration-checklist §2-B |
| **RDS KMS** | AWS 기본 키 | CMK (`prod-aurora-kms-key`) | ✅ prod/README §11 |
| **Redis 종류** | `aws_elasticache_cluster` (TLS 미지원) | `aws_elasticache_replication_group` | ⚠️ [§2-D](#2-d-redis-암호화--tls) |
| **Redis TLS** | 미설정 | `transit_encryption_enabled = true` | ⚠️ |
| **Redis Auth Token** | 없음 | Secrets Manager 관리 | ⚠️ |
| **Redis state 노출** | random_password → .tfstate 평문 | Ephemeral resource로 전환 | ⚠️ [§2-E](#2-e-terraform-state-redis-토큰-노출) |
| **S3 암호화** | SSE-S3 (AES256) | **SSE-KMS (CMK)** | ✅ prod-migration-checklist §2-D |
| **S3 버전 관리** | 없음 | raw-audio, reports 활성화 | ✅ prod-migration-checklist §2-D |
| **S3 액세스 로깅** | 없음 | 활성화 | ✅ prod/README §7.2 |
| **SQS 암호화** | Managed SSE | **CMK** | ✅ prod-migration-checklist §2-E |
| **Secrets Manager KMS** | AWS 기본 키 | CMK (`prod-secrets-kms-key`) | ✅ prod-migration-checklist §2-F |
| **Secrets Manager 교체** | 없음 | DB 비밀번호 90일 자동 교체 | ✅ prod/README §10.2 |
| **ALB HTTPS** | HTTP 80만 (dev) | HTTP→HTTPS 리다이렉트 + ACM | ⚠️ [§3-A](#3-a-alb-acm-인증서-적용) |
| **ALB WAF 연결** | 없음 | `wafv2-acl-arn` annotation 추가 | ⚠️ |
| **Pod SecurityContext** | Dev base 미적용 | Prod overlay 적용 완료 | ✅ security-hardening §3 |
| **Pod seccompProfile** | 없음 | PSS restricted 강제 | ✅ prod/README §21.1 |
| **Pod readOnlyRootFilesystem** | 없음 | Prod overlay 추가 필요 | ⚠️ [§3-B](#3-b-readonlyrootfilesystem) |
| **PSA 레이블** | 없음 | enforce 적용 | ✅ prod/README §21.1 |
| **Kyverno** | 없음 | 설치 + ClusterPolicy 4종 | ✅ prod/README §21.2~21.6 |
| **NetworkPolicy** | 없음 | Cilium + 기본 deny | ✅ prod/README §22 |
| **ClusterSecretStore** | 클러스터 전체 공유 | **네임스페이스별 SecretStore** | ⚠️ [§3-C](#3-c-clustersecretstore-→-per-namespace-secretstore) |
| **이미지 태그** | mutable tag (latest 허용) | git SHA 고정 + Kyverno latest 차단 | ✅ prod/README §21.4 |
| **이미지 Digest** | 태그 기반 | 태그 기반 (SHA 태그이므로 허용) | 허용 |
| **ECR 이미지 태그 Immutability** | 미설정 | `IMMUTABLE` 권장 | ⚠️ [§2-F](#2-f-ecr-이미지-태그-immutability) |
| **PodDisruptionBudget** | 없음 | api: min 2, ai-api: min 1 | ✅ prod/README §4.7 |
| **podAntiAffinity** | 없음 | backend api 다른 노드 분산 | ✅ prod/README §4.8 |
| **ArgoCD 인증** | Helm 기본 admin (초기값 유지) | bcrypt 비밀번호 또는 OIDC SSO | ⚠️ [§3-D](#3-d-argocd-admin-자격증명) |
| **배포 방식** | envsubst + 수동 스크립트 | **Kustomize + ArgoCD GitOps** | ✅ prod-migration-checklist §4 |
| **Cognito MFA** | 없음 | TOTP 지원 + 고급 보안 | ✅ prod/README §9 |
| **CloudWatch 알람** | 없음 | 10종 알람 + Discord 연결 | ✅ prod/README §12.2 |
| **VPC Endpoint 추가** | S3/SQS/SM/ECR | + STS, KMS Interface 추가 | ✅ prod/README §3.5 |

---

## 2. Terraform — Prod 필수 변경

### 2-A. EKS etcd KMS 봉투 암호화

Dev는 `encryption_config` 블록이 없어 etcd에 저장되는 Kubernetes Secret이 AWS 기본 키로만 암호화된다.
Prod는 CMK로 봉투 암호화를 추가해 AWS 계정 레벨 침해 시에도 Secret 내용 보호.

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
    endpoint_public_access  = false   # Dev는 true — Prod는 완전 차단
  }
}
```

> **주의**: 기존 클러스터에 적용 시 Secret 전체 재암호화 발생. 신규 클러스터 구성 시 처음부터 추가할 것.

---

### 2-B. VPC Flow Logs

Dev는 미설정. Prod는 네트워크 이상 트래픽 감지와 사후 분석을 위해 필수.

```hcl
# terraform/environments/prod/01-network/main.tf
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

### 2-C. WAF 연결 확인

`terraform/modules/waf/` 모듈은 `prod-migration-checklist.md §1-B`에 코드가 있다.
CloudFront에도 WAF를 연결하려면 `scope = "CLOUDFRONT"`로 별도 WebACL을 `us-east-1`에 생성해야 한다.

```hcl
# CloudFront WAF (us-east-1 provider 필요)
resource "aws_wafv2_web_acl" "cloudfront" {
  provider = aws.us_east_1
  scope    = "CLOUDFRONT"
  ...
}
```

ALB WAF는 `prod-migration-checklist.md §1-B`의 `aws_wafv2_web_acl_association`으로 연결.

---

### 2-D. Redis 암호화 + TLS

Dev는 `aws_elasticache_cluster`(암호화 파라미터 미지원)를 사용 중.
Prod는 `aws_elasticache_replication_group`으로 교체해야 TLS와 암호화가 가능하다.

```hcl
# terraform/modules/redis/main.tf (Prod 전환 시 교체)
resource "aws_elasticache_replication_group" "this" {
  replication_group_id       = "${var.prefix}-redis"
  description                = "Redis for ${var.prefix}"
  node_type                  = var.node_type           # cache.r6g.large
  num_cache_clusters         = 2                        # Primary + Replica
  automatic_failover_enabled = true
  multi_az_enabled           = true
  transit_encryption_enabled = true                     # TLS 활성화 (Dev 미지원)
  at_rest_encryption_enabled = true
  auth_token                 = var.auth_token           # Secrets Manager에서 주입
  kms_key_id                 = var.kms_key_id
  subnet_group_name          = aws_elasticache_subnet_group.this.name
  security_group_ids         = [aws_security_group.redis.id]
}
```

백엔드 Pod 환경변수:
```env
REDIS_TLS_ENABLED=true
REDIS_AUTH_TOKEN=${from_secrets_manager}   # ESO가 utterai-prod/backend-api-secret 에서 주입
```

---

### 2-E. Terraform State Redis 토큰 노출

현재 `random_password` 리소스 결과가 S3 tfstate에 **평문** 저장된다.
Terraform 1.10으로 업그레이드 후 `ephemeral` 리소스로 전환한다.

```hcl
# 현재 위험 방식
resource "random_password" "redis_auth" { length = 32; special = false }
resource "aws_elasticache_replication_group" "this" {
  auth_token = random_password.redis_auth.result  # tfstate 평문 저장
}

# 개선 방향 (Terraform 1.10+)
ephemeral "aws_secretsmanager_secret_version" "redis_auth" {
  secret_id = aws_secretsmanager_secret.redis_auth.id
}
resource "aws_elasticache_replication_group" "this" {
  auth_token = ephemeral.aws_secretsmanager_secret_version.redis_auth.secret_string
}
```

상세: `prod/README.md §23`

---

### 2-F. ECR 이미지 태그 Immutability

Dev는 `imageTagMutability` 미설정(MUTABLE). 같은 태그로 악성 이미지를 덮어쓸 수 있다.
Prod는 `IMMUTABLE`로 설정해 태그 덮어쓰기를 차단한다.

```hcl
# terraform/modules/ecr/main.tf
resource "aws_ecr_repository" "this" {
  name                 = var.name
  image_tag_mutability = var.env == "prod" ? "IMMUTABLE" : "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}
```

---

## 3. Kubernetes — Prod 필수 변경

### 3-A. ALB ACM 인증서 적용

`k8s-demo/apps/backend/overlays/prod/patch-ingress.yaml`의 `certificate-arn`이 `TODO` 상태.
실제 ACM ARN으로 교체하고 WAF ARN도 함께 추가한다.

```yaml
# k8s-demo/apps/backend/overlays/prod/patch-ingress.yaml
annotations:
  alb.ingress.kubernetes.io/listen-ports: '[{"HTTP":80},{"HTTPS":443}]'
  alb.ingress.kubernetes.io/ssl-redirect: "443"
  alb.ingress.kubernetes.io/certificate-arn: arn:aws:acm:ap-northeast-2:<PROD_ACCOUNT_ID>:certificate/<CERT_ID>
  alb.ingress.kubernetes.io/wafv2-acl-arn: arn:aws:wafv2:ap-northeast-2:<PROD_ACCOUNT_ID>:regional/webacl/<NAME>/<ID>
```

---

### 3-B. readOnlyRootFilesystem

Dev base는 물론 현재 Prod overlay도 `readOnlyRootFilesystem`이 빠져 있다.
컨테이너 파일시스템 쓰기를 허용하면 악성 파일 생성이 가능하다.

```yaml
# k8s-demo/apps/backend/overlays/prod/patch-deployment.yaml 에 추가
containers:
  - name: api
    securityContext:
      allowPrivilegeEscalation: false
      capabilities:
        drop: ["ALL"]
      readOnlyRootFilesystem: true   # 추가
      runAsNonRoot: true
      runAsUser: 1000
      seccompProfile:
        type: RuntimeDefault         # PSS restricted 요건
```

CPU/GPU Worker는 `HF_HOME: /tmp/huggingface`를 사용하므로 `/tmp`를 `emptyDir`로 마운트해야 한다:

```yaml
# cpu-worker, ml-gpu-worker에 추가
volumes:
  - name: hf-cache
    emptyDir: {}
volumeMounts:
  - name: hf-cache
    mountPath: /tmp/huggingface
```

---

### 3-C. ClusterSecretStore → Per-Namespace SecretStore

Dev는 `ClusterSecretStore` 하나가 클러스터 전체를 커버한다.
악의적 사용자가 새 네임스페이스에 `ExternalSecret`을 만들면 `utterai-dev/*` 전체를 꺼낼 수 있다.

Prod는 네임스페이스별 `SecretStore`로 분리한다.

```yaml
# k8s/secrets/secret-store-utterai-api.yaml
apiVersion: external-secrets.io/v1beta1
kind: SecretStore
metadata:
  name: aws-secrets-manager
  namespace: utterai-api
spec:
  provider:
    aws:
      service: SecretsManager
      region: ap-northeast-2
      auth:
        jwt:
          serviceAccountRef:
            name: utterai-api-sa   # 이 SA의 IRSA만 사용 → utterai-prod/backend-api-secret 만 접근 가능
```

생성 대상 네임스페이스: `utterai-api`, `utterai-ai-cpu`, `utterai-ai-gpu`, `utterai-batch`

각 `ExternalSecret`의 `secretStoreRef.kind`를 `ClusterSecretStore` → `SecretStore`로 변경.

---

### 3-D. ArgoCD Admin 자격증명

Helm 기본 배포 시 `admin` 초기 비밀번호가 Pod 이름 기반으로 생성되고 영구 유지될 수 있다.

**단기 조치**: Secrets Manager에서 bcrypt 해시된 비밀번호 주입

```hcl
# terraform/modules/eks-addons/main.tf
resource "helm_release" "argocd" {
  values = [yamlencode({
    configs = {
      secret = {
        argocdServerAdminPassword = var.argocd_admin_password_bcrypt  # Secrets Manager에서 주입
      }
      params = {
        "server.insecure" = false
      }
    }
  })]
}
```

**장기 목표**: Cognito OIDC를 ArgoCD SSO로 연결해 `admin` 계정 비활성화

```yaml
# ArgoCD SSO 설정 (Cognito OIDC)
configs:
  cm:
    oidc.config: |
      name: Cognito
      issuer: https://cognito-idp.ap-northeast-2.amazonaws.com/<USER_POOL_ID>
      clientID: <COGNITO_CLIENT_ID>
      clientSecret: $oidc.cognito.clientSecret
      requestedScopes: ["openid", "email"]
```

---

### 3-E. Namespace PSA 레이블 (Prod enforce)

Dev는 PSA 레이블 없음. Prod는 아래 수준으로 강제 적용.

```yaml
# utterai-api — restricted enforce (순수 API, root 불필요)
metadata:
  labels:
    pod-security.kubernetes.io/enforce: restricted
    pod-security.kubernetes.io/warn: restricted
    pod-security.kubernetes.io/audit: restricted

# utterai-ai-cpu, utterai-ai-gpu, utterai-batch — baseline enforce
# (GPU NVIDIA Device Plugin 특성상 restricted 적용 시 스케줄링 실패 가능)
metadata:
  labels:
    pod-security.kubernetes.io/enforce: baseline
    pod-security.kubernetes.io/warn: restricted   # 위반 감지는 restricted 수준으로
    pod-security.kubernetes.io/audit: restricted
```

상세: `prod/README.md §21.1`

---

## 4. 미구현 → Prod 배포 전 필수 완료 항목

아래는 dev 문서에서 "미구현" 또는 "TODO"로 표시된 항목 중 Prod 배포 전 반드시 완료해야 하는 것들이다.

### 필수 (Prod 배포 불가)

| # | 항목 | 파일 | 섹션 |
|---|------|------|------|
| 1 | ALB ACM 인증서 ARN 주입 | `k8s-demo/apps/backend/overlays/prod/patch-ingress.yaml` | [§3-A](#3-a-alb-acm-인증서-적용) |
| 2 | Redis replication_group 교체 + TLS | `terraform/modules/redis/main.tf` | [§2-D](#2-d-redis-암호화--tls) |
| 3 | EKS etcd KMS 봉투 암호화 | `terraform/environments/prod/02-eks/main.tf` | [§2-A](#2-a-eks-etcd-kms-봉투-암호화) |
| 4 | Per-Namespace SecretStore 생성 | `k8s/secrets/secret-store-*.yaml` | [§3-C](#3-c-clustersecretstore-→-per-namespace-secretstore) |
| 5 | ArgoCD admin 비밀번호 관리 | `terraform/modules/eks-addons/main.tf` | [§3-D](#3-d-argocd-admin-자격증명) |
| 6 | WAF 연결 (ALB + CloudFront) | `terraform/modules/waf/`, `modules/cloudfront/` | [§2-C](#2-c-waf-연결-확인) |

### 권장 (조기 적용)

| # | 항목 | 파일 | 섹션 |
|---|------|------|------|
| 7 | VPC Flow Logs 활성화 | `terraform/environments/prod/01-network/` | [§2-B](#2-b-vpc-flow-logs) |
| 8 | readOnlyRootFilesystem 추가 | `k8s-demo/apps/*/overlays/prod/patch-deployment.yaml` | [§3-B](#3-b-readonlyrootfilesystem) |
| 9 | ECR imageTagMutability IMMUTABLE | `terraform/modules/ecr/main.tf` | [§2-F](#2-f-ecr-이미지-태그-immutability) |
| 10 | Redis tfstate 토큰 노출 해소 (Terraform 1.10+) | `terraform/modules/redis/main.tf` | [§2-E](#2-e-terraform-state-redis-토큰-노출) |

---

## 5. 이미 Prod 문서에 포함된 항목

아래 항목들은 dev 문서에서 "미구현"으로 표시됐지만 `prod/README.md`에 이미 설계가 반영된 항목들이다.
구현 시 해당 섹션을 참고한다.

| 항목 | 위치 |
|------|------|
| Pod Security Standards + Kyverno (4종 ClusterPolicy) | `prod/README.md §21` |
| Cilium CNI + CiliumNetworkPolicy (기본 deny + 허용 룰) | `prod/README.md §22` |
| EKS Public Endpoint 완전 차단 (`endpoint_public_access = false`) | `prod/README.md §4.1` |
| PodDisruptionBudget (api: min 2, ai-api: min 1) | `prod/README.md §4.7` |
| podAntiAffinity (api Pod 다른 노드 분산) | `prod/README.md §4.8` |
| Cognito MFA + 고급 보안 | `prod/README.md §9` |
| CloudWatch 알람 10종 | `prod/README.md §12.2` |
| KMS CMK 전 서비스 적용 | `prod/README.md §11`, `prod-migration-checklist §1-A` |
| envsubst 방식 폐기 → Kustomize + ArgoCD | `prod-migration-checklist §4` |
| Kyverno latest 태그 차단 ClusterPolicy | `prod/README.md §21.4` |
| ArgoCD Auto-Sync 비활성화 (수동 Sync) | `prod/README.md §15.2` |
| Terraform Ephemeral Resources (Redis 토큰) | `prod/README.md §23.3` |

---

## 관련 문서

- [Dev 보안 전체 현황](../dev/security-overview.md)
- [Dev 보안 미비점 상세](../dev/security-gaps.md)
- [Dev 보안 하드닝 이력](../dev/security-hardening.md)
- [Prod 인프라 가이드](./README.md) — §21(PSS+Kyverno), §22(Cilium), §23(Secrets)
- [Prod 전환 체크리스트](./prod-migration-checklist.md)
