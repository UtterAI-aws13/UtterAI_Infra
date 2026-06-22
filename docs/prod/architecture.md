# UtterAI Prod 아키텍처

> AWS ap-northeast-2 (Seoul) · EKS 1.31 · Terraform 4-Layer State

---

## 목차

1. [전체 아키텍처 개요](#1-전체-아키텍처-개요)
2. [Terraform 레이어 구조](#2-terraform-레이어-구조)
3. [네트워크 아키텍처](#3-네트워크-아키텍처)
4. [EKS 클러스터](#4-eks-클러스터)
5. [데이터 레이어](#5-데이터-레이어)
6. [메시지 큐 (SQS)](#6-메시지-큐-sqs)
7. [스토리지 (S3)](#7-스토리지-s3)
8. [Helm Addon 스택](#8-helm-addon-스택)
9. [CloudFront · ALB · 트래픽 흐름](#9-cloudfront--alb--트래픽-흐름)
10. [보안 (IAM / IRSA / Secrets)](#10-보안-iam--irsa--secrets)
11. [모니터링 스택](#11-모니터링-스택)

---

## 1. 전체 아키텍처 개요

```
                           INTERNET
                               │
              ┌────────────────┴────────────────┐
              │                                 │
              ▼                                 ▼
     app.utterai.org                    API 요청
     Route 53 → CloudFront         Route 53 → ALB
              │                                 │
              ▼                                 ▼
     S3 utterai-prod-frontend        EKS utterai-prod-eks
     (정적 프론트엔드)                   (API / Worker Pods)
                                               │
                        ┌──────────────────────┼────────────────┐
                        │                      │                │
                        ▼                      ▼                ▼
                utterai-prod-rds   utterai-prod-redis   SQS Queues (4개)
                (PostgreSQL 16.9)   (Redis 7.1, 2-node)
                        │                                       │
                        └───────────── S3 Buckets (8개) ────────┘
```

---

## 2. Terraform 레이어 구조

```
terraform/environments/prod/
│
├── 01-network/   VPC · 서브넷 · NAT GW · VPC Endpoint · 보안그룹
│   State Key: prod/network/terraform.tfstate
│
├── 02-eks/       EKS 클러스터 · Node Group · OIDC Provider · EKS Addon
│   State Key: prod/platform/terraform.tfstate
│   Depends on: 01-network (vpc_id, subnet_ids)
│
├── 03-services/  RDS · Redis · S3 · SQS · Secrets · IRSA
│                 Karpenter Interruption Queue
│   State Key: prod/services/terraform.tfstate
│   Depends on: 01-network, 02-eks
│
└── 04-addons/    Helm 릴리스 전체 (LBC · KEDA · Karpenter · Monitoring …)
                  ENIConfig (Custom Networking) · CloudFront · Route53
    State Key: prod/addons/terraform.tfstate
    Depends on: 01-network, 02-eks, 03-services

State Backend: S3 utterai-prod-terraform-state (ap-northeast-2)
State Lock: S3 Native Locking (Terraform 1.10+)
```

---

## 3. 네트워크 아키텍처

### 3.1 VPC 서브넷 구성

```
┌───────────────────────────────────────────────────────────────────────────┐
│  VPC: utterai-prod-vpc  (10.20.0.0/16)   Region: ap-northeast-2          │
│  Secondary CIDR: 100.64.0.0/16  (Pod 전용 IP 공간)                        │
│                                                                           │
│         ap-northeast-2a                    ap-northeast-2c               │
│  ┌─────────────────────────┐      ┌─────────────────────────┐            │
│  │  Public Subnet          │      │  Public Subnet          │            │
│  │  10.20.1.0/24           │      │  10.20.2.0/24           │            │
│  │                         │      │                         │            │
│  │  ┌───────────────────┐  │      │                         │            │
│  │  │  NAT Gateway (1개)│  │      │  (NAT GW 없음)          │            │
│  │  │  Elastic IP 부착  │  │      │                         │            │
│  │  └───────────────────┘  │      │                         │            │
│  │  ALB 노드 (AZ 2a)       │      │  ALB 노드 (AZ 2c)       │            │
│  └────────────┬────────────┘      └────────────┬────────────┘            │
│               │ Internet GW ◄──────────────────┘                         │
│               │                                                           │
│  ┌────────────▼────────────┐      ┌──────────────────────────┐           │
│  │  Private App Subnet     │      │  Private App Subnet      │           │
│  │  10.20.11.0/24          │      │  10.20.12.0/24           │           │
│  │                         │      │                          │           │
│  │  EKS Node (Node IP)     │      │  EKS Node (Node IP)      │           │
│  │  tag: karpenter.sh/     │      │  tag: karpenter.sh/      │           │
│  │       discovery         │      │       discovery          │           │
│  └─────────────────────────┘      └──────────────────────────┘           │
│                                                                           │
│  ┌─────────────────────────┐      ┌──────────────────────────┐           │
│  │  Pod Subnet (Secondary) │      │  Pod Subnet (Secondary)  │           │
│  │  100.64.0.0/17          │      │  100.64.128.0/17         │           │
│  │                         │      │                          │           │
│  │  EKS Pod IP (Custom     │      │  EKS Pod IP (Custom      │           │
│  │  Networking via ENIConf)│      │  Networking via ENIConf) │           │
│  └─────────────────────────┘      └──────────────────────────┘           │
│                                                                           │
│  ┌─────────────────────────┐      ┌──────────────────────────┐           │
│  │  Private Data Subnet    │      │  Private Data Subnet     │           │
│  │  10.20.21.0/24          │      │  10.20.22.0/24           │           │
│  │                         │      │                          │           │
│  │  RDS PostgreSQL         │      │  RDS PostgreSQL          │           │
│  │  ElastiCache Redis      │      │  ElastiCache Redis       │           │
│  └─────────────────────────┘      └──────────────────────────┘           │
└───────────────────────────────────────────────────────────────────────────┘
```

> NAT Gateway는 2a에 1개만 배치 (비용 최적화). 2c Private App → 2a NAT GW 경유 아웃바운드.

### 3.2 라우팅 테이블 요약

| 서브넷 | 라우팅 |
|---|---|
| Public (2a, 2c) | 0.0.0.0/0 → Internet GW |
| Private App (2a, 2c) | 0.0.0.0/0 → NAT GW (2a) |
| Pod Subnet (2a, 2c) | Private App 라우팅 테이블 공유 |
| Private Data (2a, 2c) | 로컬 전용 (외부 아웃바운드 없음) |

### 3.3 VPC Endpoint

| 종류 | 서비스 | 유형 | 배치 서브넷 |
|---|---|---|---|
| S3 | com.amazonaws.ap-northeast-2.s3 | Gateway | Private App RT |
| SQS | com.amazonaws.ap-northeast-2.sqs | Interface | Private App (2a, 2c) |
| Secrets Manager | com.amazonaws.ap-northeast-2.secretsmanager | Interface | Private App (2a, 2c) |
| ECR API | com.amazonaws.ap-northeast-2.ecr.api | Interface | Private App (2a, 2c) |
| ECR DKR | com.amazonaws.ap-northeast-2.ecr.dkr | Interface | Private App (2a, 2c) |

모든 Interface Endpoint는 `utterai-prod-vpc-endpoint-sg`(포트 443, VPC CIDR 내부만 허용)에 부착.

---

## 4. EKS 클러스터

### 4.1 클러스터 기본 정보

| 항목 | 값 |
|---|---|
| 클러스터 이름 | `utterai-prod-eks` |
| Kubernetes 버전 | 1.31 |
| 인증 방식 | API_AND_CONFIG_MAP |
| 엔드포인트 | Private + Public 모두 활성 |
| 배치 서브넷 | Private App (10.20.11.0/24, 10.20.12.0/24) |
| CNI | VPC CNI v1.18.1 (Custom Networking + Prefix Delegation) |

### 4.2 Node Group 구성

```
┌──────────────────────────────────────────────────────────────────────────┐
│  EKS Cluster: utterai-prod-eks (k8s 1.31)                               │
│                                                                          │
│  ┌───────────────────────────────────────────────────────────────────┐   │
│  │  System Node Group  (utterai-prod-system)             [ENABLED]  │   │
│  │  인스턴스: t3.medium (ON_DEMAND)                                  │   │
│  │  규모: desired 2 / min 2 / max 4                                  │   │
│  │  서브넷: Private App (2a, 2c)                                     │   │
│  │  Taint: CriticalAddonsOnly=true:NoSchedule                       │   │
│  │  Label: role=system                                               │   │
│  │                                                                   │   │
│  │  실행 워크로드:                                                    │   │
│  │    CoreDNS · kube-proxy · AWS LBC · Karpenter · KEDA             │   │
│  │    External Secrets Operator · ArgoCD · Metrics Server           │   │
│  │    Promtail · Prometheus · Grafana · Loki · Tempo · Kubecost     │   │
│  └───────────────────────────────────────────────────────────────────┘   │
│                                                                          │
│  ┌───────────────────────────────────────────────────────────────────┐   │
│  │  API Node Group  (utterai-prod-api)                  [DISABLED]  │   │
│  │  인스턴스: t3.large (ON_DEMAND)                                   │   │
│  │  규모: desired 2 / min 2 / max 6                                  │   │
│  │  Taint: dedicated=api:NoSchedule                                  │   │
│  │  Label: role=api, workload=api                                    │   │
│  │  → Karpenter NodePool로 대체 예정                                  │   │
│  └───────────────────────────────────────────────────────────────────┘   │
│                                                                          │
│  ┌───────────────────────────────────────────────────────────────────┐   │
│  │  Worker Node Group  (utterai-prod-worker)            [DISABLED]  │   │
│  │  인스턴스: m5.2xlarge (ON_DEMAND) · 디스크: 100 GB gp3           │   │
│  │  Taint: 없음 (일반 워커)                                          │   │
│  │  Label: role=worker, workload=worker                              │   │
│  │  → Karpenter NodePool로 대체 예정                                  │   │
│  └───────────────────────────────────────────────────────────────────┘   │
│                                                                          │
│  ┌───────────────────────────────────────────────────────────────────┐   │
│  │  GPU Node Group  (utterai-prod-gpu)                  [DISABLED]  │   │
│  │  인스턴스: g4dn.xlarge (ON_DEMAND)                                │   │
│  │  AMI: AL2023_x86_64_NVIDIA                                       │   │
│  │  디스크: 100 GB gp3 (Launch Template)                             │   │
│  │  규모: desired 1 / min 1                                          │   │
│  │  Taint: dedicated=ai-gpu:NoSchedule, nvidia.com/gpu:NoSchedule   │   │
│  │  Label: role=gpu, workload=ai-gpu                                 │   │
│  │  → Karpenter NodePool로 대체 예정                                  │   │
│  └───────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────────┘
```

### 4.3 Custom Networking (Pod IP 분리)

```
문제: Primary 서브넷(/24 = 최대 ~250 IP)에서 노드 IP + 파드 IP 혼용 시 IP 고갈
해결: Secondary CIDR 100.64.0.0/16 + Custom Networking

┌─────────────────────────────────────────────────────────────────────────┐
│  VPC CNI 환경변수                                                        │
│    ENABLE_PREFIX_DELEGATION           = true  (노드당 Pod 수 확장)       │
│    WARM_PREFIX_TARGET                 = 1                               │
│    AWS_VPC_K8S_CNI_CUSTOM_NETWORK_CFG = true  (Custom Networking 활성)   │
│    ENI_CONFIG_LABEL_DEF = topology.kubernetes.io/zone                  │
│                                                                         │
│  ENIConfig (04-addons에서 kubernetes_manifest로 생성)                    │
│    ap-northeast-2a → Pod Subnet 100.64.0.0/17  + node-sg               │
│    ap-northeast-2c → Pod Subnet 100.64.128.0/17 + node-sg              │
│                                                                         │
│  결과: 노드 IP는 10.20.11-12.x / 파드 IP는 100.64.x.x 로 완전 분리       │
└─────────────────────────────────────────────────────────────────────────┘
```

### 4.4 EKS 관리형 Addon

| Addon | 버전 |
|---|---|
| vpc-cni | v1.18.1-eksbuild.1 |
| coredns | (latest managed) |
| kube-proxy | (latest managed) |

### 4.5 Security Group 구조

```
                 클러스터 SG (EKS 자동 생성)
                       ▲
                       │  ingress: All from node-sg
                       │  (Custom Networking pod secondary ENI → cluster SG 통신용)
                       │
         ┌─────────────┴──────────────┐
         │   utterai-prod-eks-node-sg  │
         │                            │
         │   Ingress:                 │
         │   - self (Node-to-Node)    │
         │   - cluster SG: 443        │  Control plane → kubelet API
         │   - cluster SG: 10250      │  Control plane → kubelet
         │   - cluster SG: All        │  pod secondary ENI 통신
         │                            │
         │   Egress: 0.0.0.0/0 All   │
         │                            │
         │   Tag: karpenter.sh/       │
         │        discovery=utterai-  │
         │        prod-eks            │
         └────────────────────────────┘
```

---

## 5. 데이터 레이어

### 5.1 RDS PostgreSQL

```
┌──────────────────────────────────────────────────────────────────────┐
│  RDS Instance: utterai-prod-rds                                      │
│                                                                      │
│  엔진: PostgreSQL 16.9                                               │
│  인스턴스 클래스: db.r6g.large                                        │
│  배치: Private Data Subnet (2a, 2c) — DB Subnet Group                │
│                                                                      │
│  스토리지:                                                           │
│    allocated_storage:     20 GB (gp3)                                │
│    max_allocated_storage: 100 GB (Auto Scaling)                      │
│    암호화: AES-256 (storage_encrypted=true)                           │
│                                                                      │
│  백업:                                                               │
│    retention: 7일 (기본값)                                           │
│    window: 03:00–04:00 UTC (매일)                                    │
│    maintenance: 일요일 04:00–05:00 UTC                               │
│    deletion_protection: true                                         │
│    skip_final_snapshot: false                                        │
│                                                                      │
│  비밀번호 관리:                                                       │
│    manage_master_user_password = true                                │
│    → Terraform state에 비밀번호 미저장                                │
│    → AWS Secrets Manager 자동 관리 + 자동 로테이션                   │
│                                                                      │
│  파라미터:                                                           │
│    log_min_duration_statement = 1000ms (슬로우 쿼리 로깅)            │
│    auto_minor_version_upgrade = true                                 │
│                                                                      │
│  접근 허용: node-sg · cluster-sg (포트 5432)                         │
└──────────────────────────────────────────────────────────────────────┘
```

### 5.2 ElastiCache Redis

```
┌──────────────────────────────────────────────────────────────────────┐
│  Replication Group: utterai-prod-redis                               │
│                                                                      │
│  엔진: Redis 7.1                                                     │
│  노드 타입: cache.r6g.large                                           │
│  노드 수: 2 (Primary + Replica, 각 AZ 분산)                          │
│  배치: Private Data Subnet (2a, 2c) — ElastiCache Subnet Group       │
│                                                                      │
│    AZ 2a: Primary Node (cache.r6g.large)                            │
│    AZ 2c: Replica Node (cache.r6g.large)                            │
│                                                                      │
│  보안:                                                               │
│    at_rest_encryption_enabled:  true                                 │
│    transit_encryption_enabled:  true (TLS)                           │
│    auth_token: Secrets Manager에 저장                                │
│      (/utterai-prod/redis-auth-token → REDIS_AUTH_TOKEN)            │
│                                                                      │
│  접근 허용: node-sg · cluster-sg (포트 6379)                         │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 6. 메시지 큐 (SQS)

### 6.1 AI 파이프라인 큐 체인

```
Backend API
    │  SendMessage
    ▼
┌───────────────────────────────────────────┐
│ utterai-prod-audio-preprocess-queue       │
│   Visibility Timeout: 900s (15분)         │
│   DLQ: utterai-prod-audio-preprocess-dlq  │
│   maxReceiveCount: 3 (기본값)              │
└────────────────────┬──────────────────────┘
                     │  CPU Worker (KEDA 트리거)
                     ▼
              [Stage 1: 오디오 전처리 / VAD]
                     │  SendMessage
                     ▼
┌───────────────────────────────────────────┐
│ utterai-prod-gpu-inference-queue          │
│   Visibility Timeout: 1800s (30분)        │
│   DLQ: utterai-prod-gpu-inference-dlq     │
│   maxReceiveCount: 3                      │
└────────────────────┬──────────────────────┘
                     │  GPU Worker (KEDA 트리거)
                     ▼
         [Stage 2: 화자분리 + ASR 추론]
                     │  SendMessage
                     ▼
┌───────────────────────────────────────────┐
│ utterai-prod-report-analysis-queue        │
│   Visibility Timeout: 900s (15분)         │
│   DLQ: utterai-prod-report-analysis-dlq   │
│   maxReceiveCount: 3                      │
└────────────────────┬──────────────────────┘
                     │  CPU Worker (KEDA 트리거)
                     ▼
       [Stage 3: 지표 계산 + LLM 리포트 생성]
```

### 6.2 RAG Ingest 큐

```
┌───────────────────────────────────────────┐
│ utterai-prod-rag-ingest-queue             │
│   DLQ: utterai-prod-rag-ingest-dlq        │
│   Batch Worker 소비 (문서 임베딩 → RDS)   │
└───────────────────────────────────────────┘
```

### 6.3 Karpenter Interruption 큐

```
utterai-prod-eks  (Karpenter 전용 SQS)
  message_retention: 300s
  EventBridge 규칙:
    - EC2 Spot Instance Interruption Warning
    - EC2 Instance Rebalance Recommendation
    - EC2 Instance State-change Notification
```

### 6.4 SQS 구성 공통 사항

모든 큐: `sqs_managed_sse_enabled = true` (SSE-SQS 암호화)
Dead Letter Queue: 메시지 보존 7일 (`message_retention_seconds = 604800`)

---

## 7. 스토리지 (S3)

### 7.1 버킷 목록

| 버킷 이름 | 용도 |
|---|---|
| `utterai-prod-frontend` | 프론트엔드 정적 파일 (CloudFront 원본) |
| `utterai-prod-raw-audio` | 사용자 업로드 원본 음성 (365일 후 자동 삭제) |
| `utterai-prod-template` | 분석 템플릿 파일 |
| `utterai-prod-rag-ingest` | RAG 임베딩 대상 문서 |
| `utterai-prod-reports` | 최종 분석 리포트 |
| `utterai-prod-kubecost` | Kubecost ETL 비용 데이터 (S3 백엔드) |
| `utterai-prod-loki` | Loki 로그 청크 (S3 백엔드) |
| `utterai-prod-tempo` | Tempo 트레이스 (S3 백엔드, 옵션) |

### 7.2 공통 보안 설정

- 퍼블릭 액세스 차단: `block_public_acls/policy/ignore/restrict` 전부 `true`
- 서버 사이드 암호화: AES-256 (SSE-S3)
- 접근 경로: VPC Endpoint Gateway (인터넷 미경유)

### 7.3 raw-audio 버킷 특이 사항

```
수명주기: 365일 후 객체 자동 삭제 (expire-raw-audio 규칙)
CORS: frontend_domain + 추가 허용 도메인 (GET, PUT, POST 허용)
```

---

## 8. Helm Addon 스택

### 8.1 Addon 전체 목록

| Helm 릴리스 | 차트 버전 | 네임스페이스 | 상태 |
|---|---|---|---|
| aws-load-balancer-controller | 1.8.1 | ingress-system | 활성 |
| kube-prometheus-stack | 66.2.1 | monitoring | 활성 |
| kubecost (cost-analyzer) | (variable) | kubecost | 활성 |
| tempo | (variable) | monitoring | 활성 |
| loki | 7.0.0 | monitoring | 활성 |
| promtail | 6.17.1 | monitoring | 활성 |
| metrics-server | 3.12.1 | kube-system | 활성 |
| external-secrets | 0.10.4 | external-secrets | 활성 |
| aws-efs-csi-driver | 3.0.7 | kube-system | 조건부 (IRSA 설정 시) |
| nvidia-device-plugin | 0.16.2 | kube-system | 활성 (GPU 노드 대상) |
| keda | 2.16.1 | keda | 활성 |
| karpenter | 1.3.3 | karpenter | 활성 |
| argocd (argo-cd) | 9.5.20 | argocd | 활성 |
| cluster-autoscaler | 9.37.0 | kube-system | 비활성 (`enabled=false`) |

### 8.2 KEDA + Karpenter 자동 스케일링 흐름

```
SQS 큐에 메시지 쌓임
        │
        ▼
┌───────────────────────┐
│  KEDA ScaledObject    │  SQS Trigger: 큐 메시지 수 모니터링
│  (SQS 기반 트리거)    │  → Deployment replicas 증가 요청
└────────────┬──────────┘
             │ Pod Pending (노드 부족)
             ▼
┌───────────────────────┐
│  Karpenter NodePool   │  Pod 리소스 요구 분석
│  (1.3.3)              │  → 최적 EC2 인스턴스 선택 + 기동 (~60초)
└────────────┬──────────┘
             │
             ▼
      새 노드 준비 → Pod 스케줄 → 처리 시작

SQS 큐 소진
        │
        ▼
KEDA → replicas 감소 → Pod 종료
        │
        ▼
Karpenter consolidation → 빈 노드 EC2 종료 → 비용 최소화

Karpenter 인터럽션 처리:
  EventBridge → utterai-prod-eks SQS →
  Karpenter가 Spot 인터럽션 사전 감지 + 드레인 + 대체 노드 기동
```

### 8.3 모니터링 스택 세부 설정

**kube-prometheus-stack**

```
Prometheus:
  retention:      3d
  scrapeInterval: 60s
  resourceRequest: cpu=200m / memory=1Gi
  resourceLimit:   cpu=1    / memory=3Gi

  비활성 규칙 (EKS 관리형 컨트롤 플레인 접근 불가):
    etcd / kubeControllerManager / kubeScheduler

Grafana:
  서비스 타입: ClusterIP
  타임존: Asia/Seoul
  외부 DataSource: Loki (uid=loki), Tempo (uid=tempo)
  admin 자격증명: Secrets Manager → ESO → K8s Secret

AlertManager:
  모든 경보 → null receiver (Slack 등 연동 비활성 상태)
```

**Grafana Loki**

```
배포 모드: SingleBinary (replicas=1)
스토리지:  S3 utterai-prod-loki
보존 기간: 336h (14일)
스키마:    tsdb v13 (from 2024-04-01)
```

**Grafana Tempo**

```
배포 모드: 단일 Pod
스토리지:  S3 utterai-prod-tempo
보존 기간: 72h (3일)
수신 프로토콜:
  OTLP gRPC: 0.0.0.0:4317
  OTLP HTTP: 0.0.0.0:4318
```

**Kubecost**

```
Prometheus: 외부 사용 (utterai-monitoring-prometheus:9090)
Grafana:    외부 사용 (proxy=false)
스토리지:  S3 utterai-prod-kubecost (Thanos Object Store)
IRSA:       kubecost ServiceAccount에 kubecost-role 부착
```

**Karpenter**

```
설정:
  clusterName:       utterai-prod-eks
  clusterEndpoint:   <EKS API Endpoint>
  interruptionQueue: utterai-prod-eks (SQS)

System Node에 배치 (taint toleration: CriticalAddonsOnly)
```

**Promtail**

```
로그 전송 대상: http://loki-gateway.monitoring.svc.cluster.local/loki/api/v1/push
DaemonSet 배치: 전 노드 (toleration: CriticalAddonsOnly, dedicated=api/worker/ai-gpu, nvidia.com/gpu)
```

---

## 9. CloudFront · ALB · 트래픽 흐름

### 9.1 프론트엔드 트래픽

```
사용자 브라우저
    │  HTTPS app.utterai.org
    ▼
Route 53 (Hosted Zone: Z06102331M4SC2S9CO5RJ)
    │  A Record Alias + AAAA Record Alias
    ▼
CloudFront Distribution
    │  ACM 인증서 (us-east-1, app.utterai.org)
    │  원본 1: S3 utterai-prod-frontend (정적 파일)
    │  원본 2: ALB (API 요청 프록시)
    ▼
S3 utterai-prod-frontend (SPA 정적 파일 서빙)
```

### 9.2 API 트래픽

```
사용자 브라우저 / 모바일
    │  HTTPS (API 호출)
    ▼
CloudFront → ALB (EKS ingress)
    │  ALB는 ingress.k8s.aws/stack=utterai-prod 태그로 자동 조회
    │  (04-addons에서 data.aws_lb.api로 DNS 자동 감지)
    ▼
EKS 워커 Pod (utterai-api namespace)
    │
    ├── RDS PostgreSQL (5432)
    ├── ElastiCache Redis (6379, TLS)
    └── SQS audio-preprocess-queue (VPC Endpoint 경유)
```

### 9.3 Route 53 레코드

```
app.utterai.org  →  A   (alias CloudFront distribution)
app.utterai.org  →  AAAA (alias CloudFront distribution)
```

---

## 10. 보안 (IAM / IRSA / Secrets)

### 10.1 IRSA 역할 목록 (03-services irsa 모듈)

| IAM Role | 서비스 어카운트 | 권한 범위 |
|---|---|---|
| `utterai-prod-lbc-role` | aws-load-balancer-controller | ALB 생성·관리 |
| `utterai-prod-cluster-autoscaler-role` | cluster-autoscaler | (현재 미사용, Karpenter 대체) |
| `utterai-prod-eso-role` | external-secrets | Secrets Manager GetSecretValue |
| `utterai-prod-keda-role` | keda-operator | SQS 큐 메시지 수 조회 |
| `utterai-prod-karpenter-role` | karpenter | EC2 생성·종료·SQS |
| `utterai-prod-kubecost-role` | kubecost | S3(kubecost 버킷) |
| `utterai-prod-loki-role` | loki | S3(loki 버킷) PutObject/GetObject |
| `utterai-prod-tempo-role` | tempo | S3(tempo 버킷) PutObject/GetObject |

애플리케이션 IRSA (워크로드별):

| IAM Role | 권한 범위 |
|---|---|
| `utterai-prod-api-role` | S3(raw-audio, reports) · SQS(send) · Secrets Manager |
| `utterai-prod-ai-api-role` | SQS(audio-preprocess) SendMessage |
| `utterai-prod-ai-cpu-role` | SQS(audio-preprocess recv/del, gpu-inference send, report-analysis recv/del) · S3 · Bedrock InvokeModel |
| `utterai-prod-ai-ml-gpu-role` | SQS(gpu-inference recv/del, report-analysis send) · S3 |
| `utterai-prod-batch-role` | SQS(rag-ingest recv/del) · S3(rag-ingest, reports) · Secrets Manager |

### 10.2 External Secrets Operator (ESO) 흐름

```
AWS Secrets Manager
  utterai-prod/backend-api-secret
  utterai-prod/redis-auth-token
  utterai-prod/grafana-admin-credentials
         │
         │  IRSA (eso-role) GetSecretValue
         ▼
  ClusterSecretStore (aws-secrets-manager, ap-northeast-2)
         │
         │  refreshInterval: 1h
         ▼
  ExternalSecret CRD (네임스페이스별)
         │
         │  자동 생성/갱신
         ▼
  Kubernetes Secret → Pod envFrom 주입
```

### 10.3 Node IAM 역할 정책

| 정책 | 용도 |
|---|---|
| AmazonEKSWorkerNodePolicy | EKS 워커 노드 기본 |
| AmazonEKS_CNI_Policy | VPC CNI ENI 관리 |
| AmazonEC2ContainerRegistryReadOnly | ECR 이미지 풀 |
| AmazonSSMManagedInstanceCore | SSM Session Manager 접속 |

---

## 11. 모니터링 스택

### 11.1 데이터 수집 흐름

```
┌────────────────────────────────────────────────────────────────────────┐
│  EKS 클러스터 (monitoring namespace)                                   │
│                                                                        │
│  Promtail (DaemonSet)                                                  │
│    모든 노드 로그 수집 → Loki Gateway                                  │
│                                                                        │
│  Prometheus (kube-prometheus-stack)                                    │
│    Pod/Node 메트릭 수집 (60s 간격) → 3일 보존                          │
│    ServiceMonitor: kubecost, tempo, keda, node-exporter …             │
│                                                                        │
│  Grafana                                                               │
│    DataSource 1: Prometheus (메트릭)                                   │
│    DataSource 2: Loki       (로그)                                     │
│    DataSource 3: Tempo      (트레이스)                                 │
│                                                                        │
│  Tempo (OTLP 수신기)                                                   │
│    gRPC :4317 / HTTP :4318 → S3 utterai-prod-tempo (3일 보존)          │
│                                                                        │
│  Loki (SingleBinary)                                                   │
│    S3 utterai-prod-loki (14일 보존)                                    │
│                                                                        │
│  AlertManager                                                          │
│    receiver: null (알림 연동 비활성)                                   │
│    route: Watchdog 알람 → null                                         │
│                                                                        │
│  Kubecost                                                              │
│    Prometheus: utterai-monitoring-prometheus:9090 (외부 연동)          │
│    S3 utterai-prod-kubecost (비용 ETL 데이터)                          │
└────────────────────────────────────────────────────────────────────────┘
```

### 11.2 Grafana Admin 자격증명 관리

```
Secrets Manager: utterai-prod/grafana-admin-credentials
    │  ESO ExternalSecret (refreshInterval 1h)
    ▼
K8s Secret: grafana-admin-credentials (monitoring namespace)
    │  kube-prometheus-stack grafana.admin.existingSecret
    ▼
Grafana Pod 환경변수 주입
```

---

## 참고 문서

- 운영 절차·명령어: [`docs/prod/README.md`](./README.md)
- 보안 상세: [`docs/prod/security.md`](./security.md)
- Dev vs Prod 비교: [`docs/README.md`](../README.md)
- EKS 아키텍처 흐름: [`docs/shared/eks-architecture-flow.md`](../shared/eks-architecture-flow.md)
- KEDA → Karpenter 전환: [`docs/dev/keda-karpenter-transition.md`](../dev/keda-karpenter-transition.md)
- 마이그레이션 체크리스트: [`docs/prod/migration-checklist.md`](./migration-checklist.md)
