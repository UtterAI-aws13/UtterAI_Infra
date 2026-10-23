"""Real KEDA/HPA with ElasticMQ. No AWS credentials, inference or EC2 calls."""

import importlib.util, json, pathlib, subprocess, time, datetime, urllib.request
import boto3
from botocore.config import Config

ROOT = pathlib.Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "test_integration", ROOT / "tests/capacity-policy/integration.py"
)
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)
lab = t.lab
p = t.p
ns = t.ns
forward = subprocess.Popen(
    [
        "kubectl",
        "--kubeconfig",
        lab.KUBECONFIG,
        "--context",
        lab.CONTEXT,
        "port-forward",
        "-n",
        ns,
        "service/elasticmq",
        "19324:9324",
    ],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
results = []
try:

    def reachable():
        try:
            with urllib.request.urlopen("http://127.0.0.1:19324", timeout=0.5):
                return True
        except urllib.error.HTTPError:
            return True
        except OSError:
            return False

    lab.wait(reachable, 30)
    sqs = boto3.client(
        "sqs",
        region_name="us-east-1",
        endpoint_url="http://127.0.0.1:19324",
        aws_access_key_id="local-only",
        aws_secret_access_key="local-only",
        config=Config(connect_timeout=1, read_timeout=3, retries={"max_attempts": 0}),
    )
    sqs.create_queue(QueueName="capacity-lab")
    url = "http://127.0.0.1:19324/000000000000/capacity-lab"
    sqs.purge_queue(QueueUrl=url)
    # Reuse the already tested workload; keep existing generation/rollout history.
    t.scale(0)
    lab.wait(lambda: t.settled(0) is not None)
    now = datetime.datetime.now(datetime.timezone.utc)
    start = now - datetime.timedelta(minutes=5)
    end = now + datetime.timedelta(minutes=30)
    cron = {
        "type": "cron",
        "metadata": {
            "timezone": "UTC",
            "start": f"{start.minute} {start.hour} * * *",
            "end": f"{end.minute} {end.hour} * * *",
            "desiredReplicas": "2",
        },
    }
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "local-sqs", "namespace": ns},
        "stringData": {"access": "local-only", "secret": "local-only"},
    }
    auth = {
        "apiVersion": "keda.sh/v1alpha1",
        "kind": "TriggerAuthentication",
        "metadata": {"name": "local-sqs", "namespace": ns},
        "spec": {
            "secretTargetRef": [
                {"parameter": "awsAccessKeyID", "name": "local-sqs", "key": "access"},
                {
                    "parameter": "awsSecretAccessKey",
                    "name": "local-sqs",
                    "key": "secret",
                },
            ]
        },
    }
    obj = {
        "apiVersion": "keda.sh/v1alpha1",
        "kind": "ScaledObject",
        "metadata": {"name": "demo", "namespace": ns},
        "spec": {
            "scaleTargetRef": {"name": "demo"},
            "pollingInterval": 2,
            "cooldownPeriod": 5,
            "minReplicaCount": 0,
            "maxReplicaCount": 6,
            "advanced": {
                "horizontalPodAutoscalerConfig": {
                    "behavior": {
                        "scaleDown": {
                            "stabilizationWindowSeconds": 0,
                            "policies": [
                                {"type": "Pods", "value": 10, "periodSeconds": 15}
                            ],
                        }
                    }
                }
            },
            "triggers": [
                {
                    "type": "aws-sqs-queue",
                    "authenticationRef": {"name": "local-sqs"},
                    "metadata": {
                        "queueURL": "http://elasticmq.capacity-test.svc:9324/000000000000/capacity-lab",
                        "awsEndpoint": "http://elasticmq.capacity-test.svc:9324",
                        "awsRegion": "us-east-1",
                        "queueLength": "1",
                        "scaleOnInFlight": "true",
                    },
                },
                cron,
            ],
        },
    }

    def measure(name, action, n):
        started = time.monotonic()
        action()
        lab.wait(
            lambda: (
                t.settled(n) if n else (True if t.settled(0) is not None else False)
            ),
            120,
        )
        row = {
            "test": name,
            "replicas": n,
            "seconds": round(time.monotonic() - started, 3),
            "passed": True,
        }
        results.append(row)
        print(json.dumps(row), flush=True)

    measure(
        "scheduled-minimum-with-empty-queue", lambda: lab.apply([secret, auth, obj]), 2
    )
    measure(
        "six-sqs-messages-scale-to-six",
        lambda: sqs.send_message_batch(
            QueueUrl=url,
            Entries=[
                {"Id": str(i), "MessageBody": json.dumps({"synthetic": i})}
                for i in range(6)
            ],
        ),
        6,
    )
    t.assert_equal(t.tiers(t.pods()).count("base"), 2)
    t.assert_equal(t.tiers(t.pods()).count("burst"), 4)
    measure(
        "empty-queue-returns-to-scheduled-minimum",
        lambda: sqs.purge_queue(QueueUrl=url),
        2,
    )
    # Remove scheduled minimum to emulate off-hours; real SQS scaler remains active.
    obj["spec"]["triggers"] = obj["spec"]["triggers"][:1]
    measure("off-hours-idle-scales-to-zero", lambda: lab.apply([obj]), 0)
    measure(
        "off-hours-request-wakes-worker",
        lambda: sqs.send_message(QueueUrl=url, MessageBody="{}"),
        1,
    )
    t.assert_equal(t.tiers(t.pods()).count("base"), 1)
    sqs.purge_queue(QueueUrl=url)
finally:
    forward.terminate()
    forward.wait(timeout=10)
    (ROOT / "tests/capacity-policy/results/keda.json").write_text(
        json.dumps(
            {
                "scope": "KEDA 2.16.1 + HPA + ElasticMQ, synthetic messages",
                "results": results,
            },
            indent=2,
        )
        + "\n"
    )
