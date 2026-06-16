# Terraform 레이어 의존 흐름

dev 환경 4개 레이어가 서로 어떤 output을 주고받으며 리소스가 설치되는지 정리한다.  
apply/destroy 절차는 [terraform-ops.md](./terraform-ops.md) 참고.

---

## 전체 의존 구조

```
01-network ──────────────────────────────────────────────────┐
  └─ vpc_id, subnet IDs, nat_gateway_id                      │
         │                                                    │
         ▼                                                    │
02-eks ──────────────────────────────────────────────────────┤
  └─ cluster_name, cluster_endpoint, oidc_provider_*         │
     node_security_group_id, cluster_security_group_id       │
         │                   │                               │
         ▼                   │                               │
03-services                  │                               │
  └─ lbc_role_arn            │                               │
     cluster_autoscaler_role_arn                             │
     eso_role_arn            │                               │
     frontend_bucket_*       │                               │
         │                   │                               │
         └─────────┬─────────┘                               │
                   ▼                                         │
04-addons ◄────────────────────────────────────────────────--┘
  ├─ Helm: LBC, CA, ESO, Prometheus, Loki, Promtail, NVIDIA, ArgoCD
  └─ CloudFront (ALB DNS는 data.aws_lb로 동적 조회)
```

레이어 간 값은 S3 remote state로 전달된다.  
각 레이어 state key:

| 레이어 | S3 key |
|--------|--------|
| 01-network | `dev/network/terraform.tfstate` |
| 02-eks | `dev/platform/terraform.tfstate` |
| 03-services | `dev/services/terraform.tfstate` |
| 04-addons | `dev/addons/terraform.tfstate` |

---

## 01-network

### 생성 리소스

| 리소스 | 내용 |
|--------|------|
| VPC | CIDR 블록, DNS 활성화 |
| 퍼블릭 서브넷 | ALB, NAT GW 배치 (멀티 AZ) |
| 프라이빗 앱 서브넷 | EKS 노드, Pod 배치 |
| 프라이빗 데이터 서브넷 | RDS, ElastiCache 배치 |
| NAT Gateway | 프라이빗 서브넷의 아웃바운드 인터넷 |
| 라우팅 테이블 | 퍼블릭/프라이빗 분리 |

### Outputs → 하위 레이어에서 사용

```
vpc_id                  → 02-eks (클러스터 VPC 지정)
                        → 03-services (RDS/Redis SG, IRSA)
                        → 04-addons (LBC vpcId)
public_subnet_ids       → (ALB 배치에 참고)
private_app_subnet_ids  → 02-eks (노드그룹 서브넷)
                        → 03-services (IRSA VPC endpoint)
private_data_subnet_ids → 03-services (RDS, Redis 서브넷)
nat_gateway_id          → (참조용)
```

---

## 02-eks

### 읽어오는 값 (01-network remote state)

```hcl
vpc_id                 = data.terraform_remote_state.network.outputs.vpc_id
private_app_subnet_ids = data.terraform_remote_state.network.outputs.private_app_subnet_ids
```

### 생성 리소스

| 리소스 | 내용 |
|--------|------|
| EKS 클러스터 | K8s 1.31, API+ConfigMap 인증 |
| OIDC Provider | IRSA(Pod IAM 역할) 연동용 |
| 노드 IAM Role | EC2 워커 노드 공통 역할 |
| EKS Addon: vpc-cni | Pod IP 직접 할당 (Prefix Delegation) |
| EKS Addon: coredns | 클러스터 내부 DNS |
| EKS Addon: kube-proxy | 노드 간 네트워크 규칙 |
| 노드 Security Group | 노드↔노드, 컨트롤플레인↔노드 통신 |
| NodeGroup: system | t3.small, desired 1 / min 1 / max 2 (CA 관리) |
| NodeGroup: api | t3.medium, desired 1 / min 1 / max 2 (CA 관리) |
| NodeGroup: worker | m5.xlarge, desired 1 / min 1 / max 10 (CA 관리) |
| NodeGroup: gpu | g4dn.xlarge, desired 1 / min 1 / max 2 (CA 관리) |

모든 노드그룹에 CA 자동 검색 태그가 붙음:
```
k8s.io/cluster-autoscaler/enabled          = "true"
k8s.io/cluster-autoscaler/<cluster-name>   = "owned"
```

### Outputs → 하위 레이어에서 사용

