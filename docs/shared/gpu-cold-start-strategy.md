# GPU 콜드 스타트 대응 전략 비교

작성일: 2026-06-18  
대상: UtterAI dev 환경 기준  
배경: GPU worker minReplicaCount=0 구조에서 첫 요청 시 5~10분 콜드 스타트 발생 문제

---

## 1. 전체 파이프라인 동작

### 1.1 Queue → Worker 전체 흐름

```
사용자 음성 업로드
        │
        ▼
BE finalize()
        │
        ▼
┌─────────────────────────────────────────────────────────────┐
│ audio-preprocess-queue                                      │
│ visibility_timeout: 300s  /  maxReceiveCount: 3             │
└──────────────────────┬──────────────────────────────────────┘
                       │ KEDA: queueLength=5, min=1, max=3
                       ▼
              cpu-worker (utterai-ai-cpu)
              ─ VAD, 오디오 전처리, 구간 분할
              ─ minReplicaCount: 1 (항상 1개 대기)
              ─ NodePool: cpu-worker (m6i.xlarge, on-demand)
                       │
                       │ SQS SendMessage
                       ▼
┌─────────────────────────────────────────────────────────────┐
│ gpu-inference-queue                                         │
│ visibility_timeout: 1800s  /  maxReceiveCount: 3            │
└──────────────────────┬──────────────────────────────────────┘
                       │ KEDA: queueLength=1, min=0, max=1
                       ▼
           ml-gpu-worker (utterai-ai-gpu)         ← 문제 구간
           ─ 화자 분리(pyannote), STT(Whisper)
           ─ minReplicaCount: 0 (평소 pod 없음)
           ─ NodePool: gpu (g4dn.xlarge or g5.xlarge, on-demand)
                       │
                       │ DB 저장 후 BE callback
                       │ BE.finalize()가 별도로 SQS 발행
                       ▼
┌─────────────────────────────────────────────────────────────┐
│ report-analysis-queue                                       │
│ visibility_timeout: 900s  /  maxReceiveCount: 3             │
└──────────────────────┬──────────────────────────────────────┘
                       │ KEDA: queueLength=5, min=1, max=3
                       ▼
              cpu-worker (동일 pod, _run_report_loop)
              ─ Bedrock invoke_claude() 호출
              ─ 리포트 생성 후 저장

※ rag-ingest-queue → batch-worker (RAG 파이프라인, 별도 흐름)
```

### 1.2 각 Worker의 스케일링 설정 요약

| Worker | NodePool | 인스턴스 | minReplica | maxReplica | KEDA 기준 |
|--------|----------|---------|------------|------------|-----------|
| cpu-worker | cpu-worker | m6i.xlarge (on-demand) | **1** | 3 | queueLength=5 (두 큐 합산) |
| ml-gpu-worker | gpu | g4dn.xlarge or g5.xlarge (on-demand) | **0** | 1 | queueLength=1 |
| batch-worker | batch-worker | c5/m6i.xlarge (spot) | 0 | — | queueLength 기반 |

### 1.3 GPU 콜드 스타트 발생 구간

```
gpu-inference-queue 메시지 도착
        │
        ▼ KEDA 감지 (수 초)
        │
        ▼ ml-gpu-worker: 0 → 1 스케일업 트리거
        │
        ▼ Karpenter: GPU 노드 프로비저닝 결정 (수 초)
        │
        ├─ EC2 GPU 인스턴스 부팅          ~1~3분
        ├─ GPU 드라이버 초기화             ~30초~1분
        ├─ ML 컨테이너 이미지 pull         ~1~2분  ← 대용량 이미지
        └─ pyannote + Whisper 모델 로딩   ~2~3분  ← HuggingFace에서 매번 다운로드

합계: 5~10분  ← KEDA/Karpenter로 단축 불가 (AWS/OS/컨테이너 레이어)
```

