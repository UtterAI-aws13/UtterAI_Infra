# 2026-06-12 Dev 환경 트러블슈팅 기록

EKS 클러스터 초기 배포 후 Pod 기동 불량에 대한 원인 분석 및 해결 과정을 기록한다.

---

## 1. IRSA ARN에 AWS Account ID 누락

### 증상

```
utterai-ai-cpu    utterai-cpu-worker    0/1   CrashLoopBackOff   exit code: 0
utterai-batch     utterai-batch-worker  0/1   CrashLoopBackOff   exit code: 0
utterai-ai-api    utterai-ai-api        0/1   CrashLoopBackOff   exit code: 0
utterai-ai-gpu    utterai-ml-gpu-worker 0/1   ContainerStatusUnknown
```

### 원인

`k8s-legacy/rbac/serviceaccounts.yaml`에 IRSA ARN이 `${AWS_ACCOUNT_ID}` 플레이스홀더로 정의되어 있으나, 배포 시 `envsubst` 없이 `kubectl apply -f`를 직접 실행해 Account ID가 빈 문자열로 치환됨.

```yaml
# 의도한 값
eks.amazonaws.com/role-arn: arn:aws:iam::032886669461:role/utterai-dev-ai-cpu-irsa-role

# 실제 적용된 값
eks.amazonaws.com/role-arn: arn:aws:iam:::role/utterai-dev-ai-cpu-irsa-role
#                                          ^^ Account ID 누락
```

빈 ARN으로는 STS AssumeRoleWithWebIdentity가 실패해 AWS 리소스(SQS, S3 등) 접근 불가 → Worker가 AWS SDK 초기화 실패 후 exit 0으로 종료.

### 진단 명령어

```bash
# SA annotation 확인
kubectl get sa -n utterai-ai-cpu utterai-cpu-worker-sa \
  -o jsonpath='{.metadata.annotations.eks\.amazonaws\.com/role-arn}'
# 결과: arn:aws:iam:::role/utterai-dev-ai-cpu-irsa-role  ← Account ID 비어있음

# 컨테이너 안에서 AWS 자격증명 확인
kubectl run debug --image=<image> --restart=Never \
  --overrides='{"spec":{"serviceAccountName":"utterai-cpu-worker-sa"}}' \
  --command -- python3 -c "import boto3; print(boto3.client('sts').get_caller_identity())"
```

### 해결

SA annotation을 올바른 Account ID로 직접 패치한 후, envsubst를 통해 재적용.

```bash
# 즉시 패치 (클러스터)
kubectl annotate serviceaccount -n utterai-ai-cpu utterai-cpu-worker-sa \
  eks.amazonaws.com/role-arn=arn:aws:iam::032886669461:role/utterai-dev-ai-cpu-irsa-role \
  --overwrite
# utterai-ai-api, utterai-ai-gpu, utterai-batch 동일하게 적용

# 소스 파일 재적용 (envsubst로 Account ID 주입)
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
for f in k8s-legacy/rbac/*.yaml; do
  envsubst < "$f" | kubectl apply -f -
done
```

### 재발 방지

`k8s-legacy/rbac/*.yaml`은 반드시 `scripts/k8s-deploy-legacy.sh`를 통해 배포할 것. 스크립트 내부에서 `aws sts get-caller-identity`로 Account ID를 자동 주입한 뒤 `envsubst`를 실행함.

---

## 2. utterai-ai-api readiness/liveness probe 경로 불일치

### 증상

```
utterai-ai-api    utterai-ai-api    0/1   CrashLoopBackOff
```

로그:
```
INFO:  GET /health HTTP/1.1" 404 Not Found   ← 반복 후 failureThreshold 초과
INFO:  Shutting down
```

### 원인

`k8s-legacy/workloads/ai-api-deployment.yaml`의 readiness/liveness probe 경로가 `/health`로 설정되어 있었으나 실제 FastAPI 앱의 헬스체크 엔드포인트는 `/health/ready`와 `/health/live`.