```
cluster_name              → 03-services (IRSA 연동)
                          → 04-addons (LBC clusterName, CA autoDiscovery)
cluster_endpoint          → 04-addons (Helm provider 연결)
cluster_ca_certificate    → 04-addons (Helm provider 연결)
oidc_provider_arn         → 03-services (IRSA Trust Policy)
oidc_provider_url         → 03-services (IRSA Trust Policy)
node_security_group_id    → 03-services (RDS/Redis 인바운드 허용)
cluster_security_group_id → 03-services (RDS/Redis 컨트롤플레인 허용)
node_role_arn             → (참조용)
```

---

## 03-services

### 읽어오는 값

```hcl
# 01-network
vpc_id                 = data.terraform_remote_state.network.outputs.vpc_id
private_app_subnet_ids = data.terraform_remote_state.network.outputs.private_app_subnet_ids
private_data_subnet_ids = data.terraform_remote_state.network.outputs.private_data_subnet_ids

# 02-eks
oidc_provider_arn      = data.terraform_remote_state.eks.outputs.oidc_provider_arn
oidc_provider_url      = data.terraform_remote_state.eks.outputs.oidc_provider_url
node_security_group_id = data.terraform_remote_state.eks.outputs.node_security_group_id
cluster_security_group_id = data.terraform_remote_state.eks.outputs.cluster_security_group_id
```

### 생성 리소스

| 리소스 | 내용 |
|--------|------|
| RDS (PostgreSQL) | 프라이빗 데이터 서브넷, 노드 SG에서만 인바운드 허용 |
| ElastiCache (Redis) | 프라이빗 데이터 서브넷, 노드 SG에서만 인바운드 허용 |
| S3: raw-audio | 음성 파일 원본 저장 |
| S3: reports | 분석 리포트 저장 |
| S3: template | 리포트 템플릿 |
| S3: rag-ingest | RAG 벡터 데이터 |
| S3: frontend | 정적 웹 빌드 파일 (`/current` 경로 사용) |
| SQS: audio-preprocess | 음성 전처리 작업 큐 |
| SQS: gpu-inference | GPU 추론 작업 큐 |
| SQS: report-analysis | 리포트 분석 큐 |
| SQS: rag-ingest | RAG 인제스트 큐 |
| Secrets Manager | backend-api, ai-worker, gpu-worker 시크릿 |
| ECR | utterai-backend, utterai-ai-cpu, utterai-ai-gpu 레포 |
| IRSA Role: backend-api | S3(reports), RDS, SQS 접근 |
| IRSA Role: ai-api | S3, SQS 접근 |
| IRSA Role: cpu-worker | S3, SQS, Bedrock 접근 |
| IRSA Role: gpu-worker | S3, SQS 접근 |
| IRSA Role: batch-worker | S3, SQS 접근 |
| IRSA Role: lbc | ALB 생성/관리 권한 |
| IRSA Role: cluster-autoscaler | EC2 AutoScaling 관리 권한 |
| IRSA Role: eso | Secrets Manager 읽기 권한 |

IRSA는 OIDC provider를 통해 특정 ServiceAccount에만 IAM Role을 부여한다:
```
Pod(ServiceAccount: utterai-api-sa)
  → OIDC 연동
  → IAM Role(backend-api-role) Assume
  → AWS API 직접 호출 (EC2 메타데이터 자격증명 불필요)
```

### Outputs → 04-addons에서 사용

```
lbc_role_arn                → LBC Helm serviceAccount annotation
cluster_autoscaler_role_arn → CA Helm serviceAccount annotation
eso_role_arn                → ESO Helm serviceAccount annotation
frontend_bucket_name        → CloudFront S3 origin
frontend_bucket_arn         → S3 버킷 정책 (OAC 허용)
```

---

## 04-addons

### 읽어오는 값

```hcl
# 01-network
vpc_id           = data.terraform_remote_state.network.outputs.vpc_id

# 02-eks
cluster_name     = data.terraform_remote_state.eks.outputs.cluster_name
cluster_endpoint = data.terraform_remote_state.eks.outputs.cluster_endpoint

# 03-services
lbc_irsa_role_arn                = data.terraform_remote_state.services.outputs.lbc_role_arn
cluster_autoscaler_irsa_role_arn = data.terraform_remote_state.services.outputs.cluster_autoscaler_role_arn
eso_irsa_role_arn                = data.terraform_remote_state.services.outputs.eso_role_arn
frontend_bucket_id               = data.terraform_remote_state.services.outputs.frontend_bucket_name
frontend_bucket_arn              = data.terraform_remote_state.services.outputs.frontend_bucket_arn

# ALB DNS — data source로 동적 조회 (하단 참고)
alb_dns_name     = data.aws_lb.api.dns_name
```