**핵심**: KEDA는 "언제 스케일할지", Karpenter는 "노드를 얼마나 빠르게 프로비저닝할지"를 담당한다.  
그 이후의 부팅·이미지 pull·모델 로딩 구간은 두 컴포넌트 모두 제어할 수 없다.

---

## 2. 현재 상태

| 항목 | 값 | 비고 |
|------|----|----|
| gpu-inference-queue visibility_timeout | 1800s (수정 완료) | 메시지 유실 방지 목적 |
| gpu-inference-queue maxReceiveCount | 3 (수정 완료) | DLQ 직행 방지 |
| ml-gpu-worker minReplicaCount | 0 | 콜드 스타트 원인 |
| GPU NodePool consolidationPolicy | WhenEmpty | 처리 중엔 노드 유지 |
| GPU NodePool consolidateAfter | 5m | 큐 빌 때 5분 후 노드 회수 |

visibility_timeout과 maxReceiveCount 수정은 **메시지 유실(인프라 안정성)** 문제를 해결한다.  
**사용자 대기 10분 문제(UX)** 는 아래 두 방법 중 하나로 접근해야 한다.

---

## 3. 방법 1 — 스케줄 기반 스케일링

### 개념

업무 시간대에는 ml-gpu-worker를 1개 미리 띄워두고, 야간에만 0으로 내린다.  
KEDA CronScaler와 SQS 트리거를 조합한다.

### 변경 내용

`k8s/apps/ai-worker/overlays/dev/scaledobject-ml-gpu-worker.yaml`

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: utterai-ml-gpu-worker-scaledobject
  namespace: utterai-ai-gpu
spec:
  scaleTargetRef:
    name: utterai-ml-gpu-worker
  minReplicaCount: 0
  maxReplicaCount: 1
  cooldownPeriod: 300
  advanced:
    horizontalPodAutoscalerConfig:
      behavior:
        scaleDown:
          stabilizationWindowSeconds: 300
  triggers:
    # 기존 SQS 트리거 유지
    - type: aws-sqs-queue
      authenticationRef:
        name: keda-aws-pod-identity
        kind: ClusterTriggerAuthentication
      metadata:
        queueURL: "https://sqs.ap-northeast-2.amazonaws.com/032886669461/utterai-dev-gpu-inference-queue"
        queueLength: "1"
        activationQueueLength: "1"
        awsRegion: ap-northeast-2
        identityOwner: operator
    # 추가: 업무 시간 warm-up
    - type: cron
      metadata:
        timezone: Asia/Seoul
        start: 0 9 * * 1-5    # 평일 09:00 → 1개로
        end: 0 22 * * 1-5     # 평일 22:00 → SQS 트리거에만 맡김
        desiredReplicas: "1"
```

**동작 방식**: KEDA는 여러 트리거 중 가장 높은 desired replica 값을 취한다.  
업무 시간엔 cron이 1을 요구하므로 pod가 유지되고, SQS 메시지가 없어도 내려가지 않는다.  
야간엔 cron 트리거가 비활성화되어 SQS 기반으로만 동작 (메시지 없으면 0으로 수렴).

### 해결되는 범위

```
평일 09:00~22:00:
  ─ ml-gpu-worker 1개 항상 대기
  ─ 메시지 도착 즉시 처리 (콜드 스타트 없음) ✅
  ─ 사용자 대기 10분 → 수 초로 단축 ✅

평일 22:00 이후 / 주말:
  ─ 마지막 메시지 처리 후 cooldown(300s) 경과 시 0으로 수렴
  ─ 야간/주말 첫 요청은 콜드 스타트 여전히 발생 ❌
  ─ 단, 야간 사용 빈도가 낮다면 실질적 영향 적음
