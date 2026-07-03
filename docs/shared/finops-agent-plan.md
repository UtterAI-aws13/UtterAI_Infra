# UtterAI FinOps Agent — 설계 · 구현 계획

> 최초 작성: 2026-07-03
> **범례**: ✅ 완료 / 🔄 PR 오픈 / ⬜ 미착수 / 🖱️ 수동 작업

---

## 배경

### 목적

AWS 비용 현황, 워크로드별 지출, Spot 절감 효과를 자연어로 질문해서 바로 확인할 수 있는 AI 에이전트를 구축한다.

```
"이번 달 GPU 비용 얼마야?"
"지난달 대비 EC2 비용 증감율은?"
"Spot 전환으로 얼마 절약했어?"
    ↓
FinOps Agent (Claude)
    ↓ tool_use
Cost Explorer / Kubecost API
    ↓
자연어 답변 + 수치
```

### 현재 상태

- **Kubecost**: prod 클러스터에 배포. 네임스페이스·워크로드별 일별 비용 집계. UI에서만 조회 가능.
- **AWS Cost Explorer**: 계정 레벨 서비스별 비용. API 쿼리 불가 (미연결).
- **AI Agent 인프라**: AgentCore Gateway + Lambda 패턴 구축 완료 (`05-agentcore` 스택, KURE retriever).

자연어로 비용을 조회하는 인터페이스가 없고, Kubecost 데이터를 외부에서 프로그래밍적으로 읽는 경로가 없다.

---

## 아키텍처

```
[ 사용자 ]
    │ 자연어 질문 (Slack slash command or 직접 API)
    ▼
[ FinOps Agent Lambda ]  (utterai-{env}-finops-agent)
    │ Claude claude-sonnet-4-6 + tool_use
    ├── tool: get_cost_by_service()      → AWS Cost Explorer API
    ├── tool: get_daily_cost_trend()     → AWS Cost Explorer API
    ├── tool: get_cost_forecast()        → AWS Cost Explorer API
    ├── tool: get_namespace_costs()      → Kubecost REST API  ← Phase 2
    ├── tool: get_workload_costs()       → Kubecost REST API  ← Phase 2
    └── tool: get_spot_savings()         → Kubecost REST API  ← Phase 2
    ▼
자연어 답변
```

### 데이터 소스별 역할 분리

| 소스 | 제공 데이터 | 특징 |
|------|-----------|------|
| AWS Cost Explorer | 계정 전체, 서비스별 비용 (EC2/RDS/S3 등), 예측 | VPC 불필요, IAM만 |
| Kubecost REST API | 네임스페이스·워크로드별 비용, Spot 절감액 | 클러스터 내부, VPC 필요 |

---

## Phase 1 — Cost Explorer 연동 (1~2일)

> AWS Cost Explorer API 기반. VPC 변경 없이 가장 빠르게 동작하는 에이전트 구축.

### 1-1. Lambda: finops-query

| 항목 | 내용 |
|------|------|
| 함수명 | `utterai-{env}-finops-query` |
| 런타임 | Python 3.12 |
| 메모리 | 512MB |
| 타임아웃 | 30s |
| VPC | 불필요 (Cost Explorer는 퍼블릭 엔드포인트) |
| 위치 | `terraform/environments/prod/03-services/main.tf` |

#### 제공 tool 목록

```python
def get_cost_by_service(start_date: str, end_date: str) -> dict:
    """지정 기간 AWS 서비스별 비용 합계 반환 (EC2, RDS, S3, EKS 등)"""

def get_daily_cost_trend(service: str, days: int) -> list:
    """특정 서비스의 일별 비용 트렌드 (최대 90일)"""

def get_cost_forecast(days: int) -> dict:
    """향후 N일간 비용 예측 (Cost Explorer ML 기반)"""

def get_cost_by_tag(tag_key: str, tag_value: str, start_date: str, end_date: str) -> dict:
    """태그 기반 비용 조회 (Environment=prod 등)"""
```

