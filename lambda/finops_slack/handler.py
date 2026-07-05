import hashlib
import hmac
import json
import os
import time
import urllib.parse
import boto3

lambda_client = boto3.client("lambda")
sm_client = boto3.client("secretsmanager", region_name="ap-northeast-2")

FINOPS_AGENT_ARN = os.environ["FINOPS_AGENT_LAMBDA_ARN"]
SLACK_SECRET_ID = os.environ.get("SLACK_SECRET_ID", "utterai-prod/finops-slack")

# 콜드 스타트 시 1회만 로드
_secrets = None


def _get_secrets():
    global _secrets
    if _secrets is None:
        raw = sm_client.get_secret_value(SecretId=SLACK_SECRET_ID)["SecretString"]
        _secrets = json.loads(raw)
    return _secrets


def _verify_signature(headers: dict, body: str) -> bool:
    secrets = _get_secrets()
    signing_secret = secrets["signing_secret"]
    timestamp = headers.get("x-slack-request-timestamp", "")
    try:
        if abs(time.time() - int(timestamp)) > 300:
            return False
    except (ValueError, TypeError):
        return False
    sig_base = f"v0:{timestamp}:{body}"
    expected = "v0=" + hmac.new(
        signing_secret.encode(), sig_base.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, headers.get("x-slack-signature", ""))


def handler(event, context):
    raw_body = event.get("body", "") or ""
    # API Gateway / Function URL may base64-encode the body
    if event.get("isBase64Encoded"):
        import base64
        raw_body = base64.b64decode(raw_body).decode("utf-8")
    body = raw_body
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

    if not _verify_signature(headers, body):
        print("[WARN] rejected Slack request with invalid signature")
        return {"statusCode": 401, "body": "Unauthorized"}

    params = dict(urllib.parse.parse_qsl(body))
    question = params.get("text", "").strip()
    response_url = params.get("response_url", "")
    user_name = params.get("user_name", "unknown")
    print(f"[INFO] accepted /finops request user={user_name} question_length={len(question)}")

    if not question:
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({
                "response_type": "ephemeral",
                "text": "사용법: `/finops 이번 달 AWS 비용 알려줘`",
            }),
        }

    # Slack 3초 제한 안에 즉시 응답 후 비동기 처리
    lambda_client.invoke(
        FunctionName=FINOPS_AGENT_ARN,
        InvocationType="Event",
        Payload=json.dumps({
            "question": question,
            "response_url": response_url,
            "user_name": user_name,
        }),
    )

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "response_type": "in_channel",
            "text": f"*{user_name}*: _{question}_\n조회 중입니다... ⏳",
        }),
    }
