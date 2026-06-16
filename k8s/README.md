# k8s Kustomize 구조 이해

이 디렉토리는 기존 `k8s-legacy/` 아래에 있던 실제 Kubernetes manifest를 Kustomize 방식으로 옮기기 위한 초안입니다.

현재 기준을 헷갈리지 않기 위해 먼저 구분합니다.

```text
k8s-legacy/
  지금까지 수동 배포와 테스트에 사용하던 실제 Kubernetes manifest 원본

k8s/
  k8s-legacy/의 실제 운영 의도를 Kustomize 구조로 옮기는 마이그레이션 대상
```

즉 `k8s/`는 단순 예제 폴더가 아니라, 앞으로 Argo CD와 GitOps 배포가 바라보게 만들 Kustomize 구조입니다. 다만 아직은 `k8s-legacy/`의 모든 의도가 완전히 옮겨졌는지 계속 비교하면서 다듬어야 합니다.

---

## 1. Kustomize를 왜 쓰는가

기존 `k8s-legacy/` 방식은 여러 YAML 파일을 직접 순서대로 적용하는 방식입니다.

```bash
kubectl apply -f k8s-legacy/namespaces/
kubectl apply -f k8s-legacy/rbac/
kubectl apply -f k8s-legacy/secrets/
kubectl apply -f k8s-legacy/workloads/
kubectl apply -f k8s-legacy/ingress/
```

또는 `scripts/k8s-deploy-legacy.sh`가 `envsubst`로 값을 치환한 뒤 `kubectl apply`를 실행했습니다.

이 방식은 처음에는 단순하지만, 환경이 늘어나면 관리가 어려워집니다.

```text
dev와 prod 값이 어디서 달라지는지 보기 어렵다.
image tag를 자동으로 바꾸기 어렵다.
GitOps에서 "현재 배포 상태"를 명확히 추적하기 어렵다.
공통 설정과 환경별 설정이 섞이기 쉽다.
```

Kustomize는 이 문제를 줄이기 위해 사용합니다.

```text
base:
  dev/prod 공통 Kubernetes 리소스

overlays/dev:
  dev 환경에서만 달라지는 값

overlays/prod:
  prod 환경에서만 달라지는 값
```

---

## 2. 전체 구조

현재 `k8s/` 구조는 다음과 같습니다.

```text
k8s/
├── apps/
│   ├── backend/
│   │   ├── base/
│   │   └── overlays/
│   │       ├── dev/
│   │       └── prod/
│   │
│   └── ai-worker/
│       ├── base/
│       └── overlays/
│           ├── dev/
│           └── prod/
│
└── platform/
    ├── external-secrets/
    │   └── base/
    └── observability/
        └── base/
```

큰 분류는 두 가지입니다.

```text
apps/
  UtterAI 애플리케이션 워크로드
  backend, ai-api, cpu-worker, gpu-worker, batch-worker 등

platform/
  앱이 의존하는 클러스터 공통 리소스
  예: External Secrets Operator가 사용하는 ClusterSecretStore, OpenTelemetry Collector, Grafana dashboard
```

---

## 3. base와 overlays의 차이

### 3.1 base

`base`는 공통 리소스를 모아둔 곳입니다.

예를 들어 backend base에는 다음이 들어갑니다.

```text
Deployment
Service
Ingress
HPA
ConfigMap
ServiceAccount
Role / RoleBinding
ExternalSecret
```

base에는 "이 서비스가 Kubernetes에서 동작하기 위해 기본적으로 필요한 구조"를 둡니다.

### 3.2 overlays/dev

`overlays/dev`는 dev 환경에서만 필요한 값을 둡니다.

예:

```text
dev namespace
dev ECR image tag
dev SQS URL
dev S3 bucket
dev CloudFront origin
dev IRSA role ARN
```

### 3.3 overlays/prod

`overlays/prod`는 prod 환경에서만 필요한 값을 둡니다.

예:

```text
prod namespace
prod image tag
prod domain
prod replica 수
prod HPA 범위
prod 보안 설정
```

현재 `k8s-legacy/` 원본이 dev 실제값 중심이므로, `k8s`도 dev 구조가 더 구체적입니다. prod overlay는 렌더가 깨지지 않도록 유지하면서, 추후 prod 실제값이 확정되면 별도로 보강해야 합니다.

