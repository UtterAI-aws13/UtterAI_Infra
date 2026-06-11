# UtterAI Prod 환경 인프라 가이드

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
- Prod와 Dev는 AWS 계정 수준에서 분리 권장
```

### 1.3 환경 식별

| 항목 | 값 |
|---|---|
| 환경 이름 | `prod` |
| AWS Region | `ap-northeast-2` |
| AWS Account | Dev와 분리된 별도 계정 |
| EKS Namespace | `utterai-prod` |
| 리소스 Prefix | `utterai-prod-` |

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

Prod ALB 앞에 WAF를 배치한다.

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
| 배포 Namespace | `utterai-prod` |

> Prod에서는 Control Plane Public Endpoint를 차단하고 kubectl 접근은 VPN 또는 Bastion 경유

### 4.2 NodeGroup 구성

다이어그램 기준 4개의 NodeGroup으로 역할을 분리하되, 안정적인 워크로드(System, API)는 EKS Managed NodeGroup으로, 동적 워크로드(CPU/GPU Worker)는 Karpenter NodePool로 관리하는 **하이브리드 구조**를 사용한다.

| NodeGroup | 관리 방식 | 인스턴스 타입 | 노드 수 | 용도 |
|---|---|---|---:|---|
| `prod-system-nodegroup` | EKS Managed NodeGroup | `t3.large` | 2 ~ 3 | Kubernetes 시스템 컴포넌트 (CoreDNS, ALB Controller, Karpenter 등) |
| `prod-api-nodegroup` | EKS Managed NodeGroup + HPA | `t3.xlarge` | 2 ~ 5 | 백엔드 API Pod |
| `cpu-worker-nodepool` | **Karpenter NodePool** + KEDA | `c5.2xlarge`, `c5.4xlarge` 자동 선택 | 0 ~ 4 | AI CPU 처리 (ASR, 전처리) |
| `gpu-worker-nodepool` | **Karpenter NodePool** + KEDA | `g4dn.xlarge`, `g4dn.2xlarge` 자동 선택 | 0 ~ 3 | AI GPU 처리 (Diarization, 추론) |

> System / API NodeGroup: On-Demand, 항상 실행
>
> CPU / GPU Worker: Karpenter가 Pod Pending 감지 시 EC2 자동 기동, 작업 완료 후 자동 종료
>
> 스케일링 역할 분리: KEDA(Pod 수) + Karpenter(노드 수) → 섹션 4.4, 4.5 참고

### 4.3 HPA (Horizontal Pod Autoscaler)

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: backend-api-hpa
  namespace: utterai-prod
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

### 4.4 KEDA (Queue-based Autoscaler)

KEDA는 SQS Analysis Queue의 메시지 수를 기반으로 CPU/GPU Worker Pod를 자동 스케일링한다.

```text
HPA와의 차이:
- HPA: CPU/Memory 사용률 기반 스케일링 (백엔드 API에 적용)
- KEDA: SQS 큐 메시지 수 기반 스케일링 (Worker NodeGroup에 적용)

동작 방식:
1. 분석 요청이 SQS에 쌓임
2. KEDA가 큐 메시지 수를 감지
3. CPU Worker Pod 수를 자동으로 증가
4. CPU Worker가 GPU Worker에 작업 분배
5. 큐가 비워지면 Pod 수 자동 감소
```

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: cpu-worker-scaledobject
  namespace: utterai-prod
spec:
  scaleTargetRef:
    name: cpu-worker
  minReplicaCount: 1
  maxReplicaCount: 4
  triggers:
    - type: aws-sqs-queue
      metadata:
        queueURL: https://sqs.ap-northeast-2.amazonaws.com/{ACCOUNT_ID}/utterai-prod-analysis-queue
        queueLength: "5"
        awsRegion: ap-northeast-2
        identityOwner: operator
```

> `queueLength: "5"` → 큐 메시지 5개당 Pod 1개 기준으로 스케일링
>
> KEDA가 Pod를 늘리면 → 노드가 부족 → Karpenter가 EC2를 프로비저닝 (섹션 4.5 참고)

### 4.5 Karpenter (Node Provisioner)

Karpenter는 KEDA가 생성한 `Pending` 상태의 Worker Pod를 감지하고, Pod의 리소스 요청에 맞는 EC2 인스턴스를 자동으로 기동한다.

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

