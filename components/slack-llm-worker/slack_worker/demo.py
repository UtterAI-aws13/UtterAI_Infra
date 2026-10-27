"""Loopback HTTP -> signed ingress -> simulated queue -> durable worker -> fake Slack."""
import argparse
from dataclasses import asdict
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
from pathlib import Path
import platform
import statistics
import threading
import time
import urllib.request

from .ingress import Ingress, Policy
from .local import Clock, FakeSlack, LocalQueue
from .pipeline import FakeModel, Pipeline
from .store import SQLiteStore
from .worker import Worker


def run(output=None):
    now = int(time.time())
    clock = Clock(now + 1)
    q = LocalQueue(clock)
    policy = Policy("A1", "T1", frozenset({"C1"}), frozenset({"U1"}), "B1")
    ingress = Ingress("offline-only", q, policy, clock)
    model, slack, store = FakeModel(), FakeSlack(), SQLiteStore()
    worker = Worker(store, Pipeline(model), slack, policy, clock, sleep=clock.advance)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            result = ingress.handle({"body": raw.decode(), "headers": dict(self.headers)})
            self.send_response(result["statusCode"]); self.end_headers()
            self.wfile.write(result["body"].encode())
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    latencies = []
    try:
        for i in range(100):
            raw = json.dumps({"type": "event_callback", "team_id": "T1", "api_app_id": "A1", "event_id": f"Ev{i}",
                              "event": {"type": "app_mention", "channel": "C1", "user": "U1",
                                        "text": "<@B1> 질문", "ts": f"{now}.{i:06d}"}}).encode()
            sig = "v0=" + hmac.new(b"offline-only", f"v0:{now}:".encode() + raw, hashlib.sha256).hexdigest()
            for delivery in range(2):
                req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/slack/events", data=raw,
                      headers={"Content-Type": "application/json", "X-Slack-Request-Timestamp": str(now),
                               "X-Slack-Signature": sig, "X-Slack-Retry-Num": str(delivery)})
                start = time.perf_counter()
                with urllib.request.urlopen(req, timeout=3) as response:
                    assert response.status == 200
                latencies.append((time.perf_counter() - start) * 1000)
        queued = len(q.messages)
        while q.messages:
            messages = q.receive()
            if not messages:
                clock.advance(2); continue
            raw = messages[0]
            result, delay = worker.handle(json.loads(raw["Body"]))
            if result == "ack": q.delete(raw["ReceiptHandle"])
            else: q.visibility(raw["ReceiptHandle"], delay)
        result = {"environment": platform.python_version(), "mode": "loopback HTTP + SQLite CAS + simulated SQS + fake model/Slack",
                  "unique_events": 100, "http_deliveries": 200, "enqueued": queued,
                  "model_calls": model.calls, "slack_posts": len(slack.messages), "dlq": len(q.dlq),
                  "ack_http_ms": {"p50": round(statistics.median(latencies), 3),
                                  "p95": round(sorted(latencies)[189], 3), "max": round(max(latencies), 3)},
                  "simulated_processing_elapsed_seconds": clock() - now - 1,
                  "limitations": ["No external service called", "No real LLM output evaluated",
                                  "No AWS cold start, SQS throughput or live Slack latency measured",
                                  "Ingress load is sequential HTTP requests before worker drain, not 100 simultaneous clients"]}
        assert model.calls == len(slack.messages) == 100 and not q.dlq
        if output:
            Path(output).write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        return result
    finally:
        server.shutdown(); server.server_close(); thread.join()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--output")
    print(json.dumps(run(parser.parse_args().output), indent=2, ensure_ascii=False))
