### AWS 13기 최종프로젝트 | 날짜: 2026-06-01

## EKS 플랫폼 아키텍처 구현 방향

| 번호 | 주제 | 세부 내용 |
| --- | --- | --- |
| 1 | 역할 정의 | Terraform 기반 EKS 클러스터와 Kubernetes 플랫폼 레이어를 구성한다. |
| 2 | VPC 연동 | VPC, Public Subnet, Private App Subnet, Private Data Subnet과 EKS를 연결한다. |
| 3 | EKS Cluster | system Managed Node Group과 Karpenter 기반 동적 NodePool을 함께 사용한다. |
| 4 | Namespace 전략 | API, AI CPU, AI GPU, Batch, Platform, Observability 영역을 namespace로 분리한다. |
| 5 | ALB Ingress | AWS Load Balancer Controller로 CloudFront/API 요청을 EKS 서비스로 라우팅한다. |
| 6 | Karpenter | Pod 요구사항에 맞춰 general, api, ai-cpu, ai-gpu, spot-batch 노드를 자동 생성한다. |
| 7 | KEDA | SQS 메시지 수를 기준으로 CPU/GPU/Batch Worker Pod를 자동 확장한다. |
| 8 | Scheduling Policy | taint, toleration, nodeSelector, GPU resource request로 워크로드별 노드 배치를 통제한다. |
| 9 | IRSA/RBAC | AWS 권한은 IRSA, Kubernetes 내부 권한은 RBAC로 분리한다. |
| 10 | Resource 기준 | request/limit 기준을 정해 스케줄링, Karpenter 증설, KEDA 확장의 기준값을 만든다. |

---

## 1. 내 파트의 역할 정의

### 1.1 한 줄 정의

내가 맡은 파트는 **EKS 위에서 API, AI CPU Worker, AI GPU Worker, Batch Worker가 안정적으로 배포되고 자동 확장될 수 있도록 Kubernetes 플랫폼 레이어를 설계하고 구현하는 역할**이다.

즉, 백엔드 API나 AI 모델 로직 자체를 개발하는 것이 아니라, 다음 항목을 책임진다.

- EKS Cluster 생성
- VPC와 EKS 연결
- AWS Load Balancer Controller 설치
- ALB Ingress 구성
- Karpenter 설치
- NodePool / EC2NodeClass 설계
- `general`, `api`, `ai-cpu`, `ai-gpu`, `spot-batch` NodePool 구성
- workload별 node 배치 정책 설계
- GPU 워크로드 전용 taint / toleration / resource request 기준 정의
- KEDA 기반 SQS Consumer 자동 확장
- IRSA / ServiceAccount / RBAC 기본 구조 설계
- resource request / limit 기준 정의

### 1.2 전체 서비스 안에서의 위치

```text
사용자
  ↓
Route 53
  ↓
CloudFront
  ├─ /              → S3 Frontend
  └─ /api/*         → ALB Ingress
                        ↓
                     API Service
                        ↓
                     API Pods
                        ↓
                  SQS Analysis Queue
                        ↓
                 CPU/GPU 작업 분기
                    ↓          ↓
          AI CPU Worker    AI GPU Worker
                    ↓          ↓
                 KEDA      KEDA
                    ↓          ↓
              ai-cpu NodePool  ai-gpu NodePool
                    ↓          ↓
                 분석 결과 저장 / 리포트 생성
```

이 흐름에서 내 파트는 **CloudFront 이후 API 요청이 EKS 내부로 들어오고, API/AI/Batch Pod가 적절한 노드에 배치되며, SQS 부하에 따라 Pod와 Node가 함께 확장되는 구조**를 담당한다.

### 1.3 GPU가 필요한 이유

이번 프로젝트의 AI 처리 중 일부는 CPU만으로도 가능하지만, **화자 분리(Speaker Diarization)** 같은 작업은 음성 길이와 모델 크기에 따라 처리 시간이 길어질 수 있다. 따라서 다음과 같이 AI 작업을 CPU와 GPU로 분리한다.

| AI 작업 | 추천 실행 위치 | 이유 |
| --- | --- | --- |
| VAD, 오디오 구간 자르기 | `ai-cpu NodePool` | CPU로도 충분히 처리 가능 |
| 간단한 후처리, 점수 계산 | `ai-cpu NodePool` | CPU/Memory 중심 작업 |
| RAG 검색 전처리 | `ai-cpu NodePool` 또는 Bedrock 연동 API | GPU 필요성 낮음 |
| 화자 분리 | `ai-gpu NodePool` | 모델 추론 시간이 길고 GPU 가속 효과가 큼 |
| 무거운 ASR/STT 추론 | `ai-gpu NodePool` 선택 가능 | 모델 크기와 목표 처리 시간에 따라 GPU 필요 |
| 리포트 생성 | Bedrock + API/Worker | LLM 호출 중심이므로 GPU 직접 운영 불필요 |

핵심 설계 기준은 다음이다.

```text
가벼운 AI 작업 → ai-cpu NodePool
무거운 모델 추론 → ai-gpu NodePool
재시도 가능한 일반 배치 → spot-batch NodePool
사용자 API 요청 → api NodePool
```

---

## 2. 설계 전제

### 2.1 전체 아키텍처 전제

현재 프로젝트는 다음과 같은 구조를 기준으로 한다.

```text
Frontend:
- S3 Static Website
- CloudFront
- Route 53
- WAF

Backend/API:
- EKS 내부 API Pod
- ALB Ingress를 통한 외부 진입
- Cognito 인증 연동 가능

Async Processing:
- SQS Analysis Queue
- SQS DLQ
- CPU Analysis Worker
- GPU Diarization Worker
- Batch/Consumer Worker

AI/RAG:
- S3 Raw Audio
- S3 Documents
- S3 Vector Store
- Amazon Bedrock
- S3 Reports / Artifacts
- S3 Processed Audio
- GPU 기반 화자 분리 Worker

Data:
- Aurora PostgreSQL Writer
- Aurora Read Replica
- ElastiCache Redis

Platform:
- EKS
- AWS Load Balancer Controller
- Karpenter
- KEDA
- NVIDIA Device Plugin
- IRSA
- CloudWatch / OpenTelemetry / Prometheus / Grafana
```

### 2.2 Dev 환경 기준

처음부터 Prod 수준으로 모든 것을 구성하면 구현 범위가 커지므로, Dev 환경에서는 다음 수준까지 구현하는 것을 1차 목표로 둔다.

```text
Dev 1차 목표:
- VPC output을 받아 EKS Cluster 생성
- system Managed Node Group 생성
- AWS Load Balancer Controller 설치
- Karpenter 설치
- KEDA 설치
- NVIDIA Device Plugin 설치
- namespace 기본 구조 생성
- general / api / ai-cpu / ai-gpu / spot-batch NodePool 생성
- 샘플 API Deployment + Ingress 검증
- 샘플 CPU Worker + KEDA scale 검증
- 샘플 GPU Worker + nvidia.com/gpu request 검증
```

### 2.3 Prod 확장 전제

Prod에서는 다음 요소를 추가 고려한다.

```text
Prod 확장 고려:
- Multi-AZ 기반 private subnet 배치
- Dev/Prod 계정 분리
- Argo CD 또는 GitHub Actions 기반 GitOps 배포
- WAF 룰 강화
- PodDisruptionBudget 적용
- HPA/KEDA/Cluster Autoscaling 기준 분리
- GPU NodePool 최소 warm capacity 운영 검토
- GPU 모델 이미지 크기와 cold start 시간 측정
- CloudWatch / Prometheus / Grafana 대시보드 구성
- DR Region Warm Standby 연동
```

---

## 3. Terraform 구조 방향

### 3.1 디렉터리 구조

Terraform과 Kubernetes manifest는 다음과 같이 나누는 것이 좋다.

```text
infra/
  envs/
    dev/
      main.tf
      variables.tf
      outputs.tf
      terraform.tfvars
    prod/
      main.tf
      variables.tf
      outputs.tf
      terraform.tfvars

  modules/
    vpc/
      main.tf
      variables.tf
      outputs.tf

    eks/
      main.tf
      variables.tf
      outputs.tf

    eks-addons/
      aws-load-balancer-controller.tf
      karpenter.tf
      keda.tf
      metrics-server.tf
      nvidia-device-plugin.tf

    irsa/
      lbc-irsa.tf
      karpenter-irsa.tf
      keda-irsa.tf
      api-irsa.tf
      ai-cpu-irsa.tf
      ai-gpu-irsa.tf
      batch-irsa.tf

  k8s/
    namespaces/
      namespace.yaml

    nodepools/
      ec2nodeclass-default.yaml
      ec2nodeclass-gpu.yaml
      general-nodepool.yaml
      api-nodepool.yaml
      ai-cpu-nodepool.yaml
      ai-gpu-nodepool.yaml
      spot-batch-nodepool.yaml

    rbac/
      serviceaccounts.yaml
      roles.yaml
      rolebindings.yaml

    ingress/
      api-ingress.yaml

    workloads-sample/
      api-deployment.yaml
      cpu-worker-deployment.yaml
      gpu-worker-deployment.yaml
      batch-consumer-deployment.yaml
      keda-cpu-scaledobject.yaml
      keda-gpu-scaledobject.yaml
      keda-batch-scaledobject.yaml
```

### 3.2 Terraform에서 담당할 것과 Kubernetes manifest에서 담당할 것

| 구분 | Terraform | Kubernetes Manifest / Helm |
| --- | --- | --- |
| VPC | 생성 또는 기존 VPC output 참조 | 해당 없음 |
| EKS Cluster | 생성 | 해당 없음 |
| Managed Node Group | 생성 | 해당 없음 |
| IAM Role | 생성 | 해당 없음 |
| OIDC Provider | 생성 | 해당 없음 |
| IRSA IAM Policy | 생성 | ServiceAccount annotation 연결 |
| AWS Load Balancer Controller | Helm release 가능 | Ingress 리소스 작성 |
| Karpenter | Helm release 가능 | NodePool / EC2NodeClass 작성 |
| KEDA | Helm release 가능 | ScaledObject 작성 |
| NVIDIA Device Plugin | Helm 또는 DaemonSet 설치 | GPU Pod에서 `nvidia.com/gpu` request 사용 |
| Namespace | Terraform kubernetes provider 또는 manifest | manifest 권장 |
| App Deployment | Terraform 비권장 | manifest / Helm / Argo CD 권장 |

### 3.3 설계 선택 이유

Terraform은 **AWS 리소스와 클러스터 기반 리소스**를 만들고, Kubernetes manifest는 **애플리케이션 배포 정책**을 담당하게 나누는 것이 좋다.

이렇게 나누면 다음 장점이 있다.