**Karpenter NodePool (GPU Worker)**

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
  limits:
    cpu: 64
    memory: 256Gi
  disruption:
    consolidationPolicy: WhenEmpty
    consolidateAfter: 30s
```

**EC2NodeClass (GPU Worker)**

```yaml
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: gpu-worker-class
spec:
  amiFamily: AL2
  role: karpenter-node-role
  subnetSelectorTerms:
    - tags:
        karpenter.sh/discovery: utterai-prod-eks
  securityGroupSelectorTerms:
    - tags:
        karpenter.sh/discovery: utterai-prod-eks
  tags:
    Name: karpenter-gpu-worker
    Environment: prod
```

> Karpenter 자체는 `prod-system-nodegroup`에 배포한다. Karpenter가 죽으면 Worker 노드 프로비저닝이 불가능하므로 System NodeGroup의 가용성이 전제되어야 한다

### 4.6 PodDisruptionBudget

롤링 배포 중 서비스 중단을 방지한다.

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: backend-api-pdb
  namespace: utterai-prod
spec:
  minAvailable: 2
  selector:
    matchLabels:
      app: backend-api
```

### 4.7 백엔드 Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: backend-api
  namespace: utterai-prod
spec:
  replicas: 3
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
  selector:
    matchLabels:
      app: backend-api
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

### 4.8 IRSA 구성

```text
IAM Role: utterai-prod-backend-api-role

허용 권한 (prod 버킷/큐/시크릿 한정):
- s3:GetObject, PutObject, HeadObject, DeleteObject
- s3:ListBucket (특정 prefix 한정)
- sqs:SendMessage
- secretsmanager:GetSecretValue
- kms:Decrypt (prod-aurora-kms-key 한정)
- cloudwatch:PutMetricData
```

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: backend-api-sa
  namespace: utterai-prod
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::{ACCOUNT_ID}:role/utterai-prod-backend-api-role
```

---

## 5. Aurora PostgreSQL 구성

### 5.1 기본 설정

| 항목 | 값 |
|---|---|
| 클러스터 식별자 | `utterai-prod-aurora` |
| 엔진 | Aurora PostgreSQL 16 |
| Writer 인스턴스 타입 | `db.r6g.large` |
| Reader 인스턴스 타입 | `db.r6g.large` |
| 인스턴스 수 | Writer 1 + Reader 1 (필요 시 Reader 추가) |
| 배포 | Multi-AZ |
| 백업 보존 기간 | 7일 |
| 자동 마이너 버전 업그레이드 | 비활성화 (수동 제어) |
| 암호화 | KMS (prod-aurora-kms-key) |
| Performance Insights | 활성화 |
| Enhanced Monitoring | 활성화 (60초 간격) |

### 5.2 접속 정보

```text
Writer Endpoint: utterai-prod-aurora.cluster-xxxx.ap-northeast-2.rds.amazonaws.com
Reader Endpoint: utterai-prod-aurora.cluster-ro-xxxx.ap-northeast-2.rds.amazonaws.com
Port: 5432
DB Name: utterai
User: utterai_app
Password: Secrets Manager에서 관리
```

### 5.3 Connection 관리

Prod에서는 Pod 수가 늘어날 때 DB Connection 과부하를 방지한다.

```text
SQLAlchemy Pool 설정:
- DB_POOL_SIZE=10
- DB_MAX_OVERFLOW=20
- 최대 Connection = Pod 수 × (POOL_SIZE + MAX_OVERFLOW)
- Pod 10개일 경우 최대 300 Connection

