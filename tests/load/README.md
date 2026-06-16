# 부하 테스트 실행 가이드

CA+HPA(Phase 1)와 Karpenter+KEDA(Phase 2)의 스케일링 성능을 동일 조건에서 비교 측정한다.

## 스크립트 목록

| 파일 | 역할 |
|------|------|
| `generate_api_load.py` | HTTP GET 부하 투입 → API HPA CPU 트리거 |
| `send_sqs_messages.py` | SQS 메시지 투입 → 워커 스케일 트리거 |
| `../observe/watch_scaling.sh` | 노드/Pod/HPA 상태 10초 간격 기록 |
| `../observe/measure_scale_time.sh` | Pod Pending → 노드 Ready → Pod Running 시간 측정 |

---

## 사전 준비

```bash
pip install requests boto3
```

```bash
# ALB DNS 확인
ALB_DNS=$(kubectl get ingress -n utterai-api -o jsonpath='{.items[0].status.loadBalancer.ingress[0].hostname}')

# AWS Account ID 확인
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

# 큐 URL 조합
AUDIO_QUEUE="https://sqs.ap-northeast-2.amazonaws.com/${ACCOUNT}/utterai-dev-audio-preprocess-queue"
RAG_QUEUE="https://sqs.ap-northeast-2.amazonaws.com/${ACCOUNT}/utterai-dev-rag-ingest-queue"
GPU_QUEUE="https://sqs.ap-northeast-2.amazonaws.com/${ACCOUNT}/utterai-dev-gpu-inference-queue"
```

결과 디렉터리 생성:

```bash
mkdir -p tests/observe/results
```

---

## Phase 1: CA + HPA 측정

### 시나리오 A — API 부하 (HPA CPU 트리거 + CA 노드 추가)

터미널을 3개 열고 순서대로 실행한다.

**터미널 1 — 상태 관찰 (부하 시작 전에 먼저 실행)**

```bash
chmod +x tests/observe/watch_scaling.sh
./tests/observe/watch_scaling.sh 2>&1 | tee tests/observe/results/phase1_api_$(date +%Y%m%d_%H%M%S).log
```

**터미널 2 — 노드/Pod 타이밍 측정**

```bash
chmod +x tests/observe/measure_scale_time.sh
./tests/observe/measure_scale_time.sh utterai-api utterai-api
```

**터미널 3 — 부하 투입**

```bash
python tests/load/generate_api_load.py \
  --url "http://${ALB_DNS}/api/v1/<실제_엔드포인트>" \
  --rps 50 \
  --duration 300 \
  --phase 1
```

> `/health`는 CPU 부하가 없어 HPA가 트리거되지 않는다. DB/Redis 조회가 발생하는 실제 엔드포인트를 사용해야 한다.

---

### 시나리오 B — SQS 부하 (워커 HPA CPU 트리거)

**터미널 1 — 상태 관찰**

```bash
./tests/observe/watch_scaling.sh 2>&1 | tee tests/observe/results/phase1_sqs_$(date +%Y%m%d_%H%M%S).log
```

**터미널 2 — 타이밍 측정 (워커 네임스페이스 지정)**

```bash
# cpu-worker 측정
./tests/observe/measure_scale_time.sh utterai-ai-cpu utterai-cpu-worker

# batch-worker 측정 (별도 터미널)
./tests/observe/measure_scale_time.sh utterai-batch utterai-batch-worker
```

**터미널 3 — 메시지 투입 (두 큐를 동시에 실행)**

```bash
# cpu-worker 트리거
python tests/load/send_sqs_messages.py \
  --queue-url "${AUDIO_QUEUE}" \
  --count 100 \
  --rate 10 \
  --phase 1 &

# batch-worker 트리거
python tests/load/send_sqs_messages.py \
  --queue-url "${RAG_QUEUE}" \
  --count 50 \
  --rate 5 \
  --phase 1

wait
```