```

### 비용

| 시나리오 | 계산 | 월 비용 |
|---------|------|--------|
| 현재 (min=0, 콜드 스타트) | 실제 처리 시간만 과금 (거의 0) | ~$0~10 |
| 스케줄: 평일 09~22시 유지 | g4dn.xlarge $0.526 × 13h × 22일 | **~$150** |
| 스케줄: 평일 09~22시 유지 | g5.xlarge $1.006 × 13h × 22일 | **~$288** |
| 항상 켜기 (min=1, 참고용) | g4dn.xlarge $0.526 × 24h × 30일 | **~$378** |

※ ap-northeast-2 on-demand 기준. GPU 노드가 뜨는 동안 EC2 비용 외 EBS, 데이터 전송 비용 소액 추가.

### Trade-off

| 항목 | 내용 |
|------|------|
| 장점 | 구현 단순, 적용 즉시 효과, 인프라 레벨만 수정 |
| 장점 | 업무 시간 내 콜드 스타트 완전 제거 |
| 단점 | 요청이 없어도 업무 시간 내내 GPU 비용 발생 |
| 단점 | 업무 시간 외 첫 요청은 여전히 콜드 스타트 |
| 단점 | 업무 시간 정의가 모호하면 불필요한 비용 증가 |
| 주의 | GPU NodePool `consolidationPolicy: WhenEmpty`이므로 pod가 떠 있으면 노드는 유지됨 |

---

## 4. 방법 2 — EFS 모델 캐싱

### 개념

콜드 스타트 중 가장 긴 시간을 차지하는 **모델 다운로드 단계를 제거**한다.  
pyannote, Whisper 모델 파일을 EFS에 미리 올려두고, GPU pod가 뜰 때 EFS에서 로드한다.

```
현재 콜드 스타트:
  EC2 부팅(1~3분) + 이미지 pull(1~2분) + 모델 다운로드(2~3분) = 5~10분

EFS 캐싱 적용 후:
  EC2 부팅(1~3분) + 이미지 pull(1~2분) + EFS 마운트 후 로드(~20~40초) = 3~5분
```

모델 다운로드 시간을 제거하므로 콜드 스타트가 단축되지만, 완전히 없어지지는 않는다.

### 변경 내용

**① Terraform: EFS 파일시스템 생성**

`terraform/modules/efs/main.tf` (신규 모듈)

```hcl
resource "aws_efs_file_system" "model_cache" {
  encrypted       = true
  throughput_mode = "bursting"

  tags = {
    Name = "${var.project_name}-${var.environment}-model-cache"
  }
}

resource "aws_efs_mount_target" "model_cache" {
  for_each = toset(var.private_app_subnet_ids)

  file_system_id  = aws_efs_file_system.model_cache.id
  subnet_id       = each.value
  security_groups = [aws_security_group.efs.id]
}

resource "aws_security_group" "efs" {
  name   = "${var.project_name}-${var.environment}-efs-sg"
  vpc_id = var.vpc_id

  ingress {
    from_port       = 2049  # NFS
    to_port         = 2049
    protocol        = "tcp"
    security_groups = [var.node_security_group_id]
  }
}
```

**② Kubernetes: EFS CSI Driver StorageClass + PVC**

```yaml
# efs-storageclass.yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: efs-model-cache
provisioner: efs.csi.aws.com
parameters:
  provisioningMode: efs-ap
  fileSystemId: fs-xxxxxxxxx   # Terraform output으로 주입
  directoryPerClaim: "false"
---
# efs-pvc.yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: model-cache-pvc
  namespace: utterai-ai-gpu
spec:
  accessModes:
    - ReadWriteMany   # 여러 pod에서 동시 마운트 가능
  storageClassName: efs-model-cache
  resources:
    requests:
      storage: 20Gi
```

**③ ml-gpu-worker-deployment.yaml 변경**

```yaml
# 추가: EFS 볼륨 마운트
spec:
  template:
    spec:
      volumes:
      - name: model-cache
        persistentVolumeClaim:
          claimName: model-cache-pvc
      containers:
      - name: ml-gpu-worker
        env:
        - name: HF_HOME
          value: /mnt/model-cache   # 기존 /tmp/huggingface → EFS 경로로 변경
        volumeMounts:
        - name: model-cache
          mountPath: /mnt/model-cache
