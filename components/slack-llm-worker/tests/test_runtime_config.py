import io
import json

import pytest

from slack_worker.adapters import Slack, api_base_allowed
from slack_worker.pipeline import OpenAICompatModel
from slack_worker.runtime import LoggedModel, model_from_env


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
        def generate(self, text, context):
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