#### IAM 정책

```hcl
resource "aws_iam_role_policy" "finops_query_permissions" {
  policy = jsonencode({
    Statement = [
      {
        Sid    = "CostExplorer"
        Effect = "Allow"
        Action = [
          "ce:GetCostAndUsage",
          "ce:GetCostForecast",
          "ce:GetDimensionValues",
          "ce:GetTags",
        ]
        Resource = "*"
      },
    ]
  })
}
```

### 1-2. FinOps Agent Lambda

| 항목 | 내용 |
|------|------|
| 함수명 | `utterai-{env}-finops-agent` |
| 런타임 | Python 3.12 |
| 메모리 | 1024MB |
| 타임아웃 | 60s |
| 환경변수 | `FINOPS_QUERY_LAMBDA_ARN`, `BEDROCK_REGION=ap-northeast-2` |

에이전트 Lambda는 Claude claude-sonnet-4-6를 직접 호출하고, `tool_use` 응답 시 `finops-query` Lambda를 invoke하는 agentic loop를 돌린다. AgentCore Gateway 등록 없이 Lambda-to-Lambda 직접 호출 방식으로 구현한다 (AgentCore는 외부 클라이언트 인증이 필요한 경우 Phase 3에서 추가).

```
finops-agent Lambda
  ├── Bedrock InvokeModel (claude-sonnet-4-6)
  ├── tool_use 응답 시 → finops-query Lambda invoke
  └── 최종 답변 반환
```

#### IAM 추가 정책

```hcl
# finops-agent가 Bedrock + finops-query Lambda를 호출할 수 있어야 함
Action = [
  "bedrock:InvokeModel",
  "lambda:InvokeFunction",  # finops-query ARN만
]
```

### 1-3. 시스템 프롬프트

```
당신은 UtterAI 인프라의 FinOps 전문 에이전트입니다.
AWS 비용 데이터를 조회해서 간결하고 명확하게 한국어로 답변합니다.

- 금액은 항상 USD와 KRW(환율 1,400원 기준)를 함께 표시합니다.
- 증감율이 있을 때는 방향(↑/↓)과 퍼센트를 함께 표시합니다.
- 구체적인 서비스명을 묻지 않은 경우 상위 5개 서비스만 보여줍니다.
- 날짜를 특정하지 않으면 이번 달(월초~오늘)을 기본 기간으로 사용합니다.
- 환경(dev/prod)을 구분해서 답변합니다.
```

### 1-4. 인터페이스 — 직접 Lambda invoke (Phase 1)

별도 UI 없이 AWS 콘솔 또는 CLI에서 직접 호출.

```bash
aws lambda invoke \
  --function-name utterai-prod-finops-agent \
  --payload '{"question": "이번 달 서비스별 비용 알려줘"}' \
  response.json && cat response.json
```

### Phase 1 파일 변경 목록

Lambda 코드는 비용 모니터링 인프라 관심사이므로 `UtterAI_Infra` 레포에서 관리한다.

| 파일 | 변경 내용 | 상태 |
|------|----------|------|
| `lambda/finops_query/handler.py` | Cost Explorer tool dispatcher | ✅ |
| `lambda/finops_agent/handler.py` | Claude agentic loop 구현 | ✅ |
| `terraform/environments/prod/03-services/main.tf` | finops-query + finops-agent Lambda + IAM 추가 | ✅ |
| `terraform/environments/prod/03-services/outputs.tf` | finops Lambda ARN output 추가 | ✅ |

> `BEDROCK_MODEL_ID`: `anthropic.claude-sonnet-4-6` (ap-northeast-2 확인 완료)

---

## Phase 2 — Kubecost 연동 (1~2일)

> 네임스페이스·워크로드·Spot 절감액 조회를 위한 Kubecost REST API 연동.

### 2-1. Kubecost 내부 ALB 노출

