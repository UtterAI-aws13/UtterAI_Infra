# Kubernetes 매니페스트 배포 방식 가이드

> **현재 상태**: `k8s-legacy/` 폴더 방식(레거시)에서 `k8s/` Kustomize + GitOps 방식으로 전환 완료 (dev 기준).

---

## 목차

1. [두 방식 비교 요약](#1-두-방식-비교-요약)
2. [k8s-legacy/ 방식 — envsubst 직접 주입 (레거시)](#2-k8s-legacy-방식--envsubst-직접-주입-레거시)
3. [k8s/ 방식 — Kustomize + GitOps](#3-k8s-방식--kustomize--gitops)
4. [Kustomize base / overlay 동작 원리](#4-kustomize-base--overlay-동작-원리)
5. [시크릿 및 설정값 관리](#5-시크릿-및-설정값-관리)
6. [ArgoCD GitOps 연동](#6-argocd-gitops-연동)
7. [CI/CD 파이프라인 전체 흐름](#7-cicd-파이프라인-전체-흐름)
8. [dev / prod 환경 전략 비교](#8-dev--prod-환경-전략-비교)
9. [운영 주의사항](#9-운영-주의사항)

---

## 1. 두 방식 비교 요약

| 항목 | `k8s-legacy/` 방식 (레거시) | `k8s/` 방식 (현행) |
|------|---------------------|------------------------|
| 배포 트리거 | 수동 스크립트 실행 | ArgoCD 자동 Sync (GitOps) |
| 환경 값 주입 | `envsubst` (쉘 변수 치환) | Kustomize overlay + dev-config-update workflow |
| 이미지 태그 관리 | ECR 최신 태그 자동 조회 | CD workflow → kustomization.yaml 수정 → PR → ArgoCD |
| 환경 분리 | 없음 (단일 dev) | `overlays/dev`, `overlays/prod` 분리 |
| 배포 이력 | 없음 | Git 커밋 이력으로 모든 배포 추적 |
| 롤백 | kubectl 수동 실행 | Git revert → ArgoCD 자동 반영 |
| 시크릿 관리 | envsubst로 평문 치환 | ExternalSecrets → Secrets Manager |
| Worker 스케일링 | HPA (CPU 기반) | dev: HPA / prod: KEDA ScaledObject (SQS 기반) |
| 노드 스케일링 | Cluster Autoscaler | dev: CA / prod: Karpenter |
| prod 배포 전략 | 미지원 | Blue-Green (backend) |

---

## 2. k8s-legacy/ 방식 — envsubst 직접 주입 (레거시)

### 디렉토리 구조

```
k8s-legacy/
├── namespaces/namespaces.yaml
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
│   ├── api-deployment.yaml       # ${BACKEND_TAG}, ${AWS_ACCOUNT_ID} 포함
│   ├── ai-api-deployment.yaml
│   ├── cpu-worker-deployment.yaml
│   ├── ml-gpu-worker-deployment.yaml
│   ├── batch-worker-deployment.yaml
│   ├── hpa-api.yaml
│   ├── hpa-cpu-worker.yaml
│   ├── hpa-ml-gpu-worker.yaml
│   └── hpa-batch-worker.yaml
├── ingress/api-ingress.yaml      # ${ACM_CERTIFICATE_ARN} 포함
└── observability/
    ├── otel-collector.yaml
    └── grafana-dashboard-ca-karpenter.yaml

scripts/
└── k8s-deploy-legacy.sh                 # 배포 실행 스크립트
```

### k8s-deploy-legacy.sh 동작 순서

```bash
# 1. 환경 변수 수집 (AWS + Terraform output)
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BACKEND_TAG=$(aws ecr describe-images --repository-name utterai-backend ...)
RDS_ENDPOINT=$(terraform output -raw rds_endpoint)
REDIS_ENDPOINT=$(terraform output -raw redis_endpoint)

# 2. envsubst로 플레이스홀더 치환 후 kubectl apply
apply() { envsubst < "$1" | kubectl apply -f -; }

kubectl apply -f k8s-legacy/namespaces/
kubectl apply -f k8s-legacy/observability/
apply k8s-legacy/rbac/serviceaccounts.yaml     # ${AWS_ACCOUNT_ID} 치환
kubectl apply -f k8s-legacy/secrets/
apply k8s-legacy/workloads/*.yaml              # ${BACKEND_TAG} 등 치환
apply k8s-legacy/ingress/*.yaml                # ${ACM_CERTIFICATE_ARN} 치환
```

### 한계점

- **단일 환경**: dev 전용, prod 분리 불가
- **이력 없음**: 어떤 버전이 언제 배포됐는지 Git에 기록 없음
- **롤백 불가**: 이전 상태로 돌아갈 방법 없음
- **시크릿 노출 위험**: envsubst가 민감값을 평문으로 치환 후 apply

---

## 3. k8s/ 방식 — Kustomize + GitOps

### 디렉토리 구조 (현재 기준)

```
k8s/
├── apps/
│   ├── ai-worker/
│   │   ├── base/                              # dev/prod 공통 리소스
│   │   │   ├── kustomization.yaml
│   │   │   ├── configmap.yaml                 # SQS URL 등 (dev-config-update로 갱신)
│   │   │   ├── serviceaccount.yaml            # IRSA ARN (dev-config-update로 갱신)
│   │   │   ├── rolebinding.yaml
│   │   │   ├── ai-worker-external-secret.yaml # DB 자격증명 → Secrets Manager
│   │   │   ├── cpu-worker-external-secret.yaml # HF_TOKEN → Secrets Manager
│   │   │   ├── gpu-worker-external-secret.yaml # HF_TOKEN → Secrets Manager
│   │   │   ├── ai-api-deployment.yaml
│   │   │   ├── cpu-worker-deployment.yaml
│   │   │   ├── ml-gpu-worker-deployment.yaml
│   │   │   └── batch-worker-deployment.yaml
│   │   └── overlays/
│   │       ├── dev/                           # dev: CA + CPU HPA
│   │       │   ├── kustomization.yaml
│   │       │   ├── namespace.yaml
│   │       │   ├── hpa-cpu-worker.yaml        # CPU 70% 기반 HPA
│   │       │   ├── hpa-ml-gpu-worker.yaml
│   │       │   └── hpa-batch-worker.yaml
│   │       └── prod/                          # prod: Karpenter + KEDA
│   │           ├── kustomization.yaml
│   │           ├── namespace.yaml
│   │           ├── keda-trigger-auth.yaml     # ClusterTriggerAuthentication (aws pod identity)
│   │           ├── scaledobject-cpu-worker.yaml    # SQS 기반 (min:1, max:10)
│   │           ├── scaledobject-ml-gpu-worker.yaml # scale-to-zero (min:0, max:4)
│   │           ├── scaledobject-batch-worker.yaml  # scale-to-zero (min:0, max:5)
│   │           ├── patch-configmap.yaml       # prod S3/SQS 버킷명 오버라이드
│   │           ├── patch-deployment.yaml      # securityContext 강화 등
│   │           └── patch-serviceaccount.yaml  # prod IRSA role ARN
│   └── backend/
│       ├── base/                              # dev/prod 공통 리소스
│       │   ├── kustomization.yaml
│       │   ├── configmap.yaml                 # DB_HOST, REDIS_HOST 등 (dev-config-update로 갱신)
│       │   ├── serviceaccount.yaml            # IRSA ARN (dev-config-update로 갱신)
│       │   ├── rolebinding.yaml
│       │   ├── external-secret.yaml           # DB_PASSWORD, JWT_SECRET 등 → Secrets Manager
│       │   ├── service.yaml
│       │   └── ingress.yaml
│       └── overlays/
│           ├── dev/                           # dev: Rolling Update
│           │   ├── kustomization.yaml
│           │   ├── namespace.yaml
│           │   ├── deployment.yaml            # 단일 Deployment
│           │   └── hpa.yaml
│           └── prod/                          # prod: Blue-Green
│               ├── kustomization.yaml
│               ├── namespace.yaml
│               ├── deployment-blue.yaml
│               ├── deployment-green.yaml
│               ├── hpa-blue.yaml / hpa-green.yaml
│               ├── service-blue.yaml / service-green.yaml
│               ├── patch-active-service.yaml  # 현재 active color 기록 (blue/green)
│               ├── patch-configmap.yaml       # prod endpoint 오버라이드
│               ├── patch-deployment.yaml
│               ├── patch-hpa.yaml
│               ├── patch-ingress.yaml
│               └── patch-serviceaccount.yaml
└── platform/
    ├── dev/
    │   └── kustomization.yaml                 # external-secrets + observability + image-pruner 묶음
    ├── external-secrets/base/
    │   ├── kustomization.yaml
    │   └── cluster-secret-store.yaml          # ClusterSecretStore → Secrets Manager
    ├── observability/base/
    │   ├── kustomization.yaml
    │   ├── namespace.yaml
    │   ├── otel-collector.yaml
    │   └── grafana-dashboard-ca-karpenter.yaml
    ├── image-pruner/base/
    │   ├── kustomization.yaml
    │   └── image-pruner.yaml                  # ECR 이미지 정리 CronJob
    └── karpenter/base/                        # prod 전용
        ├── kustomization.yaml
        ├── ec2nodeclass.yaml                  # default (일반) / gpu (100GB EBS)
        └── nodepools.yaml                     # system / api / worker / gpu 4종

deploy/
└── argocd/
    ├── dev/
    │   ├── ai-worker-dev.yaml                 # ArgoCD Application (automated sync)
    │   ├── backend-dev.yaml                   # ArgoCD Application (automated sync)
    │   └── platform-dev.yaml                  # ArgoCD Application (automated sync)
    └── prod/
        ├── ai-worker-prod.yaml                # ArgoCD Application (수동 sync)
        └── backend-prod.yaml                  # ArgoCD Application (수동 sync)
```

---

## 4. Kustomize base / overlay 동작 원리

### base의 역할

base는 dev/prod가 공통으로 사용하는 **리소스 뼈대**입니다. **스케일링 리소스(HPA, ScaledObject)는 base에 두지 않고** dev/prod overlay에 각각 분리합니다.

```
base/                    → Deployment, ConfigMap, ServiceAccount, ExternalSecret
overlays/dev/            → + HPA (CPU 기반)
overlays/prod/           → + ScaledObject + ClusterTriggerAuthentication (KEDA)
```

스케일링 리소스를 base에 두면 dev(HPA)와 prod(ScaledObject)가 서로 다른 리소스 종류를 사용하기 때문에 patch로 교체가 불가능하고, 두 리소스가 동시에 존재하면 충돌이 발생합니다.

### overlay 렌더링 3단계

`kubectl kustomize k8s/apps/ai-worker/overlays/prod` 실행 시:

```
1단계: 리소스 수집
  base의 모든 리소스 로드
  + prod overlay 고유 리소스 추가
    (namespace.yaml, keda-trigger-auth.yaml, scaledobject-*.yaml)

2단계: 패치 적용
  Strategic Merge Patch (patch-configmap.yaml)
    APP_ENV: dev → prod
    S3_BUCKET_*: utterai-dev-* → utterai-prod-*

3단계: 이미지 교체
  images 섹션의 newName / newTag 적용
  utterai-ai-cpu:dev-d609deb → ECR_URL/utterai-ai-cpu:prod-abc1234
```

### 패치 방식

**Strategic Merge Patch** — ConfigMap, Deployment 등 키 단위 병합

```yaml
# patch-configmap.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: utterai-ai-worker-config
data:
  APP_ENV: "prod"
  S3_BUCKET_AUDIO: "utterai-prod-raw-audio"
```

**JSON6902 Patch** — 배열 내 특정 인덱스 경로 교체 (KEDA triggers 등)

```yaml
- op: replace
  path: /spec/triggers/0/metadata/queueURL
  value: "https://sqs.ap-northeast-2.amazonaws.com/PROD/utterai-prod-audio-preprocess-queue"
```

---

## 5. 시크릿 및 설정값 관리

### 값 종류별 처리 방식

| 값 종류 | 예시 | 저장 위치 | 주입 방식 |
|---------|------|-----------|-----------|
| 민감 시크릿 | DB_PASSWORD, JWT_SECRET, HF_TOKEN | Secrets Manager | ExternalSecret → K8s Secret → Pod envFrom |
| DB 접속 정보 | DB_HOST, DB_PORT, DB_NAME, DB_USER | Secrets Manager | ExternalSecret → K8s Secret → Pod envFrom |
| 인프라 엔드포인트 | RDS endpoint, Redis endpoint | Git (base/configmap.yaml) | dev-config-update workflow가 Terraform output으로 갱신 |
| 큐/버킷 이름 | SQS URL, S3 버킷명 | Git (base/configmap.yaml, deployment env) | dev-config-update workflow가 갱신 |
| IRSA role ARN | eks.amazonaws.com/role-arn | Git (base/serviceaccount.yaml) | dev-config-update workflow가 갱신 |
| 이미지 태그 | utterai-backend:dev-ba5a2b8 | Git (overlays/dev/kustomization.yaml) | CD image update workflow가 갱신 |

### ExternalSecrets 구조

```
ClusterSecretStore (aws-secrets-manager)
  └─ AWS Secrets Manager 연결

ExternalSecret (backend-api-external-secret)
  └─ utterai-dev/backend-api-secret → K8s Secret (backend-api-secret)
       DB_PASSWORD, JWT_SECRET_KEY, INTERNAL_CALLBACK_TOKEN, REDIS_AUTH_TOKEN

ExternalSecret (ai-worker-external-secret)
  └─ utterai-dev/ai-worker-secret → K8s Secret (ai-worker-secret)
       DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD

ExternalSecret (cpu-worker-external-secret)
  └─ utterai-dev/cpu-worker-secret → K8s Secret (cpu-worker-secret)
       HF_TOKEN

ExternalSecret (gpu-worker-external-secret)
  └─ utterai-dev/gpu-worker-secret → K8s Secret (gpu-worker-secret)
       HF_TOKEN
```

민감값은 Git에 절대 기록되지 않으며, ExternalSecrets Operator가 런타임에 클러스터 내에서 Secrets Manager를 조회해 K8s Secret을 생성합니다.

---

## 6. ArgoCD GitOps 연동

ArgoCD는 `main` 브랜치의 overlay 경로를 감시하다가 변경이 감지되면 클러스터에 자동 반영합니다.

### dev ArgoCD Application (자동 sync)

```yaml
# deploy/argocd/dev/ai-worker-dev.yaml
spec:
  source:
    repoURL: https://github.com/UtterAI-aws13/UtterAI_Infra
    targetRevision: main
    path: k8s/apps/ai-worker/overlays/dev
  syncPolicy:
    automated:
      prune: true      # Git에서 삭제된 리소스 → 클러스터에서도 삭제
      selfHeal: true   # 클러스터 상태가 Git과 다르면 자동 복구
    syncOptions:
      - CreateNamespace=true
```

### prod ArgoCD Application (수동 sync)

```yaml
# deploy/argocd/prod/backend-prod.yaml
spec:
  source:
    path: k8s/apps/backend/overlays/prod
  syncPolicy:
    # automated 없음 → 수동으로 ArgoCD UI/CLI에서 Sync 실행해야 함
    syncOptions:
      - CreateNamespace=true
```

### Namespace 생성 위치

| Namespace | 생성 파일 |
|-----------|-----------|
| `utterai-api` | `k8s/apps/backend/overlays/dev/namespace.yaml` |
| `utterai-ai-api`, `utterai-ai-cpu`, `utterai-ai-gpu`, `utterai-batch` | `k8s/apps/ai-worker/overlays/dev/namespace.yaml` |
| `utterai-observability` | `k8s/platform/observability/base/namespace.yaml` |

---

## 7. CI/CD 파이프라인 전체 흐름

### 워크플로우 목록

| 파일 | 역할 | 트리거 |
|------|------|--------|
| `ai-kustomize-ci.yaml` | ai-worker overlay 렌더링 검증 | PR / feature 브랜치 push |
| `backend-kustomize-ci.yaml` | backend overlay 렌더링 검증 | PR / feature 브랜치 push |
| `ai-cd-update-image.yaml` | AI 이미지 태그 → kustomization.yaml 수정 PR | `repository_dispatch` / 수동 |
| `backend-cd-update-image.yaml` | backend 이미지 태그 → kustomization.yaml 수정 PR | `repository_dispatch` / 수동 |
| `backend-bluegreen-promote.yaml` | prod Blue-Green 트래픽 전환 | 수동 |
| `dev-config-update.yaml` | Terraform output → base manifest 갱신 PR | 수동 |

---

### 7-1. Kustomize CI — 렌더링 검증

PR 생성 또는 브랜치 push 시 dev/prod overlay가 오류 없이 렌더링되는지 검증합니다.

```
트리거: k8s/apps/ai-worker/** 변경 시 (PR or push)
  ↓
kubectl kustomize k8s/apps/ai-worker/overlays/dev → 렌더링 성공 여부 확인
kubectl kustomize k8s/apps/ai-worker/overlays/prod → 렌더링 성공 여부 확인
namespace, image 포함 여부 검증
```

---

### 7-2. CD Image Update — 이미지 배포

앱 저장소에서 새 이미지를 ECR에 push하면 이 워크플로우가 트리거되어 kustomization.yaml의 이미지 태그를 업데이트하는 PR을 생성합니다.

```
[앱 저장소] 코드 push → ECR 이미지 빌드 → ECR push (dev-{sha} 태그)
    ↓
repository_dispatch (ai-image-pushed 이벤트) → [인프라 저장소]
    ↓
ai-cd-update-image.yaml 실행
    ↓
overlays/dev/kustomization.yaml의 images.newTag를 dev-{sha}로 수정
    ↓
kubectl kustomize 렌더링 검증
    ↓
ci/ai-dev-dev-{sha}-{run_id} 브랜치로 PR 생성
    ↓
Kustomize CI 통과 → 수동 merge (브랜치 보호 규칙 적용)
    ↓
ArgoCD (automated) → main 변경 감지 → 클러스터 자동 sync
    ↓
Rolling Update 실행 → 새 이미지로 Pod 교체
```

**이미지 태그 규칙**

| 환경 | 태그 형식 | 예시 |
|------|----------|------|
| dev | `dev-{short_sha}` | `dev-d609deb` |
| prod | `prod-{short_sha}` 또는 `prod-v{semver}` | `prod-v1.2.0` |

---

### 7-3. Dev Config Update — 인프라 설정 동기화

Terraform으로 인프라가 변경됐을 때 (RDS endpoint, SQS URL, IRSA ARN 등) Git의 base manifest를 최신 상태로 동기화합니다. 이것이 **구 envsubst 방식을 대체하는 GitOps 방식**입니다.

```
수동 실행: workflow_dispatch (GitHub Actions UI)
    ↓
AWS 자격증명 획득 (OIDC → AWS Role Assume)
    ↓
terraform output으로 실제 인프라 값 읽기
  - RDS endpoint, Redis endpoint
  - SQS queue URL (4종)
  - IRSA role ARN (backend, ai-api, cpu-worker, ml-gpu-worker, batch-worker)
  - ECR repository URL
    ↓
Ruby 스크립트로 base manifest 직접 수정
  - backend/base/configmap.yaml  → DB_HOST, REDIS_HOST, SQS URL 갱신
  - backend/base/serviceaccount.yaml → IRSA ARN 갱신
  - ai-worker/base/serviceaccount.yaml → IRSA ARN 갱신 (4개 SA)
  - ai-worker/base/*-deployment.yaml → env의 SQS URL 갱신
  - overlays/dev/kustomization.yaml → ECR newName 갱신 (newTag는 유지)
    ↓
Kustomize 렌더링 검증 + envsubst placeholder 잔존 여부 체크
    ↓
ci/dev-config-{run_id} 브랜치로 PR 생성
    ↓
수동 merge → ArgoCD 자동 sync
```

**언제 실행하는가**
- 최초 dev 환경 구성 후
- Terraform으로 RDS, Redis, SQS 등 인프라 변경 후
- IRSA role ARN이 변경됐을 때

---

### 7-4. Backend Blue-Green Promote — prod 트래픽 전환

prod backend는 Blue-Green 전략으로 이미지 업데이트와 트래픽 전환을 분리합니다.

```
현재 상태: blue = active, green = standby

Step 1. CD Image Update 실행 (target_env=prod, image_tag=prod-v1.1)
  → green Deployment 이미지 태그만 업데이트
  → PR 생성 → 수동 merge → ArgoCD 수동 Sync
  → green Pod가 새 버전으로 교체 (traffic은 여전히 blue)

Step 2. green Pod 동작 확인
  (이 시점 Service는 blue를 바라봄 → 사용자 영향 없음)

Step 3. BlueGreen Promote 실행 (promote_color=green)
  → patch-active-service.yaml의 selector.color: blue → green
  → PR 생성 → 수동 merge → ArgoCD 수동 Sync
  → active Service가 green Pod로 트래픽 전환

Step 4. 문제 발생 시 즉시 롤백
  → Promote 재실행 (promote_color=blue)
  → blue Pod는 그대로 유지되어 있으므로 즉각 전환 가능
```

---

## 8. dev / prod 환경 전략 비교

### 전체 비교표

| 항목 | dev | prod |
|------|-----|------|
| **배포 방식** | Rolling Update | Blue-Green (backend) |
| **ArgoCD Sync** | 자동 (prune + selfHeal) | 수동 |
| **CD PR 머지** | 수동 (브랜치 보호 규칙) | 수동 |
| **인프라 값 갱신** | dev-config-update workflow | prod용 동등 workflow 필요 (미구현) |
| **이미지 태그** | `dev-{short_sha}` | `prod-{short_sha}` |
| **Worker 스케일링** | Cluster Autoscaler + CPU HPA | Karpenter + KEDA ScaledObject (SQS 기반) |
| **GPU worker** | HPA (min:1) | ScaledObject scale-to-zero (min:0) |
| **롤백** | Git revert → ArgoCD 자동 | Promote 재실행 or Git revert → 수동 Sync |

### Worker 스케일링 상세

#### dev: Cluster Autoscaler + CPU HPA

```
SQS 메시지 도착 → Pod CPU 상승까지 대기 (수십 초 지연)
→ HPA가 CPU 70% 초과 감지 → Pod replicas 증가
→ CA가 노드 부족 감지 → ASG 스케일 아웃 (2~4분)
```

| Worker | minReplicas | maxReplicas | 트리거 |
|--------|------------|------------|--------|
| cpu-worker | 1 | 2 | CPU 70% |
| ml-gpu-worker | 1 | 2 | CPU 70% |
| batch-worker | 1 | 5 | CPU 70% |

#### prod: Karpenter + KEDA (SQS 큐 기반)

```
SQS 메시지 도착
→ KEDA가 큐 깊이 즉시 감지
→ Pod replicas 증가 요청
→ Karpenter가 노드 프로비저닝 (30초~1분)
→ GPU worker: 큐 비면 minReplica=0 (비용 절감)
```

| Worker | minReplicas | maxReplicas | queueLength | 비고 |
|--------|------------|------------|-------------|------|
| cpu-worker | 1 | 10 | 5 | 2개 큐 모니터링 |
| ml-gpu-worker | 0 | 4 | 1 | scale-to-zero (GPU 비용 절감) |
| batch-worker | 0 | 5 | 3 | scale-to-zero (RAG 산발적) |

### Karpenter NodePool 구성 (prod)

| NodePool | 인스턴스 | Capacity | 용도 |
|----------|---------|----------|------|
| system | t3/t3a medium~large | on-demand | 시스템 Pod |
| api | c5/c6i large~xlarge | on-demand + spot | backend API |
| worker | c5/m5/m6i xlarge~2xlarge | spot 우선 | CPU worker |
| gpu | g4dn/g5 xlarge~2xlarge | on-demand only | ML GPU worker |

---

## 9. 운영 주의사항

### 이미지 태그 관리 원칙

**kustomization.yaml의 이미지 태그는 CD workflow만 수정해야 합니다.**

구조 변경(리소스 추가/삭제 등) PR 작업 시 `images` 섹션을 건드리면 CD가 이미 채워놓은 실제 태그(`dev-d609deb`)가 `dev-placeholder`로 돌아가 ArgoCD sync 시 `ImagePullBackOff`가 발생합니다.

```
❌ 잘못된 상황
구조 변경 PR 작업 시 kustomization.yaml의 newTag가 dev-placeholder인 상태로 merge
→ ArgoCD sync → dev-placeholder 이미지 pull 시도 → 실패

✅ 올바른 방법
구조 변경 PR에서 images 섹션은 절대 수정하지 않음
이미지 태그 업데이트는 항상 CD workflow가 생성한 PR을 통해서만 적용
```

### CD PR이 auto-merge되지 않는 경우

브랜치 보호 규칙에 리뷰어 승인이 required로 설정되어 있어 CD workflow가 `--auto` 플래그를 달아도 즉시 머지되지 않습니다. 이 경우 CD PR을 수동으로 머지해야 합니다.

```bash
# CI 통과한 CD PR 확인
gh pr list --label "cd"

# 수동 머지
gh pr merge <PR_NUMBER> --squash --delete-branch
```

### prod 배포 전 필수 TODO

prod 환경 구성 전 아래 항목을 채워야 합니다:

- `overlays/prod/scaledobject-*.yaml` — `TODO_PROD_*_QUEUE_URL` → 실제 prod SQS URL
- `overlays/prod/patch-serviceaccount.yaml` — `000000000000` → 실제 prod AWS 계정 ID
- `platform/karpenter/base/ec2nodeclass.yaml` — 실제 prod node role name 확인
- prod VPC 서브넷 / SG에 `karpenter.sh/discovery: utterai-prod` 태그 추가
- prod ExternalSecret의 `utterai-dev/*` key 경로 → `utterai-prod/*` 패치 추가

---

## 부록: 자주 쓰는 명령어

```bash
# overlay 렌더링 검증
kubectl kustomize k8s/apps/ai-worker/overlays/dev
kubectl kustomize k8s/apps/backend/overlays/prod

# ArgoCD 수동 Sync (prod)
argocd app sync utterai-backend-prod
argocd app get utterai-backend-prod

# dev-config-update 수동 실행
gh workflow run dev-config-update.yaml

# CD image update 수동 실행
gh workflow run ai-cd-update-image.yaml \
  --field target_env=dev \
  --field image_tag=dev-d609deb

gh workflow run backend-cd-update-image.yaml \
  --field target_env=dev \
  --field image_tag=dev-ba5a2b8

# GPU inference DLQ 확인 및 replay
aws sqs get-queue-attributes \
  --queue-url https://sqs.ap-northeast-2.amazonaws.com/032886669461/utterai-dev-gpu-inference-dlq \
  --attribute-names ApproximateNumberOfMessages

aws sqs start-message-move-task \
  --source-arn arn:aws:sqs:ap-northeast-2:032886669461:utterai-dev-gpu-inference-dlq \
  --destination-arn arn:aws:sqs:ap-northeast-2:032886669461:utterai-dev-gpu-inference-queue
```