Aurora max_connections = db.r6g.large 기준 약 1500
Connection이 80%를 넘으면 RDS Proxy 도입 검토
```

### 5.4 DB 마이그레이션 정책

```text
- 운영 시간 외(새벽 2~4시) 마이그레이션 실행
- 마이그레이션 전 스냅샷 생성 필수
- Alembic --sql 옵션으로 SQL 사전 검토 후 실행
- DDL 변경(컬럼 추가/삭제)은 반드시 무중단 방식으로 설계
```

---

## 6. ElastiCache Redis 구성

### 6.1 기본 설정

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

### 6.2 접속 정보

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
| `utterai-prod-processed-audio` | AI 처리 중간 음성 | 비공개 |
| `utterai-prod-documents` | RAG 기준 문서 | 비공개 |
| `utterai-prod-reports` | 분석 결과 리포트 | 비공개 |
| `utterai-prod-artifacts` | 분석 JSON 결과 | 비공개 |

### 7.2 버킷 공통 설정

```text
- 퍼블릭 액세스 차단: 전체 활성화
- 버전 관리: 활성화 (raw-audio, reports)
- 서버 사이드 암호화: AWS Managed Key (aws/s3)
- 액세스 로깅: 활성화
- 객체 수명 주기:
    raw-audio: 90일 후 Glacier로 이동, 1년 후 삭제
    artifacts: 1년 후 Glacier 이동
    processed-audio: 30일 후 삭제
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

| 큐 이름 | 타입 | 용도 |
|---|---|---|
| `utterai-prod-analysis-queue` | Standard | 분석 요청 메시지 |
| `utterai-prod-analysis-dlq` | Standard | 실패한 분석 요청 보관 |

### 8.2 큐 설정

```text
utterai-prod-analysis-queue:
- 메시지 보존 기간: 4일
- 가시성 타임아웃: 300초 (AI 분석 예상 최대 시간)
- 최대 메시지 크기: 256KB
- 암호화: AWS Managed Key (`aws/sqs`)
- DLQ: utterai-prod-analysis-dlq (maxReceiveCount: 3)

utterai-prod-analysis-dlq:
- 메시지 보존 기간: 14일
- DLQ 메시지 발생 시 즉시 알람 발생
```

---

## 9. Cognito 구성

### 9.1 User Pool

| 항목 | 값 |
|---|---|
| User Pool 이름 | `utterai-prod-user-pool` |
| Region | `ap-northeast-2` |
| 로그인 방식 | 이메일 |
| MFA | 선택적 (TOTP 지원) |
| 비밀번호 정책 | 최소 12자, 대소문자/숫자/특수문자 포함 |
| 이메일 인증 | 필수 |
| 고급 보안 | 활성화 (이상 로그인 감지) |
| 사용자 계정 복구 | 이메일 기반 |

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

### 10.1 Secret 목록

| Secret 이름 | 저장 내용 | 교체 주기 |
|---|---|---|
| `utterai-prod/db-password` | Aurora 비밀번호 | 90일 자동 교체 |
| `utterai-prod/redis-auth-token` | Redis AUTH 토큰 | 수동 관리 |
| `utterai-prod/internal-service-token` | 백엔드-AI 서버 내부 인증 토큰 | 수동 관리 |

> 모든 Prod Secret은 AWS Managed Key (`aws/secretsmanager`) 로 암호화

### 10.2 EKS에서 주입 방식

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: backend-api-secret
  namespace: utterai-prod
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: aws-secrets-store
    kind: ClusterSecretStore
  target:
    name: backend-api-secret
  data:
    - secretKey: DB_PASSWORD
      remoteRef:
        key: utterai-prod/db-password
    - secretKey: INTERNAL_SERVICE_TOKEN
      remoteRef:
        key: utterai-prod/internal-service-token
    - secretKey: REDIS_AUTH_TOKEN
      remoteRef:
        key: utterai-prod/redis-auth-token
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

## 12. CloudWatch / 모니터링 구성

### 12.1 로그 그룹

| 로그 그룹 | 보존 기간 | 암호화 |
|---|---|---|
| `/aws/eks/utterai-prod/backend` | 30일 | AWS Managed Key (`aws/logs`) |
| `/aws/eks/utterai-prod/ai-service` | 30일 | AWS Managed Key (`aws/logs`) |
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
| `prod-sqs-dlq-count` | DLQ 메시지 수 > 0 | Discord + 온콜 |
| `prod-sqs-queue-depth` | 큐 메시지 수 > 500 (5분) | Discord |
| `prod-ai-callback-failure` | AI Callback 실패 수 > 5 (5분) | Discord |

### 12.3 대시보드

CloudWatch 대시보드 `utterai-prod-overview`에 아래 항목을 포함한다.

