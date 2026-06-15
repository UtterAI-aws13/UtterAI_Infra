# UtterAI Dev 환경 — 보안 전체 현황

> 최종 업데이트: 2026-06-10  
> 범위: Terraform 모듈 + Kubernetes 매니페스트 전체

---

## 목차

1. [네트워크 격리](#1-네트워크-격리)
2. [Security Group 규칙](#2-security-group-규칙)
3. [IAM 및 IRSA](#3-iam-및-irsa)
4. [암호화 — At-Rest](#4-암호화--at-rest)
5. [암호화 — In-Transit](#5-암호화--in-transit)
6. [Secrets 관리](#6-secrets-관리)
7. [Kubernetes RBAC](#7-kubernetes-rbac)
8. [Kubernetes Namespace 격리](#8-kubernetes-namespace-격리)
9. [External Secrets — Secret 주입 흐름](#9-external-secrets--secret-주입-흐름)
10. [Ingress 및 외부 노출 제어](#10-ingress-및-외부-노출-제어)
11. [Pod 보안 설정](#11-pod-보안-설정)
12. [이미지 보안](#12-이미지-보안)
13. [미구현 항목](#13-미구현-항목)

---

## 1. 네트워크 격리

**파일**: `terraform/modules/vpc/main.tf`

### 서브넷 3계층 분리

```
Internet
   │
[Public Subnet × 2AZ]        ← NAT GW, ALB 위치
   │
[Private App Subnet × 2AZ]   ← EKS 노드 위치
   │
[Private Data Subnet × 2AZ]  ← RDS, Redis 위치 (아웃바운드 라우트 없음)
```

- Private App 서브넷은 단일 NAT GW를 통해서만 아웃바운드 가능
- Private Data 서브넷은 라우팅 테이블에 NAT GW 없음 → 인터넷 직접 노출 불가

### VPC Endpoint (AWS 서비스 접근 사설화)

| Endpoint | 유형 | 포트 | 효과 |
|----------|------|------|------|
| S3 | Gateway | - | S3 트래픽이 인터넷 미경유 |
| SQS | Interface | 443 | SQS 트래픽 VPC 내부 처리 |
| Secrets Manager | Interface | 443 | 시크릿 조회 트래픽 내부화 |
| ECR API | Interface | 443 | 이미지 pull 메타데이터 내부화 |
| ECR DKR | Interface | 443 | 이미지 레이어 pull 내부화 |

VPC Endpoint SG: VPC CIDR 내부에서 443만 허용

---

## 2. Security Group 규칙

### EKS 노드 SG (`terraform/modules/eks/main.tf`)

| 방향 | 포트 | 프로토콜 | 소스 | 용도 |
|------|------|----------|------|------|
| Inbound | ALL | ALL | Self | 노드 간 통신 (Pod-to-Pod) |
| Inbound | 443 | TCP | EKS Cluster SG | Control Plane → Node API |
| Inbound | 10250 | TCP | EKS Cluster SG | Control Plane → Kubelet |
| Outbound | ALL | ALL | 0.0.0.0/0 | 인터넷/AWS 서비스 접근 |

> **2026-06-10 수정**: 443/10250 소스를 `0.0.0.0/0`에서 `aws_eks_cluster.this.vpc_config[0].cluster_security_group_id`로 교체.

### RDS SG (`terraform/modules/rds/main.tf`)

| 방향 | 포트 | 소스 | 용도 |
|------|------|------|------|
| Inbound | 5432 | EKS Node SG | 노드에서 PostgreSQL 접근 |
| Inbound | 5432 | EKS Cluster SG | 클러스터에서 PostgreSQL 접근 |

아웃바운드 규칙 없음 (AWS 기본값: 허용)

### Redis SG (`terraform/modules/redis/main.tf`)

| 방향 | 포트 | 소스 | 용도 |
|------|------|------|------|
| Inbound | 6379 | EKS Node SG | 노드에서 Redis 접근 |
| Inbound | 6379 | EKS Cluster SG | 클러스터에서 Redis 접근 |

### VPC Endpoint SG

| 방향 | 포트 | 소스 | 용도 |
|------|------|------|------|
| Inbound | 443 | VPC CIDR | VPC 내부 AWS 서비스 접근 |

---

## 3. IAM 및 IRSA

### EKS 시스템 역할

**EKS Control Plane 역할** (`utterai-dev-eks-cluster-role`)
- 신뢰: `eks.amazonaws.com`
- 정책: `AmazonEKSClusterPolicy`

**EKS Node 역할** (`utterai-dev-eks-node-role`)
- 신뢰: `ec2.amazonaws.com`
- 정책 4개:

| 정책 | 용도 |
|------|------|
| AmazonEKSWorkerNodePolicy | 노드 기본 운영 |
| AmazonEKS_CNI_Policy | Pod 네트워킹 |
| AmazonEC2ContainerRegistryReadOnly | ECR 이미지 pull |
| AmazonSSMManagedInstanceCore | SSM 접근 (Bastion 대체) |

### IRSA 역할 — 워크로드별 최소 권한

**파일**: `terraform/modules/irsa/main.tf`

모든 IRSA 역할은 OIDC 조건으로 특정 ServiceAccount에서만 assume 가능:
```
condition: "StringEquals"
  aws:sts:RoleSessionName: system:serviceaccount:<namespace>:<sa-name>
```

| IRSA 역할 | ServiceAccount | 허용 리소스 | 허용 액션 |
|-----------|---------------|------------|----------|
| `utterai-dev-api-irsa-role` | `utterai-api/utterai-api-sa` | raw-audio, reports S3 | GetObject, PutObject, HeadObject, DeleteObject |
| | | audio-preprocess SQS | SendMessage |
| | | `utterai-dev/*` Secrets | GetSecretValue |
| | | CloudWatch | PutMetricData |
| `utterai-dev-ai-api-irsa-role` | `utterai-ai-api/utterai-ai-api-sa` | audio-preprocess SQS | SendMessage, GetQueueAttributes, GetQueueUrl |
| `utterai-dev-ai-cpu-irsa-role` | `utterai-ai-cpu/utterai-cpu-worker-sa` | audio-preprocess SQS | ReceiveMessage, DeleteMessage, ChangeMessageVisibility |
| | | gpu-inference SQS | SendMessage |
| | | raw-audio S3 | GetObject, PutObject |
| `utterai-dev-ai-ml-gpu-irsa-role` | `utterai-ai-gpu/utterai-ml-gpu-worker-sa` | gpu-inference SQS | ReceiveMessage, DeleteMessage, ChangeMessageVisibility |
| | | report-analysis SQS | SendMessage |
| | | raw-audio S3 | GetObject, PutObject |
| `utterai-dev-batch-irsa-role` | `utterai-batch/utterai-batch-worker-sa` | rag-ingest SQS + DLQ | ReceiveMessage, DeleteMessage, ChangeMessageVisibility |
| | | reports S3 | PutObject |
| | | `utterai-dev/db-password*` Secrets | GetSecretValue |
| `utterai-dev-eso-irsa-role` | `external-secrets/external-secrets` | `utterai-dev/*` Secrets | GetSecretValue, DescribeSecret |
| `utterai-dev-lbc-irsa-role` | `ingress-system/aws-load-balancer-controller` | ALB/NLB 전반 | EC2/ELBv2 관리 권한 |
| `utterai-dev-cluster-autoscaler-role` | `kube-system/cluster-autoscaler` | Auto Scaling Group | DescribeASG, SetDesiredCapacity, TerminateInstance |

---

## 4. 암호화 — At-Rest

| 리소스 | 구현 여부 | 방식 | 파일 |
|--------|-----------|------|------|
| RDS PostgreSQL | ✅ | `storage_encrypted = true` (AWS KMS 기본 키) | `modules/rds/main.tf` |
| S3 — 전 버킷 | ✅ | AES256 (SSE-S3) | `modules/s3/main.tf` |
| SQS — 전 큐 | ✅ | `sqs_managed_sse_enabled = true` | `modules/sqs/main.tf` |
| ECR | ✅ | `encryption_type = "KMS"` | `modules/ecr/main.tf` |
| ElastiCache Redis | ❌ | 미설정 (`aws_elasticache_cluster` 리소스 타입 제한) | `modules/redis/main.tf` |

---

## 5. 암호화 — In-Transit

| 구간 | 구현 여부 | 방식 |
|------|-----------|------|
| 사용자 → CloudFront | ✅ | HTTPS (CloudFront 기본 인증서) |
| CloudFront → ALB | ✅ | HTTP (내부 구간, ALB 뒤는 private) |
| ALB → Pod | ✅ | HTTP (VPC 내부 구간) |
| Pod → RDS | ✅ | RDS 기본 TLS |
| Pod → Redis | ⚠️ | `REDIS_TLS_ENABLED: "true"` (ConfigMap 설정은 있으나 Redis 인프라 미설정) |
| Pod → SQS | ✅ | HTTPS (VPC Endpoint 경유) |
| Pod → Secrets Manager | ✅ | HTTPS (VPC Endpoint 경유) |
| Pod → S3 | ✅ | HTTPS (VPC Endpoint 경유) |
| Pod → ECR | ✅ | HTTPS (VPC Endpoint 경유) |
| CloudFront → S3 | ✅ | OAC 서명 (sigv4, TLS) |

---

## 6. Secrets 관리

**파일**: `terraform/modules/secrets/main.tf`, `k8s/secrets/`

### Secrets Manager 시크릿 목록

| 시크릿 경로 | 포함 키 | 접근 역할 |
|------------|---------|----------|
| `utterai-dev/backend-api-secret` | DB_PASSWORD, JWT_SECRET_KEY, INTERNAL_CALLBACK_TOKEN, INTERNAL_CALLBACK_HMAC_SECRET | api-irsa, eso-irsa |
| `utterai-dev/ai-worker-secret` | DB_USER, DB_PASSWORD, DB_HOST, DB_PORT, DB_NAME | batch-irsa, eso-irsa |
| `utterai-dev/gpu-worker-secret` | HF_TOKEN | eso-irsa |

**RDS 마스터 비밀번호**: `manage_master_user_password = true` → Secrets Manager 자동 관리, Terraform 상태에 평문 미포함

### 시크릿 흐름 전체

```
AWS Secrets Manager
       │
       │  (IRSA 인증)
       ▼
External Secrets Operator (ClusterSecretStore: aws-secrets-manager)
       │
       │  ExternalSecret CR (1시간마다 갱신)
       ▼
Kubernetes Secret (utterai-api, utterai-ai-gpu, utterai-batch 네임스페이스)
       │
       │  env.valueFrom.secretKeyRef
       ▼
Pod 컨테이너 환경 변수
```

**핵심 보안 원칙**:
- 평문 시크릿이 Git에 커밋되지 않음
- Kubernetes Secret은 ESO가 자동 생성 및 갱신 (`creationPolicy: Owner`)
- 각 역할은 자신의 네임스페이스 시크릿에만 접근 가능

---

## 7. Kubernetes RBAC

**파일**: `k8s/rbac/serviceaccounts.yaml`, `k8s/rbac/rolebindings.yaml`

### ServiceAccount — IRSA 매핑

| ServiceAccount | Namespace | IRSA 역할 |
|---------------|-----------|----------|
| utterai-api-sa | utterai-api | utterai-dev-api-irsa-role |
| utterai-ai-api-sa | utterai-ai-api | utterai-dev-ai-api-irsa-role |
| utterai-cpu-worker-sa | utterai-ai-cpu | utterai-dev-ai-cpu-irsa-role |
| utterai-ml-gpu-worker-sa | utterai-ai-gpu | utterai-dev-ai-ml-gpu-irsa-role |
| utterai-batch-worker-sa | utterai-batch | utterai-dev-batch-irsa-role |

### Kubernetes Role 권한 범위

| Role | Namespace | 허용 리소스 | 허용 동작 |
|------|-----------|------------|----------|
| utterai-api-role | utterai-api | configmaps, secrets | get, list, watch |
| utterai-cpu-worker-role | utterai-ai-cpu | configmaps | get, list, watch |
| utterai-gpu-worker-role | utterai-ai-gpu | configmaps | get, list, watch |
| utterai-batch-worker-role | utterai-batch | configmaps | get, list, watch |

AWS 리소스 접근은 IRSA, Kubernetes 리소스 접근은 Role로 이중 제어. Pod가 다른 네임스페이스 리소스에 직접 접근 불가.

---

## 8. Kubernetes Namespace 격리

**파일**: `k8s/namespaces/namespaces.yaml`

| Namespace | 역할 | 외부 노출 | 주요 아웃바운드 |
|-----------|------|-----------|----------------|
| utterai-api | 백엔드 REST API | ALB Ingress (인터넷) | S3, SQS, Secrets Manager, Redis, RDS |
| utterai-ai-api | AI 내부 API | 없음 (cluster-internal) | SQS |
| utterai-ai-cpu | CPU 워커 | 없음 | SQS, S3 |
| utterai-ai-gpu | GPU 워커 | 없음 | SQS, S3, RDS, Secrets Manager |
| utterai-batch | 배치 워커 | 없음 | SQS, S3, RDS, Secrets Manager |
| utterai-observability | OTel 수집기 | 없음 | CloudWatch |

`utterai-ai-api`는 인터넷 노출 없이 클러스터 내부에서만 백엔드 API로부터 요청 수신.  
NetworkPolicy 미구현 → 네임스페이스 격리는 SG 레벨에서만 강제됨.

---

## 9. External Secrets — Secret 주입 흐름

**파일**: `k8s/secrets/`

### ClusterSecretStore

```yaml
# k8s/secrets/cluster-secret-store.yaml
kind: ClusterSecretStore
metadata:
  name: aws-secrets-manager
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
```

### ExternalSecret 목록

| ExternalSecret | Namespace | 갱신 주기 | 매핑 키 |
|---------------|-----------|----------|---------|
| backend-api-external-secret | utterai-api | 1시간 | DB_PASSWORD, JWT_SECRET_KEY, INTERNAL_CALLBACK_TOKEN, INTERNAL_CALLBACK_HMAC_SECRET |
| ai-worker-external-secret | utterai-ai-gpu | 1시간 | DB_USER, DB_PASSWORD, DB_HOST, DB_PORT, DB_NAME |
| ai-worker-external-secret | utterai-batch | 1시간 | (동일) |
| gpu-worker-external-secret | utterai-ai-gpu | 24시간 | HF_TOKEN |
| cpu-worker-external-secret | utterai-ai-cpu | 24시간 | HF_TOKEN |

---

## 10. Ingress 및 외부 노출 제어

**파일**: `k8s-demo/apps/backend/base/ingress.yaml`, `k8s-demo/apps/backend/overlays/`

### ALB Ingress 구성

```yaml
annotations:
  alb.ingress.kubernetes.io/scheme: internet-facing
  alb.ingress.kubernetes.io/target-type: ip
  alb.ingress.kubernetes.io/listen-ports: '[{"HTTP":80},{"HTTPS":443}]'
  alb.ingress.kubernetes.io/ssl-redirect: "443"          # HTTP → HTTPS 강제
  alb.ingress.kubernetes.io/certificate-arn: <ACM ARN>   # 환경별 overlay 주입
  alb.ingress.kubernetes.io/healthcheck-path: /health
  alb.ingress.kubernetes.io/healthcheck-interval-seconds: "15"
```

- HTTP 80 → HTTPS 443 자동 리다이렉트 구성 완료
- ACM 인증서 ARN은 dev/prod overlay에서 환경별로 주입 (`TODO` → 실제 ARN 교체 필요)

### CloudFront 보안 (`terraform/modules/cloudfront/main.tf`)

- `viewer_protocol_policy: redirect-to-https` — 사용자 HTTP 접근 차단
- Origin Access Control (OAC) — S3 직접 접근 차단, CloudFront 경유만 허용
- API 경로(`/api/*`) 포워딩: Authorization, Content-Type, Origin 헤더 전달
- 캐시 무효화: API 경로 TTL = 0 (캐시 미사용)

---

## 11. Pod 보안 설정

### 노드 배치 격리

모든 워크로드는 `nodeSelector` + `tolerations`으로 전용 노드 풀에만 스케줄됨:

| 워크로드 | nodeSelector | taint |
|---------|-------------|-------|
| backend | `workload: api` | `dedicated=api:NoSchedule` |
| ai-api | `workload: api` | `dedicated=api:NoSchedule` |
| cpu-worker | `workload: worker` | — |
| gpu-worker | `workload: ai-gpu` | `dedicated=ai-gpu:NoSchedule` |

GPU 워커는 추가로 `nvidia.com/gpu: "1"` 요청 → GPU 없는 노드에는 미배치.

### Resource Limits (자원 고갈 방지)

| 워크로드 | CPU req/limit | Memory req/limit | GPU |
|---------|--------------|-----------------|-----|
| backend | 250m / 1 | 512Mi / 1Gi | — |
| ai-api | 250m / 1 | 512Mi / 1Gi | — |
| cpu-worker | 2 / 4 | 4Gi / 8Gi | — |
| ml-gpu-worker | 2 / 4 | 8Gi / 14Gi | 1 |
| batch-worker | 1 / 2 | 2Gi / 4Gi | — |

### Health Probe (비정상 Pod 자동 격리)

| 워크로드 | readinessProbe 경로 | livenessProbe 경로 | 실패 임계 |
|---------|--------------------|--------------------|----------|
| backend | /health | /health | 3회 |
| ai-api | /health | /health | 3회 |
| 워커 전체 | /health | /health | 3회 |

### Graceful Shutdown

| 워크로드 | terminationGracePeriod | preStop 대기 |
|---------|----------------------|-------------|
| backend | 30s (기본값) | 없음 |
| cpu-worker | 60s | 5s |
| gpu-worker | 300s | 10s |
| batch-worker | 60s | 5s |

GPU 워커 300초: 추론 작업 완료까지 충분한 드레인 시간 확보.

### Pod SecurityContext (Prod overlay 적용)

**파일**: `k8s-demo/apps/backend/overlays/prod/patch-deployment.yaml`, `k8s-demo/apps/ai-worker/overlays/prod/patch-deployment.yaml`

> **2026-06-10 수정**: Prod overlay에 아래 설정 추가. Dev base는 개발 편의를 위해 미적용.

```yaml
spec:
  template:
    spec:
      securityContext:
        runAsNonRoot: true              # root 실행 금지
      containers:
        - securityContext:
            allowPrivilegeEscalation: false   # setuid/setgid 방지
            capabilities:
              drop: ["ALL"]                   # Linux capability 전체 제거
```

적용 대상: `backend`, `ai-api`, `cpu-worker`, `ml-gpu-worker`

---

## 12. 이미지 보안

**파일**: `terraform/modules/ecr/main.tf`

| 설정 | 값 | 효과 |
|------|-----|------|
| `scan_on_push` | `true` | ECR push 시 CVE 자동 스캔 |
| `encryption_type` | `KMS` | 이미지 레이어 KMS 암호화 |
| Lifecycle: `dev-*` | 최대 5개 | 구 이미지 자동 정리 |
| Lifecycle: `prod-*` | 최대 5개 | 구 이미지 자동 정리 |
| Lifecycle: untagged | 3일 후 삭제 | 빌드 잔여물 정리 |

---

## 13. 미구현 항목

### 우선순위 높음 (Prod 배포 전 필수)

| 항목 | 현황 | 해결 방향 |
|------|------|----------|
| **Redis 암호화** | `aws_elasticache_cluster` 타입 — 암호화 파라미터 미지원 | `aws_elasticache_replication_group`으로 교체 + `transit_encryption_enabled = true` |
| **ALB ACM ARN** | overlay에 `TODO` 플레이스홀더 | 실제 발급된 ARN으로 교체 (구조는 완성) |

### 중간 우선순위

| 항목 | 현황 | 해결 방향 |
|------|------|----------|
| **NetworkPolicy** | 미구현 | 네임스페이스 간 east-west 트래픽 제한 정책 추가 |
| **EKS Public Endpoint CIDR 제한** | `0.0.0.0/0` 허용 | `public_access_cidrs`로 팀 IP/VPN CIDR로 제한 |
| **VPC Flow Logs** | 미구현 | CloudWatch Logs 또는 S3로 전송 설정 |

### 낮은 우선순위 (선택 구현)

| 항목 | 현황 |
|------|------|
| WAF 연동 | Dev 미구현. Prod에서 CloudFront/ALB 연결 예정 |
| S3 Object Lock / Versioning | 미구현 (Dev 의도적 제외) |
| Cognito MFA | 미구현 |
| Pod Security Standards (restricted) | 미구현 |
| Falco / OPA Gatekeeper | 미구현 |
| CloudWatch 알람 (5xx, DLQ, CPU) | 미구현 |

---

## 관련 문서

- [보안 수정 이력](./security-hardening.md) — 이번에 수정한 항목 상세
- [Dev 환경 배포 가이드](./README.md)
- [인프라 환경 개요](../README.md)
