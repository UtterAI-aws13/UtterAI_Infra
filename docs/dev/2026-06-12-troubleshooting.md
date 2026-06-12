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

`k8s/rbac/serviceaccounts.yaml`에 IRSA ARN이 `${AWS_ACCOUNT_ID}` 플레이스홀더로 정의되어 있으나, 배포 시 `envsubst` 없이 `kubectl apply -f`를 직접 실행해 Account ID가 빈 문자열로 치환됨.

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
for f in k8s/rbac/*.yaml; do
  envsubst < "$f" | kubectl apply -f -
done
```

### 재발 방지

`k8s/rbac/*.yaml`은 반드시 `scripts/k8s-deploy.sh`를 통해 배포할 것. 스크립트 내부에서 `aws sts get-caller-identity`로 Account ID를 자동 주입한 뒤 `envsubst`를 실행함.

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

`k8s-demo/apps/ai-worker/base/ai-api-deployment.yaml`의 readiness/liveness probe 경로가 `/health`로 설정되어 있었으나 실제 FastAPI 앱의 헬스체크 엔드포인트는 `/health/ready`와 `/health/live`.

```yaml
# 수정 전
readinessProbe:
  httpGet:
    path: /health      # 404
livenessProbe:
  httpGet:
    path: /health      # 404
```

### 진단 명령어

```bash
# 실행 중인 컨테이너에서 엔드포인트 직접 테스트
kubectl exec -n utterai-ai-api deployment/utterai-ai-api -- \
  python3 -c "import urllib.request; \
  print(urllib.request.urlopen('http://localhost:8080/health/ready').getcode())"
# 결과: 200
```

### 해결

**수정 파일**: `k8s-demo/apps/ai-worker/base/ai-api-deployment.yaml`

```yaml
# 수정 후
readinessProbe:
  httpGet:
    path: /health/ready
livenessProbe:
  httpGet:
    path: /health/live
```

클러스터에도 즉시 적용:

```bash
kubectl patch deployment -n utterai-ai-api utterai-ai-api --type='json' \
  -p='[
    {"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/httpGet/path","value":"/health/ready"},
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

## 4. GPU 노드 disk-pressure — `disk_size` 파라미터 무효

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

### 해결

**수정 파일**: `terraform/modules/eks/main.tf`

`aws_launch_template.gpu` 리소스를 추가하고 node group에서 참조:

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
  # ... 기존 설정 ...

  # disk_size 제거, launch_template으로 대체
  launch_template {
    id      = aws_launch_template.gpu.id
    version = aws_launch_template.gpu.latest_version
  }
}
```

> **주의**: launch template 추가는 node group을 **destroy & recreate**함. GPU 노드가 잠시 내려간 후 새 노드가 100GB 디스크로 프로비저닝됨.

```bash
terraform apply \
  -target=module.eks.aws_launch_template.gpu \
  -target=module.eks.aws_eks_node_group.gpu
```

---

## 5. GPU worker — `ModuleNotFoundError: boto3` (미해결)

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

GPU 이미지(`utterai-ai-gpu:dev-e24a4bd`, 2026-06-11 빌드)에 boto3가 설치되지 않은 채로 빌드됨. 현재 AI 레포 `requirements.txt`에는 boto3가 포함되어 있으나, 이 이미지는 boto3 추가 이전 커밋 기준으로 빌드된 것.

추가로 AI 레포 조사 결과:
- 현재 `app/workers/ml_gpu_worker.py` **파일 자체가 없음** (`analysis_worker.py`로 변경된 것으로 추정)
- `analysis_worker.py`의 `start_worker()`가 `NotImplementedError` 상태 — **GPU worker 미구현**
- `if __name__ == "__main__"` 블록도 없음

### 현황 및 필요 조치

| 항목 | 상태 |
|------|------|
| GPU 이미지 boto3 | ❌ 미포함 (이미지 재빌드 필요) |
| `ml_gpu_worker.py` | ❌ 현 AI 레포에 없음 |
| GPU worker 구현 | ❌ `NotImplementedError` |
| 배포 커맨드 일치 여부 | ❌ infra에는 `python -m app.workers.ml_gpu_worker`이나 AI 레포에 해당 모듈 없음 |

**AI 팀 조치 필요**:
1. GPU worker 구현 완성 (`start_worker()` 로직 작성)
2. `if __name__ == "__main__": start_worker()` 추가
3. GPU 이미지 재빌드 (boto3 포함 확인)
4. 실제 모듈 경로 확정 후 infra deployment 커맨드 업데이트

---

## 변경된 파일 요약

| 파일 | 변경 내용 |
|------|-----------|
| `k8s-demo/apps/ai-worker/base/ai-api-deployment.yaml` | readiness probe `/health` → `/health/ready`, liveness probe `/health` → `/health/live` |
| `terraform/modules/eks/main.tf` | `aws_launch_template.gpu` 추가 + node group에 launch template 연결, `disk_size` 파라미터 제거 |

## 클러스터 직접 변경 사항 (소스 미반영)

| 대상 | 변경 내용 |
|------|-----------|
| 모든 AI worker SA annotation | IRSA ARN Account ID 교정 (`envsubst` 재적용으로 소스와 동기화됨) |
| `utterai-cpu-worker` deployment | 이미지 `dev-4f1e5d4` → `dev-b24c8b0` |
| `utterai-batch-worker` deployment | 이미지 `dev-4f1e5d4` → `dev-b24c8b0` |
