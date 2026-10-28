"""End-to-end run against LocalStack: signed HTTP → Lambda Function URL → SQS → worker
process → DynamoDB state → Ollama → loopback mock Slack.

Prerequisites: LocalStack running, `terraform -chdir=infra/terraform/envs/local apply` done,
Ollama serving MODEL_NAME. Results are local emulation, not AWS measurements.
"""
import argparse
import hashlib
import hmac
import json
import os
import platform
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).parent))
from mock_slack import MockSlack  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TF = ROOT / "infra/terraform/envs/local"
ENDPOINT = "http://localhost:4566"
AWS_ENV = {"AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
           "AWS_DEFAULT_REGION": "ap-northeast-2", "AWS_ENDPOINT_URL": ENDPOINT}


def percentile(values, q):
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, round(q * len(ordered) + 0.5) - 1))]


def wait_for(predicate, timeout, interval=0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class Harness:
    def __init__(self, model):
        os.environ.update(AWS_ENV)
        out = json.loads(subprocess.check_output(["terraform", f"-chdir={TF}", "output", "-json"]))
        self.o = {k: v["value"] for k, v in out.items()}
        self.sqs, self.dynamo = boto3.client("sqs"), boto3.client("dynamodb")
        self.model, self.run = model, uuid.uuid4().hex[:8]
        self.seq, self.lock = 0, threading.Lock()
        self.slack = MockSlack("xoxb-local-test-only").start()
        self.worker, self.log_lines = None, []

    # ── worker process ──────────────────────────────────────────────────────
    def start_worker(self, **extra):
        s = self.o["slack"]
        env = {**os.environ, **AWS_ENV, "PYTHONUNBUFFERED": "1",
               "QUEUE_URL": self.o["queue_url"], "STATE_TABLE": self.o["state_table"],
               "SLACK_BOT_SECRET_ARN": self.o["bot_secret_arn"], "SLACK_API_BASE": self.slack.api_base,
               "SLACK_APP_ID": s["app_id"], "SLACK_TEAM_ID": s["team_id"],
               "SLACK_CHANNELS": ",".join(s["channels"]), "SLACK_USERS": ",".join(s["users"]),
               "SLACK_BOT_ID": s["bot_id"], "MODEL_PROVIDER": "openai_compat",
               "MODEL_BASE_URL": "http://localhost:11434/v1", "MODEL_NAME": self.model,
               "WORKER_CONCURRENCY": "4", **extra}
        self.worker = subprocess.Popen([sys.executable, "-m", "slack_worker.runtime"], cwd=ROOT, env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        threading.Thread(target=self._read_logs, args=(self.worker,), daemon=True).start()

    def _read_logs(self, proc):
        for line in proc.stdout:
            try:
                self.log_lines.append(json.loads(line))
            except ValueError:
                self.log_lines.append({"event": "raw", "line": line.rstrip()[:200]})

    def stop_worker(self):
        self.worker.terminate()
        code = self.worker.wait(timeout=60)
        time.sleep(0.5)
        return code

    def model_calls(self):
        return sum(1 for x in self.log_lines if x.get("event") == "model_call")

    # ── Slack-side traffic ──────────────────────────────────────────────────
    def new_event(self):
        with self.lock:
            self.seq += 1
            seq = self.seq
        channel = self.o["slack"]["channels"][seq % len(self.o["slack"]["channels"])]
        return {"event_id": f"Ev{self.run}{seq:05d}", "channel": channel,
                "ts": f"{int(time.time())}.{seq:06d}", "text": f"<@B_LOCAL> 한 문장으로 답해줘: {seq}의 다음 숫자는?"}

    def send(self, ev, retry=None):
        now = int(time.time())
        body = json.dumps({"type": "event_callback", "api_app_id": "A_LOCAL", "team_id": "T_LOCAL",
                           "event_id": ev["event_id"],
                           "event": {"type": "app_mention", "channel": ev["channel"], "user": "U_LOCAL",
                                     "text": ev["text"], "ts": ev["ts"]}}, ensure_ascii=False)
        sig = "v0=" + hmac.new(self.o["signing_secret"].encode(), f"v0:{now}:{body}".encode(),
                               hashlib.sha256).hexdigest()
        headers = {"Content-Type": "application/json", "X-Slack-Request-Timestamp": str(now),
                   "X-Slack-Signature": sig}
        if retry:
            headers.update({"X-Slack-Retry-Num": str(retry), "X-Slack-Retry-Reason": "http_timeout"})
        req = urllib.request.Request(self.o["function_url"], data=body.encode(), headers=headers, method="POST")
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        return code, time.monotonic() - started

    def state(self, ev):
        item = self.dynamo.get_item(TableName=self.o["state_table"], ConsistentRead=True,
                                    Key={"pk": {"S": f"event:T_LOCAL:{ev['event_id']}"}}).get("Item")
        return json.loads(item["body"]["S"]) if item else None

    def queue_depth(self, url):
        a = self.sqs.get_queue_attributes(QueueUrl=url, AttributeNames=[
            "ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"])["Attributes"]
        return int(a["ApproximateNumberOfMessages"]) + int(a["ApproximateNumberOfMessagesNotVisible"])


def ack_summary(acks):
    seconds = [s for _, s in acks]
    return {"p50": round(statistics.median(seconds), 3), "p95": round(percentile(seconds, .95), 3),
            "max": round(max(seconds), 3)}


def burst(h, n, concurrency):
    # First wave pays the emulator's per-container Lambda start; report it separately.
    warm = [h.new_event() for _ in range(concurrency)]
    with ThreadPoolExecutor(concurrency) as pool:
        cold_acks = list(pool.map(h.send, warm))
    wait_for(lambda: all(h.slack.posts_for(e["ts"]) for e in warm), timeout=300)
    events = [h.new_event() for _ in range(n)]
    calls0 = h.model_calls()
    with ThreadPoolExecutor(concurrency) as pool:
        acks = list(pool.map(h.send, events))
    done = wait_for(lambda: all(h.slack.posts_for(e["ts"]) for e in events), timeout=600)
    return {"events": n, "client_concurrency": concurrency,
            "http_status": {str(c): sum(1 for x, _ in acks if x == c) for c in {x for x, _ in acks}},
            "first_wave_ack_seconds": ack_summary(cold_acks),
            "ack_seconds": ack_summary(acks),
            "all_answered": done,
            "slack_posts": sum(len(h.slack.posts_for(e["ts"])) for e in events),
            "max_posts_per_event": max(len(h.slack.posts_for(e["ts"])) for e in events),
            "model_calls": h.model_calls() - calls0,
            "sent_state": sum(1 for e in events if (h.state(e) or {}).get("phase") == "SENT"),
            "progress_reactions": len({c["body"]["timestamp"] for c in h.slack.calls_for("reactions.add")
                                       if c["body"]["timestamp"] in {e["ts"] for e in events}})}


def duplicates(h, n):
    events = [h.new_event() for _ in range(n)]
    calls0 = h.model_calls()
    deliveries = [(e, r) for e in events for r in (None, 1, 2)]
    with ThreadPoolExecutor(10) as pool:
        codes = list(pool.map(lambda d: h.send(d[0], d[1])[0], deliveries))
    wait_for(lambda: all(h.slack.posts_for(e["ts"]) for e in events), timeout=300)
    # Let every redelivered copy be consumed before counting.
    wait_for(lambda: h.queue_depth(h.o["queue_url"]) == 0, timeout=300)
    return {"unique_events": n, "http_deliveries": len(deliveries),
            "accepted": sum(1 for c in codes if c == 200),
            "slack_posts": sum(len(h.slack.posts_for(e["ts"])) for e in events),
            "max_posts_per_event": max(len(h.slack.posts_for(e["ts"])) for e in events),
            "model_calls": h.model_calls() - calls0}


def rate_limited(h):
    ev, calls0 = h.new_event(), h.model_calls()
    h.slack.fault(ev["ts"], "429:2")
    h.send(ev)
    wait_for(lambda: (h.state(ev) or {}).get("phase") == "SENT", timeout=120)
    return {"rejected_with_429": h.slack.rejected.count(ev["ts"]), "slack_posts": len(h.slack.posts_for(ev["ts"])),
            "model_calls": h.model_calls() - calls0, "final_phase": (h.state(ev) or {}).get("phase")}


def accepted_then_timeout(h, observe):
    ev, calls0 = h.new_event(), h.model_calls()
    h.slack.fault(ev["ts"], "timeout")
    h.send(ev)
    wait_for(lambda: (h.state(ev) or {}).get("phase") == "AMBIGUOUS", timeout=120)
    phase = (h.state(ev) or {}).get("phase")
    # Visibility is 90s after quarantine; observe one redelivery and confirm no automatic re-post.
    time.sleep(observe)
    return {"phase_after_timeout": phase, "observed_seconds": observe,
            "slack_posts": len(h.slack.posts_for(ev["ts"])), "model_calls": h.model_calls() - calls0,
            "final_phase": (h.state(ev) or {}).get("phase")}


def malformed_to_dlq(h, count):
    before = h.queue_depth(h.o["dlq_url"])
    # Unparseable bodies are never deleted and redrive after maxReceiveCount. A parseable but
    # schema-invalid job takes the worker's 90s retry path instead (≈12 min to DLQ), by design.
    bodies = ["not-json", "[]", "{broken"][:count]
    for b in bodies:
        h.sqs.send_message(QueueUrl=h.o["queue_url"], MessageBody=b)
    started = time.monotonic()
    ok = wait_for(lambda: h.queue_depth(h.o["dlq_url"]) - before >= count, timeout=180, interval=1)
    return {"malformed_messages": count, "moved_to_dlq": h.queue_depth(h.o["dlq_url"]) - before,
            "within_seconds": round(time.monotonic() - started, 1), "ok": ok, "max_receive_count": 8,
            "receive_visibility_seconds": 2}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="llama3.2:1b")
    ap.add_argument("--burst", type=int, default=100)
    ap.add_argument("--duplicates", type=int, default=20)
    ap.add_argument("--observe", type=int, default=100)
    ap.add_argument("--output", default=str(ROOT / "evidence/localstack-e2e.json"))
    args = ap.parse_args()

    h = Harness(args.model)
    for url in (h.o["queue_url"], h.o["dlq_url"]):
        h.sqs.purge_queue(QueueUrl=url)
    h.start_worker()
    time.sleep(3)
    results = {}
    try:
        results["burst"] = burst(h, args.burst, 10)
        results["duplicate_delivery"] = duplicates(h, args.duplicates)
        results["slack_429"] = rate_limited(h)
        results["slack_accepted_then_timeout"] = accepted_then_timeout(h, args.observe)
        results["worker_sigterm_exit_code"] = h.stop_worker()
        # Short receive visibility only for this case so 8 receives fit in the run.
        h.start_worker(RECEIVE_VISIBILITY="2")
        results["malformed_to_dlq"] = malformed_to_dlq(h, 3)
    finally:
        if h.worker and h.worker.poll() is None:
            h.stop_worker()
        h.slack.stop()

    health = json.loads(urllib.request.urlopen(ENDPOINT + "/_localstack/health").read())
    report = {"scope": "LocalStack AWS emulator + Ollama local model + loopback mock Slack. "
                       "Not AWS: no real Lambda cold start, IAM enforcement, Slack retries or Bedrock.",
              "date": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "environment": {"localstack": health.get("version"), "model": args.model,
                              "python": platform.python_version(), "machine": platform.machine(),
                              "worker_concurrency": 4},
              "results": results}
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
