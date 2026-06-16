# UtterAI Prod 아키텍처

> AWS ap-northeast-2 (Seoul Primary) · EKS 1.31 · Terraform 4-Layer State

---

## 목차

1. [전체 아키텍처 개요](#1-전체-아키텍처-개요)
2. [AWS 계정 구조](#2-aws-계정-구조)
3. [네트워크 아키텍처](#3-네트워크-아키텍처)
4. [트래픽 흐름](#4-트래픽-흐름)
5. [EKS 클러스터 아키텍처](#5-eks-클러스터-아키텍처)
6. [AI 분석 파이프라인](#6-ai-분석-파이프라인)
7. [자동 스케일링 구조](#7-자동-스케일링-구조)
8. [데이터 레이어](#8-데이터-레이어)
9. [보안 아키텍처](#9-보안-아키텍처)
10. [CI/CD 파이프라인](#10-cicd-파이프라인)
11. [DR 아키텍처](#11-dr-아키텍처)
12. [모니터링 아키텍처](#12-모니터링-아키텍처)

---

## 1. 전체 아키텍처 개요

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         INTERNET                                        │
└──────────────┬──────────────────────────────┬───────────────────────────┘
               │  utterai.com                 │  api.utterai.com
               ▼                              ▼
         ┌───────────┐                  ┌───────────┐
         │  Route 53 │                  │  Route 53 │
         │  (Alias)  │                  │  (Alias)  │
         └─────┬─────┘                  └─────┬─────┘
               │                              │
               ▼                              ▼
     ┌─────────────────┐         ┌────────────────────────┐
     │   CloudFront    │         │  WAF WebACL (ALB 부착) │
     │  (OAC, HTTPS)   │         │  OWASP / Rate-Limit    │
     └────────┬────────┘         └────────────┬───────────┘
              │                               │
              ▼                               ▼
   ┌──────────────────┐         ┌─────────────────────────┐
   │  S3 Static       │         │  ALB Ingress            │
   │  Frontend        │         │  (internet-facing)      │
   │  (utterai-prod-  │         │  Public Subnet 3개      │
   │   frontend)      │         └────────────┬────────────┘
   └──────────────────┘                      │
                                             │
┌────────────────────────────────────────────▼────────────────────────┐
│                    EKS Cluster (utterai-prod-eks)                       │
│                                                                         │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────────┐  │
│  │  utterai-api     │  │  utterai-ai-api  │  │  utterai-ai-cpu      │  │
│  │  Backend API     │  │  Internal AI API │  │  CPU Worker          │  │
│  │  (HPA: 3~10)     │  │  (SQS producer)  │  │  (KEDA + Karpenter)  │  │
│  └────────┬─────────┘  └────────┬─────────┘  └──────────┬───────────┘  │
│           │                     │                        │              │
│  ┌────────▼─────────────────────▼──────────┐  ┌─────────▼───────────┐  │
│  │  utterai-ai-gpu                         │  │  utterai-batch      │  │
│  │  GPU Worker (KEDA + Karpenter)          │  │  Batch Worker       │  │
│  └─────────────────────────────────────────┘  └─────────────────────┘  │
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │  utterai-observability  (OTel Collector DaemonSet)              │    │
│  └─────────────────────────────────────────────────────────────────┘    │
└───────────┬───────────────┬────────────────┬───────────────┬────────────┘
            │               │                │               │
    ┌───────▼──────┐ ┌──────▼──────┐ ┌──────▼──────┐ ┌─────▼──────────┐
    │   Aurora     │ │    Redis    │ │     S3      │ │  SQS Queues    │
    │ PostgreSQL   │ │  (Cluster)  │ │  (6 Bucket) │ │  (4 Queues)    │
    │ (Multi-AZ)   │ │  (Multi-AZ) │ │             │ │                │
    └──────────────┘ └─────────────┘ └─────────────┘ └────────────────┘
```

---

## 2. AWS 계정 구조

```
┌──────────────────────┐   ┌──────────────────────┐   ┌──────────────────────┐
│    Dev Account       │   │  Shared Tooling       │   │    Prod Account      │
│                      │   │  Account              │   │                      │
│  utterai-dev-vpc     │   │  GitHub Actions       │   │  utterai-prod-vpc    │
│  utterai-dev-eks     │   │  Amazon ECR           │   │  utterai-prod-eks    │
│  Aurora (dev)        │   │  Argo CD              │   │  Aurora (prod)       │
│  Redis (dev)         │   │  Terraform State      │   │  Redis (prod)        │
│  SQS (dev)           │   │  CloudWatch Sink      │   │  SQS (prod)          │
│  Cognito (dev)       │   │  Grafana              │   │  Cognito (prod)      │
└──────────────────────┘   └──────────┬───────────┘   └──────────┬───────────┘
                                      │  Cross-Account              │
                                      │  AssumeRole                 │
                                      └────────────────────────────►│
                                                                     │
                                                       Tokyo DR (ap-northeast-1)
                                                       ┌─────────────▼──────────┐
                                                       │  Standby EKS           │
                                                       │  Aurora Global DB      │
                                                       │  S3 CRR Replica        │
                                                       │  ECR Mirror            │
                                                       └────────────────────────┘
```

**계정 분리 이유**
- CI/CD 도구(GitHub Actions, Argo CD) 침해 시 Prod 데이터 직접 접근 불가
- Dev / Prod는 AWS 계정 경계로 완전히 격리
- Shared Tooling Account 1개가 두 환경을 공통 관리 → 운영 효율화

---

## 3. 네트워크 아키텍처

### 3.1 VPC 구조 (10.0.0.0/16)

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  VPC: utterai-prod-vpc  (10.0.0.0/16)                                       │
│                                                                              │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐             │
│  │  AZ: 2a         │  │  AZ: 2b         │  │  AZ: 2c         │             │
│  │                 │  │                 │  │                 │             │
│  │ ┌─────────────┐ │  │ ┌─────────────┐ │  │ ┌─────────────┐ │             │
│  │ │Public Subnet│ │  │ │Public Subnet│ │  │ │Public Subnet│ │             │
│  │ │10.0.1.0/24  │ │  │ │10.0.2.0/24  │ │  │ │10.0.3.0/24  │ │             │
│  │ │  ALB        │ │  │ │  ALB        │ │  │ │  ALB        │ │             │
│  │ │  NAT GW ①  │ │  │ │  NAT GW ②  │ │  │ │  NAT GW ③  │ │             │
│  │ └─────────────┘ │  │ └─────────────┘ │  │ └─────────────┘ │             │
│  │                 │  │                 │  │                 │             │
│  │ ┌─────────────┐ │  │ ┌─────────────┐ │  │ ┌─────────────┐ │             │
│  │ │Private App  │ │  │ │Private App  │ │  │ │Private App  │ │             │
│  │ │10.0.11.0/24 │ │  │ │10.0.12.0/24 │ │  │ │10.0.13.0/24 │ │             │
│  │ │  EKS Pods   │ │  │ │  EKS Pods   │ │  │ │  EKS Pods   │ │             │
│  │ └─────────────┘ │  │ └─────────────┘ │  │ └─────────────┘ │             │
│  │                 │  │                 │  │                 │             │
│  │ ┌─────────────┐ │  │ ┌─────────────┐ │  │ ┌─────────────┐ │             │
│  │ │Private Data │ │  │ │Private Data │ │  │ │Private Data │ │             │
│  │ │10.0.21.0/24 │ │  │ │10.0.22.0/24 │ │  │ │10.0.23.0/24 │ │             │
│  │ │  Aurora     │ │  │ │  Aurora     │ │  │ │  Aurora     │ │             │
│  │ │  Redis      │ │  │ │  Redis      │ │  │ │  Redis      │ │             │
│  │ └─────────────┘ │  │ └─────────────┘ │  │ └─────────────┘ │             │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘             │
│                                                                              │
│  VPC Endpoints (트래픽이 인터넷 미경유)                                        │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │  Gateway: S3                                                         │   │
│  │  Interface: SQS · Secrets Manager · CloudWatch Logs · ECR API       │   │
│  │            ECR Docker · STS · KMS                                    │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────────────┘
```

> NAT Gateway를 AZ별 1개씩 3개 배치 → 특정 AZ 장애 시에도 해당 AZ 파드의 아웃바운드 트래픽 유지

### 3.2 보안 그룹 흐름

```
Internet (0.0.0.0/0 : 443)
        │
   ┌────▼──────────────────────┐
   │  WAF WebACL               │  ALB에 부착
   │  - OWASP Managed Rules    │  (api.utterai.com 진입점)
   │  - Rate-Limit: 2000/5min  │
   └────┬──────────────────────┘
        │
   ┌────▼────────┐
   │  sg-prod-alb │  0.0.0.0/0 : 443
   └────┬────────┘
        │ :8000
   ┌────▼──────────────┐
   │  sg-prod-backend  │  (EKS Pod, Private App Subnet)
   └────┬──────┬───────┘
        │      │
   :5432│      │:6379
   ┌────▼───┐  ┌▼────────────┐
   │sg-prod │  │ sg-prod     │
   │-aurora │  │ -redis      │
   └────────┘  └─────────────┘
```

---

## 4. 트래픽 흐름

### 4.1 사용자 요청 흐름

**프론트엔드 (utterai.com)**
```
Browser ── HTTPS utterai.com ──► Route 53 (A Alias)
                                       │
                                       ▼
                               CloudFront Distribution
                               ACM 인증서 (us-east-1)
                               OAC (S3 직접접근 차단)
                                       │
                                       ▼
                           S3 utterai-prod-frontend
                           (정적 파일 서빙 / SPA 라우팅)
```

**백엔드 API (api.utterai.com)**
```
Browser ── HTTPS api.utterai.com ──► Route 53 (A Alias)
                                            │
                                            ▼
                                   WAF WebACL (ALB 부착)
                                   OWASP / Rate-Limit
                                            │
                                            ▼
                                   ALB Ingress (internet-facing)
                                   ACM 인증서 / HTTPS:443
                                   TLS Termination
                                            │
                                            ▼
                                   utterai-api namespace
                                   Backend API Pods (3~10개, HPA)
                                            │
                           ┌────────────────┼──────────────────┐
                           │                │                  │
                      ┌────▼───┐       ┌────▼────┐  ┌─────────▼──────┐
                      │Cognito │       │ Aurora  │  │ Redis          │
                      │JWT 검증│       │PostgreSQL│  │ Session/Cache  │
                      └────────┘       └────┬────┘  └────────────────┘
                                            │
                                   ┌────────▼──────────┐
                                   │ SQS               │
                                   │ audio-preprocess  │
                                   │ -queue            │
                                   └────────┬──────────┘
                                            │
                                       AI Pipeline
                                       (섹션 6 참고)
```

### 4.2 음성 파일 업로드 흐름

```
Client
  │  POST /api/v1/audio/upload-url
  ▼
Backend API
  │  S3 Presigned PUT URL 생성 (만료: 900초)
  │  (utterai-prod-raw-audio)
  ▼
Client
  │  PUT (Presigned URL) → S3 직접 업로드
  ▼
S3 utterai-prod-raw-audio
  │
  ▼
Backend API: POST /api/v1/analysis/jobs  (분석 요청 생성)
  │  analysis_jobs 레코드 생성 (Aurora)
  │  SQS audio-preprocess-queue 발행
  ▼
AI Pipeline 시작
```

---

## 5. EKS 클러스터 아키텍처

### 5.1 NodeGroup 구성

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  EKS Cluster: utterai-prod-eks (Kubernetes 1.31)                            │
│  Control Plane Endpoint: Private Only                                        │
│                                                                              │
│  ┌─────────────────────────────┐  ┌─────────────────────────────────────┐   │
│  │  prod-system-nodegroup       │  │  prod-api-nodegroup                 │   │
│  │  (EKS Managed NodeGroup)     │  │  (EKS Managed NodeGroup + HPA)      │   │
│  │  t3.large / On-Demand        │  │  t3.xlarge / On-Demand              │   │
│  │  2 ~ 3 nodes (상시)          │  │  2 ~ 5 nodes (HPA 연동)             │   │
│  │                              │  │                                     │   │
│  │  - CoreDNS                   │  │  - utterai-api (Backend REST API)   │   │
│  │  - ALB Controller            │  │  - Pod Anti-Affinity 적용           │   │
│  │  - Karpenter Controller      │  │  - RollingUpdate (maxUnavailable:0) │   │
│  │  - KEDA Operator             │  │                                     │   │
│  │  - ESO Operator              │  └─────────────────────────────────────┘   │
│  │  - Kyverno                   │                                            │
│  │  - Cilium                    │  ┌─────────────────────────────────────┐   │
│  └─────────────────────────────┘  │  cpu-worker-nodepool                 │   │
│                                    │  (Karpenter NodePool + KEDA)         │   │
│                                    │  c5.2xlarge / c5.4xlarge 자동 선택   │   │
│                                    │  0 ~ 4 nodes (수요 기반)             │   │
│                                    │                                     │   │
│                                    │  - utterai-ai-cpu (CPU Worker)      │   │
│                                    │  - utterai-ai-api (Internal AI API) │   │
│                                    └─────────────────────────────────────┘   │
│                                                                              │
│                                    ┌─────────────────────────────────────┐   │
│                                    │  gpu-worker-nodepool                 │   │
│                                    │  (Karpenter NodePool + KEDA)         │   │
│                                    │  g4dn.xlarge / g4dn.2xlarge 자동 선택│   │
│                                    │  0 ~ 3 nodes (수요 기반)             │   │
│                                    │                                     │   │
│                                    │  - utterai-ai-gpu (GPU Worker)      │   │
│                                    │  - utterai-batch (Batch Worker)     │   │
│                                    └─────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 5.2 Namespace 구성

```
utterai-prod-eks
│
├── utterai-api              Backend REST API (외부 트래픽)
│     └── backend-api-sa     IRSA: S3, SQS, SecretsManager, CloudWatch
│
├── utterai-ai-api           Internal AI API (클러스터 내부 전용)
│     └── utterai-ai-api-sa  IRSA: SQS(audio-preprocess) SendMessage
│
├── utterai-ai-cpu           CPU 기반 음성 전처리 워커
│     └── utterai-cpu-worker-sa  IRSA: SQS(2개 큐), S3, Bedrock
│
├── utterai-ai-gpu           GPU 기반 ML/LLM 추론 워커
│     └── utterai-ml-gpu-worker-sa  IRSA: SQS(2개 큐), S3
│
├── utterai-batch            RAG 문서 ingest 배치 워커
│     └── utterai-batch-worker-sa  IRSA: SQS(rag-ingest), S3, SecretsManager
│
└── utterai-observability    OTel Collector 등 모니터링 스택
```

---

## 6. AI 분석 파이프라인

### 6.1 SQS 큐 체인

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                        AI Analysis Pipeline                                  │
│                                                                              │
│  Backend API                                                                 │
│  (utterai-api)                                                               │
│       │                                                                      │
│       │ SendMessage                                                          │
│       ▼                                                                      │
│  ┌────────────────────────────┐                                              │
│  │ audio-preprocess-queue     │  VisibilityTimeout: 300s                    │
│  │ DLQ: audio-preprocess-dlq  │  maxReceiveCount: 3                         │
│  └────────────┬───────────────┘                                              │
│               │ ReceiveMessage / DeleteMessage                               │
│               ▼                                                              │
│  ┌────────────────────────────┐                                              │
│  │  CPU Worker                │  Stage 1: VAD + 오디오 전처리               │
│  │  (utterai-ai-cpu)          │  → 중간 결과 S3 저장                        │
│  └────────────┬───────────────┘  → processed.wav / vad_segments.json       │
│               │ SendMessage                                                  │
│               ▼                                                              │
│  ┌────────────────────────────┐                                              │
│  │ gpu-inference-queue        │  VisibilityTimeout: 600s                    │
│  │ DLQ: gpu-inference-dlq     │  maxReceiveCount: 3                         │
│  └────────────┬───────────────┘                                              │
│               │ ReceiveMessage / DeleteMessage                               │
│               ▼                                                              │
│  ┌────────────────────────────┐                                              │
│  │  GPU Worker                │  Stage 2: 화자분리(pyannote) + ASR(Whisper) │
│  │  (utterai-ai-gpu)          │  → S3 저장 + transcript draft 작성          │
│  └────────────┬───────────────┘  → speaker_segments / asr_result.json      │
│               │ SendMessage                                                  │
│               ▼                                                              │
│  ┌────────────────────────────┐                                              │
│  │ report-analysis-queue      │  VisibilityTimeout: 600s                    │
│  │ DLQ: report-analysis-dlq   │  maxReceiveCount: 3                         │
│  └────────────┬───────────────┘                                              │
│               │ ReceiveMessage / DeleteMessage                               │
│               ▼                                                              │
│  ┌────────────────────────────┐                                              │
│  │  CPU Worker (LLM Stage)    │  Stage 3: 지표 계산 + RAG + EXAONE 리포트  │
│  │  (utterai-ai-cpu)          │  → 최종 리포트 S3 저장                      │
│  └────────────────────────────┘  → reports/{session_id}/{job_id}.json      │
│                                                                              │
│  ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─   │
│                                                                              │
│  RAG Ingest (별도 파이프라인)                                                │
│  ┌────────────────────────────┐                                              │
│  │ rag-ingest-queue           │  관리자가 기준 문서 업로드 시 발행          │
│  │ DLQ: rag-ingest-dlq        │                                             │
│  └────────────┬───────────────┘                                              │
│               ▼                                                              │
│  ┌────────────────────────────┐                                              │
│  │  Batch Worker              │  문서 임베딩 → pgvector 저장               │
│  │  (utterai-batch)           │  KURE 임베딩 모델 사용                      │
│  └────────────────────────────┘                                              │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 6.2 S3 중간 결과 저장 구조

```
utterai-prod-raw-audio
  └── {user_upload_key}                      ← 사용자 원본 음성

utterai-prod-processed-audio
  └── intermediate/{session_id}/{job_id}/
        ├── processed.wav                    ← CPU Stage 전처리 결과
        ├── vad_segments.json                ← VAD 결과
        ├── speaker_segments.json            ← GPU Stage 화자분리 결과
        └── asr_result.json                  ← GPU Stage ASR 결과

utterai-prod-reports
  ├── transcript-drafts/{session_id}/{job_id}/
  │     └── transcript_draft.json            ← GPU Stage 초안
  └── reports/{session_id}/{job_id}.json     ← 최종 분석 리포트
```

---

## 7. 자동 스케일링 구조

### 7.1 KEDA + Karpenter 연동 흐름

```
                     SQS 메시지 증가
                           │
              ┌────────────▼────────────┐
              │  KEDA ScaledObject      │
              │  (SQS Trigger)          │
              │  queueLength: 5         │  메시지 5개당 Pod 1개
              └────────────┬────────────┘
                           │ replicas 증가 요청
                           ▼
              ┌────────────────────────┐
              │  Deployment            │
              │  (cpu-worker /         │
              │   gpu-worker)          │
              └────────────┬───────────┘
                           │ 신규 Pod → Pending (노드 부족)
                           ▼
              ┌────────────────────────┐
              │  Karpenter             │
              │  NodePool              │  Pod 리소스 요청 분석
              └────────────┬───────────┘  → 최적 인스턴스 자동 선택
                           │ EC2 기동 (~60초)
                           ▼
              ┌────────────────────────┐
              │  새 Node 준비 완료     │
              └────────────┬───────────┘
                           │ Pod 스케줄링
                           ▼
                    분석 처리 시작


                     SQS 큐 소진
                           │
              ┌────────────▼────────────┐
              │  KEDA                   │
              │  replicas → 0/1         │
              └────────────┬────────────┘
                           │ Pod 종료
                           ▼
              ┌────────────────────────┐
              │  Karpenter             │
              │  consolidateAfter: 30s │  빈 노드 감지
              └────────────┬───────────┘
                           │ EC2 종료 → 과금 중단
                           ▼
                      비용 최소화
```

### 7.2 ScaledObject 구성

| Worker | Trigger 큐 | queueLength | minReplica | maxReplica |
|---|---|---|---|---|
| cpu-worker | audio-preprocess-queue | 5 | 1 | 4 |
| gpu-worker | gpu-inference-queue | 1 | 0 | 3 |
| batch-worker | rag-ingest-queue | 1 | 0 | 2 |

---

## 8. 데이터 레이어

### 8.1 Aurora PostgreSQL

```
┌─────────────────────────────────────────────────────────────┐
│  Aurora Cluster: utterai-prod-aurora                        │
│  Engine: Aurora PostgreSQL 16                               │
│  암호화: KMS CMK (prod-aurora-kms-key)                       │
│                                                             │
│  ┌───────────────────────┐  ┌──────────────────────────┐   │
│  │  Writer Instance      │  │  Reader Instance         │   │
│  │  db.r6g.large         │  │  db.r6g.large            │   │
│  │  AZ: ap-northeast-2a  │  │  AZ: ap-northeast-2c     │   │
│  └───────────┬───────────┘  └──────────┬───────────────┘   │
│              │                         │                   │
│              └────────────┬────────────┘                   │
│                           │ Aurora Storage (6-way 복제)     │
│                           │                                 │
│  비밀번호 관리: manage_master_user_password = true           │
│  → Terraform state에 비밀번호 미저장                         │
│  → AWS가 Secrets Manager에 자동 저장 + 90일 자동 교체        │
└─────────────────────────────────────────────────────────────┘

연결 방식:
  Backend API Pod
    └── SQLAlchemy (pool_size=10, max_overflow=20)
          ├── Writer: cluster endpoint (DDL / Write)
          └── Reader: cluster-ro endpoint (조회)
```

### 8.2 ElastiCache Redis

```
┌─────────────────────────────────────────────────────────────┐
│  Replication Group: utterai-prod-redis                      │
│  Engine: Redis 7.1 / TLS + AUTH Token 필수                  │
│  암호화: KMS (AWS Managed Key)                               │
│                                                             │
│  ┌───────────────────────┐  ┌──────────────────────────┐   │
│  │  Primary Node         │  │  Replica Node            │   │
│  │  cache.r6g.large      │  │  cache.r6g.large         │   │
│  │  AZ: ap-northeast-2a  │  │  AZ: ap-northeast-2c     │   │
│  └───────────────────────┘  └──────────────────────────┘   │
│                                                             │
│  AUTH Token: Terraform random_password → Secrets Manager   │
│  ※ 현재 state 파일에 노출 → Terraform 1.10 ephemeral 전환 예정  │
└─────────────────────────────────────────────────────────────┘
```

### 8.3 S3 버킷 구성

```
utterai-prod-frontend         프론트엔드 정적 파일 (CloudFront OAC)
utterai-prod-raw-audio        사용자 업로드 원본 음성 (Presigned URL)
utterai-prod-processed-audio  AI 처리 중간 결과 (30일 후 자동 삭제)
utterai-prod-documents        RAG 기준 문서 (관리자 업로드)
utterai-prod-reports          최종 분석 리포트 (영구 보존)
utterai-prod-artifacts        분석 JSON 결과 (1년 후 Glacier)

공통 설정:
  - 퍼블릭 액세스 차단: 전체 활성화
  - 서버 사이드 암호화: KMS
  - VPC Endpoint Gateway 경유 (인터넷 미사용)
```

---

## 9. 보안 아키텍처

### 9.1 IRSA (IAM Roles for Service Accounts)

```
EKS OIDC Provider
       │
       │  신뢰 관계 (Web Identity)
       ▼
┌──────────────────────────────────────────────────────────────┐
│  Platform IRSA (3개)                                         │
│                                                              │
│  lbc-irsa-role          ALB 생성/관리                        │
│  cluster-autoscaler     (미사용, Karpenter 대체)              │
│  eso-irsa-role          SecretsManager GetSecretValue        │
└──────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────┐
│  Application IRSA (5개)                                      │
│                                                              │
│  api-irsa-role          S3(raw-audio/reports) · SQS(send)   │
│                          SecretsManager · CloudWatch         │
│                                                              │
│  ai-api-irsa-role        SQS(audio-preprocess) SendMessage   │
│                                                              │
│  ai-cpu-irsa-role        SQS(audio-preprocess) Recv/Del      │
│                          SQS(gpu-inference) Send             │
│                          SQS(report-analysis) Recv/Del       │
│                          S3 · Bedrock InvokeModel            │
│                                                              │
│  ai-ml-gpu-irsa-role     SQS(gpu-inference) Recv/Del         │
│                          SQS(report-analysis) Send           │
│                          S3                                  │
│                                                              │
│  batch-irsa-role         SQS(rag-ingest) Recv/Del            │
│                          S3(reports) PutObject               │
│                          SecretsManager (db-password)        │
└──────────────────────────────────────────────────────────────┘
```

### 9.2 External Secrets Operator (ESO) 흐름

```
┌──────────────────────────────────────────────────────────────┐
│  AWS Secrets Manager                                         │
│                                                              │
│  utterai-prod/backend-api-secret                            │
│    DB_PASSWORD · JWT_SECRET_KEY                              │
│    INTERNAL_CALLBACK_TOKEN · INTERNAL_CALLBACK_HMAC_SECRET  │
│                                                              │
│  utterai-prod/redis-auth-token                              │
│    REDIS_AUTH_TOKEN                                          │
│                                                              │
│  utterai-prod/ai-worker-secret                              │
│    DB_USER · DB_PASSWORD · DB_HOST · DB_PORT · DB_NAME       │
│                                                              │
│  utterai-prod/gpu-worker-secret                             │
│    (GPU Worker 전용 시크릿)                                  │
└───────────────────────┬──────────────────────────────────────┘
                        │  eso-irsa-role (IRSA)
                        │  GetSecretValue
                        ▼
             ┌──────────────────────┐
             │  ClusterSecretStore  │  aws-secrets-manager
             │  (ESO CRD)           │  region: ap-northeast-2
             └──────────┬───────────┘
                        │  refreshInterval: 1h
                        ▼
             ┌──────────────────────┐
             │  ExternalSecret      │  네임스페이스별 개별 적용
             │  (ESO CRD)           │
             └──────────┬───────────┘
                        │ 자동 생성/갱신
                        ▼
             ┌──────────────────────┐
             │  K8s Secret          │  Pod에 envFrom 주입
             │  (각 Namespace)      │
             └──────────────────────┘
```

### 9.3 KMS 암호화 전략

```
┌─────────────────────────────────────────────────────────────┐
│  Aurora PostgreSQL  →  CMK (prod-aurora-kms-key)            │
│    이유: Cross-Account DR(Tokyo) 복호화 시 CMK 필수         │
│                                                             │
│  ElastiCache Redis  →  AWS Managed Key (aws/elasticache)    │
│  SQS Queues (4개)   →  AWS Managed Key (aws/sqs)            │
│  Secrets Manager    →  AWS Managed Key (aws/secretsmanager) │
│  S3 Buckets (6개)   →  AWS Managed Key (aws/s3)             │
│  CloudWatch Logs    →  KMS Key (prod-logs-kms-key)          │
└─────────────────────────────────────────────────────────────┘
```

### 9.4 Pod 보안 계층

```
계층 1 — Pod Security Standards (Namespace 레이블)
  utterai-api        enforce: restricted  (root 불허, 최소 권한)
  utterai-ai-*       enforce: baseline    (GPU Device Plugin 호환)

계층 2 — Kyverno ClusterPolicy
  require-resource-limits     limits 없는 Pod 배포 거부
  disallow-latest-tag         :latest 이미지 태그 거부
  restrict-gpu-to-ai-worker   GPU 리소스는 ai-worker namespace만
  generate-default-networkpolicy  Namespace 생성 시 default-deny 자동 생성

계층 3 — Cilium (eBPF 기반)
  CiliumNetworkPolicy         L3/L4/L7 트래픽 화이트리스트
  mTLS                        utterai-api ↔ utterai-ai-* 간 상호 인증
  Hubble                      실시간 트래픽 가시성
```

---

## 10. CI/CD 파이프라인

### 10.1 전체 흐름

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                   Shared Tooling Account                                     │
│                                                                              │
│  개발자  ──── PR ────►  GitHub                                               │
│                          │                                                   │
│                          │ main 머지 (리뷰 1명 이상 필수)                    │
│                          ▼                                                   │
│              GitHub Actions (prod-deploy.yaml)                               │
│                  │                                                           │
│                  ├─ 단위 테스트 / 통합 테스트                                │
│                  ├─ Docker Build                                              │
│                  ├─ ECR Push (utterai-prod-backend:{git_sha})                │
│                  │    Seoul (ap-northeast-2) ECR                             │
│                  ├─ ECR Cross-Region Copy                                    │
│                  │    Tokyo (ap-northeast-1) ECR  ← DR용                    │
│                  ├─ 이미지 취약점 스캔 (ECR Scanning)                        │
│                  └─ K8s Manifest 이미지 태그 업데이트                        │
│                          │                                                   │
│                          ▼                                                   │
│              Argo CD  (GitOps)                                               │
│                  │  Git 저장소 감시 (overlays/prod)                          │
│                  │  Prod: Auto-Sync 비활성화 → 담당자 수동 Sync              │
│                  │                                                           │
└──────────────────┼───────────────────────────────────────────────────────────┘
                   │ Cross-Account AssumeRole
                   ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                   Prod Account                                               │
│                                                                              │
│              EKS utterai-prod-eks                                            │
│                  │  Rolling Update (maxUnavailable: 0)                       │
│                  │  PDB: minAvailable 2                                      │
│                  ▼                                                           │
│              utterai-api namespace                                           │
│              backend-api Deployment (새 이미지 배포)                         │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 10.2 Terraform 4-Layer 상태 관리

```
terraform/environments/prod/
│
├── 01-network/    VPC · 서브넷 · NAT GW · 보안그룹 · VPC Endpoint
│   State: s3://utterai-prod-terraform-state/prod/01-network/
│
├── 02-eks/        EKS 클러스터 · NodeGroup · OIDC Provider
│   State: s3://utterai-prod-terraform-state/prod/02-eks/
│   Depends on: 01-network outputs (vpc_id, subnet_ids)
│
├── 03-services/   Aurora · Redis · S3 · SQS · Cognito · IRSA · Secrets
│   State: s3://utterai-prod-terraform-state/prod/03-services/
│   Depends on: 01-network, 02-eks outputs
│
└── 04-addons/     ALB Controller · Karpenter · KEDA · ESO · Kyverno · Cilium
    State: s3://utterai-prod-terraform-state/prod/04-addons/
    Depends on: 02-eks, 03-services outputs

State Lock: S3 Native Locking (DynamoDB 불필요, Terraform 1.10+)
State 암호화: KMS
```

---

## 11. DR 아키텍처

### 11.1 Tokyo Warm Standby 구성

```
Seoul (ap-northeast-2) Primary          Tokyo (ap-northeast-1) DR
──────────────────────────────          ──────────────────────────────
utterai-prod-alb                        utterai-dr-alb
    │                                       │
Route 53 Health Check                  Route 53 Failover Record
(30초 간격, 3회 실패 시 발동) ─────────►(자동 전환)

utterai-prod-eks                        utterai-dr-eks
    (운영 규모)                              (최소 Standby 노드)

Aurora utterai-prod-aurora              Aurora utterai-dr-aurora
Writer + Reader                         Global DB Secondary
    │                                       │
    └── Global DB Replication ──────────────┘
        (복제 지연 < 1초)

S3 utterai-prod-raw-audio       ──CRR──► S3 utterai-dr-raw-audio
S3 utterai-prod-reports         ──CRR──► S3 utterai-dr-reports
S3 utterai-prod-artifacts       ──CRR──► S3 utterai-dr-artifacts

ECR Seoul utterai-prod-backend  ──────► ECR Tokyo utterai-prod-backend
    (GitHub Actions CI에서 이미지 빌드 후 자동 복사)
```

### 11.2 DR 전환 절차

```
1. Route 53 Health Check 실패 감지 (90초 이내)
      │
      ▼
2. Failover Record 활성화 → api.utterai.com → Tokyo ALB로 자동 전환
      │
      ▼
3. Aurora Global DB Promote
   AWS Console / CLI: aws rds failover-global-cluster
      │
      ▼
4. Tokyo EKS DB_HOST 환경변수 → Tokyo Writer Endpoint로 전환
   Argo CD 통해 K8s ConfigMap 업데이트 + 재배포
      │
      ▼
5. Tokyo EKS 스케일 업 (최소 → 운영 규모)
      │
      ▼
6. CloudWatch 알람 정상화 확인
   목표 RTO: 30분 이내 / RPO: 1분 이내
```

---

## 12. 모니터링 아키텍처

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  EKS Cluster                                                                 │
│                                                                              │
│  ┌──────────────────────────────────────────────┐                           │
│  │  utterai-observability namespace             │                           │
│  │                                              │                           │
│  │  OTel Collector (DaemonSet)                  │                           │
│  │  - 각 노드에서 Trace / Metrics / Logs 수집    │                           │
│  │  - Sampling: 정상 10% / 에러 100%            │                           │
│  └────────┬─────────────────────────────────────┘                           │
│           │                                                                  │
└───────────┼──────────────────────────────────────────────────────────────────┘
            │
     ┌──────┴─────────────────┐
     │                        │
     ▼                        ▼
┌──────────────┐      ┌───────────────────────────────┐
│  CloudWatch  │      │  Shared Tooling Account        │
│  (Prod Acct) │      │                               │
│              │      │  ┌──────────────────────────┐ │
│  Metrics     │      │  │  Grafana                 │ │
│  Logs        │      │  │                          │ │
│  Alarms(10개)│      │  │  DataSource:             │ │
│  Dashboard   │      │  │  - CloudWatch (메트릭)    │ │
└──────────────┘      │  │  - OTel/Tempo (Trace)    │ │
                      │  │  - Aurora Perf Insights   │ │
                      │  │                          │ │
                      │  │  Dashboard:              │ │
                      │  │  - utterai-prod-overview  │ │
                      │  │  - ai-pipeline           │ │
                      │  │  - db-detail             │ │
                      │  └──────────────────────────┘ │
                      └───────────────────────────────┘

주요 알람:
  prod-backend-5xx-rate       5xx 비율 > 5% (5분)   → Discord + 온콜
  prod-aurora-replica-lag     Replica Lag > 5초      → Discord + 온콜
  prod-sqs-dlq-count          DLQ 메시지 수 > 0      → Discord + 온콜
  prod-backend-latency-p95    p95 > 2초 (5분)        → Discord
  prod-aurora-cpu             CPU > 70% (5분)        → Discord
  prod-redis-memory           메모리 > 80% (5분)     → Discord
```

---

## 참고

- 운영 가이드 (설정값·명령어): [`docs/prod/README.md`](./README.md)
- Dev vs Prod 환경 비교: [`docs/README.md`](../README.md)
- Dev 환경 가이드: [`docs/dev/README.md`](../dev/README.md)
- EKS 플랫폼 구현 방향: [`docs/shared/eks-architecture-flow.md`](../shared/eks-architecture-flow.md)
- Prod 마이그레이션 체크리스트: [`docs/prod/migration-checklist.md`](./migration-checklist.md)
