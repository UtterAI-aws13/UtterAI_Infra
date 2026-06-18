# Dev 환경 — CA+HPA → KEDA+Karpenter 전환 가이드

> 작성일: 2026-06-17  
> 대상 환경: `dev` (utterai-dev-eks)  
> 관련 PR: [#180](https://github.com/UtterAI-aws13/UtterAI_Infra/pull/180)

---

## 목차

1. [전환 배경](#1-전환-배경)
2. [Before / After 아키텍처](#2-before--after-아키텍처)
3. [K8s 매니페스트 변경](#3-k8s-매니페스트-변경)
4. [Terraform 변경](#4-terraform-변경)
5. [적용 순서](#5-적용-순서)
6. [ScaledObject 설정 상세](#6-scaledobject-설정-상세)
7. [Karpenter NodePool 설정 상세](#7-karpenter-nodepool-설정-상세)
8. [검증 명령어](#8-검증-명령어)
9. [Phase 2 로드 테스트 실행](#9-phase-2-로드-테스트-실행)
   - [Karpenter Forced Pending 비교 실험](#91-karpenter-forced-pending-비교-실험)

---

## 1. 전환 배경

Phase 1 로드 테스트(CA+HPA)에서 확인된 한계:

| 항목 | Phase 1 (CA+HPA) | Phase 2 (KEDA+Karpenter) |
|------|-----------------|--------------------------|
| Pod 스케일 기준 | CPU 사용률 > 70% | SQS 큐 깊이 (즉시 반응) |
| Pod 스케일 지연 | 수 분 (CPU 임계값 도달 대기) | ~30초 이내 |
| 노드 프로비저닝 | CA → ASG 조정 (~3~5분) | Karpenter 직접 프로비저닝 (~60초) |
| 유휴 노드 비용 | 즉시 축소 안 됨 | GPU: 5분, CPU/Worker: 30초 후 회수 |
| 큐 적체 → 스케일 | 워커가 바빠야 반응 | 큐에 메시지가 쌓이면 즉시 반응 |

CPU 부하 없이 SQS 큐가 쌓이는 시나리오(빠른 메시지 소비)에서는 HPA가 전혀 반응하지 않는다.  
KEDA는 큐 깊이를 직접 관찰하므로 이 문제가 없다.

---

## 2. Before / After 아키텍처

### Phase 1 (CA + HPA)

```
SQS Queue → Worker Pod(소비) → CPU↑ → HPA 감지 → Pod 스케일아웃
                                         (CPU > 70% 조건)
노드 부족 시 → CA → ASG desired↑ → EC2 프로비저닝 (3~5분)
```

### Phase 2 (KEDA + Karpenter)

```
SQS Queue 적체(깊이 임계값) → KEDA 감지 → ScaledObject → Pod 스케일아웃 (~30s)
노드 부족 시 → Karpenter → EC2 직접 프로비저닝 (~60s)
큐 소진 후 → KEDA cooldown → Pod 0으로 축소 → Karpenter 빈 노드 회수
```

---

## 3. K8s 매니페스트 변경

### 3.1 변경 파일 목록

```
k8s/
├── apps/ai-worker/overlays/dev/
│   ├── kustomization.yaml              # HPA 3개 → ScaledObject 3개로 교체
│   ├── keda-trigger-auth.yaml          # NEW: ClusterTriggerAuthentication
│   ├── scaledobject-cpu-worker.yaml    # NEW: cpu-worker KEDA 스케일 설정
│   ├── scaledobject-batch-worker.yaml  # NEW: batch-worker KEDA 스케일 설정
│   └── scaledobject-ml-gpu-worker.yaml # NEW: ml-gpu-worker KEDA 스케일 설정
└── platform/
    ├── dev/kustomization.yaml          # karpenter overlay 추가
    └── karpenter/overlays/dev/
        ├── kustomization.yaml                    # NEW
        ├── patch-ec2nodeclass-default-dev.yaml   # NEW: dev용 EC2NodeClass 패치
        └── patch-ec2nodeclass-gpu-dev.yaml       # NEW: dev GPU EC2NodeClass 패치
```

### 3.2 kustomization.yaml 변경 (dev overlay)

```yaml
# 변경 전
resources:
  - hpa-cpu-worker.yaml       # CPU > 70% 기준
  - hpa-ml-gpu-worker.yaml
  - hpa-batch-worker.yaml

# 변경 후
resources:
  - keda-trigger-auth.yaml        # KEDA가 pod identity로 SQS 접근
  - scaledobject-cpu-worker.yaml
  - scaledobject-ml-gpu-worker.yaml
  - scaledobject-batch-worker.yaml
```

> **주의**: KEDA ScaledObject를 적용하면 내부적으로 HPA를 생성한다.  
> 기존 HPA(`kubectl delete hpa -n <namespace> <name>`)를 먼저 삭제한 뒤 ScaledObject를 적용해야 충돌이 없다.

### 3.3 ClusterTriggerAuthentication

```yaml
# k8s/apps/ai-worker/overlays/dev/keda-trigger-auth.yaml
apiVersion: keda.sh/v1alpha1
kind: ClusterTriggerAuthentication
metadata:
  name: keda-aws-pod-identity
spec:
  podIdentity:
    provider: aws   # KEDA가 워커 Pod의 IRSA를 그대로 사용 (별도 KEDA IRSA 불필요)
```

`identityOwner: pod` 설정과 조합해 KEDA가 각 워커 Pod의 ServiceAccount IRSA 권한으로  
SQS 큐 깊이를 조회한다. 워커 Pod IRSA에 이미 `sqs:GetQueueAttributes`가 부여돼 있으므로  
별도 KEDA 전용 IAM 역할이 필요 없다.

### 3.4 EC2NodeClass 패치 (dev 전용)

`k8s/platform/karpenter/base/ec2nodeclass.yaml`은 prod 기준으로 작성돼 있다.  
Dev overlay에서 다음 값을 패치한다.

| 항목 | base (prod) | dev 패치값 |
|------|-------------|-----------|
| `spec.role` | `utterai-prod-eks-node-role` | `utterai-dev-eks-node-role` |
| `karpenter.sh/discovery` 태그 | `utterai-prod` | `utterai-dev-eks` |

> `utterai-dev-eks`는 VPC 서브넷에 부착된 `karpenter.sh/discovery` 태그 값이다.  
> (`terraform/modules/vpc/main.tf`에서 `var.cluster_name`으로 설정)

---

## 4. Terraform 변경

### 4.1 변경 레이어 요약

| 레이어 | 파일 | 변경 내용 |
|--------|------|-----------|
| `modules/eks` | `main.tf` | 노드 보안그룹에 `karpenter.sh/discovery` 태그 추가 |
| `modules/irsa` | `main.tf`, `outputs.tf` | Karpenter controller IRSA 역할 및 정책 추가 |
| `modules/eks-addons` | `main.tf`, `variables.tf` | KEDA·Karpenter Helm 릴리스 추가, CA conditional |
| `environments/dev/03-services` | `main.tf`, `outputs.tf` | Karpenter 인터럽션 SQS 큐 추가, karpenter_role_arn 출력 |
| `environments/dev/04-addons` | `main.tf` | CA 비활성화, KEDA·Karpenter 활성화 플래그 |

### 4.2 modules/eks — 노드 SG 태그

Karpenter가 EC2NodeClass의 `securityGroupSelectorTerms`로 보안그룹을 찾기 위해  
EKS 노드 보안그룹에 발견 태그를 추가했다.

```hcl
# terraform/modules/eks/main.tf
resource "aws_security_group" "node" {
  tags = {
    Name                     = "${local.prefix}-eks-node-sg"
    "karpenter.sh/discovery" = var.cluster_name   # ← 추가
  }
}
```

### 4.3 modules/irsa — Karpenter IRSA

Karpenter controller가 EC2 인스턴스를 프로비저닝하기 위한 IAM 역할.  
ServiceAccount: `karpenter/karpenter`

주요 허용 권한:

| 권한 | 용도 |
|------|------|
| `ec2:RunInstances`, `ec2:CreateFleet` | 인스턴스 직접 생성 |
| `ec2:TerminateInstances` | 불필요 노드 회수 |
| `iam:PassRole` | 노드 역할 (`utterai-dev-eks-node-role`) 부여 |
| `ssm:GetParameter` | AL2023 AMI ID 자동 조회 |
| `pricing:GetProducts` | 인스턴스 타입 가격 조회 (Spot 선택) |
| `sqs:ReceiveMessage`, `sqs:DeleteMessage` | Spot 인터럽션 이벤트 수신 |
| `iam:CreateInstanceProfile` | 노드 인스턴스 프로파일 관리 |

### 4.4 modules/eks-addons — Helm 릴리스 추가

```hcl
# Cluster Autoscaler: count로 조건부 설치
resource "helm_release" "cluster_autoscaler" {
  count = var.cluster_autoscaler_enabled ? 1 : 0
  ...
}

# KEDA: keda_enabled = true 시 설치
resource "helm_release" "keda" {
  count      = var.keda_enabled ? 1 : 0
  chart      = "keda"
  repository = "https://kedacore.github.io/charts"
  version    = "2.16.1"
  namespace  = "keda"
  ...
}

# Karpenter: karpenter_enabled = true + IRSA ARN 있을 때 설치
resource "helm_release" "karpenter" {
  count      = var.karpenter_enabled && var.karpenter_irsa_role_arn != "" ? 1 : 0
  chart      = "karpenter"
  repository = "oci://public.ecr.aws/karpenter"
  version    = "1.3.3"
  namespace  = "karpenter"
  ...
}
```

새로운 모듈 변수:

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `cluster_autoscaler_enabled` | `true` | false로 설정 시 CA 제거 |
| `keda_enabled` | `false` | KEDA Helm 설치 여부 |
| `karpenter_enabled` | `false` | Karpenter Helm 설치 여부 |
| `karpenter_irsa_role_arn` | `""` | Karpenter controller IRSA ARN |

### 4.5 environments/dev/03-services — 인터럽션 큐

Karpenter가 Spot 인터럽션 및 rebalance 이벤트를 처리하기 위한 SQS 큐.  
큐 이름은 **클러스터 이름과 동일**해야 한다 (`utterai-dev-eks`).

```hcl
resource "aws_sqs_queue" "karpenter_interruption" {
  name                      = var.cluster_name   # "utterai-dev-eks"
  message_retention_seconds = 300
}

resource "aws_cloudwatch_event_rule" "karpenter_spot_interruption" {
  event_pattern = jsonencode({
    source      = ["aws.ec2"]
    detail-type = ["EC2 Spot Instance Interruption Warning",
                   "EC2 Instance Rebalance Recommendation",
                   "EC2 Instance State-change Notification"]
  })
}
```

### 4.6 environments/dev/04-addons — 플래그 설정

```hcl
module "eks_addons" {
  ...
  cluster_autoscaler_enabled = false          # CA 비활성화
  keda_enabled               = true           # KEDA 설치
  karpenter_enabled          = true           # Karpenter 설치
  karpenter_irsa_role_arn    = data.terraform_remote_state.services.outputs.karpenter_role_arn
}
```

---

## 5. 적용 순서

> **전제**: `aws configure` 완료, `kubectl` 컨텍스트 = `utterai-dev-eks`

### Step 1. EKS 노드 SG에 Karpenter 태그 추가

```bash
cd terraform/environments/dev/02-eks
terraform init -reconfigure
terraform plan -var="cluster_name=utterai-dev-eks" \
               -var="rds_instance_class=db.t3.medium" \
               -var="redis_node_type=cache.t3.micro"
# plan 결과에서 aws_security_group.node tags 변경만 포함되는지 확인
terraform apply -var="cluster_name=utterai-dev-eks" \
                -var="rds_instance_class=db.t3.medium" \
                -var="redis_node_type=cache.t3.micro"
```

### Step 2. Karpenter IRSA + 인터럽션 SQS 큐 생성

```bash
cd terraform/environments/dev/03-services
terraform init -reconfigure
terraform plan -var="cluster_name=utterai-dev-eks" \
               -var="rds_instance_class=db.t3.medium" \
               -var="redis_node_type=cache.t3.micro"
# plan 결과: aws_iam_role.karpenter, aws_sqs_queue.karpenter_interruption 등 신규 리소스 확인
terraform apply -var="cluster_name=utterai-dev-eks" \
                -var="rds_instance_class=db.t3.medium" \
                -var="redis_node_type=cache.t3.micro"
```

### Step 3. CA 제거 → KEDA + Karpenter 설치

```bash
cd terraform/environments/dev/04-addons
terraform init -reconfigure
terraform plan
# plan 결과:
#   - helm_release.cluster_autoscaler[0] destroy
#   + helm_release.keda[0] create
#   + helm_release.karpenter[0] create
terraform apply
```

### Step 4. Karpenter NodePool/EC2NodeClass 적용

```bash
kubectl apply -k k8s/platform/dev

# 적용 확인
kubectl get nodepools
kubectl get ec2nodeclasses
```

### Step 5. 기존 HPA 삭제

```bash
kubectl delete hpa utterai-cpu-worker-hpa   -n utterai-ai-cpu  --ignore-not-found
kubectl delete hpa utterai-ml-gpu-worker-hpa -n utterai-ai-gpu  --ignore-not-found
kubectl delete hpa utterai-batch-worker-hpa  -n utterai-batch   --ignore-not-found
```

### Step 6. KEDA ScaledObject 적용

```bash
kubectl apply -k k8s/apps/ai-worker/overlays/dev
```

---

## 6. ScaledObject 설정 상세

| 항목 | cpu-worker | batch-worker | ml-gpu-worker |
|------|-----------|--------------|---------------|
| 대상 Deployment | `utterai-cpu-worker` | `utterai-batch-worker` | `utterai-ml-gpu-worker` |
| 네임스페이스 | `utterai-ai-cpu` | `utterai-batch` | `utterai-ai-gpu` |
| 모니터 큐 | audio-preprocess<br>report-analysis | rag-ingest | gpu-inference |
| `queueLength` (Pod 1개당) | 5 | 3 | 1 |
| `activationQueueLength` | - | 1 | 1 |
| `minReplicaCount` | 0 | 0 | 0 |
| `maxReplicaCount` | 3 | 2 | 1 |
| `cooldownPeriod` | 120s | 120s | 300s |
| scaleDown stabilization | - | - | 300s |

**스케일 계산 예시** (cpu-worker, 100개 메시지 투입):  
`ceil(100 / 5) = 20 Pod 요청` → maxReplicaCount=3 에 의해 3 Pod로 제한

**`activationQueueLength`**: 큐 메시지가 이 값 이상일 때만 0→1 스케일아웃.  
gpu-worker/batch-worker는 1로 설정해 메시지 1개만 있어도 즉시 기동.

---

## 7. Karpenter NodePool 설정 상세

전체 NodePool 정의: `k8s/platform/karpenter/base/nodepools.yaml`

| NodePool | 인스턴스 패밀리 | Capacity Type | 주요 용도 | consolidateAfter |
|----------|----------------|---------------|-----------|-----------------|
| `system` | t3/t3a medium·large | on-demand | 시스템 컴포넌트 | 30s |
| `api` | c5/c6i large·xlarge | spot+on-demand | API 서버 | 30s |
| `worker` | c5/m5/c6i xlarge·2xlarge | spot+on-demand | CPU/Batch 워커 | 30s |
| `gpu` | g4dn/g5 xlarge·2xlarge | on-demand | GPU 워커 | 5m |

- **worker NodePool**: Spot 우선 → 비용 절감, 인터럽션 시 자동 재프로비저닝
- **gpu NodePool**: `WhenEmpty` 정책 → GPU Pod가 없으면 5분 후 노드 회수  
  (GPU 인스턴스는 비싸므로 scaleDown stabilization과 조합해 무분별한 회수 방지)

**EC2NodeClass 패치 (dev 전용)**:

```yaml
spec:
  role: "utterai-dev-eks-node-role"        # dev 노드 IAM 역할
  subnetSelectorTerms:
    - tags:
        karpenter.sh/discovery: "utterai-dev-eks"   # VPC 서브넷 태그와 일치
  securityGroupSelectorTerms:
    - tags:
        karpenter.sh/discovery: "utterai-dev-eks"   # EKS 노드 SG 태그와 일치
```

---

## 8. 검증 명령어

```bash
# KEDA 컨트롤러 상태
kubectl get pods -n keda

# ScaledObject 상태 (READY=True, ACTIVE=False 가 정상 대기 상태)
kubectl get scaledobject -A
kubectl describe scaledobject utterai-cpu-worker-scaledobject -n utterai-ai-cpu

# Karpenter 컨트롤러 상태
kubectl get pods -n karpenter
kubectl logs -n karpenter -l app.kubernetes.io/name=karpenter --tail=50

# Karpenter NodePool 상태
kubectl get nodepools
kubectl get nodeclaims     # Karpenter가 프로비저닝한 노드 요청

# 현재 Pod/HPA 상태 (HPA가 없어야 정상)
kubectl get hpa -A
kubectl get pods -n utterai-ai-cpu
kubectl get pods -n utterai-ai-gpu
kubectl get pods -n utterai-batch
```

---

## 9. Phase 2 로드 테스트 실행

ScaledObject 적용 후 `--phase 2` 옵션으로 실행한다.

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

# cpu-worker: 100개 메시지 → 최대 3 Pod 스케일아웃
python tests/load/send_sqs_messages.py \
  --queue-url "https://sqs.ap-northeast-2.amazonaws.com/${ACCOUNT}/utterai-dev-audio-preprocess-queue" \
  --count 100 --rate 10 --phase 2

# batch-worker: 50개 메시지 → 최대 2 Pod 스케일아웃
python tests/load/send_sqs_messages.py \
  --queue-url "https://sqs.ap-northeast-2.amazonaws.com/${ACCOUNT}/utterai-dev-rag-ingest-queue" \
  --count 50 --rate 5 --phase 2

# gpu-worker: 메시지 1개 → 즉시 Pod 기동 (Karpenter 노드 프로비저닝 포함 ~2분)
python tests/load/send_sqs_messages.py \
  --queue-url "https://sqs.ap-northeast-2.amazonaws.com/${ACCOUNT}/utterai-dev-gpu-inference-queue" \
  --count 1 --rate 1 --phase 2
```

병행 실행 권장:

```bash
# 별도 터미널에서 스케일 이벤트 실시간 관찰
./tests/observe/watch_scaling.sh | tee results/phase2_sqs.log
./tests/observe/measure_scale_time.sh utterai-ai-cpu utterai-cpu-worker
```

### 9.1 Karpenter Forced Pending 비교 실험

CA baseline과 Karpenter를 공정하게 비교하려면 SQS 부하가 아니라 **동일한 Pending Pod 조건**을 만들어야 한다.  
CA는 기존 Managed Node Group/ASG desired size를 늘리고, Karpenter는 Pending Pod의 `nodeSelector`와 resource request를 보고 NodePool 조건에 맞는 EC2를 직접 만든다.

비교 기준:

| 항목 | CA baseline | Karpenter |
|------|-------------|-----------|
| 테스트 매니페스트 | `tests/scenarios/ca-forced-pending.yaml` | `tests/scenarios/karpenter-forced-pending.yaml` |
| 반응 조건 | Pending / Unschedulable Pod | Pending / Unschedulable Pod |
| 노드 생성 방식 | worker Managed Node Group 확장 | `cpu-worker` NodePool에서 EC2 직접 생성 |
| 주요 관찰 대상 | `TriggeredScaleUp`, 노드 수 증가 | `NodeClaim`, NodePool `NODES`, 노드 Ready |
| 핵심 측정값 | Pending 감지 -> Running 전환 시간 | Pending 감지 -> Running 전환 시간 |

실험 전 상태 확인:

```bash
kubectl get pods -n karpenter
kubectl get nodepools
kubectl get nodeclaims
kubectl get nodes -L workload,karpenter.sh/capacity-type
```

관찰 터미널:

```bash
./tests/observe/watch_scaling.sh \
  | tee docs/dev/results/karpenter_forced_pending_watch_$(date +%Y%m%d_%H%M%S).log
```

측정 터미널:

```bash
./tests/observe/measure_scale_time.sh autoscaling-test karpenter-scale-test \
  | tee docs/dev/results/karpenter_forced_pending_measure_$(date +%Y%m%d_%H%M%S).log
```

부하 투입:

```bash
kubectl apply -f tests/scenarios/karpenter-forced-pending.yaml
```

성공 기준:

```text
1. autoscaling-test namespace의 테스트 Pod 일부가 Pending 상태가 된다.
2. Karpenter가 NodeClaim을 생성한다.
3. 신규 노드가 Ready 상태가 된다.
4. Pending Pod가 Running 상태로 전환된다.
5. 측정 로그에 Pending 감지 시간, 노드 Ready 시간, Pod Running 시간이 남는다.
```

정리:

```bash
kubectl delete -f tests/scenarios/karpenter-forced-pending.yaml
```

`NodeClaim READY`가 `Unknown` 상태로 오래 유지되면 Karpenter가 스케일 요청은 만들었지만 EC2 생성 또는 노드 등록 단계에서 막힌 것이다. 이 경우 아래 순서로 원인을 확인한다.

```bash
kubectl describe nodeclaim <nodeclaim-name>
kubectl logs -n karpenter -l app.kubernetes.io/name=karpenter --tail=100
kubectl describe ec2nodeclass default
kubectl get events -A --sort-by=.lastTimestamp | tail -50
```

**예상 결과**:
- 메시지 투입 후 **~30초** 내에 KEDA가 Pod 스케일아웃 요청
- 노드 부족 시 Karpenter가 **~60초** 내에 신규 노드 프로비저닝
- 큐 소진 후 `cooldownPeriod` 경과 → Pod 0으로 축소 → Karpenter 빈 노드 회수
