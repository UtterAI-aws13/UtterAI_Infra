"""Gateway policy checks against the local LiteLLM stack (gateway/compose.yaml, profile scale)
plus a worker run through the gateway on LocalStack.

Prices are TEST prices configured in gateway/litellm.yaml; budget numbers are mock spend.
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from run_localstack import Harness, wait_for  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ["docker", "compose", "-f", str(ROOT / "gateway/compose.yaml"), "--profile", "scale"]
A, B = "http://127.0.0.1:4000", "http://127.0.0.1:4001"
MASTER = "sk-local-master-test-only"
KEYS = json.loads((ROOT / "gateway/.keys.local.json").read_text())


def post(base, path, payload, key, timeout=60):
    req = urllib.request.Request(base + path, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read(2000).decode("utf-8", "replace")
    except urllib.error.URLError as e:
        return "unreachable", str(e.reason)


def get(base, path, key=MASTER):
    req = urllib.request.Request(base + path, headers={"Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def chat(base, key, model, user="", max_tokens=8):
    payload = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": "ok?"}]}
    if user:
        payload["user"] = user
    return post(base, "/v1/chat/completions", payload, key)


def temp_key(**spec):
    return post(A, "/key/generate", {"metadata": {"service": "test"}, **spec}, MASTER)[1]["key"]


def compose(*args):
    subprocess.run(COMPOSE + list(args), check=True, capture_output=True)


def access_control():
    cases = {"slack-bot→fast": ("slack-bot", "fast"), "slack-bot→quality": ("slack-bot", "quality"),
             "batch→quality": ("batch", "quality"), "batch→fast": ("batch", "fast")}
    return {name: chat(A, KEYS[svc], model)[0] for name, (svc, model) in cases.items()}


def shared_limit(rpm, requests):
    """Same key, requests alternated across two proxy instances. A per-instance counter would
    admit ~2×rpm; a shared (Redis) counter admits ~rpm."""
    key = temp_key(models=["fast"], rpm_limit=rpm)
    codes = [chat(A if i % 2 == 0 else B, key, "fast", max_tokens=1)[0] for i in range(requests)]
    return {"rpm_limit": rpm, "requests": requests, "instances": 2, "status": dict(Counter(map(str, codes)))}


def redis_outage(rpm, requests):
    key = temp_key(models=["fast"], rpm_limit=rpm)
    compose("stop", "redis")
    try:
        started = time.monotonic()
        codes = [chat(A if i % 2 == 0 else B, key, "fast", max_tokens=1)[0] for i in range(requests)]
        elapsed = time.monotonic() - started
    finally:
        compose("start", "redis")
    return {"rpm_limit": rpm, "requests": requests, "status": dict(Counter(map(str, codes))),
            "seconds": round(elapsed, 1)}


def budget(max_budget, limit):
    key = temp_key(models=["fast"], max_budget=max_budget)
    codes, block = [], None
    for _ in range(limit):
        code, body = chat(A, key, "fast", max_tokens=64)
        codes.append(code)
        if code != 200:
            try:
                err = json.loads(body).get("error", {})
                block = {"type": err.get("type"), "code": err.get("code"),
                         "mentions_budget": "budget" in str(err.get("message", "")).lower()}
            except (ValueError, AttributeError):
                block = {"raw_mentions_budget": "budget" in str(body).lower()}
            break
        time.sleep(0.3)  # spend is recorded asynchronously
    info = get(A, "/key/info?key=" + key)["info"]
    return {"max_budget_test_units": max_budget, "accepted_before_block": codes.count(200),
            "block_status": codes[-1] if codes and codes[-1] != 200 else None, "block_error": block,
            "recorded_spend_test_units": round(info.get("spend") or 0, 6)}


def worker_through_gateway(h, events, outage_seconds, probe=True):
    """Gateway stopped while jobs arrive; jobs must wait without spending model attempts."""
    h.start_worker(MODEL_BASE_URL=A + "/v1", MODEL_NAME="fast", MODEL_API_KEY=KEYS["slack-bot"],
                   MODEL_HEALTH_URL=A + "/health/readiness" if probe else "")
    time.sleep(3)
    compose("stop", "litellm-a")
    evs = [h.new_event() for _ in range(events)]
    try:
        for e in evs:
            h.send(e)
        time.sleep(outage_seconds)
    finally:
        compose("start", "litellm-a")
    wait_for(lambda: subprocess.run(["curl", "-sf", A + "/health/liveliness"], capture_output=True).returncode == 0,
             timeout=180, interval=2)
    answered = wait_for(lambda: all(h.slack.posts_for(e["ts"]) for e in evs), timeout=400, interval=2)
    states = [h.state(e) or {} for e in evs]
    keys = {f"event:T_LOCAL:{e['event_id']}" for e in evs}
    calls = [x for x in h.log_lines if x.get("event") == "model_call" and x.get("job") in keys]
    per_job = {}
    for x in calls:
        per_job.setdefault(x["job"], []).append(x["outcome"] if not x.get("error") else f"{x['outcome']}({x['error']})")
    attribution = {}
    for e in evs[:3]:
        job = f"event:T_LOCAL:{e['event_id']}"
        worker_tokens = sum((x.get("usage") or {}).get("inputTokens", 0) + (x.get("usage") or {}).get("outputTokens", 0)
                            for x in calls if x["job"] == job)
        # The gateway batches spend writes, so the end-user row appears with a delay.
        gateway_spend = None
        deadline = time.monotonic() + 90
        while gateway_spend is None and time.monotonic() < deadline:
            try:
                gateway_spend = get(A, "/customer/info?end_user_id=" + urllib.parse.quote(job)).get("spend")
            except urllib.error.HTTPError:
                time.sleep(5)
        attribution[job] = {"worker_logged_tokens": worker_tokens, "gateway_end_user_spend_test_units": gateway_spend}
    h.stop_worker()
    return {"events": events, "gateway_outage_seconds": outage_seconds, "all_answered": answered,
            "slack_posts": sum(len(h.slack.posts_for(e["ts"])) for e in evs),
            "model_call_outcomes": dict(Counter(x["outcome"] for x in calls)),
            "error_types": dict(Counter(x["error"] for x in calls if x.get("error"))),
            "per_job_call_sequence": per_job,
            "final_attempts": dict(Counter(s.get("attempts") for s in states)),
            "jobs_that_waited_out_outage": sum(1 for s in states if s.get("unavailable")),
            "per_job_attribution": attribution}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default=str(ROOT / "evidence/gateway-checks.json"))
    ap.add_argument("--outage", type=int, default=45)
    args = ap.parse_args()
    results = {"access_control_http_status": access_control(),
               "shared_rate_limit": shared_limit(rpm=10, requests=30),
               "redis_outage": redis_outage(rpm=10, requests=20),
               "budget": budget(max_budget=0.0005, limit=60)}
    h = Harness("fast")
    try:
        # Same outage twice: without, then with the readiness probe after a connection failure.
        results["worker_through_gateway_no_probe"] = worker_through_gateway(h, 5, args.outage, probe=False)
        results["worker_through_gateway_with_probe"] = worker_through_gateway(h, 5, args.outage, probe=True)
    finally:
        if h.worker and h.worker.poll() is None:
            h.stop_worker()
        h.slack.stop()
    report = {"scope": "Local LiteLLM v1.104.2 (2 instances, PostgreSQL, Redis) over Ollama; LocalStack worker run. "
                       "Budget figures use test prices, not Bedrock prices.",
              "date": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "results": results}
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