Kubecost는 현재 ClusterIP Service만 있어서 클러스터 외부(Lambda)에서 접근 불가. 내부 ALB(internal)로 노출해서 동일 VPC 안의 Lambda에서 접근 가능하게 만든다.

```
Lambda (VPC 내부)
    │ HTTP 9090
    ▼
internal ALB (utterai-prod-kubecost-internal)
    │
    ▼
Kubecost Service (kubecost-cost-analyzer.kubecost.svc:9090)
```

```yaml
# k8s/platform/addons/overlays/prod/kubecost-ingress.yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: kubecost-internal
  namespace: kubecost
  annotations:
    kubernetes.io/ingress.class: alb
    alb.ingress.kubernetes.io/scheme: internal
    alb.ingress.kubernetes.io/target-type: ip
spec:
  rules:
    - http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: kubecost-cost-analyzer
                port:
                  number: 9090
```

### 2-2. Lambda: finops-kubecost

Phase 1의 `finops-query` Lambda에 Kubecost tool을 추가한다 (별도 Lambda 불필요).

```python
def get_namespace_costs(namespace: str, window: str = "30d") -> dict:
    """특정 네임스페이스의 비용 (CPU·GPU·메모리 분리)"""

def get_workload_costs(namespace: str, window: str = "30d") -> dict:
    """네임스페이스 내 워크로드별 비용 Top 10"""

def get_spot_savings(window: str = "30d") -> dict:
    """Spot 사용으로 절약된 비용 vs On-Demand 가격"""

def get_cluster_cost_summary(window: str = "30d") -> dict:
    """클러스터 전체 비용 요약 (총액, 네임스페이스별 비중)"""
```

#### Kubecost REST API 패턴

```
GET http://<kubecost-internal-alb>/model/allocation
  ?window=30d
  &aggregate=namespace
  &accumulate=true

GET http://<kubecost-internal-alb>/model/savings/requestSizingV2
  → Spot 절감 예측
```

### 2-3. VPC 설정

`finops-query` Lambda를 VPC 안에 배치 (Kubecost ALB 접근을 위해).

```hcl
vpc_config {
  subnet_ids         = data.terraform_remote_state.network.outputs.private_app_subnet_ids
  security_group_ids = [aws_security_group.finops_query.id]
}
```

보안 그룹: Kubecost ALB 향 HTTP(9090) egress 허용.

### Phase 2 파일 변경 목록

| 파일 | 변경 내용 | 상태 |
|------|----------|------|
| `k8s/platform/addons/overlays/prod/kubecost-ingress.yaml` | Kubecost internal ALB Ingress 추가 | ⬜ |
| `terraform/environments/prod/03-services/main.tf` | finops-query Lambda VPC 설정 추가 | ⬜ |
| `terraform/environments/prod/03-services/main.tf` | finops SG + Kubecost ALB SG 규칙 추가 | ⬜ |
| `UtterAI_AI/app/lambda/finops_query/handler.py` | Kubecost tool 추가 | ⬜ |
| `terraform/environments/prod/03-services/main.tf` | `KUBECOST_ENDPOINT` 환경변수 추가 | ⬜ |

---

## Phase 3 — Slack 연동 (1일)

> `/finops 이번 달 GPU 비용 얼마야?` Slack slash command 구현.

### 3-1. Slack App 설정 (수동)

```
Slack API (api.slack.com/apps)
  → Create App → Slash Commands
  → /finops → Request URL: https://api.utterai.org/finops  (or Lambda URL)
  → Bot Token: xoxb-...  → AWS Secrets Manager 저장
```

### 3-2. Lambda: finops-slack-handler

Slack slash command 요청을 받아 `finops-agent` Lambda를 async invoke 후 즉시 200 응답 (Slack 3초 타임아웃 대응). 결과는 `response_url`로 후속 POST.

