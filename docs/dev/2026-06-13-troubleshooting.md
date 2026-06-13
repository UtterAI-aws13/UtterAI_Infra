# 2026-06-13 Dev 환경 트러블슈팅 기록

GPU worker(`utterai-ml-gpu-worker`)가 ML 추론(Diarization, ASR, Aligning)은 성공하지만 결과 저장 단계에서 매 job마다 실패하는 문제 분석 및 해결.

---

## 발견 경위

`gpu-inference-queue`에 `ApproximateNumberOfMessagesNotVisible: 2`가 유지됨(소비 후 ack 없이 visibility timeout 반복). GPU worker 로그를 확인하자 모든 job에서 동일한 두 에러가 연속 발생함을 확인.

---

## 1. S3 버킷 이름 불일치 (NoSuchBucket)

### 증상

```
ML GPU STAGE 실패: Failed to upload /tmp/tmp.../transcript_draft.json
to utterai-report-dev/transcript-drafts/.../transcript_draft.json:
An error occurred (NoSuchBucket) when calling the PutObject operation:
The specified bucket does not exist
```

모든 job에서 `SAVING TRANSCRIPT DRAFT` 단계에 동일하게 실패.

### 원인

AI 코드에 하드코딩된 버킷 이름 `utterai-report-dev`가 실제 Terraform이 생성한 버킷 이름 `utterai-dev-reports`와 다름.

```
# AI 코드가 쓰려는 버킷
utterai-report-dev          ← 존재하지 않음

# 실제 생성된 버킷 (aws s3 ls)
utterai-dev-reports         ← Terraform naming convention: utterai-dev-{resource}
```

`ml-gpu-worker-deployment.yaml`에 `S3_BUCKET_REPORT` 환경변수가 정의되어 있지 않아 AI 코드가 내부 기본값(하드코딩된 이름)을 사용함.

### 해결

**① `ml-gpu-worker-deployment.yaml`에 환경변수 추가**

```yaml
- name: S3_BUCKET_REPORT
  value: "utterai-dev-reports"
```

**② AI 레포 코드에서 환경변수를 읽도록 수정 (AI팀 담당)**

```python
# 수정 전 (하드코딩)
bucket = "utterai-report-dev"

# 수정 후 (환경변수)
bucket = os.environ["S3_BUCKET_REPORT"]
```

**③ 적용**

```bash
# 환경변수 주입 후 deployment 재배포
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
envsubst < k8s/workloads/ml-gpu-worker-deployment.yaml | kubectl apply -f -
kubectl rollout restart deployment/utterai-ml-gpu-worker -n utterai-ai-gpu
```

---

## 2. GPU Worker DB 환경변수 미주입 (psycopg.OperationalError)

### 증상

```
ML GPU STAGE 실패: (psycopg.OperationalError) connection is bad:
connection to server on socket "/var/run/postgresql/.s.PGSQL.5432" failed:
No such file or directory
        Is the server running locally and accepting connections on that socket?
```

DB_HOST가 비어있어 psycopg가 RDS 엔드포인트 대신 localhost Unix 소켓으로 연결 시도.

### 원인

`ai-worker-external-secret`은 `SecretSynced: True` 상태로 `ai-worker-secret` K8s Secret에 DB_HOST, DB_USER, DB_PASSWORD, DB_PORT, DB_NAME을 정상 동기화하고 있음.

그러나 `ml-gpu-worker-deployment.yaml`의 `env` 블록에 해당 Secret 참조가 없어서 파드 기동 시 DB 환경변수가 전혀 주입되지 않음.

```yaml
# ml-gpu-worker-deployment.yaml 현황 (문제 시점)
env:
  - name: HF_TOKEN          # gpu-worker-secret 참조 ← 주입됨
    valueFrom:
      secretKeyRef:
        name: gpu-worker-secret
        key: HF_TOKEN
  # DB_HOST, DB_USER, DB_PASSWORD, DB_PORT, DB_NAME 참조 없음 ← 누락
```

ExternalSecret이 정상이어도 Deployment에서 참조하지 않으면 파드에 주입되지 않음.

### 해결

**`ml-gpu-worker-deployment.yaml`에 `ai-worker-secret` 참조 추가**

```yaml
env:
  - name: HF_TOKEN
    valueFrom:
      secretKeyRef:
        name: gpu-worker-secret
        key: HF_TOKEN
  - name: DB_HOST
    valueFrom:
      secretKeyRef:
        name: ai-worker-secret
        key: DB_HOST
  - name: DB_PORT
    valueFrom:
      secretKeyRef:
        name: ai-worker-secret
        key: DB_PORT
  - name: DB_USER
    valueFrom:
      secretKeyRef:
        name: ai-worker-secret
        key: DB_USER
  - name: DB_PASSWORD
    valueFrom:
      secretKeyRef:
        name: ai-worker-secret
        key: DB_PASSWORD
  - name: DB_NAME
    valueFrom:
      secretKeyRef:
        name: ai-worker-secret
        key: DB_NAME
```

**적용**

```bash
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
envsubst < k8s/workloads/ml-gpu-worker-deployment.yaml | kubectl apply -f -
kubectl rollout status deployment/utterai-ml-gpu-worker -n utterai-ai-gpu
```

---

## 3. SQS visibility timeout 누적 메시지 처리

두 에러로 인해 gpu-inference-queue 메시지들이 ack 없이 visibility timeout을 반복하며 NotVisible 상태로 쌓임.

```
gpu-inference-queue:
  ApproximateNumberOfMessages:        0  (대기 없음)
  ApproximateNumberOfMessagesNotVisible: 2  ← 처리 실패 후 재시도 대기 중
```

위 두 문제 수정 후 deployment 재시작하면 visibility timeout이 지난 메시지들이 자동 재소비되어 정상 처리됨. DLQ로 이동하기 전에 해결되면 별도 조치 불필요.

DLQ 확인 명령어:

```bash
aws sqs get-queue-attributes \
  --queue-url $(aws sqs get-queue-url \
    --queue-name utterai-dev-gpu-inference-dlq \
    --region ap-northeast-2 --query QueueUrl --output text) \
  --attribute-names ApproximateNumberOfMessages \
  --region ap-northeast-2
```

---

## 변경된 파일

| 파일 | 변경 내용 |
|------|-----------|
| `k8s/workloads/ml-gpu-worker-deployment.yaml` | `S3_BUCKET_REPORT` 환경변수 추가, `ai-worker-secret`에서 DB 환경변수(DB_HOST/PORT/USER/PASSWORD/NAME) 주입 추가 |

## AI 레포 수정 필요 사항 (AI팀)

| 파일 | 변경 내용 |
|------|-----------|
| `app/pipelines/analysis_pipeline.py` (추정) | `utterai-report-dev` 하드코딩 → `os.environ["S3_BUCKET_REPORT"]` 변경 |
