# Terraform 운영 가이드

dev 환경 인프라의 apply/destroy 절차를 정리한다.

---

## 레이어 구조

```
terraform/environments/dev/
├── 01-network   → VPC, 서브넷, 라우팅
├── 02-eks       → EKS 클러스터, 노드그룹, OIDC
├── 03-services  → RDS, Redis, S3, SQS, ECR, Secrets Manager, IRSA
└── 04-addons    → EKS 애드온(LBC·CA·ESO), CloudFront
```

| 레이어 | State key | 주요 리소스 |
|--------|-----------|------------|
| 01-network | `dev/network/terraform.tfstate` | VPC, 서브넷 |
| 02-eks | `dev/platform/terraform.tfstate` | EKS, 노드그룹 |
| 03-services | `dev/services/terraform.tfstate` | RDS, Redis, S3, SQS, ECR, Secrets |
| 04-addons | `dev/addons/terraform.tfstate` | LBC, Cluster Autoscaler, CloudFront |

레이어 간 의존 관계: `01 → 02 → 03 → 04`  
각 레이어는 상위 레이어의 state를 `terraform_remote_state`로 참조한다.

---

## Apply

**반드시 의존 순서대로 실행한다.**

```bash
# 공통: 각 레이어 디렉토리에서 init 후 apply
cd terraform/environments/dev/<레이어>
terraform init
terraform plan   # 변경 내용 확인
terraform apply
```

### 전체 apply (최초 구축)

```bash
for layer in 01-network 02-eks 03-services 04-addons; do
  cd terraform/environments/dev/$layer
  terraform init && terraform apply -auto-approve
  cd -
done
```

### 특정 리소스만 apply

```bash
# GPU 노드그룹만
cd terraform/environments/dev/02-eks
terraform apply -target=module.eks.aws_eks_node_group.gpu

# worker 노드그룹 디스크 크기 변경
terraform apply -target=module.eks.aws_eks_node_group.worker

# CloudFront만
cd terraform/environments/dev/04-addons
terraform apply -target=module.cloudfront
```

---

## Destroy — EKS만 내리기 (스토리지 유지)

> **RDS·S3·SQS·ECR·Secrets Manager는 03-services에 있다.**  
> 03-services를 destroy하면 데이터가 삭제된다. 아래 절차는 03-services를 건드리지 않는다.

### 1단계: k8s 워크로드 삭제 (ALB 해제)

EKS를 내리기 전에 Load Balancer Controller가 생성한 ALB를 먼저 해제해야 한다.  
그렇지 않으면 ALB/Security Group이 VPC에 남아 네트워크 destroy가 실패한다.

```bash
# 모든 namespace의 Ingress 삭제 → ALB 자동 삭제됨
kubectl delete ingress --all -A

# 워크로드 전체 삭제 (선택)
kubectl delete -f k8s/workloads/ --ignore-not-found
```

ALB가 삭제됐는지 확인:
```bash
aws elbv2 describe-load-balancers \
  --query 'LoadBalancers[?contains(LoadBalancerName,`utterai`)].LoadBalancerName' \
  --output text
```

### 2단계: 04-addons destroy

```bash
cd terraform/environments/dev/04-addons
terraform destroy
```

Helm 릴리스(LBC, Cluster Autoscaler, ESO), CloudFront, 관련 IAM 리소스가 삭제된다.

### 3단계: 02-eks destroy

```bash
cd terraform/environments/dev/02-eks
terraform destroy
```

EKS 클러스터, 노드그룹(system/api/worker/gpu), OIDC provider가 삭제된다.  
03-services와 01-network는 변경되지 않는다.

---

## Destroy — 노드그룹만 내리기

클러스터는 유지하고 특정 노드그룹만 삭제할 때:

```bash
cd terraform/environments/dev/02-eks

# GPU 노드그룹만 삭제 (비용 절감)
terraform destroy \
  -target=module.eks.aws_eks_node_group.gpu \
  -target=module.eks.aws_launch_template.gpu

# worker 노드그룹만 삭제
terraform destroy -target=module.eks.aws_eks_node_group.worker

# api 노드그룹만 삭제
terraform destroy -target=module.eks.aws_eks_node_group.api
```

> **주의**: 노드그룹 삭제 전 해당 노드에 있는 파드를 drain하거나 deployment를 scale 0으로 내려두는 것을 권장한다.
> ```bash
> # 특정 노드 drain
> kubectl drain <node-name> --ignore-daemonsets --delete-emptydir-data
> ```

---

## Destroy — 03-services (데이터 포함 삭제)

> **경고**: 아래 명령은 RDS 데이터, S3 객체(버킷이 비어있으면), ECR 이미지, SQS 메시지를 삭제한다.  
> 반드시 데이터 백업 후 실행할 것.

```bash
cd terraform/environments/dev/03-services
terraform destroy
```

현재 설정값:
- RDS: `deletion_protection = false`, `skip_final_snapshot = true` → **즉시 삭제, 스냅샷 없음**
- S3: `force_destroy` 없음 → 버킷에 객체가 있으면 destroy 실패

S3 버킷을 강제로 비우려면:
```bash
# 버킷 내 모든 객체 삭제 후 destroy
aws s3 rm s3://utterai-dev-raw-audio --recursive
aws s3 rm s3://utterai-dev-documents --recursive
aws s3 rm s3://utterai-dev-reports --recursive
```

---

## Destroy — 전체 (완전 삭제)

```bash
for layer in 04-addons 02-eks 03-services 01-network; do
  cd terraform/environments/dev/$layer
  terraform destroy -auto-approve
  cd -
done
```

> 01-network(VPC)는 마지막에 내린다. 03-services의 RDS, ElastiCache가 VPC 안에 있으므로 먼저 삭제돼야 한다.

---

## 자주 쓰는 state 조작 명령어

```bash
# 현재 state 목록 확인
terraform state list

# 특정 리소스 state에서 제거 (실제 AWS 리소스는 유지)
terraform state rm <resource_address>

# 기존 AWS 리소스를 state에 등록 (콘솔에서 만든 리소스 가져올 때)
terraform import <resource_address> <aws_resource_id>

# state에서 리소스 확인
terraform state show <resource_address>
```

실제 사용 예시 (GPU 노드그룹 재import):
```bash
terraform state rm module.eks.aws_eks_node_group.gpu
terraform import module.eks.aws_eks_node_group.gpu utterai-dev-eks:utterai-dev-gpu
```

---

## 비용 절감 — 야간/주말 EKS 절전

클러스터 자체는 유지하고 노드그룹 desired_size를 0으로 줄이면 EC2 비용만 절감된다.

```bash
# 노드그룹 스케일 다운 (desired=0)
aws eks update-nodegroup-config \
  --cluster-name utterai-dev-eks \
  --nodegroup-name utterai-dev-worker \
  --scaling-config minSize=0,maxSize=3,desiredSize=0 \
  --region ap-northeast-2

aws eks update-nodegroup-config \
  --cluster-name utterai-dev-eks \
  --nodegroup-name utterai-dev-gpu \
  --scaling-config minSize=0,maxSize=2,desiredSize=0 \
  --region ap-northeast-2

# 복구 시
aws eks update-nodegroup-config \
  --cluster-name utterai-dev-eks \
  --nodegroup-name utterai-dev-worker \
  --scaling-config minSize=1,maxSize=3,desiredSize=1 \
  --region ap-northeast-2
```

> Terraform state와 desiredSize 값이 맞지 않게 되므로, 이후 terraform apply 전에 `terraform plan`으로 drift 확인 필요.
