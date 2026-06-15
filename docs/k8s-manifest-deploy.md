# Kubernetes 매니페스트 배포 방식 가이드

> **현재 상태**: `k8s/` 폴더 방식(레거시)에서 `k8s-demo/` Kustomize + GitOps 방식으로 전환 중입니다.

---

## 목차

1. [두 방식 비교 요약](#1-두-방식-비교-요약)
2. [k8s/ 방식 — envsubst 직접 주입](#2-k8s-방식--envsubst-직접-주입)
3. [k8s-demo/ 방식 — Kustomize + GitOps](#3-k8s-demo-방식--kustomize--gitops)
4. [Kustomize base / overlay 동작 원리](#4-kustomize-base--overlay-동작-원리)
5. [ArgoCD GitOps 연동](#5-argocd-gitops-연동)
6. [CI/CD 파이프라인 전체 흐름](#6-cicd-파이프라인-전체-흐름)
7. [dev / prod 환경 전략 비교](#7-dev--prod-환경-전략-비교)

---

## 1. 두 방식 비교 요약

| 항목 | `k8s/` 방식 | `k8s-demo/` 방식 |
|------|------------|-----------------|
| 배포 트리거 | 수동 스크립트 실행 | ArgoCD 자동 Sync (GitOps) |
| 환경 값 주입 | `envsubst` (쉘 변수 치환) | Kustomize overlay 패치 |
| 이미지 태그 관리 | AWS ECR 최신 태그 자동 조회 | GitHub Actions → kustomization.yaml 직접 수정 |
| 환경 분리 | 없음 (단일 dev 대상) | `overlays/dev`, `overlays/prod` 분리 |
| 배포 이력 | 없음 (apply만 실행) | Git 커밋 이력으로 모든 배포 추적 가능 |
| 롤백 | `kubectl` 수동 실행 | Git revert → ArgoCD 자동 반영 |
| prod 배포 전략 | 미지원 | Blue-Green (backend) / KEDA 기반 Worker |

---

## 2. k8s/ 방식 — envsubst 직접 주입

### 디렉토리 구조

```
k8s/
├── namespaces/
│   └── namespaces.yaml          # 네임스페이스 정의
├── rbac/
│   ├── serviceaccounts.yaml     # ${AWS_ACCOUNT_ID} 플레이스홀더 포함
│   └── rolebindings.yaml
├── secrets/
│   ├── cluster-secret-store.yaml
│   ├── backend-api-external-secret.yaml
│   ├── ai-worker-external-secret.yaml
│   ├── cpu-worker-external-secret.yaml
│   └── gpu-worker-external-secret.yaml
├── workloads/
│   ├── api-deployment.yaml      # ${BACKEND_TAG}, ${AWS_ACCOUNT_ID} 포함
│   ├── ai-api-deployment.yaml
│   ├── cpu-worker-deployment.yaml
│   ├── ml-gpu-worker-deployment.yaml
│   ├── batch-worker-deployment.yaml
│   ├── hpa-api.yaml
│   ├── hpa-cpu-worker.yaml
│   ├── hpa-ml-gpu-worker.yaml
│   └── hpa-batch-worker.yaml
├── ingress/
│   └── api-ingress.yaml         # ${ACM_CERTIFICATE_ARN} 포함
└── observability/
    ├── otel-collector.yaml
    └── grafana-dashboard-ca-karpenter.yaml

scripts/
└── k8s-deploy.sh                # 실제 배포 실행 스크립트
```

### k8s-deploy.sh 동작 순서

```bash
scripts/k8s-deploy.sh
```

스크립트는 다음 순서로 동작합니다:

**1단계: 환경 변수 수집**

```bash
# AWS 계정 ID 조회
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# ECR에서 각 이미지 최신 태그 조회
BACKEND_TAG=$(aws ecr describe-images --repository-name utterai-backend ...)
AI_CPU_TAG=$(aws ecr describe-images --repository-name utterai-ai-cpu ...)
AI_GPU_TAG=$(aws ecr describe-images --repository-name utterai-ai-gpu ...)

# Terraform output에서 인프라 엔드포인트 조회
RDS_ENDPOINT=$(terraform output -raw rds_endpoint)
REDIS_ENDPOINT=$(terraform output -raw redis_endpoint)

# ACM 인증서 ARN 조회
ACM_CERTIFICATE_ARN=$(aws acm list-certificates ...)
```

**2단계: `envsubst`로 플레이스홀더 치환 후 적용**

```bash
apply() {
  envsubst < "$1" | kubectl apply -f -  # 파일 내 ${변수명}을 실제 값으로 치환 후 apply
}

kubectl apply -f k8s/namespaces/          # 1. 네임스페이스
kubectl apply -f k8s/observability/       # 2. Observability
apply k8s/rbac/serviceaccounts.yaml       # 3. RBAC (${AWS_ACCOUNT_ID} 치환)
kubectl apply -f k8s/secrets/             # 4. External Secrets
apply k8s/workloads/*.yaml                # 5. 워크로드 (이미지 태그 치환)
apply k8s/ingress/*.yaml                  # 6. Ingress (ACM ARN 치환)
```

### 한계점

- **단일 환경**: dev 전용으로만 동작하며 prod 환경 분리 없음
- **수동 실행**: 배포 담당자가 직접 스크립트 실행 필요
- **이력 없음**: 어떤 이미지 버전이 언제 배포됐는지 Git에 기록되지 않음
- **롤백 불가**: 이전 상태로 돌아갈 방법이 없음

---

## 3. k8s-demo/ 방식 — Kustomize + GitOps

### 디렉토리 구조

```
k8s-demo/
├── apps/
│   ├── ai-worker/
│   │   ├── base/                          # 공통 리소스 (환경 독립적)
│   │   │   ├── kustomization.yaml         # base가 포함할 파일 목록
│   │   │   ├── configmap.yaml             # dev 기본값으로 작성된 ConfigMap
│   │   │   ├── serviceaccount.yaml
│   │   │   ├── rolebinding.yaml
│   │   │   ├── external-secrets.yaml
│   │   │   ├── ai-api-deployment.yaml
│   │   │   ├── cpu-worker-deployment.yaml
│   │   │   ├── ml-gpu-worker-deployment.yaml
│   │   │   ├── batch-worker-deployment.yaml
│   │   │   ├── ai-api-service.yaml
│   │   │   ├── keda-trigger-auth.yaml     # ClusterTriggerAuthentication
│   │   │   ├── hpa-cpu-worker.yaml        # KEDA ScaledObject (dev SQS URL)
│   │   │   ├── hpa-ml-gpu-worker.yaml     # KEDA ScaledObject (scale-to-zero)
│   │   │   └── hpa-batch-worker.yaml      # KEDA ScaledObject (scale-to-zero)
│   │   └── overlays/
│   │       ├── dev/
│   │       │   ├── kustomization.yaml
│   │       │   ├── namespace.yaml
│   │       │   └── patch-configmap.yaml   # dev 환경값 오버라이드
│   │       └── prod/
│   │           ├── kustomization.yaml
│   │           ├── namespace.yaml
│   │           ├── patch-configmap.yaml   # prod 환경값 오버라이드
│   │           ├── patch-deployment.yaml  # securityContext 등 prod 강화
│   │           ├── patch-serviceaccount.yaml  # prod IRSA role ARN
│   │           ├── patch-scaledobject-cpu-worker.yaml   # prod SQS URL
│   │           ├── patch-scaledobject-ml-gpu-worker.yaml
│   │           └── patch-scaledobject-batch-worker.yaml
│   └── backend/
│       ├── base/
│       │   ├── kustomization.yaml
│       │   ├── configmap.yaml
│       │   ├── serviceaccount.yaml
│       │   ├── rolebinding.yaml
│       │   ├── external-secret.yaml
│       │   ├── service.yaml
│       │   └── ingress.yaml
│       └── overlays/
│           ├── dev/
│           │   ├── kustomization.yaml
│           │   ├── namespace.yaml
│           │   ├── deployment.yaml        # dev Rolling Update 단일 Deployment
│           │   ├── hpa.yaml
│           │   └── patch-active-service.yaml
│           └── prod/
│               ├── kustomization.yaml
│               ├── namespace.yaml
│               ├── deployment-blue.yaml   # Blue-Green 두 개의 Deployment
│               ├── deployment-green.yaml
│               ├── hpa-blue.yaml
│               ├── hpa-green.yaml
│               ├── service-blue.yaml
│               ├── service-green.yaml
│               └── patch-active-service.yaml  # 현재 active color 기록
└── platform/
    ├── karpenter/
    │   └── base/
    │       ├── kustomization.yaml
    │       ├── ec2nodeclass.yaml          # Karpenter EC2NodeClass (default, gpu)
    │       └── nodepools.yaml             # Karpenter NodePool (system, api, worker, gpu)
    ├── external-secrets/
    │   └── base/
    │       ├── kustomization.yaml
    │       └── cluster-secret-store.yaml
    └── observability/
        └── base/
            ├── kustomization.yaml
            ├── namespace.yaml
            ├── otel-collector.yaml
            └── grafana-dashboard-ca-karpenter.yaml

deploy/
└── argocd/
    ├── dev/
    │   ├── backend-dev.yaml               # ArgoCD Application (dev backend)
    │   └── ai-worker-dev.yaml             # ArgoCD Application (dev ai-worker)
    └── prod/
        ├── backend-prod.yaml              # ArgoCD Application (prod backend)
        └── ai-worker-prod.yaml            # ArgoCD Application (prod ai-worker)
```

---

## 4. Kustomize base / overlay 동작 원리

### base란?

base는 환경에 무관한 **공통 리소스의 뼈대**입니다. dev 기본값으로 작성되어 있으며, overlay가 이를 상속해서 환경별로 값을 덮어씁니다.

```yaml
# k8s-demo/apps/ai-worker/base/kustomization.yaml
resources:
  - configmap.yaml        # APP_ENV=dev, SQS URL=dev 큐 URL
  - cpu-worker-deployment.yaml
  - hpa-cpu-worker.yaml   # dev SQS URL이 박혀있는 ScaledObject
  - ...
```

### overlay란?

overlay는 base를 참조하고, 환경별 값만 덮어쓰는 **얇은 레이어**입니다.

```yaml
# k8s-demo/apps/ai-worker/overlays/prod/kustomization.yaml
resources:
  - namespace.yaml
  - ../../base            # base의 모든 리소스를 그대로 가져옴

patches:
  - path: patch-configmap.yaml          # prod SQS URL, APP_ENV=prod로 덮어쓰기
  - path: patch-scaledobject-cpu-worker.yaml  # ScaledObject의 SQS URL만 교체
  - ...

images:
  - name: utterai-ai-cpu
    newName: 032886669461.dkr.ecr.ap-northeast-2.amazonaws.com/utterai-ai-cpu
    newTag: prod-abc1234   # CD workflow가 이 태그를 업데이트함
```

### Kustomize 렌더링 3단계

`kubectl kustomize k8s-demo/apps/ai-worker/overlays/prod` 실행 시:

```
┌──────────────────────────────────────────────────────────┐
│ 1단계: 리소스 수집                                         │
│   base의 모든 리소스 로드 (dev 값 포함)                     │
│   + namespace.yaml 추가                                    │
└────────────────────────┬─────────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────────┐
│ 2단계: 패치 적용                                           │
│                                                           │
│  Strategic Merge Patch (patch-configmap.yaml)             │
│  → ConfigMap의 특정 key만 덮어씀                           │
│    APP_ENV: dev → prod                                    │
│    SQS_URL: utterai-dev-* → utterai-prod-*               │
│                                                           │
│  JSON6902 Patch (patch-scaledobject-*.yaml)               │
│  → 배열 내 특정 경로만 외과적으로 교체                       │
│    /spec/triggers/0/metadata/queueURL → prod URL          │
└────────────────────────┬─────────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────────┐
│ 3단계: 이미지 교체                                         │
│   utterai-ai-cpu:dev-placeholder                          │
│   → 032886669461.dkr.ecr.../utterai-ai-cpu:prod-abc1234 │
└──────────────────────────────────────────────────────────┘
                         │
                         ▼
              완성된 prod 매니페스트 YAML
```

### 패치 방식 두 가지

**① Strategic Merge Patch** — ConfigMap, Deployment 등 단일 리소스 키 병합

```yaml
# patch-configmap.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: utterai-ai-worker-config  # 이름으로 매칭
data:
  APP_ENV: "prod"                 # 이 key만 prod로 교체, 나머지는 base 그대로
  S3_BUCKET_AUDIO: "utterai-prod-raw-audio"
```

**② JSON6902 Patch** — 배열 내 특정 인덱스 경로를 정확히 지정해 교체

```yaml
# patch-scaledobject-cpu-worker.yaml
- op: replace
  path: /spec/triggers/0/metadata/queueURL   # 0번 트리거의 queueURL만 교체
  value: "https://sqs.ap-northeast-2.amazonaws.com/PROD_ACCOUNT/utterai-prod-audio-preprocess-queue"
- op: replace
  path: /spec/triggers/1/metadata/queueURL   # 1번 트리거도 교체
  value: "https://sqs.ap-northeast-2.amazonaws.com/PROD_ACCOUNT/utterai-prod-report-analysis-queue"
```

KEDA ScaledObject의 `triggers`는 배열이기 때문에 Strategic Merge로는 인덱스를 지정할 수 없어 JSON6902를 사용합니다.

---

## 5. ArgoCD GitOps 연동

ArgoCD는 Git 저장소를 지속적으로 감시하다가 `main` 브랜치의 overlay 경로가 변경되면 자동으로 클러스터에 반영합니다.

### ArgoCD Application 정의

```yaml
# deploy/argocd/dev/ai-worker-dev.yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: utterai-ai-worker-dev
  namespace: argocd
spec:
  source:
    repoURL: https://github.com/UtterAI-aws13/UtterAI_Infra
    targetRevision: main                          # main 브랜치 감시
    path: k8s-demo/apps/ai-worker/overlays/dev   # 이 경로의 Kustomize 렌더링 결과를 배포
  destination:
    server: https://kubernetes.default.svc
    namespace: utterai-ai-api
  syncPolicy:
    automated:
      prune: true      # Git에서 삭제된 리소스는 클러스터에서도 삭제
      selfHeal: true   # 클러스터가 Git 상태와 다르면 자동 복구
    syncOptions:
      - CreateNamespace=true
```

```yaml
# deploy/argocd/prod/backend-prod.yaml
spec:
  syncPolicy:
    # automated 블록 없음 = 수동 Sync만 허용 (prod는 자동 배포 금지)
    syncOptions:
      - CreateNamespace=true
```

### dev vs prod syncPolicy 차이

| | dev | prod |
|--|-----|------|
| `automated` | ✅ (prune + selfHeal) | ❌ |
| 배포 방식 | main push 시 자동 반영 | ArgoCD UI/CLI에서 수동 Sync |
| 롤백 방법 | Git revert → 자동 반영 | Git revert → 수동 Sync |

---

## 6. CI/CD 파이프라인 전체 흐름

### 6-1. Kustomize CI — 렌더링 검증 (PR 시 자동 실행)

PR 또는 feature 브랜치 push 시 overlay가 정상적으로 렌더링되는지 검증합니다.

```
.github/workflows/ai-kustomize-ci.yaml
.github/workflows/backend-kustomize-ci.yaml
```

```yaml
# ai-kustomize-ci.yaml 핵심 로직
- name: Render dev overlay
  run: kubectl kustomize k8s-demo/apps/ai-worker/overlays/dev > /tmp/ai-worker-dev.yaml
       grep "namespace: utterai-ai-gpu" /tmp/ai-worker-dev.yaml   # 네임스페이스 존재 확인
       grep "image:" /tmp/ai-worker-dev.yaml                       # 이미지 정의 존재 확인

- name: Render prod overlay
  run: kubectl kustomize k8s-demo/apps/ai-worker/overlays/prod > /tmp/ai-worker-prod.yaml
```

**트리거 조건**: `k8s-demo/apps/ai-worker/**` 또는 `deploy/argocd/**` 변경 시

### 6-2. CD Update Image — 이미지 태그 업데이트

애플리케이션 저장소에서 새 이미지가 ECR에 push되면 이 workflow가 트리거되어 `kustomization.yaml`의 이미지 태그를 업데이트하는 PR을 자동 생성합니다.

```
.github/workflows/ai-cd-update-image.yaml
.github/workflows/backend-cd-update-image.yaml
```

**트리거 방식**:
- `workflow_dispatch`: 수동 실행 (이미지 태그 직접 입력)
- `repository_dispatch`: 애플리케이션 레포에서 `ai-image-pushed` 이벤트 수신

**동작 흐름**:

```
1. target_env, image_tag, ecr_registry 입력 검증
   - dev 환경: image_tag가 "dev-" 로 시작해야 함
   - prod 환경: image_tag가 "prod-" 로 시작해야 함

2. GitHub App 토큰 발급 (UTTERAI_GITOPS_APP)

3. GitOps 저장소 checkout (main 브랜치)

4. kustomization.yaml의 images 섹션을 Ruby 스크립트로 직접 수정
   - utterai-ai-cpu: newTag → 새 이미지 태그
   - utterai-ai-gpu: newTag → 새 이미지 태그

5. kubectl kustomize 렌더링 검증

6. 변경사항을 ci/ai-{env}-{tag}-{run_id} 브랜치로 push

7. PR 생성
   - dev: --auto 플래그로 CI 통과 시 자동 merge
   - prod: 사람이 검토 후 수동 merge
```

**dev 환경 PR 예시**:
```
브랜치: ci/ai-dev-dev-abc1234-27520067219
제목: ci(ai): update dev image tag to dev-abc1234
→ CI 통과 후 자동 squash merge
→ ArgoCD가 main 변경 감지 → 클러스터 자동 반영
```

**prod 환경 PR 예시**:
```
브랜치: ci/ai-prod-prod-abc1234-27520067219
제목: ci(ai): update prod image tag to prod-abc1234
→ 사람이 검토 후 수동 merge
→ ArgoCD가 변경 감지하지만 automated 없으므로 수동 Sync 필요
```

### 6-3. Backend Blue-Green Promote — 트래픽 전환 (prod 전용)

prod backend는 Blue-Green 배포 전략을 사용합니다. 이미지 업데이트와 트래픽 전환을 분리하여 안전하게 배포합니다.

```
.github/workflows/backend-bluegreen-promote.yaml
```

**Blue-Green 배포 전체 흐름**:

```
현재 상태: blue = active(v1.0), green = inactive
새 버전 v1.1 배포 시:

Step 1. Backend CD Update Image 실행 (target_env=prod, image_tag=prod-v1.1)
        → green Deployment의 image tag를 prod-v1.1로 업데이트
        → PR 생성 → 수동 merge
        → ArgoCD Sync → green Pod가 새 버전으로 교체

Step 2. green Pod 동작 확인 (smoke test, 로그 확인 등)
        (이 시점에서 Service는 여전히 blue를 바라봄 → 사용자에게 영향 없음)

Step 3. Backend BlueGreen Promote 실행 (promote_color=green)
        → patch-active-service.yaml의 selector.color: blue → green 으로 변경
        → PR 생성 → 수동 merge
        → ArgoCD Sync → active Service가 green Pod로 트래픽 전환

Step 4. 문제 발생 시 Promote 재실행 (promote_color=blue)
        → 즉시 blue로 롤백 (blue Pod는 그대로 유지되어 있음)
```

**patch-active-service.yaml 구조**:

```yaml
# k8s-demo/apps/backend/overlays/prod/patch-active-service.yaml
apiVersion: v1
kind: Service
metadata:
  name: utterai-api-service
spec:
  selector:
    color: blue   # ← CD workflow가 blue ↔ green 교체
```

---

## 7. dev / prod 환경 전략 비교

### 전체 비교표

| 항목 | dev | prod |
|------|-----|------|
| **배포 방식** | Rolling Update | Blue-Green (backend) |
| **ArgoCD Sync** | 자동 (prune + selfHeal) | 수동 |
| **CD PR 머지** | CI 통과 시 자동 | 사람이 수동 |
| **이미지 태그 접두사** | `dev-{short_sha}` | `prod-{short_sha}` |
| **트래픽 전환** | Deployment 교체 즉시 | Promote workflow 별도 실행 |
| **롤백** | Git revert → 자동 | Promote 재실행 or Git revert → 수동 Sync |
| **Worker 스케일링** | HPA (CPU 기반) → KEDA 전환 예정 | KEDA ScaledObject (SQS 기반) |
| **노드 스케일링** | Cluster Autoscaler (MNG) | Karpenter NodePool |

### dev 배포 흐름 (end-to-end)

```
[앱 저장소] 코드 push → ECR 이미지 빌드 → ECR push
    ↓
[인프라 저장소] repository_dispatch 수신
    ↓
backend-cd-update-image.yaml 실행
    ↓
k8s-demo/apps/backend/overlays/dev/kustomization.yaml 이미지 태그 수정
    ↓
ci/backend-dev-rolling-{tag}-{id} 브랜치 PR 생성
    ↓
backend-kustomize-ci.yaml 실행 (렌더링 검증)
    ↓
CI 통과 → auto squash merge → main 브랜치 반영
    ↓
ArgoCD utterai-backend-dev 앱이 변경 감지
    ↓
kubectl kustomize overlays/dev 렌더링 → 클러스터 apply
    ↓
utterai-api Deployment Rolling Update 실행
```

### prod 배포 흐름 (end-to-end)

```
[앱 저장소] 태그 push (prod-v1.2.0) → ECR 이미지 빌드 → ECR push
    ↓
[인프라 저장소] repository_dispatch 수신
    ↓
backend-cd-update-image.yaml 실행 (target_env=prod)
    ↓
현재 active color 확인 (patch-active-service.yaml 파싱)
active=blue → target=green 결정
    ↓
overlays/prod/kustomization.yaml의 utterai-backend-green 이미지 태그 수정
    ↓
ci/backend-prod-blue-green-{tag}-{id} 브랜치 PR 생성
    ↓
backend-kustomize-ci.yaml 실행 (렌더링 검증)
    ↓
팀원 코드 리뷰 → 수동 merge
    ↓
ArgoCD utterai-backend-prod 앱이 변경 감지하지만 automated 없음
→ 수동으로 ArgoCD Sync 실행
    ↓
green Deployment가 새 이미지로 교체 (blue는 그대로)
    ↓
green Pod 정상 동작 확인 (로그, health check 등)
    ↓
backend-bluegreen-promote.yaml 실행 (promote_color=green)
    ↓
patch-active-service.yaml: selector.color blue → green
    ↓
Promote PR 생성 → 수동 merge → ArgoCD 수동 Sync
    ↓
active Service가 green Pod로 트래픽 전환 완료
```

### Worker 스케일링 전략 차이

#### dev (현재): Cluster Autoscaler + HPA (CPU 기반)
```
메시지 도착 → CPU 사용률 증가 기다림 (수십 초 지연)
→ HPA가 CPU 70% 초과 감지 → Pod 스케일 아웃
→ CA가 노드 부족 감지 → ASG 스케일 아웃 (2~4분)
```

#### prod (목표): Karpenter + KEDA (SQS 큐 기반)
```
SQS 메시지 도착
→ KEDA ScaledObject가 큐 깊이 감지 (즉시)
→ Pod 스케일 아웃 요청
→ Karpenter가 새 노드 프로비저닝 (30초~1분)
→ GPU worker: 메시지 없으면 minReplica=0 (비용 절감)
```

| Worker | KEDA minReplica | maxReplica | queueLength | 비고 |
|--------|----------------|------------|-------------|------|
| cpu-worker | 1 | 10 | 5 | 2개 큐 모니터링 |
| ml-gpu-worker | 0 | 4 | 1 | **scale-to-zero** (GPU 비용 절감) |
| batch-worker | 0 | 5 | 3 | **scale-to-zero** (RAG 인제스트 산발적) |

---

## 부록: 자주 쓰는 명령어

### Kustomize 렌더링 확인

```bash
# dev overlay 렌더링
kubectl kustomize k8s-demo/apps/ai-worker/overlays/dev

# prod overlay 렌더링
kubectl kustomize k8s-demo/apps/backend/overlays/prod

# 렌더링 결과를 클러스터에 적용 (ArgoCD 없이 수동 배포)
kubectl kustomize k8s-demo/apps/ai-worker/overlays/dev | kubectl apply -f -
```

### ArgoCD 수동 Sync

```bash
# prod backend 수동 Sync
argocd app sync utterai-backend-prod

# Sync 상태 확인
argocd app get utterai-backend-prod
```

### DLQ 메시지 replay

```bash
# GPU inference DLQ 메시지 수 확인
aws sqs get-queue-attributes \
  --queue-url https://sqs.ap-northeast-2.amazonaws.com/032886669461/utterai-dev-gpu-inference-dlq \
  --attribute-names ApproximateNumberOfMessages

# DLQ → 원본 큐로 메시지 이동 (AWS 콘솔 또는 CLI)
aws sqs start-message-move-task \
  --source-arn arn:aws:sqs:ap-northeast-2:032886669461:utterai-dev-gpu-inference-dlq \
  --destination-arn arn:aws:sqs:ap-northeast-2:032886669461:utterai-dev-gpu-inference-queue
```