```

**④ 최초 1회: 모델 파일 EFS에 업로드 (Init Job)**

```yaml
# model-init-job.yaml (최초 1회 실행)
apiVersion: batch/v1
kind: Job
metadata:
  name: model-cache-init
  namespace: utterai-ai-gpu
spec:
  template:
    spec:
      nodeSelector:
        workload: ai-gpu
      tolerations:
      - key: dedicated
        operator: Equal
        value: ai-gpu
        effect: NoSchedule
      - key: nvidia.com/gpu
        operator: Exists
        effect: NoSchedule
      volumes:
      - name: model-cache
        persistentVolumeClaim:
          claimName: model-cache-pvc
      containers:
      - name: model-downloader
        image: 032886669461.dkr.ecr.ap-northeast-2.amazonaws.com/utterai-ai-gpu:latest
        command:
        - python
        - -c
        - |
          from app.models.diarization_pyannote import PyannoteWrapper
          from app.models.asr_whisper import WhisperASRWrapper
          PyannoteWrapper("pyannote/speaker-diarization-3.1", ...).load()
          WhisperASRWrapper("large-v2", ...).load()
          print("모델 캐싱 완료")
        env:
        - name: HF_HOME
          value: /mnt/model-cache
        - name: HF_TOKEN
          valueFrom:
            secretKeyRef:
              name: gpu-worker-secret
              key: HF_TOKEN
        volumeMounts:
        - name: model-cache
          mountPath: /mnt/model-cache
        resources:
          requests:
            nvidia.com/gpu: "1"
      restartPolicy: Never
```

### 해결되는 범위

```
콜드 스타트 구성 요소별 영향:

  EC2 GPU 인스턴스 부팅        ~1~3분  →  변화 없음 ❌
  GPU 드라이버 초기화           ~30초   →  변화 없음 ❌
  ML 컨테이너 이미지 pull       ~1~2분  →  변화 없음 ❌ (이미지 자체는 ECR에서 pull)
  pyannote + Whisper 모델 로딩  ~2~3분  →  EFS 로드 ~20~40초로 단축 ✅

총 콜드 스타트: 5~10분 → 3~5분으로 단축
콜드 스타트 자체는 여전히 존재 — UX 개선은 부분적
```

### 비용

| 항목 | 계산 | 월 비용 |
|------|------|--------|
| EFS 스토리지 | 모델 약 10~15GB × $0.30/GB | **~$3~5** |
| EFS Bursting 처리량 | 기본 포함 (읽기 위주, 추가 비용 거의 없음) | ~$0 |
| EFS 마운트 타깃 | NLB 비용 없음 (NFS 직접) | ~$0 |
| **합계** | | **~$3~5/월** |

방법 1 대비 비용이 압도적으로 저렴하지만, 효과는 제한적이다.

### Trade-off

| 항목 | 내용 |
|------|------|
| 장점 | 비용이 거의 없음 (~$3~5/월) |
| 장점 | 다른 방법과 병행 가능 (방법 1 + EFS 조합 시 시너지) |
| 장점 | 모델 업데이트 시 Init Job만 재실행하면 됨 |
| 단점 | 콜드 스타트 5~10분 → 3~5분으로 단축, **완전 해결 아님** |
| 단점 | EFS CSI Driver 설치 필요 (eks-addon 추가) |
| 단점 | Init Job을 최초 1회 / 모델 버전 변경 시마다 실행해야 함 |
| 단점 | EFS 마운트 지연이 간헐적으로 발생할 수 있음 (NFS 특성) |
| 주의 | EFS는 ReadWriteMany라 여러 pod가 동시 마운트 가능 → max=1 초과 시에도 유효 |

---

## 5. 두 방법 비교

| 항목 | 방법 1: 스케줄 기반 스케일링 | 방법 2: EFS 모델 캐싱 |
|------|---------------------------|----------------------|
| **콜드 스타트 제거 여부** | 업무 시간 내 완전 제거 | 부분 단축 (5~10분 → 3~5분) |
| **UX 개선** | 업무 시간 내 즉시 처리 ✅ | 여전히 수 분 대기 ⚠️ |
| **월 추가 비용** | ~$150 (g4dn.xlarge 기준) | ~$3~5 |
| **구현 복잡도** | 낮음 (ScaledObject 수정만) | 중간 (EFS, PVC, Init Job 추가) |
| **다른 방법과 병행** | 가능 | 가능 (방법 1과 조합 권장) |
| **야간/주말 첫 요청** | 콜드 스타트 발생 | 콜드 스타트 발생 (단축됨) |
| **적용 범위** | 인프라 레벨만 | 인프라 + K8s manifest |

### 선택 기준

```
업무 시간 내 UX가 최우선, 비용 감수 가능
  → 방법 1 (스케줄 기반)