### 생성 리소스 (Helm)

| Helm 릴리스 | 네임스페이스 | 역할 |
|------------|------------|------|
| aws-load-balancer-controller | ingress-system | Ingress → ALB 자동 생성 |
| cluster-autoscaler | kube-system | Pending Pod 감지 → 노드그룹 스케일 아웃/인 |
| metrics-server | kube-system | HPA CPU/메모리 메트릭 수집 |
| kube-prometheus-stack | monitoring | Prometheus + Grafana + Alertmanager |
| loki | monitoring | 로그 수집/저장 (SingleBinary, 파일시스템) |
| promtail | monitoring | 각 노드 로그 → Loki 전송 (DaemonSet) |
| external-secrets | external-secrets | ESO: Secrets Manager → K8s Secret 동기화 |
| nvidia-device-plugin | kube-system | GPU 노드에서 `nvidia.com/gpu` 리소스 노출 |
| argocd | argocd | GitOps 배포 오케스트레이션 |

### ALB DNS 자동 조회 흐름

CloudFront의 API origin에 ALB DNS를 하드코딩하면 ALB 재생성 시마다 수동 업데이트가 필요하다.  
대신 AWS LBC가 ALB 생성 시 자동으로 붙이는 태그를 이용해 동적으로 조회한다.

```
[02-eks] EKS 클러스터 생성
  output: cluster_name = "utterai-dev-eks"
          ↓
[04-addons] LBC Helm 설치
  set { name = "clusterName", value = cluster_name }
          ↓ LBC가 클러스터 이름을 인지함
[ArgoCD] Ingress 배포 (k8s-demo/apps/backend/base/ingress.yaml)
  annotation:
    alb.ingress.kubernetes.io/group.name: utterai-dev  ← 그룹 이름 지정
          ↓
[AWS LBC] Ingress 감지 → ALB 자동 생성 + 태그 부착
  elbv2.k8s.aws/cluster  = "utterai-dev-eks"   ← clusterName에서
  ingress.k8s.aws/stack  = "utterai-dev"        ← group.name에서
  ingress.k8s.aws/resource = "LoadBalancer"
          ↓
[04-addons] data "aws_lb" 로 태그 기반 조회
  data "aws_lb" "api" {
    tags = {
      "elbv2.k8s.aws/cluster" = eks_remote_state.cluster_name
      "ingress.k8s.aws/stack" = "utterai-dev"
    }
  }
  → dns_name을 CloudFront origin에 주입
```

`group.name` 은 여러 Ingress를 하나의 ALB로 묶는 기능이다.  
ALB가 재생성돼도 같은 태그가 붙으므로 data source가 항상 올바른 ALB를 찾는다.

### 생성 리소스 (AWS)

| 리소스 | 내용 |
|--------|------|
| CloudFront Distribution | S3(프론트) + ALB(API) 통합 CDN |
| CloudFront OAC | S3 접근 제어 (퍼블릭 차단, CF만 허용) |
| S3 Bucket Policy | OAC ARN 기반 GetObject 허용 |

CloudFront 라우팅:
```
https://<CF도메인>/          → S3 /current/index.html (SPA)
https://<CF도메인>/api/*     → ALB (HTTP, 캐시 없음)
403/404                      → index.html (SPA 라우팅)
```

### Outputs

```
cloudfront_distribution_id  → Invalidation 시 사용
cloudfront_domain_name      → 프론트엔드 접속 URL
```

---

## Apply 순서 요약

```
01-network → 02-eks → 03-services → 04-addons
    ↓            ↓          ↓
  VPC 생성    EKS 생성   RDS/Redis/S3    Helm + CloudFront
              (VPC 필요)  SQS/IRSA        (모든 레이어 필요)
                          (VPC+OIDC 필요)

  ※ 04-addons apply 전에 ArgoCD가 Ingress를 배포해야
     data.aws_lb 조회가 성공한다.
     Ingress가 없으면 ALB가 없어 plan/apply 실패.
```

## Destroy 순서 요약

```
Ingress 삭제(kubectl) → 04-addons → 02-eks → 03-services → 01-network
       ↓                    ↓           ↓           ↓
  ALB 자동 정리       Helm/CF 삭제  EKS 삭제   DB/S3 삭제    VPC 삭제
  (LBC가 처리)

  ※ Ingress를 먼저 지워야 LBC가 ALB를 정리한다.
     ALB가 남아있으면 VPC Security Group이 묶여 01-network destroy 실패.
  ※ 03-services destroy는 데이터 삭제를 수반하므로 신중히 결정.
```