---

## 4. backend Kustomize 구조

위치:

```text
k8s/apps/backend
```

backend는 외부 사용자 요청을 받는 메인 API입니다.

흐름:

```text
User
  -> CloudFront 또는 API 도메인
  -> ALB Ingress
  -> utterai-api-service
  -> active color Deployment
```

### 4.1 backend/base 파일 설명

```text
k8s/apps/backend/base/
├── configmap.yaml
├── deployment-blue.yaml
├── deployment-green.yaml
├── external-secret.yaml
├── hpa-blue.yaml
├── hpa-green.yaml
├── ingress.yaml
├── kustomization.yaml
├── rolebinding.yaml
├── service.yaml
└── serviceaccount.yaml
```

각 파일 역할은 다음과 같습니다.

| 파일 | 역할 |
|---|---|
| `kustomization.yaml` | backend base에 포함될 리소스 목록 |
| `deployment-blue.yaml` | blue 버전 `utterai-api` Pod 실행 정의 |
| `deployment-green.yaml` | green 버전 `utterai-api` Pod 실행 정의 |
| `service.yaml` | 현재 active color Pod 앞에 붙는 ClusterIP Service |
| `ingress.yaml` | ALB Ingress를 통해 외부 요청을 Service로 전달 |
| `hpa-blue.yaml` | blue Deployment CPU 사용률 기준 자동 스케일링 |
| `hpa-green.yaml` | green Deployment CPU 사용률 기준 자동 스케일링 |
| `configmap.yaml` | DB host, Redis host, SQS URL 같은 비민감 설정 |
| `serviceaccount.yaml` | Pod가 사용할 ServiceAccount와 IRSA role 연결 |
| `rolebinding.yaml` | ConfigMap/Secret 조회 권한 |
| `external-secret.yaml` | AWS Secrets Manager 값을 Kubernetes Secret으로 동기화 |

### 4.2 backend blue-green 구조

backend는 blue/green 배포를 위해 Deployment를 두 개로 나눕니다.

```text
Deployment:
  utterai-api-blue
  utterai-api-green

Service:
  utterai-api-service
  selector.color 값으로 blue 또는 green 중 하나를 바라봄

ConfigMap:
  utterai-api-config

Secret:
  backend-api-secret

ServiceAccount:
  utterai-api-sa
```

현재 기본 active color는 `blue`입니다.

```yaml
selector:
  app: utterai-api
  color: blue
```

`color` 값을 `green`으로 바꾸면 Service가 green Pod로 트래픽을 보냅니다.

중요한 동작:

```text
initContainer:
  alembic upgrade head
  애플리케이션 시작 전 DB migration 실행

readinessProbe:
  /health/ready
  실제 트래픽을 받을 준비가 되었는지 확인

livenessProbe:
  /health/live
  프로세스가 살아있는지 확인
```

### 4.3 backend dev overlay

위치:

```text
k8s/apps/backend/overlays/dev
```

역할:

```text
namespace: utterai-api
image: dev tag
dev ConfigMap 값
dev ServiceAccount IRSA role ARN
```

현재 dev image는 `kustomization.yaml`의 `images` 필드에서 관리합니다.

```yaml
images:
  - name: utterai-backend-blue
    newName: 032886669461.dkr.ecr.ap-northeast-2.amazonaws.com/utterai-backend
    newTag: dev-63dd74c
  - name: utterai-backend-green
    newName: 032886669461.dkr.ecr.ap-northeast-2.amazonaws.com/utterai-backend
    newTag: dev-63dd74c
```

GitHub Actions CD는 이 `newTag`를 새 image tag로 바꾸는 PR을 만들게 됩니다.

---

## 5. ai-worker Kustomize 구조

위치:

```text
k8s/apps/ai-worker
```

AI 쪽은 하나의 Pod만 있는 구조가 아닙니다. 기존 `k8s-legacy/` 실제 구조처럼 역할별 namespace가 분리되어 있습니다.