비용 최소화가 최우선, 수 분 대기 감수 가능
  → 방법 2 (EFS 캐싱)

두 방법 조합 (권장)
  → 방법 1 + 방법 2 동시 적용
     업무 시간: 즉시 처리 (방법 1 효과)
     야간/주말: 3~5분으로 단축 (방법 2 효과)
     월 추가 비용: ~$150~155
```

---

## 6. 방법 1 + 방법 2 조합

### 개념

두 방법은 해결하는 구간이 다르기 때문에 충돌 없이 동시에 적용할 수 있다.

```
방법 1 (스케줄): 업무 시간에 pod를 미리 띄워 콜드 스타트 자체를 회피
방법 2 (EFS):   콜드 스타트가 발생하더라도 모델 로딩 시간을 단축
```

두 방법을 조합하면 시간대별로 최적의 동작이 보장된다.

### 시간대별 동작

```
평일 09:00~22:00 (방법 1 작동 구간)
─────────────────────────────────────
  cron 트리거: desiredReplicas=1
  → ml-gpu-worker 1개 항상 대기
  → gpu 노드가 이미 떠 있음
  → 메시지 도착 즉시 처리 (수 초)
  → EFS는 이미 마운트된 상태 (추가 효과 없음, 그냥 동작)

평일 22:00~09:00 / 주말 (방법 2 단독 작동 구간)
─────────────────────────────────────────────────
  cron 트리거 비활성화
  → cooldown 300s 후 pod 0으로 수렴, 노드 회수
  → 첫 요청 시 콜드 스타트 발생
  
  콜드 스타트 구간별:
    EC2 부팅 + GPU 드라이버    ~1~3분  그대로
    이미지 pull (ECR)          ~1~2분  그대로
    모델 로딩 (EFS에서)        ~20~40초  ← 방법 2 효과
  
  → 합계: 3~5분 (기존 5~10분 대비 단축)
```

### 적용 순서

두 방법을 조합할 때 적용 순서가 중요하다. EFS가 없는 상태에서 방법 1을 먼저 켜면, 스케줄에 의해 pod가 뜰 때 여전히 HuggingFace에서 모델을 다운로드한다.

```
Step 1: EFS 인프라 구성 (Terraform)
  ─ EFS 파일시스템 생성
  ─ 마운트 타깃 생성 (각 서브넷)
  ─ EFS CSI Driver addon 설치

Step 2: K8s 리소스 생성
  ─ StorageClass, PVC 적용
  ─ ml-gpu-worker-deployment.yaml에 EFS 볼륨 마운트 추가
  ─ HF_HOME 환경변수 /mnt/model-cache로 변경

Step 3: Init Job 실행 (최초 1회)
  ─ 모델 파일 EFS에 다운로드
  ─ Job 완료 확인

Step 4: 스케줄 기반 ScaledObject 적용
  ─ cron 트리거 추가
  ─ 업무 시간 시작 시 pod가 뜨면서 EFS에서 모델 로드 확인