**Phase 1 예상 동작:**
- 워커 CPU가 70%에 달해야 HPA가 반응한다.
- 메시지를 빠르게 소비해 CPU 부하가 발생하지 않으면 HPA가 반응하지 않는다.
- 이 무반응/지연이 Phase 1의 한계이며, Phase 2와의 핵심 비교 지점이다.

---

## Phase 2: Karpenter + KEDA 측정

Karpenter와 KEDA가 클러스터에 배포된 이후에 실행한다.

**시나리오 A — API 부하 (동일 파라미터, `--phase 2`만 변경)**

```bash
# 터미널 1
./tests/observe/watch_scaling.sh 2>&1 | tee tests/observe/results/phase2_api_$(date +%Y%m%d_%H%M%S).log

# 터미널 2
./tests/observe/measure_scale_time.sh utterai-api utterai-api

# 터미널 3
python tests/load/generate_api_load.py \
  --url "http://${ALB_DNS}/api/v1/<실제_엔드포인트>" \
  --rps 50 \
  --duration 300 \
  --phase 2
```

**시나리오 B — SQS 부하 (KEDA 큐 깊이 트리거)**

```bash
# 터미널 1
./tests/observe/watch_scaling.sh 2>&1 | tee tests/observe/results/phase2_sqs_$(date +%Y%m%d_%H%M%S).log

# 터미널 2
./tests/observe/measure_scale_time.sh utterai-ai-cpu utterai-cpu-worker

# 터미널 3
python tests/load/send_sqs_messages.py \
  --queue-url "${AUDIO_QUEUE}" \
  --count 100 \
  --rate 10 \
  --phase 2
```

`--phase 2` 실행 시 투입 완료 후 자동으로 큐 깊이를 30초 간격으로 폴링하며 KEDA 반응(큐 소진)을 관찰한다.

**Phase 2 예상 동작:**
- KEDA가 큐 깊이를 직접 관찰하므로 메시지 도착 후 ~30초 내에 Pod scale 요청이 발생한다.
- `batch-worker`는 `minReplicaCount: 0`이므로 평소 Pod가 없다가 메시지 도착 시 cold-start된다.
- 노드가 부족하면 Karpenter가 노드를 추가한다 (CA 대비 2~3분 단축 예상).

---

## 비교 포인트

`tests/observe/results/` 아래 로그 파일과 `measure_scale_time.sh` 출력을 기준으로 비교한다.

| 측정 항목 | Phase 1 (CA+HPA) | Phase 2 (Karpenter+KEDA) |
|-----------|-----------------|--------------------------|
| Pod scale 트리거 | CPU > 70% 달성 후 | SQS 큐 깊이 임계값 도달 후 (~30s) |
| 노드 추가 소요 시간 | Pod Pending 후 3~4분 | Pod Pending 후 60~90초 |
| 워커 스케일 반응성 | CPU 부하 없으면 무반응 | 큐에 메시지 있으면 즉시 반응 |
| batch-worker 유휴 시 | minReplicas=1 상시 대기 | minReplicas=0 scale-to-zero |

---

## 파라미터 레퍼런스

### generate_api_load.py

| 파라미터 | 기본값 | 설명 |
|----------|--------|------|
| `--url` | (필수) | CPU 부하가 발생하는 API 엔드포인트 |
| `--rps` | 50 | 초당 요청 수 |
| `--duration` | 300 | 테스트 시간(초) |
| `--phase` | 1 | `1`=CA+HPA, `2`=Karpenter+KEDA |

### send_sqs_messages.py

| 파라미터 | 기본값 | 설명 |
|----------|--------|------|
| `--queue-url` | (필수) | SQS 큐 URL |
| `--count` | 100 | 전송할 메시지 수 |
| `--rate` | 10 | 초당 투입 속도 (최대 배치 단위 10) |
| `--region` | ap-northeast-2 | AWS 리전 |
| `--phase` | 1 | `1`=CA+HPA, `2`=Karpenter+KEDA |
