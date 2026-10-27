"""ChatOps end-to-end: Alertmanager webhook / signed slash commands / signed button clicks
→ LocalStack Lambda Function URL → SQS → worker → kind Kubernetes API (RBAC-scoped token)
and loopback mock Slack. The alert advisor calls a local model (Ollama or the LiteLLM gateway).

Prerequisites: scripts/local_up.sh (LocalStack + Terraform), scripts/kind_up.sh.
Results are local emulation, not AWS or real Slack measurements.
"""
import argparse
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from run_localstack import ROOT, TF, Harness, wait_for  # noqa: E402

KUBE = ROOT / "e2e/.kube.local"
KUBECTL = ["kubectl", "--kubeconfig", str(KUBE / "kubeconfig"), "-n", "utterai"]
TARGETS = {"api": {"namespace": "utterai", "deployment": "api"},
           "db": {"namespace": "utterai", "deployment": "db"},          # allowlisted, but RBAC denies patch
           "stt-worker": {"namespace": "utterai", "deployment": "stt-worker"}}
OPS_CH, ALERT_CH = "C_LOCAL1", "C_ALERT"


def deploy(name):
    d = json.loads(subprocess.check_output(KUBECTL + ["get", "deploy", name, "-o", "json"]))
    return {"generation": d["metadata"]["generation"],
            "restarted_at": d["spec"]["template"]["metadata"].get("annotations", {}).get(
                "kubectl.kubernetes.io/restartedAt")}


def pod_names(app):
    out = subprocess.check_output(KUBECTL + ["get", "pods", "-l", f"app={app}", "-o", "name"], text=True)
    return sorted(out.split())