```text
- 백엔드 API Request Count / Error Rate / p95 Latency
- Aurora CPU / 연결 수 / Replica Lag
- Redis CPU / Memory / Hit Rate
- SQS 큐 깊이 / DLQ 수
- AI Callback 성공/실패 수
- EKS Node CPU / Memory
```

### 12.4 OpenTelemetry Trace

```text
Collector: OpenTelemetry Collector (EKS 내 DaemonSet)
Backend: Grafana Tempo (Shared Tooling Account에 위치)
Sampling: 운영 중 10%, 에러 100%
```

### 12.5 Grafana

Grafana는 Shared Tooling Account에 배포되어 Prod / Dev 두 환경의 메트릭을 통합 시각화한다.

```text
위치: Shared Tooling Account
데이터 소스:
  - CloudWatch (메트릭 / 로그)
  - OpenTelemetry (Trace)
  - Aurora Performance Insights (DB 쿼리 분석)

주요 대시보드:
  - utterai-prod-overview (서비스 전체 현황)
  - utterai-prod-ai-pipeline (AI 분석 파이프라인 처리량)
  - utterai-prod-db (Aurora / Redis 상세)
```

---

## 13. CloudFront 구성

### 13.1 프론트엔드 배포

```text
Origin: S3 utterai-prod-frontend (OAC 사용, 직접 접근 차단)
Distribution: utterai-prod-frontend-cf
도메인: utterai.com, www.utterai.com
HTTPS: ACM 인증서 (us-east-1 발급 필수)
캐시 정책: 정적 파일 1년 캐시, HTML은 캐시 없음
```

### 13.2 백엔드 API 라우팅 (옵션)

CloudFront에서 경로 기반 라우팅을 사용할 경우:

```text
utterai.com/api/* → ALB (백엔드)
utterai.com/*    → S3 (프론트엔드)
```

경로 분리가 복잡할 경우 `api.utterai.com`을 ALB에 직접 연결하는 방식이 더 단순하다.

---

## 14. Shared Tooling Account

GitHub, ECR, Argo CD, Terraform, CloudWatch, Grafana는 Prod Account와 분리된 **별도 AWS 계정(Shared Tooling Account)**에서 관리한다.

```text
이유:
- CI/CD 도구가 침해되더라도 Prod 데이터에 직접 접근 불가
- 여러 환경(Dev / Prod)이 하나의 Tooling 계정을 공유하여 운영 효율화
- IAM 권한 경계를 Account 수준에서 분리
```

| 리소스 | 설명 |
|---|---|
| GitHub | 소스 코드 및 Kubernetes Manifest 저장 |
| GitHub Actions | CI 파이프라인 (테스트, 빌드, ECR Push) |
| Amazon ECR | Docker 이미지 저장소 (Seoul + Tokyo 복제) |
| Argo CD | GitOps 배포 도구 (Prod Account EKS를 대상으로 배포) |
| Terraform | 인프라 코드 실행 및 State 관리 |
| CloudWatch | 메트릭 및 로그 수집 (Cross-Account) |
| Grafana | 통합 모니터링 대시보드 |

### Cross-Account 접근

Argo CD가 Prod Account EKS에 배포하려면 Cross-Account IAM Role이 필요하다.

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
    |
    v
GitHub Actions (prod-deploy.yaml) — Shared Tooling Account
    |
    |-- 단위 테스트
    |-- 통합 테스트
    |-- Docker Build
    |-- ECR Push (utterai-prod-backend:{git_sha})  ← Seoul ECR
    |-- ECR Cross-Region Copy                       ← Tokyo ECR (DR용)
    |-- 이미지 취약점 스캔 (ECR Scanning)
    |-- Kubernetes Manifest 이미지 태그 업데이트
    v
Argo CD (prod 클러스터 감시) — Shared Tooling Account
    |
    |-- Sync 전 Diff 검토 (수동 Sync 권장)
    v
EKS utterai-prod Namespace 배포 (Rolling Update)  ← Prod Account
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

### 15.4 GitHub Actions 환경변수 (Repository Secrets)

```text
PROD_AWS_ACCESS_KEY_ID
PROD_AWS_SECRET_ACCESS_KEY
PROD_ECR_URI
PROD_EKS_CLUSTER_NAME
```

### 15.5 브랜치 보호 규칙 (main)

