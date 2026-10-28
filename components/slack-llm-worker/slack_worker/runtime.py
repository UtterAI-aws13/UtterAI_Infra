"""AWS factories are lazy. Nothing is deployed by this module."""
import json
import os
import signal
import threading
import time
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor

from .adapters import SLACK_API, QueueStats, SQSQueue, Slack
from .ingress import Ingress, Policy
from .pipeline import BedrockModel, OpenAICompatModel, Pipeline
from .store import DynamoStore
from .worker import ModelNotExecuted, ModelRejected, Worker

OPS_KINDS = ("alert", "ops", "ops_click")


def policy_from_env():
    return Policy(os.environ["SLACK_APP_ID"], os.environ["SLACK_TEAM_ID"],
                  frozenset(filter(None, os.environ["SLACK_CHANNELS"].split(","))),
                  frozenset(filter(None, os.environ["SLACK_USERS"].split(","))),
                  os.environ["SLACK_BOT_ID"], alert_channel=os.environ.get("SLACK_ALERT_CHANNEL", ""))


def sdk_config(connect=1, read=1):
    from botocore.config import Config
    return Config(connect_timeout=connect, read_timeout=read, retries={"total_max_attempts": 1})


_ingress = None
_loaded = 0


def lambda_handler(event, context):
    global _ingress, _loaded
    try:
        import boto3
        # Warm cache has bounded age so rotation eventually takes effect.
        if _ingress is None or time.monotonic() - _loaded > 300:
            secret = json.loads(boto3.client("secretsmanager", config=sdk_config(.3, .4)).get_secret_value(
                SecretId=os.environ["SLACK_SIGNING_SECRET_ARN"])["SecretString"])["signing_secret"]
            queue = SQSQueue(boto3.client("sqs", config=sdk_config(.3, .6)), os.environ["QUEUE_URL"])
            alert_token = ""
            if os.environ.get("ALERT_TOKEN_SECRET_ARN"):
                alert_token = json.loads(boto3.client("secretsmanager", config=sdk_config(.3, .4)).get_secret_value(
                    SecretId=os.environ["ALERT_TOKEN_SECRET_ARN"])["SecretString"])["alert_token"]
            _ingress = Ingress(secret, queue, policy_from_env(), alert_token=alert_token)
            _loaded = time.monotonic()
        return _ingress.handle(event)
    except Exception:
        return {"statusCode": 503, "body": "unavailable"}


class LoggedModel:
    """Emits one structured line per model call so tests can count calls without a fake."""
    def __init__(self, model, name):
        self.model, self.name = model, name

    def generate(self, text, context, user=""):
        started = time.monotonic()
        outcome, usage, error = "error", {}, None
        try:
            result = self.model.generate(text, context, user=user)
            outcome, usage = "ok", result.get("usage", {})
            return result
        except ModelNotExecuted:
            outcome = "not_executed"
            raise
        except ModelRejected as e:
            outcome, error = "rejected", e.reason
            raise
        except Exception as e:
            # Class and HTTP status only: never the message, which can echo prompts or URLs.
            error = type(e).__name__ + (f":{e.code}" if hasattr(e, "code") else "")
            raise
        finally:
            print(json.dumps({"event": "model_call", "model": self.name, "outcome": outcome, "job": user,
                              "error": error, "seconds": round(time.monotonic() - started, 3),
                              "usage": usage}))


def model_from_env(env, bedrock_client=None):
    """Paid models need explicit approval; a loopback OpenAI-compatible endpoint does not."""
    provider = env.get("MODEL_PROVIDER", "bedrock")
    if provider == "openai_compat":
        base = env["MODEL_BASE_URL"]
        local = urllib.parse.urlsplit(base).hostname in ("127.0.0.1", "localhost")
        if not local and env.get("ENABLE_PAID_MODEL") != "approved":
            raise SystemExit("Remote model endpoint disabled: approve access and budget first.")
        return LoggedModel(OpenAICompatModel(base, env["MODEL_NAME"], env.get("MODEL_API_KEY", ""),
                                             health_url=env.get("MODEL_HEALTH_URL", "")), env["MODEL_NAME"])
    if provider == "bedrock":
        if env.get("ENABLE_PAID_MODEL") != "approved":
            raise SystemExit("Paid execution disabled: approve infrastructure, access and budget first.")
        return LoggedModel(BedrockModel(bedrock_client(), env["BEDROCK_MODEL_ID"]), env["BEDROCK_MODEL_ID"])
    raise SystemExit("MODEL_PROVIDER must be bedrock or openai_compat")


class Router:
    """Q&A jobs go to the Worker, ChatOps jobs to Ops. Both keep owner/until on the job record,
    so one heartbeat implementation serves both."""
    def __init__(self, worker, ops=None):
        self.worker, self.ops = worker, ops

    def handle(self, job, owner=None, healthy=lambda: True):
        if job.get("kind") in OPS_KINDS:
            if not self.ops:
                return "retry", 90
            return self.ops.handle(job, owner or str(uuid.uuid4()), healthy)
        return self.worker.handle(job, owner, healthy)

    def heartbeat(self, key, owner):
        self.worker.heartbeat(key, owner)