class Ops(Harness):
    def __init__(self, model, base_url, api_key, health, advisor_key=""):
        super().__init__(model)
        self.advisor_key = advisor_key
        out = json.loads(subprocess.check_output(["terraform", f"-chdir={TF}", "output", "-json"]))
        self.alert_token = out["alert_token"]["value"]
        self.base_url, self.api_key, self.health = base_url, api_key, health

    def start(self, **extra):
        env = {"MODEL_BASE_URL": self.base_url, "MODEL_API_KEY": self.api_key, "MODEL_HEALTH_URL": self.health,
               "MODEL_NAME": "fast", "ADVISOR_MODEL_NAME": self.model, "ADVISOR_API_KEY": self.advisor_key,
               "OPS_TARGETS": json.dumps(TARGETS),
               "OPS_OPERATORS": "U_OPS1,U_OPS2", "OPS_APPROVAL_TTL": "20", "SLACK_ALERT_CHANNEL": ALERT_CH,
               "OPS_RUNBOOKS": str(ROOT / "examples/runbooks"),
               "K8S_API": (KUBE / "server").read_text().strip(), "K8S_TOKEN_FILE": str(KUBE / "token"),
               "K8S_CA_FILE": str(KUBE / "ca.crt"), **extra}
        self.start_worker(**env)
        time.sleep(3)

    def signed_form(self, fields):
        body = urllib.parse.urlencode(fields)
        now = str(int(time.time()))
        sig = "v0=" + hmac.new(self.o["signing_secret"].encode(), f"v0:{now}:{body}".encode(),
                               hashlib.sha256).hexdigest()
        return self._post(body, {"Content-Type": "application/x-www-form-urlencoded",
                                 "X-Slack-Request-Timestamp": now, "X-Slack-Signature": sig})

    def _post(self, body, headers, url=None):
        req = urllib.request.Request(url or self.o["function_url"], data=body.encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def command(self, user, text):
        return self.signed_form({"api_app_id": "A_LOCAL", "team_id": "T_LOCAL", "channel_id": OPS_CH,
                                 "user_id": user, "command": "/ops", "text": text, "trigger_id": uuid.uuid4().hex,
                                 "response_url": "https://hooks.slack.com/commands/T_LOCAL/1/x"})

    def click(self, user, action_id, value, message_ts, channel=OPS_CH, thread_ts=None):
        container = {"message_ts": message_ts}
        if thread_ts:
            container["thread_ts"] = thread_ts
        payload = {"type": "block_actions", "api_app_id": "A_LOCAL", "team": {"id": "T_LOCAL"},
                   "user": {"id": user}, "channel": {"id": channel}, "trigger_id": uuid.uuid4().hex,
                   "container": container, "actions": [{"action_id": action_id, "value": value}]}
        return self.signed_form({"payload": json.dumps(payload)})

    def alert(self, body, token=None):
        url = self.o["function_url"].rstrip("/") + "/alertmanager"
        return self._post(json.dumps(body), {"Content-Type": "application/json",
                                             "Authorization": "Bearer " + (token or self.alert_token)}, url)

    # ── mock Slack lookups ──────────────────────────────────────────────────────────────
    def posts_since(self, since, channel=None):
        return [p for p in list(self.slack.posts) if p["at"] >= since and (not channel or p["channel"] == channel)]

    def wait_post(self, since, pred, timeout=60):
        found = []
        wait_for(lambda: found.extend(p for p in self.posts_since(since) if pred(p)) or found, timeout)
        return found[0] if found else None

    def wait_request(self, since, thread_ts=""):
        p = self.wait_post(since, lambda p: p["thread_ts"] == thread_ts and button(p, "ops_approve"))
        return (button(p, "ops_approve"), p["ts"]) if p else (None, None)

    def updates(self, ts):
        return self.slack.calls_for("chat.update", ts=ts)

    def ephemerals(self, since):
        return [c for c in self.slack.calls_for("chat.postEphemeral") if c["at"] >= since]


def button(post, action_id):
    for b in post.get("blocks") or []:
        for e in b.get("elements", []) if b.get("type") == "actions" else []:
            if e.get("action_id") == action_id:
                return e["value"]
    return None


def alert_body(alertname, deployment, summary, fingerprint=None, status="firing"):
    fingerprint = fingerprint or uuid.uuid4().hex[:12]
    return {"version": "4", "status": status, "groupKey": f"{{}}:{{alertname=\"{alertname}\"}}",
            "alerts": [{"status": status, "fingerprint": fingerprint, "startsAt": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "labels": {"alertname": alertname, "severity": "critical", "namespace": "utterai",
                                   "deployment": deployment},
                        "annotations": {"summary": summary}}]}


CRASH = ("PodCrashLooping", "api", "api Pod가 최근 10분 동안 5회 재시작했습니다. 최근 배포는 없습니다.")
BACKLOG = ("SQSQueueBacklog", "stt-worker", "stt 작업 큐에 메시지 1,200개가 쌓였고 가장 오래된 메시지는 9분입니다.")


# ── scenarios ───────────────────────────────────────────────────────────────────────────

def status(h):
    t0 = time.time()
    code = h.command("U_VIEWER", "status api")
    post = h.wait_post(t0, lambda p: p["channel"] == OPS_CH and "Ready" in p["text"])
    return {"http_status": code, "posted": bool(post), "seconds": round(post["at"] - t0, 2) if post else None,
            "text_head": post["text"].split("\n")[0] if post else None, "patched": False}


def approval_flow(h):
    before, pods_before = deploy("api"), pod_names("api")
    t0 = time.time()
    h.command("U_OPS1", "restart api")
    aid, ts = h.wait_request(t0)
    after_request = deploy("api")
    e0 = time.time()
    h.click("U_OPS1", "ops_approve", aid, ts)      # requester approving own request
    h.click("U_VIEWER", "ops_approve", aid, ts)    # allowed Slack user, not an operator
    wait_for(lambda: len(h.ephemerals(e0)) >= 2, 60)
    blocked = deploy("api")
    h.click("U_OPS2", "ops_approve", aid, ts)
    wait_for(lambda: any("완료" in u["body"]["text"] for u in h.updates(ts)), 60)
    after = deploy("api")
    wait_for(lambda: set(pod_names("api")).isdisjoint(pods_before), 120, 2)
    return {"generation": [before["generation"], after_request["generation"], blocked["generation"],
                           after["generation"]],
            "generation_labels": ["start", "after_request", "after_self_and_viewer_approval", "after_ops2_approval"],
            "denials_shown": [c["body"]["text"] for c in h.ephemerals(e0)][:2],
            "message_updated": [u["body"]["text"] for u in h.updates(ts)],
            "pods_replaced": set(pod_names("api")).isdisjoint(pods_before)}


def double_click(h, clicks):
    before = deploy("api")
    t0 = time.time()
    h.command("U_OPS1", "restart api")
    aid, ts = h.wait_request(t0)
    e0 = time.time()
    with ThreadPoolExecutor(clicks) as pool:
        codes = list(pool.map(lambda _: h.click("U_OPS2", "ops_approve", aid, ts), range(clicks)))
    wait_for(lambda: len(h.updates(ts)) >= 1 and len(h.ephemerals(e0)) + len(h.updates(ts)) >= clicks, 90)
    time.sleep(5)
    return {"concurrent_approvals": clicks, "http_status": dict(Counter(map(str, codes))),
            "generation_increase": deploy("api")["generation"] - before["generation"],
            "message_updates": len(h.updates(ts)), "already_handled_notices": len(h.ephemerals(e0))}


def reject_and_expire(h, ttl):
    before = deploy("api")["generation"]
    t0 = time.time()
    h.command("U_OPS1", "restart api")
    aid, ts = h.wait_request(t0)
    h.click("U_OPS2", "ops_reject", aid, ts)
    wait_for(lambda: h.updates(ts), 60)
    h.click("U_OPS2", "ops_approve", aid, ts)
    t1 = time.time()
    h.command("U_OPS1", "restart api")
    aid2, ts2 = h.wait_request(t1)
    time.sleep(ttl + 2)
    h.click("U_OPS2", "ops_approve", aid2, ts2)
    wait_for(lambda: h.updates(ts2), 60)
    time.sleep(3)
    return {"rejected_update": [u["body"]["text"] for u in h.updates(ts)],
            "expired_update": [u["body"]["text"] for u in h.updates(ts2)],
            "approval_ttl_seconds": ttl, "generation_increase": deploy("api")["generation"] - before}


def rbac_denied(h):
    before = deploy("db")["generation"]
    t0 = time.time()
    h.command("U_OPS1", "restart db")
    aid, ts = h.wait_request(t0)
    h.click("U_OPS2", "ops_approve", aid, ts)
    wait_for(lambda: h.updates(ts), 60)
    return {"app_allowlist_allowed": True, "update": [u["body"]["text"] for u in h.updates(ts)],
            "db_generation_increase": deploy("db")["generation"] - before}


def crash_after_patch(h):
    h.stop_worker()
    h.start(OPS_FAULT="exit_after_patch", RECEIVE_VISIBILITY="30")
    before = deploy("api")
    t0 = time.time()
    h.command("U_OPS1", "restart api")
    aid, ts = h.wait_request(t0)
    h.click("U_OPS2", "ops_approve", aid, ts)
    exited = wait_for(lambda: h.worker.poll() is not None, 60)
    exit_code = h.worker.poll()
    patched = deploy("api")
    h.start()                              # replacement worker, no fault
    recovered = wait_for(lambda: any("완료" in u["body"]["text"] for u in h.updates(ts)), 300, 2)
    final = deploy("api")
    return {"worker_exited_after_patch": exited, "exit_code": exit_code,
            "generation": [before["generation"], patched["generation"], final["generation"]],
            "restarted_at_unchanged_on_reapply": patched["restarted_at"] == final["restarted_at"],
            "recovered_and_updated": recovered, "recovery_seconds": round(time.time() - t0, 1),
            "message_updates": len(h.updates(ts))}


def alert_flow(h):
    wrong = h.alert(alert_body(*CRASH), token="wrong-token")
    body = alert_body(*CRASH)
    t0 = time.time()
    codes = [h.alert(body) for _ in range(3)]         # Alertmanager retry / repeat of the same group
    post = h.wait_post(t0, lambda p: p["channel"] == ALERT_CH, timeout=120)
    time.sleep(8)
    result = {"wrong_token_status": wrong, "deliveries": codes,
              "posts_for_three_deliveries": len(h.posts_since(t0, ALERT_CH)),
              "seconds_to_post": round(post["at"] - t0, 2) if post else None,
              "buttons": [b for b in ("ops_status", "ops_request") if post and button(post, b)]}
    # The restart path needs a restart proposal; the model does not always give one, so try
    # the other no-recent-deploy wordings until it does (each attempt is recorded).
    tried = []
    for summary in [CRASH[2]] + ADVICE_CASES["crashloop_no_recent_deploy"][3][1:]:
        if post and button(post, "ops_request"):
            break
        t0 = time.time()
        h.alert(alert_body(CRASH[0], CRASH[1], summary))
        post = h.wait_post(t0, lambda p: p["channel"] == ALERT_CH, timeout=120)
        tried.append(bool(post and button(post, "ops_request")))
    result["alerts_until_restart_proposal"] = tried
    if post and button(post, "ops_status"):
        t1 = time.time()
        h.click("U_VIEWER", "ops_status", "api", post["ts"], channel=ALERT_CH)
        reply = h.wait_post(t1, lambda p: p["thread_ts"] == post["ts"] and "Ready" in p["text"])
        result["status_reply_in_thread"] = bool(reply)
    if post and button(post, "ops_request"):
        before = deploy("api")["generation"]
        t2 = time.time()
        h.click("U_OPS1", "ops_request", "api", post["ts"], channel=ALERT_CH)
        aid, ts = h.wait_request(t2, thread_ts=post["ts"])
        h.click("U_OPS2", "ops_approve", aid, ts, channel=ALERT_CH, thread_ts=post["ts"])
        wait_for(lambda: any("완료" in u["body"]["text"] for u in h.updates(ts)), 60)
        result["alert_to_restart"] = {"request_in_alert_thread": bool(aid),
                                      "generation_increase": deploy("api")["generation"] - before,
                                      "seconds_from_alert": round(time.time() - t0, 1)}
    return result


ADVICE_CASES = {
    # (alertname, deployment, expected proposal, summaries)
    "crashloop_no_recent_deploy": ("PodCrashLooping", "api", "restart", [
        "api Pod가 최근 10분 동안 5회 재시작했습니다. 최근 배포는 없습니다.",
        "api 컨테이너가 OOMKilled 후 반복 재시작 중입니다(재시작 8회). 마지막 배포는 3일 전입니다.",
        "api Pod 2개 중 1개가 CrashLoopBackOff 상태입니다. 오늘 배포 이력은 없습니다.",
        "api 재시작 횟수가 15분 사이 3회에서 9회로 늘었습니다. 배포 변경 없음.",
        "api Pod가 DB 연결 고갈 오류 후 계속 재시작합니다. 최근 배포 없음."]),
    "crashloop_right_after_deploy": ("PodCrashLooping", "api", "none", [
        "api v1.4.2 배포 5분 뒤부터 Pod가 반복 재시작합니다(재시작 6회).",
        "방금 배포한 api 새 버전 Pod가 CrashLoopBackOff입니다.",
        "api 배포 직후 readiness 실패로 Pod가 재시작을 반복합니다."]),
    "queue_backlog": ("SQSQueueBacklog", "stt-worker", "none", [
        "stt 작업 큐에 메시지 1,200개가 쌓였고 가장 오래된 메시지는 9분입니다.",
        "stt-worker 큐 적체가 30분째 증가 중입니다(현재 4,800개).",
        "stt 큐의 가장 오래된 메시지가 15분을 넘었습니다. 워커 replica는 최대치입니다.",
        "stt-worker 처리량보다 유입이 많아 큐가 계속 늘고 있습니다.",
        "stt 큐 메시지 2,000개, DLQ에도 12개가 쌓였습니다."]),
}


def advice_quality(h):
    """Whether the advisor's proposal follows the runbook, over varied alert wording."""
    out = {}
    for name, (alertname, deployment, expected, summaries) in ADVICE_CASES.items():
        t0 = time.time()
        for s in summaries:
            h.alert(alert_body(alertname, deployment, s))
        wait_for(lambda: len(h.posts_since(t0, ALERT_CH)) >= len(summaries), 600, 2)
        posts = h.posts_since(t0, ALERT_CH)
        def proposal(p):
            if button(p, "ops_request"):
                return "restart"
            return "none" if "LLM 요약" in json.dumps(p["blocks"] or [], ensure_ascii=False) else "no_advice"
        got = Counter(proposal(p) for p in posts)
        out[name] = {"alerts": len(summaries), "expected": expected, "proposals": dict(got),
                     "matches_runbook": got.get(expected, 0)}
    return out


def model_down(h):
    h.stop_worker()
    h.base_url, saved = "http://127.0.0.1:9/v1", h.base_url
    h.start(MODEL_HEALTH_URL="")
    t0 = time.time()
    h.alert(alert_body(*CRASH))
    post = h.wait_post(t0, lambda p: p["channel"] == ALERT_CH, timeout=120)
    h.stop_worker()
    h.base_url = saved
    return {"posted": bool(post), "seconds_to_post": round(post["at"] - t0, 2) if post else None,
            "buttons": [b for b in ("ops_status", "ops_request") if post and button(post, b)]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="quality", help="advisor model alias")
    ap.add_argument("--base-url", default="http://127.0.0.1:4000/v1")
    ap.add_argument("--health", default="http://127.0.0.1:4000/health/readiness")
    ap.add_argument("--key-file", default=str(ROOT / "gateway/.keys.local.json"))
    ap.add_argument("--output", default=str(ROOT / "evidence/chatops-e2e.json"))
    ap.add_argument("--only", nargs="*", help="re-run these scenarios and merge into the existing output")
    args = ap.parse_args()
    keys = json.loads(Path(args.key_file).read_text()) if Path(args.key_file).exists() else {}

    h = Ops(args.model, args.base_url, keys.get("slack-bot", ""), args.health, keys.get("ops-advisor", ""))
    for url in (h.o["queue_url"], h.o["dlq_url"]):
        h.sqs.purge_queue(QueueUrl=url)
    h.start()
    scenarios = {"status_command": status, "approval_flow": approval_flow,
                 "double_click": lambda h: double_click(h, 6), "reject_and_expire": lambda h: reject_and_expire(h, 20),
                 "rbac_denied": rbac_denied, "alert_flow": alert_flow, "advice_quality": advice_quality,
                 "crash_after_patch": crash_after_patch, "model_down": model_down}
    previous = json.loads(Path(args.output).read_text()) if args.only and Path(args.output).exists() else {}
    results = previous.get("results", {})
    try:
        for name, run in scenarios.items():
            if not args.only or name in args.only:
                results[name] = run(h)
    finally:
        if h.worker and h.worker.poll() is None:
            h.stop_worker()
        h.slack.stop()
    calls = [x for x in h.log_lines if x.get("event") == "model_call"]
    report = {"scope": "LocalStack + kind Kubernetes (RBAC-scoped ServiceAccount token) + loopback mock Slack; "
                       f"advisor model '{args.model}' via {args.base_url}. Not AWS, EKS or real Slack.",
              "date": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "model_call_outcomes": dict(Counter(x["outcome"] for x in calls)),
              "results": results}
    if args.only:
        report.update(model_call_outcomes=previous.get("model_call_outcomes"), date=previous.get("date"),
                      rerun={"scenarios": args.only, "date": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                             "model_call_outcomes": dict(Counter(x["outcome"] for x in calls))})
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
