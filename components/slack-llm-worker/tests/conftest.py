import hashlib
import hmac
import json
from dataclasses import asdict

import pytest

from slack_worker.ingress import Job, Policy

NOW = 1800000000
SECRET = "offline-test-signing-secret"


@pytest.fixture
def policy():
    return Policy("A1", "T1", frozenset({"C1"}), frozenset({"U1"}), "B1")


@pytest.fixture
def job():
    return asdict(Job("event:T1:Ev1", "T1", "C1", "U1", "질문", NOW, "mention", str(NOW) + ".001"))


def signed(body=None, stamp=NOW, kind="application/json", **extra):
    if body is None:
        body = {"type": "event_callback", "api_app_id": "A1", "team_id": "T1", "event_id": "Ev1",
                "event": {"type": "app_mention", "channel": "C1", "user": "U1",
                          "text": "<@B1> 질문", "ts": str(NOW) + ".001"}}
    raw = json.dumps(body, ensure_ascii=False) if isinstance(body, (dict, list)) else body
    signature = "v0=" + hmac.new(SECRET.encode(), f"v0:{stamp}:{raw}".encode(), hashlib.sha256).hexdigest()
    return {"body": raw, "headers": {"Content-Type": kind, "X-Slack-Request-Timestamp": str(stamp),
                                     "X-Slack-Signature": signature, **extra}}
