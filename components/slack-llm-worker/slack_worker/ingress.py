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

    def __post_init__(self):
        if not all((self.app, self.team, self.channels, self.users, self.bot)):
            raise ValueError("explicit single-workspace allowlists required")

    def allows(self, job):
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


def callback_allowed(url):
    try:
        p = urlsplit(url)
        return (p.scheme == "https" and p.hostname == "hooks.slack.com"
                and p.port in (None, 443) and not p.username and not p.password
                and p.path.startswith("/commands/") and not p.query and not p.fragment)
    except ValueError:
        return False


def reply(code, text=""):
    return {"statusCode": code, "headers": {"content-type": "application/json"},
            "body": json.dumps(text, ensure_ascii=False)}


class Ingress:
    def __init__(self, secret, queue, policy, clock=time.time):
        self.secret, self.queue, self.policy, self.clock = secret, queue, policy, clock

    def handle(self, event):
        method = event.get("requestContext", {}).get("http", {}).get("method", "POST")
        if method != "POST":
            return reply(405)
        try:
            raw = event.get("body") or ""
            # Check encoded length before allocation/decoding.
            if not isinstance(raw, str) or len(raw) > 32768:
                return reply(413)
            body = base64.b64decode(raw, validate=True) if event.get("isBase64Encoded") else raw.encode()
            if len(body) > 16384:
                return reply(413)
            headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
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
                          e.get("thread_ts") or e["ts"])
            elif kind == "application/x-www-form-urlencoded":
                fields = parse_qs(body.decode(), keep_blank_values=True, strict_parsing=True,
                                  max_num_fields=40)
                if any(len(v) != 1 for v in fields.values()):
                    return reply(400)
                p = {k: v[0] for k, v in fields.items()}
                if p.get("api_app_id") != self.policy.app or p.get("command") != self.policy.command:
                    return reply(403)
                if p.get("ssl_check") == "1":
                    return reply(200)
                if not callback_allowed(p["response_url"]) or not p["trigger_id"]:
                    return reply(400)
                # trigger_id identifies an invocation; hashing identical text would drop legitimate requests.
                key = hashlib.sha256((p["team_id"] + ":" + p["trigger_id"]).encode()).hexdigest()
                job = Job("slash:" + key, p["team_id"], p["channel_id"], p["user_id"],
                          p.get("text", "").strip(), int(stamp), "slash", response_url=p["response_url"])
            else:
                return reply(415)
            data = asdict(job)
            if not self.policy.allows(data):
                return reply(403)
            if not job.text or len(job.text) > 4000 or not (0 <= self.clock() - job.created <= 600):
                return reply(400)
        except (ValueError, KeyError, TypeError, AttributeError, UnicodeError):
            return reply(400)
        try:
            # No pre-enqueue dedup write: it can lose a job between that write and SendMessage.
            # Retries are enqueued as well; the consumer owns durable deduplication.
            self.queue.send(data)
        except Exception:
            return reply(503, {"error": "queue_unavailable"})
        if job.kind == "slash":
            return reply(200, {"response_type": "ephemeral", "text": "접수했습니다. 비동기로 처리합니다."})
        return reply(200)