- 인프라 변경과 애플리케이션 배포 변경이 섞이지 않는다.
- Terraform state가 너무 복잡해지지 않는다.
- 백엔드/AI 팀이 Kubernetes manifest만 수정하면서 배포 실험을 하기 쉽다.
- Argo CD 같은 GitOps 도구를 붙이기 쉽다.
- GPU Worker, CPU Worker, API Deployment를 독립적으로 배포할 수 있다.

---

## 4. VPC 연동 방향

### 4.1 VPC 구조

EKS는 기존 VPC와 연동하는 구조로 잡는다.

```text
VPC 10.0.0.0/16

Public Subnet
├─ ALB
├─ NAT Gateway
└─ Bastion 또는 관리용 리소스 선택 가능

Private App Subnet
├─ EKS system node
├─ EKS api node
├─ EKS ai-cpu node
├─ EKS ai-gpu node
└─ EKS spot-batch node

Private Data Subnet
├─ Aurora PostgreSQL
├─ Aurora Read Replica
└─ ElastiCache Redis
```

### 4.2 EKS 노드 위치

EKS Worker Node는 기본적으로 **Private App Subnet**에 배치한다.

```text
ALB:
- Public Subnet
- internet-facing
- CloudFront 또는 사용자 요청을 받음

EKS Worker Node:
- Private App Subnet
- 외부에서 직접 접근 불가
- NAT Gateway 또는 VPC Endpoint로 외부 AWS 서비스 접근

Aurora / Redis:
- Private Data Subnet
- EKS Node Security Group에서만 접근 허용
```

### 4.3 VPC 담당자에게 받아야 하는 output

3개 AZ를 사용하려면 VPC 담당자에게 다음 값을 받아야 한다.

```hcl
vpc_id                 = "vpc-xxxx"
public_subnet_ids      = ["subnet-public-a", "subnet-public-b", "subnet-public-c"]
private_app_subnet_ids = ["subnet-app-a", "subnet-app-b", "subnet-app-c"]
private_data_subnet_ids = ["subnet-data-a", "subnet-data-b", "subnet-data-c"]
nat_gateway_ids        = ["nat-a", "nat-b", "nat-c"]
```

Karpenter가 Subnet과 Security Group을 자동으로 찾을 수 있게 태그도 맞춰야 한다.

```text
Public Subnet Tag (3개 AZ 모두):
- kubernetes.io/role/elb = 1

Private App Subnet Tag (3개 AZ 모두):
- kubernetes.io/role/internal-elb = 1
- karpenter.sh/discovery = utterai-dev

Node Security Group Tag:
- karpenter.sh/discovery = utterai-dev
```

### 4.4 3개 AZ 설정 방법

3개 AZ를 실제로 적용하려면 다음 세 레이어에서 모두 설정해야 한다.

#### 레이어 1: Terraform — EKS 클러스터와 Managed Node Group에 3개 서브넷 지정

EKS 컨트롤 플레인과 Managed Node Group 생성 시 3개 AZ의 서브넷을 모두 넘겨야 EKS가 각 AZ에 노드를 배치할 수 있다.

```hcl
# eks module 호출 예시
module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.0"

  cluster_name    = "utterai-dev"
  cluster_version = "1.31"

  vpc_id     = var.vpc_id
  # 3개 AZ 서브넷을 모두 지정한다
  subnet_ids = var.private_app_subnet_ids  # ["subnet-app-a", "subnet-app-b", "subnet-app-c"]

  eks_managed_node_groups = {
    system = {
      instance_types = ["t3.large"]
      min_size       = 2
      max_size       = 4
      desired_size   = 3

      # Managed Node Group도 3개 AZ 서브넷 지정
      subnet_ids = var.private_app_subnet_ids
    }
  }
}
```

여기서 `subnet_ids`에 3개 AZ 서브넷을 모두 넣으면 EKS가 노드를 3개 AZ에 분산 배치한다.

#### 레이어 2: Karpenter EC2NodeClass — 서브넷 태그로 자동 분산

Karpenter는 `subnetSelectorTerms`의 태그로 서브넷을 자동으로 찾는다. 3개 AZ 서브넷 모두에 같은 태그를 붙이면 Karpenter가 알아서 3개 AZ에 분산해 노드를 생성한다. EC2NodeClass 자체는 변경이 필요 없다.

```yaml
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: default
spec:
  amiFamily: AL2023
  role: utterai-dev-karpenter-node-role
  subnetSelectorTerms:
    - tags:
        karpenter.sh/discovery: utterai-dev  # 3개 AZ 서브넷 모두 이 태그를 가져야 한다
  securityGroupSelectorTerms:
    - tags:
        karpenter.sh/discovery: utterai-dev
```

특정 AZ에만 노드를 만들고 싶으면 NodePool의 `requirements`에 AZ를 명시한다.

```yaml
# 3개 AZ 모두 허용하는 경우 (기본값, 권장)
requirements:
  - key: topology.kubernetes.io/zone
    operator: In
    values: ["ap-northeast-2a", "ap-northeast-2b", "ap-northeast-2c"]
```

```yaml
# GPU 인스턴스 가용성이 특정 AZ에 몰릴 때 AZ를 좁히는 경우
requirements:
  - key: topology.kubernetes.io/zone
    operator: In
    values: ["ap-northeast-2a", "ap-northeast-2c"]
```

GPU 인스턴스(g5, g6)는 모든 AZ에서 동일하게 지원되지 않는 경우가 있으므로, GPU NodePool은 인스턴스 가용성을 확인한 후 AZ 범위를 결정한다.

#### 레이어 3: Kubernetes manifest — topologySpreadConstraints로 Pod AZ 분산

노드가 3개 AZ에 분산되어 있어도 Pod가 특정 AZ에 몰릴 수 있다. `topologySpreadConstraints`를 사용하면 Pod를 AZ에 균등하게 분산시킬 수 있다.

```yaml
# API Deployment 예시 — AZ 분산 적용
spec:
  template:
    spec:
      topologySpreadConstraints:
        - maxSkew: 1
          topologyKey: topology.kubernetes.io/zone
          whenUnsatisfiable: DoNotSchedule
          labelSelector:
            matchLabels:
              app: utter-api
      nodeSelector:
        workload: api
      tolerations:
        - key: dedicated
          operator: Equal
          value: api
          effect: NoSchedule
```

| 필드 | 의미 |
| --- | --- |
| `maxSkew: 1` | AZ 간 Pod 수 차이를 최대 1개로 제한 |
| `topologyKey: topology.kubernetes.io/zone` | AZ를 기준으로 분산 |
| `whenUnsatisfiable: DoNotSchedule` | 조건 불만족 시 Pending 유지 (엄격한 분산) |
| `whenUnsatisfiable: ScheduleAnyway` | 조건 불만족 시에도 최선 배치 (느슨한 분산) |

API와 같이 안정성이 중요한 워크로드는 `DoNotSchedule`, GPU Worker처럼 노드 수가 적은 워크로드는 `ScheduleAnyway`가 적합하다.

#### 3개 AZ 설정 요약

```text
[VPC 담당자]
  └─ 3개 AZ에 서브넷 생성 및 태그 부착 (a, b, c)

[Terraform]
  └─ EKS module subnet_ids에 3개 AZ 서브넷 전달
  └─ Managed Node Group subnet_ids에 3개 AZ 서브넷 전달

[Karpenter EC2NodeClass]
  └─ subnetSelectorTerms 태그가 3개 AZ 서브넷에 모두 붙어있으면 자동 분산
  └─ (선택) NodePool requirements에 zone 명시 가능

[Kubernetes Manifest]
  └─ topologySpreadConstraints로 Pod를 AZ에 균등 분산
```

### 4.5 GPU Node를 위한 VPC 주의점

GPU Node는 일반 Node보다 인스턴스 크기가 크고 비용도 높다. 따라서 다음을 확인해야 한다.

#### IP 여유 확인

EKS는 기본적으로 **VPC CNI**를 사용하는데, 이 방식은 Pod 하나당 VPC IP 하나를 서브넷에서 직접 할당한다. 즉 Pod가 늘어날수록 서브넷 IP가 그만큼 소비된다.

```text
서브넷 CIDR 예시:
- 10.0.1.0/24 → 사용 가능한 IP 251개 (256 - AWS 예약 5개)
- 10.0.0.0/22 → 사용 가능한 IP 1019개
- 10.0.0.0/21 → 사용 가능한 IP 2043개
```

GPU 인스턴스는 일반 인스턴스보다 IP 소비가 더 크다. Karpenter와 EKS VPC CNI는 노드 생성 시 실제 Pod 수와 무관하게 **IP를 미리 예약(warm pool)**해 두는데, 인스턴스 크기가 클수록 ENI 수와 ENI당 IP 수가 많아 예약량이 늘어난다.

```text
인스턴스별 ENI / IP 예시:
- t3.medium:   ENI 3개 × IP 6개  = 최대 18개 IP
- m6i.2xlarge: ENI 4개 × IP 15개 = 최대 60개 IP
- g5.2xlarge:  ENI 4개 × IP 15개 = 최대 60개 IP
- g5.12xlarge: ENI 8개 × IP 30개 = 최대 240개 IP
```

GPU 노드가 몇 개 없어도 인스턴스 크기가 크면 서브넷 IP를 많이 잡아간다.

```text
/24 서브넷 (IP 251개) 예시:
  system 노드 2개 (t3.large) × 18 IP  = 36개
  api 노드 3개   (m6i.large) × 30 IP  = 90개
  GPU 노드 2개   (g5.2xlarge) × 60 IP = 120개
  ───────────────────────────────────
  합계 246개 → /24 거의 소진
  → 이 상태에서 노드/Pod 추가 생성 시 IP 부족으로 실패
```

IP 부족이 발생하면 다음 증상이 나타난다.

```text
IP 부족 증상:
- 새 Pod가 Pending 상태에서 멈춤
- Karpenter가 노드를 생성하려다 실패
- kubectl describe pod에서 "Failed to allocate address" 에러
- kubectl describe node에서 IP exhaustion 관련 이벤트
```

**권장 대응 방법:**

1. **서브넷 CIDR를 넉넉하게 설계한다 (VPC 담당자와 협의)**

```text
Private App Subnet 권장:
- Dev:  AZ당 /24 이상 (IP 251개)
- Prod: AZ당 /22 이상 (IP 1019개)

3개 AZ이면 3개 서브넷 모두 동일하게 설계한다.
```

2. **VPC CNI prefix delegation을 활성화한다 (IP 효율 향상)**

prefix delegation을 켜면 ENI에 IP를 개별로 붙이는 대신 `/28` 블록(16개 IP)을 통째로 할당한다. 같은 서브넷 CIDR에서 더 많은 Pod를 수용할 수 있다.

