# UtterAI Dev 환경 — 부하 테스트 시나리오

> 최종 업데이트: 2026-06-10  
> 목적: CA+HPA (Phase 1) 성능 기준을 측정하고, Karpenter+KEDA (Phase 2) 전환 후 개선 수치를 비교한다.

---

## 목차

1. [SQS 파이프라인 구조](#1-sqs-파이프라인-구조)
2. [Phase 1 — CA + HPA](#2-phase-1--ca--hpa)
3. [Phase 2 — Karpenter + KEDA](#3-phase-2--karpenter--keda)
4. [스크립트 실행 가이드](#4-스크립트-실행-가이드)
5. [Phase 1 vs 2 비교 지표](#5-phase-1-vs-2-비교-지표)

---

## 1. SQS 파이프라인 구조

테스트를 이해하기 위해 전체 파이프라인 흐름을 먼저 파악해야 한다.

```
사용자 요청
    │
    ▼
[Backend API] ──────────────────────────────────────► audio-preprocess-queue
                                                              │
                                                   [CPU Worker] (음성 전처리)
                                                              │
                                                   gpu-inference-queue
                                                              │
                                                   [ML GPU Worker] (STT/화자분리)
                                                              │
                                                   report-analysis-queue
                                                              │
                                                   [CPU Worker] (Bedrock LLM 분석)
                                                              │
                                                         S3 (reports)

문서 ingest 경로:
[Backend API] ──► rag-ingest-queue ──► [Batch Worker] ──► 벡터 DB
```

### HPA 설정 (Phase 1 기준)

| 워크로드 | Namespace | min | max | 트리거 | 소비 큐 |
|---------|-----------|-----|-----|--------|---------|
| utterai-api | utterai-api | 1 | 4 | CPU 70% | — (외부 HTTP) |
| utterai-cpu-worker | utterai-ai-cpu | 1 | 2 | CPU 70% | audio-preprocess-queue |
| utterai-ml-gpu-worker | utterai-ai-gpu | 1 | 2 | CPU 70% | gpu-inference-queue |
| utterai-batch-worker | utterai-batch | 1 | 5 | CPU 70% | rag-ingest-queue |

---

## 2. Phase 1 — CA + HPA

### 2.1 동작 원리

```
부하 투입
  │
  ▼
Pod CPU 상승
  │  (HPA 평가 주기: ~15초, stabilizationWindow: 60초)
  ▼
HPA → replica 증가 요청
  │
  ▼
새 Pod → Pending (노드 자원 부족)
  │  (CA scan interval: ~10초)
  ▼
CA → ASG desired 증가 → EC2 프로비저닝
  │  (~2~4분 소요: EC2 기동 + kubelet + 이미지 pull)
  ▼
노드 Ready → Pod Running
```

**핵심 제약**: SQS 큐에 메시지가 쌓여도 워커 Pod CPU가 낮으면 HPA가 반응하지 않는다.  
즉, 큐 적체와 스케일링 사이에 CPU 임계값 도달까지 걸리는 지연이 존재한다.

### 2.2 테스트 시나리오

#### 시나리오 A — API HTTP 부하 (HPA 검증)

```bash
# ALB DNS 확인
ALB_DNS=$(kubectl get ingress -n utterai-api -o jsonpath='{.items[0].status.loadBalancer.ingress[0].hostname}')

# 부하 투입 (50 RPS, 5분)
# 주의: /health는 CPU 부하가 거의 없음 — 실제 API 엔드포인트를 사용해야 HPA 트리거됨
python tests/load/generate_api_load.py \
  --url http://${ALB_DNS}/api/v1/<실제_엔드포인트> \
  --rps 50 \
  --duration 300
```

**예상 타임라인**:

| 경과 시간 | 이벤트 |
|----------|-------|
| T+0 | 부하 시작 |
| T+1~3min | API Pod CPU 70% 초과 → HPA 반응 |
| T+3~4min | 신규 Pod Pending (api 노드 자원 소진) |
| T+5~8min | CA가 api 노드풀 EC2 추가 |
| T+8~10min | 신규 Pod Running, 레이턴시 정상화 |

#### 시나리오 B — SQS 부하 (워커 HPA + CA 검증)

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# Step 1: audio-preprocess-queue에 투입 → cpu-worker HPA 트리거 대기
python tests/load/send_sqs_messages.py \
  --queue-url "https://sqs.ap-northeast-2.amazonaws.com/${ACCOUNT_ID}/utterai-dev-audio-preprocess-queue" \
  --count 100

# Step 2: rag-ingest-queue에 투입 → batch-worker HPA 트리거 (별도 터미널)
python tests/load/send_sqs_messages.py \
  --queue-url "https://sqs.ap-northeast-2.amazonaws.com/${ACCOUNT_ID}/utterai-dev-rag-ingest-queue" \
  --count 200
```

**예상 타임라인**:

| 경과 시간 | 이벤트 |
|----------|-------|
| T+0 | SQS 메시지 투입 완료 |
| T+0~2min | 워커가 메시지 소비 시작, CPU 상승 중 |
| T+2~4min | CPU 70% 초과 → HPA 반응 (replica 증가) |
| T+4~6min | 신규 워커 Pod Pending |
| T+6~10min | CA가 worker 노드 추가 완료 |
| T+10~13min | 신규 Pod Running, 큐 소비 재개 |

**Phase 1의 병목**: SQS 큐에 메시지가 100개 쌓여도, 기존 워커가 처리 중이라 CPU가 임계값에 달하기 전까지는 스케일이 안 됨. 큐 깊이와 스케일링이 직접 연동되지 않는 구조.

---

## 3. Phase 2 — Karpenter + KEDA

> Phase 1 테스트 완료 후 전환. 이 섹션은 전환 전 설계 문서 역할을 한다.

### 3.1 동작 원리

```
SQS 메시지 투입
  │
  ▼
KEDA ScaledObject — SQS 큐 깊이 폴링 (30초 주기)
  │  (큐 깊이 >= 임계값이면 즉시 replica 계산)
  ▼
Pod scale-up 요청
  │
  ▼
새 Pod → Pending (노드 자원 부족)
  │  (Karpenter: Pending Pod 감지 즉시 EC2 Fleet API 직접 호출)
  ▼
노드 프로비저닝 (~30~60초)
  │
  ▼
Pod Running
```

**CA 대비 Karpenter 차이**:
- CA: ASG → EC2 Launch Template → kubelet 기동 순서로 간접 호출 (2~4분)
- Karpenter: EC2 Fleet API 직접 호출, NodePool 기반 최적 인스턴스 선택 (30~60초)

**HPA 대비 KEDA 차이**:
- HPA: CPU/Memory 메트릭 → 임계값 도달 후 반응 (1~3분 지연)
- KEDA: SQS 큐 깊이 직접 관찰 → 메시지 도착 후 30초 내 반응, minReplicas=0 지원

### 3.2 KEDA ScaledObject 설계 (예정)

전환 시 아래 ScaledObject를 각 워커 네임스페이스에 배포한다.  
기존 HPA는 제거하고 KEDA가 대체한다.

**cpu-worker** (`utterai-ai-cpu`):
```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: utterai-cpu-worker-scaledobject
  namespace: utterai-ai-cpu
spec:
  scaleTargetRef:
    name: utterai-cpu-worker
  minReplicaCount: 0          # 큐가 비면 0으로 축소 (dev 비용 절감)
  maxReplicaCount: 5
  pollingInterval: 30
  cooldownPeriod: 120
  triggers:
    - type: aws-sqs-queue
      metadata:
        queueURL: https://sqs.ap-northeast-2.amazonaws.com/<ACCOUNT>/utterai-dev-audio-preprocess-queue
        queueLength: "5"      # 메시지 5개당 Pod 1개
        awsRegion: ap-northeast-2
        identityOwner: operator
```

**ml-gpu-worker** (`utterai-ai-gpu`):
```yaml
triggers:
  - type: aws-sqs-queue
    metadata:
      queueURL: https://sqs.ap-northeast-2.amazonaws.com/<ACCOUNT>/utterai-dev-gpu-inference-queue
      queueLength: "3"        # GPU 비용 고려 — 메시지 3개당 Pod 1개
      awsRegion: ap-northeast-2
```

**batch-worker** (`utterai-batch`):
```yaml
triggers:
  - type: aws-sqs-queue
    metadata:
      queueURL: https://sqs.ap-northeast-2.amazonaws.com/<ACCOUNT>/utterai-dev-rag-ingest-queue
      queueLength: "10"
      awsRegion: ap-northeast-2
```

### 3.3 Karpenter NodePool 설계 (예정)

```yaml
# worker NodePool — cpu-worker, batch-worker 대상
apiVersion: karpenter.sh/v1beta1
kind: NodePool
metadata:
  name: utterai-worker
spec:
  template:
    spec:
      nodeClassRef:
        name: utterai-worker-nodeclass
      requirements:
        - key: karpenter.k8s.aws/instance-family
          operator: In
          values: ["t3", "c5"]
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["spot", "on-demand"]
  limits:
    cpu: 64
  disruption:
    consolidationPolicy: WhenUnderutilized
    consolidateAfter: 30s

# gpu NodePool — ml-gpu-worker 대상
apiVersion: karpenter.sh/v1beta1
kind: NodePool
metadata:
  name: utterai-gpu
spec:
  template:
    spec:
      requirements:
        - key: karpenter.k8s.aws/instance-family
          operator: In
          values: ["g4dn"]
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["on-demand"]   # GPU는 Spot 미허용 (prod 정책 동일 적용)
```

### 3.4 테스트 시나리오 (Phase 2)

Phase 1과 동일한 스크립트를 사용하되, 관찰 포인트가 다르다.

```bash
# 시나리오 B와 동일한 SQS 투입 명령어 사용

# 핵심 관찰 포인트 (Phase 2):
# 1. KEDA가 큐 깊이 감지 후 몇 초 만에 Pod scale 요청을 내리는가
# 2. Karpenter가 Pending Pod를 감지 후 노드를 몇 초 만에 Ready로 만드는가
# 3. minReplicas=0 상태에서 첫 메시지 도착 후 Pod Ready까지 총 시간
```

**예상 타임라인 (Phase 2)**:

| 경과 시간 | 이벤트 |
|----------|-------|
| T+0 | SQS 메시지 투입 |
| T+30s | KEDA 폴링 감지 → Pod scale 요청 |
| T+35s | Pod Pending (minReplicas=0이면 노드 없음) |
| T+60~90s | Karpenter 노드 프로비저닝 완료 |
| T+90~120s | Pod Running, 큐 소비 시작 |

---

## 4. 스크립트 실행 가이드

### 준비

```bash
# Python 의존성
pip install requests boto3

# AWS 인증 확인
aws sts get-caller-identity

# kubeconfig 설정
aws eks update-kubeconfig --name utterai-dev-eks --region ap-northeast-2
```

### 전체 실행 순서 (권장)

```
터미널 1: 전체 상태 관찰 (테스트 내내 실행)
터미널 2: 스케일 타이밍 측정 (시나리오별로 실행)
터미널 3: 부하 투입
```

```bash
# 터미널 1 — 관찰 (Phase 1 또는 2 모두 동일)
./tests/observe/watch_scaling.sh 2>&1 | tee docs/dev/results/phase1_$(date +%Y%m%d_%H%M%S).log

# 터미널 2 — 타이밍 측정 (시나리오 B: cpu-worker 기준)
./tests/observe/measure_scale_time.sh utterai-ai-cpu utterai-cpu-worker

# 터미널 3 — 부하 투입 (시나리오 A + B 동시)
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ALB_DNS=$(kubectl get ingress -n utterai-api -o jsonpath='{.items[0].status.loadBalancer.ingress[0].hostname}')

python tests/load/generate_api_load.py \
  --url "http://${ALB_DNS}/api/v1/<엔드포인트>" \
  --rps 50 --duration 300 &

python tests/load/send_sqs_messages.py \
  --queue-url "https://sqs.ap-northeast-2.amazonaws.com/${ACCOUNT_ID}/utterai-dev-audio-preprocess-queue" \
  --count 100
```

### 결과 저장 위치

```
docs/dev/results/
├── phase1_YYYYMMDD_HHMMSS.log   ← watch_scaling.sh 출력
└── phase2_YYYYMMDD_HHMMSS.log   ← Phase 2 전환 후
```

---

## 5. Phase 1 vs 2 비교 지표

두 Phase를 동일한 부하 조건(SQS 100개, API 50 RPS)으로 실행하고 아래 수치를 기록한다.

| 지표 | Phase 1 (CA+HPA) | Phase 2 (Karpenter+KEDA) | 목표 개선 |
|------|:---------------:|:----------------------:|:-------:|
| 스케일 트리거 | CPU 70% 도달 | SQS depth >= N | — |
| SQS 투입 → Pod scale 요청 | 1~3분 (CPU 도달 대기) | ~30초 (즉시 감지) | ~4배 |
| Pod Pending → 노드 Ready | 2~4분 | 30~60초 | ~4배 |
| 전체 스케일아웃 시간 | **5~10분** | **1~2분** | ~5배 |
| SQS 적체 무반응 구간 | 있음 | 없음 | — |
| minReplicas=0 (dev 비용) | 불가 | 가능 | — |
| 스케일다운 후 노드 반환 | CA: 10분 후 축소 | Karpenter: 30초 후 consolidation | — |

### 기록 항목 (measure_scale_time.sh 출력 기준)

```
Phase 1 결과 기록:
  Pod Pending 감지:       +___s
  노드 Ready:             +___s
  Pod Running:            +___s
  전체 스케일아웃 시간:    ___초

Phase 2 결과 기록:
  Pod Pending 감지:       +___s
  노드 Ready:             +___s
  Pod Running:            +___s
  전체 스케일아웃 시간:    ___초
```

---

## 관련 문서

- [전체 보안 현황](./security/overview.md)
- [Dev 환경 배포 가이드](./README.md)
- 테스트 스크립트: `tests/load/`, `tests/observe/`
