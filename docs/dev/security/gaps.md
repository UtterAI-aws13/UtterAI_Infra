# UtterAI Dev 환경 — 보안 미비점 상세 분석

> 작성일: 2026-06-11
> 범위: EKS 클러스터 구성 시점 보안 점검 (Manifest 주입 포함)

기존 `security-overview.md`·`security-hardening.md`에서 다루지 않은 항목을 중심으로 정리한다.
각 항목은 **위험도**, **현재 상태**, **해결 방향** 순으로 기술한다.

---

## 목차

1. [envsubst Manifest 주입 보안 위험](#1-envsubst-manifest-주입-보안-위험)
2. [k8s-legacy/ 기본 매니페스트 securityContext 누락](#2-k8s-기본-매니페스트-securitycontext-누락)
3. [Kubernetes Secrets KMS 봉투 암호화 미설정](#3-kubernetes-secrets-kms-봉투-암호화-미설정)
4. [ClusterSecretStore 네임스페이스 제한 없음](#4-clustersecretstore-네임스페이스-제한-없음)
5. [Namespace PodSecurityAdmission 레이블 없음](#5-namespace-podsecurityadmission-레이블-없음)
6. [이미지 Digest 고정 없음 (Mutable Tag)](#6-이미지-digest-고정-없음-mutable-tag)
7. [PodDisruptionBudget 없음](#7-poddisruptionbudget-없음)
8. [ArgoCD 기본 admin 자격증명 유지](#8-argocd-기본-admin-자격증명-유지)
9. [EKS API Public Endpoint CIDR 제한 없음](#9-eks-api-public-endpoint-cidr-제한-없음)

---

## 1. envsubst Manifest 주입 보안 위험

**파일**: `scripts/k8s-deploy-legacy.sh`
**위험도**: 높음

### 현재 구조

```bash
apply() {
  envsubst < "$1" | kubectl apply -f -
}
```

`envsubst`가 YAML 파일 내 `$VAR` 패턴을 전부 치환한 뒤, 결과를 바로 `kubectl apply`로 파이프한다.

### 문제점 3가지

#### (1) 변수 범위 미지정 → 의도치 않은 치환

`envsubst`를 인자 없이 호출하면 현재 셸 환경의 **모든 변수**를 치환한다.
YAML 안에 `$HOME`, `$PATH`, `$USER` 같은 문자열이 있으면 호스트 값으로 교체된다.
컨테이너 커맨드·스크립트 인라인 예제에 `$var` 형태가 있으면 조용히 깨진다.

```bash
# 위험: 전체 환경 변수 치환
envsubst < manifest.yaml | kubectl apply -f -

# 안전: 치환할 변수 명시적 지정
envsubst '${AWS_ACCOUNT_ID} ${BACKEND_TAG} ${AI_CPU_TAG} ${AI_GPU_TAG} ${RDS_ENDPOINT} ${REDIS_ENDPOINT} ${ACM_CERTIFICATE_ARN}' \
  < manifest.yaml | kubectl apply -f -
```

#### (2) 빈 변수 허용 → 잘못된 매니페스트 적용

`latest_tag()` 함수가 ECR에서 태그를 못 찾으면 빈 문자열을 반환한다.
`${BACKEND_TAG}`가 비어있으면 이미지 주소가 `...amazonaws.com/utterai-backend:` 로 주입되어
`ImagePullBackOff`가 아닌 예기치 않은 이미지(레지스트리 기본값)를 참조할 수 있다.

```bash
# 현재: 빈 값 허용
export BACKEND_TAG=$(latest_tag "utterai-backend")  # ECR 오류 시 빈 문자열

# 개선: 빈 값이면 즉시 중단
export BACKEND_TAG=$(latest_tag "utterai-backend")
[[ -z "$BACKEND_TAG" ]] && { echo "ERROR: BACKEND_TAG 조회 실패"; exit 1; }
```

#### (3) dry-run 없이 즉시 apply → 검증 기회 없음

치환된 YAML을 서버에 보내기 전 유효성 검사가 없다.
스키마 오류·네임스페이스 충돌을 사전에 잡지 못한다.

```bash
# 개선: server-side dry-run 후 apply
envsubst '...' < "$1" | kubectl apply --dry-run=server -f - \
  && envsubst '...' < "$1" | kubectl apply -f -
```

### 권장 개선 순서

1. `envsubst` 호출에 변수 목록 명시
2. 각 exported 변수에 빈 값 가드(`[[ -z ]]`) 추가
3. `--dry-run=server` 스텝을 실제 apply 전에 추가

---

## 2. k8s-legacy/ 기본 매니페스트 securityContext 누락

**파일**: `k8s-legacy/workloads/*.yaml`
**위험도**: 중간 (Dev 허용 범위이나 점진 적용 권장)

`security-hardening.md` #3 에서 Prod overlay(`k8s/`) 에 securityContext를 추가했으나,
**실제 배포에 사용되는 `k8s-legacy/workloads/` 기본 매니페스트에는 전혀 적용되지 않았다.**

현재 `k8s-legacy/workloads/` 내 모든 Deployment에 아래 항목이 없다:

| 설정 | 기본 동작 | 위험 |
|------|----------|------|
| `runAsNonRoot` | 컨테이너가 root(UID 0)로 실행 가능 | 컨테이너 탈출 시 노드 root 접근 |
| `allowPrivilegeEscalation: false` | setuid/setgid 바이너리로 권한 상승 가능 | 공격자가 프로세스 권한 확대 |
| `capabilities.drop: ["ALL"]` | 기본 Linux capability 전체 보유 | 네트워크·파일 조작 범위 넓어짐 |
| `readOnlyRootFilesystem` | 컨테이너 파일시스템 쓰기 허용 | 악성 파일 생성·persistence |

### 적용 가이드

```yaml
# k8s-legacy/workloads/api-deployment.yaml — containers 하위에 추가
containers:
  - name: api
    securityContext:
      allowPrivilegeEscalation: false
      runAsNonRoot: true
      runAsUser: 1000
      capabilities:
        drop: ["ALL"]
      readOnlyRootFilesystem: true  # /tmp, /var 등 필요 시 emptyDir 마운트 별도 설정
```

> **예외**: `cpu-worker`, `ml-gpu-worker`는 `HF_HOME: /tmp/huggingface`를 사용하므로
> `readOnlyRootFilesystem: true` 적용 시 `/tmp`를 emptyDir로 마운트해야 한다.

```yaml
# cpu/gpu-worker에 추가할 volume 설정
volumes:
  - name: hf-cache
    emptyDir: {}
volumeMounts:
  - name: hf-cache
    mountPath: /tmp/huggingface
```

---

## 3. Kubernetes Secrets KMS 봉투 암호화 미설정

**파일**: `terraform/modules/eks/main.tf`
**위험도**: 중간

EKS 클러스터의 etcd에 저장되는 Kubernetes Secret은 기본적으로 AWS 관리형 키로만 암호화된다.
커스텀 KMS CMK를 사용한 **봉투 암호화(envelope encryption)**가 미설정된 상태다.

### 현재 상태

```hcl
resource "aws_eks_cluster" "this" {
  # encryption_config 블록 없음
}
```

etcd 데이터는 AWS 기본 암호화만 적용됨. AWS 계정 레벨 침해 시 Secret 내용 접근 가능.

### 개선

```hcl
# terraform/modules/eks/main.tf에 추가
resource "aws_kms_key" "eks_secrets" {
  description             = "${local.prefix} EKS Secrets Encryption"
  deletion_window_in_days = 7
  enable_key_rotation     = true
}

resource "aws_eks_cluster" "this" {
  ...
  encryption_config {
    resources = ["secrets"]
    provider {
      key_arn = aws_kms_key.eks_secrets.arn
    }
  }
}
```

> **주의**: 기존 클러스터에 적용 시 Secret 재암호화 작업이 발생함. 신규 클러스터에는 생성 시 함께 적용 권장.

---

## 4. ClusterSecretStore 네임스페이스 제한 없음

**파일**: `k8s-legacy/secrets/cluster-secret-store.yaml`
**위험도**: 중간

현재 `ClusterSecretStore`는 인증 섹션(`auth`) 없이 ESO Pod의 IRSA에 전적으로 의존한다.
`ClusterSecretStore`는 클러스터 전체 네임스페이스에서 참조 가능하므로,
악의적 사용자가 새 네임스페이스에 `ExternalSecret`을 생성하면 `utterai-dev/*` 범위의
Secrets Manager 시크릿 전체를 꺼낼 수 있다.

### 현재 상태

```yaml
# cluster-secret-store.yaml — auth 없음
spec:
  provider:
    aws:
      service: SecretsManager
      region: ap-northeast-2
      # auth 블록 없음 → ESO Pod의 IRSA 전체 권한 위임
```

### 개선 방향 (우선순위 낮음)

ClusterSecretStore 대신 **네임스페이스별 SecretStore**로 분리한다.
각 워크로드 네임스페이스에 독립적인 SecretStore를 두면, 한 네임스페이스가
다른 네임스페이스의 시크릿을 참조하는 구조가 근본적으로 차단된다.

```yaml
# utterai-api 네임스페이스 전용 SecretStore
apiVersion: external-secrets.io/v1beta1
kind: SecretStore
metadata:
  name: aws-secrets-manager
  namespace: utterai-api
spec:
  provider:
    aws:
      service: SecretsManager
      region: ap-northeast-2
      auth:
        jwt:
          serviceAccountRef:
            name: utterai-api-sa   # 해당 SA의 IRSA만 사용
```

> Dev 현시점은 IRSA 정책 범위(`utterai-dev/*`)로 1차 제어 중이므로 허용.
> Prod에서는 SecretStore 분리 + IRSA 범위 최소화를 강하게 권장.

---

## 5. Namespace PodSecurityAdmission 레이블 없음

**파일**: `k8s-legacy/namespaces/namespaces.yaml`
**위험도**: 중간

Kubernetes 1.25+의 내장 **PodSecurity Admission(PSA)**을 사용하면 네임스페이스 레벨에서
Pod 보안 기준(`privileged` / `baseline` / `restricted`)을 강제할 수 있다.
현재 모든 네임스페이스에 PSA 레이블이 없어 어떤 Pod도 보안 기준 검사를 받지 않는다.

### 현재 상태

```yaml
# 현재: PSA 레이블 없음
apiVersion: v1
kind: Namespace
metadata:
  name: utterai-api
  labels:
    app.kubernetes.io/part-of: utterai
```

### 개선 — 단계적 적용

```yaml
# 1단계: warn 모드 (거부하지 않고 경고만)
apiVersion: v1
kind: Namespace
metadata:
  name: utterai-api
  labels:
    app.kubernetes.io/part-of: utterai
    pod-security.kubernetes.io/warn: baseline
    pod-security.kubernetes.io/warn-version: latest

# 2단계: enforce 모드 (위반 Pod 생성 거부)
    pod-security.kubernetes.io/enforce: baseline
    pod-security.kubernetes.io/enforce-version: latest
```

| 네임스페이스 | 권장 수준 |
|-------------|----------|
| utterai-api | baseline |
| utterai-ai-cpu | baseline |
| utterai-ai-gpu | baseline (GPU 특성상 restricted 어려움) |
| utterai-batch | baseline |
| kube-system | privileged (시스템 컴포넌트) |
| ingress-system | baseline |
| external-secrets | baseline |

---

## 6. 이미지 Digest 고정 없음 (Mutable Tag)

**파일**: `scripts/k8s-deploy-legacy.sh`, `k8s-legacy/workloads/*.yaml`
**위험도**: 중간

`latest_tag()`는 ECR에서 **가장 최근에 push된 이미지 태그**를 가져온다.
태그는 mutable(덮어쓰기 가능)하므로, 동일 태그로 악성 이미지가 push되면
다음 배포 시 자동으로 악성 버전이 적용된다.

### 현재 동작

```bash
# 태그 기반 조회 — 태그는 언제든 다른 이미지를 가리킬 수 있음
latest_tag() {
  aws ecr describe-images ... \
    --query 'sort_by(imageDetails, &imagePushedAt)[-1].imageTags[0]'
}
```

### 개선 — SHA256 Digest 사용

```bash
# 태그 대신 immutable digest 조회
latest_digest() {
  local repo=$1
  aws ecr describe-images \
    --repository-name "$repo" \
    --region "$AWS_REGION" \
    --query 'sort_by(imageDetails, &imagePushedAt)[-1].imageDigest' \
    --output text
}

export BACKEND_DIGEST=$(latest_digest "utterai-backend")
# 이미지 주소: image@sha256:abc123...
```

```yaml
# Deployment에서 태그 대신 digest 지정
image: 123456789.dkr.ecr.ap-northeast-2.amazonaws.com/utterai-backend@sha256:abc123...
```

> ECR `imageTagMutability`를 `IMMUTABLE`로 설정해 태그 덮어쓰기 자체를 차단하는 것도 병행 권장.

---

## 7. PodDisruptionBudget 없음

**파일**: `k8s-legacy/workloads/`
**위험도**: 낮음

노드 드레인(업그레이드, Spot 회수) 시 `PodDisruptionBudget(PDB)`이 없으면
kubectl이 Deployment의 모든 Pod를 동시에 제거할 수 있다.
`utterai-api` 처럼 외부 트래픽을 받는 서비스는 순간적으로 0개가 될 수 있다.

### 개선

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: utterai-api-pdb
  namespace: utterai-api
spec:
  minAvailable: 1   # 드레인 중에도 최소 1개 Pod 유지
  selector:
    matchLabels:
      app: utterai-api
```

적용 대상: `utterai-api` (외부 트래픽), `utterai-ai-api` (내부 트래픽)
워커류(`cpu`, `gpu`, `batch`)는 SQS 기반 비동기 처리이므로 PDB 우선순위 낮음.

---

## 8. ArgoCD 기본 admin 자격증명 유지

**파일**: `terraform/modules/eks-addons/main.tf`
**위험도**: 중간

ArgoCD는 Helm 기본값으로 배포될 경우 `admin` 계정 비밀번호가
**Pod 이름 기반 자동 생성값**으로 초기화된다.
별도 Secret 주입이나 SSO 설정이 없으면 초기 비밀번호가 영구적으로 유지될 수 있다.

### 현재 상태

```hcl
resource "helm_release" "argocd" {
  # admin 비밀번호, SSO, OIDC 설정 없음
  values = [yamlencode({
    server.service.type = "ClusterIP"
  })]
}
```

### 개선 방향

```hcl
values = [yamlencode({
  configs = {
    secret = {
      # Secrets Manager에서 bcrypt 해시된 비밀번호 주입
      argocdServerAdminPassword = var.argocd_admin_password_bcrypt
    }
    params = {
      "server.insecure" = false
    }
  }
  server = {
    service = { type = "ClusterIP" }
  }
})]
```

> 장기적으로는 Cognito/OIDC를 ArgoCD SSO로 연결해 admin 계정을 비활성화하는 방향 권장.

---

## 9. EKS API Public Endpoint CIDR 제한 없음

**파일**: `terraform/modules/eks/main.tf`
**위험도**: 중간 (security-overview.md §13에도 언급, 상세 보완)

`endpoint_public_access = true` 이지만 `public_access_cidrs`를 지정하지 않아
전 세계 어디서나 EKS API 서버에 접근 시도할 수 있다.
인증이 필요하므로 즉각적 침해는 어렵지만, brute-force 또는 취약한 kubeconfig 유출 시 위험하다.

### 현재 상태

```hcl
vpc_config {
  endpoint_private_access = true
  endpoint_public_access  = true
  # public_access_cidrs 미설정 → 0.0.0.0/0 허용
}
```

### 개선

```hcl
vpc_config {
  endpoint_private_access = true
  endpoint_public_access  = true
  public_access_cidrs     = var.allowed_cidr_blocks  # 팀 IP, VPN CIDR 목록
}
```

```hcl
# terraform/environments/dev/02-eks/variables.tf
variable "allowed_cidr_blocks" {
  type    = list(string)
  default = ["X.X.X.X/32"]  # 팀 고정 IP 또는 VPN 출구 IP
}
```

---

## 요약 — 우선순위 정리

| # | 항목 | 위험도 | 공수 | Prod 전 필수 |
|---|------|--------|------|-------------|
| 1 | envsubst 변수 범위 명시 + 빈값 가드 | 높음 | 낮음 (스크립트 수정) | ✅ |
| 2 | k8s-legacy/ 매니페스트 securityContext 추가 | 중간 | 낮음 (YAML 추가) | ✅ |
| 3 | EKS Public Endpoint CIDR 제한 | 중간 | 낮음 (변수 추가) | ✅ |
| 4 | Kubernetes Secrets KMS 암호화 | 중간 | 중간 (클러스터 재구성) | 권장 |
| 5 | Namespace PSA 레이블 | 중간 | 낮음 (YAML 레이블 추가) | 권장 |
| 6 | ArgoCD admin 비밀번호 관리 | 중간 | 중간 (Secret 연동) | 권장 |
| 7 | 이미지 Digest 고정 | 중간 | 중간 (스크립트+YAML 수정) | 선택 |
| 8 | ClusterSecretStore 분리 | 낮음 | 높음 (구조 변경) | Prod 권장 |
| 9 | PodDisruptionBudget | 낮음 | 낮음 (YAML 추가) | 선택 |

---

## 관련 문서

- [보안 전체 현황](./overview.md)
- [보안 수정 이력](./hardening.md)
- [Dev 환경 배포 가이드](../README.md)