```

### 변경 파일 목록

| 파일 | 변경 내용 | 방법 |
|------|----------|------|
| `terraform/modules/efs/` (신규) | EFS 파일시스템, 마운트 타깃, SG | 방법 2 |
| `terraform/environments/dev/03-services/main.tf` | efs 모듈 호출 추가 | 방법 2 |
| `terraform/modules/eks-addons/main.tf` | EFS CSI Driver Helm 추가 | 방법 2 |
| `k8s/apps/ai-worker/base/ml-gpu-worker-deployment.yaml` | EFS 볼륨 마운트, HF_HOME 변경 | 방법 2 |
| `k8s/apps/ai-worker/base/efs-pvc.yaml` (신규) | StorageClass, PVC | 방법 2 |
| `k8s/apps/ai-worker/base/model-init-job.yaml` (신규) | 최초 모델 캐싱 Job | 방법 2 |
| `k8s/apps/ai-worker/overlays/dev/scaledobject-ml-gpu-worker.yaml` | cron 트리거 추가 | 방법 1 |

### 비용

| 항목 | 계산 | 월 비용 |
|------|------|--------|
| GPU 노드 (평일 09~22h, g4dn.xlarge) | $0.526 × 13h × 22일 | ~$150 |
| EFS 스토리지 | 모델 ~10~15GB × $0.30/GB | ~$3~5 |
| **합계** | | **~$153~155/월** |

방법 1 단독 대비 +$3~5로 야간/주말 콜드 스타트까지 개선된다.

### 조합 적용 후 전체 시나리오

```
[시나리오 A] 평일 오전 10시, 첫 번째 사용자 요청

  09:00에 cron 트리거 → pod 1개 스케일업
  └─ GPU 노드 이미 존재 (전날 22시 이후 회수됐다면 새로 프로비저닝)
  └─ EFS 마운트 → 모델 로드 완료
  
  10:00 요청 도착 시: worker 이미 대기 중 → 수 초 내 처리 ✅

[시나리오 B] 평일 오전 8시 50분 (스케줄 시작 전), 첫 번째 사용자 요청

  cron 아직 비활성 → SQS 트리거만 동작
  → 콜드 스타트 발생
  → EC2 부팅 + 이미지 pull + EFS 모델 로드 (3~5분)
  → 처리 완료 ⚠️ (완전 해결 아님, 하지만 기존 10분보다 단축)

[시나리오 C] 주말 오후, 사용자 요청

  cron 비활성 (주말 제외)
  → 콜드 스타트 (3~5분)
  → 처리 완료 ⚠️

[시나리오 D] 평일 오후 3시, GPU 노드 이미 떠있는 상태에서 두 번째 요청

  worker 대기 중 → 즉시 처리 ✅
```

### Trade-off 요약

| 항목 | 내용 |
|------|------|
| 장점 | 업무 시간 콜드 스타트 완전 제거 |
| 장점 | 야간/주말도 3~5분으로 단축 |
| 장점 | EFS는 한 번 구성하면 유지 비용 거의 없음 |
| 단점 | 구현 복잡도가 가장 높음 (EFS + 스케줄 동시 관리) |
| 단점 | 평일 09~22h GPU 비용은 방법 1과 동일하게 발생 |
| 단점 | Init Job 관리 필요 (모델 버전 변경 시 재실행) |
| 주의 | EFS 구성을 먼저 완료한 뒤 스케줄을 활성화해야 함 |

---

## 7. 현재 구조의 한계 (참고)

어떤 방법을 선택해도 아래 항목은 별도로 AI 팀에 요청해야 한다.

| 항목 | 내용 | 담당 |
|------|------|------|
| receive_message VisibilityTimeout=600 파라미터 제거 | 큐 기본값(1800s) 사용하도록 | AI 팀 |
| heartbeat (ChangeMessageVisibility) 추가 | 장시간 처리 중 타임아웃 자동 연장 | AI 팀 |
| GPU 추론 완료 후 BE callback HTTP POST 호출 | 현재 누락 | AI 팀 |
