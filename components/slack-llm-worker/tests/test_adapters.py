import io
import json
from urllib.error import HTTPError

import boto3
import pytest
from botocore.stub import Stubber

from slack_worker.adapters import SQSQueue, Slack
from slack_worker.pipeline import BedrockModel
from slack_worker.store import Conflict, DynamoStore
from slack_worker.worker import AmbiguousDelivery, PermanentDelivery, RateLimited


def client(service):
    return boto3.client(service, region_name="ap-northeast-2", aws_access_key_id="offline",
                        aws_secret_access_key="offline")


def test_sqs_sdk_shapes(job):
    sqs = client("sqs"); q = SQSQueue(sqs, "https://sqs.ap-northeast-2.amazonaws.com/123456789012/jobs")
    with Stubber(sqs) as stub:
        stub.add_response("send_message", {"MessageId": "one"},
                          {"QueueUrl": q.url, "MessageBody": json.dumps(job, ensure_ascii=False)})
        stub.add_response("receive_message", {}, {"QueueUrl": q.url, "MaxNumberOfMessages": 1,
                          "WaitTimeSeconds": 20, "VisibilityTimeout": 90,
                          "MessageSystemAttributeNames": ["ApproximateReceiveCount"]})
        stub.add_response("change_message_visibility", {}, {"QueueUrl": q.url,
                          "ReceiptHandle": "receipt", "VisibilityTimeout": 90})
        stub.add_response("delete_message", {}, {"QueueUrl": q.url, "ReceiptHandle": "receipt"})
        q.send(job); assert q.receive() == []; q.visibility("receipt", 90); q.delete("receipt")
        stub.assert_no_pending_responses()


def test_dynamo_cas_and_consistent_read():
    dynamo = client("dynamodb"); table = "slack-worker-state"; store = DynamoStore(dynamo, table)
    state = {"rev": 1, "expires": 1800000000, "phase": "MODEL"}
    with Stubber(dynamo) as stub:
        stub.add_response("get_item", {}, {"TableName": table, "Key": {"pk": {"S": "job"}}, "ConsistentRead": True})
        stub.add_response("put_item", {}, {"TableName": table,
                          "Item": {"pk": {"S": "job"}, "rev": {"N": "1"}, "body": {"S": json.dumps(state)},
                                   "expires": {"N": "1800000000"}},
                          "ConditionExpression": "attribute_not_exists(pk)"})
        stub.add_client_error("put_item", service_error_code="ConditionalCheckFailedException")
        assert store.get("job") is None
        store.cas("job", 0, state)
        with pytest.raises(Conflict): store.cas("job", 0, state)
        stub.assert_no_pending_responses()


def test_dynamo_update_is_version_fenced():
    dynamo = client("dynamodb"); store = DynamoStore(dynamo, "state")
    state = {"rev": 2, "expires": 1800000000, "phase": "RESULT"}
    with Stubber(dynamo) as stub:
        stub.add_response("put_item", {}, {"TableName": "state",
                          "Item": {"pk": {"S": "job"}, "rev": {"N": "2"},
                                   "body": {"S": json.dumps(state)}, "expires": {"N": "1800000000"}},
                          "ConditionExpression": "rev = :r", "ExpressionAttributeValues": {":r": {"N": "1"}}})
        store.cas("job", 1, state)
        stub.assert_no_pending_responses()


def test_model_output_cannot_ping_channel(job):
    opener = Opener(b'{"ok":true,"ts":"123.4"}')
    Slack("fake", opener).send(job, "<!channel> <@U123>")
    assert json.loads(opener.requests[0][0].data)["text"] == "&lt;!channel&gt; &lt;@U123&gt;"


def test_bedrock_converse_contract():
    bedrock = client("bedrock-runtime")
    with Stubber(bedrock) as stub:
        stub.add_response("converse", {"output": {"message": {"role": "assistant", "content": [{"text": "answer"}]}},
                          "stopReason": "end_turn", "usage": {"inputTokens": 5, "outputTokens": 2, "totalTokens": 7},
                          "metrics": {"latencyMs": 10}})
        result = BedrockModel(bedrock, "approved-model-id").generate("question", "reference")
        assert result["text"] == "answer" and result["usage"]["inputTokens"] == 5
        stub.assert_no_pending_responses()


class Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *_): pass


class Opener:
    def __init__(self, result): self.result = result; self.requests = []
    def open(self, req, timeout):
        self.requests.append((req, timeout))
        if isinstance(self.result, Exception): raise self.result
        return Response(self.result)


def test_slack_thread_and_no_auto_retry(job):
    opener = Opener(b'{"ok":true,"ts":"123.4"}')
    assert Slack("fake-token", opener).send(job, "answer") == {"ts": "123.4"}
    req, timeout = opener.requests[0]
    body = json.loads(req.data)
    assert body["thread_ts"] == job["thread_ts"] and body["mrkdwn"] is False
    assert len(opener.requests) == 1 and timeout == 8


def test_response_url_never_receives_bot_token(job):
    opener = Opener(b"ok")
    job.update(kind="slash", response_url="https://hooks.slack.com/commands/T/B/test")
    Slack("fake-token", opener).send(job, "answer")
    req, _ = opener.requests[0]
    assert not req.has_header("Authorization")
    assert json.loads(req.data)["response_type"] == "ephemeral"


@pytest.mark.parametrize("code,expected", [(429, RateLimited), (500, AmbiguousDelivery),
                                         (302, AmbiguousDelivery), (403, PermanentDelivery)])
def test_slack_http_outcomes(job, code, expected):
    opener = Opener(HTTPError("https://slack.com", code, "", {"Retry-After": "42"}, None))
    with pytest.raises(expected) as e: Slack("fake-token", opener).send(job, "answer")
    if code == 429: assert e.value.seconds == 42
    assert len(opener.requests) == 1


@pytest.mark.parametrize("raw,expected", [(b'{"ok":false,"error":"invalid_auth"}', PermanentDelivery),
                                        (b'{"ok":false,"error":"internal_error"}', AmbiguousDelivery),
                                        (b'{"ok":true}', AmbiguousDelivery), (b'bad', AmbiguousDelivery)])
def test_slack_json_outcomes(job, raw, expected):
    with pytest.raises(expected): Slack("fake-token", Opener(raw)).send(job, "answer")
