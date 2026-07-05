import json
import os
import urllib.request
import boto3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

bedrock = boto3.client("bedrock-runtime", region_name=os.environ.get("BEDROCK_REGION", "ap-northeast-2"))
lambda_client = boto3.client("lambda")

MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "global.anthropic.claude-haiku-4-5-20251001-v1:0")
FINOPS_QUERY_ARN = os.environ["FINOPS_QUERY_LAMBDA_ARN"]

SYSTEM_PROMPT = """\
당신은 UtterAI 인프라의 FinOps 전문 에이전트입니다.
AWS 비용 데이터를 조회해서 간결하고 명확하게 한국어로 답변합니다.

## 날짜 규칙 (반드시 준수)
- 오늘 날짜: {today}
- 이번 달 시작: {month_start}
- "오늘", "오늘자" → start_date={today}, end_date={tomorrow} (Cost Explorer end는 exclusive)
- "어제" → start_date={yesterday}, end_date={today}
- "이번 달", 날짜 미지정 → start_date={month_start}, end_date={tomorrow}
- "지난 N일" → days=N
- "Spot 전환 후", "Spot으로 전환하고", "전환 이후 누적" → get_spot_savings의 window=since_tracking

## 도구 선택 규칙
- "서비스별 합계" (기간 합산) → get_cost_by_service
- "일별 + 서비스별" (날짜별 서비스 breakdown) → get_daily_cost_by_service
- "일별 추이" (단일 서비스 or 전체 합계 트렌드) → get_daily_cost_trend
- "예측" → get_cost_forecast
- "네임스페이스/워크로드/Spot" → Kubecost tools

## 출력 규칙
- 금액: USD + KRW(환율 1,400원) 함께 표시 (예: $12.34 / 약 17,276원)
- 증감: 방향(↑/↓)과 퍼센트 함께
- 서비스 미지정 시 상위 5개만
- 도구 결과에 없는 금액, 비율, 절감액을 추론하거나 계산해서 만들지 않습니다.
- 도구가 status=data_unavailable을 반환하면 숫자를 0으로 바꾸지 말고 데이터가 준비되지 않았다고 답합니다.\
"""

