# Prod 전환 체크리스트 — Terraform & K8s 매니페스트

> 작성일: 2026-06-11
> Dev → Prod 전환 시 추가·변경이 필요한 항목을 레이어별로 정리한다.
> Dev 코드를 기준으로 **무엇이 없고, 무엇이 달라져야 하는지**에 집중한다.

---

## 목차

1. [Terraform — 신규 모듈](#1-terraform--신규-모듈)
2. [Terraform — 기존 모듈 변경 파라미터](#2-terraform--기존-모듈-변경-파라미터)
3. [Terraform — environments/prod 레이어 구성](#3-terraform--environmentsprod-레이어-구성)
4. [K8s 매니페스트 — 주입 방식 변경](#4-k8s-매니페스트--주입-방식-변경)
5. [K8s 매니페스트 — 파일별 변경 내용](#5-k8s-매니페스트--파일별-변경-내용)
6. [K8s 매니페스트 — 신규 추가 파일](#6-k8s-매니페스트--신규-추가-파일)
7. [전환 순서](#7-전환-순서)

---

## 1. Terraform — 신규 모듈

Dev에 없어서 Prod에서 새로 작성해야 하는 모듈들이다.

### 1-A. `terraform/modules/kms/`

Prod는 서비스별 CMK를 별도로 사용한다. Dev는 AWS 관리형 키만 사용 중.

```hcl
# modules/kms/main.tf
resource "aws_kms_key" "this" {
  description             = "${var.alias} encryption key"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "Enable IAM User Permissions"
        Effect = "Allow"
        Principal = { AWS = "arn:aws:iam::${var.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid    = "Allow service use"
        Effect = "Allow"
        Principal = { Service = var.allowed_services }  # ["rds.amazonaws.com", "s3.amazonaws.com" 등]
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = "*"
      }
    ]
  })
}

resource "aws_kms_alias" "this" {
  name          = "alias/${var.alias}"
  target_key_id = aws_kms_key.this.key_id
}
```

호출 시 서비스별로 인스턴스화:

```hcl
# environments/prod/01-network/main.tf
module "kms_rds"     { source = "../../../modules/kms"; alias = "prod-aurora-kms-key" }
module "kms_redis"   { source = "../../../modules/kms"; alias = "prod-redis-kms-key" }
module "kms_s3"      { source = "../../../modules/kms"; alias = "prod-s3-kms-key" }
module "kms_sqs"     { source = "../../../modules/kms"; alias = "prod-sqs-kms-key" }
module "kms_secrets" { source = "../../../modules/kms"; alias = "prod-secrets-kms-key" }
module "kms_eks"     { source = "../../../modules/kms"; alias = "prod-eks-secrets-kms-key" }
```

---

### 1-B. `terraform/modules/waf/`

Dev에는 WAF가 없다. Prod ALB 앞에 WAF를 연결해야 한다.

```hcl
# modules/waf/main.tf
resource "aws_wafv2_web_acl" "this" {
  name  = "${var.prefix}-waf"
  scope = "REGIONAL"   # ALB 연결 시 REGIONAL

  default_action { allow {} }

  rule {
    name     = "AWSManagedRulesCommonRuleSet"
    priority = 1
    override_action { none {} }
    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesCommonRuleSet"
        vendor_name = "AWS"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "CommonRuleSet"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "RateLimitRule"
    priority = 2
    action { block {} }
    statement {
      rate_based_statement {
        limit              = 2000   # IP당 5분 2000 req
        aggregate_key_type = "IP"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "RateLimit"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${var.prefix}-waf"
    sampled_requests_enabled   = true
  }
}

# ALB에 WAF 연결
resource "aws_wafv2_web_acl_association" "this" {
  resource_arn = var.alb_arn
  web_acl_arn  = aws_wafv2_web_acl.this.arn
}
```

---

### 1-C. `terraform/modules/karpenter/`

Dev는 Cluster Autoscaler를 사용하지만 Prod는 **Karpenter + KEDA** 구조다.
Karpenter 설치 자체는 Helm으로, NodePool/EC2NodeClass는 별도 K8s 매니페스트로 관리한다.

```hcl
# modules/karpenter/main.tf

# Karpenter Controller IRSA
resource "aws_iam_role" "karpenter_controller" {
  name = "${var.prefix}-karpenter-controller-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = var.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${var.oidc_provider_url}:sub" = "system:serviceaccount:karpenter:karpenter"
          "${var.oidc_provider_url}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "karpenter_controller" {
  role       = aws_iam_role.karpenter_controller.name
  policy_arn = aws_iam_policy.karpenter_controller.arn
}

resource "aws_iam_policy" "karpenter_controller" {
  name   = "${var.prefix}-karpenter-controller-policy"
  policy = file("${path.module}/policies/karpenter-controller-policy.json")
  # EC2:CreateLaunchTemplate, EC2:RunInstances, EC2:TerminateInstances 등 포함
}

# Karpenter Node IAM Role (Karpenter가 프로비저닝한 노드에 부여)
resource "aws_iam_role" "karpenter_node" {
  name = "${var.prefix}-karpenter-node-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "karpenter_node_worker"   { policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy" ... }
resource "aws_iam_role_policy_attachment" "karpenter_node_cni"      { policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy" ... }
resource "aws_iam_role_policy_attachment" "karpenter_node_ecr"      { policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly" ... }
resource "aws_iam_role_policy_attachment" "karpenter_node_ssm"      { policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore" ... }

resource "aws_iam_instance_profile" "karpenter_node" {
  name = "${var.prefix}-karpenter-node-profile"
  role = aws_iam_role.karpenter_node.name
}

# Helm으로 Karpenter 설치
resource "helm_release" "karpenter" {
  name       = "karpenter"
  repository = "oci://public.ecr.aws/karpenter"
  chart      = "karpenter"
  version    = "1.0.0"
  namespace  = "karpenter"
  create_namespace = true

  set { name = "settings.clusterName";     value = var.cluster_name }
  set { name = "settings.interruptionQueue"; value = var.cluster_name }
  set { name = "serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn"
        value = aws_iam_role.karpenter_controller.arn }

  # system 노드에 배치
  set { name = "tolerations[0].key";    value = "CriticalAddonsOnly" }
  set { name = "tolerations[0].operator"; value = "Exists" }
}
```

---

### 1-D. `terraform/modules/keda/`

Dev는 HPA(CPU 메트릭)만 사용하지만, Prod는 KEDA로 SQS 큐 깊이 기반 스케일링을 추가한다.

```hcl
# modules/keda/main.tf

# KEDA Controller IRSA
resource "aws_iam_role" "keda" {
  name = "${var.prefix}-keda-irsa-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = var.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${var.oidc_provider_url}:sub" = "system:serviceaccount:keda:keda-operator"
          "${var.oidc_provider_url}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "keda" {
  role = aws_iam_role.keda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "sqs:GetQueueAttributes",
        "sqs:GetQueueUrl",
        "cloudwatch:GetMetricData",
      ]
      Resource = var.queue_arns
    }]
  })
}

resource "helm_release" "keda" {
  name       = "keda"
  repository = "https://kedacore.github.io/charts"
  chart      = "keda"
  version    = "2.15.0"
  namespace  = "keda"
  create_namespace = true

  set { name = "serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn"
        value = aws_iam_role.keda.arn }
}
```

---

## 2. Terraform — 기존 모듈 변경 파라미터

동일한 모듈을 쓰되 **값이 달라지는 항목**들이다.

### 2-A. `modules/eks` — EKS 클러스터

| 파라미터 | Dev 값 | Prod 값 | 이유 |
|---------|--------|---------|------|
| `endpoint_public_access` | `true` | `false` | 외부 kubectl 차단, VPN 경유 |
| `public_access_cidrs` | 없음 | 해당 없음 (public=false) | |
| `encryption_config` | 없음 | KMS CMK 추가 | etcd Secret 봉투 암호화 |
| `subnet_ids` AZ 수 | 2개 | 3개 | Multi-AZ HA |
| system node 타입 | `t3.small` | `t3.large` | 시스템 컴포넌트 안정성 |
| api node 타입 | `t3.medium` | `t3.xlarge` | 트래픽 처리 용량 |
| cpu worker 타입 | `t3.large` | `c5.2xlarge` | AI 처리 성능 |
| gpu worker 타입 | `g4dn.xlarge` | `g4dn.xlarge` ~ `g4dn.2xlarge` | |
| node autoscaler | Cluster Autoscaler | **Karpenter** | 동적 노드 프로비저닝 |

```hcl
# environments/prod/02-eks/main.tf 추가 필요
resource "aws_eks_cluster" "this" {
  ...
  encryption_config {           # Dev에 없는 블록
    resources = ["secrets"]
    provider {
      key_arn = module.kms_eks.key_arn
    }
  }
  vpc_config {
    endpoint_private_access = true
    endpoint_public_access  = false   # Dev는 true
    subnet_ids              = var.private_app_subnet_ids
  }
}
```

---

### 2-B. `modules/rds` → Aurora로 교체

Dev는 RDS Single Instance(`aws_db_instance`)를 사용하지만, Prod는 **Aurora PostgreSQL Cluster**(`aws_rds_cluster`)로 바꿔야 한다.
`modules/aurora`가 이미 존재하므로 해당 모듈을 호출한다.

| 파라미터 | Dev (rds) | Prod (aurora) |
|---------|-----------|---------------|
| 리소스 타입 | `aws_db_instance` | `aws_rds_cluster` + `aws_rds_cluster_instance` |
| 인스턴스 클래스 | `db.t3.medium` | `db.r6g.large` (Writer + Reader) |
| Multi-AZ | 없음 | Writer 1 + Reader 1 |
| `skip_final_snapshot` | `true` | **`false`** |
| `deletion_protection` | `false` | **`true`** |
| KMS 암호화 | 없음 | `kms_key_id = module.kms_rds.key_arn` |
| Performance Insights | 없음 | 활성화 |
| `backup_retention_period` | 없음 | `7` |
| 자동 마이너 업그레이드 | 없음 | **비활성화** (`auto_minor_version_upgrade = false`) |

---

### 2-C. `modules/redis` — Replication Group 교체

Dev는 `aws_elasticache_cluster`(암호화 미지원)를 사용한다.
Prod는 `aws_elasticache_replication_group`으로 교체해야 TLS/Auth가 활성화된다.

| 파라미터 | Dev | Prod |
|---------|-----|------|
| 리소스 타입 | `aws_elasticache_cluster` | `aws_elasticache_replication_group` |
| 노드 타입 | `cache.t3.micro` | `cache.r6g.large` |
| 복제본 수 | 0 | `num_cache_clusters = 2` (Primary + Replica) |
| `transit_encryption_enabled` | 없음 | **`true`** |
| `at_rest_encryption_enabled` | 없음 | **`true`** |
| `auth_token` | 없음 | Secrets Manager에서 주입 |
| `automatic_failover_enabled` | 없음 | **`true`** |
| `multi_az_enabled` | 없음 | **`true`** |
| KMS | 없음 | `kms_key_id = module.kms_redis.key_arn` |

```hcl
# modules/redis/main.tf (Prod 전환 시 교체)
resource "aws_elasticache_replication_group" "this" {
  replication_group_id       = "${var.prefix}-redis"
  description                = "Redis for ${var.prefix}"
  node_type                  = var.node_type
  num_cache_clusters         = var.num_cache_clusters    # 2
  automatic_failover_enabled = var.automatic_failover_enabled
  multi_az_enabled           = var.multi_az_enabled
  transit_encryption_enabled = true
  at_rest_encryption_enabled = true
  auth_token                 = var.auth_token            # Secrets Manager 조회 후 주입
  kms_key_id                 = var.kms_key_id
  subnet_group_name          = aws_elasticache_subnet_group.this.name
  security_group_ids         = [aws_security_group.redis.id]
}
```

---

### 2-D. `modules/s3` — KMS 및 버전 관리

| 파라미터 | Dev | Prod |
|---------|-----|------|
| 암호화 방식 | SSE-S3 (AES256) | **SSE-KMS** (CMK) |
| `versioning` | 비활성화 | **활성화** (raw-audio, reports) |
| 액세스 로깅 | 없음 | 활성화 |
| 수명 주기 | raw-audio 365일 삭제 | raw-audio 90일→Glacier, 1년 삭제 |
| CORS AllowedOrigins | CloudFront dev + localhost | `app.utterai.org`, `www.utterai.org` 만 |

---

### 2-E. `modules/sqs` — KMS CMK

| 파라미터 | Dev | Prod |
|---------|-----|------|
| 암호화 | `sqs_managed_sse_enabled = true` | `kms_master_key_id = module.kms_sqs.key_id` |

---

### 2-F. `modules/secrets` — KMS CMK + 자동 교체

| 파라미터 | Dev | Prod |
|---------|-----|------|
| KMS 암호화 | 없음 | `kms_key_id = module.kms_secrets.key_arn` |
| `recovery_window_in_days` | `0` (즉시 삭제 가능) | `30` |
| DB 비밀번호 자동 교체 | 없음 | `rotation_rules { automatically_after_days = 90 }` |

---

### 2-G. `modules/irsa` — Prod 추가 권한

| 역할 | 추가 필요 권한 |
|------|-------------|
| 모든 역할 | `kms:Decrypt`, `kms:GenerateDataKey` (해당 CMK 한정) |
| `eso-irsa-role` | `kms:Decrypt` (secrets KMS) |
| Cluster Autoscaler 역할 | **삭제** (Karpenter로 교체) |
| 신규: Karpenter Controller | EC2 프로비저닝 권한 (`karpenter-controller-policy.json`) |
| 신규: KEDA Operator | `sqs:GetQueueAttributes`, `cloudwatch:GetMetricData` |

---

### 2-H. `modules/vpc` — 3 AZ + NAT × 3

| 파라미터 | Dev | Prod |
|---------|-----|------|
| AZ 수 | 2 | **3** |
| NAT Gateway | 1개 (공유) | **3개** (AZ별 독립) — AZ 장애 시 아웃바운드 유지 |
| VPC Endpoint 추가 | 없음 | `STS Interface`, `KMS Interface` 추가 |

---

## 3. Terraform — environments/prod 레이어 구성

Dev의 4-레이어 구조와 동일하게 prod를 만든다.

```
terraform/environments/prod/
├── 01-network/         ← VPC, Subnet, IGW, NAT×3, VPC Endpoints, KMS 키 전체, WAF
│   ├── main.tf
│   ├── variables.tf
│   ├── outputs.tf
│   └── terraform.tf    ← backend: s3://utterai-prod-terraform-state
├── 02-eks/             ← EKS Cluster, Node Groups, Karpenter 설치, OIDC
│   ├── main.tf
│   ├── variables.tf
│   ├── outputs.tf
│   └── terraform.tf
├── 03-services/        ← Aurora, Redis(ReplicationGroup), S3, SQS, Secrets, ECR, IRSA, KEDA
│   ├── main.tf
│   ├── variables.tf
│   ├── outputs.tf
│   └── terraform.tf
└── 04-addons/          ← ALB Controller, External Secrets, ArgoCD, Prometheus, Karpenter NodePool
    ├── main.tf
    ├── variables.tf
    ├── outputs.tf
    └── terraform.tf
```

**State 버킷** — Prod 전용 계정에 별도 생성 필요:

```hcl
# terraform.tf (prod 공통)
terraform {
  backend "s3" {
    bucket         = "utterai-prod-terraform-state"
    key            = "prod/<layer>/terraform.tfstate"
    region         = "ap-northeast-2"
    encrypt        = true
    kms_key_id     = "alias/prod-tfstate-kms-key"   # Dev는 KMS 없음
    use_lockfile   = true
  }
}
```

---

## 4. K8s 매니페스트 — 주입 방식 변경

### 현재 Dev 방식: envsubst + k8s-deploy-legacy.sh

```bash
envsubst < manifest.yaml | kubectl apply -f -
```

**Prod에서는 이 방식을 그대로 쓰면 안 된다.** 이유:
- 변수 빈 값 가드 없음 → 잘못된 이미지가 조용히 배포됨
- 수동 실행 스크립트 → 감사 추적 없음
- Argo CD Auto-Sync 불가 (GitOps 아님)

### Prod 방식: Kustomize + ArgoCD (k8s/ 구조 활용)

`k8s/` 하위의 Kustomize base/overlays 구조가 이미 있다.
Prod 배포는 이 구조를 ArgoCD가 감시해서 자동으로 적용한다.

```
k8s/apps/
├── backend/
│   ├── base/                   ← Dev와 공통 정의
│   └── overlays/
│       ├── dev/                ← Dev 전용 패치
│       └── prod/               ← Prod 전용 패치 (여기서 모든 차이를 선언)
└── ai-worker/
    ├── base/
    └── overlays/
        ├── dev/
        └── prod/
```

**이미지 태그 주입:** envsubst 대신 ArgoCD Image Updater 또는 CI에서 `kustomize edit set image` 사용

```bash
# CI에서 이미지 태그 업데이트 (k8s-deploy-legacy.sh 대체)
cd k8s/apps/backend/overlays/prod
kustomize edit set image \
  utterai-backend=123456789.dkr.ecr.ap-northeast-2.amazonaws.com/utterai-backend:${GIT_SHA}
git commit -am "ci: update prod image to ${GIT_SHA}"
git push
# → ArgoCD가 변경 감지 후 수동 Sync 대기
```

---

## 5. K8s 매니페스트 — 파일별 변경 내용

### 5-A. `k8s-legacy/namespaces/namespaces.yaml` — PSA 레이블 추가

```yaml
# Prod에서는 모든 워크로드 네임스페이스에 PSA enforce 적용
apiVersion: v1
kind: Namespace
metadata:
  name: utterai-api
  labels:
    app.kubernetes.io/part-of: utterai
    pod-security.kubernetes.io/enforce: baseline
    pod-security.kubernetes.io/enforce-version: latest
    pod-security.kubernetes.io/warn: restricted
    pod-security.kubernetes.io/warn-version: latest
```

---

### 5-B. `k8s-legacy/workloads/api-deployment.yaml`

| 항목 | Dev | Prod |
|------|-----|------|
| `replicas` | 1 | **3** |
| `strategy` | 없음 (기본 RollingUpdate) | `maxSurge: 1, maxUnavailable: 0` 명시 |
| `securityContext` | 없음 | **추가** |
| `podAntiAffinity` | 없음 | **추가** (노드 분산) |
| `APP_ENV` | `dev` | `prod` |
| `LOG_LEVEL` | `DEBUG` | **`INFO`** |
| `FRONTEND_ORIGIN` | CloudFront dev 도메인 | `https://app.utterai.org` |
| `CORS_ALLOW_ORIGINS` | CloudFront dev 도메인 | `https://app.utterai.org,https://www.utterai.org` |
| `DB_POOL_SIZE` | `5` | **`10`** |
| `DB_MAX_OVERFLOW` | `10` | **`20`** |
| `RAW_AUDIO_BUCKET` | `utterai-dev-raw-audio` | `utterai-prod-raw-audio` |
| SQS URL prefix | `utterai-dev-` | `utterai-prod-` |
| `OTEL_RESOURCE_ATTRIBUTES` | `deployment.environment=dev` | `deployment.environment=prod` |

```yaml
# Prod overlay에 추가할 패치
spec:
  replicas: 3
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
  template:
    spec:
      affinity:
        podAntiAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            - labelSelector:
                matchLabels:
                  app: utterai-api
              topologyKey: kubernetes.io/hostname   # 같은 노드에 2개 배치 금지
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
      containers:
        - name: api
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
            readOnlyRootFilesystem: true
```

---

### 5-C. `k8s-legacy/workloads/cpu-worker-deployment.yaml`

| 항목 | Dev | Prod |
|------|-----|------|
| `replicas` | 1 | 1 (KEDA가 제어하므로 의미 없음) |
| securityContext | 없음 | **추가** |
| `APP_ENV` | `dev` | `prod` |
| `LOG_LEVEL` | `DEBUG` | `INFO` |
| SQS URL prefix | `utterai-dev-` | `utterai-prod-` |
| `S3_BUCKET_REPORT` | `utterai-dev-reports` | `utterai-prod-reports` |

---

### 5-D. `k8s-legacy/workloads/ml-gpu-worker-deployment.yaml`

| 항목 | Dev | Prod |
|------|-----|------|
| securityContext | 없음 | **추가** |
| `APP_ENV` | `dev` | `prod` |
| `LOG_LEVEL` | `DEBUG` | `INFO` |
| SQS URL prefix | `utterai-dev-` | `utterai-prod-` |
| GPU capacity type | Spot 허용 | **On-Demand only** (Karpenter NodePool에서 제어) |

---

### 5-E. `k8s-legacy/workloads/hpa-*.yaml`

| HPA | Dev minReplicas | Prod minReplicas | Dev maxReplicas | Prod maxReplicas |
|-----|----------------|-----------------|----------------|-----------------|
| api | 1 | **3** | 2 | **10** |
| cpu-worker | 1 | **1** (KEDA minReplicaCount로 이관) | 2 | **4** |
| ml-gpu-worker | 1 | 삭제 → **KEDA ScaledObject로 교체** | 2 | — |
| batch-worker | 1 | **1** (KEDA minReplicaCount로 이관) | 5 | **5** |

Prod에서 cpu/gpu worker HPA는 KEDA ScaledObject로 대체되므로 충돌 방지를 위해 HPA를 삭제한다.

---

### 5-F. `k8s-legacy/ingress/api-ingress.yaml`

| 항목 | Dev | Prod |
|------|-----|------|
| `listen-ports` | `[{"HTTP":80}]` | **`[{"HTTP":80},{"HTTPS":443}]`** |
| `ssl-redirect` | 없음 | **`"443"` 추가** |
| `certificate-arn` | 없음 | **ACM ARN 주입** |
| `scheme` | `internet-facing` | `internet-facing` (동일) |
| WAF 연동 | 없음 | `alb.ingress.kubernetes.io/wafv2-acl-arn: <WAF ARN>` |

```yaml
# Prod Ingress 추가 annotation
annotations:
  alb.ingress.kubernetes.io/listen-ports: '[{"HTTP":80},{"HTTPS":443}]'
  alb.ingress.kubernetes.io/ssl-redirect: "443"
  alb.ingress.kubernetes.io/certificate-arn: arn:aws:acm:ap-northeast-2:<ACCOUNT_ID>:certificate/<ARN>
  alb.ingress.kubernetes.io/wafv2-acl-arn: arn:aws:wafv2:ap-northeast-2:<ACCOUNT_ID>:regional/webacl/<NAME>/<ID>
```

---

### 5-G. `k8s-legacy/rbac/serviceaccounts.yaml`

IRSA role ARN의 `utterai-dev-` → `utterai-prod-` 교체, 계정 ID도 Prod 계정으로.

```yaml
# Dev
eks.amazonaws.com/role-arn: arn:aws:iam::${DEV_ACCOUNT_ID}:role/utterai-dev-api-irsa-role

# Prod
eks.amazonaws.com/role-arn: arn:aws:iam::${PROD_ACCOUNT_ID}:role/utterai-prod-api-irsa-role
```

---

### 5-H. `k8s-legacy/secrets/*-external-secret.yaml`

시크릿 경로의 `utterai-dev/` → `utterai-prod/` 교체.

```yaml
# Dev
remoteRef:
  key: utterai-dev/backend-api-secret

# Prod
remoteRef:
  key: utterai-prod/backend-api-secret
```

`refreshInterval`은 유지(1h). HF_TOKEN처럼 24h였던 것도 1h로 통일 권장.

---

### 5-I. `k8s-legacy/secrets/cluster-secret-store.yaml`

Prod에서는 `ClusterSecretStore` 대신 **네임스페이스별 `SecretStore`** 로 분리한다.
(`security-gaps.md` §4 참고)

```yaml
# 기존 ClusterSecretStore 대신 각 네임스페이스에 개별 SecretStore 생성
apiVersion: external-secrets.io/v1beta1
kind: SecretStore
metadata:
  name: aws-secrets-manager
  namespace: utterai-api       # 각 네임스페이스마다 동일 이름으로 생성
spec:
  provider:
    aws:
      service: SecretsManager
      region: ap-northeast-2
      auth:
        jwt:
          serviceAccountRef:
            name: utterai-api-sa   # 해당 네임스페이스의 SA만 사용
```

대상 네임스페이스: `utterai-api`, `utterai-ai-cpu`, `utterai-ai-gpu`, `utterai-batch`

---

## 6. K8s 매니페스트 — 신규 추가 파일

### 6-A. Karpenter NodePool + EC2NodeClass (CPU Worker)

```yaml
# k8s-legacy/karpenter/cpu-worker-nodepool.yaml
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: ai-cpu
spec:
  template:
    metadata:
      labels:
        workload: worker
    spec:
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: cpu-worker-class
      taints:
        - key: dedicated
          value: ai-cpu
          effect: NoSchedule
      requirements:
        - key: node.kubernetes.io/instance-type
          operator: In
          values: ["c5.2xlarge", "c5.4xlarge", "m6i.2xlarge"]
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["on-demand"]
  limits:
    cpu: "32"
    memory: 128Gi
  disruption:
    consolidationPolicy: WhenEmpty
    consolidateAfter: 30s
---
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: cpu-worker-class
spec:
  amiFamily: AL2
  role: utterai-prod-karpenter-node-role
  subnetSelectorTerms:
    - tags:
        karpenter.sh/discovery: utterai-prod-eks
  securityGroupSelectorTerms:
    - tags:
        karpenter.sh/discovery: utterai-prod-eks
  tags:
    Name: karpenter-cpu-worker
    Environment: prod
```

### 6-B. Karpenter NodePool + EC2NodeClass (GPU Worker)

```yaml
# k8s-legacy/karpenter/gpu-worker-nodepool.yaml
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
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: gpu-worker-class
      taints:
        - key: dedicated
          value: ai-gpu
          effect: NoSchedule
      requirements:
        - key: node.kubernetes.io/instance-type
          operator: In
          values: ["g4dn.xlarge", "g4dn.2xlarge"]
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["on-demand"]   # Prod: Spot 금지
  limits:
    cpu: "32"
    memory: 128Gi
  disruption:
    consolidationPolicy: WhenEmpty
    consolidateAfter: 30s
```

---

### 6-C. KEDA ScaledObject (CPU Worker)

```yaml
# k8s-legacy/keda/cpu-worker-scaledobject.yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: utterai-cpu-worker-scaledobject
  namespace: utterai-ai-cpu
spec:
  scaleTargetRef:
    name: utterai-cpu-worker
  minReplicaCount: 1      # Prod: 항상 1개 유지 (Dev는 0 허용)
  maxReplicaCount: 4
  cooldownPeriod: 300
  triggers:
    - type: aws-sqs-queue
      authenticationRef:
        name: keda-aws-auth
      metadata:
        queueURL: https://sqs.ap-northeast-2.amazonaws.com/${PROD_ACCOUNT_ID}/utterai-prod-audio-preprocess-queue
        queueLength: "5"    # 메시지 5개당 Pod 1개
        awsRegion: ap-northeast-2
        identityOwner: operator
```

### 6-D. KEDA ScaledObject (GPU Worker)

```yaml
# k8s-legacy/keda/gpu-worker-scaledobject.yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: utterai-gpu-worker-scaledobject
  namespace: utterai-ai-gpu
spec:
  scaleTargetRef:
    name: utterai-ml-gpu-worker
  minReplicaCount: 0      # GPU는 비용 때문에 큐 없으면 0 허용
  maxReplicaCount: 3
  cooldownPeriod: 600     # GPU 노드 종료 전 여유 시간
  triggers:
    - type: aws-sqs-queue
      authenticationRef:
        name: keda-aws-auth
      metadata:
        queueURL: https://sqs.ap-northeast-2.amazonaws.com/${PROD_ACCOUNT_ID}/utterai-prod-gpu-inference-queue
        queueLength: "3"
        awsRegion: ap-northeast-2
        identityOwner: operator
```

---

### 6-E. PodDisruptionBudget

```yaml
# k8s-legacy/pdb/pdb.yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: utterai-api-pdb
  namespace: utterai-api
spec:
  minAvailable: 2    # 3개 중 최소 2개 유지
  selector:
    matchLabels:
      app: utterai-api
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: utterai-ai-api-pdb
  namespace: utterai-ai-api
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app: utterai-ai-api
```

---

## 7. 전환 순서

```text
1. Prod AWS 계정 생성 및 기본 권한 설정
2. Terraform State S3 버킷 + KMS 키 생성 (수동)
3. terraform apply — 01-network (VPC, KMS, WAF)
4. terraform apply — 02-eks (EKS Cluster, Karpenter 설치)
5. terraform apply — 03-services (Aurora, Redis, S3, SQS, Secrets, IRSA, KEDA)
6. terraform apply — 04-addons (ALB Controller, ESO, ArgoCD, Prometheus)
7. k8s apply — namespaces, SecretStore (네임스페이스별)
8. k8s apply — RBAC (serviceaccounts, rolebindings) with Prod IRSA ARN
9. k8s apply — ExternalSecret (utterai-prod/* 경로)
10. k8s apply — Karpenter NodePool + EC2NodeClass
11. k8s apply — KEDA ScaledObject (cpu/gpu worker)
12. k8s apply — PodDisruptionBudget
13. k8s apply — Workloads (api, ai-api, cpu-worker, gpu-worker, batch-worker)
14. k8s apply — Ingress (HTTPS + WAF ARN)
15. Route 53 레코드 연결 + ACM 인증서 확인
16. 배포 후 5분 CloudWatch 알람 모니터링
```

> **주의**: 6단계(Aurora)까지는 데이터가 없으므로 destroy/recreate 자유.
> 7단계 이후 실 데이터가 들어오면 삭제 금지. `deletion_protection = true` 설정 확인 후 진행.

---

## 관련 문서

- [Prod 환경 인프라 가이드](./README.md)
- [Dev vs Prod 비교](../README.md)
- [보안 미비점 분석](../dev/security/gaps.md)
