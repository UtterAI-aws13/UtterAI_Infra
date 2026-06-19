# Dev / Prod 환경 차이점

이 문서는 `k8s/apps` 및 `k8s/platform` 아래 dev와 prod overlay의 실질적인 차이를 정리합니다.

---

## 목차

1. [Backend](#1-backend)
2. [AI Worker](#2-ai-worker)
3. [KEDA ScaledObject](#3-keda-scaledobject)
4. [Karpenter EC2NodeClass](#4-karpenter-ec2nodeclass)
5. [Network Policy](#5-network-policy)
6. [보안 컨텍스트](#6-보안-컨텍스트)
7. [EFS PersistentVolume](#7-efs-persistentvolume)
8. [환경변수 / ConfigMap](#8-환경변수--configmap)

---

## 1. Backend

### 1.1 배포 전략

| 항목 | Dev | Prod |
|------|-----|------|
| namespace | `utterai-api` | `utterai-prod-api` |
| 배포 방식 | 단일 Deployment (`utterai-api`) | Blue/Green (`utterai-api-blue` + `utterai-api-green`) |
| active 전환 방법 | 해당 없음 | `patch-active-service.yaml`로 Service selector의 `color` 값 변경 |
| 기본 active color | 해당 없음 | `blue` |

Dev는 단일 Deployment로 단순하게 운영합니다. Prod는 무중단 배포를 위해 blue/green을 분리하고, Service selector만 바꿔 트래픽을 전환합니다.

### 1.2 복제본 및 HPA

| 항목 | Dev | Prod |
|------|-----|------|
| HPA 수 | 1개 (`utterai-api-hpa`) | 2개 (`utterai-api-blue-hpa`, `utterai-api-green-hpa`) |
| minReplicas | 1 | 2 (blue·green 각각) |
| maxReplicas | 4 | 4 (blue·green 각각) |
| CPU 임계값 | 70% | 70% |
| scaleUp stabilizationWindow | 60s | 60s |
| scaleDown stabilizationWindow | 300s | 300s |

Prod에서 minReplicas를 2로 올린 이유: blue 또는 green 중 하나만 active이므로 최소 가용 파드를 보장하려면 비활성 슬롯도 1개 이상 떠 있어야 합니다.

### 1.3 PodDisruptionBudget

| 항목 | Dev | Prod |
|------|-----|------|
| PDB | 없음 | blue PDB + green PDB (각 `minAvailable: 1`) |

노드 유지보수나 Karpenter 통합(consolidation) 시 blue/green 각각 최소 1개 파드를 유지합니다.

### 1.4 Secret 추가

| 환경변수 | Dev | Prod |
|----------|-----|------|
| `INTERNAL_CALLBACK_HMAC_SECRET` | 없음 | backend-api-secret에서 주입 |

---

## 2. AI Worker

### 2.1 Overlay 구성 차이

Dev overlay는 별도 patch 없이 ScaledObject와 KEDA TriggerAuthentication만 추가합니다. Prod overlay는 다음을 추가로 덮어씁니다.

| 파일 | 역할 |
|------|------|
| `patch-configmap.yaml` | prod SQS URL, S3 버킷명, OTEL 엔드포인트 |
| `patch-deployment.yaml` | `APP_ENV=prod`, 보안 컨텍스트 강화 |
| `patch-external-secret.yaml` | prod IRSA 주석 |
| `patch-serviceaccount.yaml` | prod IRSA role ARN |
| `patch-efs-pvc.yaml` | prod EFS 파일시스템 ID |
| `network-policy.yaml` | namespace별 ingress/egress 허용 규칙 |
| `pdb.yaml` | CPU worker PodDisruptionBudget |

### 2.2 PodDisruptionBudget

| 항목 | Dev | Prod |
|------|-----|------|
| PDB | 없음 | `utterai-cpu-worker-pdb` (namespace: `utterai-ai-cpu`, `minAvailable: 1`) |

GPU worker와 batch worker는 `minReplicaCount: 0`이어서 PDB 대상이 아닙니다.

---

## 3. KEDA ScaledObject

### 3.1 CPU Worker

| 항목 | Dev | Prod |
|------|-----|------|
| minReplicaCount | 1 | 1 |
| maxReplicaCount | 3 | **10** |
| cooldownPeriod | 120s | **300s** |
| activationQueueLength | 설정 없음 | **0** |
| SQS 큐 | `utterai-dev-audio-preprocess-queue` | `utterai-prod-audio-preprocess-queue` |
|  | `utterai-dev-report-analysis-queue` | `utterai-prod-report-analysis-queue` |

`activationQueueLength: 0`은 메시지가 1개라도 있으면 즉시 scale-up을 허용합니다 (기본값 적용 시 메시지 1개에 scale-up 안 되는 버그 방지 — PR #220 수정 사항).

### 3.2 GPU Worker

| 항목 | Dev | Prod |
|------|-----|------|
| minReplicaCount | 0 | 0 |
| maxReplicaCount | 1 | **4** |
| cooldownPeriod | 300s | **600s** |
| scaleDown stabilizationWindow | 300s | **600s** |
| scaleOnInFlight | `"true"` | 설정 없음 |
| SQS 큐 | `utterai-dev-gpu-inference-queue` | `utterai-prod-gpu-inference-queue` |

GPU 인스턴스는 기동 시간이 길어 prod에서 cooldown을 600s로 늘렸습니다. Dev는 `scaleOnInFlight: true`로 in-flight 메시지도 count하여 scale 판단에 포함하지만, prod에서는 제거해 완료된 메시지 기준으로만 판단합니다.

### 3.3 Batch Worker

| 항목 | Dev | Prod |
|------|-----|------|
| minReplicaCount | 0 | 0 |
| maxReplicaCount | 2 | **5** |
| cooldownPeriod | 120s | **300s** |
| SQS 큐 | `utterai-dev-rag-ingest-queue` | `utterai-prod-rag-ingest-queue` |

---

## 4. Karpenter EC2NodeClass

Base(`k8s/platform/karpenter/base/ec2nodeclass.yaml`)는 prod 기본값을 담고, dev overlay가 이를 dev 값으로 덮어씁니다.

### 4.1 default NodeClass

| 항목 | Dev (overlay 적용 후) | Prod (overlay 적용 후) |
|------|----------------------|----------------------|
| IAM Role | `utterai-dev-eks-node-role` | `utterai-prod-eks-node-role` |
| Subnet 탐색 태그 | `karpenter.sh/discovery: utterai-dev-eks` | `karpenter.sh/discovery: utterai-prod` |
| Security Group 탐색 태그 | `karpenter.sh/discovery: utterai-dev-eks` | `karpenter.sh/discovery: utterai-prod` |
| AMI | 기본값 (미지정) | `al2023@latest` |
| EBS 크기 (`/dev/xvda`) | 50Gi gp3 | 50Gi gp3 |

### 4.2 gpu NodeClass

| 항목 | Dev (overlay 적용 후) | Prod (base 그대로) |
|------|----------------------|--------------------|
| IAM Role | `utterai-dev-eks-node-role` | `utterai-prod-eks-node-role` |
| Subnet 탐색 태그 | `karpenter.sh/discovery: utterai-dev-eks` | `karpenter.sh/discovery: utterai-prod` |
| AMI | 기본값 (미지정) | `al2023@latest` |
| EBS 크기 (`/dev/xvda`) | 미지정 (기본값) | **100Gi gp3** |

GPU 노드는 모델 파일 크기를 고려해 prod에서 EBS를 100Gi로 늘렸습니다.

### 4.3 NodePool (공통)

NodePool은 dev/prod 공통으로 base를 그대로 사용합니다 (환경별 overlay 없음).

| NodePool | capacity-type | instance family | instance size | 비고 |
|----------|--------------|----------------|--------------|------|
| system | on-demand | t3, t3a | medium, large | CriticalAddonsOnly taint |
| api | on-demand + spot | t3 | medium | api taint |
| cpu-worker | **on-demand** | m6i | xlarge | 재처리 비용 고려 — spot 제외 |
| batch-worker | **spot** → on-demand | c5, c6i, m5, m6i | xlarge | 중단 후 재시작 허용 |
| gpu | on-demand | g4dn, g5 | xlarge | nvidia.com/gpu taint |

---

## 5. Network Policy

Dev에는 NetworkPolicy가 없습니다. Prod에서는 모든 namespace에 `default-deny-all`을 먼저 적용하고 필요한 통신만 명시적으로 허용합니다.

### 5.1 Backend (`utterai-prod-api`)

| 정책 이름 | 방향 | 허용 대상 |
|-----------|------|-----------|
| `default-deny-all` | Ingress + Egress | 전체 차단 (기본) |
| `allow-ingress-alb` | Ingress | ALB → Pod 8080 |
| `allow-ingress-prometheus` | Ingress | `monitoring` namespace → Pod 8080 |
| `allow-egress-dns` | Egress | UDP/TCP 53 |
| `allow-egress-aws` | Egress | 443 (SQS/S3/Secrets Manager), 5432 (RDS), 6379 (Redis) |
| `allow-egress-otel` | Egress | `utterai-observability` namespace 4318 |
| `allow-egress-ai-api` | Egress | `utterai.io/environment: prod` label 네임스페이스 8080 |

### 5.2 AI Worker

각 namespace(`utterai-ai-cpu`, `utterai-ai-gpu`, `utterai-batch`)에 독립적으로 적용됩니다.

| 정책 이름 | 대상 namespace | 방향 | 허용 대상 |
|-----------|---------------|------|-----------|
| `default-deny-all` | cpu, gpu, batch | Ingress + Egress | 전체 차단 |
| `allow-ingress-prometheus` | cpu, gpu | Ingress | `monitoring` namespace → 8080 |
| `allow-egress-dns` | cpu, gpu, batch | Egress | UDP/TCP 53 |
| `allow-egress-aws` (cpu) | cpu | Egress | 443, 5432, **6379** |
| `allow-egress-aws` (gpu) | gpu | Egress | 443, 5432, **6379** |
| `allow-egress-aws` (batch) | batch | Egress | 443, **5432** |
| `allow-egress-otel` | cpu, gpu, batch | Egress | `utterai-observability` namespace 4318 |

batch worker는 Redis를 사용하지 않아 6379 egress가 없습니다.

---

## 6. 보안 컨텍스트

Dev deployment에는 보안 컨텍스트가 없습니다. Prod에서는 `patch-deployment.yaml`로 아래를 강제합니다.

**Pod 수준 (모든 worker):**

```yaml
securityContext:
  runAsNonRoot: true
  seccompProfile:
    type: RuntimeDefault
```

**컨테이너 수준 (모든 worker):**

```yaml
securityContext:
  allowPrivilegeEscalation: false
  capabilities:
    drop: ["ALL"]
```

---

## 7. EFS PersistentVolume

GPU worker가 모델 파일을 공유하기 위해 EFS를 마운트합니다.

| 항목 | Dev | Prod |
|------|-----|------|
| `volumeHandle` (EFS 파일시스템 ID) | `fs-00a39fe81d32c61d2` | `fs-07c0d3d13a2663887` |
| PVC 이름 | `model-cache-pvc` (namespace: `utterai-ai-gpu`) | 동일 |
| 용량 | 20Gi | 20Gi |
| accessMode | ReadWriteMany | ReadWriteMany |

Prod의 `volumeHandle`은 `patch-efs-pvc.yaml`로 base 값을 덮어씁니다.

---

## 8. 환경변수 / ConfigMap

### 8.1 AI Worker ConfigMap

| 환경변수 | Dev | Prod |
|----------|-----|------|
| `APP_ENV` | (미설정) | `prod` |
| `S3_BUCKET_RAG` | `utterai-dev-documents` | `utterai-prod-rag-ingest` |
| `BEDROCK_REPORT_MODEL_ID` | 없음 | `global.anthropic.claude-haiku-4-5-20251001-v1:0` |
| `SQS_AUDIO_PREPROCESS_QUEUE_URL` | dev SQS URL | prod SQS URL |
| `SQS_GPU_INFERENCE_QUEUE_URL` | dev SQS URL | prod SQS URL |
| `SQS_REPORT_ANALYSIS_QUEUE_URL` | dev SQS URL | prod SQS URL |
| `SQS_RAG_INGEST_QUEUE_URL` | dev SQS URL | prod SQS URL |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | 없음 | `http://otel-collector.utterai-observability.svc.cluster.local:4318` |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | 없음 | `http/protobuf` |

### 8.2 SQS URL 전체 목록

| 큐 | Dev URL | Prod URL |
|----|---------|---------|
| audio-preprocess | `.../utterai-dev-audio-preprocess-queue` | `.../utterai-prod-audio-preprocess-queue` |
| gpu-inference | `.../utterai-dev-gpu-inference-queue` | `.../utterai-prod-gpu-inference-queue` |
| report-analysis | `.../utterai-dev-report-analysis-queue` | `.../utterai-prod-report-analysis-queue` |
| rag-ingest | `.../utterai-dev-rag-ingest-queue` | `.../utterai-prod-rag-ingest-queue` |

모든 SQS URL은 계정 ID `032886669461`, 리전 `ap-northeast-2` 기준입니다.

---

## 후속 작업 (모니터링 팀)

Prod 모니터링 스택 기동을 위해 AWS Secrets Manager에 아래 값이 필요합니다. 현재 시크릿 컨테이너는 생성됐으나 값이 없어 ExternalSecret이 동기화되지 않은 상태입니다.

```bash
# Grafana 관리자 계정 (JSON 형식, 키는 _ 사용)
aws secretsmanager put-secret-value \
  --secret-id "utterai-prod/grafana-admin-credentials" \
  --secret-string '{"admin_user":"admin","admin_password":"<비밀번호>"}'

# Alertmanager Slack Webhook URL
aws secretsmanager put-secret-value \
  --secret-id "utterai-prod/alertmanager-slack-webhook" \
  --secret-string "https://hooks.slack.com/services/..."
```

값 입력 후 강제 동기화:

```bash
kubectl annotate externalsecret grafana-admin-credentials -n monitoring \
  force-sync=$(date +%s) --overwrite

kubectl annotate externalsecret alertmanager-slack-webhook -n monitoring \
  force-sync=$(date +%s) --overwrite
```
