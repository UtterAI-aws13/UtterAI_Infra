"""GitOps rollback end-to-end, modelled on the real 6/30 batch-worker incident (a CI image-tag
commit, CrashLoopBackOff minutes later, fixed by deploying the previous build).

Path: CI commit in Gitea → Argo CD sync in kind → Pods crash → Alertmanager-shaped webhook →
LocalStack Lambda → SQS → worker gathers evidence (Kubernetes, Git, Argo CD) → rollback button →
rollback PR by the bot → human approval and merge in Gitea → Gitea webhook → worker follows
Argo CD sync and Pod health → recovery reported in the Slack thread.

Prerequisites: scripts/local_up.sh, scripts/kind_up.sh, scripts/gitops_up.sh, gateway stack.
Local emulation only: kind, Gitea, LocalStack and a loopback Slack mock. Not EKS, GitHub or Slack.
"""
import argparse
import base64
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from run_chatops import ALERT_CH, KUBE, Ops, button  # noqa: E402
from run_localstack import ROOT, wait_for  # noqa: E402

GITEA = "http://127.0.0.1:3300/api/v1/repos/utterai/deploy"
CRED = ROOT / "e2e/.gitops.local"
KUBECTL = ["kubectl", "--kubeconfig", str(KUBE / "kubeconfig")]
BATCH = "apps/batch-worker/kustomization.yaml"
GOOD, BAD, HOTFIX = "prod-aa314fc", "prod-6e134f1", "prod-79e4740"
RUNBOOK = "https://github.com/UtterAI-aws13/UtterAI_Infra/blob/main/docs/runbooks/pod-crashloop.md"
GITOPS = {"base": "http://127.0.0.1:3300", "repo": "utterai/deploy", "branch": "main",
          "apps": {"batch-worker": {"path": BATCH, "namespace": "utterai-batch", "deployment": "batch-worker",
                                    "argocd_app": "batch-worker"},
                   "cpu-worker": {"path": "apps/cpu-worker/kustomization.yaml", "namespace": "utterai-ai-cpu",
                                  "deployment": "cpu-worker", "argocd_app": "cpu-worker"}}}


def gitea(method, path, body=None, user="ops-admin"):
    if user == "ops-admin":
        auth = "Basic " + base64.b64encode(b"ops-admin:local-admin-test-only").decode()
    else:
        auth = "token " + (CRED / f"{user}.token").read_text().strip()
    req = urllib.request.Request(GITEA + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": auth, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        return e.code, e.read(300).decode("utf-8", "replace")


def ci_set_tag(tag, pr_no):
    """What the real CI does: one commit on main that bumps newTag."""
    _, f = gitea("GET", f"/contents/{BATCH}?ref=main")
    content = base64.b64decode(f["content"]).decode()
    old = next(line.split(":")[1].strip() for line in content.splitlines() if "newTag" in line)
    new = content.replace(f"newTag: {old}", f"newTag: {tag}")
    code, out = gitea("PUT", f"/contents/{BATCH}", {"content": base64.b64encode(new.encode()).decode(),
                                                    "sha": f["sha"], "branch": "main",
                                                    "message": f"ci(ai): update prod image tag to {tag} (#{pr_no})"})
    return out["commit"]["sha"] if code in (200, 201) else None


def pods(ns, app):
    out = json.loads(subprocess.check_output(KUBECTL + ["-n", ns, "get", "pods", "-l", f"app={app}", "-o", "json"]))
    rows = []
    for p in out["items"]:
        for c in p["status"].get("containerStatuses", []):
            rows.append({"name": p["metadata"]["name"], "image": next((s["image"] for s in p["spec"]["containers"] if s["name"] == c["name"]), c["image"]).rsplit(":", 1)[-1], "ready": c["ready"],
                         "waiting": (c.get("state") or {}).get("waiting", {}).get("reason"),
                         "restarts": c.get("restartCount", 0)})
        if not p["status"].get("containerStatuses"):
            rows.append({"name": p["metadata"]["name"], "image": "", "ready": False,
                         "waiting": p["status"].get("phase")})
    return rows


def argocd(app):
    a = json.loads(subprocess.check_output(KUBECTL + ["-n", "argocd", "get", "applications.argoproj.io", app, "-o", "json"]))
    return a["status"]["sync"]["status"], a["status"]["health"]["status"], a["status"]["sync"].get("revision", "")


def alert(h, alertname, ns, pod, container, summary, description, severity="critical"):
    """Same labels and annotations as the team's PrometheusRule (plus runbook_url)."""
    body = {"version": "4", "status": "firing", "groupKey": f'{{}}:{{alertname="{alertname}"}}',
            "alerts": [{"status": "firing", "fingerprint": f"{pod}-{int(time.time())}",
                        "startsAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "labels": {"alertname": alertname, "severity": severity, "namespace": ns, "pod": pod,
                                   "container": container},
                        "annotations": {"summary": summary, "description": description, "runbook_url": RUNBOOK}}]}
    return h.alert(body)