```text
utterai-ai-api:
  Backend가 내부 호출하는 AI API

utterai-ai-cpu:
  음성 전처리와 Bedrock 리포트 생성 CPU worker

utterai-ai-gpu:
  STT/화자분리 등 GPU 기반 ML worker

utterai-batch:
  RAG 문서 ingest batch worker
```

### 5.1 ai-worker/base 파일 설명

```text
k8s/apps/ai-worker/base/
├── ai-api-deployment.yaml
├── ai-api-service.yaml
├── batch-worker-deployment.yaml
├── configmap.yaml
├── cpu-worker-deployment.yaml
├── external-secrets.yaml
├── hpa-batch-worker.yaml
├── hpa-cpu-worker.yaml
├── hpa-ml-gpu-worker.yaml
├── kustomization.yaml
├── ml-gpu-worker-deployment.yaml
├── rolebinding.yaml
└── serviceaccount.yaml
```

각 파일 역할은 다음과 같습니다.

| 파일 | 역할 |
|---|---|
| `ai-api-deployment.yaml` | 내부 AI API 서버 실행 |
| `ai-api-service.yaml` | AI API를 클러스터 내부 Service로 노출 |
| `cpu-worker-deployment.yaml` | CPU 기반 음성 전처리/리포트 worker |
| `ml-gpu-worker-deployment.yaml` | GPU 기반 STT/화자분리 worker |
| `batch-worker-deployment.yaml` | RAG ingest batch worker |
| `configmap.yaml` | S3, SQS, OTel 등 공통 설정 |
| `serviceaccount.yaml` | 각 worker별 IRSA role 연결 |
| `rolebinding.yaml` | 각 namespace 안에서 ConfigMap 조회 권한 |
| `external-secrets.yaml` | HF token, DB 접속정보를 AWS Secrets Manager에서 동기화 |
| `hpa-*.yaml` | worker별 HPA |
| `kustomization.yaml` | AI base 리소스 목록 |

### 5.2 AI API

AI API는 `utterai-ai-api` namespace에서 실행됩니다.

```text
Deployment:
  utterai-ai-api

Service:
  utterai-ai-api-service

Image:
  utterai-ai-cpu
```

AI API는 외부 ALB로 직접 노출하지 않고 ClusterIP Service로만 노출합니다.

### 5.3 CPU Worker

CPU Worker는 `utterai-ai-cpu` namespace에서 실행됩니다.

주요 역할:

```text
audio-preprocess queue 소비
음성 전처리 수행
gpu-inference queue로 전달
report-analysis queue 소비
Bedrock 기반 리포트 생성
```

필요한 값:

```text
HF_TOKEN
SQS_AUDIO_PREPROCESS_QUEUE_URL
SQS_GPU_INFERENCE_QUEUE_URL
SQS_REPORT_ANALYSIS_QUEUE_URL
S3_BUCKET_AUDIO
S3_BUCKET_REPORT
BEDROCK_REGION
```

### 5.4 ML GPU Worker

ML GPU Worker는 `utterai-ai-gpu` namespace에서 실행됩니다.

주요 역할:

```text
gpu-inference queue 소비
GPU 기반 STT/화자분리 수행
report-analysis queue로 전달
```

GPU worker에는 다음 설정이 중요합니다.

```yaml
resources:
  requests:
    nvidia.com/gpu: "1"
  limits:
    nvidia.com/gpu: "1"
```

또한 GPU 노드에 스케줄링되도록 nodeSelector와 toleration이 필요합니다.

```yaml
nodeSelector:
  workload: ai-gpu
```

### 5.5 Batch Worker

Batch Worker는 `utterai-batch` namespace에서 실행됩니다.

역할:

```text
rag-ingest queue 소비
S3 문서를 읽음
DB/vector store에 적재
```

DB 접속 정보는 ConfigMap이 아니라 ExternalSecret이 만든 Kubernetes Secret에서 가져옵니다.

---

## 6. platform Kustomize 구조

### 6.1 External Secrets

위치:

```text
k8s/platform/external-secrets/base
```

현재 포함된 리소스:

```text
ClusterSecretStore
```

`ClusterSecretStore`는 External Secrets Operator가 AWS Secrets Manager에서 값을 읽어오도록 하는 클러스터 공통 설정입니다.

앱별 `ExternalSecret`은 이 공통 store를 참조합니다.

