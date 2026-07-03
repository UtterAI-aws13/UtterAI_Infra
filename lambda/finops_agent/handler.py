import json
import os
import boto3
from datetime import datetime

bedrock = boto3.client("bedrock-runtime", region_name=os.environ.get("BEDROCK_REGION", "ap-northeast-2"))
lambda_client = boto3.client("lambda")

MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-sonnet-4-6-20251101-v1:0")
FINOPS_QUERY_ARN = os.environ["FINOPS_QUERY_LAMBDA_ARN"]

SYSTEM_PROMPT = """\
당신은 UtterAI 인프라의 FinOps 전문 에이전트입니다.
AWS 비용 데이터를 조회해서 간결하고 명확하게 한국어로 답변합니다.

규칙:
- 금액은 USD와 KRW(환율 1,400원 기준)를 함께 표시합니다 (예: $12.34 / 약 17,276원)
- 증감율은 방향(↑/↓)과 퍼센트를 함께 표시합니다
- 서비스명을 특정하지 않은 경우 상위 5개만 보여줍니다
- 날짜를 특정하지 않으면 이번 달(월초~오늘)을 기본 기간으로 사용합니다
- 오늘 날짜: {today}
- 이번 달 시작: {month_start}\
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
        "name": "get_daily_cost_trend",
        "description": "특정 서비스의 일별 비용 트렌드. 증감 추이 파악에 사용. service='ALL'이면 전체 합계.",
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
]


def call_tool(name: str, tool_input: dict) -> str:
    resp = lambda_client.invoke(
        FunctionName=FINOPS_QUERY_ARN,
        Payload=json.dumps({"tool": name, "params": tool_input}),
    )
    return json.dumps(json.loads(resp["Payload"].read()), ensure_ascii=False)


def handler(event, context):
    question = event.get("question", "")
    if not question:
        return {"error": "question 필드가 필요합니다"}

    today = datetime.now().strftime("%Y-%m-%d")
    month_start = datetime.now().strftime("%Y-%m-01")

    messages = [{"role": "user", "content": question}]
    system = SYSTEM_PROMPT.format(today=today, month_start=month_start)

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
                    return {"answer": block["text"]}

        if stop_reason == "tool_use":
            tool_results = []
            for block in content:
                if block.get("type") == "tool_use":
                    result = call_tool(block["name"], block["input"])
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": result,
                    })
            messages.append({"role": "user", "content": tool_results})
            continue

        break

    return {"error": "답변 생성 실패", "stop_reason": stop_reason}