TOOL_DEFINITIONS = [
    {
        "name": "get_cost_by_service",
        "description": "지정 기간 AWS 서비스별 비용 합계 반환 (EC2, RDS, S3, EKS 등). 이번 달 전체 비용 현황 파악에 사용.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "시작일 YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "종료일 YYYY-MM-DD (당일 미포함)"},
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "get_daily_cost_by_service",
        "description": "날짜별 × 서비스별 비용 matrix. '일별 서비스별', '날짜별 breakdown' 질문에 사용. get_cost_by_service(월별 합산)와 다르게 날짜별로 쪼개줌.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "시작일 YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "종료일 YYYY-MM-DD (당일 미포함, exclusive)"},
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "get_daily_cost_trend",
        "description": "단일 서비스 또는 전체(ALL)의 일별 비용 추이. 특정 서비스가 며칠간 얼마 나왔는지 트렌드 파악에 사용.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "AWS 서비스명 (예: 'Amazon EC2') 또는 'ALL'"},
                "days": {"type": "integer", "description": "조회 일수 (최대 90)"},
            },
            "required": ["service", "days"],
        },
    },
    {
        "name": "get_cost_forecast",
        "description": "향후 N일간 월별 비용 예측 (AWS Cost Explorer ML 기반)",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "예측 일수"},
            },
            "required": ["days"],
        },
    },
    {
        "name": "get_cost_by_tag",
        "description": "태그 기준 비용 조회. 환경(Environment=prod/dev)별 비용 분리에 사용.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tag_key": {"type": "string", "description": "태그 키 (예: Environment)"},
                "tag_value": {"type": "string", "description": "태그 값 (예: prod)"},
                "start_date": {"type": "string", "description": "시작일 YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "종료일 YYYY-MM-DD"},
            },
            "required": ["tag_key", "tag_value", "start_date", "end_date"],
        },
    },
    {
        "name": "get_namespace_costs",
        "description": "Kubecost: EKS 네임스페이스별 비용. CPU/메모리/GPU 비용 포함. 쿠버네티스 워크로드 비용 파악에 사용.",
        "input_schema": {
            "type": "object",
            "properties": {
                "window": {"type": "string", "description": "조회 기간 (예: 7d, 30d, 2024-06-01T00:00:00Z,2024-07-01T00:00:00Z)"},
            },
            "required": [],
        },
    },
    {
        "name": "get_workload_costs",
        "description": "Kubecost: 특정 네임스페이스 내 Deployment별 비용 조회. 특정 서비스의 쿠버네티스 비용 확인에 사용.",
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "쿠버네티스 네임스페이스명"},
                "window": {"type": "string", "description": "조회 기간 (예: 7d, 30d)"},
            },
            "required": ["namespace"],
        },
    },
    {
        "name": "get_spot_savings",
        "description": "Spot 절감액 조회. status=data_unavailable이면 숫자를 추론하지 말고 reason을 그대로 안내.",
        "input_schema": {
            "type": "object",
            "properties": {
                "window": {
                    "type": "string",
                    "description": "조회 기간(예: 7d, 30d). 전환 이후 누적은 since_tracking",
                },
                "start_date": {"type": "string", "description": "명시적 시작일 YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "명시적 종료일 YYYY-MM-DD, exclusive"},
                "group_by": {
                    "type": "string",
                    "enum": ["none", "instance_type", "nodepool"],
                    "description": "절감액 상세 분류 기준",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_cluster_cost_summary",
        "description": "Kubecost: EKS 클러스터 전체 비용 (CPU/메모리/GPU/스토리지/네트워크 분류). 클러스터 리소스 종류별 비용 파악에 사용.",
        "input_schema": {
            "type": "object",
            "properties": {
                "window": {"type": "string", "description": "조회 기간 (예: 7d, 30d)"},
            },
            "required": [],
        },
    },
]


def call_tool(name: str, tool_input: dict) -> dict:
    resp = lambda_client.invoke(
        FunctionName=FINOPS_QUERY_ARN,
        Payload=json.dumps({"tool": name, "params": tool_input}),
    )
    return json.loads(resp["Payload"].read())


def _money(usd: float) -> str:
    return f"${usd:,.2f} / 약 {round(usd * 1400):,}원"


def _format_spot_savings(tool_response: dict) -> str:
    if tool_response.get("error"):
        return f":warning: Spot 절감 데이터를 조회하지 못했습니다.\n원인: {tool_response['error']}"

    result = tool_response.get("result") or {}
    status = result.get("status")
    period = result.get("period") or {}
    start = period.get("start", "-")
    end = period.get("end", "-")

    if status == "data_unavailable":
        return (
            ":hourglass_flowing_sand: *Spot 절감 데이터가 아직 준비되지 않았습니다.*\n"
            f"조회 기간: {start} ~ {end} (종료일 미포함)\n"
            f"원인: {result.get('reason', 'CUR 데이터 미도착')}\n"
            "AWS CUR 2.0 데이터가 도착한 뒤 자동으로 조회할 수 있습니다."
        )

    if status != "complete":
        return (
            ":warning: *Spot 절감액을 확정할 수 없습니다.*\n"
            f"조회 기간: {start} ~ {end} (종료일 미포함)\n"
            f"원인: {result.get('reason', '일부 기준가격 누락')}\n"
            f"누락 기준가격 행: {result.get('missing_baseline_items', 0):,}건"
        )

    actual = float(result["spot_actual_usd"])
    baseline = float(result["on_demand_equivalent_usd"])
    savings = float(result["savings_usd"])
    savings_pct = float(result["savings_pct"])

    lines = [
        f"*Spot 절감 현황*  `{start} ~ {end}` (종료일 미포함)",
        "",
        "```",
        f"온디맨드 환산 비용  {_money(baseline)}",
        f"실제 Spot 비용      {_money(actual)}",
        f"절감액              {_money(savings)}",
        f"절감률              {savings_pct:.1f}%",
        "```",
        f"Spot 사용시간: {float(result.get('spot_node_hours', 0)):.1f}시간 · 리소스 {int(result.get('spot_resource_count', 0))}개",
    ]

    breakdown = result.get("breakdown") or []
    if breakdown:
        lines.extend(["", "*상세 절감액*"])
        for item in breakdown[:5]:
            item_savings = item.get("savings_usd")
            item_pct = item.get("savings_pct")
            if item_savings is not None and item_pct is not None:
                lines.append(
                    f"• {item.get('group', 'unallocated')}: {_money(float(item_savings))} ({float(item_pct):.1f}%)"
                )

    lines.extend([
        "",
        f"기준: AWS CUR 온디맨드 공개가격 · 데이터 갱신: {result.get('data_updated_at') or '확인 불가'}",
    ])
    return "\n".join(lines)


def _return_answer(answer: str, response_url: str, user_name: str, question: str) -> dict:
    if response_url:
        prefix = f"*{user_name}*: _{question}_\n\n" if user_name else ""
        _post_to_slack(response_url, prefix + answer)
    return {"answer": answer}


def _post_to_slack(response_url: str, text: str) -> None:
    payload = json.dumps({"response_type": "in_channel", "text": text}).encode()
    req = urllib.request.Request(
        response_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5):
        pass


def handler(event, context):
    question = event.get("question", "")
    response_url = event.get("response_url", "")
    user_name = event.get("user_name", "")

    if not question:
        return {"error": "question 필드가 필요합니다"}

    now = datetime.now(ZoneInfo("Asia/Seoul"))
    today = now.strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    month_start = now.strftime("%Y-%m-01")

    messages = [{"role": "user", "content": question}]
    system = SYSTEM_PROMPT.format(
        today=today, tomorrow=tomorrow, yesterday=yesterday, month_start=month_start
    )

    for _ in range(10):
        resp = bedrock.invoke_model(
            modelId=MODEL_ID,
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 2048,
                "system": system,
                "messages": messages,
                "tools": TOOL_DEFINITIONS,
            }),
        )
        body = json.loads(resp["body"].read())
        stop_reason = body.get("stop_reason")
        content = body.get("content", [])

        messages.append({"role": "assistant", "content": content})

        if stop_reason == "end_turn":
            for block in content:
                if block.get("type") == "text":
                    answer = block["text"]
                    if response_url:
                        prefix = f"*{user_name}*: _{question}_\n\n" if user_name else ""
                        _post_to_slack(response_url, prefix + answer)
                    return {"answer": answer}

        if stop_reason == "tool_use":
            tool_results = []
            tool_uses = [block for block in content if block.get("type") == "tool_use"]
            for block in tool_uses:
                result = call_tool(block["name"], block["input"])
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block["id"],
                    "content": json.dumps(result, ensure_ascii=False),
                })

            # Spot 절감액은 재무 수치이므로 Claude의 후속 자유 생성 단계를 거치지 않는다.
            # 단일 Spot tool 호출일 때 검증된 필드만 코드 템플릿으로 렌더링한다.
            if len(tool_uses) == 1 and tool_uses[0]["name"] == "get_spot_savings":
                answer = _format_spot_savings(result)
                return _return_answer(answer, response_url, user_name, question)

            messages.append({"role": "user", "content": tool_results})
            continue

        break

    error_msg = "답변 생성에 실패했습니다. 다시 시도해주세요."
    if response_url:
        _post_to_slack(response_url, error_msg)
    return {"error": error_msg, "stop_reason": stop_reason}
