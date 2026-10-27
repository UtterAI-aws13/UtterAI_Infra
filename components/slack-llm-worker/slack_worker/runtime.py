"""AWS factories are lazy. Nothing is deployed by this module."""
import json
import os
import signal
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from .adapters import SQSQueue, Slack
from .ingress import Ingress, Policy
from .pipeline import BedrockModel, Pipeline
from .store import DynamoStore
from .worker import Worker


def policy_from_env():
    return Policy(os.environ["SLACK_APP_ID"], os.environ["SLACK_TEAM_ID"],
                  frozenset(filter(None, os.environ["SLACK_CHANNELS"].split(","))),
                  frozenset(filter(None, os.environ["SLACK_USERS"].split(","))),
                  os.environ["SLACK_BOT_ID"])


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
            _ingress = Ingress(secret, queue, policy_from_env())
            _loaded = time.monotonic()
        return _ingress.handle(event)
    except Exception:
        return {"statusCode": 503, "body": "unavailable"}


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
    if os.environ.get("ENABLE_PAID_MODEL") != "approved":
        raise SystemExit("Paid execution disabled: approve infrastructure, access and budget first.")
    import boto3
    count = int(os.environ.get("WORKER_CONCURRENCY", "2"))
    if not 1 <= count <= 8:
        raise SystemExit("concurrency must be 1..8")
    policy = policy_from_env()
    queue = SQSQueue(boto3.client("sqs", config=sdk_config(2, 25)), os.environ["QUEUE_URL"])
    state_client = boto3.client("dynamodb", config=sdk_config(2, 3))
    token = json.loads(boto3.client("secretsmanager", config=sdk_config(2, 3)).get_secret_value(
        SecretId=os.environ["SLACK_BOT_SECRET_ARN"])["SecretString"])["bot_token"]
    model = BedrockModel(boto3.client("bedrock-runtime", config=sdk_config(2, 60)),
                         os.environ["BEDROCK_MODEL_ID"])
    worker = Worker(DynamoStore(state_client, os.environ["STATE_TABLE"]),
                    Pipeline(model, os.environ.get("APPROVED_CORPUS")), Slack(token), policy)
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
