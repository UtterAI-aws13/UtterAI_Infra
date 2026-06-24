# UtterAI Prod 환경 인프라 가이드

> AWS ap-northeast-2 (Primary) + ap-northeast-1 (DR) | EKS 1.31 | Terraform + Kubernetes

---

## 목차

1. [환경 개요](#1-환경-개요)
2. [도메인 구성](#2-도메인-구성)
3. [네트워크 구성](#3-네트워크-구성)
4. [EKS 클러스터 구성](#4-eks-클러스터-구성)
5. [PostgreSQL 구성 및 Aurora 전환 계획](#5-postgresql-구성-및-aurora-전환-계획)
6. [ElastiCache Redis 구성](#6-elasticache-redis-구성)
7. [S3 버킷 구성](#7-s3-버킷-구성)
8. [SQS 구성](#8-sqs-구성)
9. [Cognito 구성](#9-cognito-구성)
10. [Secrets Manager 구성](#10-secrets-manager-구성)
11. [KMS 구성](#11-kms-구성)
12. [CloudWatch / 모니터링 구성](#12-cloudwatch--모니터링-구성)
13. [CloudFront 구성](#13-cloudfront-구성)
14. [Shared Tooling Account](#14-shared-tooling-account)
15. [CI/CD 파이프라인](#15-cicd-파이프라인)
16. [Terraform 구조](#16-terraform-구조)
17. [환경변수 전체 목록](#17-환경변수-전체-목록)
18. [DR (재해 복구) 구성](#18-dr-재해-복구-구성--tokyo-warm-standby)
19. [장애 대응 체계](#19-장애-대응-체계)
20. [운영 주의사항](#20-prod-환경-운영-주의사항)
21. [Pod 보안 정책 (PSS + Kyverno)](#21-pod-보안-정책-pod-security-standards--kyverno)
22. [Cilium (CNI + 네트워크 보안)](#22-cilium-cni--네트워크-보안)
23. [Terraform 시크릿 관리 현황 및 고도화](#23-terraform-시크릿-관리-현황-및-고도화)

> `app.utterai.org` CloudFront WAF v1 규칙 선택 ADR: [`cloudfront-waf-adr.md`](./cloudfront-waf-adr.md)

---

## 1. 환경 개요

### 1.1 목적

Prod 환경은 실제 사용자에게 서비스를 제공하는 운영 환경이다.

```text
- 실제 치료사 / 보호자 / 아동 데이터 처리
- 24/7 안정적인 서비스 제공
- 장애 발생 시 신속한 감지 및 복구
- 법적 / 의료 데이터 보호 기준 준수
```

### 1.2 기본 원칙

```text
- 고가용성: Multi-AZ 배포, 자동 장애 조치
- 보안 최우선: 최소 권한, 암호화, 접근 로깅 전면 적용
- 성능 보장: 충분한 인스턴스 크기, HPA로 자동 확장
- 변경 통제: main 브랜치 머지 + 승인 후 배포
- Prod와 Dev는 AWS 계정 수준에서 분리
```

### 1.3 환경 식별

| 항목 | 값 |
|---|---|
| 환경 이름 | `prod` |
| AWS Region | `ap-northeast-2` |
| AWS Account | Dev와 분리된 별도 계정 |
| EKS 클러스터 이름 | `utterai-prod-eks` |
| 리소스 Prefix | `utterai-prod-` |
| Terraform State S3 버킷 | `utterai-prod-terraform-state` |

### 1.4 아키텍처 요약

```
User → Route 53 → CloudFront → ALB Ingress → EKS
                                               ├── API Pod          (api NodePool, MNG + HPA)
                                               ├── AI API Pod       (ai-api NodePool, 내부 전용)
                                               ├── CPU AI Worker    (ai-cpu NodePool, KEDA)
                                               ├── GPU AI Worker    (ai-gpu NodePool, KEDA)
                                               └── Batch Worker     (batch NodePool, KEDA)

SQS 파이프라인:
  audio-preprocess-queue → gpu-inference-queue → report-analysis-queue
  rag-ingest-queue (RAG 문서 ingest 전용)
```

> Dev vs Prod 환경 상세 비교: [`docs/README.md`](../README.md)

---

## 2. 도메인 구성

```text
프론트엔드:   https://utterai.com
             https://www.utterai.com
백엔드 API:   https://api.utterai.com
```

### Route 53 레코드

| 레코드 | 타입 | 대상 |
|---|---|---|
| `utterai.com` | A (Alias) | CloudFront Distribution |
| `www.utterai.com` | CNAME | `utterai.com` |
| `api.utterai.com` | A (Alias) | ALB DNS |

---

## 3. 네트워크 구성

### 3.1 VPC

| 항목 | 값 |
|---|---|
| VPC CIDR | `10.0.0.0/16` |
| VPC 이름 | `utterai-prod-vpc` |
| AZ 수 | 3개 (ap-northeast-2a, ap-northeast-2b, ap-northeast-2c) |

### 3.2 Subnet 구성

| Subnet | CIDR | AZ | 용도 |
|---|---|---|---|
| Public Subnet A | `10.0.1.0/24` | ap-northeast-2a | ALB, NAT Gateway |
| Public Subnet B | `10.0.2.0/24` | ap-northeast-2b | ALB, NAT Gateway |
| Public Subnet C | `10.0.3.0/24` | ap-northeast-2c | ALB, NAT Gateway |
| Private App Subnet A | `10.0.11.0/24` | ap-northeast-2a | EKS Pod |
| Private App Subnet B | `10.0.12.0/24` | ap-northeast-2b | EKS Pod |
| Private App Subnet C | `10.0.13.0/24` | ap-northeast-2c | EKS Pod |
| Private Data Subnet A | `10.0.21.0/24` | ap-northeast-2a | Aurora, Redis |
| Private Data Subnet B | `10.0.22.0/24` | ap-northeast-2b | Aurora, Redis |
| Private Data Subnet C | `10.0.23.0/24` | ap-northeast-2c | Aurora, Redis |

> Prod는 각 AZ마다 NAT Gateway 1개씩 배치 (3개) → AZ 장애 시에도 아웃바운드 유지

### 3.3 보안 그룹 구성

| 보안 그룹 | 허용 Inbound | 목적 |
|---|---|---|
| `sg-prod-alb` | 0.0.0.0/0 : 443 | ALB 외부 트래픽 수신 |
| `sg-prod-backend` | sg-prod-alb : 8000 | ALB에서 백엔드 Pod로 |
| `sg-prod-aurora` | sg-prod-backend : 5432 | 백엔드에서 DB로 |
| `sg-prod-redis` | sg-prod-backend : 6379 | 백엔드에서 Redis로 |

> Prod에서는 Bastion 직접 접근 불허. DB 작업은 EKS Job 또는 AWS Systems Manager Session Manager 경유

### 3.4 WAF 구성

```text
AWS WAF Rules:
- AWS Managed Rules Common (OWASP Top 10 기본 차단)
- AWS Managed Rules Known Bad Inputs
- Rate-based Rule: IP당 5분에 2000 req 초과 시 차단
- Geo-restriction: 필요 시 서비스 대상 국가만 허용
```

### 3.5 VPC Endpoint

| 서비스 | Endpoint 타입 |
|---|---|
| S3 | Gateway |
| SQS | Interface |
| Secrets Manager | Interface |
| CloudWatch Logs | Interface |
| ECR API | Interface |
| ECR Docker | Interface |
| STS | Interface |
| KMS | Interface |

---

## 4. EKS 클러스터 구성

### 4.1 클러스터 기본 설정

| 항목 | 값 |
|---|---|
| 클러스터 이름 | `utterai-prod-eks` |
| Kubernetes 버전 | `1.31` |
| Control Plane Endpoint | Private only (Public 접근 차단) |

> Prod에서는 Control Plane Public Endpoint를 차단하고 kubectl 접근은 VPN 또는 Systems Manager 경유

### 4.2 Namespace 구성

Dev와 동일한 Namespace 구조를 사용한다. 환경 분리는 Namespace가 아닌 AWS 계정 수준에서 이루어진다.

| Namespace | 용도 |
|---|---|
| `utterai-api` | 외부 트래픽을 받는 백엔드 REST API |
| `utterai-ai-api` | 클러스터 내부 전용 AI 처리 API |
| `utterai-ai-cpu` | CPU 기반 음성 전처리 워커 |
| `utterai-ai-gpu` | GPU 기반 ML/LLM 추론 워커 |
| `utterai-batch` | RAG 문서 ingest 배치 워커 |
| `utterai-observability` | OpenTelemetry Collector 등 모니터링 스택 |

### 4.3 NodeGroup 구성

안정적인 워크로드(System, API)는 EKS Managed NodeGroup으로, 동적 워크로드(CPU/GPU Worker)는 Karpenter NodePool로 관리하는 **하이브리드 구조**를 사용한다.

| NodeGroup | 관리 방식 | 인스턴스 타입 | 노드 수 | 용도 |
|---|---|---|---:|---|
| `prod-system-nodegroup` | EKS Managed NodeGroup | `t3.large` | 2 ~ 3 | CoreDNS, ALB Controller, Karpenter |
| `prod-api-nodegroup` | EKS Managed NodeGroup + HPA | `t3.xlarge` | 2 ~ 5 | 백엔드 API Pod |
| `cpu-worker-nodepool` | Karpenter NodePool + KEDA | `c5.2xlarge`, `c5.4xlarge` | 0 ~ 4 | AI CPU 처리 |
| `gpu-worker-nodepool` | Karpenter NodePool + KEDA | `g4dn.xlarge`, `g4dn.2xlarge` | 0 ~ 3 | AI GPU 처리 |

> System / API NodeGroup: On-Demand, 항상 실행
>
> CPU / GPU Worker: KEDA가 Pod 수 조정 → Karpenter가 노드 프로비저닝 (섹션 4.5, 4.6 참고)

### 4.4 HPA (Horizontal Pod Autoscaler)

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: backend-api-hpa
  namespace: utterai-api
spec:
  minReplicas: 3
  maxReplicas: 10
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 60
    - type: Resource
      resource:
        name: memory
        target:
          type: Utilization
          averageUtilization: 70
```

### 4.5 KEDA (Queue-based Autoscaler)

KEDA는 SQS 큐 메시지 수를 기반으로 Worker Pod를 자동 스케일링한다.

```text
동작 방식:
1. 분석 요청이 audio-preprocess-queue에 쌓임
2. KEDA가 큐 메시지 수를 감지 → CPU Worker Pod 수 증가
3. CPU Worker가 전처리 후 gpu-inference-queue로 전달
4. KEDA가 gpu-inference-queue 감지 → GPU Worker Pod 수 증가
5. 큐가 비워지면 Pod 수 자동 감소 → Karpenter가 노드 종료
```

**CPU Worker ScaledObject**

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: cpu-worker-scaledobject
  namespace: utterai-ai-cpu
spec:
  scaleTargetRef:
    name: utterai-cpu-worker
  minReplicaCount: 1
  maxReplicaCount: 4
  triggers:
    - type: aws-sqs-queue
      metadata:
        queueURL: https://sqs.ap-northeast-2.amazonaws.com/{ACCOUNT_ID}/utterai-prod-audio-preprocess-queue
        queueLength: "5"
        awsRegion: ap-northeast-2
        identityOwner: operator
```

**GPU Worker ScaledObject**

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: gpu-worker-scaledobject
  namespace: utterai-ai-gpu
spec:
  scaleTargetRef:
    name: utterai-ml-gpu-worker
  minReplicaCount: 0
  maxReplicaCount: 3
  triggers:
    - type: aws-sqs-queue
      metadata:
        queueURL: https://sqs.ap-northeast-2.amazonaws.com/{ACCOUNT_ID}/utterai-prod-gpu-inference-queue
        queueLength: "1"
        awsRegion: ap-northeast-2
        identityOwner: operator
```

> `queueLength` 값은 큐 메시지 1개당 Pod 1개를 의미. GPU 워커는 1개 작업에 메모리 점유가 크므로 1로 설정

### 4.6 Karpenter (Node Provisioner)

Karpenter는 KEDA가 생성한 `Pending` Pod를 감지해 EC2를 자동 프로비저닝한다.

```text
KEDA + Karpenter 전체 흐름:

[SQS 메시지 증가]
  → KEDA: Worker Pod replicas 증가
  → 새 Pod가 Pending (노드 부족)
  → Karpenter: Pod 리소스 요청 확인 → EC2 기동 (~60초)
  → Pod가 새 노드에 스케줄링 → 분석 시작

[SQS 큐 소진]
  → KEDA: Worker Pod replicas 0으로 감소
  → 노드가 빈 상태
  → Karpenter: 30초 후 EC2 종료 → 과금 중단
```

**CPU Worker NodePool**

```yaml
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: cpu-worker-nodepool
spec:
  template:
    metadata:
      labels:
        role: cpu-worker
    spec:
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: cpu-worker-class
      requirements:
        - key: node.kubernetes.io/instance-type
          operator: In
          values: ["c5.2xlarge", "c5.4xlarge"]
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["on-demand"]
        - key: topology.kubernetes.io/zone
          operator: In
          values: ["ap-northeast-2a", "ap-northeast-2b", "ap-northeast-2c"]
      taints:
        - key: dedicated
          value: ai-cpu
          effect: NoSchedule
  limits:
    cpu: 64
    memory: 256Gi
  disruption:
    consolidationPolicy: WhenEmpty
    consolidateAfter: 30s
```

**GPU Worker NodePool**

```yaml
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: gpu-worker-nodepool
spec:
  template:
    metadata:
      labels:
        role: gpu-worker
    spec:
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: gpu-worker-class
      requirements:
        - key: node.kubernetes.io/instance-type
          operator: In
          values: ["g4dn.xlarge", "g4dn.2xlarge"]
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["on-demand"]
        - key: topology.kubernetes.io/zone
          operator: In
          values: ["ap-northeast-2a", "ap-northeast-2b", "ap-northeast-2c"]
      taints:
        - key: dedicated
          value: ai-gpu
          effect: NoSchedule
  limits:
    cpu: 64
    memory: 256Gi
  disruption:
    consolidationPolicy: WhenEmpty
    consolidateAfter: 30s
```

> Karpenter 자체는 `prod-system-nodegroup`에 배포. Karpenter가 죽으면 Worker 노드 프로비저닝이 불가능하므로 System NodeGroup의 가용성이 전제되어야 한다

### 4.7 PodDisruptionBudget

롤링 배포 중 서비스 중단을 방지한다.

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: backend-api-pdb
  namespace: utterai-api
spec:
  minAvailable: 2
  selector:
    matchLabels:
      app: backend-api
```

### 4.8 백엔드 Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: backend-api
  namespace: utterai-api
spec:
  replicas: 3
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
  template:
    spec:
      serviceAccountName: backend-api-sa
      affinity:
        podAntiAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            - labelSelector:
                matchExpressions:
                  - key: app
                    operator: In
                    values: [backend-api]
              topologyKey: kubernetes.io/hostname
      containers:
        - name: backend-api
          image: {ECR_URI}/utterai-backend:{git_sha}
          ports:
            - containerPort: 8000
          resources:
            requests:
              cpu: 500m
              memory: 1Gi
            limits:
              cpu: 1000m
              memory: 2Gi
          readinessProbe:
            httpGet:
              path: /health/ready
              port: 8000
            initialDelaySeconds: 15
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /health/live
              port: 8000
            initialDelaySeconds: 30
            periodSeconds: 20
```

> `podAntiAffinity`로 Pod를 서로 다른 노드에 분산 배치

### 4.9 IRSA 구성

IRSA는 총 **8개** 역할로 구성된다. 플랫폼 컴포넌트용 3개와 애플리케이션 워크로드용 5개로 나뉜다.

**플랫폼 컴포넌트 (3개)**

| IAM Role | Namespace / SA | 허용 권한 |
|---|---|---|
| `utterai-prod-lbc-irsa-role` | `ingress-system/aws-load-balancer-controller` | ALB/NLB 관리 전체 (LBC 공식 정책) |
| `utterai-prod-cluster-autoscaler-role` | `kube-system/cluster-autoscaler` | AutoScaling Group DescribeSet, SetDesiredCapacity 등 |
| `utterai-prod-eso-irsa-role` | `external-secrets/external-secrets` | `secretsmanager:GetSecretValue`, `DescribeSecret` (`utterai-prod/*`) |

> ESO Role이 핵심. 모든 ExternalSecret은 ESO IRSA를 통해 Secrets Manager에 접근하므로 개별 Pod Role에는 Secrets Manager 권한이 없어도 된다.

**애플리케이션 워크로드 (5개)**

| IAM Role | Namespace / SA | 허용 권한 요약 |
|---|---|---|
| `utterai-prod-api-irsa-role` | `utterai-api/utterai-api-sa` | S3(raw-audio, reports) GetObject/PutObject, SQS(audio-preprocess) SendMessage, CloudWatch PutMetricData |
| `utterai-prod-ai-api-irsa-role` | `utterai-ai-api/utterai-ai-api-sa` | SQS(audio-preprocess) SendMessage, GetQueueAttributes |
| `utterai-prod-ai-cpu-irsa-role` | `utterai-ai-cpu/utterai-cpu-worker-sa` | SQS(audio-preprocess) Receive/Delete, SQS(gpu-inference) Send, SQS(report-analysis) Receive/Delete, S3(raw-audio) GetObject/PutObject, S3(reports) PutObject, Bedrock InvokeModel |
| `utterai-prod-ai-ml-gpu-irsa-role` | `utterai-ai-gpu/utterai-ml-gpu-worker-sa` | SQS(gpu-inference) Receive/Delete, SQS(report-analysis) Send, S3(raw-audio) GetObject/PutObject |
| `utterai-prod-batch-irsa-role` | `utterai-batch/utterai-batch-worker-sa` | SQS(rag-ingest) Receive/Delete, S3(reports) PutObject, Secrets Manager GetSecretValue (db-password) |

```yaml
# 예시: 백엔드 API ServiceAccount
apiVersion: v1
kind: ServiceAccount
metadata:
  name: utterai-api-sa
  namespace: utterai-api
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::{ACCOUNT_ID}:role/utterai-prod-api-irsa-role
```

---

## 5. PostgreSQL 구성 및 Aurora 전환 계획

### 5.1 현재 적용 상태

현재 prod는 Aurora가 아니라 단일 RDS PostgreSQL 인스턴스로 운영 중이다. Terraform state 기준 리소스는 `module.rds.aws_db_instance.this`이며, AWS 실물은 `utterai-prod-rds`이다.

| 항목 | 값 |
|---|---|
| 인스턴스 식별자 | `utterai-prod-rds` |
| 엔진 | PostgreSQL 16 |
| 인스턴스 타입 | `db.r6g.large` |
| Terraform 리소스 | `aws_db_instance` |
| 배포 | Single-AZ (`MultiAZ=false`) |
| 백업 보존 기간 | 7일 |
| 자동 마이너 버전 업그레이드 | 활성화 |
| 암호화 | `storage_encrypted=true` |
| 비밀번호 관리 | `manage_master_user_password=true` |

### 5.2 Aurora 전환 계획

원래 prod DB는 Multi-AZ 고가용성과 빠른 장애 조치를 위해 Aurora PostgreSQL로 전환하는 것이 목표다. 현재 `terraform/modules/aurora` 모듈은 존재하지만 prod `03-services`에는 아직 연결되어 있지 않다.

전환 목표 구성:

| 항목 | 목표 값 |
|---|---|
| 클러스터 식별자 | `utterai-prod-aurora` |
| 엔진 | Aurora PostgreSQL 16 |
| 인스턴스 수 | Writer 1 + Reader 1 이상 |
| 배포 | Multi-AZ |
| 접속 endpoint | Writer endpoint / Reader endpoint 분리 |
| 장애 조치 | Aurora failover |

전환 작업 항목:

- `modules/rds` 호출을 `modules/aurora` 호출로 교체
- Aurora reader 인스턴스를 추가해 writer/reader를 서로 다른 AZ에 배치
- 기존 `utterai-prod-rds` 데이터 이관 방식 결정: snapshot restore, logical dump/restore, 또는 DMS
- backend/worker `DB_HOST`를 Aurora writer endpoint로 변경
- RDS managed secret ARN 변경에 따른 ExternalSecret 참조 재검토
- 전환 전 최종 스냅샷, 롤백 절차, 운영 중단 시간 또는 read-only window 확정

단기 안정화가 우선이면 Aurora 전환 전에 현재 RDS에 `multi_az = true`를 적용하는 방안도 검토할 수 있다.

### 5.3 접속 정보

```text
Current Endpoint: utterai-prod-rds.c76womumyurf.ap-northeast-2.rds.amazonaws.com
Future Writer Endpoint: utterai-prod-aurora.cluster-xxxx.ap-northeast-2.rds.amazonaws.com
Future Reader Endpoint: utterai-prod-aurora.cluster-ro-xxxx.ap-northeast-2.rds.amazonaws.com
Port: 5432
DB Name: utterai
User: utterai_app
Password: Secrets Manager에서 관리
```

### 5.4 Connection 관리

```text
SQLAlchemy Pool 설정:
- DB_POOL_SIZE=10
- DB_MAX_OVERFLOW=20
- 최대 Connection = Pod 수 × 30
- Pod 10개일 경우 최대 300 Connection

현재 RDS max_connections는 인스턴스 클래스 기준으로 산정
Aurora 전환 후 max_connections = db.r6g.large 기준 약 1500
Connection이 80%를 넘으면 RDS Proxy 도입 검토
```

### 5.5 DB 마이그레이션 정책

```text
- 운영 시간 외(새벽 2~4시) 마이그레이션 실행
- 마이그레이션 전 스냅샷 생성 필수
- Alembic --sql 옵션으로 SQL 사전 검토 후 실행
- DDL 변경(컬럼 추가/삭제)은 반드시 무중단 방식으로 설계
```

---

## 6. ElastiCache Redis 구성

| 항목 | 값 |
|---|---|
| 클러스터 식별자 | `utterai-prod-redis` |
| 엔진 버전 | Redis 7.x |
| 노드 타입 | `cache.r6g.large` |
| 복제 그룹 | Primary 1 + Replica 1 |
| Multi-AZ | 활성화 |
| 자동 장애 조치 | 활성화 |
| TLS | 활성화 |
| AUTH Token | 활성화 (Secrets Manager 관리) |
| 자동 백업 | 활성화 (1일 보존) |
| 암호화 | AWS Managed Key (`aws/elasticache`) |

```text
Primary Endpoint: utterai-prod-redis.xxxxxx.cache.amazonaws.com
Port: 6379
```

---

## 7. S3 버킷 구성

### 7.1 버킷 목록

| 버킷 이름 | 용도 | 퍼블릭 여부 |
|---|---|---|
| `utterai-prod-frontend` | 프론트엔드 정적 파일 | CloudFront 경유만 허용 |
| `utterai-prod-raw-audio` | 사용자 업로드 원본 음성 | 비공개 |
| `utterai-prod-template` | 분석 템플릿 문서 | 비공개 |
| `utterai-prod-rag-ingest` | RAG 문서 ingest 원본 | 비공개 |
| `utterai-prod-reports` | 분석 결과 리포트 | 비공개 |

### 7.2 버킷 공통 설정

```text
현재 모듈(terraform/modules/s3/main.tf) 구현 상태:
- 퍼블릭 액세스 차단: 전체 활성화
- 서버 사이드 암호화: AES256 (SSE-S3)
- 객체 수명 주기: raw-audio 365일 후 삭제

Prod 추가 예정 (modules/s3에 변수 추가 필요):
- 서버 사이드 암호화: SSE-KMS (Prod CMK)로 교체
- 버전 관리: raw-audio, reports에 활성화
- 액세스 로깅: 활성화
- raw-audio 수명 주기: 90일 Glacier 전환, 1년 삭제로 변경
```

### 7.3 CORS 설정 (raw-audio 버킷)

```json
[
  {
    "AllowedHeaders": ["*"],
    "AllowedMethods": ["GET", "PUT", "POST"],
    "AllowedOrigins": [
      "https://utterai.com",
      "https://www.utterai.com"
    ],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3000
  }
]
```

---

## 8. SQS 구성

### 8.1 큐 목록

Dev와 동일한 4단계 파이프라인 구조를 사용한다.

| 큐 이름 | 용도 | 소비자 |
|---|---|---|
| `utterai-prod-audio-preprocess-queue` | 음성 전처리 요청 | CPU Worker |
| `utterai-prod-gpu-inference-queue` | GPU 추론 요청 (화자분리 + ASR) | GPU Worker |
| `utterai-prod-report-analysis-queue` | Bedrock 리포트 생성 요청 | CPU Worker (Bedrock) |
| `utterai-prod-rag-ingest-queue` | RAG 문서 ingest | Batch Worker |
| `utterai-prod-audio-preprocess-dlq` | 전처리 실패 메시지 보관 | — |
| `utterai-prod-gpu-inference-dlq` | GPU 추론 실패 메시지 보관 | — |
| `utterai-prod-report-analysis-dlq` | 리포트 생성 실패 메시지 보관 | — |
| `utterai-prod-rag-ingest-dlq` | RAG ingest 실패 메시지 보관 | — |

### 8.2 큐 설정

```text
utterai-prod-audio-preprocess-queue:
- 메시지 보존 기간: 4일
- 가시성 타임아웃: 300초 (AI 분석 예상 최대 시간)
- 최대 메시지 크기: 256KB
- 암호화: AWS Managed Key (`aws/sqs`)
- DLQ: utterai-prod-audio-preprocess-dlq (maxReceiveCount: 3)

utterai-prod-gpu-inference-queue:
- 가시성 타임아웃: 600초 (GPU 추론 소요 시간 반영)
- DLQ: utterai-prod-gpu-inference-dlq (maxReceiveCount: 2)

utterai-prod-report-analysis-queue:
- 가시성 타임아웃: 300초
- DLQ: utterai-prod-report-analysis-dlq (maxReceiveCount: 3)

utterai-prod-rag-ingest-queue:
- 가시성 타임아웃: 300초
- DLQ: utterai-prod-rag-ingest-dlq (maxReceiveCount: 3)

모든 DLQ:
- 메시지 보존 기간: 14일
- DLQ 메시지 발생 시 즉시 CloudWatch 알람 발생
```

---

## 9. Cognito 구성

### 9.1 User Pool

| 항목 | 값 |
|---|---|
| User Pool 이름 | `utterai-prod-user-pool` |
| 로그인 방식 | 이메일 |
| MFA | 선택적 (TOTP 지원) |
| 비밀번호 정책 | 최소 12자, 대소문자/숫자/특수문자 포함 |
| 이메일 인증 | 필수 |
| 고급 보안 | 활성화 (이상 로그인 감지) |

### 9.2 App Client

| 항목 | 값 |
|---|---|
| 클라이언트 이름 | `utterai-prod-web-client` |
| 인증 흐름 | `USER_PASSWORD_AUTH`, `REFRESH_TOKEN_AUTH` |
| Callback URL | `https://utterai.com/auth/callback` |
| Logout URL | `https://utterai.com/logout` |
| 토큰 만료 | Access Token 1시간, Refresh Token 30일 |

---

## 10. Secrets Manager 구성

### 10.1 시크릿 주입 전체 흐름

```
[AWS Secrets Manager]
  │  KMS(prod-secrets-kms-key)로 저장값 암호화
  │
  ▼
[External Secrets Operator (ESO)]
  │  IRSA(eso-irsa-role)로 GetSecretValue 호출
  │  refreshInterval: 1h — 로테이션 자동 반영
  ▼
[ClusterSecretStore: aws-secrets-manager]
  │  클러스터 전체에서 공유되는 Secrets Manager 연결점
  │
  ▼
[ExternalSecret (네임스페이스별)]
  │  Secrets Manager 키 → K8s Secret 키 매핑
  ▼
[K8s Secret]
  │  Pod의 env.valueFrom.secretKeyRef 로 주입
  ▼
[Pod 환경변수]
```

> Pod 자체의 IRSA Role에는 Secrets Manager 권한이 없다.
> ESO가 대신 읽고 K8s Secret으로 동기화하므로 역할 권한이 분리된다.

### 10.2 Secret 목록

| Secret 이름 | 저장 키 | 교체 주기 |
|---|---|---|
| `utterai-prod/backend-api-secret` | `DB_PASSWORD`, `JWT_SECRET_KEY`, `INTERNAL_CALLBACK_TOKEN`, `INTERNAL_CALLBACK_HMAC_SECRET`, `REDIS_AUTH_TOKEN` | DB_PASSWORD 90일 자동 교체, 나머지 수동 |
| `utterai-prod/ai-worker-secret` | `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`, `DB_NAME` (AI Worker → BE DB 직접 접근용) | 수동 관리 |
| `utterai-prod/gpu-worker-secret` | `HF_TOKEN` (HuggingFace Access Token) | 수동 관리 |

> 모든 Prod Secret은 AWS Managed Key (`aws/secretsmanager`) 로 암호화

### 10.3 ClusterSecretStore

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ClusterSecretStore
metadata:
  name: aws-secrets-manager
spec:
  provider:
    aws:
      service: SecretsManager
      region: ap-northeast-2
      # auth는 ESO Pod의 IRSA(eso-irsa-role)를 자동 사용
```

### 10.4 ExternalSecret 목록

**backend-api-secret** (`utterai-api` 네임스페이스)

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: backend-api-external-secret
  namespace: utterai-api
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: aws-secrets-manager
    kind: ClusterSecretStore
  target:
    name: backend-api-secret
    creationPolicy: Owner
  data:
    - secretKey: DB_PASSWORD
      remoteRef:
        key: utterai-prod/backend-api-secret
        property: DB_PASSWORD
    - secretKey: JWT_SECRET_KEY
      remoteRef:
        key: utterai-prod/backend-api-secret
        property: JWT_SECRET_KEY
    - secretKey: INTERNAL_CALLBACK_TOKEN
      remoteRef:
        key: utterai-prod/backend-api-secret
        property: INTERNAL_CALLBACK_TOKEN
    - secretKey: INTERNAL_CALLBACK_HMAC_SECRET
      remoteRef:
        key: utterai-prod/backend-api-secret
        property: INTERNAL_CALLBACK_HMAC_SECRET
    - secretKey: REDIS_AUTH_TOKEN
      remoteRef:
        key: utterai-prod/backend-api-secret
        property: REDIS_AUTH_TOKEN
```

**ai-worker-secret** (`utterai-ai-gpu` + `utterai-batch` 두 네임스페이스에 동일하게 동기화)

```yaml
# utterai-ai-gpu 네임스페이스와 utterai-batch 네임스페이스 각각에 배포
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: ai-worker-external-secret
  namespace: utterai-ai-gpu   # utterai-batch 네임스페이스에도 동일 적용
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: aws-secrets-manager
    kind: ClusterSecretStore
  target:
    name: ai-worker-secret
    creationPolicy: Owner
  data:
    - secretKey: DB_USER
      remoteRef:
        key: utterai-prod/ai-worker-secret
        property: DB_USER
    - secretKey: DB_PASSWORD
      remoteRef:
        key: utterai-prod/ai-worker-secret
        property: DB_PASSWORD
    - secretKey: DB_HOST
      remoteRef:
        key: utterai-prod/ai-worker-secret
        property: DB_HOST
    - secretKey: DB_PORT
      remoteRef:
        key: utterai-prod/ai-worker-secret
        property: DB_PORT
    - secretKey: DB_NAME
      remoteRef:
        key: utterai-prod/ai-worker-secret
        property: DB_NAME
```

**gpu-worker-secret** (`utterai-ai-gpu` 네임스페이스)

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: gpu-worker-external-secret
  namespace: utterai-ai-gpu
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: aws-secrets-manager
    kind: ClusterSecretStore
  target:
    name: gpu-worker-secret
    creationPolicy: Owner
  data:
    - secretKey: HF_TOKEN
      remoteRef:
        key: utterai-prod/gpu-worker-secret
        property: HF_TOKEN
```

---

## 11. KMS 구성

Aurora만 CMK(Customer Managed Key)를 사용하고, 나머지 서비스는 AWS Managed Key를 사용한다.

Aurora에 CMK가 필요한 이유: Cross-account Aurora Global Database(Seoul → Tokyo DR) 구성 시 AWS Managed Key(`aws/rds`)는 계정 간 공유가 불가능하다. CMK는 키 정책에 Tokyo 계정을 허용하여 Secondary 클러스터가 데이터를 복호화할 수 있게 한다.

| KMS Key | 종류 | 암호화 대상 |
|---|---|---|
| `prod-aurora-kms-key` | **CMK** (Customer Managed Key) | Aurora PostgreSQL — Cross-account DR 필요 |
| `aws/elasticache` | AWS Managed Key | ElastiCache Redis |
| `aws/s3` | AWS Managed Key | S3 버킷 |
| `aws/sqs` | AWS Managed Key | SQS 큐 |
| `aws/secretsmanager` | AWS Managed Key | Secrets Manager |
| `aws/logs` | AWS Managed Key | CloudWatch Logs |

```text
CMK(prod-aurora-kms-key) 키 정책 원칙:
- 백엔드 IAM Role: kms:Decrypt, kms:GenerateDataKey 허용
- KMS Key 관리(생성/삭제/교체): 별도 관리자 Role만 허용
- Tokyo DR 계정: kms:Decrypt 허용 (Aurora Global DB Secondary용)
- CloudTrail로 모든 KMS 사용 이력 기록
- 삭제 전 대기 기간: 30일 (실수로 인한 데이터 손실 방지)
```

---

## 12. CloudWatch / Prometheus / Loki / 모니터링 구성

Prod 모니터링은 CloudWatch, Prometheus, Loki, Tempo의 역할을 분리한다.

| 수집 대상 | 저장 위치 | 시각화 |
|---|---|---|
| AWS 관리 지표(ALB, Aurora, Redis, SQS, CloudFront) | CloudWatch Metrics | CloudWatch Dashboard, Grafana CloudWatch datasource |
| Kubernetes 지표(Node, Pod, kube-state-metrics, cAdvisor) | Prometheus TSDB | Grafana Prometheus datasource |
| 애플리케이션 OTLP metrics | OpenTelemetry Collector → Prometheus scrape | Grafana Prometheus datasource |
| 애플리케이션 trace | OpenTelemetry Collector → Tempo → S3 (`utterai-prod-tempo`) | Grafana Tempo datasource |
| 애플리케이션 / Kubernetes 로그 | Promtail → Loki → S3 (`utterai-prod-loki`) | Grafana Loki datasource |
| AWS 서비스 / 감사 로그 | CloudWatch Logs | CloudWatch Logs Insights, Grafana CloudWatch datasource |

### 12.1 로그 저장소

애플리케이션 Pod stdout/stderr 로그는 CloudWatch Logs에 중복 적재하지 않고 Loki에 저장한다.
Loki는 IRSA로 `utterai-prod-loki` S3 버킷에 접근하며, retention은 14일(`336h`)로 제한한다.
분산 추적 데이터는 Tempo가 `utterai-prod-tempo` S3 버킷에 저장하며, retention은 3일(`72h`)로 제한한다.

| 저장소 | 보존 기간 | 암호화 |
|---|---|---|
| `utterai-prod-loki` | 14일 | S3 SSE-S3 |
| `utterai-prod-tempo` | 3일 | S3 SSE-S3 |
| `/aws/rds/cluster/utterai-prod-aurora` | 30일 | AWS Managed Key (`aws/logs`) |
| `/aws/elasticache/utterai-prod-redis` | 14일 | AWS Managed Key (`aws/logs`) |

### 12.2 알람 전체 목록

| 알람 이름 | 조건 | 알림 채널 |
|---|---|---|
| `prod-backend-5xx-rate` | 5xx 비율 > 5% (5분) | Discord + 온콜 |
| `prod-backend-latency-p95` | p95 응답 시간 > 2초 (5분) | Discord |
| `prod-aurora-cpu` | CPU > 70% (5분) | Discord |
| `prod-aurora-connections` | 연결 수 > 1000 (5분) | Discord |
| `prod-aurora-replica-lag` | Replica Lag > 5초 | Discord + 온콜 |
| `prod-redis-cpu` | CPU > 70% (5분) | Discord |
| `prod-redis-memory` | 메모리 > 80% (5분) | Discord |
| `prod-sqs-dlq-count` | DLQ 메시지 수 > 0 (4개 DLQ 각각) | Discord + 온콜 |
| `prod-sqs-queue-depth` | 큐 메시지 수 > 500 (5분) | Discord |
| `prod-ai-callback-failure` | AI Callback 실패 수 > 5 (5분) | Discord |

### 12.3 대시보드

CloudWatch 대시보드 `utterai-prod-overview`:

```text
- 백엔드 API Request Count / Error Rate / p95 Latency
- Aurora CPU / 연결 수 / Replica Lag
- Redis CPU / Memory / Hit Rate
- SQS 큐 깊이 / DLQ 수 (4개 큐 각각)
- AI Callback 성공/실패 수
- EKS Node CPU / Memory
```

### 12.4 OpenTelemetry / Grafana

```text
Collector: OpenTelemetry Collector (EKS 내 Deployment, utterai-observability namespace)
Trace backend: Tempo (monitoring namespace, S3 backend: utterai-prod-tempo)
Sampling: 운영 중 10%, 에러 100%
```

### 12.5 Prometheus

Prod 클러스터에는 `kube-prometheus-stack`을 배포한다. Prometheus는 Kubernetes 기본 지표와 OpenTelemetry Collector의 Prometheus exporter endpoint를 scrape한다.

```text
Namespace: monitoring
Retention: 운영 정책에 맞춰 별도 지정
Scrape 대상:
  - kube-state-metrics
  - prometheus-node-exporter
  - kubelet / cAdvisor
  - ServiceMonitor: otel-collector.utterai-observability
```

애플리케이션 Pod는 OTLP HTTP로 Collector에 metrics를 전송하고, Collector는 `prom-exporter` service port(`8889`)에서 Prometheus scrape endpoint를 노출한다. `ServiceMonitor`는 `k8s/platform/observability/base/otel-collector.yaml`에 함께 정의한다.

### 12.6 Grafana

Grafana 데이터 소스:
  - CloudWatch (메트릭 / 로그)
  - Prometheus (Kubernetes / 애플리케이션 metrics)
  - Loki (애플리케이션 / Kubernetes logs)
  - Tempo (OpenTelemetry trace)
  - Aurora Performance Insights (DB 쿼리 분석)

주요 대시보드:
  - utterai-prod-overview (서비스 전체 현황)
  - utterai-prod-ai-pipeline (AI 파이프라인 처리량 및 큐 깊이)
  - utterai-prod-db (Aurora / Redis 상세)
  - UtterAI CA vs Karpenter (node autoscaling 전환 검증)
```

`UtterAI CA vs Karpenter` dashboard는 `k8s/platform/observability/base/grafana-dashboard-ca-karpenter.yaml`에 정의한다. Dev에서는 CA baseline을 먼저 수집하고, Karpenter 설치 후 같은 dashboard에서 `karpenter_*` 지표와 비교한다.

### 12.7 Alerting

Prod 알림의 1차 기준은 CloudWatch Alarm이다. AWS 관리 지표와 서비스 SLO 알림은 CloudWatch Alarm에서 Discord/온콜 채널로 전송한다.

Prometheus Alertmanager는 on-call routing과 중복 알림 정책이 확정된 뒤 활성화한다. 그 전까지 Prometheus는 Kubernetes/App metrics 저장 및 Grafana 시각화 경로로 사용한다.

---

## 13. CloudFront 구성

```text
Origin: S3 utterai-prod-frontend (OAC 사용, 직접 접근 차단)
Distribution: utterai-prod-frontend-cf
도메인: utterai.com, www.utterai.com
HTTPS: ACM 인증서 (us-east-1 발급 필수)
캐시 정책: 정적 파일 1년 캐시, HTML은 캐시 없음
```

백엔드 API 라우팅은 `api.utterai.com`을 ALB에 직접 연결하는 방식을 사용한다.

---

## 14. Shared Tooling Account

GitHub, ECR, Argo CD, Terraform, CloudWatch, Grafana는 Prod Account와 분리된 **별도 AWS 계정(Shared Tooling Account)**에서 관리한다.

| 리소스 | 설명 |
|---|---|
| GitHub | 소스 코드 및 Kubernetes Manifest 저장 |
| GitHub Actions | CI 파이프라인 (테스트, 빌드, ECR Push) |
| Amazon ECR | Docker 이미지 저장소 (Seoul + Tokyo 복제) |
| Argo CD | GitOps 배포 도구 (Prod Account EKS를 대상으로 배포) |
| Terraform | 인프라 코드 실행 및 State 관리 |
| CloudWatch | 메트릭 및 로그 수집 (Cross-Account) |
| Grafana | 통합 모니터링 대시보드 |

**Cross-Account 접근**

```text
Tooling Account → Prod Account EKS 접근:
- Tooling Account의 Argo CD가 AssumeRole로 Prod Account IAM Role 사용
- Prod Account IAM Role에 EKS 배포 최소 권한만 부여
```

---

## 15. CI/CD 파이프라인

### 15.1 배포 흐름

```text
main 브랜치 PR 머지 (필수 리뷰 1명 이상)
    │
    ▼
GitHub Actions (prod-deploy.yaml) — Shared Tooling Account
    ├── 단위 테스트 / 통합 테스트
    ├── Docker Build
    ├── ECR Push (utterai-prod-backend:{git_sha})  ← Seoul ECR
    ├── ECR Cross-Region Copy                       ← Tokyo ECR (DR용)
    ├── 이미지 취약점 스캔 (ECR Scanning)
    └── Kubernetes Manifest 이미지 태그 업데이트
    ▼
Argo CD (prod 클러스터 감시) — Shared Tooling Account
    └── Sync 전 Diff 검토 (수동 Sync)
    ▼
EKS Rolling Update  ← Prod Account
```

### 15.2 Argo CD 배포 정책

```text
- Prod 배포는 Auto-Sync 비활성화. 담당자가 Argo CD UI에서 직접 Sync
- 배포 전 Diff를 반드시 확인
- 배포 후 5분간 알람 모니터링
- 문제 발생 시 이전 이미지 태그로 즉시 롤백
```

### 15.3 ECR 이미지 태그 규칙

```text
utterai-prod-backend:{git_sha_short}  (배포 버전)
utterai-prod-backend:stable           (운영 중 버전)
```

### 15.4 브랜치 보호 규칙 (main)

```text
- PR 리뷰 최소 1명 승인 필수
- CI 통과 필수
- Force Push 금지
- 브랜치 삭제 금지
```

---

## 16. Terraform 구조

Prod 인프라는 Dev와 동일하게 4개 레이어로 분리 관리한다.

```text
terraform/
├── modules/              ← Dev와 공용 모듈
│   ├── vpc/
│   ├── eks/
│   ├── eks-addons/
│   ├── irsa/
│   ├── rds/
│   ├── redis/
│   ├── s3/
│   ├── sqs/
│   ├── secrets/
│   ├── cloudfront/
│   ├── waf/
│   └── ecr/
└── environments/
    └── prod/
        ├── 01-network/   ← VPC, 서브넷 (State: prod/network)
        ├── 02-eks/       ← EKS 클러스터, NodeGroup (State: prod/platform)
        ├── 03-services/  ← RDS, Redis, S3, SQS, IRSA, ECR (State: prod/services)
        └── 04-addons/    ← Helm 애드온, WAF, CloudFront (State: prod/addons)
```

### 16.1 Terraform 상태 관리

```text
Backend: S3 (utterai-prod-terraform-state)
Lock: S3 네이티브 락 (use_lockfile = true)
암호화: KMS
```

### 16.2 레이어 간 의존 관계

```text
01-network ──→ 02-eks ──→ 03-services ──→ 04-addons
```

### 16.3 Terraform 적용 정책

```text
- terraform plan 결과를 PR에 포함 (GitHub Actions)
- terraform apply는 리뷰 승인 후 실행
- Prod 리소스 삭제는 수동으로만 허용 (lifecycle.prevent_destroy 설정)
```

---

## 17. 환경변수 전체 목록

백엔드 Pod에 주입되는 환경변수 전체이다.

```env
# 앱 기본
APP_ENV=prod
APP_NAME=utterai-backend
API_BASE_PATH=/api/v1
LOG_LEVEL=INFO

# CORS
CORS_ALLOW_ORIGINS=https://utterai.com,https://www.utterai.com

# Cognito
COGNITO_REGION=ap-northeast-2
COGNITO_USER_POOL_ID=ap-northeast-2_xxxxxxxx
COGNITO_CLIENT_ID=xxxxxxxxxxxxxxxxxxxxxxxxxx
COGNITO_JWKS_URL=https://cognito-idp.ap-northeast-2.amazonaws.com/ap-northeast-2_xxxxxxxx/.well-known/jwks.json

# Aurora PostgreSQL
DB_HOST=utterai-prod-aurora.cluster-xxxx.ap-northeast-2.rds.amazonaws.com
DB_PORT=5432
DB_NAME=utterai
DB_USER=utterai_app
DB_PASSWORD=${from_secrets_manager}
DB_POOL_SIZE=10
DB_MAX_OVERFLOW=20

# Redis
REDIS_HOST=utterai-prod-redis.xxxxxx.cache.amazonaws.com
REDIS_PORT=6379
REDIS_DB=0
REDIS_TLS_ENABLED=true
REDIS_AUTH_TOKEN=${from_secrets_manager}

# S3
S3_RAW_AUDIO_BUCKET=utterai-prod-raw-audio
S3_TEMPLATE_BUCKET=utterai-prod-template
S3_RAG_INGEST_BUCKET=utterai-prod-rag-ingest
S3_REPORT_BUCKET=utterai-prod-reports
S3_PRESIGNED_UPLOAD_EXPIRES_SECONDS=900
S3_PRESIGNED_DOWNLOAD_EXPIRES_SECONDS=300

# SQS
SQS_AUDIO_PREPROCESS_QUEUE_URL=https://sqs.ap-northeast-2.amazonaws.com/{ACCOUNT_ID}/utterai-prod-audio-preprocess-queue
SQS_GPU_INFERENCE_QUEUE_URL=https://sqs.ap-northeast-2.amazonaws.com/{ACCOUNT_ID}/utterai-prod-gpu-inference-queue
SQS_REPORT_ANALYSIS_QUEUE_URL=https://sqs.ap-northeast-2.amazonaws.com/{ACCOUNT_ID}/utterai-prod-report-analysis-queue
SQS_RAG_INGEST_QUEUE_URL=https://sqs.ap-northeast-2.amazonaws.com/{ACCOUNT_ID}/utterai-prod-rag-ingest-queue

# AI Service (클러스터 내부 통신)
AI_SERVICE_BASE_URL=http://ai-service.utterai-ai-api.svc.cluster.local:8000
INTERNAL_CALLBACK_TOKEN=${from_secrets_manager}

# 관측
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector.utterai-observability.svc.cluster.local:4318
CLOUDWATCH_LOG_GROUP=/aws/eks/utterai-prod/backend
```

---

## 18. DR (재해 복구) 구성 — Tokyo Warm Standby

### 18.1 DR 목표

```text
DR 유형: Warm Standby (평소에 최소 규모로 운영, 장애 시 빠르게 확장)
RTO 목표: 30분 이내
RPO 목표: 1분 이내 (Aurora Global DB 복제 지연 기준)
```

### 18.2 DR 구성 요소

| 리소스 | Seoul (Primary) | Tokyo (DR) | 전환 방식 |
|---|---|---|---|
| ALB | `utterai-prod-alb` | `utterai-dr-alb` | Route 53 Failover |
| EKS | `utterai-prod-eks` | `utterai-dr-eks` (최소 노드 Standby) | 수동 또는 자동 스케일 업 |
| Aurora | Writer + Reader | Aurora Global DB Secondary | Global DB Promote |
| S3 | Seoul 버킷 | S3 CRR 자동 복제 | 버킷 포인터 전환 |
| ECR | Seoul ECR | Tokyo ECR 자동 복사 | 이미지 자동 복제 |

### 18.3 Route 53 Failover

```text
Primary Record: api.utterai.com → Seoul ALB (Health Check 연결)
Secondary Record: api.utterai.com → Tokyo DR ALB (Failover)

Health Check:
- 대상: https://api.utterai.com/health/ready
- 간격: 30초
- 실패 임계값: 3회 연속 실패 시 Failover 발동
```

### 18.4 Aurora Global DB

```text
Primary Cluster: utterai-prod-aurora (ap-northeast-2)
Secondary Cluster: utterai-dr-aurora (ap-northeast-1)
복제 지연: 일반적으로 < 1초

장애 시 Promote 절차:
1. Tokyo Secondary를 Primary로 Promote
2. Tokyo EKS의 DB_HOST 환경변수를 Tokyo Writer Endpoint로 전환
3. Route 53 Failover 확인
```

### 18.5 S3 Cross-Region Replication

```text
복제 대상:
  utterai-prod-raw-audio    → utterai-dr-raw-audio (ap-northeast-1)
  utterai-prod-reports      → utterai-dr-reports (ap-northeast-1)
  utterai-prod-rag-ingest   → utterai-dr-rag-ingest (ap-northeast-1)

설정:
  - 복제 규칙: 신규 객체부터 적용
  - 암호화: KMS (DR 계정 KMS Key)
  - 삭제 마커 복제: 비활성화
```

---

## 19. 장애 대응 체계

### 19.1 장애 등급

| 등급 | 조건 | 대응 시간 |
|---|---|---|
| P1 (Critical) | 서비스 전면 불가, 데이터 손실 위험 | 즉시 (24/7) |
| P2 (High) | 주요 기능 장애 (업로드/분석 불가) | 1시간 내 |
| P3 (Medium) | 일부 기능 저하, 성능 저하 | 4시간 내 |
| P4 (Low) | 부분적 UX 저하, 알람만 발생 | 다음 근무일 |

### 19.2 롤백 절차

```text
1. Argo CD UI에서 이전 배포 버전 확인
2. 이전 이미지 태그로 Kubernetes Manifest 수정
3. Argo CD에서 Sync 실행
4. 배포 후 /health/ready 확인
5. CloudWatch 알람 정상화 확인
6. 장애 원인 분석 후 Post-mortem 작성
```

---

## 20. Prod 환경 운영 주의사항

```text
1. 직접 DB 접근 금지: Prod DB는 EKS Job 또는 Systems Manager를 통해서만 접근
2. 실제 데이터 다운로드 금지: 아동/보호자 개인정보, 음성 파일은 로컬 반출 금지
3. 배포 전 반드시 Dev 검증: Dev에서 정상 동작 확인 후 Prod 배포
4. Terraform 변경은 plan 검토 후 적용: 리소스 삭제 방지 규칙 반드시 확인
5. IAM Key 미사용: 모든 접근은 IRSA 또는 IAM Role 기반. 장기 Access Key 발급 금지
6. 알람 무시 금지: Discord 알람 발생 시 즉시 원인 파악 및 조치
7. KMS Key 삭제 금지: 암호화된 데이터 복호화 불가로 이어짐
```

---

## 21. Pod 보안 정책 (Pod Security Standards + Kyverno)

Dev 환경에서는 배포 YAML을 올바르게 작성하는 관행(컨벤션)에만 의존한다. Prod에서는 잘못된 설정이 배포되는 것을 시스템이 직접 막아야 한다. 이를 위해 두 계층을 적용한다.

```text
계층 1 — Pod Security Standards (Kubernetes 네이티브)
  Kubernetes API Server가 Namespace 레이블을 읽어 Pod 스펙을 직접 검사.
  Kyverno 없이도 동작하며 가장 기본적인 보안 기준선 역할을 한다.

계층 2 — Kyverno (정책 엔진)
  Kubernetes Admission Webhook으로 동작. Pod 생성/수정 요청을 API Server가
  etcd에 저장하기 직전에 Kyverno에게 전달하여 허용/거부/자동수정을 결정한다.
  PSS보다 세밀한 규칙과 자동 수정(Mutate), 리소스 자동 생성(Generate)이 가능하다.
```

### 21.1 Pod Security Standards (PSS) 레이블

Namespace에 레이블을 추가하는 것만으로 활성화된다. 별도 컴포넌트 설치가 필요 없다.

```text
enforce: 위반 Pod를 거부 (배포 자체가 실패)
warn:    위반 시 경고 메시지 반환 (배포는 됨)
audit:   위반 내역을 감사 로그에 기록 (배포는 됨)
```

**적용 레벨 기준**

| Namespace | enforce | 이유 |
|---|---|---|
| `utterai-prod-api` | `restricted` | 순수 API 서버. root 불필요, 최소 권한 강제 |
| `utterai-prod-ai-worker` | `baseline` | GPU 워크로드는 NVIDIA Device Plugin 특성상 `restricted` 적용 시 스케줄링 실패 가능. `warn=restricted`로 위반 감지는 유지 |

`restricted` 레벨이 강제하는 항목:
```text
- runAsNonRoot: true 필수
- allowPrivilegeEscalation: false 필수
- capabilities.drop: ["ALL"] 필수
- seccompProfile.type: RuntimeDefault 또는 Localhost 필수
- hostNetwork / hostPID / hostIPC: false 필수
```

실제 Namespace YAML (`k8s/apps/backend/overlays/prod/namespace.yaml`):
```yaml
labels:
  pod-security.kubernetes.io/enforce: restricted
  pod-security.kubernetes.io/warn: restricted
  pod-security.kubernetes.io/audit: restricted
```

### 21.2 Kyverno 설치

```bash
helm repo add kyverno https://kyverno.github.io/kyverno/
helm repo update

helm install kyverno kyverno/kyverno \
  --namespace kyverno \
  --create-namespace \
  --set replicaCount=3
```

> `replicaCount=3`: Kyverno가 죽으면 Admission Webhook이 응답하지 않아 Pod 배포 자체가 막힌다. Prod에서는 반드시 3개로 운영한다.

### 21.3 ClusterPolicy: resource limits 필수

limits 없는 Pod가 배포되면 GPU/CPU 노드가 메모리 초과로 OOM 종료될 수 있다.

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: require-resource-limits
spec:
  validationFailureAction: Enforce
  background: true
  rules:
  - name: check-container-limits
    match:
      any:
      - resources:
          kinds: [Pod]
          namespaces:
          - utterai-prod-api
          - utterai-prod-ai-worker
    validate:
      message: "resources.limits.cpu 와 resources.limits.memory 는 필수입니다"
      pattern:
        spec:
          containers:
          - resources:
              limits:
                memory: "?*"
                cpu: "?*"
```

### 21.4 ClusterPolicy: latest 태그 금지

`:latest` 태그는 어떤 이미지가 실행 중인지 추적이 불가능하다. Prod 배포는 항상 git SHA 태그를 사용해야 한다.

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: disallow-latest-tag
spec:
  validationFailureAction: Enforce
  background: true
  rules:
  - name: check-image-tag
    match:
      any:
      - resources:
          kinds: [Pod]
          namespaces:
          - utterai-prod-api
          - utterai-prod-ai-worker
    validate:
      message: "이미지 태그에 :latest 는 사용할 수 없습니다. git SHA 태그를 사용하세요"
      foreach:
      - list: "request.object.spec.containers"
        deny:
          conditions:
            any:
            - key: "{{ element.image }}"
              operator: Equals
              value: "*:latest"
            - key: "{{ element.image }}"
              operator: NotContains
              value: ":"
```

### 21.5 ClusterPolicy: GPU 요청은 ai-worker namespace만

GPU On-Demand 노드는 비용이 높다. 잘못된 namespace에서 `nvidia.com/gpu` 리소스를 요청하면 불필요한 GPU 노드가 프로비저닝된다.

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: restrict-gpu-to-ai-worker
spec:
  validationFailureAction: Enforce
  background: true
  rules:
  - name: check-gpu-namespace
    match:
      any:
      - resources:
          kinds: [Pod]
    exclude:
      any:
      - resources:
          namespaces:
          - utterai-prod-ai-worker
          - kube-system
    validate:
      message: "nvidia.com/gpu 리소스는 utterai-prod-ai-worker namespace에서만 요청할 수 있습니다"
      deny:
        conditions:
          any:
          - key: "{{ request.object.spec.containers[].resources.limits.\"nvidia.com/gpu\" | length(@) }}"
            operator: GreaterThan
            value: "0"
```

### 21.6 ClusterPolicy: NetworkPolicy 자동 생성

Namespace 생성 시 기본 차단 NetworkPolicy를 자동으로 함께 생성한다. 이후 필요한 통신만 명시적으로 허용하는 화이트리스트 방식으로 운영한다.

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: generate-default-networkpolicy
spec:
  rules:
  - name: default-deny
    match:
      any:
      - resources:
          kinds: [Namespace]
          selector:
            matchLabels:
              utterai.io/environment: prod
    generate:
      apiVersion: networking.k8s.io/v1
      kind: NetworkPolicy
      name: default-deny-all
      namespace: "{{ request.object.metadata.name }}"
      synchronize: true
      data:
        spec:
          podSelector: {}
          policyTypes:
          - Ingress
          - Egress
```

> `synchronize: true`: Namespace가 삭제 후 재생성되어도 Kyverno가 NetworkPolicy를 다시 생성한다.

### 21.7 정책 상태 확인

```bash
# 모든 ClusterPolicy 상태 확인
kubectl get clusterpolicy

# 특정 Policy가 실제로 차단한 내역 확인
kubectl get policyreport -A

# 특정 Pod에 대한 정책 검사 결과
kubectl describe policyreport -n utterai-prod-api
```

### 21.8 정책 단계적 적용 순서

신규 클러스터에 바로 `Enforce` 모드로 적용하면 기존 배포가 모두 막힌다. 아래 순서로 단계적으로 적용한다.

```text
1단계 — Audit 모드로 설치
  validationFailureAction: Audit
  실제 배포는 막지 않고 위반 내역만 policyreport에 기록.
  1~2일간 위반 현황을 파악한다.

2단계 — 위반 수정
  policyreport를 확인하여 위반 중인 Deployment를 수정한다.
  (resource limits 추가, 이미지 태그 수정 등)

3단계 — Enforce 모드로 전환
  validationFailureAction: Enforce
  이후 위반 배포는 API Server 단계에서 거부된다.
```

---

## 22. Cilium (CNI + 네트워크 보안)

### 22.1 선택 이유

Kubernetes 기본 NetworkPolicy는 IP/포트 기반 L3/L4 제어만 가능하고, AWS VPC CNI는 NetworkPolicy를 직접 지원하지 않아 별도 플러그인이 필요하다. Cilium은 eBPF 기반으로 커널 레벨에서 동작해 사이드카 없이 아래를 모두 제공한다.

```text
- L3/L4/L7 NetworkPolicy (HTTP path, DNS 기반 제어 포함)
- Pod 간 mTLS (Mutual Authentication)
- Hubble — 실시간 트래픽 가시성 UI
- sidecar 없음 → GPU Pod에 영향 없음
- VPC CNI 대체 → 단일 CNI로 통합
```

### 22.2 EKS 설치 방식 선택

Cilium은 EKS에서 두 가지 방식으로 설치할 수 있다.

| 방식 | 설명 | 권장 시점 |
|---|---|---|
| ENI 모드 (VPC CNI 완전 교체) | Cilium이 직접 ENI를 관리. 기능 풀 사용 가능 | **Prod 첫 구성 시 (권장)** |
| Chaining 모드 (VPC CNI 위에 추가) | VPC CNI와 공존. 덜 침습적이나 기능 제한 | 운영 중인 클러스터에 후추가 시 |

> Prod 클러스터를 처음 구성하는 시점에는 ENI 모드로 시작하는 것이 가장 좋다. 운영 중인 클러스터에서 CNI를 교체하려면 노드 드레인 및 재생성이 필요해 부담이 크다.

### 22.3 설치 (EKS ENI 모드)

EKS 클러스터 생성 시 `vpc-cni` 애드온을 비활성화하거나, Terraform에서 `cluster_addons`에서 vpc-cni를 제외한 뒤 Cilium을 설치한다.

```bash
helm repo add cilium https://helm.cilium.io/
helm repo update

helm install cilium cilium/cilium \
  --namespace kube-system \
  --set eni.enabled=true \
  --set ipam.mode=eni \
  --set egressMasqueradeInterfaces=eth0 \
  --set tunnel=disabled \
  --set nodeinit.enabled=true \
  --set hubble.relay.enabled=true \
  --set hubble.ui.enabled=true \
  --set kubeProxyReplacement=true \
  --set k8sServiceHost=<EKS_API_ENDPOINT> \
  --set k8sServicePort=443
```

> `<EKS_API_ENDPOINT>`: Terraform output `eks_cluster_endpoint` 값을 사용한다.

설치 후 상태 확인:
```bash
cilium status --wait
cilium connectivity test
```

### 22.4 CiliumNetworkPolicy — UtterAI 트래픽 규칙

Kyverno가 Namespace 생성 시 기본 `default-deny-all` NetworkPolicy를 생성한다 (섹션 21.6). 그 위에 아래 허용 규칙을 추가해 필요한 통신만 열어준다.

**utterai-prod-api: 허용 트래픽**

```yaml
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: utterai-prod-api-policy
  namespace: utterai-prod-api
specs:
- endpointSelector:
    matchLabels:
      app: utterai-api
  ingress:
  # ALB에서 인입되는 트래픽 허용
  - fromEntities:
    - world
    toPorts:
    - ports:
      - port: "8080"
        protocol: TCP
  egress:
  # Aurora 접근 허용
  - toCIDRSet:
    - cidr: 10.0.21.0/24
    - cidr: 10.0.22.0/24
    - cidr: 10.0.23.0/24
    toPorts:
    - ports:
      - port: "5432"
        protocol: TCP
  # Redis 접근 허용
  - toCIDRSet:
    - cidr: 10.0.21.0/24
    - cidr: 10.0.22.0/24
    - cidr: 10.0.23.0/24
    toPorts:
    - ports:
      - port: "6379"
        protocol: TCP
  # SQS VPC Endpoint 접근 허용
  - toEntities:
    - aws
    toPorts:
    - ports:
      - port: "443"
        protocol: TCP
  # OTEL Collector 접근 허용
  - toEndpoints:
    - matchLabels:
        app: otel-collector
        io.kubernetes.pod.namespace: utterai-observability
    toPorts:
    - ports:
      - port: "4318"
        protocol: TCP
  # DNS 허용 (모든 egress 정책 적용 시 필수)
  - toEndpoints:
    - matchLabels:
        io.kubernetes.pod.namespace: kube-system
        k8s-app: kube-dns
    toPorts:
    - ports:
      - port: "53"
        protocol: UDP
      rules:
        dns:
        - matchPattern: "*"
```

**utterai-prod-ai-worker: 허용 트래픽**

```yaml
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: utterai-prod-ai-worker-policy
  namespace: utterai-prod-ai-worker
specs:
- endpointSelector:
    matchLabels:
      app.kubernetes.io/part-of: utterai
  egress:
  # SQS VPC Endpoint 접근 허용 (큐 소비/생산)
  - toEntities:
    - aws
    toPorts:
    - ports:
      - port: "443"
        protocol: TCP
  # S3 VPC Endpoint 접근 허용 (음성 파일 읽기/쓰기)
  - toEntities:
    - aws
    toPorts:
    - ports:
      - port: "443"
        protocol: TCP
  # Bedrock 접근 허용 (CPU Worker → 리포트 생성)
  - toFQDNs:
    - matchPattern: "bedrock.ap-northeast-2.amazonaws.com"
    toPorts:
    - ports:
      - port: "443"
        protocol: TCP
  # HuggingFace 모델 다운로드 허용 (초기 기동 시)
  - toFQDNs:
    - matchPattern: "*.huggingface.co"
    toPorts:
    - ports:
      - port: "443"
        protocol: TCP
  # OTEL Collector 접근 허용
  - toEndpoints:
    - matchLabels:
        app: otel-collector
        io.kubernetes.pod.namespace: utterai-observability
    toPorts:
    - ports:
      - port: "4318"
        protocol: TCP
  # DNS 허용
  - toEndpoints:
    - matchLabels:
        io.kubernetes.pod.namespace: kube-system
        k8s-app: kube-dns
    toPorts:
    - ports:
      - port: "53"
        protocol: UDP
      rules:
        dns:
        - matchPattern: "*"
```

> `toFQDNs`: Cilium의 DNS 기반 egress 제어. 기본 Kubernetes NetworkPolicy에는 없는 기능으로, IP가 아닌 도메인 이름으로 외부 접근을 제어한다.

### 22.5 mTLS (Mutual Authentication)

Cilium 1.14 이상에서 Pod 간 mTLS를 지원한다. 인증서 관리를 Cilium이 담당하므로 Istio처럼 별도 인증서 배포 작업이 필요 없다.

```bash
# Cilium 설치 시 mTLS 활성화 옵션 추가
helm upgrade cilium cilium/cilium \
  --namespace kube-system \
  --reuse-values \
  --set authentication.mutual.spire.enabled=true \
  --set authentication.mutual.spire.install.enabled=true
```

활성화 후 특정 namespace 간 통신에 mTLS 강제:

```yaml
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: require-mtls-api-to-ai
  namespace: utterai-prod-ai-worker
specs:
- endpointSelector: {}
  ingress:
  - fromEndpoints:
    - matchLabels:
        io.kubernetes.pod.namespace: utterai-prod-api
    authentication:
      mode: required   # mTLS 인증 없으면 차단
```

### 22.6 Hubble — 트래픽 가시성

Hubble은 Cilium의 관측 도구로, 클러스터 내 모든 트래픽 흐름을 실시간으로 시각화한다.

```bash
# Hubble CLI 설치
cilium hubble enable --ui

# 포트 포워딩으로 UI 접근
cilium hubble ui
# → http://localhost:12000

# CLI로 실시간 트래픽 확인
hubble observe --namespace utterai-prod-api --follow

# 특정 Pod 간 트래픽만 필터
hubble observe \
  --from-label app=utterai-api \
  --to-label app=utterai-ai-worker \
  --follow
```

Hubble UI에서 확인할 수 있는 항목:
```text
- Namespace 간 트래픽 흐름 다이어그램
- 드롭된 패킷 및 차단 이유
- HTTP 요청/응답 상태 코드 (L7 가시성 활성화 시)
- DNS 쿼리 내역
```

### 22.7 상태 확인 명령어

```bash
# Cilium 전체 상태
cilium status

# 모든 노드의 Cilium Agent 상태
kubectl get pods -n kube-system -l k8s-app=cilium

# NetworkPolicy 적용 현황
kubectl get ciliumnetworkpolicy -A

# 특정 Pod에 적용된 정책 확인
kubectl exec -n kube-system <cilium-pod> -- \
  cilium endpoint list

# 트래픽 드롭 확인
hubble observe --verdict DROPPED --follow
```

---

## 23. Terraform 시크릿 관리 현황 및 고도화

### 23.1 현재 방식별 상태

| 시크릿 | Terraform 관리 방식 | State 노출 여부 |
|---|---|---|
| Aurora DB 비밀번호 | `manage_master_user_password = true` (AWS 자동 관리) | 없음 (안전) |
| Redis AUTH 토큰 | `random_password` 리소스 생성 후 Secrets Manager에 저장 | **있음 (위험)** |
| backend-api-secret | 빈 시크릿 껍데기 생성 → 수동 CLI 주입 | 없음 |
| ai-worker-secret | 빈 시크릿 껍데기 생성 → 수동 CLI 주입 | 없음 |
| gpu-worker-secret | 빈 시크릿 껍데기 생성 → 수동 CLI 주입 | 없음 |

**현재 위험 지점**: `terraform/modules/redis/main.tf`의 `random_password` 리소스 결과값이 S3 state 파일에 평문으로 저장된다.

```hcl
# 현재 방식 — Redis 토큰이 .tfstate에 평문 노출
resource "random_password" "redis_auth" {
  length  = 32
  special = false
}

resource "aws_elasticache_replication_group" "this" {
  auth_token = random_password.redis_auth.result  # ← state에 저장됨
}
```

### 23.2 Mozilla SOPS

SOPS(Secrets OPerationS)는 YAML/JSON/ENV 파일을 KMS/age/PGP 키로 암호화하여 Git에 커밋할 수 있게 하는 도구다.

```text
적용 패턴:
  secrets.enc.yaml (암호화 커밋) → SOPS + KMS → 평문 값 복호화

Terraform 연동:
  data "sops_file" "secrets" { source_file = "secrets.enc.yaml" }
  → terraform-provider-sops 사용
```

**현재 프로젝트에서의 평가**

- `terraform.tfvars`에 민감 정보가 없음 (AZ, CIDR 등 비민감 설정만 포함)
- 실제 시크릿은 이미 AWS Secrets Manager 경유 (ESO → K8s Secret 흐름 확립)
- Git에 암호화된 시크릿을 저장할 필요가 없는 현재 구조에서 도입 실익이 낮음

> SOPS는 Helm values 파일에 시크릿을 포함하거나, 팀 간 `.env` 파일을 Git으로 공유해야 하는 경우에 유효하다.

### 23.3 Terraform Ephemeral Resources (고도화 권장)

Terraform 1.10+에서 도입된 `ephemeral` 리소스는 plan/apply 시 값을 메모리에서만 사용하고 `.tfstate`에 저장하지 않는다.

**Redis 토큰 state 노출 문제 해결 방향**

```hcl
# 고도화 방향 — Redis 토큰을 state에 저장하지 않는 방식
# Terraform 1.10+ 필요

# 1. Secrets Manager에서 외부 주입된 토큰을 ephemeral로 읽기
ephemeral "aws_secretsmanager_secret_version" "redis_auth" {
  secret_id = aws_secretsmanager_secret.redis_auth.id
}

# 2. ElastiCache 리소스에서 ephemeral 값 참조 (state 미기록)
resource "aws_elasticache_replication_group" "this" {
  auth_token = ephemeral.aws_secretsmanager_secret_version.redis_auth.secret_string
}
```

| 항목 | 현재 방식 | Ephemeral 적용 후 |
|---|---|---|
| Redis 토큰 저장 위치 | S3 state 파일 (평문) | 메모리 only (state 미기록) |
| state 파일 유출 시 | 토큰 즉시 노출 | 영향 없음 |
| Terraform 버전 요건 | 제한 없음 | 1.10 이상 |

### 23.4 고도화 우선순위

| 방법 | 해결 문제 | 적용 난이도 | 권장 시점 |
|---|---|---|---|
| **Terraform Ephemeral** | Redis 토큰 state 노출 | Terraform 버전 업 + 리소스 타입 변경 | Terraform 1.10 업그레이드 시 즉시 |
| **SOPS** | Git 암호화 시크릿 버전 관리 | 팀 키 배포 + provider 추가 | 현 구조에서 필요성 낮음 |

> 현재 가장 실질적인 보안 개선은 Terraform 1.10으로 업그레이드 후 `modules/redis/main.tf`의 `random_password`를 ephemeral 리소스로 전환하는 것이다.

---

## 참고

- EKS 아키텍처 상세 설계: [`docs/shared/eks-architecture-flow.md`](../shared/eks-architecture-flow.md)
- Dev vs Prod 환경 비교: [`docs/README.md`](../README.md)
- Dev 환경 구현 가이드: [`docs/dev/README.md`](../dev/README.md)
- 브랜치/커밋 규칙: [`CONTRIBUTING.md`](../../CONTRIBUTING.md)