def crash_alert(h):
    crashing = []
    wait_for(lambda: crashing.extend(p for p in pods("utterai-batch", "batch-worker")
                                     if p["waiting"] == "CrashLoopBackOff"
                                     or (not p["ready"] and p.get("restarts", 0) >= 2)) or crashing, 180, 2)
    pod = crashing[0]["name"]
    t0 = time.time()
    alert(h, "UtterAIPodCrashLoopBackOff", "utterai-batch", pod, "worker", f"Pod CrashLoopBackOff: {pod}",
          f"utterai-batch/{pod} 컨테이너 worker가 CrashLoopBackOff 상태입니다.\n"
          f"kubectl logs {pod} -n utterai-batch --previous")
    post = h.wait_post(t0, lambda p: p["channel"] == ALERT_CH and pod in p["text"], timeout=180)
    return pod, t0, post


def section(post, prefix):
    for b in post.get("blocks") or []:
        t = (b.get("text") or {}).get("text", "")
        if t.startswith(prefix):
            return t
    return None


def wait_synced(tag, timeout=180):
    return wait_for(lambda: argocd("batch-worker")[0] == "Synced" and any(
        p["image"] == tag for p in pods("utterai-batch", "batch-worker")), timeout, 3)


def reset(tag=GOOD):
    """Start each scenario from a healthy main: no open rollback PRs, the given tag deployed."""
    _, prs = gitea("GET", "/pulls?state=open")
    for p in prs:
        gitea("PATCH", f"/pulls/{p['number']}", {"state": "closed"})
    _, f = gitea("GET", f"/contents/{BATCH}?ref=main")
    if f"newTag: {tag}" not in base64.b64decode(f["content"]).decode():
        ci_set_tag(tag, 300)
    wait_for(lambda: argocd("batch-worker")[:2] == ("Synced", "Healthy") and all(
        p["image"] == tag and p["ready"] for p in pods("utterai-batch", "batch-worker")), 240, 3)


# ── scenarios ───────────────────────────────────────────────────────────────────────────