def ops_from_env(env, store, slack, policy, sqs_client=None):
    """ChatOps is off unless OPS_TARGETS is set. The Kubernetes token decides what it can do."""
    if not env.get("OPS_TARGETS"):
        return None
    from .k8s import Kube
    from .ops import Advisor, Ops, OpsPolicy
    targets = json.loads(env["OPS_TARGETS"])
    if not isinstance(targets, dict) or not all({"namespace", "deployment"} <= set(t) for t in targets.values()):
        raise SystemExit("OPS_TARGETS must map name -> {namespace, deployment}")
    operators = frozenset(filter(None, env.get("OPS_OPERATORS", "").split(",")))
    if not operators or not operators <= policy.users:
        raise SystemExit("OPS_OPERATORS must be a non-empty subset of SLACK_USERS")
    advisor = None
    if env.get("ADVISOR_MODEL_NAME"):
        base = env["MODEL_BASE_URL"]
        local = urllib.parse.urlsplit(base).hostname in ("127.0.0.1", "localhost")
        if not local and env.get("ENABLE_PAID_MODEL") != "approved":
            raise SystemExit("Remote model endpoint disabled: approve access and budget first.")
        # Short budget: an alert must not wait a minute for enrichment.
        # Its own gateway key (ADVISOR_API_KEY) so alert enrichment has its own model, limit and budget.
        model = OpenAICompatModel(base, env["ADVISOR_MODEL_NAME"], env.get("ADVISOR_API_KEY", env.get("MODEL_API_KEY", "")),
                                  max_tokens=300, timeout=30, health_url=env.get("MODEL_HEALTH_URL", ""),
                                  system=Advisor.SYSTEM, temperature=0)
        advisor = Advisor(LoggedModel(model, env["ADVISOR_MODEL_NAME"]))
    pipeline = tuple(json.loads(env.get("OPS_PIPELINE", "[]")))
    if not all({"stage", "queue", "target"} <= set(st) and st["target"] in targets for st in pipeline):
        raise SystemExit("OPS_PIPELINE stages need stage, queue and a target from OPS_TARGETS")
    verify_checks = int(env.get("OPS_VERIFY_CHECKS", "5"))
    if not 1 <= verify_checks <= 6:
        raise SystemExit("OPS_VERIFY_CHECKS must be 1..6: each check is one SQS receive (maxReceiveCount 8)")
    after_patch = (lambda: os._exit(1)) if env.get("OPS_FAULT") == "exit_after_patch" else None
    return Ops(store, Kube(env["K8S_API"], env["K8S_TOKEN_FILE"], env["K8S_CA_FILE"]), slack, policy,
               OpsPolicy(targets, operators, int(env.get("OPS_APPROVAL_TTL", "600")), pipeline=pipeline,
                         verify_interval=int(env.get("OPS_VERIFY_INTERVAL", "60")), verify_checks=verify_checks,
                         backlog_warn=int(env.get("OPS_BACKLOG_WARN", "100"))),
               advisor=advisor, runbooks=env.get("OPS_RUNBOOKS"), after_patch=after_patch,
               queues=QueueStats(sqs_client) if pipeline and sqs_client else None)


def process_message(queue, worker, raw):
    receipt = raw["ReceiptHandle"]
    try:
        job = json.loads(raw["Body"])
        if not isinstance(job, dict):
            raise ValueError()
        owner = str(uuid.uuid4())
        stop, lost = threading.Event(), threading.Event()
        def heartbeat():
            deadline = time.monotonic() + 180
            while not stop.wait(10):
                try:
                    if time.monotonic() > deadline:
                        raise TimeoutError()
                    worker.heartbeat(job["key"], owner)
                    queue.visibility(receipt, 90)
                except Exception:
                    # No future side effect can pass the lease check after the lease expires.
                    # In-flight Slack requests remain ambiguous; never retry them automatically.
                    lost.set()
                    return
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            outcome, delay = worker.handle(job, owner, healthy=lambda: not lost.is_set())
        finally:
            stop.set()
            thread.join(timeout=3)
        print(json.dumps({"event": "job_outcome", "outcome": outcome,
                          "heartbeat_lost": lost.is_set(),
                          "receive_count": raw.get("Attributes", {}).get("ApproximateReceiveCount")}))
        if outcome == "ack":
            queue.delete(receipt)
        else:
            queue.visibility(receipt, delay)
    except Exception:
        # Malformed jobs are left for the SQS redrive policy; no silent delete.
        print(json.dumps({"event": "job_retry"}))


def main():
    import boto3
    model = model_from_env(os.environ, lambda: boto3.client("bedrock-runtime", config=sdk_config(2, 60)))
    count = int(os.environ.get("WORKER_CONCURRENCY", "2"))
    if not 1 <= count <= 8:
        raise SystemExit("concurrency must be 1..8")
    policy = policy_from_env()
    queue = SQSQueue(boto3.client("sqs", config=sdk_config(2, 25)), os.environ["QUEUE_URL"],
                     int(os.environ.get("RECEIVE_VISIBILITY", "90")))
    state_client = boto3.client("dynamodb", config=sdk_config(2, 3))
    token = json.loads(boto3.client("secretsmanager", config=sdk_config(2, 3)).get_secret_value(
        SecretId=os.environ["SLACK_BOT_SECRET_ARN"])["SecretString"])["bot_token"]
    store = DynamoStore(state_client, os.environ["STATE_TABLE"])
    slack = Slack(token, api_base=os.environ.get("SLACK_API_BASE", SLACK_API))
    worker = Router(Worker(store, Pipeline(model, os.environ.get("APPROVED_CORPUS")), slack, policy),
                    ops_from_env(os.environ, store, slack, policy, boto3.client("sqs", config=sdk_config(2, 5))))
    stopping = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())
    def slot():
        while not stopping.is_set():
            try:
                for message in queue.receive():
                    # Drain the message already received even if SIGTERM arrived during long polling.
                    process_message(queue, worker, message)
            except Exception:
                stopping.wait(2)
    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(slot) for _ in range(count)]
        for f in futures:
            f.result()


if __name__ == "__main__":
    main()
