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
envsubst < k8s-legacy/workloads/ml-gpu-worker-deployment.yaml | kubectl apply -f -
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
envsubst < k8s-legacy/workloads/ml-gpu-worker-deployment.yaml | kubectl apply -f -
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
| `k8s-legacy/workloads/ml-gpu-worker-deployment.yaml` | `S3_BUCKET_REPORT` 환경변수 추가, `ai-worker-secret`에서 `BE_DB_*` 환경변수(HOST/PORT/USER/PASSWORD/NAME) 주입 추가 |

## AI 레포 수정 필요 사항 (AI팀)

| 파일 | 변경 내용 |
|------|-----------|
| `app/config.py` | `s3_bucket_report` 기본값 수정 (`utterai-report-dev` → `utterai-dev-reports`) |

---

## 4. GPU Worker IRSA 역할에 reports 버킷 쓰기 권한 누락 (AccessDenied)

### 증상

S3 버킷 이름이 올바르게 설정된 후 새로운 에러 발생:

```
ML GPU STAGE 실패: Failed to upload .../transcript_draft.json
to utterai-dev-reports/transcript-drafts/...:
An error occurred (AccessDenied) when calling the PutObject operation:
arn:aws:sts::032886669461:assumed-role/utterai-dev-ai-ml-gpu-irsa-role/...
is not authorized to perform: s3:PutObject on resource: arn:aws:s3:::utterai-dev-reports/...
```

### 원인

`terraform/modules/irsa/main.tf`의 GPU worker(`aws_iam_role_policy.ai_ml_gpu`) 정책에 `s3:PutObject`가 `utterai-dev-raw-audio/*`에만 허용되어 있고, `utterai-dev-reports/*`에 대한 권한이 누락되어 있었음.

```hcl
# 수정 전 — raw_audio 버킷만 허용
{
  Effect   = "Allow"
  Action   = ["s3:GetObject", "s3:PutObject"]
  Resource = ["${var.raw_audio_bucket_arn}/*"]
},
```

### 해결

**`terraform/modules/irsa/main.tf`** GPU worker 정책 블록에 reports 버킷 PutObject 추가:

```hcl
{
  Effect   = "Allow"
  Action   = ["s3:GetObject", "s3:PutObject"]
  Resource = ["${var.raw_audio_bucket_arn}/*"]
},
{
  Effect   = "Allow"
  Action   = ["s3:PutObject"]
  Resource = ["${var.reports_bucket_arn}/*"]
},
```

**Terraform 적용:**

```bash
cd terraform/environments/dev/03-services
terraform init -reconfigure
terraform apply \
  -target='module.irsa.aws_iam_role_policy.ai_ml_gpu' \
  -var="cluster_name=utterai-dev-eks" \
  -var="rds_instance_class=db.t3.medium" \
  -var="redis_node_type=cache.t3.micro"
```

---

## 5. BE_DB_* prefix 불일치 — GPU Worker DB 연결 실패

### 증상

환경변수 주입 후에도 DB 에러 지속:

```
(psycopg.OperationalError) connection failed:
  connection to server at "10.10.22.42", port 5432 failed:
  FATAL: password authentication failed for user "utterai_app"
  connection to server at "10.10.22.42", port 5432 failed:
  FATAL: no pg_hba.conf entry for host "10.10.11.129", user "utterai_app",
         database "utterai", no encryption
```

### 원인

두 가지 문제가 복합적으로 발생:

1. **env prefix 불일치**: 처음에 `DB_*`로 주입했으나 `rds.py`는 `settings.be_database_url`을 사용하므로 `BE_DB_*` prefix가 필요함. `DB_*`는 AI pgvector DB용 `db_*` 필드로 매핑됨.

2. **SSL 미설정**: psycopg3는 기본적으로 SSL/비SSL 연결을 모두 시도. RDS pg_hba.conf가 `hostssl` 엔트리만 허용하므로 비SSL 시도가 거절됨.

### 해결

**① `ml-gpu-worker-deployment.yaml`에서 env 이름을 `DB_*` → `BE_DB_*`로 수정** (이미 반영)

**② AI 레포 `app/storage/rds.py`에 `sslmode=require` 추가** (AI팀 PR #28):

```python
_be_engine = create_async_engine(
    settings.be_database_url,
    connect_args={"sslmode": "require"},
)
```

---

## 6. BE RDS 비밀번호 불일치 (미해결 — BE팀 확인 필요)

### 증상

SSL 연결은 pg_hba를 통과하지만 비밀번호 인증 실패:

```
FATAL: password authentication failed for user "utterai_app"
```

### 원인

`utterai-dev/ai-worker-secret`의 `DB_PASSWORD` 값이 RDS에 생성된 `utterai_app` 유저의 실제 비밀번호와 다름.

### 확인 방법

```bash
# 현재 시크릿 값 확인
aws secretsmanager get-secret-value \
  --secret-id utterai-dev/ai-worker-secret \
  --region ap-northeast-2 \
  --query SecretString --output text

# RDS에서 직접 접속 테스트 (Bastion 또는 동일 VPC 내 파드에서)
psql "host=utterai-dev-rds.c76womumyurf.ap-northeast-2.rds.amazonaws.com \
      port=5432 dbname=utterai user=utterai_app sslmode=require"
```

### 해결

BE팀이 `utterai_app` 유저의 실제 RDS 비밀번호를 확인 후 Secrets Manager 업데이트:

```bash
aws secretsmanager update-secret \
  --secret-id utterai-dev/ai-worker-secret \
  --secret-string '{
    "DB_USER": "utterai_app",
    "DB_PASSWORD": "<실제 비밀번호>",
    "DB_HOST": "utterai-dev-rds.c76womumyurf.ap-northeast-2.rds.amazonaws.com",
    "DB_PORT": "5432",
    "DB_NAME": "utterai"
  }' \
  --region ap-northeast-2
```

시크릿 업데이트 후 ExternalSecret이 1시간 이내 자동 갱신되거나 아래로 즉시 갱신:

```bash
kubectl annotate externalsecret ai-worker-external-secret -n utterai-ai-gpu \
  force-sync=$(date +%s) --overwrite
kubectl rollout restart deployment/utterai-ml-gpu-worker -n utterai-ai-gpu
```

---

## 전체 변경 파일 요약

### Infra 레포 (UtterAI_Infra)

| 파일 | 변경 내용 |
|------|-----------|
| `k8s-legacy/workloads/ml-gpu-worker-deployment.yaml` | `S3_BUCKET_REPORT` 추가, `DB_*` → `BE_DB_*` env 이름 수정 |
| `terraform/modules/irsa/main.tf` | GPU worker IRSA에 `utterai-dev-reports` s3:PutObject 권한 추가 |
| `docs/dev/README.md` | 5.6절 EKS 접근 권한 및 kubeconfig 설정 가이드 추가 |

### AI 레포 (UtterAI_AI)

| 파일 | 변경 내용 | PR |
|------|-----------|-----|
| `app/config.py` | `s3_bucket_report` 기본값 수정 | #27 (merged) |
| `app/storage/rds.py` | `sslmode=require` 추가 | #28 (미머지) |

## 미해결 사항

| 항목 | 담당 | 내용 |
|------|------|------|
| RDS 비밀번호 불일치 | BE팀 | `utterai_app` 실제 비밀번호 확인 후 `ai-worker-secret` 업데이트 |
| AI PR #28 배포 | AI팀 | `fix/rds-ssl-require` 머지 후 새 이미지 빌드·배포 필요 |