```yaml
secretStoreRef:
  name: aws-secrets-manager
  kind: ClusterSecretStore
```

적용 순서상 `ClusterSecretStore`는 앱의 `ExternalSecret`보다 먼저 있어야 합니다.

### 6.2 Observability

위치:

```text
k8s/platform/observability/base
```

현재 포함된 리소스:

```text
utterai-observability Namespace
OpenTelemetry Collector ConfigMap / Deployment / Service
OpenTelemetry Collector ServiceMonitor
Grafana dashboard ConfigMap
```

앱 Pod는 다음 주소로 telemetry를 전송합니다.

```text
http://otel-collector.utterai-observability.svc.cluster.local:4318
```

`ServiceMonitor`와 Grafana dashboard는 Terraform EKS addon 레이어의 `kube-prometheus-stack` 설치를 전제로 합니다.

필요한 전제:

```text
monitoring namespace
ServiceMonitor CRD
Grafana dashboard sidecar 설정
```

현재 Terraform module의 `kube_prometheus_stack`이 `monitoring` namespace를 생성하고 Prometheus/Grafana 관련 CRD를 설치합니다.

---

## 7. image tag는 어디서 바뀌는가

Kustomize에서는 image 치환을 `kustomization.yaml`의 `images` 필드로 관리합니다.

backend 예시:

```yaml
images:
  - name: utterai-backend
    newName: 032886669461.dkr.ecr.ap-northeast-2.amazonaws.com/utterai-backend
    newTag: dev-63dd74c
```

AI 예시:

```yaml
images:
  - name: utterai-ai-cpu
    newName: 032886669461.dkr.ecr.ap-northeast-2.amazonaws.com/utterai-ai-cpu
    newTag: dev-63dd74c
  - name: utterai-ai-gpu
    newName: 032886669461.dkr.ecr.ap-northeast-2.amazonaws.com/utterai-ai-gpu
    newTag: dev-63dd74c
```

흐름:

```text
App repo GitHub Actions
  -> Docker image build
  -> ECR push
  -> Infra repo workflow 호출
  -> kustomization.yaml의 newTag 변경
  -> GitOps PR 생성
  -> merge
  -> Argo CD sync
```

---

## 8. 수동 테스트 방법

Kustomize는 실제 적용 전에 최종 YAML을 렌더링해서 확인할 수 있습니다.

### 8.1 backend dev 렌더링

```bash
cd UtterAI_Infra

kubectl kustomize k8s/apps/backend/overlays/dev > /tmp/backend-dev.yaml
```

확인:

```bash
grep "namespace:" /tmp/backend-dev.yaml | sort -u
grep "image:" /tmp/backend-dev.yaml
grep "utterai-api" /tmp/backend-dev.yaml
```

실제 dev cluster에 수동 적용:

```bash
kubectl apply -k k8s/apps/backend/overlays/dev
```

상태 확인:

```bash
kubectl get pods -n utterai-api
kubectl get svc -n utterai-api
kubectl get ingress -n utterai-api
kubectl describe pod -n utterai-api <pod-name>
```

### 8.2 ai-worker dev 렌더링

```bash
cd UtterAI_Infra

kubectl kustomize k8s/apps/ai-worker/overlays/dev > /tmp/ai-worker-dev.yaml
```

확인:

```bash
grep "namespace:" /tmp/ai-worker-dev.yaml | sort -u
grep "image:" /tmp/ai-worker-dev.yaml
grep "ExternalSecret" /tmp/ai-worker-dev.yaml
```

실제 dev cluster에 수동 적용:

```bash
kubectl apply -k k8s/apps/ai-worker/overlays/dev
```

상태 확인:

```bash
kubectl get pods -n utterai-ai-api
kubectl get pods -n utterai-ai-cpu
kubectl get pods -n utterai-ai-gpu
kubectl get pods -n utterai-batch
```

### 8.3 platform External Secrets 렌더링

```bash
cd UtterAI_Infra

kubectl kustomize k8s/platform/external-secrets/base > /tmp/platform-external-secrets.yaml
```

수동 적용:

```bash
kubectl apply -k k8s/platform/external-secrets/base
```

### 8.4 platform Observability 렌더링

