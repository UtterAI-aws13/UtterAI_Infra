import io
import json
import socket
from urllib.error import HTTPError, URLError

import pytest

from slack_worker.adapters import Slack, api_base_allowed
from slack_worker.pipeline import OpenAICompatModel
from slack_worker.runtime import LoggedModel, model_from_env
from slack_worker.worker import ModelNotExecuted, ModelRejected


class Opener:
    def __init__(self, body):
        self.body, self.requests = body, []

    def open(self, req, timeout):
        self.requests.append((req, timeout))
        return io.BytesIO(json.dumps(self.body).encode())


def test_openai_compat_request_and_usage_mapping():
    opener = Opener({"choices": [{"message": {"content": "답변"}}],
                     "usage": {"prompt_tokens": 11, "completion_tokens": 3}})
    model = OpenAICompatModel("http://127.0.0.1:4000/v1/", "fast", api_key="sk-local", opener=opener)
    result = model.generate("질문", "근거")
    req, timeout = opener.requests[0]
    sent = json.loads(req.data)
    assert req.full_url == "http://127.0.0.1:4000/v1/chat/completions" and timeout == 60
    assert req.get_header("Authorization") == "Bearer sk-local"
    assert sent["model"] == "fast" and sent["max_tokens"] == 700 and sent["stream"] is False
    assert result == {"text": "답변", "usage": {"inputTokens": 11, "outputTokens": 3}}


class FailingOpener:
    def __init__(self, error):
        self.error = error

    def open(self, req, timeout):
        raise self.error


def http_error(code, body=b"", headers=None):
    return HTTPError("http://127.0.0.1:4000/v1/chat/completions", code, "x", headers or {}, io.BytesIO(body))


@pytest.mark.parametrize("error, expected", [
    (URLError(ConnectionRefusedError()), ("not_executed", None)),
    (URLError(socket.gaierror()), ("not_executed", None)),
    (http_error(429, headers={"Retry-After": "12"}), ("not_executed", 12)),
    (http_error(401, b'{"error": "key not allowed to access model"}'), ("rejected", "model_access_denied")),
    (http_error(400, b'{"error": "Budget has been exceeded"}'), ("rejected", "model_budget_exceeded")),
    (http_error(422, b'{"error": "ExceededTokenBudget: budget exceeded"}'), ("rejected", "model_budget_exceeded")),
])
def test_gateway_failures_map_to_failure_points(error, expected):
    model = OpenAICompatModel("http://127.0.0.1:4000/v1", "fast", opener=FailingOpener(error))
    kind, detail = expected
    with pytest.raises(ModelNotExecuted if kind == "not_executed" else ModelRejected) as info:
        model.generate("q", "")
    assert (info.value.retry_after if kind == "not_executed" else info.value.reason) == detail


@pytest.mark.parametrize("error", [URLError(TimeoutError("timed out")), http_error(500), http_error(502)])
def test_ambiguous_gateway_failures_are_not_refunded(error):
    model = OpenAICompatModel("http://127.0.0.1:4000/v1", "fast", opener=FailingOpener(error))
    with pytest.raises(Exception) as info:
        model.generate("q", "")
    assert not isinstance(info.value, (ModelNotExecuted, ModelRejected))


class SequenceOpener:
    """Scripted responses per URL suffix; records which URLs were hit."""
    def __init__(self, script):
        self.script, self.hits = script, []

    def open(self, req, timeout):
        url = req.full_url
        self.hits.append(url.rsplit("/", 1)[-1])
        outcome = self.script[url.rsplit("/", 1)[-1]].pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        resp = io.BytesIO(json.dumps(outcome).encode())
        resp.status = 200
        return resp


OK = {"choices": [{"message": {"content": "ok"}}]}


def test_readiness_probe_refunds_while_gateway_restarts():
    from http.client import RemoteDisconnected
    opener = SequenceOpener({"completions": [URLError(ConnectionRefusedError()), OK],
                             "readiness": [RemoteDisconnected(), {"status": "healthy"}]})
    model = OpenAICompatModel("http://127.0.0.1:4000/v1", "fast", opener=opener,
                              health_url="http://127.0.0.1:4000/health/readiness")
    with pytest.raises(ModelNotExecuted):
        model.generate("q", "")          # gateway down: refused
    with pytest.raises(ModelNotExecuted):
        model.generate("q", "")          # port open, app not serving: probe fails, no model call
    assert model.generate("q", "")["text"] == "ok"
    assert opener.hits == ["completions", "readiness", "readiness", "completions"]


def test_no_probe_on_the_healthy_path():
    opener = SequenceOpener({"completions": [OK, OK]})
    model = OpenAICompatModel("http://127.0.0.1:4000/v1", "fast", opener=opener,
                              health_url="http://127.0.0.1:4000/health/readiness")
    model.generate("q", ""); model.generate("q", "")
    assert opener.hits == ["completions", "completions"]


def test_job_key_is_sent_for_spend_attribution():
    opener = Opener({"choices": [{"message": {"content": "ok"}}]})
    OpenAICompatModel("http://127.0.0.1:4000/v1", "fast", opener=opener).generate("q", "", user="event:T1:Ev1")
    assert json.loads(opener.requests[0][0].data)["user"] == "event:T1:Ev1"


def test_openai_compat_rejects_unbounded_output():
    with pytest.raises(ValueError):
        OpenAICompatModel("http://127.0.0.1:4000/v1", "fast", max_tokens=4000)


def test_bedrock_requires_paid_approval():
    with pytest.raises(SystemExit):
        model_from_env({"MODEL_PROVIDER": "bedrock", "BEDROCK_MODEL_ID": "m"}, lambda: None)


def test_loopback_model_runs_without_paid_approval():
    model = model_from_env({"MODEL_PROVIDER": "openai_compat", "MODEL_BASE_URL": "http://localhost:11434/v1",
                            "MODEL_NAME": "llama3.2:1b"})
    assert isinstance(model, LoggedModel)


def test_remote_openai_compat_requires_paid_approval():
    env = {"MODEL_PROVIDER": "openai_compat", "MODEL_BASE_URL": "https://gateway.example.com/v1",
           "MODEL_NAME": "fast"}
    with pytest.raises(SystemExit):
        model_from_env(env)
    assert isinstance(model_from_env({**env, "ENABLE_PAID_MODEL": "approved"}), LoggedModel)


def test_logged_model_reports_failures(capsys):
    class Broken:
        def generate(self, text, context, user=""):
            raise TimeoutError()
    with pytest.raises(TimeoutError):
        LoggedModel(Broken(), "fast").generate("q", "")
    line = json.loads(capsys.readouterr().out)
    assert line["event"] == "model_call" and line["outcome"] == "error"


@pytest.mark.parametrize("url, allowed", [
    ("https://slack.com/api", True),
    ("http://127.0.0.1:9000/api", True),
    ("http://localhost:9000/api", True),
    ("https://evil.example.com/api", False),
    ("http://10.0.0.5/api", False),
    ("https://slack.com.evil.example.com/api", False),
])
def test_slack_api_base_is_slack_or_loopback(url, allowed):
    assert api_base_allowed(url) is allowed
    if not allowed:
        with pytest.raises(ValueError):
            Slack("xoxb-test", api_base=url)
