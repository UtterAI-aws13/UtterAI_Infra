# UtterAI Prod 아키텍처

> AWS ap-northeast-2 (Seoul) · EKS 1.31 · Terraform 5-Layer State
> 이 문서는 코드(Terraform/K8s manifest)뿐 아니라 **2026-07-04 기준 실제 AWS/클러스터 조회 결과**로 교차검증한 내용을 담고 있다. 코드상으로만 존재하고 아직 배포되지 않은 부분은 명시적으로 표시했다.

---

## 목차

1. [전체 아키텍처 개요](#1-전체-아키텍처-개요)
2. [Terraform 레이어 구조](#2-terraform-레이어-구조)
3. [네트워크 아키텍처](#3-네트워크-아키텍처)
4. [EKS 클러스터](#4-eks-클러스터)
5. [Karpenter 오토스케일링 (NodePool)](#5-karpenter-오토스케일링-nodepool)
6. [데이터 레이어](#6-데이터-레이어)
7. [메시지 큐 (SQS)](#7-메시지-큐-sqs)
8. [스토리지 (S3)](#8-스토리지-s3)
9. [Lambda / 서버리스 파이프라인](#9-lambda--서버리스-파이프라인)
10. [Bedrock AgentCore RAG Evidence Gateway (미배포)](#10-bedrock-agentcore-rag-evidence-gateway-미배포)
11. [Helm Addon 스택](#11-helm-addon-스택)
12. [ArgoCD / GitOps 배포 흐름](#12-argocd--gitops-배포-흐름)
13. [Backend Blue-Green 배포](#13-backend-blue-green-배포)
14. [CloudFront · ALB · 트래픽 흐름](#14-cloudfront--alb--트래픽-흐름)
15. [보안 (IAM / IRSA / Secrets)](#15-보안-iam--irsa--secrets)
16. [모니터링 & 관측성 스택](#16-모니터링--관측성-스택)
17. [알려진 한계 및 리스크](#17-알려진-한계-및-리스크)
18. [참고 문서](#참고-문서)

---

## 1. 전체 아키텍처 개요

```
                                    INTERNET
                                        │
     ┌──────────────────┬──────────────┼───────────────┬─────────────────────┐
     ▼                  ▼              ▼               ▼                     ▼
app.utterai.org   grafana-prod-agent  API 요청     Slack /finops      grafana-prod.internal
www.utterai.org   .utterai.org (public)                                 (VPN 필요)
     │                  │              │               │                     │
     ▼                  ▼              ▼               ▼                     ▼
CloudFront +      ALB(internet)    Route53→ALB    Lambda Function    ALB(internal)
WAF(edge)         → Grafana        (api.utterai.org)  URL(finops-slack)  → Grafana
     │                                  │               │
     ├──▶ S3 frontend (SPA)             ▼               ▼
     └──▶ ALB(API) 프록시         EKS utterai-prod-eks   finops-agent Lambda
                                  (System NG + Karpenter  (Bedrock Claude
                                   NodePool 6종)            agentic loop)
                                        │                    │
                    ┌───────────────────┼──────────────┐     ▼
                    ▼                   ▼              ▼   finops-query Lambda
             utterai-prod-rds    utterai-prod-redis  SQS 파이프라인     │
             (PostgreSQL 16.13,  (Redis 7.1, 2노드,   (4단계 큐 + DLQ)  ├─ Cost Explorer
              Single-AZ)          failover 비활성)         │            └─ Kubecost(내부 ALB)
                    │                                       ▼
                    └──────────── S3 버킷(10개) ────── CPU/GPU/Batch Worker
                                                       (Karpenter 스케일)

Client VPN(cvpn-endpoint) ──▶ Private App Subnet  (운영자 kubectl/RDS 접근용, available)

자체 호스팅 관측성 (utterai-observability ns):
  OTel Collector → Data Prepper → OpenSearch(1노드) + Arize Phoenix(LLM 트레이싱 PoC)

※ Bedrock AgentCore RAG Evidence Gateway(리포트 근거 검색)는 Terraform 코드는 존재하지만
   실제 AWS에는 아직 적용되지 않음 (§10 참고)
```

### 1.1 이번 리비전에서 새로 반영된 것

기존 문서(2026-06 기준) 대비 이번 조사에서 실제 AWS/클러스터 상태로 확인·갱신한 항목:

- **Karpenter가 API/Worker/GPU 매니지드 노드그룹을 완전히 대체함** (실제로 3개 노드그룹 모두 삭제 확인, `system` 노드그룹만 잔존)
- **FinOps Slack 비용 조회 봇**이 Lambda 3개(finops-slack/agent/query)로 실제 배포·가동 중
- **Bedrock AgentCore RAG Evidence Gateway + kure-retriever Lambda**는 코드는 merge됐지만 **AWS에 미적용 상태** (ECR 이미지 없음, terraform state 없음, Gateway 없음)
- **자체 호스팅 관측성 스택**(OpenSearch + Data Prepper + Arize Phoenix)이 `utterai-observability` 네임스페이스에서 가동 중 — 기존 문서엔 전혀 없던 내용
- **Kubecost 내부 ALB Ingress** 신설 (FinOps Lambda가 Kubecost REST API를 호출하기 위함)
- **Grafana가 ALB 2개**(내부용 + 외부 공개용 `grafana-prod-agent.utterai.org`)로 노출됨 — 후자는 외부 에이전트/자동화 조회용으로 추정
- **Alertmanager가 Discord 웹훅과 연동됨** (기존 문서의 "receiver: null"은 더 이상 사실이 아님)
- **Backend가 Blue-Green 배포 구조로 전환됨** (기존 단일 Deployment → blue/green Deployment + 활성 Service 스위칭)
- **Client VPN Endpoint** 신설 (운영자용 Private 서브넷 접근, `available` 상태)
- ⚠️ **운영 리스크**: `02-eks`의 로컬 tfvars(git 미추적)에 노드그룹 비활성화 플래그가 없어, 다음 `terraform apply` 시 레거시 매니지드 노드그룹이 재생성될 수 있음 (§17)

---

## 2. Terraform 레이어 구조

```
terraform/environments/prod/
│
├── 01-network/    VPC · 서브넷 · NAT GW · VPC Endpoint · 보안그룹 · Client VPN
│   State Key: prod/network/terraform.tfstate                         [적용됨]
│
├── 02-eks/        EKS 클러스터 · System Node Group · OIDC · EKS Addon(vpc-cni/coredns/
│                  kube-proxy/ebs-csi-driver)
│   State Key: prod/platform/terraform.tfstate                        [적용됨]
│   Depends on: 01-network
│   ⚠️ api/worker/gpu 매니지드 노드그룹 변수 기본값이 true — 로컬 tfvars에
│      명시적 false 없으면 apply 시 재생성 위험 (§17)
│
├── 03-services/   RDS · Redis · S3 · SQS · Secrets · IRSA · Karpenter Interruption Queue
│                  + Lambda: collect-papers, finops-query/agent/slack, kure-retriever(코드만)
│   State Key: prod/services/terraform.tfstate                        [적용됨, 2026-07-03 갱신]
│   Depends on: 01-network, 02-eks
│
├── 04-addons/     Helm 릴리스 전체(LBC·KEDA·Karpenter·모니터링 등) · ENIConfig ·
│                  CloudFront · WAF(edge) · Route53 · ACM
│   State Key: prod/addons/terraform.tfstate                          [적용됨]
│   Depends on: 01-network, 02-eks, 03-services
│
└── 05-agentcore/  Bedrock AgentCore Gateway (report-evidence-gateway, MCP)
    State Key: prod/agentcore/terraform.tfstate                       [미적용 — state 파일 자체가 없음]
    Depends on: 03-services (kure_retriever Lambda ARN)

State Backend: S3 utterai-prod-terraform-state (ap-northeast-2)
State Lock: S3 Native Locking (Terraform 1.10+)
```

> `05-agentcore`는 코드가 main 브랜치에 merge되어 있지만, S3 백엔드에 `prod/agentcore/terraform.tfstate` 자체가 존재하지 않는다. 즉 **한 번도 apply되지 않았다.** §10 참고.

---

## 3. 네트워크 아키텍처

### 3.1 VPC 서브넷 구성 (기존과 동일, 변경 없음)

```
┌───────────────────────────────────────────────────────────────────────────┐
│  VPC: utterai-prod-vpc  (10.20.0.0/16)   Region: ap-northeast-2          │
│  Secondary CIDR: 100.64.0.0/16  (Pod 전용 IP 공간)                        │
│                                                                           │
│         ap-northeast-2a                    ap-northeast-2c               │
│  ┌─────────────────────────┐      ┌─────────────────────────┐            │
│  │  Public Subnet          │      │  Public Subnet          │            │
│  │  10.20.1.0/24           │      │  10.20.2.0/24           │            │
│  │  NAT Gateway (1개)      │      │  (NAT GW 없음)          │            │
│  │  ALB 노드 (AZ 2a)       │      │  ALB 노드 (AZ 2c)       │            │
│  └────────────┬────────────┘      └────────────┬────────────┘            │
│               │ Internet GW ◄──────────────────┘                         │
│  ┌────────────▼────────────┐      ┌──────────────────────────┐           │
│  │  Private App Subnet     │      │  Private App Subnet      │           │
│  │  10.20.11.0/24          │      │  10.20.12.0/24           │           │
│  │  EKS Node · Client VPN  │      │  EKS Node                │           │
│  │  target-network 연결    │      │                          │           │
│  └─────────────────────────┘      └──────────────────────────┘           │
│  ┌─────────────────────────┐      ┌──────────────────────────┐           │
│  │  Pod Subnet (Secondary) │      │  Pod Subnet (Secondary)  │           │
│  │  100.64.0.0/17          │      │  100.64.128.0/17         │           │
│  └─────────────────────────┘      └──────────────────────────┘           │
│  ┌─────────────────────────┐      ┌──────────────────────────┐           │
│  │  Private Data Subnet    │      │  Private Data Subnet     │           │
│  │  10.20.21.0/24          │      │  10.20.22.0/24           │           │
│  │  RDS · ElastiCache      │      │  RDS · ElastiCache       │           │
│  └─────────────────────────┘      └──────────────────────────┘           │
└───────────────────────────────────────────────────────────────────────────┘
```

> NAT Gateway는 2a에 1개만 배치 (비용 최적화). 2c Private App → 2a NAT GW 경유. 단일 NAT GW이므로 2a 장애 시 2c 아웃바운드도 영향받는 단일 장애점 — §17 참고.

### 3.2 VPC Endpoint

| 종류 | 서비스 | 유형 |
|---|---|---|
| S3 | com.amazonaws.ap-northeast-2.s3 | Gateway |
| SQS | com.amazonaws.ap-northeast-2.sqs | Interface |
| Secrets Manager | com.amazonaws.ap-northeast-2.secretsmanager | Interface |
| ECR API / DKR | com.amazonaws.ap-northeast-2.ecr.* | Interface |

### 3.3 Client VPN (신규)

```
Client VPN Endpoint: cvpn-endpoint-03acc479b72c9abf1  [상태: available]
  인증 방식: 클라이언트 인증서 (mutual TLS)
  분할 터널(Split-tunnel): 활성
  클라이언트 CIDR: 172.16.0.0/22
  연결 대상: Private App Subnet
  용도: 운영자가 퍼블릭 엔드포인트 없이 kubectl / RDS / Redis 등에 접근
```

이 VPN이 준비되어 있음에도 **EKS API 엔드포인트는 여전히 Public+Private 동시 활성** 상태다 — VPN 전용으로 좁힐 수 있는 여지가 있다(§17).

---

## 4. EKS 클러스터

### 4.1 클러스터 기본 정보

| 항목 | 값 |
|---|---|
| 클러스터 이름 | `utterai-prod-eks` |
| Kubernetes 버전 | 1.31 (노드 실측: v1.31.14-eks) |
| 인증 방식 | API_AND_CONFIG_MAP |
| 엔드포인트 | Private + Public 모두 활성 (⚠️ §17) |
| CNI | VPC CNI v1.18.1 (Custom Networking + Prefix Delegation) |

### 4.2 노드 구성 — 실측 확인 결과

> **기존 문서는 API/Worker/GPU 노드그룹을 "DISABLED, Karpenter로 대체 예정"이라 기술했으나, 실제로는 이미 완전히 대체되었다.** `aws eks list-nodegroups` 실측 결과 `utterai-prod-system` 단 하나만 존재한다.

```
┌───────────────────────────────────────────────────────────────────────┐
│  EKS Managed Node Group (실측: 1개만 존재)                            │
│                                                                        │
│  utterai-prod-system                                                  │
│    인스턴스: t3.medium (ON_DEMAND)                                    │
│    실측 스케일: desired 2 / min 2 / max 4                             │
│    Taint: CriticalAddonsOnly=true:NoSchedule                          │
│    Label: role=system                                                 │
│    실행 워크로드: CoreDNS·kube-proxy·LBC·Karpenter·KEDA·ESO·ArgoCD·    │
│                  모니터링 스택 등 클러스터 필수 애드온                  │
│                                                                        │
│  api / worker / gpu 매니지드 노드그룹 → 삭제됨. Karpenter NodePool로  │
│  완전 대체 (§5)                                                        │
└───────────────────────────────────────────────────────────────────────┘
```

### 4.3 EKS 관리형 Addon

| Addon | 비고 |
|---|---|
| vpc-cni | v1.18.1-eksbuild.1 |
| coredns | managed |
| kube-proxy | managed |
| **aws-ebs-csi-driver** | **신규** — 전용 IRSA 역할(`ebs-csi-controller-sa`) 부착 |

### 4.4 Custom Networking (Pod IP 분리) — 변경 없음

```
Secondary CIDR 100.64.0.0/16 + ENIConfig(AZ별 Pod Subnet)
  ENABLE_PREFIX_DELEGATION=true / AWS_VPC_K8S_CNI_CUSTOM_NETWORK_CFG=true
결과: 노드 IP 10.20.11-12.x / 파드 IP 100.64.x.x 로 분리
```

---

## 5. Karpenter 오토스케일링 (NodePool)

기존 문서에서 "Karpenter NodePool로 대체 예정"이라 미래형으로 적었던 부분이 **실제로 완전히 구현·가동 중**이다. Terraform이 아니라 `k8s/platform/karpenter/base/`의 순수 K8s manifest(GitOps, ArgoCD `utterai-platform-prod` Application으로 배포)로 관리된다.

### 5.1 NodePool 6종

| NodePool | Capacity Type | 인스턴스 패밀리 | Taint | Consolidation |
|---|---|---|---|---|
| `platform` | on-demand | t3/t3a medium/large | 없음 | WhenEmptyOrUnderutilized, 30s |
| `system` | on-demand | t3/t3a medium/large | CriticalAddonsOnly=true:NoSchedule | WhenEmptyOrUnderutilized, 30s |
| `api` | on-demand+spot | t3.medium | dedicated=api:NoSchedule | WhenEmptyOrUnderutilized, 30s |
| `cpu-worker` | spot+on-demand | m5/m5a/m6i/m6a.xlarge | dedicated=worker:NoSchedule | WhenEmptyOrUnderutilized, 5m |
| `batch-worker` | spot+on-demand | c5/c6i/c6a/m5/m6i large~xlarge | dedicated=worker:NoSchedule | WhenEmptyOrUnderutilized, 30s |
| `gpu` | spot+on-demand | g4dn/g5 xlarge~2xlarge | dedicated=ai-gpu, nvidia.com/gpu:NoSchedule | WhenEmpty, 10m |

> `gpu` NodePool이 Spot+On-Demand를 모두 허용한다. GPU 워크로드는 On-Demand 전용이어야 한다는 정책이 있었다면 현재 코드와 불일치 — 정책 재확인 필요(§17).

### 5.2 EC2NodeClass

| Class | AMI | 루트 볼륨 |
|---|---|---|
| `default` | AL2023 latest | 50Gi gp3 |
| `gpu` | AL2023 latest (NVIDIA) | 100Gi gp3 |

둘 다 `karpenter.sh/discovery=utterai-prod-eks` 태그로 서브넷/보안그룹/역할을 자동 탐색.

### 5.3 실측 노드 스냅샷 (조사 시점)

| 노드 | NodePool | 인스턴스 타입 |
|---|---|---|
| system 노드그룹 × 2 | (매니지드 NG) | t3.medium |
| platform × 3 | platform | t3a.medium |
| api × 2 | api | t3.medium |
| cpu-worker × 1 | cpu-worker | m6i.xlarge |
| batch-worker, gpu | — | 0대 (min=0, 유휴 시 완전 스케일다운) |

### 5.4 KEDA + Karpenter 오토스케일링 흐름 (변경 없음)

```
SQS 큐 적재 → KEDA ScaledObject(SQS 트리거) → replica 증가 요청
→ Pod Pending → Karpenter가 최적 인스턴스 선택·기동(~60초) → 스케줄
SQS 큐 소진 → KEDA replica 감소 → Karpenter consolidation → 노드 종료
Spot 인터럽션 → EventBridge → utterai-prod-eks SQS → Karpenter 사전 드레인
```

---

## 6. 데이터 레이어

### 6.1 RDS PostgreSQL — 실측 확인

```
┌──────────────────────────────────────────────────────────────────────┐
│  RDS Instance: utterai-prod-rds        (aws rds describe-db-instances)│
│                                                                      │
│  엔진: PostgreSQL 16.13  (auto_minor_version_upgrade로 16.9→16.13)   │
│  인스턴스 클래스: db.r6g.large                                       │
│  Multi-AZ: false  ← 여전히 단일 AZ, Aurora 아님                       │
│  스토리지: 20 GB gp3 (allocated), max 100GB 오토스케일링              │
│  암호화: AES-256                                                     │
│  비밀번호: manage_master_user_password=true (Secrets Manager 자동관리)│
│  deletion_protection: true / skip_final_snapshot: false              │
└──────────────────────────────────────────────────────────────────────┘
```

`terraform/modules/aurora` 모듈은 여전히 존재하지만 prod `03-services`는 `modules/rds` 사용 중. Aurora/Multi-AZ 전환은 **아직 계획 단계이며 우선순위가 낮게 책정**되어 있다(내부 문서 `stabilization-status.md` 기준 13개 갭 중 8~9순위).

### 6.2 ElastiCache Redis — 실측 확인

```
┌──────────────────────────────────────────────────────────────────────┐
│  Replication Group: utterai-prod-redis  (aws elasticache describe-…) │
│                                                                      │
│  엔진: Redis 7.1 / 노드 타입: cache.r6g.large / 노드 수: 2           │
│  automatic_failover: disabled                                        │
│  multi_az: disabled                                                  │
│  → 노드는 2개지만 자동 장애조치가 꺼져 있어 실질적 HA 아님 (§17)      │
│  암호화: at-rest ✓ / in-transit(TLS) ✓                                │
│  auth_token: Secrets Manager 저장 (utterai-prod/redis-auth-token)     │
│    ⚠️ random_password로 생성되어 tfstate에 평문 저장됨 (§17)          │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 7. 메시지 큐 (SQS)

실측 확인(`aws sqs list-queues`) — 총 9개 큐, 기존 문서와 구조 동일.

```
audio-preprocess-queue ──▶ CPU Worker ──▶ gpu-inference-queue ──▶ GPU Worker
                                                                     │
                                                                     ▼
                                                       report-analysis-queue ──▶ CPU Worker
rag-ingest-queue ──▶ Batch Worker (임베딩 → RDS)

+ 각 파이프라인 큐마다 전용 DLQ (4개)
+ utterai-prod-eks (Karpenter Spot 인터럽션 큐)
```

모든 큐: SSE-SQS 암호화. DLQ 보존 7일.

---

## 8. 스토리지 (S3)

실측(`aws s3api list-buckets`) — **10개 버킷** (기존 문서 8개 대비 2개 증가).

| 버킷 | 용도 |
|---|---|
| `utterai-prod-frontend` | 프론트엔드 정적 파일 (CloudFront 원본) |
| `utterai-prod-raw-audio` | 사용자 업로드 원본 음성 (365일 자동삭제) |
| `utterai-prod-template` | 분석 템플릿 |
| `utterai-prod-rag-ingest` | RAG 임베딩 대상 문서 |
| `utterai-prod-reports` | 최종 분석 리포트 |
| **`utterai-prod-transcripts`** | **신규** — 음성 전사(transcript) 결과 저장 |
| `utterai-prod-kubecost` | Kubecost 비용 ETL (S3 백엔드) |
| `utterai-prod-loki` | Loki 로그 청크 |
| `utterai-prod-tempo` | Tempo 트레이스 |
| `utterai-prod-terraform-state` | Terraform state 백엔드 (앱 데이터 아님) |

공통 보안: 퍼블릭 액세스 전면 차단, SSE-S3 암호화, VPC Endpoint Gateway 경유(인터넷 미경유).

---

## 9. Lambda / 서버리스 파이프라인

`03-services` 레이어에서 관리되며, **실측(`aws lambda list-functions`) 결과 4개가 실제로 배포·가동 중**이고 1개(kure-retriever)는 코드만 존재한다.

### 9.1 FinOps 비용 조회 Slack 봇 — **배포·가동 중**

`docs/shared/finops-agent-plan.md`의 3-Phase 설계를 구현한 것으로, AgentCore Gateway/MCP를 의도적으로 쓰지 않고 순수 Lambda-to-Lambda + Bedrock InvokeModel 방식을 택했다("주요 결정 사항: AgentCore Gateway 미사용").

```
Slack "/finops 이번 달 GPU 비용 얼마야?"
    │  HTTPS POST
    ▼
finops-slack Lambda  (Function URL, AuthType=NONE, 자체 HMAC 서명 검증)
    │  Secrets Manager(utterai-prod/finops-slack)에서 Slack Signing Secret 조회
    │  서명 검증 + replay 방지(300초 윈도우) 후 "조회 중입니다..." 즉시 응답(3초 제한 대응)
    │  비동기 invoke (InvocationType=Event)
    ▼
finops-agent Lambda  (Memory 512MB, Timeout 60s)
    │  Bedrock Runtime invoke_model, 모델: global.anthropic.claude-haiku-4-5-20251001-v1:0
    │    (⚠️ 계획 문서는 sonnet-4-6 명시 — 실제 배포는 haiku-4-5, 확인 필요)
    │  최대 10회 tool_use 루프, 9개 도구 정의를 함께 전달
    ▼
finops-query Lambda  (Memory 256MB, Timeout 30s) — 순수 디스패처, Bedrock 미사용
    │  AWS Cost Explorer(us-east-1 ce 클라이언트): 서비스별/일별/태그별 비용, 예측
    │  Kubecost REST(내부 ALB 경유): 네임스페이스/워크로드별 비용, Spot 절감액, 클러스터 요약
    ▼
결과 → finops-agent가 종합 응답 생성 → Slack response_url로 POST
```

### 9.2 collect-papers — **배포·가동 중**

```
EventBridge 월간 스케줄 트리거
UtterAI_AI 리포지토리 소스에서 논문 수집 → rag-ingest 버킷/큐에 적재 → Bedrock 호출
Memory 128MB / Timeout 900s (15분)
```

### 9.3 kure-retriever — **코드만 존재, AWS 미배포**

```
정의 위치: terraform/environments/prod/03-services/main.tf
목적: KURE-v1 임베딩 + pgvector 기반 한국어 문서 검색 (RDS 대상)
VPC 연결 + 5분 간격 CloudWatch 워밍업 규칙까지 코드에 정의되어 있으나:

  - ECR 리포지토리 utterai-kure-retriever는 존재하지만 이미지가 하나도 push되지 않음
  - aws lambda list-functions 결과에 kure-retriever 없음
  - terraform state(prod/services/terraform.tfstate)에도 해당 리소스 없음

→ terraform apply가 아직 실행되지 않았거나(또는 이미지 부재로 실패),
  이 Lambda에 의존하는 §10의 AgentCore Gateway도 연쇄적으로 미배포 상태다.
```

---

## 10. Bedrock AgentCore RAG Evidence Gateway (미배포)

리포트 생성 기능의 "근거 검색(Evidence Research Agent)" — 한국어/해외 언어치료 문헌을 RAG로 검색하는 별도 서브시스템. **§9의 FinOps 봇과는 무관한 별개 기능**이며, 같은 시기에 코드가 merge되어 혼동하기 쉽다.

```
Terraform: terraform/environments/prod/05-agentcore/
Resource: aws_bedrockagentcore_gateway "utterai-prod-report-evidence-gateway"
  Protocol: MCP / Authorizer: AWS_IAM
  IAM Role: bedrock-agentcore.amazonaws.com 신뢰, kure_retriever Lambda invoke 권한

Gateway Target 7개 (모두 동일한 kure_retriever Lambda로 라우팅,
  bedrockAgentCoreToolName으로 내부 분기 — 에이전트의 도구 선택 능력을 보여주기 위해
  의도적으로 통합하지 않고 7개로 분리):
    search_korean_evidence / search_international_evidence /
    search_clinical_guidelines / hybrid_search / rerank_evidence /
    fetch_document_section / resolve_ontology_synonyms
```

**미배포 확인 근거**:
- `aws s3 ls s3://utterai-prod-terraform-state/prod/` → `prod/agentcore/terraform.tfstate` 자체가 없음 (한 번도 apply되지 않음)
- `aws bedrock-agentcore-control list-gateways` (ap-northeast-2 / us-east-1 / us-west-2 확인) → 전부 결과 없음
- 의존 대상인 `kure_retriever` Lambda도 미배포(§9.3)

즉 이 레이어는 **"완료된 아키텍처"가 아니라 "코드 완료, 인프라 미적용" 상태**다. 사용자가 "전체 아키텍처 완료"로 인지하고 있다면 이 부분은 예외로 명확히 알고 있어야 한다.

---

## 11. Helm Addon 스택

### 11.1 Addon 전체 목록 (버전 실측·확인)

| Helm 릴리스 | 버전 | 네임스페이스 | 상태 |
|---|---|---|---|
| aws-load-balancer-controller | 1.8.1 | ingress-system | 활성 |
| kube-prometheus-stack | 66.2.1 | monitoring | 활성 |
| kubecost (cost-analyzer) | (variable) | kubecost | 활성 + 내부 ALB Ingress 신규 |
| tempo | (variable) | monitoring | 활성 |
| loki | 7.0.0 | monitoring | 활성 |
| promtail | 6.17.1 | monitoring | 활성 |
| metrics-server | 3.12.1 | kube-system | 활성 |
| external-secrets | 0.10.4 | external-secrets | 활성 |
| aws-efs-csi-driver | 3.0.7 | kube-system | 조건부 |
| nvidia-device-plugin | 0.16.2 | kube-system | 활성 |
| keda | 2.16.1 | keda | 활성 |
| karpenter | 1.3.3 | karpenter | 활성 |
| argocd (argo-cd) | 9.5.20 | argocd | 활성 |
| cluster-autoscaler | 9.37.0 | kube-system | 비활성 (count=0, Karpenter로 대체) |

> Karpenter NodePool/EC2NodeClass 자체는 Helm/Terraform이 아니라 §5의 순수 K8s manifest(GitOps)로 관리된다 — 애드온 설치(Helm)와 스케일링 정책(K8s manifest)의 관리 주체가 분리되어 있다는 점에 유의.

### 11.2 모니터링 스택 세부 (변경분만)

```
Prometheus: retention 3d, PersistentVolume 신규 적용 (gp2, 20Gi)
Grafana: additionalDataSources에 Tempo tracesToLogs(→Loki 연계), serviceMap, nodeGraph 추가
  → 메트릭-로그-트레이스 상호 연계(correlation) 구성 강화
Loki: compactor.delete_request_store=s3 추가 (S3 기반 보존기간 삭제 처리)
Kubecost: internal-alb-ingress.yaml 신규 — §14 참고
```

---

## 12. ArgoCD / GitOps 배포 흐름

k8s manifest는 Terraform이 아닌 ArgoCD Application으로 배포된다. 조사 시점 스냅샷:

| Application | Sync | Health |
|---|---|---|
| utterai-ai-service-prod | OutOfSync | Healthy |
| utterai-ai-worker-prod | Synced | Healthy |
| utterai-backend-prod | OutOfSync | Healthy |
| utterai-platform-prod | Synced | Healthy |

`utterai-platform-prod`가 Karpenter NodePool, External Secrets, Observability, Kubecost, Image-pruner를 포함한 `k8s/platform/prod/kustomization.yaml` 5개 컴포넌트를 배포한다.

> `ai-service-prod`/`backend-prod`가 OutOfSync인 것은 조사 시점의 일시적 드리프트일 수 있으나, 운영 중 지속되면 Git 저장소와 실제 클러스터 상태가 벌어지고 있다는 신호이므로 확인이 필요하다.

---

## 13. Backend Blue-Green 배포

기존에는 단일 Deployment였으나, **Blue-Green 구조로 전환 완료**되었다 (`k8s/apps/backend/overlays/prod/`).

```
                       utterai-api-service (활성 트래픽 대상, Ingress가 바라보는 고정 이름)
                                │
                     selector: {app: utterai-api, color: <blue|green>}
                                │  ← 여기만 바꾸면 즉시 트래픽 전환
                ┌───────────────┴───────────────┐
                ▼                               ▼
   utterai-api-blue-service            utterai-api-green-service
   Deployment: replicas=0              Deployment: replicas=2
   image tag: prod-0b75293             image tag: prod-aa065de   ← 현재 활성(green)
   (직전 안정 버전, 즉시 롤백 대기)      HPA: min2/max4, cpu 70%
```

- 배포 절차: green에 신규 이미지 태그로 배포·검증 → 문제 없으면 `patch-active-service.yaml`의 selector를 `green`으로 전환(현재 상태) → blue는 replicas=0으로 대기, 롤백 필요 시 selector만 `blue`로 되돌리면 즉시 이전 버전으로 복귀.
- 두 Deployment 모두 initContainer로 alembic 마이그레이션 + 시드 스크립트 실행, PodAntiAffinity(호스트별) + TopologySpreadConstraint(AZ별) 적용.
- 보안 하드닝(양쪽 공통): runAsNonRoot, seccomp RuntimeDefault, capability drop ALL, readOnlyRootFilesystem(+`/tmp` emptyDir).
- ExternalSecret refreshInterval을 5분으로 단축(기본 1h) — RDS 마스터 비밀번호가 7일 주기로 로테이션되므로, 로테이션 직후 stale password 노출 시간을 최소화하기 위함(명시적 주석으로 근거 남김). `ai-service`/`ai-worker` 오버레이도 동일한 이유로 5분 refresh 적용.

---

## 14. CloudFront · ALB · 트래픽 흐름

### 14.1 ALB 인벤토리 (실측)

| ALB | Scheme | 용도 |
|---|---|---|
| `k8s-utteraiprod-*` | internet-facing | API 트래픽 (`api.utterai.org`, CloudFront 2차 오리진) |
| `k8s-grafanaprodintern-*` | internal | Grafana 내부 접근 (`grafana-prod.internal.utterai.org`, VPN 필요) |
| `k8s-grafanaprodagentp-*` | internet-facing | Grafana 외부 공개 접근 (`grafana-prod-agent.utterai.org`, HTTPS+ACM, 별도 ALB group) |
| `k8s-kubecostinternal-*` | internal | Kubecost REST/UI (FinOps Lambda 및 내부 사용자용) |

### 14.2 CloudFront

```
Distribution: E19RUFYJ2DD3MI
Aliases: app.utterai.org, www.utterai.org
Origin 1: S3-utterai-prod-frontend (SPA 정적 파일)
Origin 2: ALB-API → api.utterai.org (API 프록시)
WAF: utterai-prod-frontend-edge-waf (CLOUDFRONT scope, us-east-1)
  - AWSManagedRulesCommonRuleSet + IP 기반 RateLimit
```

⚠️ **Regional WAF는 어떤 ALB에도 연결되어 있지 않다** (`aws wafv2 list-web-acls --scope REGIONAL` 결과 없음). CloudFront 엣지만 보호되고, ALB로 직접 요청 시(예: 도메인 우회) WAF 보호를 받지 않는다(§17).

### 14.3 Route 53 레코드 (실측)

```
app.utterai.org               A/AAAA   → CloudFront
www.utterai.org                A/AAAA   → CloudFront
api.utterai.org                CNAME    → ALB (API)
grafana-prod-agent.utterai.org CNAME    → ALB (Grafana 공개)
grafana-prod.internal.utterai.org CNAME → ALB (Grafana 내부, VPN 필요)
grafana-agent-lab-tokyo.internal.utterai.org CNAME → (dev-tokyo 환경, 별도)
+ ACM 인증서 검증용 CNAME/TXT 다수
```

---

## 15. 보안 (IAM / IRSA / Secrets)

### 15.1 IRSA 역할 (애드온)

| IAM Role | 서비스 어카운트 |
|---|---|
| `utterai-prod-lbc-role` | aws-load-balancer-controller |
| `utterai-prod-eso-role` | external-secrets |
| `utterai-prod-keda-role` | keda-operator |
| `utterai-prod-karpenter-role` | karpenter |
| `utterai-prod-kubecost-role` | kubecost |
| `utterai-prod-loki-role` / `-tempo-role` | loki / tempo |
| `ebs-csi-controller-sa` | aws-ebs-csi-driver (신규) |

### 15.2 IRSA 역할 (애플리케이션)

| IAM Role | 권한 범위 |
|---|---|
| `utterai-prod-api-role` | S3(raw-audio, reports, transcripts) · SQS(send) · Secrets Manager |
| `utterai-prod-ai-api-role` | SQS(audio-preprocess) SendMessage |
| `utterai-prod-ai-cpu-role` | SQS(audio-preprocess/report-analysis) · S3 · Bedrock InvokeModel |
| `utterai-prod-ai-ml-gpu-role` | SQS(gpu-inference/report-analysis) · S3 |
| `utterai-prod-ai-service-role` | Bedrock InvokeModel 전용 (SQS/S3 없음, HTTP 리포트-챗 서비스, 신규) |
| `utterai-prod-batch-role` | SQS(rag-ingest) · S3(rag-ingest, reports) · Secrets Manager |

### 15.3 External Secrets Operator 흐름 (변경 없음, refresh interval만 일부 단축)

```
AWS Secrets Manager (backend-api-secret, redis-auth-token, grafana-admin-credentials,
                      finops-slack, collect-papers-secret, alertmanager-discord 등)
    │ IRSA(eso-role) GetSecretValue
    ▼
ClusterSecretStore (aws-secrets-manager, ap-northeast-2) — ⚠️ 전 네임스페이스 공유 단일 스토어
    │ refreshInterval: 기본 1h, backend/ai-service/ai-worker는 5m로 단축
    ▼
ExternalSecret → K8s Secret → Pod envFrom
```

### 15.4 Node IAM 역할 (변경 없음)

AmazonEKSWorkerNodePolicy · AmazonEKS_CNI_Policy · AmazonEC2ContainerRegistryReadOnly · AmazonSSMManagedInstanceCore

---

## 16. 모니터링 & 관측성 스택

### 16.1 표준 관측성 (monitoring 네임스페이스, 기존과 대부분 동일)

```
Promtail(DaemonSet) → Loki(SingleBinary, S3, 14일 보존)
Prometheus(kube-prometheus-stack, PVC 20Gi 신규, 3일 보존) → Grafana
Tempo(OTLP gRPC:4317/HTTP:4318, S3, 3일 보존)
Grafana: DataSource 3종(Prometheus/Loki/Tempo) + trace↔log 상호 연계 신규 구성
  노출: 내부 ALB(grafana-prod.internal.utterai.org, VPN 필요) +
        공개 ALB(grafana-prod-agent.utterai.org, HTTPS, 외부 에이전트/자동화 조회용으로 추정)
AlertManager: Discord 웹훅 연동 확인(Secrets Manager: utterai-prod/alertmanager-discord)
  → 기존 문서의 "receiver: null(알림 비활성)"은 더 이상 사실이 아님
Kubecost: 내부 ALB Ingress 신규(§11.2) — Prometheus/Grafana는 외부(기존 스택) 재사용,
  S3(utterai-prod-kubecost) Thanos Object Store
```

### 16.2 자체 호스팅 LLM 관측성 스택 (신규, `utterai-observability` 네임스페이스)

기존 문서에 전혀 없던 부분으로, 실제 Pod 조회 결과 4개 워크로드가 가동 중이다.

```
┌────────────────────────────────────────────────────────────────────┐
│  utterai-observability namespace                                   │
│                                                                      │
│  otel-collector (Deployment)                                        │
│    애플리케이션(backend/ai-service/ai-worker)의 OTLP 트레이스 수신    │
│    (OTEL_EXPORTER_OTLP_ENDPOINT → otel-collector:4318)               │
│         │                                                            │
│         ▼                                                            │
│  data-prepper (Deployment)                                           │
│    OTel 스팬을 OpenSearch 인덱스 포맷으로 변환·적재                   │
│         │                                                            │
│         ▼                                                            │
│  opensearch-0 (StatefulSet, 1노드, 자체 호스팅 — AWS managed 아님)    │
│    풀텍스트 트레이스 검색·저장                                        │
│                                                                       │
│  phoenix (Deployment, Arize Phoenix)                                 │
│    LLM 전용 트레이싱/평가 UI — 코드 주석상 PoC(proof-of-concept) 라벨│
│    ai-worker의 PHOENIX_TRACING_ENABLED=true, phoenix.project=        │
│    utterai-ai-cpu-poc 로 연결됨                                      │
└────────────────────────────────────────────────────────────────────┘
```

> OpenSearch는 AWS OpenSearch Service(관리형)가 아니라 **클러스터 내부에 자체 호스팅된 단일 노드**다. 프로덕션 관측성 데이터를 단일 노드·비관리형으로 운영 중이라는 점은 가용성 리스크로 볼 수 있다(§17).

---

## 17. 알려진 한계 및 리스크

문서 정확성을 위해, "완료된 것처럼 보이지만 실제로는 갭이 있는" 항목을 모아 정리한다.

| 항목 | 현재 상태 | 리스크 |
|---|---|---|
| **02-eks tfvars 노드그룹 플래그** | 로컬 tfvars(git 미추적)에 `api/worker/gpu_node_group_enabled=false` 미설정, 변수 기본값 `true` | 다음 `terraform apply` 시 이미 폐기된 레거시 매니지드 노드그룹이 Karpenter NodePool과 함께 재생성될 수 있음 — **즉시 조치 권장** |
| **05-agentcore / kure-retriever** | Terraform 코드·ECR 리포지토리는 존재하나 이미지 미푸시, state 없음, Gateway 없음 | "리포트 근거 검색 RAG" 기능 전체가 실제로는 동작하지 않음 |
| **RDS** | Single-AZ, `modules/rds` (Aurora 아님) | 장애 시 자동 failover 없음, 계획된 전환 우선순위 낮음 |
| **Redis** | 노드 2개지만 `automatic_failover=disabled`, `multi_az=disabled` | 이름만 복제 그룹이지 실질적 자동 장애조치 없음 |
| **EKS API 엔드포인트** | Public+Private 동시 활성, Client VPN은 이미 준비됨 | VPN 전용으로 좁힐 수 있음에도 퍼블릭 노출 유지 |
| **ALB WAF** | CloudFront 엣지에만 WAF 적용, Regional WAF 없음 | ALB 직접 접근 시 WAF 우회 가능 |
| **CMK 미사용** | S3/SQS/Secrets Manager 전부 AWS 관리형 키 | 키 관리 세분화·감사 추적 불가 |
| **Redis auth token** | `random_password`로 생성되어 tfstate에 평문 저장 | state 접근 권한이 곧 자격증명 노출로 이어짐 |
| **ClusterSecretStore** | 전 네임스페이스 공유 단일 스토어 | 한 네임스페이스 침해 시 전체 Secret 접근 가능(측면 이동 리스크) |
| **자체 호스팅 OpenSearch** | 단일 노드, 비관리형 | 노드 장애 시 관측성 데이터 유실 가능 |
| **단일 NAT Gateway** | 2a에만 배치 | 2a 장애 시 2c의 아웃바운드도 영향 |
| **FinOps 모델 ID** | 계획 문서는 `sonnet-4-6`, 실제 배포는 `claude-haiku-4-5` | 의도적 변경인지 확인 필요 |
| **GPU NodePool capacity** | Spot+On-Demand 허용 | "GPU는 On-Demand 전용" 정책이 있었다면 코드와 불일치 |
| **ArgoCD 동기화** | `ai-service-prod`, `backend-prod` 조사 시점 OutOfSync | Git과 클러스터 상태 드리프트 여부 확인 필요 |

---

## 참고 문서

- 운영 절차·명령어: [`docs/prod/README.md`](./README.md)
- 보안 상세: [`docs/prod/security.md`](./security.md)
- 안정화 현황(코드-문서 불일치 트래킹): [`docs/prod/stabilization-status.md`](./stabilization-status.md)
- FinOps 에이전트 설계 원안: [`docs/shared/finops-agent-plan.md`](../shared/finops-agent-plan.md)
- Dev vs Prod 비교: [`docs/README.md`](../README.md)
- EKS 아키텍처 흐름: [`docs/shared/eks-architecture-flow.md`](../shared/eks-architecture-flow.md)
- 마이그레이션 체크리스트(초기 계획, 현재 상태 아님 — 참고용): [`docs/prod/migration-checklist.md`](./migration-checklist.md)