def bad_deploy_rollback(h, clicks=3):
    reset()
    ci_sha = ci_set_tag(BAD, 355)
    t_commit = time.time()
    wait_synced(BAD)
    pod, t_alert, post = crash_alert(h)
    result = {"ci_commit": ci_sha[:7], "seconds_commit_to_alert": round(t_alert - t_commit, 1),
              "alert_posted": bool(post), "seconds_alert_to_post": round(post["at"] - t_alert, 2) if post else None,
              "evidence": (section(post, "수집한 근거") or "").split("\n")[1:] if post else None,
              "llm_summary": section(post, "LLM 요약") if post else None,
              "rollback_offered": bool(post and button(post, "ops_rollback"))}
    if not result["rollback_offered"]:
        return result
    # Double clicks from two operators: exactly one PR and one announcement.
    t1 = time.time()
    with ThreadPoolExecutor(clicks) as pool:
        list(pool.map(lambda u: h.click(u, "ops_rollback", button(post, "ops_rollback"), post["ts"], channel=ALERT_CH),
                      ["U_OPS1", "U_OPS2", "U_OPS1"][:clicks]))
    announce = h.wait_post(t1, lambda p: p["thread_ts"] == post["ts"] and "되돌리기 PR #" in p["text"], timeout=90)
    time.sleep(8)
    _, prs = gitea("GET", "/pulls?state=open")
    mine = [p for p in prs if p["head"]["ref"].startswith("rollback/batch-worker")]
    pr = mine[0] if mine else None
    result.update(concurrent_clicks=clicks, open_rollback_prs=len(mine),
                  announcements=sum(1 for p in h.posts_since(t1) if "되돌리기 PR #" in p["text"]),
                  private_notices=len(h.ephemerals(t1)))
    if not pr:
        return result
    _, f = gitea("GET", f"/contents/{BATCH}?ref={pr['head']['ref']}")
    _, statuses = gitea("GET", f"/commits/{pr['head']['sha']}/statuses")
    result.update(pr=pr["number"], pr_author=pr["user"]["login"],
                  pr_changes_tag_to=next(l.split(":")[1].strip() for l in base64.b64decode(f["content"]).decode().splitlines()
                                         if "newTag" in l),
                  required_status=[s.get("state") or s.get("status") for s in statuses if s["context"] == "slack-ops/still-valid"][:1])
    # The bot cannot merge its own PR: branch protection wants another person's approval.
    code, _ = gitea("POST", f"/pulls/{pr['number']}/merge", {"Do": "merge"}, user="slackops")
    result["bot_merge_attempt_http"] = code
    gitea("POST", f"/pulls/{pr['number']}/reviews", {"event": "APPROVED", "body": "근거 확인, 롤백 승인"}, user="ops2")
    t_merge = time.time()
    code, _ = gitea("POST", f"/pulls/{pr['number']}/merge", {"Do": "merge"}, user="ops2")
    result["human_merge_http"] = code
    done = h.wait_post(t_merge, lambda p: p["thread_ts"] == post["ts"] and p["text"].startswith(("복구 확인", "복구 안 됨")),
                       timeout=240)
    sync, health, rev = argocd("batch-worker")
    result.update(recovery_report=done["text"] if done else None,
                  seconds_merge_to_report=round(done["at"] - t_merge, 1) if done else None,
                  argocd_after=f"{sync}/{health}", running_tags=sorted({p["image"] for p in pods("utterai-batch", "batch-worker")}),
                  seconds_alert_to_recovered=round(done["at"] - t_alert, 1) if done else None)
    return result


def stale_at_click(h):
    """Someone pushes a fix between the alert and the click: the click must not open a PR."""
    reset()
    ci_set_tag(BAD, 356)
    wait_synced(BAD)
    pod, t_alert, post = crash_alert(h)
    offered = bool(post and button(post, "ops_rollback"))
    ci_set_tag(HOTFIX, 357)            # a teammate already deployed a hotfix
    t1 = time.time()
    h.click("U_OPS1", "ops_rollback", button(post, "ops_rollback"), post["ts"], channel=ALERT_CH)
    wait_for(lambda: h.ephemerals(t1), 60)
    time.sleep(3)
    _, prs = gitea("GET", "/pulls?state=open")
    wait_synced(HOTFIX)
    return {"rollback_offered_at_alert": offered, "notice": [c["body"]["text"] for c in h.ephemerals(t1)][:1],
            "prs_opened": sum(1 for p in prs if p["head"]["ref"].startswith("rollback/batch-worker"))}