```bash
cd UtterAI_Infra

kubectl kustomize k8s/platform/observability/base > /tmp/platform-observability.yaml
```

수동 적용:

```bash
kubectl apply -k k8s/platform/observability/base
```

주의:

```text
kube-prometheus-stack이 먼저 설치되어 있어야 ServiceMonitor와 Grafana dashboard가 정상 동작합니다.
```

주의:

```text
External Secrets Operator가 클러스터에 먼저 설치되어 있어야 합니다.
ESO가 AWS Secrets Manager를 읽을 수 있는 IRSA 권한을 가지고 있어야 합니다.
ClusterSecretStore가 먼저 적용되어야 앱별 ExternalSecret이 정상 동작합니다.
```

---

## 9. 적용 순서

수동 테스트를 한다면 순서는 다음이 안전합니다.

```text
1. platform/external-secrets/base
2. platform/observability/base
3. apps/backend/overlays/dev
4. apps/ai-worker/overlays/dev
```

명령어:

```bash
cd UtterAI_Infra

kubectl apply -k k8s/platform/external-secrets/base
kubectl apply -k k8s/platform/observability/base
kubectl apply -k k8s/apps/backend/overlays/dev
kubectl apply -k k8s/apps/ai-worker/overlays/dev
```

다만 운영 원칙상 최종 목표는 수동 apply가 아니라 Argo CD sync입니다.

```text
PR merge
  -> main 변경
  -> Argo CD가 Git 변경 감지
  -> cluster 상태를 Git 상태와 일치시킴
```

---

## 10. 기존 k8s-legacy/와 비교할 때 보는 기준

마이그레이션 작업을 계속할 때는 항상 기존 `k8s-legacy/`와 비교해야 합니다.

확인 기준:

```text
리소스 이름이 같은가?
namespace가 같은가?
ServiceAccount 이름이 같은가?
Secret 이름이 같은가?
ConfigMap key가 빠지지 않았는가?
SQS URL이 같은가?
S3 bucket 이름이 같은가?
health check path가 같은가?
container command가 같은가?
resource requests/limits가 같은가?
nodeSelector/toleration이 같은가?
HPA min/max/behavior가 같은가?
Ingress annotation이 같은가?
```

중요:

```text
k8s에 파일이 있다고 해서 migration이 끝난 것은 아닙니다.
k8s-legacy/ 원본과 비교해서 실제 운영 의도가 보존되어야 migration이 끝난 것입니다.
```

---

## 11. 지금 남아 있는 주의사항

현재 `k8s`는 dev 실제값을 우선 반영한 상태입니다.

주의할 점:

```text
prod overlay는 아직 실제 prod 값이 완성된 상태가 아닙니다.
일부 ARN, endpoint, domain 값은 TODO 또는 placeholder일 수 있습니다.
ExternalSecret이 동작하려면 ESO와 AWS Secrets Manager 권한이 먼저 준비되어야 합니다.
Observability 리소스가 동작하려면 kube-prometheus-stack이 먼저 설치되어 있어야 합니다.
ALB Ingress가 동작하려면 AWS Load Balancer Controller가 설치되어 있어야 합니다.
GPU worker가 동작하려면 GPU node와 NVIDIA device plugin이 준비되어야 합니다.
```

따라서 실제 배포 전에는 다음을 확인해야 합니다.

```bash
kubectl get crd | grep external-secrets
kubectl get crd | grep servicemonitors
kubectl get pods -n external-secrets
kubectl get pods -n monitoring
kubectl get pods -n kube-system | grep aws-load-balancer
kubectl get nodes --show-labels | grep ai-gpu
```

---

## 12. 한 줄 요약

`k8s/`는 기존 `k8s-legacy/`의 실제 운영 manifest를 Kustomize 방식으로 옮겨, 나중에 Argo CD가 안정적으로 배포할 수 있게 만드는 전환용 구조입니다.

```text
k8s-legacy/ 원본을 읽고
  -> k8s/base에 공통 리소스를 두고
  -> k8s/overlays/dev, prod에서 환경별 값을 나누고
  -> kubectl kustomize로 검증하고
  -> Argo CD가 Git 상태를 cluster에 반영하게 만든다.
```