```hcl
# Terraform EKS module 예시
eks_managed_node_groups = {
  system = {
    # ...
  }
}

# VPC CNI addon 설정
cluster_addons = {
  vpc-cni = {
    configuration_values = jsonencode({
      env = {
        ENABLE_PREFIX_DELEGATION = "true"
        WARM_PREFIX_TARGET       = "1"
      }
    })
  }
}
```

3. **별도 서브넷을 만들어 GPU 노드를 분리한다 (선택)**

비용이 크거나 IP 격리가 필요한 경우 GPU 노드 전용 서브넷을 별도로 만들고 EC2NodeClass에 지정한다.

```yaml
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: gpu
spec:
  subnetSelectorTerms:
    - tags:
        karpenter.sh/discovery: utterai-dev
        workload: gpu   # GPU 전용 서브넷에만 이 태그를 붙인다
```

#### 기타 주의점

- GPU Node가 S3, ECR, CloudWatch, SQS에 접근할 수 있어야 한다.
- NAT Gateway 비용을 줄이려면 VPC Endpoint를 검토한다.
- GPU 모델 이미지가 크면 ECR pull 시간이 길어질 수 있다.
- 모델 파일을 S3에서 다운로드한다면 S3 Gateway Endpoint 또는 Interface Endpoint 구성을 검토한다.

---

## 5. EKS Cluster 구조

### 5.1 기본 구조

EKS는 다음과 같은 구조로 구성한다.

```text
EKS Cluster
├─ Managed Node Group: system
│  ├─ CoreDNS
│  ├─ kube-proxy
│  ├─ VPC CNI
│  ├─ AWS Load Balancer Controller
│  ├─ Karpenter Controller
│  ├─ KEDA Operator
│  ├─ metrics-server
│  └─ NVIDIA Device Plugin
│
└─ Karpenter NodePools
   ├─ general
   ├─ api
   ├─ ai-cpu
   ├─ ai-gpu
   └─ spot-batch
```

### 5.2 system node group에서 실행되는 서비스

#### CoreDNS

Kubernetes 클러스터 내부의 DNS 서버다. Pod끼리 서비스 이름으로 통신할 수 있게 해준다.

```text
역할:
- Service 이름을 ClusterIP로 변환
- Pod 간 내부 통신의 기반

예시:
- cpu-worker가 "utter-api-service"로 요청하면
  CoreDNS가 해당 Service의 ClusterIP를 반환
- 없으면 Pod 간 이름 기반 통신 불가
```

#### kube-proxy

각 노드에서 실행되는 네트워크 프록시다. Service로 들어오는 트래픽을 실제 Pod IP로 전달하는 iptables/ipvs 규칙을 관리한다.

```text
역할:
- Service ClusterIP → 실제 Pod IP로 트래픽 라우팅
- 노드마다 DaemonSet으로 실행됨

예시:
- ClusterIP:8080으로 온 요청을
  뒤에 있는 Pod 3개 중 하나로 로드밸런싱
```

#### VPC CNI (aws-node)

EKS 전용 CNI 플러그인으로, Pod에 VPC IP를 직접 할당한다.

```text
역할:
- Pod 생성 시 서브넷 IP를 Pod에 직접 부여
- Pod와 VPC 내 다른 리소스(Aurora, Redis 등)가
  같은 네트워크 계층에서 직접 통신 가능

예시:
- Pod IP: 10.0.2.45 (서브넷 IP 그대로 사용)
- Aurora에서 Pod IP를 직접 인식 가능
- 오버레이 네트워크 없이 성능 손실 최소화
```

#### AWS Load Balancer Controller

Kubernetes Ingress/Service 리소스를 보고 AWS ALB/NLB를 자동으로 생성하고 관리한다.

```text
역할:
- Ingress 리소스 → ALB 생성
- Service(LoadBalancer 타입) → NLB 생성
- 타겟 그룹, 리스너, 헬스체크 자동 관리

예시:
- utter-api-ingress를 apply하면
  ALB가 자동 생성되고 api Pod로 트래픽 연결
- 없으면 Ingress를 만들어도 ALB가 생기지 않음
```

#### Karpenter Controller

Pending 상태의 Pod를 감지해 적합한 EC2 노드를 자동으로 생성하고, 불필요한 노드를 제거한다.

```text
역할:
- Pending Pod의 nodeSelector, toleration,
  resource request를 분석
- 적합한 NodePool의 EC2 인스턴스 자동 생성
- 비어있거나 활용률이 낮은 노드 자동 삭제(consolidation)

예시:
- GPU Worker Pod가 Pending되면
  ai-gpu NodePool 기준으로 g5 인스턴스 생성
- 없으면 Pending Pod가 영원히 배치되지 않음
```

#### KEDA Operator

외부 이벤트 소스(SQS, Kafka 등)를 기준으로 Deployment의 replica 수를 자동으로 조절한다.

```text
역할:
- SQS 메시지 수를 주기적으로 폴링
- 설정한 queueLength 기준 초과 시 replica 증가
- 메시지 감소 시 replica 감소 (0까지 가능)

예시:
- cpu-analysis-queue 메시지가 50개 쌓이면
  queueLength: 5 기준으로 cpu-worker 10개로 확장
- 없으면 SQS 기반 자동 확장 불가 (HPA로는 SQS 연동 안 됨)
```

#### metrics-server

각 노드와 Pod의 CPU/Memory 사용량을 수집한다. HPA와 `kubectl top` 명령어의 기반이 된다.

```text
역할:
- 노드/Pod의 실시간 CPU, Memory 사용량 수집
- HPA가 scale 기준으로 사용하는 메트릭 제공
- kubectl top nodes / kubectl top pods 지원

예시:
- HPA가 "CPU 70% 초과 시 replica 증가" 설정이 있으면
  metrics-server가 수집한 값을 기준으로 판단
- 없으면 HPA 동작 불가, kubectl top 명령 불가
```

#### NVIDIA Device Plugin

GPU 노드에서 `nvidia.com/gpu` 리소스를 Kubernetes가 인식할 수 있도록 등록한다.

```text
역할:
- GPU 노드에서 DaemonSet으로 실행
- 노드의 GPU 수를 Kubernetes에 리소스로 등록
- Pod의 nvidia.com/gpu request/limit 처리

예시:
- g5.2xlarge 노드 (GPU 1개) 생성 시
  Device Plugin이 해당 노드에 nvidia.com/gpu: 1 등록
- GPU Worker Pod가 nvidia.com/gpu: 1 요청하면
  해당 노드에만 스케줄링
- 없으면 GPU Pod가 어느 노드에나 배치되려 하고
  GPU를 실제로 사용하지 못함
```

### 5.3 왜 system Managed Node Group이 필요한가

Karpenter는 노드를 자동으로 만들어주는 도구이지만, Karpenter 자체도 Kubernetes Pod로 실행된다. 따라서 Karpenter가 실행될 최소한의 기본 노드가 필요하다.

이 역할을 `system` Managed Node Group이 담당한다.

```text
system node group 역할:
- Karpenter Controller 실행
- KEDA Operator 실행
- AWS Load Balancer Controller 실행
- CoreDNS 실행
- metrics-server 실행
- 클러스터 필수 addon 안정적 운영
```

### 5.4 system node group 추천값

Dev 기준:

```text
instance type: t3.medium 또는 t3.large
min size: 1
desired size: 2
max size: 3
capacity type: on-demand
```

Prod 기준:

```text
instance type: t3.large 또는 m6i.large
min size: 2
desired size: 2~3
max size: 4
capacity type: on-demand
```

### 5.5 Karpenter NodePool 구성

Karpenter로 생성할 NodePool은 다음과 같다.

| NodePool | 역할 | Capacity | 특징 |
| --- | --- | --- | --- |
| `general` | 일반 Pod, 테스트 Pod | On-Demand 또는 Spot 혼합 | 명확히 분리되지 않은 작업 수용 |
| `api` | 백엔드 API Pod | On-Demand | 사용자 요청 처리, 안정성 우선 |
| `ai-cpu` | CPU 기반 AI Worker | On-Demand | VAD, 전처리, 후처리, RAG 보조 |
| `ai-gpu` | GPU 기반 AI Worker | On-Demand 우선 | 화자 분리, 무거운 STT/추론 |
| `spot-batch` | 일반 비동기 batch worker | Spot | 재시도 가능한 작업, 비용 절감 |

---

## 6. Namespace 전략

### 6.1 Namespace 목록

Namespace는 다음과 같이 나눈다.

```text
kube-system
platform-system
ingress-system
karpenter
keda
utter-api
utter-ai-cpu
utter-ai-gpu
utter-batch
utter-observability
```

### 6.2 Namespace별 역할

| Namespace | 역할 |
| --- | --- |
| `kube-system` | EKS 기본 컴포넌트 |
| `platform-system` | 공통 플랫폼 컴포넌트 |
| `ingress-system` | AWS Load Balancer Controller |
| `karpenter` | Karpenter Controller |
| `keda` | KEDA Operator |
| `utter-api` | 백엔드 API 서버 |
| `utter-ai-cpu` | CPU 기반 AI Worker |
| `utter-ai-gpu` | GPU 기반 AI Worker |
| `utter-batch` | 일반 SQS Consumer, batch worker |
| `utter-observability` | Prometheus, Grafana, OpenTelemetry Collector |

### 6.3 Namespace 분리 이유

Namespace를 분리하는 이유는 다음과 같다.

- API, CPU AI, GPU AI, Batch의 배포 주기를 분리할 수 있다.
- ServiceAccount와 IRSA 권한을 namespace별로 다르게 줄 수 있다.
- ResourceQuota와 LimitRange를 다르게 적용할 수 있다.
- GPU 워크로드가 일반 API 워크로드와 섞이지 않도록 운영 정책을 명확히 할 수 있다.
- 장애 발생 시 어느 영역에서 문제가 생겼는지 추적하기 쉽다.

### 6.4 Namespace 예시 Manifest

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: utter-api
---
apiVersion: v1
kind: Namespace
metadata:
  name: utter-ai-cpu
---
apiVersion: v1
kind: Namespace
metadata:
  name: utter-ai-gpu
---
apiVersion: v1
kind: Namespace
metadata:
  name: utter-batch
```

---

## 7. AWS Load Balancer Controller와 ALB Ingress

### 7.1 외부 진입 구조

프론트엔드는 S3 + CloudFront로 제공하고, API 요청만 EKS로 보낸다.

```text
사용자
  ↓
Route 53
  ↓
