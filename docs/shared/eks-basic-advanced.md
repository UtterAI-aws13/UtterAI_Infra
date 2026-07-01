# UtterAI EKS 구성 가이드 — 초기 구성 & 고도화

> 이 문서는 실제 구현된 코드(`terraform/modules/eks`, `terraform/modules/eks-addons`, `k8s/` 디렉터리) 기준으로 작성됐다.  
> 설계 배경과 의사결정 흐름을 함께 기록한다.

---

## 목차

### Part 1 — 초기 구성
1. [전체 아키텍처 흐름](#1-전체-아키텍처-흐름)
2. [Terraform 레이어 구조](#2-terraform-레이어-구조)
3. [EKS 클러스터](#3-eks-클러스터)
4. [네트워크 — VPC CNI Custom Networking](#4-네트워크--vpc-cni-custom-networking)
5. [노드 그룹 구성](#5-노드-그룹-구성)
6. [OIDC & IRSA](#6-oidc--irsa)
7. [플랫폼 헬름 스택 (eks-addons 모듈)](#7-플랫폼-헬름-스택-eks-addons-모듈)
8. [Namespace 전략](#8-namespace-전략)
9. [워크로드 배치 전략 — taint / toleration / nodeSelector](#9-워크로드-배치-전략--taint--toleration--nodeselector)

### Part 2 — 고도화
10. [오토스케일링 전환 — CA+HPA → KEDA+Karpenter](#10-오토스케일링-전환--cahpa--kedakarpenter)
11. [Karpenter NodePool 상세](#11-karpenter-nodepool-상세)
12. [KEDA ScaledObject 상세](#12-keda-scaledobject-상세)
13. [관측성 스택](#13-관측성-스택)
14. [보안 강화](#14-보안-강화)
15. [AI 서비스 분리 (ai-service)](#15-ai-서비스-분리-ai-service)

---

# Part 1 — 초기 구성

## 1. 전체 아키텍처 흐름

```
사용자
  ↓
Route 53 → CloudFront
  ├─ / (정적)        → S3 Frontend
  └─ /api/* (동적)   → ALB (internet-facing, Public Subnet)
                          ↓
                     utterai-{env}-api (EKS Backend Pod)
                          ↓ SQS
              ┌───────────┴──────────────┐
              ↓                          ↓
   utterai-ai-cpu (CPU Worker)   utterai-ai-gpu (GPU Worker)
         ↓ SQS                          ↓
   utterai-batch (RAG Ingest)    utterai-ai-service (HTTP AI)
              ↓                          ↓
         S3 / RDS (pgvector) / Bedrock
```

**EKS가 담당하는 레이어**: ALB 이후 모든 컨테이너 실행, 오토스케일링, 플랫폼 운영

---

## 2. Terraform 레이어 구조

4개 레이어가 **순서대로 의존**한다. S3 Remote State로 레이어 간 output을 공유한다.

```
01-network   →   02-eks   →   03-services   →   04-addons
  VPC/서브넷       EKS 클러스터    IRSA/SQS/S3       Helm 릴리스
  Secondary CIDR   노드 그룹       RDS/Redis          LBC/Karpenter
  ENIConfig 준비   OIDC Provider   Karpenter SQS      KEDA/Observability
```

| 레이어 | 주요 리소스 | 파일 경로 |
|---|---|---|
| `01-network` | VPC, 서브넷, NAT GW, Secondary CIDR | `terraform/environments/{env}/01-network/` |
| `02-eks` | EKS Cluster, 노드 그룹, OIDC | `terraform/environments/{env}/02-eks/` |
| `03-services` | IRSA 역할, SQS, S3, RDS, Redis, Karpenter SQS | `terraform/environments/{env}/03-services/` |
| `04-addons` | ENIConfig, Helm 릴리스, CloudFront | `terraform/environments/{env}/04-addons/` |

---

## 3. EKS 클러스터

**파일**: `terraform/modules/eks/main.tf`

### 핵심 설정

```hcl
resource "aws_eks_cluster" "this" {
  name    = var.cluster_name
  version = var.kubernetes_version

  vpc_config {
    subnet_ids              = var.private_app_subnet_ids  # Private App Subnet
    endpoint_private_access = true
    endpoint_public_access  = true
  }

  access_config {
    authentication_mode = "API_AND_CONFIG_MAP"  # API 방식 + ConfigMap 병행
  }
}
```

- **엔드포인트**: Public+Private 동시 활성화. 외부에서 kubectl 사용 가능, 노드-컨트롤플레인 통신은 내부망
- **인증 모드**: `API_AND_CONFIG_MAP` — aws-auth ConfigMap 방식과 EKS Access Entry API 방식 모두 지원

### EKS Addon

| Addon | 버전 | 특이사항 |
|---|---|---|
| `vpc-cni` | v1.18.1-eksbuild.1 | Prefix Delegation + Custom Networking + NetworkPolicy 활성화 |
| `coredns` | 최신 | system 노드 그룹 생성 후 설치 (`depends_on`) |
| `kube-proxy` | 최신 | — |
| `aws-ebs-csi-driver` | 최신 | IRSA 역할 연결 (`{prefix}-ebs-csi-role`) |

---

## 4. 네트워크 — VPC CNI Custom Networking

### 배경

기본 VPC CNI는 노드 서브넷에서 Pod IP를 할당한다. **노드가 많아지면 App Subnet IP가 빠르게 고갈**된다.  
Custom Networking은 Pod IP를 별도 Secondary CIDR 서브넷(`100.64.0.0/16` 대역)에서 할당해 App Subnet IP를 보존한다.

### 구성 방식

```
App Subnet (10.0.x.x/24)  →  노드 primary ENI (노드 IP만 사용)
Pod Subnet (100.64.x.x/19) →  Pod secondary ENI (Pod IP 할당)
```

**vpc-cni Addon 설정** (`terraform/modules/eks/main.tf:102-113`):

```hcl
configuration_values = jsonencode({
  env = {
    ENABLE_PREFIX_DELEGATION         = "true"   # /28 블록 단위 IP 할당 (효율 향상)
    WARM_PREFIX_TARGET               = "1"
    AWS_VPC_K8S_CNI_CUSTOM_NETWORK_CFG = "true" # Custom Networking 활성화
    ENI_CONFIG_LABEL_DEF             = "topology.kubernetes.io/zone"
  }
  enableNetworkPolicy = "true"  # K8s NetworkPolicy 지원 활성화
})
```

**ENIConfig** (AZ별, `terraform/environments/{env}/04-addons/main.tf`):

```hcl
resource "kubernetes_manifest" "eniconfig" {
  for_each = data.terraform_remote_state.network.outputs.pod_subnet_az_map
  manifest = {
    apiVersion = "crd.k8s.amazonaws.com/v1alpha1"
    kind       = "ENIConfig"
    metadata   = { name = each.key }  # AZ 이름 (예: ap-northeast-2a)
    spec = {
      subnet         = each.value     # Pod 전용 서브넷 ID
      securityGroups = [node_sg_id]
    }
  }
}
```

### Security Group 양방향 허용

Custom Networking에서 Pod secondary ENI(노드 SG)와 컨트롤플레인 ENI(클러스터 SG) 간 통신을 위해 **양방향 허용 규칙**을 추가한다.  
(이를 빠뜨리면 Pod-to-Pod DNS 통신 불통 발생 — 2026-06-12 트러블슈팅 참고)

```hcl
# 노드 SG → 클러스터 SG 허용 (modules/eks/main.tf:227-232)
resource "aws_vpc_security_group_ingress_rule" "cluster_from_node_sg" {
  security_group_id            = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
  referenced_security_group_id = aws_security_group.node.id
  ip_protocol                  = "-1"
}
```

---

## 5. 노드 그룹 구성

**파일**: `terraform/modules/eks/main.tf`

### 노드 그룹 목록

| 노드 그룹 | 인스턴스 타입 기본값 | Taint | 용도 |
|---|---|---|---|
| `system` | t3.medium | `CriticalAddonsOnly=true:NoSchedule` | 플랫폼 addon (ArgoCD, 모니터링, LBC 등) |
| `api` | t3.medium | `dedicated=api:NoSchedule` | 백엔드 API 서버 |
| `worker` | m5.xlarge | 없음 | CPU 배치 워커 (on-demand, 50GB 디스크) |
| `gpu` | g4dn.xlarge | `dedicated=ai-gpu:NoSchedule` | GPU AI 추론 워커 |

모든 노드 그룹은 `count = var.{name}_node_group_enabled ? 1 : 0` 플래그로 환경별로 켜고 끌 수 있다.

### GPU 노드 그룹 특이사항

`AL2023_x86_64_NVIDIA` AMI를 사용할 때 `disk_size` 파라미터가 동작하지 않는다.  
Launch Template을 별도로 생성해 `/dev/xvda` 100GB로 지정한다 (`terraform/modules/eks/main.tf:370-390`).

### Cluster Autoscaler 태그

Karpenter/CA가 노드를 인식할 수 있도록 노드 SG와 노드 그룹에 태그를 부착한다:

```hcl
tags = {
  "k8s.io/cluster-autoscaler/enabled"             = "true"
  "k8s.io/cluster-autoscaler/${var.cluster_name}" = "owned"
  "karpenter.sh/discovery"                         = var.cluster_name  # 노드 SG에도 부착
}
```

---

## 6. OIDC & IRSA

**파일**: `terraform/modules/eks/main.tf`, `terraform/modules/irsa/main.tf`

### OIDC Provider

EKS 클러스터 생성 후 OIDC Provider를 생성해 ServiceAccount가 AWS IAM Role을 Assume할 수 있게 한다.

```hcl
resource "aws_iam_openid_connect_provider" "this" {
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.cluster.certificates[0].sha1_fingerprint]
  url             = aws_eks_cluster.this.identity[0].oidc[0].issuer
}
```

### IRSA 역할 목록

`terraform/modules/irsa/main.tf`에 서비스별 최소 권한 원칙으로 정의:

| ServiceAccount | Namespace | 주요 AWS 권한 |
|---|---|---|
| `utterai-api-sa` | `utterai-{env}-api` | S3(presign), SQS SendMessage, Secrets Manager, CloudWatch |
| `utterai-cpu-worker-sa` | `utterai-ai-cpu` | SQS Receive/Delete(audio-preprocess, report-analysis), S3 R/W, Bedrock |
| `utterai-ml-gpu-worker-sa` | `utterai-ai-gpu` | SQS Receive/Delete(gpu-inference), S3 R/W, Secrets Manager |
| `utterai-batch-worker-sa` | `utterai-batch` | SQS Receive/Delete(rag-ingest), S3 R/W, Secrets Manager |
| `utterai-ai-service-sa` | `utterai-ai-service` | Bedrock만 (`bedrock:InvokeModel`) |
| `keda-operator` | `keda` | SQS `GetQueueAttributes` (4개 큐) |
| `karpenter` | `karpenter` | EC2 생성/삭제, IAM PassRole, SQS(인터럽션 큐) |
| `aws-load-balancer-controller` | `ingress-system` | ALB/NLB 관리 정책 |
| `external-secrets` | `external-secrets` | Secrets Manager GetSecretValue |
| `loki` / `tempo` | `monitoring` | S3 R/W (각 버킷) |
| `kubecost` | `kubecost` | S3 R/W, EC2 Describe, pricing |
| `ebs-csi-controller-sa` | `kube-system` | EBS CSI 드라이버 정책 |

---

## 7. 플랫폼 헬름 스택 (eks-addons 모듈)

**파일**: `terraform/modules/eks-addons/main.tf`

모든 헬름 릴리스는 `04-addons` 레이어에서 일괄 설치한다.

### 플랫폼 컴포넌트

| 컴포넌트 | 차트 버전 | 네임스페이스 | 역할 |
|---|---|---|---|
| AWS Load Balancer Controller | 1.8.1 | `ingress-system` | Ingress → ALB 자동 생성 |
| External Secrets Operator | 0.10.4 | `external-secrets` | Secrets Manager → K8s Secret |
| Metrics Server | 3.12.1 | `kube-system` | kubectl top, HPA 메트릭 |
| ArgoCD | 9.5.20 | `argocd` | GitOps 배포 |
| EFS CSI Driver | 3.0.7 | `kube-system` | 공유 스토리지 (prod) |
| NVIDIA Device Plugin | 0.16.2 | `kube-system` | GPU 노드 `nvidia.com/gpu` 리소스 등록 |
| Cluster Autoscaler | 9.37.0 | `kube-system` | `cluster_autoscaler_enabled=true` 시 설치 (현재 false) |
| KEDA | 2.16.1 | `keda` | `keda_enabled=true` 시 설치 |
| Karpenter | 1.3.3 | `karpenter` | `karpenter_enabled=true` 시 설치 |

### 관측성 컴포넌트

| 컴포넌트 | 차트 버전 | 역할 |
|---|---|---|
| kube-prometheus-stack | 66.2.1 | Prometheus + Grafana + Alertmanager |
| Grafana Loki | 7.0.0 | 로그 수집 (S3 백엔드) |
| Promtail | 6.17.1 | 노드 Pod stdout 수집 → Loki |
| Grafana Tempo | (변수) | 분산 트레이싱 (OTLP, S3 백엔드) |
| Kubecost | (변수) | 비용 모니터링 (Prometheus 연동) |

### Prometheus 전체 네임스페이스 수집 설정

```hcl
serviceMonitorSelectorNilUsesHelmValues = false
podMonitorSelectorNilUsesHelmValues     = false
ruleSelectorNilUsesHelmValues           = false
serviceMonitorNamespaceSelector         = {}  # 모든 네임스페이스 ServiceMonitor 수집
```

### Alertmanager 설정

```hcl
alertmanagerConfigMatcherStrategy = { type = "None" }
# → AlertmanagerConfig의 namespace 라벨 제약 해제 (team=utterai 라벨 기반 라우팅 가능)
```

---

## 8. Namespace 전략

### 실제 네임스페이스 목록

| Namespace | 용도 |
|---|---|
| `kube-system` | EKS 기본 addon |
| `ingress-system` | AWS Load Balancer Controller |
| `argocd` | GitOps 배포 |
| `karpenter` | Karpenter Controller |
| `keda` | KEDA Operator |
| `external-secrets` | External Secrets Operator |
| `monitoring` | Prometheus, Grafana, Alertmanager, Loki, Tempo, Kubecost |
| `utterai-observability` | OTel Collector, Promtail (앱 레벨 관측성) |
| `utterai-{env}-api` | 백엔드 API 서버 |
| `utterai-ai-cpu` | CPU AI Worker (오디오 전처리, 리포트 분석) |
| `utterai-ai-gpu` | GPU AI Worker (화자 분리, STT 추론) |
| `utterai-batch` | Batch Worker (RAG ingest) |
| `utterai-ai-service` | AI HTTP 서비스 (report-chat 동기 응답) |

### 분리 이유

- **배포 독립성**: 서비스별 이미지 업데이트가 다른 서비스에 영향 없음
- **IRSA 최소 권한**: Namespace별 ServiceAccount → 개별 IAM Role
- **NetworkPolicy 격리**: prod에서 네임스페이스 간 트래픽 명시적 허용 (기본 차단)
- **Pod Security Standards**: prod `utterai-ai-service`에 `enforce: baseline` 적용

---

## 9. 워크로드 배치 전략 — taint / toleration / nodeSelector

### 배치 정책 요약

| 워크로드 | nodeSelector | toleration | 배치 노드 |
|---|---|---|---|
| Backend API | `dedicated: api` | `dedicated=api:NoSchedule` | api 노드 그룹 / Karpenter api NodePool |
| CPU Worker | `workload: cpu-worker` | `dedicated=worker:NoSchedule` | worker 노드 그룹 / Karpenter cpu-worker NodePool |
| GPU Worker | `workload: ai-gpu` | `dedicated=ai-gpu:NoSchedule`, `nvidia.com/gpu:NoSchedule` | gpu 노드 그룹 / Karpenter gpu NodePool |
| Batch Worker | `workload: batch-worker` | `dedicated=worker:NoSchedule` | worker 노드 그룹 / Karpenter batch-worker NodePool |
| AI Service | `workload: cpu-worker` | `dedicated=worker:NoSchedule` | cpu-worker NodePool 공유 (임베딩 모델 공유) |
| 플랫폼 addon | `role: system` | `CriticalAddonsOnly:NoSchedule` | system 노드 그룹 |

### GPU Worker 리소스 요청

```yaml
resources:
  requests:
    cpu: "2"
    memory: "6Gi"
    nvidia.com/gpu: "1"
  limits:
    cpu: "4"
    memory: "14Gi"
    nvidia.com/gpu: "1"
```

`nvidia.com/gpu`는 정수 단위로 요청한다. NVIDIA Device Plugin이 GPU 노드에 해당 리소스를 등록하고, 이 요청이 있는 Pod만 GPU 노드에 스케줄된다.

---

# Part 2 — 고도화

## 10. 오토스케일링 전환 — CA+HPA → KEDA+Karpenter

### 전환 배경

| 항목 | Phase 1 (CA+HPA) | Phase 2 (KEDA+Karpenter) |
|---|---|---|
| Pod 스케일 기준 | CPU 사용률 > 70% | SQS 큐 깊이 (즉시 반응) |
| Pod 스케일 지연 | 수 분 (CPU 임계값 도달 대기) | ~30초 이내 |
| 노드 프로비저닝 | CA → ASG 조정 (~3~5분) | Karpenter 직접 프로비저닝 (~60초) |
| 유휴 노드 회수 | 느림 | 설정 가능 (cpu-worker: 5m, batch: 30s, gpu: 10m) |

CPU 부하 없이 SQS 큐가 쌓이는 시나리오에서 HPA는 전혀 반응하지 않는다. KEDA는 큐 깊이를 직접 관찰하므로 이 문제가 없다.

### 현재 상태 (dev/prod 모두 전환 완료)

| 환경 | CA | KEDA | Karpenter |
|---|---|---|---|
| dev | ❌ 비활성화 | ✅ | ✅ |
| prod | ❌ 비활성화 | ✅ | ✅ |

**`terraform/environments/{env}/04-addons/main.tf`**:

```hcl
cluster_autoscaler_enabled = false
keda_enabled               = true
keda_irsa_role_arn         = data.terraform_remote_state.services.outputs.keda_role_arn
karpenter_enabled          = true
karpenter_irsa_role_arn    = data.terraform_remote_state.services.outputs.karpenter_role_arn
```

### KEDA 인증 방식

`identityOwner: operator` — KEDA operator 자체 IRSA(`{prefix}-keda-irsa-role`)로 SQS 큐 깊이를 조회한다.  
워커 Pod IRSA와 별개이며, `sqs:GetQueueAttributes` 권한이 4개 큐에 부여돼 있다.

```yaml
# k8s/apps/ai-worker/overlays/{env}/keda-trigger-auth.yaml
apiVersion: keda.sh/v1alpha1
kind: ClusterTriggerAuthentication
metadata:
  name: keda-aws-pod-identity
spec:
  podIdentity:
    provider: aws
```

### Karpenter 인터럽션 큐

Spot 인터럽션 이벤트 처리용 SQS 큐를 `03-services`에서 생성한다. **큐 이름은 클러스터 이름과 동일**해야 한다.

```hcl
resource "aws_sqs_queue" "karpenter_interruption" {
  name                      = var.cluster_name  # "utterai-dev-eks" 또는 "utterai-prod"
  message_retention_seconds = 300
}
```

---

## 11. Karpenter NodePool 상세

**파일**: `k8s/platform/karpenter/base/nodepools.yaml`

EC2NodeClass는 dev/prod overlay 패치로 Role·서브넷 태그를 환경별로 분리한다.

### NodePool 목록

| NodePool | 인스턴스 패밀리 | Capacity | Taint | consolidationPolicy | consolidateAfter |
|---|---|---|---|---|---|
| `platform` | t3/t3a medium·large | on-demand | 없음 | WhenEmptyOrUnderutilized | 30s |
| `system` | t3/t3a medium·large | on-demand | `CriticalAddonsOnly=true:NoSchedule` | WhenEmptyOrUnderutilized | 30s |
| `api` | t3 medium | spot+on-demand | `dedicated=api:NoSchedule` | WhenEmptyOrUnderutilized | 30s |
| `cpu-worker` | m5/m5a/m6i/m6a xlarge | spot+on-demand | `dedicated=worker:NoSchedule` | WhenEmptyOrUnderutilized | **5m** |
| `batch-worker` | c5/c6i/c6a/m5/m6i large·xlarge | spot+on-demand | `dedicated=worker:NoSchedule` | WhenEmptyOrUnderutilized | 30s |
| `gpu` | g4dn/g5 xlarge·2xlarge | spot+on-demand | `dedicated=ai-gpu:NoSchedule`, `nvidia.com/gpu=true:NoSchedule` | **WhenEmpty** | **10m** |

### 설계 포인트

- **worker → cpu-worker + batch-worker 분리**: 인스턴스 패밀리와 스케일링 특성이 달라 분리. batch-worker는 spot pool 다양화를 위해 5종 패밀리 사용
- **gpu**: `WhenEmpty` 정책 — GPU Pod가 없을 때만 회수 (ScaledObject scaleDown stabilization 300s와 조합해 무분별한 재기동 방지). consolidateAfter 10m으로 여유
- **cpu-worker**: 5m 대기 후 회수 — 임베딩 모델 로드 시간 고려 (재기동 비용이 높음)

### EC2NodeClass dev/prod 패치

```yaml
# k8s/platform/karpenter/overlays/dev/patch-ec2nodeclass-default-dev.yaml
spec:
  role: "utterai-dev-eks-node-role"
  subnetSelectorTerms:
    - tags:
        karpenter.sh/discovery: "utterai-dev-eks"
  securityGroupSelectorTerms:
    - tags:
        karpenter.sh/discovery: "utterai-dev-eks"
```

---

## 12. KEDA ScaledObject 상세

**파일**: `k8s/apps/ai-worker/overlays/{env}/scaledobject-*.yaml`

### dev ScaledObject 설정

| 항목 | cpu-worker | batch-worker | ml-gpu-worker |
|---|---|---|---|
| 대상 Deployment | `utterai-cpu-worker` | `utterai-batch-worker` | `utterai-ml-gpu-worker` |
| 네임스페이스 | `utterai-ai-cpu` | `utterai-batch` | `utterai-ai-gpu` |
| 모니터 큐 | audio-preprocess + report-analysis | rag-ingest | gpu-inference |
| `queueLength` (Pod 1개당) | 5 | 3 | 1 |
| `activationQueueLength` | — | 0 | 0 |
| `minReplicaCount` | **1** | 0 | 0 |
| `maxReplicaCount` | 3 | 2 | 1 |
| `cooldownPeriod` | 120s | 120s | 300s |
| scaleDown stabilization | — | — | 300s |
| `scaleOnInFlight` | — | — | **true** |
| `identityOwner` | operator | operator | operator |

### prod 차이점

| 항목 | dev | prod |
|---|---|---|
| cpu-worker maxReplica | 3 | **10** |
| cpu-worker cooldown | 120s | **300s** |
| activationQueueLength | 0 | 0 |

### 설계 포인트

- **`activationQueueLength: 0`**: 큐에 메시지가 1개라도 있으면 즉시 0→1 스케일아웃
- **`scaleOnInFlight: true`** (gpu-worker): 처리 중인 메시지도 큐 깊이로 계산. GPU 추론 중 새 메시지 도착 시 조기 스케일아웃 방지
- **cpu-worker minReplica=1**: 모델 로드 cold start를 피하기 위해 최소 1개 상시 유지

---

## 13. 관측성 스택

### 전체 데이터 흐름

```
앱 Pod (FastAPI BE / CPU Worker / GPU Worker)
  ├── OTel SDK → OTLP HTTP :4318 → OTel Collector
  │     ├── traces → Tempo (S3 백엔드)
  │     ├── metrics → Prometheus exporter :8889
  │     └── logs → Loki (S3 백엔드)
  └── stdout/stderr → Promtail → Loki
                                   ↓
                               Grafana (Tempo/Prometheus/Loki 연동)
```

### Loki 민감정보 마스킹 (Promtail 파이프라인)

`terraform/modules/eks-addons/main.tf`의 Promtail values에 정규식 replace 파이프라인이 내장돼 있다:

- `Authorization: Bearer <token>` → `[REDACTED_AUTHORIZATION]`
- `password`, `token`, `api_key`, `hf_token` 등 → `[REDACTED_SENSITIVE_FIELD]`
- AWS Presigned URL (`X-Amz-Signature` 등) → `[REDACTED_PRESIGNED_URL]`

### Grafana 데이터소스 연동

| 데이터소스 | UID | URL |
|---|---|---|
| Prometheus | `prometheus` | 자동 (kube-prometheus-stack 내장) |
| Loki | `loki` | `http://loki-gateway.monitoring.svc.cluster.local` |
| Tempo | `tempo` | `http://tempo.monitoring.svc.cluster.local:3100` |

**Tempo → Loki 1클릭 이동** 설정:
```hcl
tracesToLogs = {
  datasourceUid   = "loki"
  tags            = ["service.name"]
  mappedTags      = [{ key = "service.name", value = "service_name" }]
  filterByTraceID = true
  lokiSearch      = true
}
```

### Grafana 대시보드 (코드화, ArgoCD 관리)

| 파일 | 내용 |
|---|---|
| `k8s/platform/observability/base/grafana-dashboard-utterai.yaml` | API Health, Audio Pipeline, GPU Inference, Infrastructure |
| `k8s/platform/observability/base/grafana-dashboard-ca-karpenter.yaml` | Karpenter NodeClaim 상태 |
| `k8s/platform/observability/base/grafana-dashboard-pipeline.yaml` | 파이프라인 처리 현황 |

### Discord 알림

`alertmanager-discord-config.yaml` + `alertmanager-discord-secret.yaml` (ExternalSecret)이 kustomization에 active.  
AWS Secrets Manager에 `utterai-prod/alertmanager-discord` 시크릿을 수동 등록해야 실제 알림이 동작한다:

```bash
aws secretsmanager create-secret \
  --name "utterai-prod/alertmanager-discord" \
  --secret-string '{"webhook_url":"https://discord.com/api/webhooks/..."}' \
  --region ap-northeast-2
```

---

## 14. 보안 강화

### NetworkPolicy (prod)

prod 네임스페이스(`utterai-ai-worker/overlays/prod`, `utterai-ai-service/overlays/prod`)에 기본 차단 후 명시 허용 정책 적용:

```yaml
# 기본: 모든 ingress/egress 차단
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-all
spec:
  podSelector: {}
  policyTypes: [Ingress, Egress]
```

허용하는 트래픽:
- Ingress: Backend → AI Worker/Service (8000), Prometheus Scrape (monitoring namespace)
- Egress: DNS (53), AWS 서비스 (443), RDS (5432), OTel Collector (4318)

### Pod Security Standards (prod ai-service)

```yaml
# k8s/apps/ai-service/overlays/prod/namespace.yaml
labels:
  pod-security.kubernetes.io/enforce: baseline    # 위험 권한 실행 차단
  pod-security.kubernetes.io/warn: restricted     # 더 엄격한 기준 경고
  pod-security.kubernetes.io/audit: restricted
```

### 컨테이너 보안 설정 (prod ai-service, ai-worker)

```yaml
securityContext:
  runAsNonRoot: true
  seccompProfile:
    type: RuntimeDefault

containers:
  - securityContext:
      allowPrivilegeEscalation: false
      capabilities:
        drop: ["ALL"]
      readOnlyRootFilesystem: true  # /tmp는 emptyDir로 별도 마운트
```

### PodDisruptionBudget (prod)

노드 업그레이드/Karpenter consolidation 시 서비스 중단 방지:

```yaml
# k8s/apps/ai-worker/overlays/prod/pdb.yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
spec:
  minAvailable: 1
```

### AZ 분산 (prod api, ai-service)

```yaml
topologySpreadConstraints:
- maxSkew: 1
  topologyKey: topology.kubernetes.io/zone
  whenUnsatisfiable: ScheduleAnyway
  labelSelector:
    matchLabels:
      app: utterai-ai-service
```

---

## 15. AI 서비스 분리 (ai-service)

### 배경

기존 AI 처리는 SQS 기반 비동기 워커로만 구성됐다. report-chat 기능은 **사용자 질문에 즉시 응답**해야 하므로 동기 HTTP 서비스로 분리했다.

**관련 PR**: [#361 feature/deploy-ai-report-chat-service](https://github.com/UtterAI-aws13/UtterAI_Infra/pull/361)

### ai-service vs ai-worker 비교

| | `utterai-ai-service` | `utterai-ai-cpu` (ai-worker) |
|---|---|---|
| 통신 방식 | 동기 HTTP | 비동기 SQS |
| 스케일링 | HPA (CPU/메모리) | KEDA (SQS 큐 깊이) |
| IRSA 권한 | Bedrock만 | SQS + S3 + Bedrock |
| 이미지 | `utterai-ai-cpu` 동일 이미지 | `utterai-ai-cpu` |
| 노드 배치 | cpu-worker NodePool (taint `dedicated=worker`) | cpu-worker NodePool |
| 용도 | report-chat 실시간 RAG 응답 | 오디오 전처리 / 리포트 생성 |

### 트래픽 경로

```
Backend (utterai-prod-api) → HTTP :8000 → ai-service (utterai-ai-service)
                                                ↓
                                    Bedrock (Claude) + pgvector RDS
```

NetworkPolicy로 `utterai-prod-api` 네임스페이스에서 오는 트래픽만 허용한다.

### 리소스 설정 (prod)

초기 `requests 1Gi / limits 3Gi`에서 OOMKilled 발생 후 상향 조정:

```yaml
resources:
  requests:
    cpu: 500m
    memory: 2Gi   # cpu-worker와 동일한 임베딩 모델 로드
  limits:
    cpu: "2"
    memory: 6Gi
```

---

## 참고 문서

| 문서 | 내용 |
|---|---|
| [`docs/dev/keda-karpenter-transition.md`](../dev/keda-karpenter-transition.md) | KEDA+Karpenter 전환 상세 가이드 및 현황 |
| [`docs/prod/eks-advanced-operations-progress.md`](../prod/eks-advanced-operations-progress.md) | EKS 고도화 진행 현황 (관측성 중심) |
| [`docs/dev/troubleshooting/`](../dev/troubleshooting/) | 환경별 트러블슈팅 기록 |
| [`docs/shared/gpu-cold-start-strategy.md`](gpu-cold-start-strategy.md) | GPU cold start 전략 |
| [`docs/prod/security.md`](../prod/security.md) | prod 보안 상세 |
