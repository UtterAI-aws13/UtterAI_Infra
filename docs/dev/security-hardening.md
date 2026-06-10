# UtterAI Dev 환경 — 보안 하드닝 현황

> 최종 업데이트: 2026-06-10

---

## 개요

Dev 환경 코드 리뷰에서 식별된 보안 취약점과 그에 대한 수정 내역을 정리한다.  
각 항목은 **완료 / 미완료(Prod 전 필수) / 허용** 세 가지 상태로 분류한다.

---

## 완료된 수정 사항

### 1. EKS Node Security Group — 과도한 `0.0.0.0/0` ingress 제거

**파일**: `terraform/modules/eks/main.tf`

**문제**  
노드 SG의 포트 443(HTTPS), 10250(kubelet API) ingress가 `0.0.0.0/0`으로 열려 있었다.  
노드가 private subnet에 있어 실제 외부 노출은 없으나, subnet 설정 변경 시 즉시 노출되는 구조였다.

**수정**  
`cidr_blocks = ["0.0.0.0/0"]` → `security_groups = [aws_eks_cluster.this.vpc_config[0].cluster_security_group_id]`  
EKS 컨트롤 플레인 SG에서만 허용하도록 제한했다.

```hcl
# 수정 전
ingress {
  from_port   = 443
  protocol    = "tcp"
  cidr_blocks = ["0.0.0.0/0"]
}

# 수정 후
ingress {
  from_port       = 443
  protocol        = "tcp"
  security_groups = [aws_eks_cluster.this.vpc_config[0].cluster_security_group_id]
}
```

---

### 2. RDS 보호 설정 variable화

**파일**: `terraform/modules/rds/variables.tf`, `terraform/modules/rds/main.tf`, `terraform/environments/dev/03-services/main.tf`

**문제**  
`skip_final_snapshot = true`, `deletion_protection = false`가 모듈 내에 하드코딩되어 있었다.  
Prod 모듈을 새로 만들 때 동일 모듈을 재사용하면 실수로 보호 설정이 꺼진 채 배포될 수 있었다.

**수정**  
두 값을 variable로 분리하고, dev 호출부에서 명시적으로 주입한다.  
Prod 모듈은 `skip_final_snapshot = false`, `deletion_protection = true`로 오버라이드한다.

```hcl
# terraform/modules/rds/variables.tf (추가)
variable "skip_final_snapshot" {
  type    = bool
  default = true
}
variable "deletion_protection" {
  type    = bool
  default = false
}

# terraform/environments/dev/03-services/main.tf (명시화)
module "rds" {
  ...
  skip_final_snapshot = true   # dev: 편의 우선
  deletion_protection  = false
}
```

---

### 3. Pod SecurityContext 하드닝 — Prod overlay 적용

**파일**:  
- `k8s-demo/apps/backend/overlays/prod/patch-deployment.yaml`  
- `k8s-demo/apps/ai-worker/overlays/prod/patch-deployment.yaml`

**문제**  
모든 workload에 `securityContext`가 전혀 없었다. 컨테이너가 root로 실행되거나 커널 권한을 획득할 수 있는 상태였다.

**수정**  
Prod overlay에 아래 securityContext를 추가했다. Dev base에는 적용하지 않아 개발 편의성을 유지했다.

```yaml
spec:
  template:
    spec:
      securityContext:
        runAsNonRoot: true          # root 실행 금지
      containers:
        - name: <container-name>
          securityContext:
            allowPrivilegeEscalation: false   # setuid/setgid 실행 방지
            capabilities:
              drop: ["ALL"]                   # Linux capability 전체 제거
```

적용 대상 workload: `backend`, `ai-api`, `cpu-worker`, `ml-gpu-worker`, `llm-gpu-worker`

---

## 미완료 — Prod 배포 전 필수

### 4. ALB HTTPS — ACM 인증서 ARN 주입

**파일**:  
- `k8s-demo/apps/backend/overlays/dev/patch-ingress.yaml`  
- `k8s-demo/apps/backend/overlays/prod/patch-ingress.yaml`

**상태**: 구조는 완성 (HTTP→HTTPS redirect, `ssl-redirect: "443"` 포함). ARN만 채우면 됨.

```yaml
# TODO 자리에 실제 ARN으로 교체
alb.ingress.kubernetes.io/certificate-arn: arn:aws:acm:ap-northeast-2:<ACCOUNT_ID>:certificate/<ARN>
```

ACM 인증서 발급 방법은 `docs/dev/README.md` 5.3절 참고.

### 5. Redis — `aws_elasticache_replication_group`으로 교체

**파일**: `terraform/modules/redis/main.tf`

**상태**: 현재 `aws_elasticache_cluster` 사용 중. 이 리소스 타입은 `transit_encryption_enabled`를 지원하지 않음.  
TLS 활성화를 위해 `aws_elasticache_replication_group`으로 리소스 타입을 교체해야 한다.  
교체 시 기존 리소스 destroy → recreate 필요. Dev 데이터 손실에 주의.

```hcl
# 변경 대상
resource "aws_elasticache_replication_group" "this" {
  ...
  transit_encryption_enabled = var.transit_encryption_enabled
  at_rest_encryption_enabled = var.at_rest_encryption_enabled
}
```

---

## 허용 (Dev 환경 기준)

| 항목 | 허용 이유 |
|------|-----------|
| **NetworkPolicy 없음** | VPC/SG 격리로 보완. Prod에서 namespace 간 트래픽 제어 시 추가 예정 |
| **EKS API public endpoint** | `endpoint_private_access = true`도 함께 설정. Dev 운영 편의상 허용 범위 |
| **RDS `deletion_protection = false`** | Dev 환경 민첩성 우선. Prod에서는 `true` |
| **Pod SecurityContext (Dev base 미적용)** | 개발 환경에서 이미지 루트 실행 가능성 고려. Prod overlay에만 적용 |

---

## 관련 문서

- [Dev 환경 적용 가이드](./README.md)
- [인프라 환경 개요](../README.md)
- EKS 아키텍처 상세: [`docs/eks-architecture-flow.md`](../eks-architecture-flow.md)
