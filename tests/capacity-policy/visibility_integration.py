"""Measure real lifecycle heartbeat expiry against local ElasticMQ, not AWS SQS."""

import importlib.util, json, pathlib, subprocess, time
import boto3
from botocore.config import Config

ROOT = pathlib.Path(__file__).resolve().parents[2]


def load(name, path):
    s = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


lab = load("lab", ROOT / "scripts/capacity-policy/local_cluster.py")
life = load("life", ROOT.parent / "UtterAI_AI/app/workers/lifecycle.py")
p = subprocess.Popen(
    [
        "kubectl",
        "--kubeconfig",
        lab.KUBECONFIG,
        "--context",
        lab.CONTEXT,
        "port-forward",
        "-n",
        "capacity-test",
        "service/elasticmq",
        "19325:9324",
    ],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    time.sleep(1)
    sqs = boto3.client(
        "sqs",
        region_name="us-east-1",
        endpoint_url="http://127.0.0.1:19325",
        aws_access_key_id="local-only",
        aws_secret_access_key="local-only",
        config=Config(connect_timeout=1, read_timeout=10, retries={"max_attempts": 0}),
    )
    sqs.create_queue(QueueName="lease-test")
    url = "http://127.0.0.1:19325/000000000000/lease-test"
    sqs.purge_queue(QueueUrl=url)
    sqs.send_message(QueueUrl=url, MessageBody="synthetic lease test")
    msg = sqs.receive_message(QueueUrl=url, VisibilityTimeout=life.VISIBILITY_SECONDS)[
        "Messages"
    ][0]

    # Execute the production heartbeat once, then stop (simulated process death).
    class Once:
        def is_set(self):
            return False

        def wait(self, interval):
            return True

    started = time.monotonic()
    life.heartbeat(sqs, url, msg["ReceiptHandle"], Once())
    found = None
    while time.monotonic() - started < 150:
        found = sqs.receive_message(QueueUrl=url, WaitTimeSeconds=5).get("Messages")
        if found:
            break
    assert found, "message did not become visible"
    elapsed = time.monotonic() - started
    assert life.VISIBILITY_SECONDS - 1 <= elapsed < life.VISIBILITY_SECONDS + 10, (
        elapsed
    )
    sqs.delete_message(QueueUrl=url, ReceiptHandle=found[0]["ReceiptHandle"])
    report = {
        "scope": "ElasticMQ; one production heartbeat followed by no further extension",
        "configured_visibility_seconds": life.VISIBILITY_SECONDS,
        "observed_redelivery_seconds": round(elapsed, 3),
        "old_visibility_setting_seconds": 1800,
        "old_setting_measured": False,
        "passed": True,
    }
    (ROOT / "tests/capacity-policy/results/visibility.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    print(json.dumps(report), flush=True)
finally:
    p.terminate()
    p.wait(timeout=10)
