"""Function URL / API Gateway HTTP API v2 compatible signed ingress."""
import base64
import hashlib
import hmac
import json
import re
import time
from dataclasses import asdict, dataclass
from urllib.parse import parse_qs, urlsplit


@dataclass(frozen=True)
class Policy:
    app: str
    team: str
    channels: frozenset[str]
    users: frozenset[str]
    bot: str
    command: str = "/ask"
    alert_channel: str = ""
    ops_command: str = "/ops"

    def __post_init__(self):
        if not all((self.app, self.team, self.channels, self.users, self.bot)):
            raise ValueError("explicit single-workspace allowlists required")

    def allows(self, job):
        if job["kind"] in ("alert", "git_event"):
            # Alerts come from Alertmanager, not a Slack user; they may only reach the alert channel.
            return bool(self.alert_channel) and job["team"] == self.team and job["channel"] == self.alert_channel
        return (job["team"] == self.team and job["channel"] in self.channels
                and job["user"] in self.users)


@dataclass(frozen=True)
class Job:
    key: str
    team: str
    channel: str
    user: str
    text: str
    created: int
    kind: str
    thread_ts: str = ""
    response_url: str = ""
    version: int = 1
    message_ts: str = ""   # the mention itself, for the progress reaction (thread_ts may be the parent)


def callback_allowed(url):
    try:
        p = urlsplit(url)
        return (p.scheme == "https" and p.hostname == "hooks.slack.com"
                and p.port in (None, 443) and not p.username and not p.password
                and p.path.startswith("/commands/") and not p.query and not p.fragment)
    except ValueError:
        return False


OPS_ACTIONS = frozenset({"ops_approve", "ops_reject", "ops_request", "ops_status", "ops_rollback"})
ALERT_LABELS = ("alertname", "severity", "namespace", "deployment", "service", "pod", "container")
ALERT_ANNOTATIONS = ("summary", "description", "runbook", "runbook_url")


def alert_text(status, alerts):
    """Keep only the fields the worker uses, bounded, so the job fits the 4,000-char limit."""
    kept = []
    for a in alerts:
        labels, notes = a.get("labels") or {}, a.get("annotations") or {}
        kept.append({"labels": {k: str(labels[k])[:120] for k in ALERT_LABELS if k in labels},
                     "annotations": {k: str(notes[k])[:300] for k in ALERT_ANNOTATIONS if k in notes},
                     "startsAt": str(a.get("startsAt", ""))[:40]})
        text = json.dumps({"status": status, "alerts": kept}, ensure_ascii=False)
        if len(text) > 4000:
            kept.pop()
            break
    return json.dumps({"status": status, "alerts": kept}, ensure_ascii=False)


def reply(code, text=""):
    return {"statusCode": code, "headers": {"content-type": "application/json"},
            "body": json.dumps(text, ensure_ascii=False)}


