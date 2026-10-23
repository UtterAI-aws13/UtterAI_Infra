"""Paired before/after heartbeat crash measurements against localhost ElasticMQ.

The baseline function is extracted, unchanged, from the pinned pre-change commit.
Each sample runs the real heartbeat function in a child process and SIGKILLs it
immediately after the first successful visibility extension. No accelerated time.
"""

import argparse
import ast
import concurrent.futures
import datetime
import hashlib
import importlib.util
import json
import logging
import math
import pathlib
import selectors
import statistics
import subprocess
import sys
import threading
import time

import boto3
from botocore.config import Config

ROOT = pathlib.Path(__file__).resolve().parents[2]
AI = ROOT.parent / "UtterAI_AI"
ENDPOINT = "http://127.0.0.1:19327"
BASELINE_REF = "35802e8b0dc479f868f21d5770cc4ac4bbf42abb"


def client():
    return boto3.client(
        "sqs",
        region_name="us-east-1",
        endpoint_url=ENDPOINT,
        aws_access_key_id="local-only",
        aws_secret_access_key="local-only",
        config=Config(
            connect_timeout=1,
            read_timeout=10,
            retries={"max_attempts": 0},
            max_pool_connections=100,
        ),
    )


def child(profile, source):
    data = json.loads(sys.stdin.readline())
    sqs = client()

    class Observed:
        def change_message_visibility(self, **kwargs):
            result = sqs.change_message_visibility(**kwargs)
            print(
                json.dumps(
                    {
                        "extended_ns": time.monotonic_ns(),
                        "visibility": kwargs["VisibilityTimeout"],
                    }
                ),
                flush=True,
            )
            return result

    if profile == "before":
        tree = ast.parse(source.read_text())
        fn = next(
            x
            for x in tree.body
            if isinstance(x, ast.FunctionDef) and x.name == "_extend_visibility"
        )
        namespace = {"threading": threading, "logger": logging.getLogger("baseline")}
        exec(
            compile(ast.Module(body=[fn], type_ignores=[]), str(source), "exec"),
            namespace,
        )
        heartbeat = namespace["_extend_visibility"]
    else:
        spec = importlib.util.spec_from_file_location("lifecycle", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        heartbeat = module.heartbeat
    heartbeat(Observed(), data["url"], data["receipt"], threading.Event())


def distribution(values):
    values = sorted(values)
    if not values:
        return None
    return {
        "n": len(values),
        "min": values[0],
        "p50": statistics.median(values),
        "p95": values[math.ceil(0.95 * len(values)) - 1],
        "max": values[-1],
    }


def main(trials, output, baseline_ref=BASELINE_REF):
    output.mkdir(parents=True, exist_ok=False)
    baseline_head = subprocess.check_output(
        ["git", "-C", str(AI), "rev-parse", "--verify", baseline_ref + "^{commit}"],
        text=True,
    ).strip()
    baseline = subprocess.check_output(
        ["git", "-C", str(AI), "show", baseline_head + ":app/workers/ml_gpu_worker.py"],
        text=True,
    )
    baseline_path = output / "baseline-ml_gpu_worker.py"
    baseline_path.write_text(baseline)
    changed = AI / "app/workers/lifecycle.py"
    metadata = {
        "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "baseline_git_head": baseline_head,
        "baseline_function": "_extend_visibility",
        "baseline_source_sha256": hashlib.sha256(baseline.encode()).hexdigest(),
        "changed_source_sha256": hashlib.sha256(changed.read_bytes()).hexdigest(),
        "measurement_sha256": hashlib.sha256(
            pathlib.Path(__file__).read_bytes()
        ).hexdigest(),
        "trials_per_profile": trials,
        "fault": "SIGKILL immediately after successful first visibility extension",
        "intervals_seconds": {"before": 240, "after": 30},
        "visibility_seconds": {"before": 1800, "after": 120},
        "time_acceleration": False,
        "message_workload": "one synthetic message per isolated queue; no model/inference",
        "parallelism": "all paired samples overlap in one server run; not independent server-day repetitions",
        "aws": False,
        "endpoint": ENDPOINT,
    }
    (output / "environment.json").write_text(json.dumps(metadata, indent=2) + "\n")
    sqs = client()
    lock = threading.Lock()
    started_file = (output / "started.jsonl").open("w")
    rows_file = (output / "trials.jsonl").open("w")
    prefix = "ab-" + output.name.lower()

    def sample(trial, profile):
        name = f"{prefix}-{trial:02d}-{profile}"
        url = ENDPOINT + "/000000000000/" + name
        sqs.create_queue(QueueName=name)
        sent = sqs.send_message(
            QueueUrl=url, MessageBody=json.dumps({"trial": trial, "profile": profile})
        )
        received = sqs.receive_message(QueueUrl=url, VisibilityTimeout=120)["Messages"][
            0
        ]
        assert received["MessageId"] == sent["MessageId"]
        source = baseline_path if profile == "before" else changed
        process = subprocess.Popen(
            [sys.executable, __file__, "--child", profile, "--source", str(source)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        row = {
            "trial": trial,
            "profile": profile,
            "message_id": sent["MessageId"],
            "passed": False,
        }
        try:
            process.stdin.write(
                json.dumps({"url": url, "receipt": received["ReceiptHandle"]}) + "\n"
            )
            process.stdin.flush()
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            if not selector.select(30):
                raise TimeoutError("heartbeat did not acknowledge extension")
            event = json.loads(process.stdout.readline())
            selector.close()
            process.kill()
            killed = time.monotonic_ns()
            process.wait(timeout=5)
            assert process.returncode == -9
            visibility = event["visibility"]
            assert visibility == (1800 if profile == "before" else 120)
            row.update(
                visibility_seconds=visibility,
                extension_ns=event["extended_ns"],
                killed_ns=killed,
                extension_to_kill_seconds=(killed - event["extended_ns"]) / 1e9,
                child_exit=process.returncode,
            )
            with lock:
                started_file.write(json.dumps(row) + "\n")
                started_file.flush()
            deadline = time.monotonic() + visibility + 30
            while time.monotonic() < deadline:
                messages = sqs.receive_message(
                    QueueUrl=url, WaitTimeSeconds=5, VisibilityTimeout=60
                ).get("Messages", [])
                if not messages:
                    continue
                observed = time.monotonic_ns()
                assert (
                    len(messages) == 1 and messages[0]["MessageId"] == sent["MessageId"]
                )
                elapsed = (observed - killed) / 1e9
                assert visibility - 2 <= elapsed <= visibility + 15, (
                    elapsed,
                    visibility,
                )
                row.update(
                    passed=True,
                    redelivery_after_kill_seconds=round(elapsed, 6),
                    observed_ns=observed,
                )
                sqs.delete_message(
                    QueueUrl=url, ReceiptHandle=messages[0]["ReceiptHandle"]
                )
                break
            if not row["passed"]:
                raise TimeoutError("message did not reappear")
        except Exception as exc:
            row["error"] = type(exc).__name__ + ": " + str(exc)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()
            with lock:
                rows_file.write(json.dumps(row) + "\n")
                rows_file.flush()
                print(json.dumps(row), flush=True)
        return row

    rows = []
    started = time.monotonic()

    def write_summary(complete=False):
        groups = {}
        for profile in ["before", "after"]:
            group = [r for r in rows if r["profile"] == profile]
            groups[profile] = {
                "attempted": len(group),
                "passed": sum(r["passed"] for r in group),
                "failed": sum(not r["passed"] for r in group),
                "redelivery_seconds": distribution(
                    [r["redelivery_after_kill_seconds"] for r in group if r["passed"]]
                ),
            }
        data = {
            "scope": "actual original and changed heartbeat functions; SIGKILL; localhost ElasticMQ; no AWS/ML",
            "completed": complete,
            "requested_trials_per_profile": trials,
            "profiles": groups,
            "wall_seconds": round(time.monotonic() - started, 3),
        }
        if all(groups[p]["redelivery_seconds"] for p in groups):
            a, b = [groups[p]["redelivery_seconds"]["p95"] for p in ["before", "after"]]
            data["p95_reduction_percent"] = (a - b) / a * 100
        (output / "summary.json").write_text(json.dumps(data, indent=2) + "\n")

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=trials * 2) as pool:
            pending = {
                pool.submit(sample, trial, profile)
                for trial in range(1, trials + 1)
                for profile in ["before", "after"]
            }
            while pending:
                done, pending = concurrent.futures.wait(
                    pending, timeout=10, return_when=concurrent.futures.FIRST_COMPLETED
                )
                rows.extend(f.result() for f in done)
                write_summary()
            write_summary(complete=True)
    finally:
        rows_file.close()
        started_file.close()
    if len(rows) != trials * 2 or not all(r["passed"] for r in rows):
        raise RuntimeError("before/after samples include failures; inspect raw results")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=20)
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--baseline-ref", default=BASELINE_REF)
    parser.add_argument("--child", choices=["before", "after"])
    parser.add_argument("--source", type=pathlib.Path)
    args = parser.parse_args()
    if args.child:
        child(args.child, args.source)
    else:
        if not 1 <= args.trials <= 20:
            parser.error("trials must be between 1 and 20")
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        main(
            args.trials,
            args.output or ROOT / "tests/capacity-policy/results/ab-redelivery" / stamp,
            args.baseline_ref,
        )
