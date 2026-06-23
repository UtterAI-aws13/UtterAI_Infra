# EKS Private Endpoint 전환 가이드

> 작성일: 2026-06-23  
> 상태: **미적용** — 사전 조건 구성 후 적용 예정  
> 관련 파일: `terraform/modules/eks/main.tf:69-71`, `terraform/modules/vpc/main.tf`

---

## 목차

1. [왜 보안 설정을 하는가](#1-왜-보안-설정을-하는가)
2. [EKS Endpoint 보안이 중요한 이유](#2-eks-endpoint-보안이-중요한-이유)
3. [현재 워크플로우](#3-현재-워크플로우)
4. [적용 후 워크플로우](#4-적용-후-워크플로우)
5. [사전 조건 — 접근 수단 확보](#5-사전-조건--접근-수단-확보)
6. [적용 절차](#6-적용-절차)

---

## 1. 왜 보안 설정을 하는가

### 1.1 기본 개념: 심층 방어 (Defense in Depth)

보안은 단일 장벽이 아니라 **여러 레이어의 방어**를 겹치는 구조다. 어느 한 레이어가 뚫려도 다음 레이어가 막는다.

```
인터넷
  │
  ├── 레이어 1: 네트워크 경계 (EKS endpoint 비공개, WAF, SG)
  │
  ├── 레이어 2: 인증/인가 (IAM, RBAC, IRSA, Cognito)
  │
  ├── 레이어 3: 런타임 격리 (PSA, NetworkPolicy, SecurityContext)
  │
  └── 레이어 4: 데이터 보호 (KMS, S3 암호화, RDS 암호화, TLS)
```

현재 UtterAI Prod는 레이어 2~4는 상당 부분 적용되어 있다. **레이어 1 — 네트워크 경계가 가장 약한 고리다.** EKS API 서버가 인터넷에 열려 있는 것이 대표적 예다.

### 1.2 실제 위협 시나리오

**시나리오 A: 자격증명 탈취 후 API 서버 직접 공격**

1. 개발자 PC의 `~/.kube/config`가 피싱/악성코드로 유출된다.
2. 공격자는 어디서든 `kubectl --server=https://<EKS_ENDPOINT>` 명령을 날릴 수 있다.
3. RBAC 권한 범위 내에서 Pod 배포, Secret 조회, 노드 접근이 가능해진다.

→ **Endpoint가 비공개면**: 자격증명이 유출되어도 VPC 내부에 있지 않은 공격자는 API 서버에 도달 자체가 불가능하다.

**시나리오 B: 인터넷 스캐너에 의한 익스플로잇**

[Shodan](https://www.shodan.io), [Censys](https://censys.io) 같은 인터넷 스캐너는 Kubernetes API 서버(`port 443`, `k8s` 헤더)를 상시 탐색한다. 공개된 EKS endpoint는 자동으로 인덱싱되고, 알려진 취약점(misconfigured RBAC, anonymous access 등)을 자동 시도하는 봇의 대상이 된다.

→ **Endpoint가 비공개면**: 스캐너에 노출되지 않아 탐색 자체가 차단된다.

**시나리오 C: AWS IAM 키 유출**

AWS Access Key가 GitHub에 실수로 push되거나 빌드 로그에 노출되는 사고는 빈번하다. IAM 키가 있으면 `aws eks update-kubeconfig`로 kubeconfig를 자동 생성할 수 있다.

→ **Endpoint가 비공개면**: IAM 키가 유출되어도 kubectl 접근은 VPC 안에서만 가능하다.

---

## 2. EKS Endpoint 보안이 중요한 이유

### 2.1 EKS API 서버는 클러스터의 뇌다

EKS API 서버(`kube-apiserver`)는 클러스터의 모든 상태를 제어한다.

- **Pod 배포/삭제** — 악성 워크로드 주입 가능
- **Secret 조회** — DB 비밀번호, API 키, 인증서 전체 접근
- **ConfigMap 수정** — 앱 설정 변조
- **RBAC 수정** — 권한 상승(privilege escalation)
- **노드 접근** — `kubectl exec`, `kubectl debug`로 노드 쉘 획득

API 서버가 뚫리면 나머지 모든 보안 레이어(NetworkPolicy, PSA, SecurityContext)는 의미가 없다. 공격자가 `kubectl delete networkpolicy --all`로 지워버릴 수 있기 때문이다.

### 2.2 현재 상태와 리스크

```hcl
# terraform/modules/eks/main.tf:69-71
vpc_config {
  endpoint_private_access = true
  endpoint_public_access  = true   # ← 인터넷에서 직접 접근 가능
}
```

`endpoint_public_access = true`인 상태에서 EKS API 서버 URL은 다음과 같이 공개 DNS로 노출된다:

```
https://<CLUSTER_ID>.gr7.ap-northeast-2.eks.amazonaws.com
```

이 주소는 AWS가 공개 DNS에 등록하며, 누구나 ping/curl로 도달 가능하다. IAM 인증이 막고 있지만, 그것 하나에 의존하는 구조다.

### 2.3 적용 후 변화

```
현재: 인터넷 → EKS API 서버 (IAM으로만 차단)
적용 후: 인터넷 → [VPC 경계에서 차단] → 접근 불가
          VPC 내부 → EKS API 서버 (IAM + 네트워크 이중 차단)
```

---

## 3. 현재 워크플로우

```
┌─────────────────────────────────────────────────────┐
│ 개발자 로컬                                           │
│                                                     │
│  kubectl get pods ──────────────────────────────┐  │
│  terraform apply (04-addons) ───────────────────┤  │
└─────────────────────────────────────────────────┼──┘
                                                  │ 인터넷 (퍼블릭)
                                                  ▼
                                    EKS Public Endpoint
                                    (0.0.0.0/0 허용)
                                                  │
                                                  ▼
                                          EKS Control Plane
                                          (ap-northeast-2)

┌─────────────────────────────────────────────────────┐
│ GitHub Actions (ubuntu-latest, VPC 외부)             │
│                                                     │
│  git push (이미지 태그 업데이트) ──────────────────┐ │
└──────────────────────────────────────────────────┼─┘
                                                   │ GitHub (퍼블릭)
                                                   ▼
                                              Git 저장소
                                                   │
                                                   │ ArgoCD가 감지 (VPC 내부)
                                                   ▼
                                          EKS Control Plane
```

**GitHub Actions는 EKS API를 직접 호출하지 않는다.** `kubectl kustomize`는 로컬 YAML 렌더링이고, 실제 클러스터 반영은 VPC 내부의 ArgoCD가 담당한다.

---

## 4. 적용 후 워크플로우

```
┌─────────────────────────────────────────────────────┐
│ 개발자 로컬                                           │
│                                                     │
│  kubectl get pods ──┐                               │
│  terraform apply ───┤                               │
└─────────────────────┼───────────────────────────────┘
                      │
                      ▼
          [SSM 포트포워딩 or Client VPN]
                      │ VPC 내부 경로
                      ▼
              EKS Private Endpoint
              (VPC 내부에서만 접근 가능)
                      │
                      ▼
            EKS Control Plane

┌─────────────────────────────────────────────────────┐
│ GitHub Actions                                       │
│                                                     │
│  git push ─────────────────────────────────────────┐│
└────────────────────────────────────────────────────┼┘
                                                     │ 변경 없음
                                                     ▼
                                                Git 저장소
                                                     │
                                                     │ ArgoCD (VPC 내부)
                                                     ▼
                                           EKS Control Plane
```

**GitHub Actions 워크플로우는 변경 없다.** 영향받는 것은 개발자 로컬에서의 직접 접근뿐이다.

---

## 5. 사전 조건 — 접근 수단 확보

`endpoint_public_access = false` 적용 전에 반드시 VPC 내부 접근 수단이 있어야 한다. 수단 없이 적용하면 로컬 `kubectl`과 `terraform apply`(04-addons)가 영구 차단된다.

### 현재 인프라에서 활용 가능한 것

`modules/eks/main.tf:55`에 노드 IAM Role에 SSM이 이미 붙어 있다:

```hcl
resource "aws_iam_role_policy_attachment" "node_ssm" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
```

EKS 노드는 이미 SSM Session Manager로 접속 가능한 상태다.

---

### 옵션 A: SSM 포트포워딩 (권장 — 추가 리소스 없음)

SSM을 이용해 로컬 포트를 EKS Private Endpoint로 터널링한다. 비용 발생 없음.

**동작 방식:**

```
로컬 kubectl → localhost:8443
                    │ SSM Session (암호화 터널)
                    ▼
              EKS 노드 (SSM Agent)
                    │ VPC 내부
                    ▼
         EKS Private Endpoint (443)
```

**설정 방법:**

```bash
# 1. EKS 노드 인스턴스 ID 확인
aws ec2 describe-instances \
  --filters "Name=tag:eks:cluster-name,Values=utterai-prod" \
            "Name=instance-state-name,Values=running" \
  --query "Reservations[0].Instances[0].InstanceId" \
  --output text

# 2. SSM 포트포워딩 시작 (백그라운드)
aws ssm start-session \
  --target <INSTANCE_ID> \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters '{
    "host": ["<EKS_PRIVATE_ENDPOINT>"],
    "portNumber": ["443"],
    "localPortNumber": ["8443"]
  }'

# 3. kubeconfig에서 server 주소를 localhost로 변경
kubectl config set-cluster <CLUSTER_NAME> \
  --server=https://localhost:8443 \
  --insecure-skip-tls-verify=true

# 4. kubectl 사용
kubectl get pods -A
```

**EKS Private Endpoint 주소 확인:**

```bash
aws eks describe-cluster \
  --name utterai-prod \
  --query "cluster.endpoint" \
  --output text
# 출력: https://XXXX.gr7.ap-northeast-2.eks.amazonaws.com
# → "https://" 제거한 호스트명을 host 파라미터에 사용
```

**작업 완료 후 kubeconfig 원복:**

```bash
aws eks update-kubeconfig --name utterai-prod --region ap-northeast-2
# endpoint_public_access = false 적용 후에는 이 명령은 VPN/SSM 연결 중에만 동작
```

---

### 옵션 B: AWS Client VPN (팀 전체 상시 연결)

팀 인원이 늘거나 SSM 포트포워딩이 불편해지면 전환을 고려한다.

**비용**: VPN Endpoint ~$0.10/hr + 연결당 $0.05/hr ≈ 월 **$70~120**

```hcl
# terraform/environments/prod/01-network/main.tf 에 추가
resource "aws_ec2_client_vpn_endpoint" "this" {
  description            = "utterai-prod-client-vpn"
  server_certificate_arn = aws_acm_certificate.vpn_server.arn
  client_cidr_block      = "172.16.0.0/22"
  vpc_id                 = module.vpc.vpc_id
  security_group_ids     = [aws_security_group.vpn.id]

  authentication_options {
    type                       = "certificate-authentication"
    root_certificate_chain_arn = aws_acm_certificate.vpn_client.arn
  }

  connection_log_options {
    enabled = false
  }
}
```

---

### 옵션 C: EC2 Bastion (가장 단순)

퍼블릭 서브넷에 t3.nano 하나. SSH 없이 SSM으로 접속, 노드에서 kubectl 실행.

**비용**: t3.nano 월 **$4~5**

```hcl
resource "aws_instance" "bastion" {
  ami                    = data.aws_ami.amazon_linux_2023.id
  instance_type          = "t3.nano"
  subnet_id              = module.vpc.public_subnet_ids[0]
  iam_instance_profile   = aws_iam_instance_profile.bastion_ssm.name
  vpc_security_group_ids = [aws_security_group.bastion.id]

  tags = { Name = "utterai-prod-bastion" }
}
# SSH 인바운드 없음 — SSM으로만 접속
```

---

## 6. 적용 절차

`endpoint_public_access = false`는 **불가역적 변경**이다. 아래 순서를 반드시 지킨다.

### 사전 확인 체크리스트

```
[ ] 옵션 A/B/C 중 하나를 구성하고 실제로 kubectl 동작 확인
[ ] SSM 포트포워딩 또는 VPN 통해 kubectl get nodes 성공 확인
[ ] terraform plan (04-addons)이 새 접근 경로로 정상 실행 확인
[ ] 팀 전체가 새 접근 방법 숙지
```

### Terraform 변경

```hcl
# terraform/modules/eks/main.tf:67-71
vpc_config {
  subnet_ids              = var.private_app_subnet_ids
  endpoint_private_access = true
  endpoint_public_access  = false   # true → false
}
```

```bash
cd terraform/environments/prod/02-eks
terraform plan   # 변경 내용 확인
terraform apply  # 적용 즉시 퍼블릭 차단
```

### 적용 후 즉시 확인

```bash
# VPN/SSM 없이 시도 → 타임아웃 또는 연결 거부 확인
kubectl get nodes  # 실패해야 정상

# VPN/SSM 연결 후 시도 → 성공 확인
[SSM 포트포워딩 실행]
kubectl get nodes  # 성공해야 정상
```

---

## 관련 문서

- [Prod 보안 현황](./security.md) — 전체 보안 TODO 우선순위
- [Prod 인프라 가이드](./README.md) — §3 네트워크 구성
- [Prod 전환 체크리스트](./migration-checklist.md)
