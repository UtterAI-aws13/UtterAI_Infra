# EKS Private Endpoint 전환 가이드

> 작성일: 2026-06-23  
> 상태: **미적용** — 사전 조건 구성 후 적용 예정  
> 관련 파일: `terraform/modules/eks/main.tf:69-71`, `terraform/environments/prod/01-network/main.tf`

---

## 목차

1. [왜 보안 설정을 하는가](#1-왜-보안-설정을-하는가)
2. [EKS Endpoint 보안이 중요한 이유](#2-eks-endpoint-보안이-중요한-이유)
3. [현재 워크플로우](#3-현재-워크플로우)
4. [적용 후 워크플로우](#4-적용-후-워크플로우)
5. [ACM 현황 및 VPN 인증서 위치](#5-acm-현황-및-vpn-인증서-위치)
6. [구현 순서 — 인프라 작업](#6-구현-순서--인프라-작업)
7. [팀원 각각 해야 할 순서](#7-팀원-각각-해야-할-순서)
8. [EKS Endpoint 닫기](#8-eks-endpoint-닫기)

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

Shodan, Censys 같은 인터넷 스캐너는 Kubernetes API 서버(`port 443`, `k8s` 헤더)를 상시 탐색한다. 공개된 EKS endpoint는 자동으로 인덱싱되고, 알려진 취약점(misconfigured RBAC, anonymous access 등)을 자동 시도하는 봇의 대상이 된다.

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

`endpoint_public_access = true`인 상태에서 EKS API 서버 URL은 공개 DNS로 노출된다:

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

┌─────────────────────────────────────────────────────┐
│ GitHub Actions (ubuntu-latest, VPC 외부)             │
│                                                     │
│  git push (이미지 태그 업데이트) ──────────────────┐ │
└──────────────────────────────────────────────────┼─┘
                                                   │ GitHub (퍼블릭)
                                                   ▼
                                              Git 저장소
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
           AWS VPN Client (앱 클릭 → Connect)
                      │ TLS 터널
                      ▼
              Client VPN Endpoint
              (utterai-prod VPC 내부 서브넷)
                      │ VPC 내부 경로
                      ▼
              EKS Private Endpoint
                      │
                      ▼
            EKS Control Plane

┌─────────────────────────────────────────────────────┐
│ GitHub Actions                                       │
│  git push ──────────────────────────────────────┐   │
└─────────────────────────────────────────────────┼───┘
                                                  │ 변경 없음
                                                  ▼
                                             Git 저장소
                                                  │ ArgoCD (VPC 내부)
                                                  ▼
                                        EKS Control Plane
```

**GitHub Actions 워크플로우는 변경 없다.** 영향받는 것은 개발자 로컬에서의 직접 접근뿐이다.

VPN 연결 중에는 EKS 외에 VPC 안 모든 리소스에도 직접 접근 가능해진다:

```bash
# 현재는 불가능 — VPN 연결 후 가능
psql -h utterai-prod-rds.xxxx.ap-northeast-2.rds.amazonaws.com
redis-cli -h utterai-prod-redis.xxxx.ap-northeast-2.cache.amazonaws.com
```

---

## 5. ACM 현황 및 VPN 인증서 위치

### 현재 ACM 인증서 구조

| 인증서 | 리전 | 용도 | Terraform 위치 | 상태 |
|--------|------|------|----------------|------|
| `api.utterai.org` + `*.utterai.org` | ap-northeast-2 | ALB HTTPS 종료 | `prod/04-addons/main.tf:199` | ✅ 올바른 위치 |
| CloudFront 도메인 인증서 | us-east-1 | CloudFront HTTPS | `prod/04-addons/main.tf:116` | ✅ 올바른 위치 (CloudFront는 us-east-1 필수) |

### VPN용 인증서 위치

Client VPN은 기존 인증서와 **완전히 별개**다. VPN 전용 인증서를 새로 만들어 `ap-northeast-2` ACM에 올린다.

| 인증서 | 올리는 위치 | 역할 |
|--------|------------|------|
| 서버 인증서 | ACM ap-northeast-2 | VPN 서버 신원 증명 |
| CA(루트) 인증서 | ACM ap-northeast-2 | 클라이언트 인증서 서명 검증 |
| 클라이언트 인증서 | ACM 업로드 불필요 | 팀원 `.ovpn` 파일에 포함 |

> 클라이언트 인증서는 ACM에 올리지 않는다. 팀원별 `.ovpn` 파일에 직접 삽입해서 배포한다.

---

## 6. 구현 순서 — 인프라 작업

> 인프라 담당자 1인이 진행. 팀원에게는 §7의 순서만 전달.

---

### Step 1: easy-rsa 설치 및 인증서 생성

```bash
# easy-rsa 클론
git clone https://github.com/OpenVPN/easy-rsa.git
cd easy-rsa/easyrsa3

# PKI 초기화
./easyrsa init-pki

# CA 생성 (Common Name 입력 프롬프트: utterai-prod-vpn-ca)
./easyrsa build-ca nopass

# 서버 인증서 생성
./easyrsa build-server-full utterai-prod-vpn-server nopass

# 팀원별 클라이언트 인증서 생성 (팀원 이름으로 구분)
./easyrsa build-client-full vpn-alice nopass
./easyrsa build-client-full vpn-bob nopass
./easyrsa build-client-full vpn-carol nopass
./easyrsa build-client-full vpn-david nopass
```

생성 결과 위치:

```
pki/
├── ca.crt                        # CA 인증서
├── issued/
│   ├── utterai-prod-vpn-server.crt
│   ├── vpn-alice.crt
│   ├── vpn-bob.crt
│   ├── vpn-carol.crt
│   └── vpn-david.crt
└── private/
    ├── utterai-prod-vpn-server.key
    ├── vpn-alice.key
    ├── vpn-bob.key
    ├── vpn-carol.key
    └── vpn-david.key
```

---

### Step 2: ACM에 서버 인증서 + CA 업로드

```bash
# 서버 인증서 업로드 (ap-northeast-2)
aws acm import-certificate \
  --region ap-northeast-2 \
  --certificate fileb://pki/issued/utterai-prod-vpn-server.crt \
  --private-key fileb://pki/private/utterai-prod-vpn-server.key \
  --certificate-chain fileb://pki/ca.crt

# 출력된 ARN을 저장 (Terraform에 사용)
# CertificateArn: arn:aws:acm:ap-northeast-2:032886669461:certificate/xxxx

# CA 인증서 업로드 (클라이언트 인증서 검증용)
aws acm import-certificate \
  --region ap-northeast-2 \
  --certificate fileb://pki/ca.crt \
  --private-key fileb://pki/private/ca.key

# 출력된 ARN을 저장
# CertificateArn: arn:aws:acm:ap-northeast-2:032886669461:certificate/yyyy
```

---

### Step 3: Terraform — Client VPN 리소스 추가

`terraform/environments/prod/01-network/main.tf`에 추가:

```hcl
# ── Client VPN ────────────────────────────────────────────────────────────────

resource "aws_ec2_client_vpn_endpoint" "this" {
  description            = "${local.prefix}-client-vpn"
  server_certificate_arn = var.vpn_server_certificate_arn
  client_cidr_block      = "172.16.0.0/22"   # VPC CIDR(10.10.0.0/16)과 겹치지 않는 대역
  vpc_id                 = aws_vpc.this.id
  security_group_ids     = [aws_security_group.vpn.id]

  authentication_options {
    type                       = "certificate-authentication"
    root_certificate_chain_arn = var.vpn_ca_certificate_arn
  }

  connection_log_options {
    enabled = false
  }

  tags = {
    Name = "${local.prefix}-client-vpn"
  }
}

resource "aws_ec2_client_vpn_network_association" "this" {
  client_vpn_endpoint_id = aws_ec2_client_vpn_endpoint.this.id
  subnet_id              = aws_subnet.private_app[0].id
}

resource "aws_ec2_client_vpn_authorization_rule" "vpc" {
  client_vpn_endpoint_id = aws_ec2_client_vpn_endpoint.this.id
  target_network_cidr    = var.vpc_cidr   # VPC 전체 대역 접근 허용
  authorize_all_groups   = true
}

resource "aws_security_group" "vpn" {
  name        = "${local.prefix}-vpn-sg"
  description = "Security group for Client VPN endpoint"
  vpc_id      = aws_vpc.this.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${local.prefix}-vpn-sg"
  }
}
```

`terraform/environments/prod/01-network/variables.tf`에 추가:

```hcl
variable "vpn_server_certificate_arn" {
  type        = string
  description = "ACM certificate ARN for Client VPN server (ap-northeast-2)."
}

variable "vpn_ca_certificate_arn" {
  type        = string
  description = "ACM CA certificate ARN for Client VPN client authentication (ap-northeast-2)."
}
```

`terraform/environments/prod/01-network/terraform.tfvars` (또는 Secrets Manager / CI 변수)에 추가:

```hcl
vpn_server_certificate_arn = "arn:aws:acm:ap-northeast-2:032886669461:certificate/xxxx"
vpn_ca_certificate_arn     = "arn:aws:acm:ap-northeast-2:032886669461:certificate/yyyy"
```

```bash
cd terraform/environments/prod/01-network
terraform plan
terraform apply
```

---

### Step 4: 팀원별 .ovpn 파일 생성

```bash
# AWS에서 VPN 기본 설정 파일 다운로드
ENDPOINT_ID=$(aws ec2 describe-client-vpn-endpoints \
  --region ap-northeast-2 \
  --query "ClientVpnEndpoints[?Tags[?Key=='Name'&&Value=='utterai-prod-client-vpn']].ClientVpnEndpointId" \
  --output text)

aws ec2 export-client-vpn-client-configuration \
  --region ap-northeast-2 \
  --client-vpn-endpoint-id $ENDPOINT_ID \
  --output text > base.ovpn

# 팀원별 인증서/키를 삽입해서 개인 파일 생성
# (아래 스크립트를 팀원 수만큼 반복)
for NAME in alice bob carol david; do
  cp base.ovpn utterai-prod-${NAME}.ovpn

  echo "" >> utterai-prod-${NAME}.ovpn
  echo "<cert>" >> utterai-prod-${NAME}.ovpn
  cat pki/issued/vpn-${NAME}.crt >> utterai-prod-${NAME}.ovpn
  echo "</cert>" >> utterai-prod-${NAME}.ovpn

  echo "<key>" >> utterai-prod-${NAME}.ovpn
  cat pki/private/vpn-${NAME}.key >> utterai-prod-${NAME}.ovpn
  echo "</key>" >> utterai-prod-${NAME}.ovpn
done
```

각 팀원에게 본인 이름의 `.ovpn` 파일을 전달한다. **슬랙 DM 직접 전송은 피하고, 비밀번호 걸린 zip 또는 1Password 같은 비밀번호 관리자를 통해 공유한다.**

---

### Step 5: 동작 검증

팀원 1명이 §7 순서를 완료한 뒤, VPN 연결 상태에서 아래를 확인한다.

```bash
# VPN 연결 상태에서
kubectl get nodes   # 성공 확인
terraform plan      # prod/04-addons에서 정상 실행 확인
```

검증 완료 후 §8(EKS endpoint 닫기)로 진행한다.

---

## 7. 팀원 각각 해야 할 순서

> 인프라 담당자에게 `.ovpn` 파일을 받은 후 진행.

---

### Step 1: AWS VPN Client 앱 설치

[https://aws.amazon.com/vpn/client-vpn-download/](https://aws.amazon.com/vpn/client-vpn-download/) 에서 OS에 맞는 버전 설치.

---

### Step 2: .ovpn 파일 import

```
AWS VPN Client 앱 실행
→ File > Manage Profiles
→ Add Profile
→ 전달받은 utterai-prod-<본인이름>.ovpn 선택
→ Add Profile 클릭
```

---

### Step 3: VPN 연결

```
AWS VPN Client 앱
→ utterai-prod 프로파일 선택
→ Connect 클릭
→ 상태가 "Connected"로 바뀌면 완료 (30초 내외)
```

---

### Step 4: kubeconfig 업데이트 (최초 1회, VPN 연결 상태에서)

```bash
aws eks update-kubeconfig \
  --name utterai-prod \
  --region ap-northeast-2
```

---

### Step 5: 동작 확인

```bash
kubectl get nodes
# NAME                STATUS   ROLES    AGE
# ip-10-10-x-x ...   Ready    <none>   ...
```

---

### 이후 매번 사용할 때

```
AWS VPN Client → Connect → kubectl / terraform 사용 → Disconnect
```

VPN을 끊으면 EKS endpoint 및 VPC 내부 리소스 접근이 차단된다.

---

### 팀원 퇴사 시 처리

```bash
# 해당 팀원 클라이언트 인증서 revoke
cd easy-rsa/easyrsa3
./easyrsa revoke vpn-<퇴사자이름>
./easyrsa gen-crl

# CRL을 VPN endpoint에 적용 (Terraform 변수로 관리하거나 AWS CLI로 직접 업로드)
aws ec2 import-client-vpn-client-certificate-revocation-list \
  --client-vpn-endpoint-id $ENDPOINT_ID \
  --certificate-revocation-list fileb://pki/crl.pem \
  --region ap-northeast-2
```

---

## 8. EKS Endpoint 닫기

> §6 Step 5 검증 완료 후 진행. **불가역적 변경.**

### 사전 체크리스트

```
[ ] 팀원 전체 VPN 연결 후 kubectl get nodes 성공 확인
[ ] terraform plan (prod/04-addons) VPN 연결 상태에서 정상 실행 확인
[ ] 팀 전체에 "오늘부터 VPN 없이 kubectl 안 됩니다" 공지
```

### Terraform 변경

```hcl
# terraform/modules/eks/main.tf:69-71
vpc_config {
  subnet_ids              = var.private_app_subnet_ids
  endpoint_private_access = true
  endpoint_public_access  = false   # true → false
}
```

```bash
cd terraform/environments/prod/02-eks
terraform plan
terraform apply   # 적용 즉시 퍼블릭 차단
```

### 적용 후 즉시 확인

```bash
# VPN 끊은 상태에서 시도 → 타임아웃 확인 (정상)
kubectl get nodes   # 타임아웃 또는 연결 거부

# VPN 연결 후 시도 → 성공 확인 (정상)
kubectl get nodes   # Ready 노드 목록 출력
```

---

## 관련 문서

- [Prod 보안 현황](./security.md) — 전체 보안 TODO 우선순위
- [Prod 인프라 가이드](./README.md) — §3 네트워크 구성
- [Prod 전환 체크리스트](./migration-checklist.md)
