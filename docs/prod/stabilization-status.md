# Prod 환경 안정화 현황

> 작성일: 2026-06-24  
> 기준: `terraform/environments/prod/`, `k8s/apps/*/overlays/prod/`, `k8s/platform/` 실제 코드  
> **범례**: ✅ 코드 확인 완료 / ⚠️ 부분 적용·불일치 / ❌ 미적용

---

## 목차

1. [Terraform — 01-network (VPC / 네트워크)](#1-terraform--01-network)
2. [Terraform — 02-eks (EKS 클러스터)](#2-terraform--02-eks)
3. [Terraform — 03-services (데이터 / 메시지 / IRSA)](#3-terraform--03-services)
4. [Terraform — 04-addons (Helm 애드온 / CloudFront / WAF)](#4-terraform--04-addons)
5. [K8s — 플랫폼 (Karpenter / KEDA / 모니터링)](#5-k8s--플랫폼)
6. [K8s — 워크로드 (Backend / AI Worker)](#6-k8s--워크로드)
7. [K8s — 보안 (NetworkPolicy / PSA / SecurityContext / PDB)](#7-k8s--보안)
8. [미완성 항목 — 우선순위별](#8-미완성-항목--우선순위별)
9. [주요 불일치 (문서 vs 코드)](#9-주요-불일치-문서-vs-코드)

---

## 1. Terraform — 01-network

| 항목 | 상태 | 비고 |
|------|------|------|
| VPC (`10.20.0.0/16`) | ✅ | `terraform.tfvars` 확인 |
| AZ 구성 | ⚠️ 2개 (2a, 2c) | tfvars 기준. docs는 3개로 기재 → **불일치** (§9 참고) |
| Public / Private App / Private Data Subnet | ✅ | 각 AZ 당 `/24` 서브넷 |
| Pod 전용 Secondary CIDR (`100.64.0.0/16`) | ✅ | `architecture.md` 및 04-addons ENIConfig 확인 |
| NAT Gateway | ⚠️ 2a에 1개 | 2개 AZ 기준 1개 공유 — AZ 장애 시 2c 아웃바운드 영향 |
| Client VPN (certificate-based) | ✅ | `main.tf` — endpoint, SG, subnet association, authorization rule 모두 구성 |
| VPN split_tunnel | ✅ | `true` — VPC/Pod CIDR만 VPN 경유, 인터넷 직접 연결 |
| VPN 팀원 `.ovpn` 배포 | ✅ | dohyun 연결 확인 완료 (kubectl, 인터넷 정상) |
| VPC Endpoint (S3 Gateway, SQS/SM/ECR Interface) | ✅ | `architecture.md` §3.3 확인 |
| VPC Endpoint SG Pod CIDR 허용 | ✅ | `compact([vpc_cidr, pod_cidr])` 버그 수정 반영 |
| VPC Flow Logs | ❌ | `01-network/main.tf`에 `aws_flow_log` 없음 |

---

## 2. Terraform — 02-eks

| 항목 | 상태 | 비고 |
|------|------|------|
| EKS 클러스터 (`utterai-prod-eks`, k8s 1.31) | ✅ | `terraform.tfvars` 확인 |
| System NodeGroup (t3.medium, desired 2 / min 2 / max 4) | ✅ | `api_node_group_enabled = false` 나머지 비활성화 |
| API / Worker / GPU NodeGroup | ✅ 비활성화 | `*_node_group_enabled = false` — Karpenter NodePool로 대체 |
| OIDC Provider | ✅ | `modules/eks` 내 자동 생성 |
| EKS 엔드포인트 (`endpoint_public_access`) | ⚠️ `true` | VPN 구축 완료 (PR #289). `false` 전환 대기 — 팀원 전체 확인 후 적용 가능 |
| EKS etcd KMS 봉투 암호화 | ❌ | `encryption_config` 블록 없음 |
| EKS 관리형 애드온 (vpc-cni v1.18.1, coredns, kube-proxy) | ✅ | `modules/eks-addons` 확인 |
| VPC CNI Custom Networking (`AWS_VPC_K8S_CNI_CUSTOM_NETWORK_CFG=true`) | ✅ | architecture.md §4.3 기준 |
| VPC CNI NetworkPolicy 활성화 | ✅ | `ENABLE_NETWORK_POLICY = "true"` |

---

## 3. Terraform — 03-services

### 3-1. RDS

| 항목 | 상태 | 비고 |
|------|------|------|
| RDS 엔진 | ✅ PostgreSQL 16 (Single Instance) | `modules/rds` 사용 — Aurora **미전환** |
| 인스턴스 타입 | ✅ `db.r6g.large` | |
| Multi-AZ | ❌ `multi_az = false` | Single-AZ 운영 중. AZ 장애 시 수동 복구 필요 |
| `deletion_protection = true` | ✅ | |
| `skip_final_snapshot = false` | ✅ | |
| `storage_encrypted = true` | ✅ | AWS 기본 키 (CMK 미전환) |
| `manage_master_user_password = true` | ✅ | tfstate에 비밀번호 미저장, Secrets Manager 자동 관리 |
| 백업 보존 7일 | ✅ | |
| Aurora 전환 | ❌ | `modules/aurora` 모듈 존재하나 `03-services`에서 미연결 |

### 3-2. Redis

| 항목 | 상태 | 비고 |
|------|------|------|
| 리소스 타입 | ✅ `aws_elasticache_replication_group` | |
| 노드 타입 | ✅ `cache.r6g.large` × 2 | `terraform.tfvars: redis_num_cache_nodes = 2` |
| TLS (`transit_encryption_enabled`) | ✅ | |
| At-rest 암호화 | ✅ | AWS 기본 키 (CMK 미전환) |
| Auth Token | ✅ | `random_password` → Secrets Manager (`utterai-prod/redis-auth-token`) |
| `automatic_failover_enabled` | ❌ | 미설정 — node 2개이나 장애 조치 비활성 |
| `multi_az_enabled` | ❌ | 미설정 |
| Redis tfstate 토큰 노출 | ❌ | `random_password.result`가 S3 state에 평문 저장 |

### 3-3. S3

| 항목 | 상태 | 비고 |
|------|------|------|
| 8개 버킷 구성 (frontend/raw-audio/template/rag-ingest/reports/kubecost/loki/tempo) | ✅ | `modules/s3` |
| 퍼블릭 접근 차단 (전부 `true`) | ✅ | |
| SSE-S3 암호화 | ✅ | SSE-KMS(CMK)로 전환 미완 |
| raw-audio 365일 수명주기 | ✅ | |
| S3 버전 관리 (`raw-audio`, `reports`) | ❌ | 모듈에 versioning 블록 없음 |
| S3 액세스 로깅 | ❌ | |

### 3-4. SQS

| 항목 | 상태 | 비고 |
|------|------|------|
| 4개 큐 (audio-preprocess / gpu-inference / report-analysis / rag-ingest) | ✅ | |
| 4개 DLQ | ✅ | |
| Karpenter Interruption Queue (`utterai-prod-eks`) | ✅ | EventBridge 규칙 포함 |
| 가시성 타임아웃 (audio-preprocess 900s, gpu-inference 1800s) | ✅ | |
| SQS SSE 암호화 (`sqs_managed_sse_enabled = true`) | ✅ | CMK 미전환 |

### 3-5. Secrets Manager

| 항목 | 상태 | 비고 |
|------|------|------|
| `utterai-prod/backend-api-secret` | ✅ | 빈 껍데기 생성, 수동 CLI 주입 방식 |
| `utterai-prod/ai-worker-secret` | ✅ | 동일 |
| `utterai-prod/gpu-worker-secret` | ✅ | HF_TOKEN |
| `utterai-prod/collect-papers-secret` | ✅ | Lambda용 |
| `utterai-prod/grafana-admin-credentials` | ✅ | ESO → Grafana admin 주입 |
| KMS CMK 암호화 | ❌ | AWS 기본 키 사용 중 |
| DB 비밀번호 자동 교체 (90일) | ❌ | `rag_ingest_secret_enabled = true` 외 rotation 미설정 |

### 3-6. IRSA

| IAM Role | 상태 | 대상 |
|----------|------|------|
| `utterai-prod-lbc-role` | ✅ | aws-load-balancer-controller |
| `utterai-prod-cluster-autoscaler-role` | ✅ (미사용) | Karpenter로 대체, enabled=false |
| `utterai-prod-eso-role` | ✅ | External Secrets Operator |
| `utterai-prod-keda-role` | ✅ | KEDA Operator |
| `utterai-prod-karpenter-role` | ✅ | Karpenter Controller |
| `utterai-prod-kubecost-role` | ✅ | Kubecost |
| `utterai-prod-loki-role` | ✅ | Loki |
| `utterai-prod-tempo-role` | ✅ | Tempo |
| `utterai-prod-api-role` | ✅ | Backend API (S3/SQS/SM) |
| `utterai-prod-ai-api-role` | ✅ | AI API |
| `utterai-prod-ai-cpu-role` | ✅ | CPU Worker (SQS recv/del, S3, Bedrock) |
| `utterai-prod-ai-ml-gpu-role` | ✅ | GPU Worker (SQS recv/del, S3) + S3 PutObject reports 최근 fix |
| `utterai-prod-batch-role` | ✅ | Batch Worker (SQS rag-ingest, S3, SM) |

### 3-7. Lambda

| 항목 | 상태 | 비고 |
|------|------|------|
| `collect_papers` Lambda (Python 3.12, timeout 900s) | ✅ | |
| EventBridge 월 1회 트리거 (매월 1일 UTC 00:00) | ✅ | |
| Lambda IAM (SM GetSecretValue, S3 GetObject/PutObject, SQS SendMessage, Bedrock InvokeModel) | ✅ | |
| FinOps Lambda 3종 (`finops-slack`/`finops-agent`/`finops-query`) | ✅ | 2026-07-03 신규 배포, `main.tf:380-639`. 상세는 [`architecture.md` §9.1](./architecture.md#91-finops-비용-조회-slack-봇--배포가동-중) |
| `finops-query` `KUBECOST_ENDPOINT` 재현성 | ⚠️ | `kubecost_alb_endpoint` 변수가 `*.tfvars`(git 미추적)에만 값 존재 — clone 후 바로 apply 시 빈 문자열로 회귀 (`architecture.md` 리스크 표 참고) |

---

## 4. Terraform — 04-addons

| 항목 | 상태 | 비고 |
|------|------|------|
| ENIConfig (Custom Networking, Pod IP 분리) | ✅ | `kubernetes_manifest.eniconfig` — AZ별 Pod Subnet 매핑 |
| Helm Addon: aws-load-balancer-controller (1.8.1) | ✅ | |
| Helm Addon: kube-prometheus-stack (66.2.1) | ✅ | |
| Helm Addon: Kubecost | ✅ | `kubecost_enabled = true`, S3 백엔드 |
| Helm Addon: Loki (7.0.0) | ✅ | S3 `utterai-prod-loki`, retention 14일 |
| Helm Addon: Tempo | ✅ | S3 `utterai-prod-tempo`, retention 3일 |
| Helm Addon: Promtail (6.17.1) | ✅ | |
| Helm Addon: metrics-server (3.12.1) | ✅ | |
| Helm Addon: external-secrets (0.10.4) | ✅ | |
| Helm Addon: KEDA (2.16.1) | ✅ | `keda_enabled = true` |
| Helm Addon: Karpenter (1.3.3) | ✅ | `karpenter_enabled = true`, Interruption Queue 연결 |
| Helm Addon: ArgoCD (9.5.20) | ✅ | bcrypt admin 비밀번호 주입 완료 |
| Helm Addon: Cluster Autoscaler | ✅ 비활성화 | `cluster_autoscaler_enabled = false` |
| Grafana admin 자격증명 (SM → ESO → K8s Secret) | ✅ | `grafana_admin_credentials_enabled = true` |
| AlertManager receiver | ❌ `null` | 알림 연동 미설정 |
| CloudFront Distribution (`app.utterai.org`, `www.utterai.org`) | ✅ | S3 OAC, SPA 서빙 |
| CloudFront WAF WebACL (CLOUDFRONT scope) | ✅ | `cloudfront_waf_enabled` — AWSManagedRulesCommonRuleSet + IP RateLimit |
| ACM 인증서 (ALB용, `api.utterai.org` / `*.utterai.org`) | ✅ | ap-northeast-2 발급 |
| ACM 인증서 (CloudFront용, `*.utterai.org`) | ✅ | us-east-1 발급 |
| Route53 A/AAAA alias (CloudFront) | ✅ | Hosted Zone `Z06102331M4SC2S9CO5RJ` |
| ALB WAF 연결 (`wafv2-acl-arn` annotation) | ❌ | Regional WebACL 미생성 — ALB에 WAF 미연결 |
| CloudWatch 알람 | ❌ | 미설정 |

---

## 5. K8s — 플랫폼

### Karpenter NodePool (6개)

| NodePool | 인스턴스 | Capacity | 용도 |
|----------|---------|----------|------|
| `platform` | t3/t3a medium~large | On-Demand | 플랫폼 컴포넌트 |
| `system` | t3/t3a medium~large | On-Demand | 시스템 (Taint: CriticalAddonsOnly) |
| `api` | t3 medium | On-Demand + Spot | API 백엔드 |
| `cpu-worker` | m5/m5a/m6i/m6a xlarge | Spot + On-Demand | CPU AI 처리 |
| `batch-worker` | c5/c6i/c6a/m5/m6i large~xlarge | Spot + On-Demand | RAG ingest |
| `gpu` | g4dn/g5 xlarge~2xlarge | Spot + On-Demand | GPU 추론 |

> **주의**: 현재 NodePool `gpu`는 Spot 허용(`["spot", "on-demand"]`) — docs/memory의 "Prod GPU On-Demand only" 정책과 **불일치**. 코드 기준 Spot 허용 상태.

### KEDA ScaledObject

| ScaledObject | Namespace | minReplica | maxReplica | Trigger |
|-------------|-----------|-----------|-----------|---------|
| `utterai-cpu-worker-scaledobject` | utterai-ai-cpu | **1** | 10 | SQS audio-preprocess-queue (qLen 5) + report-analysis-queue (qLen 5) |
| `utterai-ml-gpu-worker-scaledobject` | utterai-ai-gpu | **0** | 4 | SQS gpu-inference-queue (qLen 1) |
| `utterai-batch-worker-scaledobject` | utterai-batch | (dev 오버레이 확인 필요) | — | SQS rag-ingest-queue |

> CPU Worker는 두 큐 동시 트리거. GPU Worker cooldown 600s, scaleDown stabilization 600s.

### 플랫폼 kustomization

```
k8s/platform/prod/kustomization.yaml:
  - external-secrets/base
  - observability/base  (otel-collector, grafana-dashboard-ca-karpenter)
  - image-pruner/base
  - karpenter/overlays/prod
```

| 항목 | 상태 | 비고 |
|------|------|------|
| ClusterSecretStore (`aws-secrets-manager`) | ✅ | ESO IRSA 자동 사용 |
| OTel Collector (utterai-observability namespace) | ✅ | OTLP HTTP :4318 |
| Grafana Dashboard (CA vs Karpenter) | ✅ | `grafana-dashboard-ca-karpenter.yaml` |
| Image Pruner (ECR 이미지 정리) | ✅ | `image-pruner/base` |
| EC2NodeClass (`default`, `gpu`) | ✅ | role `utterai-prod-eks-node-role`, karpenter.sh/discovery 태그 |

---

## 6. K8s — 워크로드

### Backend (Blue/Green)

| 항목 | 상태 | 비고 |
|------|------|------|
| Blue/Green Deployment 구조 | ✅ | `deployment-blue.yaml`, `deployment-green.yaml` |
| 현재 replicas | ⚠️ blue: `0`, green: `0` | `prod-placeholder` 이미지 — 실제 이미지 미주입 상태 |
| HPA (blue/green 각각) | ✅ | minReplica: 1, maxReplica: 4, CPU 70% |
| Ingress + ALB ACM (HTTP→HTTPS redirect) | ✅ | `patch-ingress.yaml` |
| initContainer db-migrate (alembic) | ✅ | |
| `patch-configmap.yaml` (prod 환경변수) | ✅ | APP_ENV=prod, SQS/S3 prod URL |
| `patch-serviceaccount.yaml` (IRSA ARN) | ✅ | |
| Blue/Green 전환용 `patch-active-service.yaml` | ✅ | |

### AI Worker

| 항목 | 상태 | 비고 |
|------|------|------|
| CPU Worker Deployment (utterai-ai-cpu) | ✅ | SQS/S3 prod URL, APP_ENV=prod, HF_HOME=/tmp/huggingface |
| GPU (ML) Worker Deployment (utterai-ai-gpu) | ✅ | |
| Batch Worker Deployment (utterai-batch) | ✅ | SQS rag-ingest-queue prod URL |
| ExternalSecret 연동 (ai-worker, gpu-worker, rag-ingest) | ✅ | |
| ai-api dead reference 제거 | ✅ | ConfigMap, Namespace, AI_SERVICE_BASE_URL, NetworkPolicy 정리 (2026-06-23) |

---

## 7. K8s — 보안

| 항목 | 상태 | 적용일 | 파일 |
|------|------|--------|------|
| **NetworkPolicy** (deny-all + 명시적 허용) | ✅ | 2026-06-16 | `backend/overlays/prod/network-policy.yaml`, `ai-worker/overlays/prod/network-policy.yaml` |
| **PodDisruptionBudget** (blue/green `minAvailable: 1`) | ✅ | 2026-06-16 | `backend/overlays/prod/pdb.yaml`, `ai-worker/overlays/prod/pdb.yaml` |
| **ALB HTTPS + ACM** | ✅ | 2026-06-22 | `backend/overlays/prod/patch-ingress.yaml` |
| **ArgoCD bcrypt 비밀번호** | ✅ | 2026-06-22 | `modules/eks-addons/main.tf` |
| **PSA 레이블** (api: restricted / ai-*,batch: baseline) | ✅ | 2026-06-22 | `*/overlays/prod/namespace.yaml` |
| **SecurityContext 전체** (runAsNonRoot, seccompProfile:RuntimeDefault, allowPrivilegeEscalation:false, capabilities.drop:ALL) | ✅ | 2026-06-22 | `*/overlays/prod/patch-deployment.yaml` |
| **podAntiAffinity** (blue/green requiredDuring...) | ✅ | 2026-06-22 | `deployment-blue/green.yaml` |
| **readOnlyRootFilesystem** + `/tmp` emptyDir | ✅ | 2026-06-23 | 전 워크로드 |
| **ECR IMMUTABLE** | ✅ | 2026-06-23 | `modules/ecr/variables.tf` default 변경 |
| **ALB WAF 연결** | ❌ | — | `wafv2-acl-arn` annotation 없음 |
| **Kyverno** | ❌ | — | 미설치 |
| **Per-Namespace SecretStore** | ❌ | — | 현재 ClusterSecretStore 1개 공유 |

---

## 8. 미완성 항목 — 우선순위별

### 즉시 적용 가능 (운영 영향 없음)

| 순위 | 항목 | 위치 | 예상 영향 |
|------|------|------|---------|
| 1 | **EKS `endpoint_public_access = false`** | `modules/eks/main.tf:70` | VPN 구축 완료. 팀원 전체 확인 후 즉시 가능. GitHub Actions 무영향 (ArgoCD 경유). |
| 2 | **S3 버전 관리 + 액세스 로깅** (`raw-audio`, `reports`) | `modules/s3/main.tf` | 기존 데이터 영향 없음. 스토리지 비용 소폭 증가. |
| 3 | **Redis `automatic_failover_enabled = true` + `multi_az_enabled = true`** | `modules/redis/main.tf` | `terraform apply` 시 ElastiCache 수분 다운타임 가능. 별도 공지 필요. |

### 중간 난이도 (신규 리소스 / 다수 파일 수정)

| 순위 | 항목 | 위치 | 예상 영향 |
|------|------|------|---------|
| 4 | **ALB WAF 연결** | `04-addons/main.tf` + `patch-ingress.yaml` | `wafv2-acl-arn` annotation 추가. Regional WebACL 신규 생성 필요. |
| 5 | **CloudWatch 알람** | Terraform 신규 (`04-addons` 또는 별도 레이어) | SQS DLQ, ALB 5xx, RDS CPU 등 핵심 알람 설정. AlertManager receiver 연동 포함. |
| 6 | **Per-Namespace SecretStore 분리** | K8s — 네임스페이스별 `SecretStore` yaml + ExternalSecret 5개 수정 | ClusterSecretStore → 네임스페이스 격리. |
| 7 | **Cognito MFA 활성화** | `modules/cognito/main.tf` | 사용자 영향 있음. TOTP 설정 UX 안내 필요. |

### 신중히 계획 필요 (불가역적 / 운영 영향)

| 순위 | 항목 | 주의사항 |
|------|------|---------|
| 8 | **RDS Multi-AZ** (`multi_az = true`) | 단기 조치. Aurora 전환 전 가용성 보완. downtime 없이 전환 가능하나 몇 분간 재시작. |
| 9 | **Aurora 전환** | `modules/rds` → `modules/aurora` 교체. 데이터 이관(snapshot restore / DMS) 필요. 운영 중단 시간 확정 필수. |
| 10 | **VPC Flow Logs 활성화** | CloudWatch Logs 비용 발생 (트래픽량 비례). 보존 30일 권장. |
| 11 | **S3/SQS/SM KMS CMK 전환** | 기존 버킷/큐 설정 변경만으로 가능 (데이터 이동 없음). KMS 키 정책 설계 선행 필요. |
| 12 | **EKS etcd KMS 봉투 암호화** | 기존 클러스터 적용 시 전체 Secret 재암호화 발생. 신규 클러스터 생성 시 처음부터 포함 권장. |
| 13 | **Redis tfstate 토큰 노출 해소** | Terraform 1.10+ `ephemeral` 리소스 전환. 버전 업그레이드 선행 필요. |

---

## 9. 주요 불일치 (문서 vs 코드)

| 항목 | docs/prod/README.md 기재 | 실제 코드 (tfvars/manifest) | 판정 |
|------|------------------------|--------------------------|------|
| VPC CIDR | `10.0.0.0/16` | `10.20.0.0/16` | **코드 기준** — docs 오기 |
| AZ 수 | 3개 (2a, 2b, 2c) | 2개 (2a, 2c) | **코드 기준** — docs 미반영 |
| NAT Gateway 수 | 3개 (AZ별 독립) | 1개 (2a 공유) | **코드 기준** — docs 목표치 |
| Karpenter GPU NodePool capacity | On-Demand only | Spot + On-Demand 허용 | **코드 기준** — prod GPU Spot 허용 중 |
| Backend HPA maxReplicas | docs: 10 | 실제: 4 (`hpa-blue/green.yaml`) | **코드 기준** |
| CPU Worker maxReplicaCount | docs: 4 | 실제: 10 (`scaledobject-cpu-worker.yaml`) | **코드 기준** |
| GPU Worker maxReplicaCount | docs: 3 | 실제: 4 (`scaledobject-ml-gpu-worker.yaml`) | **코드 기준** |

---

## 참고 문서

- [Prod 보안 현황 상세](./security.md)
- [Prod 아키텍처 다이어그램](./architecture.md)
- [EKS Private Endpoint 전환 가이드](./eks-private-endpoint.md)
- [Prod 전환 체크리스트](./migration-checklist.md)
- [Dev vs Prod 환경 비교](../README.md)
- [Grafana 핵심 메트릭 가이드](../shared/grafana-core-metrics-guide.md)
