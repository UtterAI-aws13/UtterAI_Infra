# UtterAI Prod 환경 — 보안 적용 계획

> 작성일: 2026-06-15
> **이 문서는 Prod 전환 시 적용해야 할 보안 항목 계획이다. 현재 Prod는 미구성 상태.**
> Dev 보안 문서(`security-overview`, `security-gaps`, `security-hardening`)를 기반으로
> Dev에서 허용한 것 중 Prod에서 강화해야 할 항목을 정리한다.

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
| **Redis 리소스 타입** | `aws_elasticache_cluster` (TLS 미지원) | `aws_elasticache_replication_group` |
| **Redis TLS** | 미설정 | `transit_encryption_enabled = true` |
| **Redis Auth Token** | 없음 | Secrets Manager 관리 |
| **Redis tfstate 노출** | `random_password` → .tfstate 평문 | Terraform 1.10+ ephemeral 전환 |
| **S3 암호화** | SSE-S3 (AES256) | SSE-KMS (CMK) |
| **S3 버전 관리** | 없음 | raw-audio, reports 활성화 |
| **S3 액세스 로깅** | 없음 | 활성화 |
| **SQS 암호화** | Managed SSE | CMK |
| **Secrets Manager KMS** | AWS 기본 키 | CMK (`prod-secrets-kms-key`) |
| **Secrets Manager 교체** | 없음 | DB 비밀번호 90일 자동 교체 |
| **ALB HTTPS** | dev overlay에서 설정 예정 | HTTP→HTTPS 리다이렉트 + ACM ARN 주입 |
| **ALB WAF 연결** | 없음 | `wafv2-acl-arn` annotation |
| **Pod SecurityContext** | Prod overlay 패치 파일 준비됨 (미적용) | 클러스터 구성 후 적용 |
| **readOnlyRootFilesystem** | 없음 | Prod overlay에 추가 예정 |
| **seccompProfile** | 없음 | PSS restricted 강제로 자동 요건화 |
| **PSA 레이블** | 없음 | enforce 적용 |
| **Kyverno** | 없음 | 설치 + ClusterPolicy 4종 |
| **NetworkPolicy** | 없음 | Cilium + 기본 deny-all |
| **ClusterSecretStore** | 클러스터 전체 공유 | 네임스페이스별 SecretStore 분리 |
| **이미지 태그** | mutable tag 허용 | git SHA 고정 + Kyverno latest 차단 |
| **ECR Immutability** | MUTABLE | IMMUTABLE |
| **PodDisruptionBudget** | 없음 | api: min 2, ai-api: min 1 |
| **podAntiAffinity** | 없음 | backend api 다른 노드 분산 |
| **ArgoCD 인증** | Helm 기본 admin (초기값) | bcrypt 비밀번호 주입 또는 Cognito SSO |
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

`prod-migration-checklist.md §1-B`에 ALB WAF 코드가 있다.
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

### 2-D. Redis 암호화 + TLS

`aws_elasticache_cluster`는 `transit_encryption_enabled`를 지원하지 않아 리소스 타입 자체를 교체해야 한다.

```hcl
resource "aws_elasticache_replication_group" "this" {
  replication_group_id       = "${var.prefix}-redis"
  node_type                  = var.node_type           # cache.r6g.large
  num_cache_clusters         = 2
  automatic_failover_enabled = true
  multi_az_enabled           = true
  transit_encryption_enabled = true
  at_rest_encryption_enabled = true
  auth_token                 = var.auth_token           # Secrets Manager에서 주입
  kms_key_id                 = var.kms_key_id
}
```

백엔드 Pod 환경변수:
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

현재 `k8s-demo/apps/backend/overlays/prod/patch-ingress.yaml`의 `certificate-arn`이 `TODO` 상태.

```yaml
annotations:
  alb.ingress.kubernetes.io/listen-ports: '[{"HTTP":80},{"HTTPS":443}]'
  alb.ingress.kubernetes.io/ssl-redirect: "443"
  alb.ingress.kubernetes.io/certificate-arn: arn:aws:acm:ap-northeast-2:<PROD_ACCOUNT_ID>:certificate/<CERT_ID>
  alb.ingress.kubernetes.io/wafv2-acl-arn: arn:aws:wafv2:ap-northeast-2:<PROD_ACCOUNT_ID>:regional/webacl/<NAME>/<ID>
```

---

### 3-B. readOnlyRootFilesystem + seccompProfile

현재 Prod overlay에도 두 항목이 빠져 있음. PSS `restricted` 레이블 적용 시 `seccompProfile`은 자동 요건이 되므로 반드시 추가해야 배포가 통과된다.

```yaml
# k8s-demo/apps/backend/overlays/prod/patch-deployment.yaml
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
            name: utterai-api-sa   # 이 SA의 IRSA 범위 내 시크릿만 접근 가능
```

생성 대상 네임스페이스: `utterai-api`, `utterai-ai-cpu`, `utterai-ai-gpu`, `utterai-batch`
각 `ExternalSecret`의 `secretStoreRef.kind`를 `ClusterSecretStore` → `SecretStore`로 변경.

---

### 3-D. ArgoCD Admin 자격증명

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

### 3-E. Namespace PSA 레이블

```yaml
# utterai-api — restricted enforce
labels:
  pod-security.kubernetes.io/enforce: restricted
  pod-security.kubernetes.io/warn: restricted
  pod-security.kubernetes.io/audit: restricted

# utterai-ai-cpu, utterai-ai-gpu, utterai-batch — baseline enforce
# GPU NVIDIA Device Plugin 특성상 restricted 적용 시 스케줄링 실패 가능
labels:
  pod-security.kubernetes.io/enforce: baseline
  pod-security.kubernetes.io/warn: restricted
  pod-security.kubernetes.io/audit: restricted
```

---

## 4. 우선순위별 TODO 목록

### Prod 배포 불가 — 반드시 완료 후 배포

| # | 항목 | 파일 |
|---|------|------|
| 1 | ALB ACM 인증서 ARN 주입 | `k8s-demo/apps/backend/overlays/prod/patch-ingress.yaml` |
| 2 | Redis replication_group 교체 + TLS 활성화 | `terraform/modules/redis/main.tf` |
| 3 | EKS etcd KMS 봉투 암호화 | `terraform/environments/prod/02-eks/main.tf` |
| 4 | Per-Namespace SecretStore 생성 | `k8s/secrets/secret-store-*.yaml` |
| 5 | ArgoCD admin 비밀번호 관리 | `terraform/modules/eks-addons/main.tf` |
| 6 | WAF 연결 (ALB + CloudFront) | `terraform/modules/waf/` |

### 조기 적용 권장

| # | 항목 | 파일 |
|---|------|------|
| 7 | VPC Flow Logs 활성화 | `terraform/environments/prod/01-network/` |
| 8 | readOnlyRootFilesystem + seccompProfile 추가 | `k8s-demo/apps/*/overlays/prod/patch-deployment.yaml` |
| 9 | ECR imageTagMutability IMMUTABLE 설정 | `terraform/modules/ecr/main.tf` |
| 10 | Redis tfstate 토큰 노출 해소 (Terraform 1.10+) | `terraform/modules/redis/main.tf` |

---

## 관련 문서

- [Dev 보안 전체 현황](../dev/security-overview.md)
- [Dev 보안 미비점 상세](../dev/security-gaps.md)
- [Dev 보안 하드닝 이력](../dev/security-hardening.md)
- [Prod 인프라 가이드](./README.md) — §21(PSS+Kyverno 계획), §22(Cilium 계획), §23(Secrets 계획)
- [Prod 전환 체크리스트](./prod-migration-checklist.md)