```text
- PR 리뷰 최소 1명 승인 필수
- CI 통과 필수
- Force Push 금지
- 브랜치 삭제 금지
```

---

## 16. Terraform 구조

Prod 인프라는 `terraform/environments/prod/` 디렉터리에서 관리한다.

```text
terraform/
├── modules/
│   ├── vpc/
│   ├── eks/
│   ├── aurora/
│   ├── redis/
│   ├── s3/
│   ├── sqs/
│   ├── cognito/
│   ├── kms/
│   ├── waf/
│   └── iam/
└── environments/
    └── prod/
        ├── main.tf
        ├── variables.tf
        ├── outputs.tf
        └── terraform.tfvars
```

### 16.1 Terraform 상태 관리

```text
Backend: S3 (utterai-prod-terraform-state)
Lock: DynamoDB (utterai-prod-terraform-lock)
암호화: KMS
```

### 16.2 Terraform 적용 정책

```text
- terraform plan 결과를 PR에 포함 (Atlantis 또는 GitHub Actions)
- terraform apply는 리뷰 승인 후 실행
- Prod 리소스 삭제는 수동으로만 허용 (lifecycle.prevent_destroy 설정)
```

### 16.3 주요 Terraform Output

```hcl
output "aurora_writer_endpoint" {}
output "aurora_reader_endpoint" {}
output "redis_endpoint" {}
output "raw_audio_bucket_name" {}
output "report_bucket_name" {}
output "analysis_queue_url" {}
output "backend_api_role_arn" {}
output "cognito_user_pool_id" {}
output "cognito_client_id" {}
output "alb_dns_name" {}
output "cloudfront_distribution_id" {}
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
FRONTEND_ORIGIN=https://utterai.com
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
S3_PROCESSED_AUDIO_BUCKET=utterai-prod-processed-audio
S3_DOCUMENT_BUCKET=utterai-prod-documents
S3_REPORT_BUCKET=utterai-prod-reports
S3_ARTIFACT_BUCKET=utterai-prod-artifacts
S3_PRESIGNED_UPLOAD_EXPIRES_SECONDS=900
S3_PRESIGNED_DOWNLOAD_EXPIRES_SECONDS=300

# SQS
SQS_ANALYSIS_QUEUE_URL=https://sqs.ap-northeast-2.amazonaws.com/{ACCOUNT_ID}/utterai-prod-analysis-queue
SQS_ANALYSIS_DLQ_URL=https://sqs.ap-northeast-2.amazonaws.com/{ACCOUNT_ID}/utterai-prod-analysis-dlq

# AI Service
AI_SERVICE_BASE_URL=http://ai-service.utterai-prod.svc.cluster.local:8000
INTERNAL_SERVICE_TOKEN=${from_secrets_manager}

# 관측
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector.observability.svc.cluster.local:4317
CLOUDWATCH_LOG_GROUP=/aws/eks/utterai-prod/backend
```

---

## 18. DR (재해 복구) 구성 — Tokyo Warm Standby

다이어그램 기준 Tokyo(ap-northeast-1) Region에 Warm Standby DR을 구성한다.

### 18.1 DR 구성 개요

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
| S3 | Seoul 버킷 | S3 CRR (Cross-Region Replication) 자동 복제 | 버킷 포인터 전환 |
| ECR | `utterai-prod-ecr` (Seoul) | `utterai-prod-ecr` (Tokyo Copy) | 이미지 자동 복제 |

### 18.3 Route 53 Failover 설정

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
복제 대상 버킷:
  utterai-prod-raw-audio    → utterai-dr-raw-audio (ap-northeast-1)
  utterai-prod-reports      → utterai-dr-reports (ap-northeast-1)
  utterai-prod-artifacts    → utterai-dr-artifacts (ap-northeast-1)

복제 설정:
  - 복제 규칙: 전체 객체 / 신규 객체부터 적용
  - 암호화: KMS (DR 계정 KMS Key)
  - 삭제 마커 복제: 비활성화
```

### 18.6 ECR Cross-Region Copy

```text
GitHub Actions CI에서 이미지 빌드 후 Seoul ECR Push 이후
Tokyo ECR에도 동일 이미지 자동 복사

utterai-prod-backend:{git_sha} → ap-northeast-1 ECR 동일 태그 복사
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