```yaml
# 수정 전
readinessProbe:
  httpGet:
    path: /health      # 404
livenessProbe:
  httpGet:
    path: /health      # 404
```

ai-api는 DB 연결이 없으므로 readiness도 `/health/live`를 사용한다 (`/health/ready`는 DB 연결을 검사하므로 불필요한 실패 유발).

### 해결

**수정 파일**: `k8s-legacy/workloads/ai-api-deployment.yaml`

```yaml
# 수정 후
readinessProbe:
  httpGet:
    path: /health/live
livenessProbe:
  httpGet:
    path: /health/live
```

클러스터에도 즉시 적용:

```bash
kubectl patch deployment -n utterai-ai-api utterai-ai-api --type='json' \
  -p='[
    {"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/httpGet/path","value":"/health/live"},
    {"op":"replace","path":"/spec/template/spec/containers/0/livenessProbe/httpGet/path","value":"/health/live"}
  ]'
```

---

## 3. cpu-worker / batch-worker `__main__` 진입점 누락

### 증상

```
utterai-ai-cpu    utterai-cpu-worker    0/1   CrashLoopBackOff   exit code: 0  (5초 만에 종료)
utterai-batch     utterai-batch-worker  0/1   CrashLoopBackOff   exit code: 0  (5초 만에 종료)
```

로그:
```
UserWarning: pkg_resources is deprecated ...
  from pkg_resources import (
# 이후 아무 출력 없음
```

### 원인

Worker 모듈(`app/workers/cpu_worker.py`, `app/workers/rag_ingest_worker.py`)에 `if __name__ == "__main__": start_worker()` 블록이 없었음. `python -m app.workers.cpu_worker` 실행 시 함수 정의만 하고 아무것도 호출하지 않아 즉시 exit 0으로 종료.

exit code 0이어서 애플리케이션 오류처럼 보이지 않아 진단이 지연됨.

### 진단 과정

```bash
# 1. IRSA 정상 여부 확인 (AWS 자격증명 테스트)
kubectl run cpu-debug --image=<image> --restart=Never \
  --overrides='{"spec":{"serviceAccountName":"utterai-cpu-worker-sa"}}' \
  --command -- python3 -c "import boto3; print(boto3.client('sts').get_caller_identity()['Arn'])"
# 결과: arn:aws:sts::032886669461:assumed-role/... (IRSA 정상)

# 2. 워커 소스 코드 직접 확인
kubectl run debug --image=<image> --restart=Never \
  --command -- sh -c "tail -20 /app/app/workers/cpu_worker.py"
# 결과: except Exception as e: 블록으로 끝남, __main__ 없음
```

### 해결

AI 레포에서 각 worker 파일 끝에 진입점 추가 후 이미지 재빌드.

```python
# app/workers/cpu_worker.py 끝에 추가
if __name__ == "__main__":
    start_worker()

# app/workers/rag_ingest_worker.py 끝에 추가
if __name__ == "__main__":
    start_worker()
```

새 이미지(`dev-b24c8b0`) 빌드 후 deployment 업데이트:

```bash
kubectl set image deployment/utterai-cpu-worker -n utterai-ai-cpu \
  worker=032886669461.dkr.ecr.ap-northeast-2.amazonaws.com/utterai-ai-cpu:dev-b24c8b0

kubectl set image deployment/utterai-batch-worker -n utterai-batch \
  worker=032886669461.dkr.ecr.ap-northeast-2.amazonaws.com/utterai-ai-cpu:dev-b24c8b0

kubectl rollout status deployment/utterai-cpu-worker -n utterai-ai-cpu
kubectl rollout status deployment/utterai-batch-worker -n utterai-batch
```

---

## 4. CloudFront 도메인 ERR_NAME_NOT_RESOLVED

### 증상

브라우저 콘솔:
```
GET https://d4kxfdssuth29.cloudfront.net/api/v1/... net::ERR_NAME_NOT_RESOLVED
```

API pod는 Running 상태이고 ALB까지 연결은 정상이나, CloudFront URL 자체가 존재하지 않는 도메인.