def stale_after_pr(h):
    """A new commit lands while the rollback PR waits for review: the PR is put on hold."""
    reset()
    ci_set_tag(BAD, 358)
    wait_synced(BAD)
    pod, t_alert, post = crash_alert(h)
    t1 = time.time()
    h.click("U_OPS1", "ops_rollback", button(post, "ops_rollback"), post["ts"], channel=ALERT_CH)
    h.wait_post(t1, lambda p: p["thread_ts"] == post["ts"] and "되돌리기 PR #" in p["text"], timeout=90)
    _, prs = gitea("GET", "/pulls?state=open")
    pr = next(p for p in prs if p["head"]["ref"].startswith("rollback/batch-worker"))
    t2 = time.time()
    ci_set_tag(HOTFIX, 359)
    hold = h.wait_post(t2, lambda p: p["thread_ts"] == post["ts"] and "보류" in p["text"], timeout=90)
    _, statuses = gitea("GET", f"/commits/{pr['head']['sha']}/statuses")
    gitea("POST", f"/pulls/{pr['number']}/reviews", {"event": "APPROVED", "body": "승인"}, user="ops2")
    code, body = gitea("POST", f"/pulls/{pr['number']}/merge", {"Do": "merge"}, user="ops2")
    gitea("PATCH", f"/pulls/{pr['number']}", {"state": "closed"}, user="ops2")
    wait_synced(HOTFIX)
    return {"pr": pr["number"], "hold_notice": hold["text"] if hold else None,
            "seconds_push_to_notice": round(hold["at"] - t2, 1) if hold else None,
            "status_after_push": [s.get("state") or s.get("status") for s in statuses if s["context"] == "slack-ops/still-valid"][:1],
            "merge_attempt_http": code}


def pending_no_rollback(h):
    """The cpu-worker case: Pending for lack of capacity. Evidence yes, rollback no."""
    pod = next(p["name"] for p in pods("utterai-ai-cpu", "cpu-worker"))
    t0 = time.time()
    alert(h, "UtterAIPodPendingTooLong", "utterai-ai-cpu", pod, "worker", f"Pod Pending 5분 초과: {pod}",
          f"utterai-ai-cpu/{pod} 이 5분 이상 Pending 상태입니다.\nKarpenter 노드 프로비저닝 실패 또는 리소스 부족일 수 있습니다.",
          severity="warning")
    post = h.wait_post(t0, lambda p: p["channel"] == ALERT_CH and pod in p["text"], timeout=180)
    return {"posted": bool(post), "evidence": (section(post, "수집한 근거") or "").split("\n")[1:] if post else None,
            "llm_summary": section(post, "LLM 요약") if post else None,
            "rollback_offered": bool(post and button(post, "ops_rollback"))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default=str(ROOT / "evidence/gitops-e2e.json"))
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args()
    keys = json.loads((ROOT / "gateway/.keys.local.json").read_text())
    h = Ops("quality", "http://127.0.0.1:4000/v1", keys["slack-bot"], "http://127.0.0.1:4000/health/readiness",
            keys["ops-advisor"])
    h.sqs.purge_queue(QueueUrl=h.o["queue_url"])
    h.start(OPS_GITOPS=json.dumps(GITOPS), GITOPS_TOKEN_FILE=str(CRED / "slackops.token"),
            OPS_VERIFY_INTERVAL="15", OPS_VERIFY_CHECKS="6", OPS_PIPELINE="[]")
    scenarios = {"bad_deploy_rollback": bad_deploy_rollback, "stale_at_click": stale_at_click,
                 "stale_after_pr": stale_after_pr, "pending_no_rollback": pending_no_rollback}
    previous = json.loads(Path(args.output).read_text()) if args.only and Path(args.output).exists() else {}
    results = previous.get("results", {})
    try:
        for name, run in scenarios.items():
            if not args.only or name in args.only:
                results[name] = run(h)
                print(name, json.dumps(results[name], ensure_ascii=False), flush=True)
    finally:
        if h.worker and h.worker.poll() is None:
            h.stop_worker()
        h.slack.stop()
    calls = [x for x in h.log_lines if x.get("event") == "model_call"]
    errors = [x for x in h.log_lines if x.get("event") == "ops_error"]
    print("ops_errors", json.dumps(Counter(f"{x['kind']}:{x['error']}@{x['at']}" for x in errors)))
    report = {"scope": "kind + Argo CD v2.14 (core) + Gitea 1.22 + LocalStack + loopback mock Slack; summarizer "
                       "'quality' (qwen2.5:3b) via local LiteLLM. Incident modelled on the 6/30 batch-worker outage. "
                       "Not EKS, GitHub or real Slack.",
              "date": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "model_call_outcomes": dict(Counter(x["outcome"] for x in calls)), "results": results}
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
