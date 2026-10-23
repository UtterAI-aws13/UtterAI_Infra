"""Repeated real KEDA/HPA measurements using ElasticMQ and synthetic HTTP Pods.

Run after repeated_measurement.py to avoid changing its cluster load mid-run.
Messages are completed by this harness; Pods do not perform audio inference.
"""

import argparse
import datetime
import hashlib
import importlib.util
import json
import pathlib
import subprocess
import time
import urllib.error
import urllib.request

import boto3
from botocore.config import Config

ROOT = pathlib.Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "repeated", ROOT / "tests/capacity-policy/repeated_measurement.py"
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
lab = m.lab


def main(trials, off_hours, output):
    run = m.Measurement(trials, output, 0.25)
    name = "queue-repeat"
    forward = subprocess.Popen(
        [
            "kubectl",
            "--kubeconfig",
            lab.KUBECONFIG,
            "--context",
            lab.CONTEXT,
            "port-forward",
            "-n",
            m.NS,
            "service/elasticmq",
            "19326:9324",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def reachable():
        try:
            with urllib.request.urlopen("http://127.0.0.1:19326", timeout=0.5):
                return True
        except urllib.error.HTTPError:
            return True
        except OSError:
            return False

    try:
        run.wait(reachable, 30)
        sqs = boto3.client(
            "sqs",
            region_name="us-east-1",
            endpoint_url="http://127.0.0.1:19326",
            aws_access_key_id="local-only",
            aws_secret_access_key="local-only",
            config=Config(
                connect_timeout=1, read_timeout=5, retries={"max_attempts": 0}
            ),
        )
        sqs.create_queue(QueueName="capacity-repeated")
        url = "http://127.0.0.1:19326/000000000000/capacity-repeated"
        sqs.purge_queue(QueueUrl=url)
        lab.apply([run.deployment(name, 2)])
        run.wait(lambda: run.settled(name, 2))
        now = datetime.datetime.now(datetime.timezone.utc)
        start = now - datetime.timedelta(minutes=5)
        end = now + datetime.timedelta(minutes=90)
        secret = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "repeat-sqs", "namespace": m.NS},
            "stringData": {"access": "local-only", "secret": "local-only"},
        }
        auth = {
            "apiVersion": "keda.sh/v1alpha1",
            "kind": "TriggerAuthentication",
            "metadata": {"name": "repeat-sqs", "namespace": m.NS},
            "spec": {
                "secretTargetRef": [
                    {
                        "parameter": "awsAccessKeyID",
                        "name": "repeat-sqs",
                        "key": "access",
                    },
                    {
                        "parameter": "awsSecretAccessKey",
                        "name": "repeat-sqs",
                        "key": "secret",
                    },
                ]
            },
        }
        so = {
            "apiVersion": "keda.sh/v1alpha1",
            "kind": "ScaledObject",
            "metadata": {"name": name, "namespace": m.NS},
            "spec": {
                "scaleTargetRef": {"name": name},
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
                        "authenticationRef": {"name": "repeat-sqs"},
                        "metadata": {
                            "queueURL": "http://elasticmq.capacity-test.svc:9324/000000000000/capacity-repeated",
                            "awsEndpoint": "http://elasticmq.capacity-test.svc:9324",
                            "awsRegion": "us-east-1",
                            "queueLength": "1",
                            "scaleOnInFlight": "true",
                        },
                    },
                    {
                        "type": "cron",
                        "metadata": {
                            "timezone": "UTC",
                            "start": f"{start.minute} {start.hour} * * *",
                            "end": f"{end.minute} {end.hour} * * *",
                            "desiredReplicas": "2",
                        },
                    },
                ],
            },
        }
        lab.apply([secret, auth, so])

        def hpa_ready():
            obj = lab.get("scaledobject/" + name, m.NS)
            return any(
                c["type"] == "Ready" and c["status"] == "True"
                for c in obj.get("status", {}).get("conditions", [])
            )

        run.wait(hpa_ready, 120)
        run.wait(lambda: run.settled(name, 2))
        environment = {
            "utc": now.isoformat(),
            "context": lab.CONTEXT,
            "keda": "2.16.1",
            "elasticmq": "1.6.9",
            "trials_up_down": trials,
            "trials_off_hours": off_hours,
            "polling_seconds": 2,
            "hpa_scale_down_stabilization": 0,
            "cooldown_seconds": 5,
            "production_gpu_cooldown_seconds": 600,
            "scheduled_floor": 2,
            "max_replicas": 6,
            "queue_target": 1,
            "in_flight_included": True,
            "message_completion": "harness receives and deletes synthetic messages",
            "audio_inference": False,
            "aws": False,
            "policy_sha256": hashlib.sha256(
                (ROOT / "components/capacity-policy/policy.py").read_bytes()
            ).hexdigest(),
            "measurement_sha256": hashlib.sha256(
                pathlib.Path(__file__).read_bytes()
            ).hexdigest(),
        }
        (output / "environment.json").write_text(
            json.dumps(environment, indent=2) + "\n"
        )

        def finish_messages(expected):
            received = []
            deadline = time.monotonic() + 5
            while len(received) < expected and time.monotonic() < deadline:
                received.extend(
                    sqs.receive_message(
                        QueueUrl=url, MaxNumberOfMessages=10, VisibilityTimeout=60
                    ).get("Messages", [])
                )
            if len(received) != expected:
                raise AssertionError(
                    ("unexpected queue count", len(received), expected)
                )
            response = sqs.delete_message_batch(
                QueueUrl=url,
                Entries=[
                    {"Id": str(i), "ReceiptHandle": x["ReceiptHandle"]}
                    for i, x in enumerate(received)
                ],
            )
            if response.get("Failed"):
                raise AssertionError(response["Failed"])

        exact = lambda n: (
            lambda s: (
                s["live"] == n
                and s["ready"] == n
                and s["base_ready"] == min(n, 2)
                and s["burst_ready"] == max(0, n - 2)
            )
        )
        for trial in range(1, trials + 1):
            run.trial = trial
            run.case(
                "sqs_6_messages_scale_2_to_6",
                name,
                lambda: sqs.send_message_batch(
                    QueueUrl=url,
                    Entries=[
                        {
                            "Id": str(i),
                            "MessageBody": json.dumps({"trial": trial, "synthetic": i}),
                        }
                        for i in range(6)
                    ],
                ),
                lambda s: exact(6)(s) and s["spot_ready"] == 4,
            )
            run.wait(lambda: run.settled(name, 6))
            run.case(
                "queue_completed_returns_to_scheduled_2",
                name,
                lambda: finish_messages(6),
                exact(2),
            )
            run.wait(lambda: run.settled(name, 2))
            run.collect_logs()
        so["spec"]["triggers"] = so["spec"]["triggers"][:1]
        lab.apply([so])
        run.wait(
            lambda: (
                run.snapshot(name)["live"] == 0
                and run.snapshot(name)["terminating"] == 0
            ),
            120,
        )
        for trial in range(1, off_hours + 1):
            run.trial = trial
            run.case(
                "off_hours_message_wakes_1",
                name,
                lambda: sqs.send_message(QueueUrl=url, MessageBody="{}"),
                exact(1),
                base_floor=0,
            )
            run.case(
                "off_hours_empty_queue_returns_to_0",
                name,
                lambda: finish_messages(1),
                lambda s: s["live"] == 0,
                base_floor=0,
            )
            run.wait(lambda: not run.pods(name))
        run.collect_logs()
        run.write_summary(complete=True)
        summary_path = output / "summary.json"
        summary = json.loads(summary_path.read_text())
        summary["latency_start"] = (
            "immediately before local SQS SDK send/receive-delete calls; includes client/API round-trip"
        )
        summary["requested_trials_per_scenario"] = {
            "sqs_6_messages_scale_2_to_6": trials,
            "queue_completed_returns_to_scheduled_2": trials,
            "off_hours_message_wakes_1": off_hours,
            "off_hours_empty_queue_returns_to_0": off_hours,
        }
        summary["off_hours_deficit_field"] = (
            "Common raw field uses base target 2; not an outage metric when the scheduled floor is 0."
        )
        summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    finally:
        forward.terminate()
        forward.wait(timeout=10)
        run.raw.close()
        run.trace.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--trials", type=int, default=20)
    p.add_argument("--off-hours", type=int, default=10)
    p.add_argument("--output", type=pathlib.Path)
    args = p.parse_args()
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.output or ROOT / "tests/capacity-policy/results/keda-repeated" / stamp
    print("OUTPUT=" + str(output), flush=True)
    main(args.trials, args.off_hours, output)