```python
def handler(event, context):
    # 1. Slack 서명 검증 (HMAC-SHA256)
    # 2. finops-agent Lambda async invoke
    # 3. 즉시 {"response_type": "in_channel", "text": "조회 중..."} 반환

def finops_agent_callback(question, response_url):
    # finops-agent 결과를 Slack response_url로 POST
```

### 3-3. 외부 노출

기존 CloudFront + ALB 경로에 `/finops` 엔드포인트 추가, 또는 Lambda Function URL(HTTPS) 직접 사용.

Lambda Function URL 방식 (권장, ALB 변경 불필요):

```hcl
resource "aws_lambda_function_url" "finops_slack" {
  function_name      = aws_lambda_function.finops_slack_handler.function_name
  authorization_type = "NONE"  # Slack 서명 검증으로 보안
}
```

### Phase 3 파일 변경 목록

| 파일 | 변경 내용 | 상태 |
|------|----------|------|
| `terraform/environments/prod/03-services/main.tf` | finops-slack-handler Lambda + Function URL | ⬜ |
| `terraform/modules/secrets/main.tf` | Slack Bot Token secret 추가 | ⬜ |
| `UtterAI_AI/app/lambda/finops_slack/handler.py` | Slack slash command handler 구현 | ⬜ |
| Slack API 콘솔 | `/finops` slash command 등록 + Lambda URL 연결 | ⬜ 🖱️ |
| AWS Secrets Manager | `utterai-prod/slack-finops-bot-token` 수동 등록 | ⬜ 🖱️ |

---

## 질문 예시

Phase 1 (Cost Explorer):
```
"이번 달 서비스별 비용 보여줘"
"EC2 비용이 지난달 대비 얼마나 늘었어?"
"다음 달 비용 예측해줘"
"prod 환경 태그 기준으로 이번 달 비용 알려줘"
```

Phase 2 (+ Kubecost):
```
"utterai-prod-api 네임스페이스 지난달 비용은?"
"GPU 워커 vs CPU 워커 비용 비교해줘"
"Spot 인스턴스로 이번 달 얼마 절약했어?"
"가장 비용이 많이 드는 워크로드 Top 5는?"
```

Phase 3 (Slack):
```
/finops 이번 달 총 AWS 비용
/finops GPU 비용 7월 vs 6월 비교
/finops Spot 절감액 이번 달
```

---

## 구현 순서 요약

| Phase | 소요 시간 | 핵심 작업 | 결과물 |
|-------|---------|----------|--------|
| Phase 1 | 1~2일 | Cost Explorer Lambda + Claude agentic loop | CLI/콘솔로 비용 자연어 조회 |
| Phase 2 | 1~2일 | Kubecost internal ALB + Lambda VPC 이동 | 네임스페이스·Spot 비용 조회 추가 |
| Phase 3 | 1일 | Slack slash command handler | `/finops` 슬랙 명령으로 조회 |

---

## 주요 결정 사항

**AgentCore Gateway 미사용 (Phase 1~2)**

KURE retriever는 AgentCore Gateway를 거쳐 외부 클라이언트(Bedrock Runtime)에서 호출하는 구조. FinOps agent는 Lambda-to-Lambda 직접 호출로 충분하고, 인증 레이어가 불필요하게 늘어나지 않는다. Phase 3 Slack 연동 이후에도 Lambda Function URL에서 바로 agent Lambda를 호출하는 방식이 단순하다.

**Kubecost 노출 방식 — internal ALB 선택**

다른 대안:
- S3 ETL 직접 파싱: bingen(이진) 포맷이라 파싱 복잡도 높음
- kubectl port-forward: 자동화 불가
- internal ALB: VPC 내부만 접근 가능, 외부 노출 없음 → 선택

**환율 고정 — 1,400원/USD**

Cost Explorer는 USD로 반환. 매 요청마다 환율 API를 호출하지 않고 시스템 프롬프트에 고정 환율 명시. 정확한 KRW가 필요하면 별도 환율 tool 추가 가능.
