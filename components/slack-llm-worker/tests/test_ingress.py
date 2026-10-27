import base64
import json
from urllib.parse import urlencode

import pytest

from slack_worker.ingress import Ingress, callback_allowed
from slack_worker.local import Clock, LocalQueue
from conftest import NOW, SECRET, signed


@pytest.fixture
def setup(policy):
    clock = Clock(NOW + 1)
    queue = LocalQueue(clock)
    return Ingress(SECRET, queue, policy, clock), queue


def test_signed_mention_and_thread(setup):
    app, q = setup
    event = signed()
    p = json.loads(event["body"]); p["event"]["thread_ts"] = "1799999990.001"
    assert app.handle(signed(p))["statusCode"] == 200
    job = json.loads(q.receive()[0]["Body"])
    assert job["thread_ts"] == "1799999990.001" and job["text"] == "질문"


def test_retry_header_does_not_drop_first_success(setup):
    app, q = setup
    assert app.handle(signed(**{"X-Slack-Retry-Num": "1"}))["statusCode"] == 200
    assert len(q.messages) == 1


def test_queue_failure_is_not_acknowledged(setup):
    app, q = setup
    def fail(_):
        raise TimeoutError()
    q.send = fail
    assert app.handle(signed())["statusCode"] == 503


@pytest.mark.parametrize("stamp", [NOW - 301, NOW + 302])
def test_replay_and_future_rejected(setup, stamp):
    app, q = setup
    assert app.handle(signed(stamp=stamp))["statusCode"] == 401
    assert not q.messages


def test_tampering_and_base64(setup):
    app, q = setup
    e = signed(); e["body"] += " "
    assert app.handle(e)["statusCode"] == 401
    e = signed(); e["body"] = base64.b64encode(e["body"].encode()).decode(); e["isBase64Encoded"] = True
    assert app.handle(e)["statusCode"] == 200


def test_signed_challenge(setup):
    app, q = setup
    assert json.loads(app.handle(signed({"type": "url_verification", "challenge": "xyz"}))["body"]) == {"challenge": "xyz"}
    assert not q.messages


@pytest.mark.parametrize("change", ["bot", "other_event", "other_team", "other_user", "other_app"])
def test_bot_and_allowlists(setup, change):
    app, q = setup
    p = json.loads(signed()["body"])
    if change == "bot": p["event"]["bot_id"] = "B2"
    if change == "other_event": p["event"]["type"] = "message"
    if change == "other_team": p["team_id"] = "T2"
    if change == "other_user": p["event"]["user"] = "U2"
    if change == "other_app": p["api_app_id"] = "A2"
    assert app.handle(signed(p))["statusCode"] in (200, 403)
    assert not q.messages


def test_slash_key_is_per_invocation_and_private(setup):
    app, q = setup
    p = dict(api_app_id="A1", team_id="T1", channel_id="C1", user_id="U1", command="/ask",
             text="동일 질문", trigger_id="one", response_url="https://hooks.slack.com/commands/T/B/secret")
    def send(): return app.handle(signed(urlencode(p), kind="application/x-www-form-urlencoded"))
    assert json.loads(send()["body"])["response_type"] == "ephemeral"
    send(); p["trigger_id"] = "two"; send()
    jobs = [json.loads(m["Body"]) for m in q.messages]
    assert jobs[0]["key"] == jobs[1]["key"] != jobs[2]["key"]


@pytest.mark.parametrize("url", ["http://hooks.slack.com/commands/a", "https://hooks.slack.com.evil/commands/a",
                               "https://hooks.slack.com@evil/commands/a", "https://127.0.0.1/commands/a",
                               "https://hooks.slack.com:444/commands/a", "https://hooks.slack.com/services/a"])
def test_callback_ssrf_rejected(url):
    assert not callback_allowed(url)


@pytest.mark.parametrize("raw", ["[]", "null", "{", "123"])
def test_malformed_signed_json(setup, raw):
    assert setup[0].handle(signed(raw))["statusCode"] == 400


def test_oversized(setup):
    assert setup[0].handle(signed("x" * 33000))["statusCode"] == 413
