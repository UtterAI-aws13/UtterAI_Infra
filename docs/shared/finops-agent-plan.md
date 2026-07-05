# UtterAI FinOps Agent — 설계 · 구현 계획

> 최초 작성: 2026-07-03 · 구현 완료(Phase 1~3): 2026-07-03
> **범례**: ✅ 완료 / 🔄 PR 오픈 / ⬜ 미착수 / 🖱️ 수동 작업
>
> **as-built 요약**: 이 문서는 설계 당시 계획이며, 실제 구현은 Phase 1~3 모두 완료되어 prod에 배포·가동 중이다.
> 계획과 달라진 부분(레포 위치, 최종 모델 ID 등)은 각 Phase 하단에 실제 값으로 갱신해 두었다.
> 구현 상세는 [`docs/prod/architecture.md` §9.1](../prod/architecture.md#91-finops-비용-조회-slack-봇--배포가동-중) 참고.

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

> **`BEDROCK_MODEL_ID` 최종값: `global.anthropic.claude-haiku-4-5-20251001-v1:0`**
> 계획 당시엔 `anthropic.claude-sonnet-4-6`을 가정했으나 배포 중 3회 교체했다:
> 1. `anthropic.claude-sonnet-4-6` → on-demand 직접 호출 불가(inference profile 필요)
> 2. `global.anthropic.claude-sonnet-4-6`(inference profile ID) → Marketplace 미구독으로 AccessDenied
> 3. `global.anthropic.claude-sonnet-4-5-20250929-v1:0` → 계정에서 별도 활성화 필요, 보류
> 4. **`global.anthropic.claude-haiku-4-5-20251001-v1:0`(채택)** → ai-worker에서 이미 활성화된 모델이라 즉시 동작 확인
>
> IAM도 `bedrock:InvokeModel` Resource에 `inference-profile/*`를 추가해야 했다(최초엔 `foundation-model/*`만 있어서 AccessDenied 발생).

---

## Phase 2 — Kubecost 연동 (1~2일)

> 네임스페이스·워크로드·Spot 절감액 조회를 위한 Kubecost REST API 연동.

### 2-1. Kubecost 내부 ALB 노출 — ✅ 실제 구현 (계획 대비 경로·포트 변경)

Kubecost는 현재 ClusterIP Service만 있어서 클러스터 외부(Lambda)에서 접근 불가. 내부 ALB(internal)로 노출해서 동일 VPC 안의 Lambda에서 접근 가능하게 만든다.

```
Lambda (VPC 내부, finops_query SG egress 80/tcp)
    │ HTTP :80
    ▼
internal ALB (group.name=kubecost-internal, ArgoCD가 프로비저닝)
    │ target-type=ip, 백엔드 포트 9090
    ▼
Kubecost Service (kubecost.kubecost.svc:9090)
```

실제 파일 위치·서비스명·포트가 계획과 다르다: Ingress는 `k8s/platform/kubecost/base/internal-alb-ingress.yaml`(계획의 `addons/overlays/prod/` 아님)이고, 백엔드 Service 이름은 `kubecost-cost-analyzer`가 아니라 `kubecost`다. Lambda→ALB 구간은 리스너 포트 80이며(SG egress도 80), ALB→Pod 구간만 9090을 사용한다.

```yaml
# k8s/platform/kubecost/base/internal-alb-ingress.yaml (실제)
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: kubecost-internal
  namespace: kubecost
  annotations:
    alb.ingress.kubernetes.io/scheme: internal
    alb.ingress.kubernetes.io/target-type: ip
    alb.ingress.kubernetes.io/listen-ports: '[{"HTTP": 80}]'
    alb.ingress.kubernetes.io/group.name: kubecost-internal
spec:
  ingressClassName: alb
  rules:
    - http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: kubecost
                port:
                  number: 9090
```

### 2-2. Lambda: finops-kubecost — ✅ 계획대로 구현 (별도 Lambda 없이 finops-query에 통합)

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

#### Kubecost REST API 패턴 — ✅ 실제로는 `/model/allocation` 한 엔드포인트만 사용

계획 당시엔 Spot 절감액 조회에 별도 `/model/savings/requestSizingV2`를 검토했으나, 실제 구현은 `aggregate` 파라미터만 바꿔가며 `/model/allocation` 하나로 4개 tool(네임스페이스/워크로드/Spot/클러스터 요약)을 전부 처리한다 — 응답에 이미 `spotCost`/`onDemandCost`/`gpuCost` 등이 포함되어 있어 별도 엔드포인트가 불필요했다.

```
GET http://<kubecost-internal-alb>/model/allocation?window=30d&aggregate=namespace&accumulate=true
GET http://<kubecost-internal-alb>/model/allocation?window=30d&aggregate=deployment&accumulate=true&namespace=<ns>
GET http://<kubecost-internal-alb>/model/allocation?window=30d&aggregate=cluster&accumulate=true
  → cluster 응답의 spotCost/onDemandCost 비율로 get_spot_savings 계산
  → cluster 응답의 cpuCost/ramCost/gpuCost/pvCost/networkCost로 get_cluster_cost_summary 계산
```

### 2-3. VPC 설정

`finops-query` Lambda를 VPC 안에 배치 (Kubecost ALB 접근을 위해).

```hcl
vpc_config {
  subnet_ids         = data.terraform_remote_state.network.outputs.private_app_subnet_ids
  security_group_ids = [aws_security_group.finops_query.id]
}
```

보안 그룹(`aws_security_group.finops_query`, 실제): egress 80/tcp(Kubecost ALB, 계획 문서엔 9090으로 적었으나 ALB 리스너가 80이라 80으로 구현) + egress 443/tcp(Cost Explorer/Secrets Manager HTTPS). ingress 없음.

### Phase 2 파일 변경 목록 — ✅ 전체 완료

| 파일 | 변경 내용 | 상태 |
|------|----------|------|
| `k8s/platform/kubecost/base/internal-alb-ingress.yaml` | Kubecost internal ALB Ingress 추가 (계획 경로: `addons/overlays/prod/`) | ✅ |
| `terraform/environments/prod/03-services/main.tf` | finops-query Lambda VPC 설정 추가 | ✅ |
| `terraform/environments/prod/03-services/main.tf` | finops SG(`finops_query`) + egress 규칙 추가 | ✅ |
| `lambda/finops_query/handler.py` | Kubecost tool 4종 추가 (계획 경로: `UtterAI_AI/app/lambda/` — 실제로는 Infra 레포에 구현) | ✅ |
| `terraform/environments/prod/03-services/variables.tf` | `kubecost_alb_endpoint` 변수 추가, Lambda 환경변수 `KUBECOST_ENDPOINT`로 주입 | ✅ |

---

## Phase 3 — Slack 연동 (1일)

> `/finops 이번 달 GPU 비용 얼마야?` Slack slash command 구현.

### 3-1. Slack App 설정 (수동) — ✅ 완료, 단 Bot Token은 불필요했음

```
Slack API (api.slack.com/apps)
  → Create App → Slash Commands
  → /finops → Request URL: finops-slack Lambda의 Function URL
  → Signing Secret → AWS Secrets Manager(utterai-prod/finops-slack)에 {"signing_secret": "..."} 형태로 저장
```

**계획 대비 단순화된 점**: 계획 문서는 `Bot Token(xoxb-...)`을 전제했지만, 실제 응답은 Slack Web API(`chat.postMessage`)가 아니라 slash command 요청에 담겨 온 **`response_url`로 직접 POST**하는 방식이라 Bot Token 자체가 필요 없다. Secrets Manager에는 서명 검증용 `signing_secret` 하나만 저장한다.

### 3-2. Lambda: finops-slack — ✅ 완료 (역할 분리가 계획과 다름)

`finops-slack`은 서명 검증과 async invoke만 담당하고, **최종 답변의 Slack POST는 `finops-agent`가 직접 수행**한다(계획은 `finops-slack`이 콜백까지 담당하는 것으로 가정했었음).

```python
# lambda/finops_slack/handler.py — 실제 구현
def handler(event, context):
    # 1. Slack 서명 검증: v0=HMAC-SHA256(signing_secret, f"v0:{timestamp}:{body}"), replay 방지(300s)
    # 2. question이 없으면 사용법 안내(ephemeral) 즉시 반환
    # 3. finops-agent Lambda를 InvocationType=Event로 비동기 invoke (question, response_url, user_name 전달)
    # 4. 즉시 {"response_type": "in_channel", "text": "조회 중입니다... ⏳"} 반환 (Slack 3초 제한 대응)

# lambda/finops_agent/handler.py — 실제로 이 Lambda가 response_url에 최종 답변을 POST
def _post_to_slack(response_url, text):
    # urllib으로 {"response_type": "in_channel", "text": text} POST
```

### 3-3. 외부 노출 — ✅ Lambda Function URL 방식 채택 (계획대로)

```hcl
# terraform/environments/prod/03-services/main.tf:620 (실제)
resource "aws_lambda_function_url" "finops_slack" {
  function_name      = aws_lambda_function.finops_slack.function_name
  authorization_type = "NONE"  # Slack 서명 검증으로 보안
}

resource "aws_lambda_permission" "finops_slack_public" {
  statement_id           = "AllowPublicFunctionURL"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.finops_slack.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}
```

CloudFront/ALB 경로 추가 대안은 채택하지 않음 — Function URL이 계획대로 가장 단순했다.

### Phase 3 파일 변경 목록 — ✅ 전체 완료

| 파일 | 변경 내용 | 상태 |
|------|----------|------|
| `terraform/environments/prod/03-services/main.tf` | finops-slack Lambda + Function URL + `aws_lambda_permission`(공개 invoke 허용) | ✅ |
| `terraform/modules/secrets/main.tf` | ~~Slack Bot Token secret~~ (불필요, 아래 수동 등록으로 대체) | ✅ (해당 없음) |
| `lambda/finops_slack/handler.py` | Slack slash command handler 구현 (계획 경로: `UtterAI_AI/app/lambda/` — 실제로는 Infra 레포) | ✅ |
| `lambda/finops_agent/handler.py` | `get_daily_cost_by_service` tool 추가 + Slack POST 콜백 로직 추가("쿼리 정확도 개선") | ✅ |
| Slack API 콘솔 | `/finops` slash command 등록 + Function URL 연결 | ✅ 🖱️ |
| AWS Secrets Manager | `utterai-prod/finops-slack` (`{"signing_secret": "..."}`) 수동 등록, Terraform 미관리 | ✅ 🖱️ |

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

| Phase | 계획 소요 | 실제 소요 | 핵심 작업 | 결과물 | 상태 |
|-------|---------|---------|----------|--------|------|
| Phase 1 | 1~2일 | 당일 | Cost Explorer Lambda + Claude agentic loop | CLI/콘솔로 비용 자연어 조회 | ✅ |
| Phase 2 | 1~2일 | 당일 | Kubecost internal ALB + Lambda VPC 이동 | 네임스페이스·Spot 비용 조회 추가 | ✅ |
| Phase 3 | 1일 | 당일 | Slack slash command handler | `/finops` 슬랙 명령으로 조회 | ✅ |

세 Phase 모두 2026-07-03 하루 안에 구현·배포 완료(`fc2d7f0` → `dff6ebe`, 중간에 Bedrock 모델 교체 fix 커밋 4개 포함). `aws lambda list-functions`로 3개 Lambda 실배포 확인됨(`docs/prod/architecture.md` §9.1).

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

---

## Phase 4 — Spot 절감액 정확도·운영 고도화 (진행 중)

> 시작일: 2026-07-05
> 배경: 기존 `get_spot_savings`는 Kubecost Allocation 응답에 존재하지 않는
> `spotCost`/`onDemandCost` 필드를 `0`으로 처리했고, Claude가 `totalCost=21.33`을
> 절감액으로 오해해 Slack에 잘못 표시했다. 실제 클러스터에는 Spot 노드가 존재하므로
> "Spot 사용 0"이라는 답변도 사실과 다르다.

### 계산 기준

Spot 절감액은 전환 전후 총비용 단순 비교가 아니라 **동일한 Spot 사용량을
온디맨드 공개가격으로 실행했을 때의 환산 비용**과 비교한다.

```text
온디맨드 환산 비용 = Σ pricing/publicOnDemandCost
실제 Spot 비용     = Σ lineItem/UnblendedCost
Spot 절감액         = 온디맨드 환산 비용 - 실제 Spot 비용
Spot 절감률         = Spot 절감액 / 온디맨드 환산 비용 × 100
```

전환 전후 총비용은 워크로드 사용량 변화가 섞이므로 별도 지표로만 제공한다.

### 구현 단계와 진행 상태

| 단계 | 작업 | 완료 기준 | 상태 |
|------|------|-----------|------|
| 4-1 | 오답 즉시 차단 | 누락 필드를 `0`으로 만들지 않고 `data_unavailable` 반환, LLM 숫자 추론 금지, 단위 테스트 | ✅ 완료 |
| 4-2 | 비용 데이터 기반 | CUR 2.0/Data Export, S3, Glue, Athena Workgroup, 최소권한 IAM을 Terraform으로 관리 | ✅ 완료 |
| 4-3 | 정확한 계산 도구 | Athena 기반 `get_spot_savings(start_date, end_date, group_by)`와 구조화된 응답 계약 구현 | ✅ 완료 |
| 4-4 | 결정적 Slack 출력 | 숫자·표는 코드가 생성하고 Claude는 의도/기간 선택만 담당 | ✅ 완료 |
| 4-5 | 전환 후 누적 조회 | 검증된 `SPOT_TRACKING_START_DATE`부터 누적 절감액 조회 | ✅ 완료 |
| 4-6 | 검증·관측성·배포 | 테스트, 데이터 최신성/오류 지표·알람, 실제 Lambda/Slack 검증 | ✅ 완료 (CUR 첫 전달 대기) |

### 목표 응답 계약

```json
{
  "status": "complete",
  "period": {
    "start": "2026-07-01",
    "end": "2026-07-06",
    "timezone": "Asia/Seoul"
  },
  "spot_actual_usd": 8.41,
  "on_demand_equivalent_usd": 29.74,
  "savings_usd": 21.33,
  "savings_pct": 71.7,
  "spot_node_hours": 92.4,
  "breakdown": [],
  "data_updated_at": "2026-07-05T12:00:00Z",
  "calculation_method": "AWS_CUR_PUBLIC_ON_DEMAND"
}
```

`complete`가 아닌 경우 숫자를 `0`으로 대체하지 않는다. `status`, `reason`,
`data_updated_at`을 이용해 Slack에 데이터 미준비/지연을 명시한다.

### 조사 결과 (착수 시점)

- AWS 계정에 BCM Data Export 없음
- 레거시 CUR Report Definition 없음
- Glue Database 없음
- Athena는 기본 `primary` Workgroup만 존재
- 실제 EKS 노드 중 Spot 3대 확인(API NodePool 2대, cpu-worker NodePool 1대)
- 현재 `get_spot_savings(window=30d)` 실제 반환:
  `total_usd=21.33`, `spot_usd=0`, `on_demand_usd=0`, `spot_ratio_pct=0`

각 단계 완료 직후 이 표의 상태와 실제 변경 파일·검증 결과를 갱신한다.

### 4-1 완료 기록 — 오답 즉시 차단

- `lambda/finops_query/handler.py`
  - Kubecost `totalCost`를 유지하되 `cluster_total_usd`로만 명시
  - 존재하지 않는 `spotCost`/`onDemandCost`와 파생 절감률 제거
  - 정확한 CUR 기반 계산 전까지 `status=data_unavailable` 반환
- `lambda/finops_agent/handler.py`
  - 도구에 없는 숫자·비율·절감액 추론 금지 규칙 추가
  - `data_unavailable`을 `0`으로 변환하지 않도록 명시
- `tests/unit/test_finops_query.py`
  - `totalCost=21.33`이 절감액으로 노출되지 않는 회귀 테스트
  - 빈 Kubecost Allocation 응답 테스트
- 검증: `python -m unittest tests.unit.test_finops_query -v` 2건 통과,
  Lambda 핸들러 3종 `py_compile` 통과

### 4-2 완료 기록 — CUR 2.0 · Athena 기반

- `terraform/environments/prod/03-services/finops_billing.tf` 신규
  - CUR 2.0 BCM Data Export: `utterai-prod-cur-2-0`
  - 시간 단위·리소스 단위 Parquet, 기존 월 파티션 덮어쓰기
  - 암호화·퍼블릭 차단·400일 보존 S3 버킷
  - 30일 만료 Athena 결과 버킷
  - Glue DB `utterai_prod_finops`, 파티션 프로젝션 테이블 `cur2_spot_costs`
  - Athena Workgroup `utterai-prod-finops`, 쿼리당 1GiB 스캔 제한
  - finops-query 역할에 Athena/Glue/S3 최소권한 추가
- `terraform.tf`: BCM API용 `aws.us_east_1` provider alias 추가
- `variables.tf`: `spot_tracking_start_date`(`2026-07-01`) 추가
- AWS 적용 결과: FinOps 신규 리소스 17개 생성, 기존 리소스 변경/삭제 없음
- Export 상태: `HEALTHY` 확인
- Provider 5.100이 AWS 기본값 `BILLING_VIEW_ARN`,
  `INCLUDE_MANUAL_DISCOUNT_COMPATIBILITY`를 응답에 추가해 최초 apply가 inconsistent
  result로 끝나는 문제가 있었고, 두 값을 코드에 명시한 뒤 state taint를 해제해 해소
- 검증: `terraform validate` 성공, FinOps target plan 인프라 변경 없음

### 4-3 완료 기록 — Athena 기반 Spot 절감 계산

- `lambda/finops_query/spot_savings.py` 신규
  - KST 기준 명시 날짜 또는 `Nd` 조회, 최대 90일 제한
  - `billing_period` 파티션 프루닝과 SQL 입력 allowlist
  - EC2 `SpotUsage` 사용 행만 집계
  - `pricing_public_on_demand_cost - line_item_unblended_cost`로 절감액 계산
  - 전체/인스턴스 타입/NodePool 기준 breakdown 지원
  - 기준가격 누락 시 `partial`, 데이터 미도착 시 `data_unavailable`
- `lambda/finops_query/handler.py`: 기존 Kubecost 추정 로직을 Athena 모듈로 교체
- `terraform/environments/prod/03-services/main.tf`
  - finops-query 패키징을 단일 파일에서 디렉터리 ZIP으로 변경
  - Athena DB/Table/Workgroup 및 추적 시작일 환경변수 연결
- `tests/unit/test_finops_query.py`
  - 파티션/Spot 필터 SQL, 기간 제한, 정확한 계산, 데이터 미도착 회귀 테스트
- 검증: 단위 테스트 4건 통과, Terraform validate 성공

### 4-4 완료 기록 — 결정적 Slack 출력

- `lambda/finops_agent/handler.py`
  - 단일 `get_spot_savings` 호출은 Claude 후속 생성을 건너뜀
  - `complete` 응답만 온디맨드 환산액·실제 비용·절감액·절감률로 렌더링
  - `partial`은 절감액을 숨기고 기준가격 누락 경고
  - `data_unavailable`은 `$0`, `0%` 대신 CUR 준비 상태 표시
  - USD→KRW 계산과 Slack 표를 코드에서 결정적으로 생성
- `tests/unit/test_finops_agent.py`
  - 정상/부분/데이터 미도착 출력 테스트 3건 추가
- 검증: FinOps 단위 테스트 총 7건 통과

### 4-5 완료 기록 — 전환 후 누적 조회

- 추적 기준일: `2026-07-01`
  - 현재 운영 Spot 노드의 최초 생성 시점과 CUR 2.0 최초 수집 가능 월을 기준으로 설정
  - Terraform 변수 `spot_tracking_start_date`로 변경 가능
- “Spot 전환 후/전환하고/전환 이후 누적” 질문을 `window=since_tracking`으로 매핑
- 명시 날짜·최근 N일 요청도 추적 시작일 이전으로 내려가지 않도록 clamp
- Agent 날짜 계산을 Lambda 기본 UTC에서 `Asia/Seoul`로 수정
- Spot tool schema에 `start_date`, `end_date`, `group_by` 추가
- 검증: 누적 기준일·tool schema 테스트를 추가해 총 9건 통과

### 4-6 완료 기록 — 검증·관측성·배포

- `lambda/finops_query/spot_savings.py`
  - `CURDataAvailable`, `SpotBaselineCoveragePct`, query success/error 커스텀 지표
- `terraform/environments/prod/03-services/finops_observability.tf` 신규
  - FinOps Lambda 로그 3종을 Terraform state로 import하고 보존기간 30일 적용
  - Lambda Errors 3개, Athena 500MiB/5분 스캔, Spot 쿼리 오류 경보 생성
- `lambda/finops_slack/handler.py`
  - Slack 서명/response URL/질문이 포함될 수 있는 raw header/body 로그 제거
  - 사용자명과 질문 길이만 구조화 로그로 남김
- 프로덕션 배포
  - finops-query/agent/slack Lambda 코드 및 환경변수 갱신 성공
  - target plan/apply로 기존 비-FinOps 드리프트를 제외하고 삭제 없이 적용
  - 적용 후 FinOps target plan `No changes` 확인
- 실제 호출 검증
  - `get_spot_savings(window=since_tracking)` → `data_unavailable`, 기간 2026-07-01~2026-07-06(KST) 정상
  - 자연어 “우리 spot으로 전환하고 얼마 줄었지?” → 숫자를 만들지 않고 CUR 준비 중 메시지 반환
- CUR 2.0 Export 자체는 `HEALTHY`. 조사 시점에는 execution 0건/S3 Parquet 미도착으로,
  첫 전달 후 동일 질문이 자동으로 실제 절감액 표로 전환된다.