### 원인

인프라 코드 전반에 이전 배포 시 사용했던 CloudFront 도메인(`d4kxfdssuth29.cloudfront.net`)이 하드코딩되어 있었음. 실제 배포된 CloudFront 배포의 도메인은 `d129p1nkqgquw3.cloudfront.net`.

영향 범위:
- `k8s-legacy/workloads/api-deployment.yaml` ConfigMap — `FRONTEND_ORIGIN`, `CORS_ALLOW_ORIGINS`
- `terraform/environments/dev/03-services/main.tf` — `frontend_domain`

### 진단 명령어

```bash
# 실제 CloudFront 배포 도메인 확인
aws cloudfront list-distributions \
  --query 'DistributionList.Items[*].{id:Id,domain:DomainName,origins:Origins.Items[*].DomainName}' \
  --output table
```

### 해결

**수정 파일**: `k8s-legacy/workloads/api-deployment.yaml`

```yaml
# 수정 전
FRONTEND_ORIGIN: "https://d4kxfdssuth29.cloudfront.net"
CORS_ALLOW_ORIGINS: "https://d4kxfdssuth29.cloudfront.net"

# 수정 후
FRONTEND_ORIGIN: "https://d129p1nkqgquw3.cloudfront.net"
CORS_ALLOW_ORIGINS: "https://d129p1nkqgquw3.cloudfront.net"
```

**수정 파일**: `terraform/environments/dev/03-services/main.tf`

```hcl
# 수정 전
frontend_domain = "d4kxfdssuth29.cloudfront.net"

# 수정 후
frontend_domain = "d129p1nkqgquw3.cloudfront.net"
```

클러스터 ConfigMap 즉시 반영:

```bash
kubectl patch configmap utterai-api-config -n utterai-api --type merge \
  -p '{"data":{"FRONTEND_ORIGIN":"https://d129p1nkqgquw3.cloudfront.net","CORS_ALLOW_ORIGINS":"https://d129p1nkqgquw3.cloudfront.net"}}'
kubectl rollout restart deployment/utterai-api -n utterai-api
```

---

## 5. CORS_ORIGINS pydantic-settings 파싱 오류

### 증상

```
utterai-api    utterai-api    0/1   Init:CrashLoopBackOff
```

로그:
```
pydantic_settings.utils.SettingsError: error parsing value for field "cors_origins" from source "EnvSettingsSource"
```

### 원인

`settings.py`에서 `cors_origins: list[str]` 필드를 환경변수로 받는데, pydantic-settings는 `list[str]`을 파싱할 때 JSON 배열 형식(`["...", "..."]`)을 기대함. ConfigMap에 plain string으로 설정되어 있었음.

```yaml
# 잘못된 형식 (plain string)
CORS_ORIGINS: "https://d129p1nkqgquw3.cloudfront.net"

# 올바른 형식 (JSON 배열)
CORS_ORIGINS: "[\"https://d129p1nkqgquw3.cloudfront.net\",\"http://localhost:5173\"]"
```

### 해결

**수정 파일**: `k8s-legacy/workloads/api-deployment.yaml`

```yaml
CORS_ORIGINS: "[\"https://d129p1nkqgquw3.cloudfront.net\",\"http://localhost:5173\"]"
```

클러스터 즉시 반영:

```bash
kubectl patch configmap utterai-api-config -n utterai-api --type merge \
  -p '{"data":{"CORS_ORIGINS":"[\"https://d129p1nkqgquw3.cloudfront.net\",\"http://localhost:5173\"]"}}'
kubectl rollout restart deployment/utterai-api -n utterai-api
```

---

## 6. Worker 노드 disk-pressure 연쇄 eviction 루프

### 증상

```
batch-worker    utterai-batch-worker    0/1   Evicted   (반복)
cpu-worker      utterai-cpu-worker      0/1   Evicted   (반복)
```

노드 상태:
```
Taints: node.kubernetes.io/disk-pressure:NoSchedule
Conditions:
  DiskPressure: True  ← no space left on device
```

