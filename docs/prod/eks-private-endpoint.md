# EKS Private Endpoint 전환 가이드

> 작성일: 2026-06-23
> 상태: **VPN 구축 완료 / EKS 퍼블릭 엔드포인트 차단 대기 중**
> 관련 파일: `terraform/modules/eks/main.tf`, `terraform/environments/prod/01-network/main.tf`

---

## 목차

1. [초기 상태와 문제](#1-초기-상태와-문제)
2. [왜 EKS Endpoint 보안이 중요한가](#2-왜-eks-endpoint-보안이-중요한가)
3. [전체 워크플로우 변화](#3-전체-워크플로우-변화)
4. [구현 방식 결정: Certificate-based VPN](#4-구현-방식-결정-certificate-based-vpn)
5. [실제 구현 과정](#5-실제-구현-과정)
6. [현재 상태](#6-현재-상태)
7. [팀원 VPN 설정 순서](#7-팀원-vpn-설정-순서)
8. [EKS 퍼블릭 엔드포인트 차단 (다음 단계)](#8-eks-퍼블릭-엔드포인트-차단-다음-단계)
9. [팀원 퇴사 시 처리](#9-팀원-퇴사-시-처리)

---

## 1. 초기 상태와 문제

### 1.1 초기 EKS 설정

작업 시작 시점에서 EKS 클러스터의 endpoint 설정은 다음과 같았다.

```hcl
# terraform/modules/eks/main.tf
vpc_config {
  endpoint_private_access = true
  endpoint_public_access  = true   # ← 인터넷에서 직접 접근 가능
}
```

`endpoint_public_access = true` 상태에서 EKS API 서버 URL은 공개 DNS로 노출된다.

```
https://<CLUSTER_ID>.gr7.ap-northeast-2.eks.amazonaws.com
```

이 주소는 AWS가 공개 DNS에 등록하며, 인터넷 어디서든 IAM 인증만 있으면 `kubectl`과 `terraform`으로 클러스터에 접근할 수 있는 상태였다.

### 1.2 실제 위협 시나리오

**시나리오 A: 자격증명 탈취**

개발자 PC의 `~/.kube/config` 또는 AWS Access Key가 피싱/악성코드/GitHub push 실수로 유출되면, 공격자는 어디서든 EKS API 서버에 접근할 수 있다. RBAC 권한 범위 내에서 Pod 배포, Secret 조회, 노드 접근이 가능하다.

**시나리오 B: 인터넷 스캐너**

Shodan, Censys 같은 스캐너는 Kubernetes API 서버(port 443, k8s 헤더)를 상시 탐색한다. 공개된 EKS endpoint는 자동으로 인덱싱되어 misconfigured RBAC, anonymous access 등을 자동 시도하는 봇의 대상이 된다.

Endpoint가 비공개면 스캐너에 노출되지 않아 탐색 자체가 차단된다.

### 1.3 EKS API 서버가 왜 치명적인가

EKS API 서버(`kube-apiserver`)는 클러스터의 모든 상태를 제어한다. API 서버가 뚫리면 나머지 보안 레이어(NetworkPolicy, PSA, SecurityContext)는 의미가 없다. 공격자가 `kubectl delete networkpolicy --all`로 무력화할 수 있기 때문이다.

| API 서버 권한 | 결과 |
|-------------|------|
| Pod 배포 | 악성 워크로드 주입 |
| Secret 조회 | DB 비밀번호, API 키 전체 노출 |
| ConfigMap 수정 | 앱 설정 변조 |
| RBAC 수정 | 권한 상승 |
| kubectl exec | 노드 쉘 획득 |

---

## 2. 왜 EKS Endpoint 보안이 중요한가

### 심층 방어 (Defense in Depth)

보안은 단일 장벽이 아니라 여러 레이어의 방어를 겹치는 구조다.

```
인터넷
  │
  ├── 레이어 1: 네트워크 경계 (EKS endpoint 비공개, VPN, SG)  ← 이번 작업
  │
  ├── 레이어 2: 인증/인가 (IAM, RBAC, IRSA)
  │
  ├── 레이어 3: 런타임 격리 (PSA, NetworkPolicy, SecurityContext)
  │
  └── 레이어 4: 데이터 보호 (KMS, S3 암호화, RDS 암호화, TLS)
```

UtterAI Prod는 레이어 2~4는 상당 부분 적용되어 있었으나, **레이어 1 — 네트워크 경계가 가장 약한 고리**였다.

---

## 3. 전체 워크플로우 변화

### 적용 전 (현재까지)

```
개발자 로컬
  │
  │ 인터넷 (퍼블릭)
  ▼
EKS Public Endpoint (0.0.0.0/0 허용)
  │
  ▼
EKS Control Plane
```

### 적용 후 (EKS 퍼블릭 엔드포인트 차단 시)

```
개발자 로컬
  │
  ▼
OpenVPN GUI (Connect)
  │ TLS 터널 (인증서 기반 mutual TLS)
  ▼
AWS Client VPN Endpoint (utterai-prod VPC 내부)
  │ VPC 내부 경로
  ▼
EKS Private Endpoint
  │
  ▼
EKS Control Plane
```

**GitHub Actions는 영향 없다.** GitHub Actions는 EKS API를 직접 호출하지 않는다. 이미지 태그 업데이트 후 ArgoCD(VPC 내부)가 감지해서 배포하는 구조이기 때문이다.

### Split Tunnel 라우팅

VPN 연결 시 **VPC 트래픽만** VPN을 통과하고 나머지 인터넷 트래픽은 직접 연결된다.

```
클라이언트
  ├── 10.20.0.0/16 (VPC) → VPN 터널 → EKS, RDS, Redis 등
  ├── 100.64.0.0/16 (Pod CIDR) → VPN 터널
  └── 그 외 (AWS API, 인터넷 등) → 직접 연결 (인터넷 영향 없음)
```

---

## 4. 구현 방식 결정: Certificate-based VPN

### 검토한 방식들

| 방식 | 설명 | 결정 |
|------|------|------|
| AWS SSM Session Manager | IAM 인증으로 EC2 배스천 경유 | 복잡, 별도 배스천 필요 |
| SAML + IAM Identity Center | IdP 연동, SSO 기반 VPN | 설정 복잡, 현재 IdP 없음 |
| **Certificate-based VPN** | 팀원별 X.509 인증서, mutual TLS | **선택** |

### Certificate-based VPN 선택 이유

- 팀원 4명 규모에서 one-time 설정으로 충분
- AWS IAM과 독립적 — IAM 키 유출이 VPN 접근으로 이어지지 않는다
- 팀원 퇴사 시 해당 인증서만 revoke하면 즉시 접근 차단
- 별도 IdP 없이 구축 가능

### 인증서 구조

```
utterai-prod-vpn-ca (CA)
  ├── vpn.utterai.org (서버 인증서) → ACM 업로드
  ├── vpn-dohyun (클라이언트) → .ovpn 파일에 포함
  ├── vpn-eunyoung (클라이언트) → .ovpn 파일에 포함
  ├── vpn-jungmin (클라이언트) → .ovpn 파일에 포함
  └── vpn-jiwon (클라이언트) → .ovpn 파일에 포함
```

CA와 서버 인증서만 ACM에 업로드한다. 클라이언트 인증서는 ACM에 올리지 않고 `.ovpn` 파일에 직접 삽입한다.

---

## 5. 실제 구현 과정

### Step 1: 인증서 생성

**easy-rsa 시도 → 실패**

Windows Git Bash 환경에서 easy-rsa를 시도했으나, OpenSSL 경로 형식(`/c/Users/...`)이 Windows 네이티브 OpenSSL 바이너리와 충돌하여 동작하지 않았다. Git for Windows에 포함된 OpenSSL을 PowerShell에서 직접 사용하는 방식으로 전환했다.

**사용한 OpenSSL 경로**: `C:\Program Files\Git\usr\bin\openssl.exe`

**서버 인증서 생성 시 주의사항**

첫 시도에서 `CN=utterai-prod-vpn-server`로 생성했더니 ACM이 "Certificate does not have a domain" 오류로 거부했다. AWS ACM은 서버 인증서에 도메인 형식 CN을 요구한다.

`CN=vpn.utterai.org`로 재생성하고, 아래 확장 설정을 명시했다.

```ini
# server-ext.cnf
[v3_server]
basicConstraints = CA:FALSE
extendedKeyUsage = serverAuth        # TLS 서버 인증용
keyUsage = digitalSignature, keyEncipherment
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid,issuer
```

서버 인증서 서명 시 `-extfile server-ext.cnf -extensions v3_server` 옵션을 명시하지 않으면, OpenVPN의 `remote-cert-tls server` 검증에서 TLS 핸드쉐이크가 실패한다.

**생성 결과 위치** (repo 외부, 로컬 보관):

```
C:\Users\DGSO1\vpn-certs\
├── certs\
│   ├── ca.crt / ca.key
│   ├── server.crt / server.key
│   ├── client-dohyun.crt / client-dohyun.key
│   ├── client-eunyoung.crt / client-eunyoung.key
│   ├── client-jungmin.crt / client-jungmin.key
│   └── client-jiwon.crt / client-jiwon.key
└── ovpn\
    ├── utterai-prod-dohyun.ovpn
    ├── utterai-prod-eunyoung.ovpn
    ├── utterai-prod-jungmin.ovpn
    └── utterai-prod-jiwon.ovpn
```

> 인증서 파일 및 `.ovpn` 파일은 절대 git에 올리지 않는다. 배포 시 비밀번호 걸린 zip 또는 1Password 같은 비밀번호 관리자를 통해 공유한다.

---

### Step 2: ACM 업로드

```bash
# 서버 인증서 업로드
aws acm import-certificate \
  --region ap-northeast-2 \
  --certificate fileb://certs/server.crt \
  --private-key fileb://certs/server.key \
  --certificate-chain fileb://certs/ca.crt

# CA 인증서 업로드 (클라이언트 인증서 서명 검증용)
aws acm import-certificate \
  --region ap-northeast-2 \
  --certificate fileb://certs/ca.crt \
  --private-key fileb://certs/ca.key
```

업로드된 ARN은 `terraform/environments/prod/01-network/terraform.tfvars`에 기록했다. 이 파일은 `.gitignore`에 포함되어 있다.

---

### Step 3: Terraform — Client VPN 리소스 추가

`terraform/environments/prod/01-network/main.tf`에 다음을 추가했다.

```hcl
locals {
  prefix = "${var.project_name}-${var.environment}"
}

resource "aws_security_group" "vpn" {
  name        = "${local.prefix}-vpn-sg"
  description = "Security group for Client VPN endpoint"
  vpc_id      = module.vpc.vpc_id

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

resource "aws_ec2_client_vpn_endpoint" "this" {
  description            = "${local.prefix}-client-vpn"
  server_certificate_arn = var.vpn_server_certificate_arn
  client_cidr_block      = "172.16.0.0/22"
  vpc_id                 = module.vpc.vpc_id
  security_group_ids     = [aws_security_group.vpn.id]
  split_tunnel           = true   # VPC 트래픽만 VPN 통과, 인터넷은 직접 연결

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
  subnet_id              = module.vpc.private_app_subnet_ids[0]
}

resource "aws_ec2_client_vpn_authorization_rule" "vpc" {
  client_vpn_endpoint_id = aws_ec2_client_vpn_endpoint.this.id
  target_network_cidr    = var.vpc_cidr
  authorize_all_groups   = true
}
```

**VPC Endpoint SG 버그 수정** (`terraform/modules/vpc/main.tf`)

Terraform plan 중 vpc_endpoint SG의 ingress rule에서 Pod CIDR(`100.64.0.0/16`)이 제거될 뻔했다. Pod → VPC Endpoint 통신이 끊기는 문제로, `compact([var.vpc_cidr, var.pod_cidr])`로 수정했다.

```hcl
# 수정 전
cidr_blocks = [var.vpc_cidr]

# 수정 후
cidr_blocks = compact([var.vpc_cidr, var.pod_cidr])
```

---

### Step 4: .ovpn 파일 생성

AWS CLI로 기본 설정 파일을 다운로드하고, 팀원별 인증서/키를 삽입했다.

**주의사항 — Windows PowerShell에서 파일 생성 시**

`Out-File -Encoding utf8`로 생성하면 UTF-8 BOM(`EF BB BF`)이 추가되고 CRLF 줄바꿈이 삽입된다. OpenVPN 클라이언트가 이를 파싱하지 못해 연결 실패가 발생한다.

반드시 아래 방식으로 저장해야 한다.

```powershell
[System.IO.File]::WriteAllText(
  $outputPath,
  ($content -replace "\r\n", "\n"),
  [System.Text.UTF8Encoding]::new($false)  # BOM 없는 UTF-8
)
```

---

### Step 5: Split Tunnel 문제

**초기 설정**: `split_tunnel = false` (풀 터널)

풀 터널 모드에서는 VPN 연결 시 모든 트래픽이 VPN을 통과한다. 결과:
- `aws eks update-kubeconfig` 실패 — AWS 퍼블릭 API(`eks.ap-northeast-2.amazonaws.com`)에 도달 불가
- 인터넷 연결 끊김

**수정**: `split_tunnel = true`로 변경 후 apply

이후 VPN 연결 시 서버가 push하는 route:
```
route 10.20.0.0 255.255.0.0    # VPC
route 100.64.0.0 255.255.0.0   # Pod CIDR
```
`0.0.0.0/0`(default route)은 push하지 않아 인터넷은 직접 연결된다.

---

### Step 6: AWS VPN Client → OpenVPN GUI 교체

`split_tunnel = true` 적용 후에도 **AWS VPN Client for Windows**는 TAP 어댑터 메트릭을 강제로 1로 설정하고 WiFi default route를 삭제하는 동작을 했다. 수동으로 메트릭을 9000으로 설정해도 VPN 재연결 시 1로 리셋됐다.

```
# AWS VPN Client 연결 후 route print 결과 (split_tunnel = true 인데도)
0.0.0.0  0.0.0.0  172.16.1.1  172.16.1.3  1   ← VPN이 default route 점유
# WiFi default route 없음 → 인터넷 끊김
```

**해결**: **OpenVPN GUI**로 교체

OpenVPN GUI는 서버가 push한 route만 적용한다. 서버 라우팅 테이블에 `0.0.0.0/0`이 없으므로 default route를 건드리지 않는다.

OpenVPN GUI 연결 로그 확인:
```
PUSH_REPLY: route 10.20.0.0 255.255.0.0,route 100.64.0.0 255.255.0.0,...
→ route ADD 10.20.0.0 MASK 255.255.0.0 172.16.1.33 METRIC 200
→ route ADD 100.64.0.0 MASK 255.255.0.0 172.16.1.33 METRIC 200
# 0.0.0.0/0 route 없음 → 인터넷 정상
```

---

## 6. 현재 상태

| 항목 | 상태 |
|------|------|
| AWS Client VPN endpoint 생성 | 완료 (`cvpn-endpoint-03acc479b72c9abf1`) |
| 서버 인증서 ACM 업로드 | 완료 (`CN=vpn.utterai.org`) |
| CA 인증서 ACM 업로드 | 완료 |
| Split tunnel | 완료 (`split_tunnel = true`) |
| 팀원 .ovpn 파일 생성 | 완료 (dohyun, eunyoung, jungmin, jiwon) |
| OpenVPN GUI 연결 확인 (dohyun) | 완료 — 인터넷 정상, kubectl 정상 |
| EKS `endpoint_public_access = false` | **미적용** — 다음 단계 |

VPN 연결 상태에서 동작 확인:
```bash
$ kubectl get nodes
NAME                                              STATUS   ROLES    AGE
ip-10-20-11-245.ap-northeast-2.compute.internal   Ready    <none>   2d21h
ip-10-20-11-254.ap-northeast-2.compute.internal   Ready    <none>   4d1h
...
$ curl https://google.com
<HTML>301 Moved...   # 인터넷 정상
```

---

## 7. 팀원 VPN 설정 순서

> 인프라 담당자에게 `.ovpn` 파일을 받은 후 진행. 비밀번호 걸린 zip 또는 1Password로 전달받는다.

---

### Step 1: OpenVPN GUI 설치

[https://openvpn.net/community-downloads/](https://openvpn.net/community-downloads/) 에서 **Windows 64-bit Installer** 다운로드 후 설치.

> AWS VPN Client가 아니라 OpenVPN GUI를 사용한다. AWS VPN Client는 Windows에서 인터넷 연결을 끊는 문제가 있다.

---

### Step 2: .ovpn 파일 등록

1. `C:\Users\<본인이름>\OpenVPN\config\` 폴더에 `utterai-prod-<본인이름>.ovpn` 파일 복사
2. 시작 메뉴에서 **OpenVPN GUI** 실행
3. 우측 하단 시스템 트레이 아이콘 우클릭 → `utterai-prod-<본인이름>` → **Connect**

---

### Step 3: kubeconfig 업데이트 (최초 1회, VPN 연결 상태에서)

```bash
aws eks update-kubeconfig \
  --name utterai-prod-eks \
  --region ap-northeast-2
```

---

### Step 4: 동작 확인

```bash
kubectl get nodes
# NAME                                              STATUS   ROLES    AGE
# ip-10-20-11-x.ap-northeast-2.compute.internal    Ready    <none>   ...
```

---

### 매번 사용할 때

```
OpenVPN GUI → Connect → kubectl / terraform 사용 → Disconnect
```

VPN 연결 중에는 인터넷도 정상적으로 사용 가능하다 (split tunnel 적용).

---

## 8. EKS 퍼블릭 엔드포인트 차단 (다음 단계)

> 팀원 전체 VPN 연결 확인 후 진행. **적용 즉시 VPN 없이 kubectl이 불가능해진다.**

### 사전 체크리스트

```
[ ] 팀원 전체 OpenVPN GUI 설치 및 .ovpn 파일 등록 완료
[ ] 팀원 전체 VPN 연결 후 kubectl get nodes 성공 확인
[ ] 팀 전체에 "오늘부터 VPN 없이 kubectl 안 됩니다" 공지
```

### Terraform 변경

```hcl
# terraform/modules/eks/main.tf
vpc_config {
  subnet_ids              = var.private_app_subnet_ids
  endpoint_private_access = true
  endpoint_public_access  = false   # true → false
}
```

```bash
cd terraform/environments/prod/02-eks
terraform plan
terraform apply
```

### 적용 후 즉시 확인

```bash
# VPN 끊은 상태 → 타임아웃 확인 (정상)
kubectl get nodes   # connection timeout

# VPN 연결 후 → 성공 확인 (정상)
kubectl get nodes   # Ready 노드 목록 출력
```

---

## 9. 팀원 퇴사 시 처리

퇴사자의 `.ovpn` 파일을 회수하는 것만으로는 부족하다. 인증서 자체를 revoke해야 한다.

```bash
# CA key와 OpenSSL을 사용해 CRL 생성
openssl ca -config ca.cnf -revoke client-<퇴사자>.crt
openssl ca -config ca.cnf -gencrl -out crl.pem

# CRL을 VPN endpoint에 적용
aws ec2 import-client-vpn-client-certificate-revocation-list \
  --client-vpn-endpoint-id cvpn-endpoint-03acc479b72c9abf1 \
  --certificate-revocation-list fileb://crl.pem \
  --region ap-northeast-2
```

적용 즉시 해당 인증서로의 VPN 연결이 차단된다.

---

## 관련 문서

- [Prod 보안 현황](./security.md) — 전체 보안 TODO 우선순위
- [ACM 현황](./security.md#acm)