class Ingress:
    def __init__(self, secret, queue, policy, clock=time.time, alert_token="", git_secret="", git_branch="main"):
        self.secret, self.queue, self.policy, self.clock = secret, queue, policy, clock
        self.alert_token, self.git_secret, self.git_branch = alert_token, git_secret, git_branch

    def handle(self, event):
        method = event.get("requestContext", {}).get("http", {}).get("method", "POST")
        if method != "POST":
            return reply(405)
        try:
            path = event.get("rawPath", "/")
            limit = 65536 if path == "/git" else 16384   # push payloads carry commit lists
            raw = event.get("body") or ""
            # Check encoded length before allocation/decoding.
            if not isinstance(raw, str) or len(raw) > 2 * limit:
                return reply(413)
            body = base64.b64decode(raw, validate=True) if event.get("isBase64Encoded") else raw.encode()
            if len(body) > limit:
                return reply(413)
            headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
            if path == "/git":
                job = self.git_job(headers, body)
            elif path == "/alertmanager":
                job = self.alert_job(headers, body)
            else:
                job = self.slack_job(headers, body)
            if not isinstance(job, Job):
                return job
        except (ValueError, KeyError, TypeError, AttributeError, UnicodeError):
            return reply(400)
        return self.enqueue(job)

    def slack_job(self, headers, body):
        stamp = headers.get("x-slack-request-timestamp", "")
        if abs(self.clock() - int(stamp)) > 300:
            return reply(401)
        expected = "v0=" + hmac.new(self.secret.encode(), b"v0:" + stamp.encode() + b":" + body,
                                   hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, headers.get("x-slack-signature", "")):
            return reply(401)
        kind = headers.get("content-type", "").split(";")[0].strip()
        if kind == "application/json":
            p = json.loads(body)
            if not isinstance(p, dict):
                return reply(400)
            if p.get("type") == "url_verification":
                return reply(200, {"challenge": p["challenge"]})
            if p.get("api_app_id") != self.policy.app or p.get("team_id") != self.policy.team:
                return reply(403)
            e = p.get("event", {})
            if p.get("type") != "event_callback" or e.get("type") != "app_mention":
                return reply(200)
            if e.get("bot_id") or e.get("subtype") or e.get("user") == self.policy.bot:
                return reply(200)
            # timestamp is the Slack message timestamp, NOT our receipt timestamp.
            origin = float(e["ts"])
            text = re.sub(r"^\s*<@" + re.escape(self.policy.bot) + r">\s*", "", e["text"]).strip()
            job = Job("event:" + self.policy.team + ":" + p["event_id"], p["team_id"],
                      e["channel"], e["user"], text, int(origin), "mention",
                      e.get("thread_ts") or e["ts"], message_ts=e["ts"])
        elif kind == "application/x-www-form-urlencoded":
            fields = parse_qs(body.decode(), keep_blank_values=True, strict_parsing=True,
                              max_num_fields=40)
            if any(len(v) != 1 for v in fields.values()):
                return reply(400)
            p = {k: v[0] for k, v in fields.items()}
            if "payload" in p:
                return self.click_job(json.loads(p["payload"]), stamp)
            if p.get("api_app_id") != self.policy.app or p.get("command") not in (
                    self.policy.command, self.policy.ops_command):
                return reply(403)
            if p.get("ssl_check") == "1":
                return reply(200)
            if not callback_allowed(p["response_url"]) or not p["trigger_id"]:
                return reply(400)
            # trigger_id identifies an invocation; hashing identical text would drop legitimate requests.
            key = hashlib.sha256((p["team_id"] + ":" + p["trigger_id"]).encode()).hexdigest()
            kind = "ops" if p["command"] == self.policy.ops_command else "slash"
            job = Job(kind + ":" + key, p["team_id"], p["channel_id"], p["user_id"],
                      p.get("text", "").strip(), int(stamp), kind, response_url=p["response_url"])
        else:
            return reply(415)
        return job

    def enqueue(self, job):
        data = asdict(job)
        if not self.policy.allows(data):
            return reply(403)
        if not job.text or len(job.text) > 4000 or not (0 <= self.clock() - job.created <= 600):
            return reply(400)
        try:
            # No pre-enqueue dedup write: it can lose a job between that write and SendMessage.
            # Retries are enqueued as well; the consumer owns durable deduplication.
            self.queue.send(data)
        except Exception:
            return reply(503, {"error": "queue_unavailable"})
        if job.kind == "slash":
            return reply(200, {"response_type": "ephemeral", "text": "접수했습니다. 비동기로 처리합니다."})
        if job.kind == "ops":
            # ChatOps answers in the channel, so the whole team sees who ran what.
            return reply(200, {"response_type": "in_channel"})
        return reply(200)

    def alert_job(self, headers, body):
        """Alertmanager webhook. It cannot sign like Slack, so it carries a bearer token."""
        if not self.alert_token or not self.policy.alert_channel:
            return reply(404)
        if not hmac.compare_digest(headers.get("authorization", "").encode(),
                                   ("Bearer " + self.alert_token).encode()):
            return reply(401)
        p = json.loads(body)
        alerts, status = p["alerts"], p["status"]
        if status not in ("firing", "resolved") or not isinstance(alerts, list) or not 1 <= len(alerts) <= 20:
            return reply(400)
        # Alertmanager re-sends the same group on repeat_interval and on webhook retries. A new
        # firing of the same alert has a new startsAt, so it is not swallowed by this key.
        ident = p["groupKey"] + "|" + status + "|" + ",".join(
            sorted(f"{a['fingerprint']}@{a['startsAt']}" for a in alerts))
        key = "alert:" + hashlib.sha256(ident.encode()).hexdigest()
        return Job(key, self.policy.team, self.policy.alert_channel, "alertmanager",
                   alert_text(status, alerts), int(self.clock()), "alert")

    def git_job(self, headers, body):
        """Deploy-repository webhook (Gitea, GitHub-compatible signature). Only merged/closed pull
        requests and pushes to the deploy branch matter: they drive rollback PR follow-up."""
        if not self.git_secret:
            return reply(404)
        expected = hmac.new(self.git_secret.encode(), body, hashlib.sha256).hexdigest()
        if not (hmac.compare_digest(headers.get("x-hub-signature-256", ""), "sha256=" + expected)
                or hmac.compare_digest(headers.get("x-gitea-signature", ""), expected)):
            return reply(401)
        event = headers.get("x-github-event") or headers.get("x-gitea-event") or ""
        delivery = headers.get("x-github-delivery") or headers.get("x-gitea-delivery") or ""
        if not delivery:
            return reply(400)
        p = json.loads(body)
        if event == "pull_request":
            pr = p["pull_request"]
            if p.get("action") != "closed":
                return reply(200)
            text = {"event": "pull_request", "number": int(pr["number"]), "merged": bool(pr.get("merged")),
                    "merge_sha": str(pr.get("merge_commit_sha") or "")[:64], "head_ref": str(pr["head"]["ref"])[:200]}
        elif event == "push":
            if p.get("ref") != "refs/heads/" + self.git_branch:
                return reply(200)
            files = sorted({f for c in p.get("commits", []) for k in ("added", "modified", "removed")
                            for f in c.get(k) or []})
            text = {"event": "push", "after": str(p["after"])[:64], "files": files[:50],
                    "pusher": str((p.get("pusher") or {}).get("login") or (p.get("pusher") or {}).get("username") or "")[:80]}
        else:
            return reply(200)
        # Deliveries are retried by the forge with the same id; the id is the dedupe key.
        key = "git:" + hashlib.sha256(delivery.encode()).hexdigest()
        return Job(key, self.policy.team, self.policy.alert_channel, "git", json.dumps(text), int(self.clock()),
                   "git_event")

    def click_job(self, a, stamp):
        """Slack block_actions (button click). Signed like any other Slack request."""
        if a.get("type") != "block_actions":
            return reply(200)
        if a.get("api_app_id") != self.policy.app or a["team"]["id"] != self.policy.team:
            return reply(403)
        action = a["actions"][0]
        if action["action_id"] not in OPS_ACTIONS or len(action.get("value", "")) > 200:
            return reply(400)
        container = a["container"]
        # Every click has its own trigger_id: a double click is two jobs, and the action
        # record (not this key) decides that only one of them executes.
        key = "click:" + hashlib.sha256((self.policy.team + ":" + a["trigger_id"]).encode()).hexdigest()
        text = json.dumps({"action": action["action_id"], "value": action.get("value", ""),
                           "message_ts": container["message_ts"]})
        return Job(key, self.policy.team, a["channel"]["id"], a["user"]["id"], text, int(stamp), "ops_click",
                   container.get("thread_ts") or container["message_ts"])
