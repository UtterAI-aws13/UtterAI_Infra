import json
import html
import urllib.error
import urllib.request

from .ingress import callback_allowed
from .worker import AmbiguousDelivery, PermanentDelivery, RateLimited


class SQSQueue:
    def __init__(self, client, url):
        self.client, self.url = client, url

    def send(self, job):
        return self.client.send_message(QueueUrl=self.url, MessageBody=json.dumps(job, ensure_ascii=False))

    def receive(self):
        return self.client.receive_message(QueueUrl=self.url, MaxNumberOfMessages=1,
                                          WaitTimeSeconds=20, VisibilityTimeout=90,
                                          MessageSystemAttributeNames=["ApproximateReceiveCount"]).get("Messages", [])

    def visibility(self, receipt, seconds):
        self.client.change_message_visibility(QueueUrl=self.url, ReceiptHandle=receipt,
                                              VisibilityTimeout=seconds)

    def delete(self, receipt):
        self.client.delete_message(QueueUrl=self.url, ReceiptHandle=receipt)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Slack:
    def __init__(self, token, opener=None):
        self.token = token
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def send(self, job, text):
        text = html.escape(text, quote=False)
        if job["kind"] == "slash":
            if not callback_allowed(job["response_url"]):
                raise PermanentDelivery()
            url = job["response_url"]
            payload = {"response_type": "ephemeral", "replace_original": False, "text": text,
                       "unfurl_links": False, "unfurl_media": False, "mrkdwn": False}
            headers = {"Content-Type": "application/json"}
        else:
            if not job["thread_ts"]:
                raise PermanentDelivery()
            url = "https://slack.com/api/chat.postMessage"
            payload = {"channel": job["channel"], "thread_ts": job["thread_ts"], "text": text,
                       "mrkdwn": False, "parse": "none", "unfurl_links": False, "unfurl_media": False}
            headers = {"Content-Type": "application/json", "Authorization": "Bearer " + self.token}
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
        if job["kind"] == "slash":
            return {"response_url_accepted": True}
        try:
            body = json.loads(raw)
            if not body.get("ok"):
                if body.get("error") == "ratelimited":
                    raise RateLimited(30)
                if body.get("error") in {"invalid_auth", "token_revoked", "not_authed", "missing_scope",
                                         "channel_not_found", "not_in_channel", "is_archived", "no_text"}:
                    raise PermanentDelivery()
                raise AmbiguousDelivery()
            if not body.get("ts"):
                raise AmbiguousDelivery()
            return {"ts": body["ts"]}
        except (ValueError, TypeError, AttributeError):
            raise AmbiguousDelivery() from None