### 원인

Worker 노드 EBS 기본값 20GB + 대용량 ML 이미지(utterai-ai-cpu ~3GB, utterai-ai-gpu ~7GB)로 디스크가 꽉 참. eviction → 파드 재생성 → 이미지 재pull → 또 디스크 부족의 무한 루프.

복합 원인:
1. Terraform `worker_node_disk_size` 변수 미정의 → 기본값 20GB
2. 파드에 `ephemeral-storage` limit 미설정 → 단일 파드가 디스크를 독점 가능
3. 미사용 컨테이너 이미지 자동 GC 없음

### 해결

**① EBS 디스크 50GB로 증설**

`terraform/modules/eks/variables.tf`:
```hcl
variable "worker_node_disk_size" {
  type    = number
  default = 50
}
```

`terraform/modules/eks/main.tf`:
```hcl
resource "aws_eks_node_group" "worker" {
  disk_size = var.worker_node_disk_size
  # ...
}
```

`terraform/environments/dev/02-eks/main.tf`:
```hcl
module "eks" {
  worker_node_disk_size = var.worker_node_disk_size
}
```

```bash
terraform apply -target=module.eks.aws_eks_node_group.worker
```

**② ephemeral-storage limit 추가**

`k8s-legacy/workloads/cpu-worker-deployment.yaml`:
```yaml
resources:
  requests:
    cpu: "2"
    memory: "4Gi"
    ephemeral-storage: "10Gi"
  limits:
    cpu: "4"
    memory: "8Gi"
    ephemeral-storage: "20Gi"
```

`k8s-legacy/workloads/batch-worker-deployment.yaml`:
```yaml
resources:
  requests:
    cpu: "1"
    memory: "2Gi"
    ephemeral-storage: "5Gi"
  limits:
    cpu: "2"
    memory: "4Gi"
    ephemeral-storage: "10Gi"
```

**③ 미사용 이미지 자동 정리 DaemonSet**

`k8s-legacy/workloads/image-pruner.yaml` 신규 생성 — worker 노드에서 1시간마다 `crictl rmi --prune` 실행:

```yaml
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: utterai-image-pruner
  namespace: kube-system
spec:
  template:
    spec:
      nodeSelector:
        workload: worker
      hostPID: true
      containers:
        - name: pruner
          image: alpine:3.19
          command:
            - /bin/sh
            - -c
            - |
              while true; do
                nsenter -t 1 -m -- crictl rmi --prune || true
                sleep 3600
              done
          securityContext:
            privileged: true
```

```bash
kubectl apply -f k8s-legacy/workloads/image-pruner.yaml
# 결과: 3개 worker 노드에 DaemonSet Pod 기동 확인
```

---

## 7. GPU 노드 disk-pressure — `disk_size` 파라미터 무효

### 증상

```
utterai-ai-gpu    utterai-ml-gpu-worker    0/1   Pending
```

GPU 노드 상태:
```
Taints: dedicated=ai-gpu:NoSchedule
        node.kubernetes.io/disk-pressure:NoSchedule
Conditions:
  DiskPressure: True  ← kubelet has disk pressure
```

### 원인

`terraform/modules/eks/main.tf`의 `aws_eks_node_group.gpu`에 `disk_size = 100`을 추가했으나 **`AL2023_x86_64_NVIDIA` AMI 타입에서는 `disk_size` 파라미터가 무시됨**. Terraform plan/apply 후 "No changes"로 응답하며 실제 디스크 크기는 기본값인 20GB 유지.

```bash
# 실제 노드 디스크 크기 확인
INSTANCE_ID=$(kubectl get node ip-10-10-11-250... \
  -o jsonpath='{.spec.providerID}' | cut -d'/' -f5)
aws ec2 describe-volumes --volume-ids <vol-id> \
  --query 'Volumes[0].Size' --output text
# 결과: 20  ← disk_size = 100 설정에도 불구하고 20GB
```

NVIDIA AMI의 경우 `disk_size` 대신 **Launch Template의 `block_device_mappings`** 으로만 디스크 크기를 제어할 수 있음.