CloudFront
  ├─ /             → S3 Frontend
  └─ /api/*        → ALB Ingress
                       ↓
                    API Service
                       ↓
                    API Pod
```

### 7.2 ALB Ingress를 사용하는 이유

API 요청은 HTTP/HTTPS 기반의 L7 요청이다. 따라서 경로 기반 라우팅, 헬스체크, TLS 연동, WAF 연동이 가능한 ALB가 적합하다.

```text
ALB Ingress 장점:
- /api 경로 기반 라우팅 가능
- health check path 지정 가능
- target-type ip로 Pod 직접 라우팅 가능
- WAF 연결 가능
- ACM 인증서 연결 가능
```

### 7.3 Ingress 예시

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: utter-api-ingress
  namespace: utter-api
  annotations:
    alb.ingress.kubernetes.io/scheme: internet-facing
    alb.ingress.kubernetes.io/target-type: ip
    alb.ingress.kubernetes.io/healthcheck-path: /health/ready
    alb.ingress.kubernetes.io/listen-ports: '[{"HTTP":80},{"HTTPS":443}]'
spec:
  ingressClassName: alb
  rules:
    - host: api.dev.utterai.com
      http:
        paths:
          - path: /api
            pathType: Prefix
            backend:
              service:
                name: utter-api-service
                port:
                  number: 8080
```

### 7.4 API Service 예시

```yaml
apiVersion: v1
kind: Service
metadata:
  name: utter-api-service
  namespace: utter-api
spec:
  type: ClusterIP
  selector:
    app: utter-api
  ports:
    - port: 8080
      targetPort: 8080
```

### 7.5 백엔드 팀과 맞춰야 할 값

| 항목 | 예시 |
| --- | --- |
| API container port | `8080` |
| readiness path | `/health/ready` |
| liveness path | `/health/live` |
| API base path | `/api` |
| Service name | `utter-api-service` |
| Namespace | `utter-api` |

---

## 8. Karpenter 설계 방향

### 8.1 Karpenter의 역할

Karpenter는 Pending 상태의 Pod를 보고 필요한 EC2 Node를 자동으로 생성한다.

```text
Pod 생성
  ↓
스케줄러가 기존 노드에 배치 시도
  ↓
자원이 부족하거나 조건에 맞는 노드가 없음
  ↓
Pod Pending
  ↓
Karpenter가 Pending Pod의 조건 확인
  ↓
nodeSelector / toleration / resource request 분석
  ↓
적절한 NodePool 선택
  ↓
EC2 Node 생성
  ↓
Pod 배치
```

### 8.2 EC2NodeClass와 NodePool 역할 차이

| 리소스 | 역할 |
| --- | --- |
| `EC2NodeClass` | AWS EC2 관련 설정을 정의한다. Subnet, SecurityGroup, AMI, IAM Role, BlockDevice 등을 지정한다. |
| `NodePool` | Kubernetes 스케줄링 정책을 정의한다. label, taint, instance type, capacity type, CPU limit 등을 지정한다. |

쉽게 말하면 다음과 같다.

```text
EC2NodeClass:
- 어떤 AWS 환경에 EC2를 만들 것인가?

NodePool:
- 어떤 Pod를 위해 어떤 조건의 Node를 만들 것인가?
```

### 8.3 NodePool 전체 구성

```text
Karpenter NodePools
├─ general
│  └─ 일반 Pod, 테스트 Pod
│
├─ api
│  └─ 사용자 요청 처리 API Pod
│
├─ ai-cpu
│  └─ VAD, 전처리, RAG 보조, 후처리 Worker
│
├─ ai-gpu
│  └─ 화자 분리, 무거운 STT/추론 Worker
│
└─ spot-batch
   └─ 재시도 가능한 일반 batch 작업
```

### 8.4 GPU NodePool 추가 설계 포인트

GPU NodePool은 일반 NodePool과 다르게 다음 요소가 필요하다.

```text
GPU NodePool 필요 요소:
- GPU 인스턴스 타입 선택
- GPU 지원 AMI 또는 NVIDIA driver 구성
- NVIDIA Device Plugin 설치
- Pod에서 nvidia.com/gpu resource request 사용
- GPU Node 전용 taint 적용
- GPU Worker에만 toleration 부여
- GPU 모델 이미지 pull 시간과 cold start 시간 측정
```

GPU NodePool은 비용이 높으므로 모든 AI Pod를 올리는 것이 아니라, **GPU가 필요한 Worker만 명확하게 배치**해야 한다.

---

## 9. NodePool 상세 설계

### 9.1 전체 NodePool 요약

| NodePool | 용도 | Capacity | 배치 기준 | 비용/안정성 관점 |
| --- | --- | --- | --- | --- |
| `system` | 클러스터 핵심 addon | On-Demand | Managed Node Group | 안정성 최우선 |
| `general` | 일반 Pod, 테스트 Pod | On-Demand 또는 Spot | `workload=general` | 유연성 |
| `api` | 백엔드 API | On-Demand | `workload=api` | 사용자 요청 안정성 |
| `ai-cpu` | CPU 기반 AI 처리 | On-Demand | `workload=ai-cpu` | CPU/Memory 자원 격리 |
| `ai-gpu` | GPU 기반 AI 처리 | On-Demand 우선 | `workload=ai-gpu`, `nvidia.com/gpu` | 화자 분리/무거운 추론 전용 |
| `spot-batch` | 일반 비동기 batch | Spot | `workload=batch` | 비용 절감 |

### 9.2 general NodePool

`general` NodePool은 명확하게 분리되지 않은 일반 Pod나 테스트 Pod를 수용한다.

```yaml
metadata:
  labels:
    workload: general
```

추천 용도:

- 테스트용 Pod
- 임시 Job
- 중요도가 낮은 내부 도구
- PoC 단계의 작은 Worker

주의할 점은 운영 핵심 서비스는 general에 두지 않는 것이다.

```text
중요 서비스 배치 원칙:
- API는 api NodePool
- CPU AI는 ai-cpu NodePool
- GPU AI는 ai-gpu NodePool
- 일반 Batch는 spot-batch NodePool
```

### 9.3 api NodePool

`api` NodePool은 사용자 요청을 직접 처리하는 백엔드 API Pod 전용이다.

```text
api NodePool 역할:
- 인증된 사용자 요청 처리
- Presigned URL 발급
- 분석 요청 생성
- SQS 메시지 발행
- 결과 조회 API 처리
- DB/Redis 접근
```

API는 사용자 응답 시간에 직접 영향을 주기 때문에 Spot보다 On-Demand가 적합하다.

```yaml
labels:
  workload: api

taints:
  - key: dedicated
    value: api
    effect: NoSchedule
```

API Pod는 다음과 같이 배치한다.

```yaml
nodeSelector:
  workload: api

tolerations:
  - key: dedicated
    operator: Equal
    value: api
    effect: NoSchedule
```

### 9.4 ai-cpu NodePool

`ai-cpu` NodePool은 CPU와 Memory를 많이 사용하는 AI 관련 작업을 API 서버와 분리하기 위한 노드풀이다.

```text
ai-cpu NodePool 역할:
- VAD
- 오디오 전처리
- 짧은 음성 feature 추출
- RAG 검색 보조
- 통계/점수 계산
- 리포트 생성 전 데이터 정리
- CPU 기반 가벼운 모델 추론
```

추천 인스턴스 계열:

```text
m 계열: 범용 CPU/Memory 균형
c 계열: CPU 중심 처리
r 계열: 메모리 사용량이 큰 전처리
```

배치 정책:

```yaml
labels:
  workload: ai-cpu

taints:
  - key: dedicated
    value: ai-cpu
    effect: NoSchedule
```

AI CPU Pod 예시:

```yaml
nodeSelector:
  workload: ai-cpu

tolerations:
  - key: dedicated
    operator: Equal
    value: ai-cpu
    effect: NoSchedule
```

API NodePool과 분리하는 이유는 다음과 같다.

```text
API와 AI CPU를 섞으면 생기는 문제:
- AI 작업이 CPU를 많이 사용하면 API 응답 지연 발생
- API Pod scale-out과 AI Pod scale-out이 같은 노드 자원을 경쟁
- 장애 원인 분리가 어려워짐
- 비용/성능 튜닝 기준이 모호해짐
```

따라서 `ai-cpu` NodePool을 별도로 둔다.

### 9.5 ai-gpu NodePool

`ai-gpu` NodePool은 GPU가 필요한 AI 작업을 전용으로 처리한다.

```text
ai-gpu NodePool 역할:
- 화자 분리 모델 추론
- 긴 음성 파일 diarization 처리
- GPU가 필요한 ASR/STT 추론
- GPU 가속이 필요한 대형 음성 모델 처리
```

이번 프로젝트에서는 특히 **화자 분리(Speaker Diarization)** 작업을 GPU NodePool에 배치하는 방향으로 잡는다.

```text
화자 분리 작업 흐름:
S3 Raw Audio
  ↓
CPU 전처리 Worker
  ↓
SQS GPU Diarization Queue
  ↓
KEDA ScaledObject
  ↓
GPU Diarization Worker Pod
  ↓
ai-gpu NodePool
  ↓
S3 Processed Audio / Aurora Metadata 저장
```

GPU NodePool은 다음 기준으로 설계한다.

| 항목 | 기준 |
| --- | --- |
| Capacity | Dev는 On-Demand 0~1개, Prod는 On-Demand 우선 |
| Scale 방식 | KEDA로 GPU Worker Pod 증가, Karpenter로 GPU Node 생성 |
| 배치 조건 | `nodeSelector: workload=ai-gpu` |
| 격리 조건 | `taint: dedicated=ai-gpu:NoSchedule` |
| GPU 요청 | `resources.limits.nvidia.com/gpu: 1` |
| 비용 관리 | min 0 가능, 단 cold start 측정 필요 |
| 안정성 | 긴 처리 작업은 visibility timeout, checkpoint, DLQ 설계 필요 |

GPU NodePool label/taint 예시:

```yaml
labels:
  workload: ai-gpu

taints:
  - key: dedicated
    value: ai-gpu
    effect: NoSchedule
```

GPU Worker Pod 배치 예시:

```yaml
nodeSelector:
  workload: ai-gpu

tolerations:
  - key: dedicated
    operator: Equal
    value: ai-gpu
    effect: NoSchedule

resources:
  requests:
    cpu: "2"
    memory: "8Gi"
    nvidia.com/gpu: "1"
  limits:
    cpu: "4"
    memory: "16Gi"
    nvidia.com/gpu: "1"
```

중요한 점은 `nvidia.com/gpu`는 CPU처럼 초과 사용이 가능한 자원이 아니라, 일반적으로 정수 단위로 요청한다는 것이다. GPU 1장을 쓰는 Worker는 `nvidia.com/gpu: 1`로 명확히 요청한다.

#### GPU NodePool을 별도로 두는 이유

```text
GPU NodePool 분리 이유:
- GPU 인스턴스는 비용이 높다.
- 일반 API Pod가 GPU Node에 올라가면 비용 낭비가 발생한다.
- GPU Worker는 이미지와 모델 파일이 커서 cold start가 길 수 있다.
- GPU 장애와 API 장애를 분리해서 볼 수 있다.
- GPU 사용률, 처리 시간, queue backlog를 별도로 관찰해야 한다.
```

### 9.6 spot-batch NodePool

`spot-batch` NodePool은 SQS Consumer처럼 재시도 가능한 비동기 작업을 처리한다.

```text
spot-batch NodePool 역할:
- 일반 batch 작업
- 리포트 후처리
- 알림 발송
- 실패 시 재처리 가능한 작업
- DLQ로 보낼 수 있는 작업
```

Spot을 사용하는 이유는 다음과 같다.

```text
Spot 사용이 적합한 이유:
- 사용자 요청을 직접 처리하지 않음
- SQS 기반 재시도 가능
- 실패 시 DLQ 처리 가능
- 비용 절감 효과가 큼
```

배치 정책:

```yaml
labels:
  workload: batch

taints:
  - key: dedicated
    value: batch
    effect: NoSchedule
```

주의할 점은 GPU 작업을 무조건 Spot에 올리지 않는 것이다. 화자 분리 작업은 처리 시간이 길 수 있으므로, Dev에서는 On-Demand GPU로 안정성을 먼저 검증하고 이후 GPU Spot을 별도 NodePool로 확장하는 것이 좋다.

---

## 10. Karpenter NodePool 예시

### 10.1 기본 EC2NodeClass

```yaml
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: default
spec:
  amiFamily: AL2023
  role: utterai-dev-karpenter-node-role
  subnetSelectorTerms:
    - tags:
        karpenter.sh/discovery: utterai-dev
  securityGroupSelectorTerms:
    - tags:
        karpenter.sh/discovery: utterai-dev
  tags:
    Project: utterai
    Environment: dev
```

### 10.2 GPU EC2NodeClass

GPU Node는 일반 Node와 AMI/드라이버 구성이 달라질 수 있으므로 별도 EC2NodeClass로 분리하는 것이 좋다.

```yaml
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: gpu
spec:
  amiFamily: AL2023
  role: utterai-dev-karpenter-node-role
  subnetSelectorTerms:
    - tags:
        karpenter.sh/discovery: utterai-dev
  securityGroupSelectorTerms:
    - tags:
        karpenter.sh/discovery: utterai-dev
  tags:
    Project: utterai
    Environment: dev
    Workload: ai-gpu
```

실제 운영에서는 사용하는 EKS/Karpenter 버전에 맞춰 GPU 지원 AMI, userData, NVIDIA driver 구성을 확인해야 한다.

### 10.3 api NodePool 예시

```yaml
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: api
spec:
  template:
    metadata:
      labels:
        workload: api
    spec:
      taints:
        - key: dedicated
          value: api
          effect: NoSchedule
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: default
      requirements:
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["on-demand"]
        - key: kubernetes.io/arch
          operator: In
          values: ["amd64"]
        - key: karpenter.k8s.aws/instance-family
          operator: In
          values: ["m6i", "m7i", "c6i", "c7i"]
  limits:
    cpu: "100"
  disruption:
    consolidationPolicy: WhenEmptyOrUnderutilized
    consolidateAfter: 1m
```

### 10.4 ai-cpu NodePool 예시

```yaml
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: ai-cpu
spec:
  template:
    metadata:
      labels:
        workload: ai-cpu
    spec:
      taints:
        - key: dedicated
          value: ai-cpu
          effect: NoSchedule
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: default
      requirements:
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["on-demand"]
        - key: kubernetes.io/arch
          operator: In
          values: ["amd64"]
        - key: karpenter.k8s.aws/instance-family
          operator: In
          values: ["m6i", "m7i", "c6i", "c7i", "r6i", "r7i"]
  limits:
    cpu: "200"
  disruption:
    consolidationPolicy: WhenEmptyOrUnderutilized
    consolidateAfter: 5m
```

### 10.5 ai-gpu NodePool 예시

```yaml
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: ai-gpu
spec:
  template:
    metadata:
      labels:
        workload: ai-gpu
    spec:
      taints:
        - key: dedicated
          value: ai-gpu
          effect: NoSchedule
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: gpu
      requirements:
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["on-demand"]
        - key: kubernetes.io/arch
          operator: In
          values: ["amd64"]
        - key: karpenter.k8s.aws/instance-family
          operator: In
          values: ["g5", "g6"]
        - key: karpenter.k8s.aws/instance-gpu-count
          operator: In
          values: ["1"]
  limits:
    cpu: "100"
  disruption:
    consolidationPolicy: WhenEmptyOrUnderutilized
    consolidateAfter: 10m
```

Dev 단계에서는 GPU 비용을 막기 위해 다음 중 하나를 선택한다.

```text
Dev GPU 운영 선택지:
1. ai-gpu NodePool은 manifest만 준비하고 필요할 때만 적용
2. min 0으로 두고 KEDA 요청이 있을 때만 GPU Node 생성
3. 데모 전에는 GPU Node 1개를 미리 warm-up
4. 비용 확인을 위해 짧은 시간만 테스트 후 즉시 scale down
```

### 10.6 spot-batch NodePool 예시

```yaml
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: spot-batch
spec:
  template:
    metadata:
      labels:
        workload: batch
    spec:
      taints:
        - key: dedicated
          value: batch
          effect: NoSchedule
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: default
      requirements:
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["spot"]
        - key: kubernetes.io/arch
          operator: In
          values: ["amd64"]
        - key: karpenter.k8s.aws/instance-family
          operator: In
          values: ["m6i", "m7i", "c6i", "c7i"]
  limits:
    cpu: "300"
  disruption:
    consolidationPolicy: WhenEmptyOrUnderutilized
    consolidateAfter: 1m
```

---

## 11. taint / toleration / nodeSelector 전략

### 11.1 기본 개념

```text
nodeSelector:
- Pod가 어떤 label을 가진 Node에 올라갈지 지정한다.

taint:
- 특정 Node에 아무 Pod나 올라오지 못하게 막는다.

toleration:
- 해당 taint를 허용하는 Pod만 그 Node에 올라갈 수 있게 한다.
```

### 11.2 워크로드별 배치 표

| Workload | nodeSelector | toleration | 배치 NodePool |
| --- | --- | --- | --- |
| API Pod | `workload=api` | `dedicated=api` | `api` |
| AI CPU Pod | `workload=ai-cpu` | `dedicated=ai-cpu` | `ai-cpu` |
| AI GPU Pod | `workload=ai-gpu` | `dedicated=ai-gpu` | `ai-gpu` |
| Batch Consumer | `workload=batch` | `dedicated=batch` | `spot-batch` |
| 일반 Pod | `workload=general` | 없음 또는 general | `general` |

### 11.3 API Pod 예시

```yaml
spec:
  template:
    spec:
      nodeSelector:
        workload: api
      tolerations:
        - key: dedicated
          operator: Equal
          value: api
          effect: NoSchedule
```

### 11.4 AI CPU Worker 예시

```yaml
spec:
  template:
    spec:
      nodeSelector:
        workload: ai-cpu
      tolerations:
        - key: dedicated
          operator: Equal
          value: ai-cpu
          effect: NoSchedule
```

### 11.5 AI GPU Worker 예시

```yaml
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
      containers:
        - name: diarization-worker
          image: <account-id>.dkr.ecr.ap-northeast-2.amazonaws.com/utter-ai-gpu:latest
          resources:
            requests:
              cpu: "2"
              memory: "8Gi"
              nvidia.com/gpu: "1"
            limits:
              cpu: "4"
              memory: "16Gi"
              nvidia.com/gpu: "1"
```

### 11.6 Batch Consumer 예시

```yaml
spec:
  template:
    spec:
      nodeSelector:
        workload: batch
      tolerations:
        - key: dedicated
          operator: Equal
          value: batch
          effect: NoSchedule
```

---

## 12. KEDA 설계 방향

### 12.1 KEDA 역할

KEDA는 SQS 메시지 수를 기준으로 Worker Pod 수를 늘리거나 줄인다.

```text
SQS 메시지 증가
  ↓
KEDA가 queue length 확인
  ↓
Worker Deployment replica 증가
  ↓
Pod Pending 발생 가능
  ↓
Karpenter가 Node 생성
  ↓
Worker Pod가 메시지 처리
  ↓
Queue 감소
  ↓
KEDA scale-in
```

### 12.2 Queue 분리 전략

AI 작업에 CPU/GPU가 섞이므로 Queue도 분리하는 것이 좋다.

```text
SQS Queues:
- analysis-request-queue
- cpu-analysis-queue
- gpu-diarization-queue
- report-generation-queue
- analysis-dlq
```

| Queue | 처리 Worker | NodePool |
| --- | --- | --- |
| `analysis-request-queue` | API 또는 Dispatcher | `api` 또는 `ai-cpu` |
| `cpu-analysis-queue` | CPU AI Worker | `ai-cpu` |
| `gpu-diarization-queue` | GPU Diarization Worker | `ai-gpu` |
| `report-generation-queue` | Report Worker | `spot-batch` 또는 `ai-cpu` |
| `analysis-dlq` | 실패 메시지 보관 | 직접 처리 없음 |

### 12.3 CPU Worker ScaledObject 예시

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: cpu-analysis-worker-scaler
  namespace: utter-ai-cpu
spec:
  scaleTargetRef:
    name: cpu-analysis-worker
  minReplicaCount: 0
  maxReplicaCount: 10
  pollingInterval: 30
  cooldownPeriod: 300
  triggers:
    - type: aws-sqs-queue
      metadata:
        queueURL: https://sqs.ap-northeast-2.amazonaws.com/123456789012/cpu-analysis-queue
        queueLength: "5"
        awsRegion: ap-northeast-2
```

### 12.4 GPU Worker ScaledObject 예시

GPU Worker는 비용이 높으므로 maxReplicaCount를 작게 시작한다.

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: gpu-diarization-worker-scaler
  namespace: utter-ai-gpu
spec:
  scaleTargetRef:
    name: gpu-diarization-worker
  minReplicaCount: 0
  maxReplicaCount: 2
  pollingInterval: 30
  cooldownPeriod: 600
  triggers:
    - type: aws-sqs-queue
      metadata:
        queueURL: https://sqs.ap-northeast-2.amazonaws.com/123456789012/gpu-diarization-queue
        queueLength: "1"
        awsRegion: ap-northeast-2
```

GPU 작업은 메시지 1개당 처리 시간이 길 수 있으므로 `queueLength`를 작게 잡는다.

```text
GPU KEDA 기준:
- queueLength: 1부터 시작
- maxReplicaCount: Dev 1~2
- cooldownPeriod: CPU보다 길게
- visibility timeout: 실제 처리 시간보다 길게
- DLQ: 반드시 연결
```

### 12.5 Batch Worker ScaledObject 예시

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: batch-worker-scaler
  namespace: utter-batch
spec:
  scaleTargetRef:
    name: batch-worker
  minReplicaCount: 0
  maxReplicaCount: 20
  pollingInterval: 30
  cooldownPeriod: 300
  triggers:
    - type: aws-sqs-queue
      metadata:
        queueURL: https://sqs.ap-northeast-2.amazonaws.com/123456789012/report-generation-queue
        queueLength: "10"
        awsRegion: ap-northeast-2
```

---

## 13. IRSA / ServiceAccount / RBAC 구조

### 13.1 IRSA와 RBAC 차이

```text
RBAC:
- Kubernetes 내부에서 어떤 리소스를 볼 수 있고 수정할 수 있는가?
- 예: Pod 조회, ConfigMap 조회, Lease 갱신

IRSA:
- Pod가 어떤 AWS 서비스에 접근할 수 있는가?
- 예: S3 읽기, SQS 메시지 수신, Bedrock 호출, CloudWatch 로그 전송
```

### 13.2 ServiceAccount 권한 분리

| ServiceAccount | Namespace | 필요한 AWS 권한 |
| --- | --- | --- |
| `aws-load-balancer-controller-sa` | `ingress-system` | ALB, TargetGroup, Listener, SecurityGroup 일부 관리 |
| `karpenter-sa` | `karpenter` | EC2 생성/삭제, InstanceProfile, SQS interruption queue |
| `keda-operator-sa` | `keda` | SQS queue attribute 조회 |
| `utter-api-sa` | `utter-api` | S3 presigned URL, SQS SendMessage, SecretsManager read |
| `ai-cpu-worker-sa` | `utter-ai-cpu` | SQS Receive/Delete, S3 read/write, Bedrock invoke 선택 |
| `ai-gpu-worker-sa` | `utter-ai-gpu` | SQS Receive/Delete, S3 raw audio read, S3 processed audio write, model artifact read |
| `batch-worker-sa` | `utter-batch` | SQS Receive/Delete, S3 reports write, Aurora 접근 secret read |

### 13.3 API ServiceAccount 예시

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: utter-api-sa
  namespace: utter-api
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::123456789012:role/utterai-dev-api-irsa-role
```

### 13.4 GPU Worker ServiceAccount 예시

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ai-gpu-worker-sa
  namespace: utter-ai-gpu
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::123456789012:role/utterai-dev-ai-gpu-irsa-role
```

### 13.5 GPU Worker IAM Policy 기준

GPU Worker는 필요한 권한만 가져야 한다.

```text
ai-gpu-worker-sa 권한 예시:
- SQS gpu-diarization-queue ReceiveMessage
- SQS gpu-diarization-queue DeleteMessage
- SQS gpu-diarization-queue ChangeMessageVisibility
- S3 Raw Audio bucket GetObject
- S3 Processed Audio bucket PutObject
- S3 Model Artifact bucket GetObject
- CloudWatch Logs PutLogEvents
- SecretsManager GetSecretValue 일부
```

API 서버 권한과 GPU Worker 권한을 섞으면 안 된다.

```text
권한 분리 원칙:
- API Pod는 GPU Queue에 메시지를 넣을 수만 있다.
- GPU Worker는 메시지를 읽고 처리할 수 있다.
- GPU Worker는 Cognito나 ALB를 관리할 권한이 없다.
- API Pod는 GPU 모델 artifact 전체를 읽을 필요가 없다.
```

---

## 14. Resource request / limit 기준

### 14.1 request와 limit의 의미

```text
request:
- Kubernetes Scheduler가 Pod를 어느 Node에 배치할지 판단하는 기준
- Karpenter가 Node 크기를 결정할 때도 중요한 기준

limit:
- 컨테이너가 사용할 수 있는 최대 자원 경계
- CPU는 제한 가능
- Memory는 초과 시 OOMKilled 발생 가능
- GPU는 보통 정수 단위로 명시
```

### 14.2 초기 Resource 기준

Dev 초기값은 다음처럼 잡는다.

| Workload | Request | Limit | Replica 기준 |
| --- | --- | --- | --- |
| API Pod | `250m CPU / 512Mi` | `1 CPU / 1Gi` | min 2, max 10 |
| CPU AI Worker | `1 CPU / 2Gi` | `2 CPU / 4Gi` | KEDA min 0, max 10 |
| GPU Diarization Worker | `2 CPU / 8Gi / 1 GPU` | `4 CPU / 16Gi / 1 GPU` | KEDA min 0, max 2 |
| Batch Worker | `500m CPU / 1Gi` | `2 CPU / 2Gi` | KEDA min 0, max 20 |
| Karpenter | Helm 기본값 + 명시 request | 기본값 기준 | system node |
| KEDA | Helm 기본값 + 명시 request | 기본값 기준 | system node |
| LBC | Helm 기본값 + 명시 request | 기본값 기준 | system node |

### 14.3 API Deployment resource 예시

```yaml
resources:
  requests:
    cpu: "250m"
    memory: "512Mi"
  limits:
    cpu: "1"
    memory: "1Gi"
```

### 14.4 CPU AI Worker resource 예시

```yaml
resources:
  requests:
    cpu: "1"
    memory: "2Gi"
  limits:
    cpu: "2"
    memory: "4Gi"
```

### 14.5 GPU Worker resource 예시

```yaml
resources:
  requests:
    cpu: "2"
    memory: "8Gi"
    nvidia.com/gpu: "1"
  limits:
    cpu: "4"
    memory: "16Gi"
    nvidia.com/gpu: "1"
```

### 14.6 Resource 기준을 잡는 이유

Resource request/limit을 명확히 잡아야 하는 이유는 다음과 같다.

```text
- Scheduler가 Pod를 배치할 수 있다.
- Karpenter가 필요한 Node 크기를 계산할 수 있다.
- GPU Worker가 GPU 없는 Node에 배치되는 것을 막을 수 있다.
- API Pod와 AI Pod의 자원 경합을 줄일 수 있다.
- 비용 예측이 가능해진다.
- 부하 테스트 후 튜닝 기준이 생긴다.
```

---

## 15. AI 처리 흐름 설계

### 15.1 전체 AI 처리 흐름

```text
1. 사용자가 음성 파일 업로드 요청
2. API가 S3 Presigned URL 발급
3. 사용자가 S3 Raw Audio에 직접 업로드
4. API가 분석 요청 생성
5. API가 analysis-request-queue에 메시지 발행
6. Dispatcher 또는 CPU Worker가 작업 유형 분리
7. CPU 전처리 작업은 cpu-analysis-queue로 이동
8. 화자 분리 작업은 gpu-diarization-queue로 이동
9. KEDA가 CPU/GPU Worker를 각각 확장
10. Karpenter가 ai-cpu 또는 ai-gpu Node를 생성
11. Worker가 S3에서 원본 오디오를 읽어 처리
12. 결과를 S3 Processed Audio, Aurora, S3 Reports에 저장
13. API가 사용자에게 분석 결과 조회 제공
```

### 15.2 CPU/GPU 분리 흐름

```text
S3 Raw Audio
  ↓
CPU Preprocessing Worker
  ├─ VAD / segment split
  ├─ audio validation
  └─ metadata extraction
        ↓
GPU Diarization Queue
        ↓
GPU Diarization Worker
        ├─ speaker diarization
        ├─ speaker timeline 생성
        └─ 결과 저장
```

### 15.3 GPU Worker가 처리할 데이터

| 입력 | 처리 | 출력 |
| --- | --- | --- |
| S3 Raw Audio 경로 | 오디오 다운로드 | 로컬 임시 파일 |
| 전처리 segment 정보 | 화자 분리 모델 추론 | speaker timeline |
| job metadata | 처리 상태 갱신 | Aurora job status |
| 모델 artifact | GPU 추론 | diarization result JSON |

### 15.4 GPU 작업 실패 처리

GPU 작업은 비용이 높고 처리 시간이 길 수 있으므로 실패 처리 기준이 필요하다.

```text
실패 처리 기준:
- SQS visibility timeout을 예상 처리 시간보다 길게 설정
- 처리 중 heartbeat 또는 ChangeMessageVisibility 고려
- 실패 횟수 초과 시 DLQ 이동
- Worker 재시작 시 중복 처리 가능성을 고려해 idempotent하게 설계
- S3 output key는 job_id 기반으로 고정
- Aurora job status는 PROCESSING / COMPLETED / FAILED로 관리
```

### 15.5 GPU cold start 고려

GPU Node는 scale from zero 시 다음 시간이 추가된다.

```text
GPU cold start 구성 요소:
- Karpenter가 EC2 GPU 인스턴스 생성하는 시간
- Node Ready 대기 시간
- NVIDIA driver / device plugin 준비 시간
- 대용량 Docker image pull 시간
- 모델 파일 다운로드 또는 로딩 시간
- Worker 프로세스 초기화 시간
```

따라서 발표에서는 이렇게 설명하면 좋다.

```text
Dev에서는 비용 절감을 위해 GPU NodePool을 0까지 줄일 수 있도록 설계하되,
실제 데모나 운영에서는 GPU cold start가 사용자 경험에 영향을 줄 수 있으므로
최소 1개의 warm GPU Worker 또는 예약된 warm-up 전략을 검토합니다.
```

---

## 16. 배포 Manifest 예시

### 16.1 API Deployment 예시

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: utter-api
  namespace: utter-api
spec:
  replicas: 2
  selector:
    matchLabels:
      app: utter-api
  template:
    metadata:
      labels:
        app: utter-api
    spec:
      serviceAccountName: utter-api-sa
      nodeSelector:
        workload: api
      tolerations:
        - key: dedicated
          operator: Equal
          value: api
          effect: NoSchedule
      containers:
        - name: api
          image: <account-id>.dkr.ecr.ap-northeast-2.amazonaws.com/utter-api:latest
          ports:
            - containerPort: 8080
          readinessProbe:
            httpGet:
              path: /health/ready
              port: 8080
          livenessProbe:
            httpGet:
              path: /health/live
              port: 8080
          resources:
            requests:
              cpu: "250m"
              memory: "512Mi"
            limits:
              cpu: "1"
              memory: "1Gi"
```

### 16.2 CPU Worker Deployment 예시

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cpu-analysis-worker
  namespace: utter-ai-cpu
spec:
  replicas: 0
  selector:
    matchLabels:
      app: cpu-analysis-worker
  template:
    metadata:
      labels:
        app: cpu-analysis-worker
    spec:
      serviceAccountName: ai-cpu-worker-sa
      nodeSelector:
        workload: ai-cpu
      tolerations:
        - key: dedicated
          operator: Equal
          value: ai-cpu
          effect: NoSchedule
      containers:
        - name: worker
          image: <account-id>.dkr.ecr.ap-northeast-2.amazonaws.com/utter-ai-cpu:latest
          resources:
            requests:
              cpu: "1"
              memory: "2Gi"
            limits:
              cpu: "2"
              memory: "4Gi"
```

### 16.3 GPU Worker Deployment 예시

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: gpu-diarization-worker
  namespace: utter-ai-gpu
spec:
  replicas: 0
  selector:
    matchLabels:
      app: gpu-diarization-worker
  template:
    metadata:
      labels:
        app: gpu-diarization-worker
    spec:
      serviceAccountName: ai-gpu-worker-sa
      nodeSelector:
        workload: ai-gpu
      tolerations:
        - key: dedicated
          operator: Equal
          value: ai-gpu
          effect: NoSchedule
      containers:
        - name: diarization-worker
          image: <account-id>.dkr.ecr.ap-northeast-2.amazonaws.com/utter-ai-gpu:latest
          env:
            - name: QUEUE_NAME
              value: gpu-diarization-queue
            - name: AWS_REGION
              value: ap-northeast-2
          resources:
            requests:
              cpu: "2"
              memory: "8Gi"
              nvidia.com/gpu: "1"
            limits:
              cpu: "4"
              memory: "16Gi"
              nvidia.com/gpu: "1"
```

---

## 17. 팀원 파트와 연결되는 지점

### 17.1 백엔드 담당과 연결

백엔드 팀에는 다음 계약을 전달한다.

```yaml
namespace: utter-api
serviceAccount: utter-api-sa
containerPort: 8080
healthCheckPath: /health/ready
apiBasePath: /api
nodeSelector:
  workload: api
tolerations:
  dedicated: api
```

백엔드 API가 해야 할 일은 다음이다.

```text
- 사용자의 분석 요청 수신
- S3 Presigned URL 발급
- 분석 job metadata 저장
- SQS analysis-request-queue 메시지 발행
- 분석 상태 조회 API 제공
```

### 17.2 AI CPU 담당과 연결

AI CPU 담당에게는 다음 계약을 전달한다.

```yaml
namespace: utter-ai-cpu
serviceAccount: ai-cpu-worker-sa
queue: cpu-analysis-queue
nodeSelector:
  workload: ai-cpu
resources:
  request: 1 CPU / 2Gi
  limit: 2 CPU / 4Gi
```

AI CPU Worker가 해야 할 일은 다음이다.

```text
- SQS 메시지 수신
- S3 Raw Audio 다운로드
- VAD / segment split / 전처리
- 필요한 경우 GPU Diarization Queue에 메시지 발행
- 중간 결과 저장
```

### 17.3 AI GPU 담당과 연결

AI GPU 담당에게는 다음 계약을 전달한다.

```yaml
namespace: utter-ai-gpu
serviceAccount: ai-gpu-worker-sa
queue: gpu-diarization-queue
nodeSelector:
  workload: ai-gpu
tolerations:
  dedicated: ai-gpu
resources:
  request: 2 CPU / 8Gi / 1 GPU
  limit: 4 CPU / 16Gi / 1 GPU
```

AI GPU Worker가 해야 할 일은 다음이다.

```text
- SQS gpu-diarization-queue 메시지 수신
- S3 Raw Audio 또는 전처리 segment 다운로드
- 화자 분리 모델 실행
- speaker timeline JSON 생성
- S3 Processed Audio 또는 S3 Artifacts에 결과 저장
- Aurora job metadata 상태 갱신
```

### 17.4 DevOps 담당과 연결

DevOps 담당과는 다음 항목을 맞춘다.

```text
- ECR repository 이름
- GitHub Actions image push 경로
- Argo CD Application 경로
- dev/prod overlay 구조
- kubeconfig 접근 방식
- Terraform output 전달 방식
```

### 17.5 DB 담당과 연결

DB 담당과는 다음 항목을 맞춘다.

```text
- Aurora endpoint
- DB secret 이름
- API와 Worker가 접근할 DB user 분리 여부
- job status table 구조
- result metadata table 구조
```

---

## 18. 구현 순서

### 18.1 1단계: EKS 기본 클러스터

```text
목표:
- VPC output 연결
- EKS Cluster 생성
- system Managed Node Group 생성
- kubeconfig 연결
```

검증 명령어:

```bash
kubectl get nodes
kubectl get pods -n kube-system
```

### 18.2 2단계: Namespace / ServiceAccount 기본 구조

```text
목표:
- namespace 생성
- ServiceAccount 생성
- 기본 RBAC 생성
```

검증 명령어:

```bash
kubectl get ns
kubectl get sa -A
```

### 18.3 3단계: AWS Load Balancer Controller

```text
목표:
- LBC IRSA 생성
- Helm으로 LBC 설치
- 샘플 API Service/Ingress 생성
- ALB 생성 확인
```

검증 명령어:

```bash
kubectl get ingress -n utter-api
kubectl describe ingress utter-api-ingress -n utter-api
```

### 18.4 4단계: Karpenter 설치

```text
목표:
- Karpenter IRSA 생성
- Karpenter Helm 설치
- EC2NodeClass 생성
- NodePool 생성
```

검증 명령어:

```bash
kubectl get nodepool
kubectl get ec2nodeclass
kubectl get nodes -L workload,karpenter.sh/capacity-type
```

### 18.5 5단계: NVIDIA Device Plugin 설치

```text
목표:
- GPU Node에서 nvidia.com/gpu 리소스가 보이도록 설정
- GPU Worker가 GPU를 request할 수 있게 구성
```

검증 명령어:

```bash
kubectl get daemonset -n kube-system
kubectl describe node <gpu-node-name> | grep -i nvidia
kubectl get nodes -o custom-columns=NAME:.metadata.name,GPU:.status.allocatable.nvidia\.com/gpu
```

### 18.6 6단계: NodePool 배치 테스트

```text
목표:
- API Pod는 api NodePool에 배치
- CPU AI Pod는 ai-cpu NodePool에 배치
- GPU AI Pod는 ai-gpu NodePool에 배치
- Batch Pod는 spot-batch NodePool에 배치
```

검증 명령어:

```bash
kubectl get pod -o wide -n utter-api
kubectl get pod -o wide -n utter-ai-cpu
kubectl get pod -o wide -n utter-ai-gpu
kubectl get pod -o wide -n utter-batch
```

### 18.7 7단계: KEDA + SQS 검증

```text
목표:
- CPU queue 메시지 증가 시 CPU Worker 증가
- GPU queue 메시지 증가 시 GPU Worker 증가
- Worker Pod pending 시 Karpenter Node 생성
- 메시지 처리 후 scale-in 확인
```

검증 명령어:

```bash
kubectl get scaledobject -A
kubectl get hpa -A
kubectl get pods -n utter-ai-cpu -w
kubectl get pods -n utter-ai-gpu -w
kubectl get nodes -L workload
```

---

## 19. Dev 구현 범위

### 19.1 Dev에서 반드시 구현할 것

```text
- EKS Cluster
- system Managed Node Group
- AWS Load Balancer Controller
- ALB Ingress
- Karpenter
- KEDA
- NVIDIA Device Plugin
- namespace 구조
- ServiceAccount / IRSA 구조
- general NodePool
- api NodePool
- ai-cpu NodePool
- ai-gpu NodePool
- spot-batch NodePool
- 샘플 API Deployment
- 샘플 CPU Worker Deployment
- 샘플 GPU Worker Deployment
- 샘플 KEDA ScaledObject
```

### 19.2 Dev에서 선택 구현할 것

```text
- 실제 GPU 모델 실행
- GPU Node warm-up 전략
- Prometheus/Grafana GPU dashboard
- Argo CD 배포 자동화
- PodDisruptionBudget
- NetworkPolicy
```

### 19.3 Dev에서 제외해도 되는 것

```text
- Multi-Region DR 완전 구현
- GPU Spot 운영 자동화
- 복잡한 canary 배포
- 모든 AI 모델 운영 최적화
- 완전한 운영급 보안 정책
```

---

## 20. 발표에서 말할 설계 근거

### 20.1 전체 역할 설명

```text
제가 맡은 부분은 EKS 클러스터 자체를 만드는 것에 그치지 않고,
API, AI CPU, AI GPU, Batch 워크로드가 각각 적절한 노드에 배치되고
SQS 부하에 따라 자동 확장될 수 있는 Kubernetes 플랫폼 레이어를 구성하는 것입니다.
```

### 20.2 NodePool 분리 설명

```text
API 서버는 사용자 요청을 직접 처리하므로 On-Demand 기반 api NodePool에 배치했습니다.
VAD나 전처리처럼 CPU로 처리 가능한 AI 작업은 ai-cpu NodePool에 배치했고,
화자 분리처럼 GPU 가속이 필요한 작업은 ai-gpu NodePool로 분리했습니다.
재시도 가능한 일반 Batch 작업은 spot-batch NodePool에 배치해 비용 효율성을 높이는 방향으로 설계했습니다.
```

### 20.3 KEDA + Karpenter 설명

```text
KEDA는 SQS Queue의 메시지 수를 기준으로 Worker Pod 수를 조절합니다.
이때 새로 늘어난 Pod가 기존 Node에 배치되지 못하면 Karpenter가 Pod의 nodeSelector,
toleration, resource request를 보고 적절한 NodePool에서 EC2 Node를 자동 생성합니다.
```

### 20.4 GPU 설계 설명

```text
화자 분리 작업은 음성 길이에 따라 처리 시간이 길고 GPU 가속 효과가 크기 때문에
일반 CPU Node와 분리된 ai-gpu NodePool에서 실행하도록 설계했습니다.
GPU Worker는 nvidia.com/gpu 리소스를 명시적으로 요청하고,
ai-gpu NodePool에는 taint를 적용해 GPU가 필요 없는 Pod가 올라가지 못하도록 했습니다.
이를 통해 GPU 비용 낭비를 줄이고 API 서버와 AI 추론 작업의 자원 경합을 방지할 수 있습니다.
```

### 20.5 IRSA 설명

```text
AWS 접근 권한은 Pod 단위로 분리했습니다.
API Pod는 S3 Presigned URL 발급과 SQS 메시지 발행 권한만 갖고,
GPU Worker는 SQS 메시지 수신, S3 오디오 읽기, 처리 결과 쓰기 권한만 갖도록 분리했습니다.
Kubernetes 내부 권한은 RBAC로, AWS 서비스 접근 권한은 IRSA로 분리했습니다.
```

---

## 21. 팀 회의 때 가져갈 질문

### 21.1 백엔드 관련 질문

- API 서버 포트는 `8080`으로 확정할 것인가?
- health check path는 `/health/ready`, `/health/live`로 갈 것인가?
- 분석 요청 생성 시 어떤 SQS Queue에 메시지를 넣을 것인가?
- API가 Aurora에 직접 저장하는 metadata 범위는 어디까지인가?
- API가 Redis를 사용하는 경우 어떤 key 구조를 사용할 것인가?

### 21.2 AI CPU 관련 질문

- VAD와 전처리 작업은 CPU에서 처리해도 충분한가?
- CPU Worker가 GPU Queue로 넘겨야 하는 payload 구조는 무엇인가?
- 전처리 결과를 S3에 저장할 것인가, 메시지에 직접 넣을 것인가?
- CPU Worker의 평균 처리 시간은 어느 정도인가?

### 21.3 AI GPU 관련 질문

- 화자 분리 모델은 어떤 모델을 사용할 것인가?
- GPU 1개당 동시에 몇 개의 작업을 처리할 것인가?
- 평균 음성 길이와 예상 처리 시간은 어느 정도인가?
- GPU Worker Docker image 크기는 어느 정도인가?
- 모델 파일을 image에 포함할 것인가, S3에서 런타임에 받을 것인가?
- GPU Node를 데모 전에 미리 띄워둘 것인가?
- GPU 작업 실패 시 재시도 횟수와 DLQ 기준은 어떻게 할 것인가?

### 21.4 DevOps 관련 질문

- ECR repository 이름은 어떻게 나눌 것인가?
- API, CPU Worker, GPU Worker 이미지는 각각 분리할 것인가?
- Argo CD를 쓸 것인가, GitHub Actions에서 kubectl apply할 것인가?
- dev/prod overlay는 Kustomize로 나눌 것인가?

### 21.5 비용 관련 질문

- Dev에서 GPU Node를 항상 켜둘 것인가, 필요할 때만 켤 것인가?
- GPU 인스턴스 테스트 시간 제한을 둘 것인가?
- Karpenter consolidation 시간을 얼마로 둘 것인가?
- Spot은 batch에만 쓸 것인가, GPU Spot도 나중에 검토할 것인가?

---

## 22. 체크리스트

### 22.1 EKS 기본 체크리스트

- [ ] VPC output 연결
- [ ] EKS Cluster 생성
- [ ] system Managed Node Group 생성
- [ ] kubeconfig 연결
- [ ] CoreDNS 정상 동작
- [ ] VPC CNI 정상 동작

### 22.2 Add-on 체크리스트

- [ ] AWS Load Balancer Controller 설치
- [ ] Karpenter 설치
- [ ] KEDA 설치
- [ ] metrics-server 설치
- [ ] NVIDIA Device Plugin 설치

### 22.3 NodePool 체크리스트

- [ ] general NodePool 생성
- [ ] api NodePool 생성
- [ ] ai-cpu NodePool 생성
- [ ] ai-gpu NodePool 생성
- [ ] spot-batch NodePool 생성
- [ ] GPU EC2NodeClass 생성
- [ ] NodePool label 확인
- [ ] NodePool taint 확인

### 22.4 Workload 배치 체크리스트

- [ ] API Pod가 api NodePool에 배치
- [ ] CPU AI Pod가 ai-cpu NodePool에 배치
- [ ] GPU AI Pod가 ai-gpu NodePool에 배치
- [ ] Batch Pod가 spot-batch NodePool에 배치
- [ ] GPU Pod에서 `nvidia.com/gpu` request 확인
- [ ] 일반 Pod가 GPU Node에 잘못 배치되지 않는지 확인

### 22.5 KEDA 체크리스트

- [ ] CPU Queue ScaledObject 생성
- [ ] GPU Queue ScaledObject 생성
- [ ] Batch Queue ScaledObject 생성
- [ ] Queue 메시지 증가 시 Pod scale-out 확인
- [ ] Pod Pending 발생 시 Karpenter Node 생성 확인
- [ ] Queue 메시지 처리 후 scale-in 확인

### 22.6 IRSA 체크리스트

- [ ] API IRSA 생성
- [ ] AI CPU Worker IRSA 생성
- [ ] AI GPU Worker IRSA 생성
- [ ] Batch Worker IRSA 생성
- [ ] LBC IRSA 생성
- [ ] Karpenter IRSA 생성
- [ ] KEDA IRSA 생성

---

## 23. 최종 정리

이 파트의 핵심은 단순히 EKS 클러스터를 만드는 것이 아니다.

**API, AI CPU, AI GPU, Batch 워크로드가 각각 다른 성격을 가지고 있기 때문에, 이들을 Kubernetes 위에서 안전하게 분리하고 자동 확장되도록 만드는 플랫폼 구조를 설계하는 것**이 핵심이다.

최종 구조는 다음과 같다.

```text
API 요청:
CloudFront → ALB Ingress → API Pod → api NodePool

CPU AI 작업:
SQS CPU Queue → KEDA → CPU Worker Pod → ai-cpu NodePool

GPU AI 작업:
SQS GPU Queue → KEDA → GPU Worker Pod → ai-gpu NodePool

일반 Batch 작업:
SQS Batch Queue → KEDA → Batch Worker Pod → spot-batch NodePool

Node 자동 생성:
Pending Pod → Karpenter → NodePool 조건 확인 → EC2 Node 생성

권한 분리:
ServiceAccount → IRSA → 최소 AWS 권한 부여
```

따라서 Dev 단계에서는 다음까지 완성하면 충분히 의미 있는 플랫폼 아키텍처 구현으로 볼 수 있다.

```text
EKS
+ AWS Load Balancer Controller
+ Karpenter
+ KEDA
+ NVIDIA Device Plugin
+ IRSA
+ general/api/ai-cpu/ai-gpu/spot-batch NodePool
+ 샘플 API/CPU Worker/GPU Worker 배포
+ SQS 기반 scale-out 검증
```

## 24. 멀티 AZ와 EKS 동작 보충 설명

### 24.1 멀티 AZ가 의미하는 것

멀티 AZ는 **AZ마다 역할이 다르다**는 뜻이 아니라, **같은 역할의 워커 노드와 서비스가 여러 AZ에 분산될 수 있다**는 뜻이다.

이 구조에서는 다음 두 개념을 분리해서 이해해야 한다.

- 역할 분리: `api`, `ai-cpu`, `ai-gpu`, `spot-batch`, `general` 같은 **NodePool**이 담당한다.
- 가용성 분산: 실제 **EC2 Worker Node**가 `ap-northeast-2a`, `ap-northeast-2b`, `ap-northeast-2c` 중 어디에 생성될지를 담당한다.

즉 `AZ`는 기능 단위가 아니라 **장애 분산과 가용성 확보를 위한 배치 단위**다.

### 24.2 Control Plane / Managed Node Group / NodePool 차이

세 개념은 역할이 다르다.

- `EKS Control Plane`: AWS가 관리하는 클러스터 제어 영역이다. 우리가 AZ별로 직접 배치하지 않는다.
- `Managed Node Group`: 클러스터 운영용 기본 워커 노드 그룹이다. 논리적으로 1개일 수 있지만, 실제 노드는 여러 AZ에 분산될 수 있다.
- `Karpenter NodePool`: 어떤 종류의 노드를 만들 수 있는지 정의하는 정책이다. NodePool 정의는 1개라도 실제 노드는 여러 AZ에 걸쳐 생성될 수 있다.

따라서 `AZ마다 NodeGroup이 1개씩`, `AZ마다 NodePool이 1개씩` 생긴다고 이해하는 것은 부정확하다. 더 정확한 표현은 다음과 같다.

```text
NodeGroup / NodePool 정의는 논리적으로 1개
실제 EC2 Worker Node는 여러 AZ 서브넷에 분산 생성 가능
```

### 24.3 처음부터 3개 AZ에 모두 노드가 뜨는가

처음부터 모든 워크로드용 노드가 3개 AZ에 균등하게 미리 떠 있는 것은 아니다.

구분해서 보면:

- `system Managed Node Group`은 클러스터 운영을 위해 처음부터 일정 수의 기본 노드가 떠 있을 수 있다.
- `Karpenter NodePool` 기반 노드는 워크로드가 실제로 필요할 때 생성된다.

즉 다음 흐름에 가깝다.

```text
1. system node는 초기부터 일부 기동
2. API / CPU / GPU / Batch Pod 생성
3. 기존 node에 수용 불가 시 Pod가 Pending
4. Karpenter가 적절한 NodePool과 AZ를 선택
5. 새 EC2 node 생성
```

### 24.4 Pod 수와 Node 수를 누가 정하는가

`Pod 수`와 `Node 수`는 서로 다른 리소스가 결정한다.

- `Deployment`, `Job`, `HPA`, `KEDA ScaledObject`: **Pod 개수**를 결정한다.
- `Managed Node Group 설정`, `Karpenter NodePool 정책`: **Node 생성 범위와 조건**을 결정한다.

즉:

```text
manifest -> Pod를 몇 개 원하는가
NodePool -> 어떤 종류의 Node에 올릴 수 있는가
Karpenter -> 그 Pod들을 담기 위해 Node를 몇 대 만들 것인가
```

따라서 `NodePool이 Pod 개수를 직접 정한다`기보다, **Pod가 늘어난 뒤 그 Pod를 수용할 적절한 Node를 NodePool 정책에 따라 생성한다**고 이해하는 것이 맞다.

### 24.5 Node와 Worker의 관계

Worker는 Node를 만드는 것이 아니라, **Node 위에서 실행되는 Pod**다.

구조를 단순화하면 다음과 같다.

```text
EKS Cluster
  -> Node Group / NodePool
    -> EC2 Node
      -> Pod
        -> Container
```

예를 들어:

- `utter-api Pod`는 `api NodePool` 조건을 만족하는 Node 위에 올라간다.
- `cpu-analysis-worker Pod`는 `ai-cpu NodePool` 조건을 만족하는 Node 위에 올라간다.
- `gpu-diarization-worker Pod`는 `ai-gpu NodePool` 조건과 GPU 자원 조건을 만족하는 Node 위에 올라간다.

### 24.6 이 문서 구조에서 최종 해석

이 문서의 EKS 구조를 한 문장으로 정리하면 다음과 같다.

```text
클러스터는 1개이고,
system Managed Node Group은 기본 워커 노드를 제공하며,
api / ai-cpu / ai-gpu / spot-batch / general NodePool은
워크로드 요구사항에 따라 여러 AZ 서브넷에 걸쳐 필요한 시점에 node를 생성한다.
```

즉 기능은 `NodePool`이 나누고, 가용성은 `멀티 AZ`가 담당한다.

이 흐름에서 내 파트는 **CloudFront 이후 API 요청이 EKS 내부로 들어오고, API/AI/Batch Pod가 적절한 노드에 배치되며, SQS 부하에 따라 Pod와 Node가 함께 확장되는 구조**를 담당한다.

```text
저는 EKS 기반 플랫폼 레이어를 담당해 API, CPU AI, GPU AI, Batch 작업을 각각 다른 NodePool로 분리하고,
KEDA와 Karpenter를 통해 SQS 부하에 따라 Pod와 Node가 함께 자동 확장되는 구조를 설계했습니다.
특히 화자 분리처럼 GPU가 필요한 작업은 ai-gpu NodePool로 격리해 비용 낭비와 자원 경합을 줄였습니다.
```
