import json
import html
import urllib.error
import urllib.parse
import urllib.request

from .ingress import callback_allowed
from .worker import AmbiguousDelivery, PermanentDelivery, RateLimited


class SQSQueue:
    def __init__(self, client, url, visibility=90):
        self.client, self.url, self.receive_visibility = client, url, visibility

    def send(self, job):
        return self.client.send_message(QueueUrl=self.url, MessageBody=json.dumps(job, ensure_ascii=False))

    def receive(self):
        return self.client.receive_message(QueueUrl=self.url, MaxNumberOfMessages=1,
                                          WaitTimeSeconds=20, VisibilityTimeout=self.receive_visibility,
                                          MessageSystemAttributeNames=["ApproximateReceiveCount"]).get("Messages", [])

    def visibility(self, receipt, seconds):
        self.client.change_message_visibility(QueueUrl=self.url, ReceiptHandle=receipt,
                                              VisibilityTimeout=seconds)

    def delete(self, receipt):
        self.client.delete_message(QueueUrl=self.url, ReceiptHandle=receipt)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


SLACK_API = "https://slack.com/api"


def api_base_allowed(url):
    """Real Slack, or a loopback mock for local integration tests. Nothing else."""
    if url == SLACK_API:
        return True
    p = urllib.parse.urlsplit(url)
    return p.scheme == "http" and p.hostname in ("127.0.0.1", "localhost") and not p.username


class Slack:
    def __init__(self, token, opener=None, api_base=SLACK_API):
        if not api_base_allowed(api_base):
            raise ValueError("Slack API base must be slack.com or a loopback mock")
        self.token, self.api_base = token, api_base
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def send(self, job, text):
        text = html.escape(text, quote=False)
        if job["kind"] == "slash":
            if not callback_allowed(job["response_url"]):
                raise PermanentDelivery()
            payload = {"response_type": "ephemeral", "replace_original": False, "text": text,
                       "unfurl_links": False, "unfurl_media": False, "mrkdwn": False}
            return self._call(job["response_url"], payload, {"Content-Type": "application/json"}, slash=True)
        if not job["thread_ts"]:
            raise PermanentDelivery()
        return self.post(job["channel"], text, thread_ts=job["thread_ts"])

    def post(self, channel, text, blocks=None, thread_ts=""):
        """chat.postMessage. NOT idempotent: an unknown outcome must not be retried blindly."""
        payload = {"channel": channel, "text": text, "mrkdwn": False, "parse": "none",
                   "unfurl_links": False, "unfurl_media": False}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        if blocks:
            payload["blocks"] = blocks
        return self.api("chat.postMessage", payload)

    def update(self, channel, ts, text, blocks=None):
        """chat.update replaces a message with the same content on every call: safe to repeat."""
        return self.api("chat.update", {"channel": channel, "ts": ts, "text": text, "blocks": blocks or []})

    def ephemeral(self, channel, user, text, thread_ts=""):
        payload = {"channel": channel, "user": user, "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        return self.api("chat.postEphemeral", payload)

    def api(self, method, payload):
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + self.token}
        return self._call(self.api_base + "/" + method, payload, headers)

    def _call(self, url, payload, headers, slash=False):
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with self.opener.open(req, timeout=8) as response:
                raw = response.read(65536)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    delay = int(e.headers.get("Retry-After", "30"))
                except ValueError:
                    delay = 30
                raise RateLimited(delay) from None
            # 5xx/redirect outcome is not a proof of non-delivery.
            if 400 <= e.code < 500:
                raise PermanentDelivery() from None
            raise AmbiguousDelivery() from None
        except (OSError, TimeoutError) as e:
            raise AmbiguousDelivery() from e
        if slash:
            return {"response_url_accepted": True}
        try:
            body = json.loads(raw)
            if not body.get("ok"):
                if body.get("error") == "ratelimited":
                    raise RateLimited(30)
                if body.get("error") in {"invalid_auth", "token_revoked", "not_authed", "missing_scope",
                                         "channel_not_found", "not_in_channel", "is_archived", "no_text",
                                         "message_not_found", "cant_update_message", "user_not_in_channel"}:
                    raise PermanentDelivery()
                raise AmbiguousDelivery()
            if url.endswith("/chat.postEphemeral"):
                return {"ephemeral": True}
            if not body.get("ts"):
                raise AmbiguousDelivery()
            return {"ts": body["ts"]}
        except (ValueError, TypeError, AttributeError):
            raise AmbiguousDelivery() from None