또한 `disk_size` 파라미터와 `launch_template` 블록을 **동시에 사용할 수 없음**. 함께 설정 시 다음 오류 발생:
```
InvalidParameterException: Disk size must be specified within the launch template
```

### 해결

**수정 파일**: `terraform/modules/eks/main.tf`

`aws_launch_template.gpu` 리소스를 추가하고 node group에서 참조. `disk_size` 파라미터는 제거:

```hcl
resource "aws_launch_template" "gpu" {
  name_prefix = "${local.prefix}-gpu-"

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = 100
      volume_type           = "gp3"
      delete_on_termination = true
    }
  }

  tag_specifications {
    resource_type = "instance"
    tags = {
      Name = "${local.prefix}-gpu-node"
    }
  }
}

resource "aws_eks_node_group" "gpu" {
  # disk_size 제거 — launch_template으로 대체
  launch_template {
    id      = aws_launch_template.gpu.id
    version = aws_launch_template.gpu.latest_version
  }
}
```

> **주의**: launch template 추가는 node group을 **destroy & recreate**함.

### terraform state 불일치 대응

launch template이 콘솔에서 수동 삭제되거나 terraform state와 실제 AWS 리소스가 어긋났을 때:

```bash
# 1. state에서 GPU node group 제거 후 import
terraform state rm module.eks.aws_eks_node_group.gpu
terraform import module.eks.aws_eks_node_group.gpu utterai-dev-eks:utterai-dev-gpu

# 2. apply
terraform apply \
  -target=module.eks.aws_launch_template.gpu \
  -target=module.eks.aws_eks_node_group.gpu
```

---

## 8. Frontend CI — CloudFront Invalidation AccessDenied

### 증상

GitHub Actions FE 배포 CI 성공 후 브라우저에서 구버전 JS 번들 반환 또는:

```
An error occurred (AccessDenied) when calling the CreateInvalidation operation
```

### 원인

`utterai-dev-github-actions-frontend-deploy-role` IAM 역할에 `cloudfront:CreateInvalidation` 권한이 없었음.

### 해결

IAM 콘솔에서 해당 역할에 인라인 정책 추가:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "cloudfront:CreateInvalidation",
      "Resource": "arn:aws:cloudfront::032886669461:distribution/E1N6L79JRJI9XI"
    }
  ]
}
```

---

## 9. Frontend CI — 환경변수 미주입 (wrong branch)

### 증상

GitHub Actions FE 배포 CI 로그:
```
AWS_REGION:  (비어있음)
```

### 원인

GitHub Actions의 `environment: dev` 레벨 변수는 job에 `environment: dev` 선언이 있어야 주입됨. FE CI가 `main` 브랜치에서 트리거되어 `environment: prod`로 실행되었으나 실제 변수는 `dev` environment에만 정의되어 있었음.

### 해결

FE 레포의 `dev` 브랜치에서 CI를 트리거하거나, CI workflow에 `environment: dev` 를 명시적으로 선언.

```bash
# dev 브랜치에서 push 또는 workflow_dispatch 실행
git checkout dev && git push origin dev
```

---

## 10. GPU worker — `ModuleNotFoundError: boto3` (PATH 미설정)

### 증상

```
utterai-ai-gpu    utterai-ml-gpu-worker    0/1   CrashLoopBackOff   exit code: 1
```

로그:
```
File "/app/app/workers/ml_gpu_worker.py", line 8, in <module>
    import boto3
ModuleNotFoundError: No module named 'boto3'
```

### 원인

`pytorch/pytorch` base 이미지는 conda Python(`/opt/conda/bin/python`)을 PATH 기본값으로 등록함. `uv sync`는 패키지를 `/app/.venv`에 설치하지만, `ENV PATH`를 업데이트하지 않으면 CMD의 `python`이 conda Python을 가리켜 venv 패키지를 찾지 못함.

boto3는 `pyproject.toml` base `dependencies`에 정의되어 있으므로 venv에는 설치되어 있음. 문제는 **어느 Python이 실행되느냐**였음.

```
# 수정 전 Dockerfile.gpu (dev-8e4da75, dev-9aff4b0 빌드)
RUN uv sync --frozen --no-dev --extra gpu
# ENV PATH 없음
CMD ["python", "-m", "app.workers.ml_gpu_worker"]
#    ^^ /opt/conda/bin/python → boto3 없음
```

**이미지별 fix 포함 여부:**

| 태그 | PATH fix 포함 |
|------|--------------|
| `dev-8e4da75` | ❌ |
| `dev-9aff4b0` | ❌ (`3cb3143`과 병렬 브랜치에서 merge됨) |
| `dev-a730163` | ✅ |

### 해결

AI 레포 `Dockerfile.gpu`에 `ENV PATH` 추가 (commit `3cb3143`, PR #24로 merge):

```dockerfile
RUN uv sync --frozen --no-dev --extra gpu
ENV PATH="/app/.venv/bin:$PATH"   # 추가
CMD ["python", "-m", "app.workers.ml_gpu_worker"]
```

새 이미지 `dev-a730163`으로 deployment 업데이트:

```bash
kubectl set image deployment/utterai-ml-gpu-worker -n utterai-ai-gpu \
  ml-gpu-worker=032886669461.dkr.ecr.ap-northeast-2.amazonaws.com/utterai-ai-gpu:dev-a730163
```

### Rolling update 데드락 주의

GPU 노드가 1대(GPU 1개)일 때 rolling update 중 구 파드(CrashLoopBackOff)가 GPU resource request를 점유하면 신 파드가 스케줄되지 못하는 데드락 발생. 구 파드를 수동 삭제해 GPU 해제:

```bash
# 구 버전 CrashLoopBackOff 파드 삭제 → 신 파드 스케줄 가능
kubectl delete pod <old-crashloop-pod> -n utterai-ai-gpu
```

---

## 변경된 파일 요약

| 파일 | 변경 내용 |
|------|-----------|
| `k8s-legacy/workloads/ai-api-deployment.yaml` | probe 경로 `/health` → `/health/live` |
| `k8s-legacy/workloads/api-deployment.yaml` | CloudFront 도메인 교정, CORS_ORIGINS JSON 배열 형식 |
| `k8s-legacy/workloads/cpu-worker-deployment.yaml` | ephemeral-storage requests/limits 추가 |
| `k8s-legacy/workloads/batch-worker-deployment.yaml` | ephemeral-storage requests/limits 추가 |
| `k8s-legacy/workloads/image-pruner.yaml` | DaemonSet 신규 생성 (worker 노드 이미지 자동 GC) |
| `terraform/modules/eks/main.tf` | `aws_launch_template.gpu` 추가, GPU node group `disk_size` 제거 → launch template 연결 |
| `terraform/modules/eks/variables.tf` | `worker_node_disk_size` 변수 추가 (default 50) |
| `terraform/environments/dev/02-eks/main.tf` | `worker_node_disk_size` 모듈 전달 추가 |
| `terraform/environments/dev/02-eks/variables.tf` | `worker_node_disk_size` 변수 추가 |
| `terraform/environments/dev/03-services/main.tf` | `frontend_domain` 도메인 교정 |

## 클러스터 직접 변경 사항 (소스 미반영)

| 대상 | 변경 내용 |
|------|-----------|
| 모든 AI worker SA annotation | IRSA ARN Account ID 교정 (envsubst 재적용으로 소스와 동기화) |
| `utterai-cpu-worker` deployment | 이미지 순차 업데이트 (dev-b24c8b0 → 최신) |
| `utterai-batch-worker` deployment | 이미지 순차 업데이트 (dev-b24c8b0 → 최신) |
| `utterai-ml-gpu-worker` deployment | 이미지 `dev-8e4da75` → `dev-a730163` (PATH fix 포함) |
| IAM `frontend-deploy-role` | `cloudfront:CreateInvalidation` 인라인 정책 추가 |
