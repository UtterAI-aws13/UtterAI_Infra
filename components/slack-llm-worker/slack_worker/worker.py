import time
import uuid


class Busy(Exception):
    pass


class LostLease(Exception):
    pass


class RateLimited(Exception):
    def __init__(self, seconds):
        self.seconds = max(1, min(43200, int(seconds)))


class PermanentDelivery(Exception):
    pass


class AmbiguousDelivery(Exception):
    pass


class ModelNotExecuted(Exception):
    """The request provably never reached a model: connection refused, name resolution failed,
    or the gateway rejected it on a rate limit before routing. Does not spend an attempt."""
    def __init__(self, retry_after=None):
        self.retry_after = retry_after


class ModelRejected(Exception):
    """Permanent gateway refusal (key not allowed for the model, budget exhausted)."""
    def __init__(self, reason):
        self.reason = reason


def unavailable_delay(count):
    # 15, 30, 60, 120, 300... seconds. With maxReceiveCount 8 a job rides out a gateway outage
    # of roughly 20 minutes before landing in the DLQ for a manual redrive.
    return min(300, 15 * 2 ** max(0, count - 1))


class Worker:
    """One handle() per occupied concurrency slot; no prefetch beyond available slots."""
    def __init__(self, store, pipeline, slack, policy, clock=time.time, lease=60, max_attempts=2, sleep=time.sleep):
        self.store, self.pipeline, self.slack = store, pipeline, slack
        self.policy, self.clock, self.lease, self.max_attempts = policy, clock, lease, max_attempts
        self.sleep = sleep

    def claim(self, job, owner):
        now = self.clock()
        def update(s):
            if s and s["phase"] in ("SENT", "AMBIGUOUS", "FAILED"):
                return None
            if s and s.get("until", 0) > now:
                raise Busy()
            if s and s["phase"] == "POSTING":
                s.update(phase="AMBIGUOUS", reason="stale_post_intent", owner="", until=0)
                return s
            s = s or {"phase": "MODEL", "attempts": 0, "expires": int(now) + 35 * 86400}
            if not self.policy.allows(job) or not 0 <= now - job["created"] <= 600:
                s.update(phase="FAILED", reason="policy_or_age")
            else:
                s.update(owner=owner, until=now + self.lease)
            return s
        return self.store.mutate(job["key"], update)

    def owned(self, key, owner, fn):
        def update(s):
            if (not s or s.get("owner") != owner or s.get("until", 0) <= self.clock()
                    or s["phase"] in ("SENT", "AMBIGUOUS", "FAILED")):
                raise LostLease()
            return fn(s)
        return self.store.mutate(key, update)

    def heartbeat(self, key, owner):
        def extend(s):
            s["until"] = self.clock() + self.lease
            return s
        self.owned(key, owner, extend)

    def channel_slot(self, job):
        now = self.clock()
        def reserve(s):
            if s and s["next"] > now:
                raise RateLimited(int(s["next"] - now) + 1)
            return {"next": now + 1.1, "expires": int(now) + 86400}
        self.store.mutate("rate:" + job["team"] + ":" + job["channel"], reserve)

    def handle(self, job, owner=None, healthy=lambda: True):
        owner = owner or str(uuid.uuid4())
        try:
            if job["version"] != 1 or job["kind"] not in ("mention", "slash"):
                return "retry", 90
            if not job["text"] or len(job["text"]) > 4000:
                return "retry", 90
            state = self.claim(job, owner)
            if state["phase"] == "SENT":
                return "ack", 0
            if state["phase"] in ("AMBIGUOUS", "FAILED"):
                return "quarantine", 90
            if "result" not in state:
                self.mark_progress(job, state)
                def attempt(s):
                    if s["attempts"] >= self.max_attempts:
                        s.update(phase="FAILED", reason="model_attempt_limit")
                    else:
                        s["attempts"] += 1
                    return s
                state = self.owned(job["key"], owner, attempt)
                if state["phase"] == "FAILED":
                    return "quarantine", 90
                # Failure points are handled differently: an undelivered request is refunded,
                # a permanent refusal fails fast, and anything else (read timeout, 5xx after the
                # request left) keeps the spent attempt because the model may have run.
                try:
                    result = self.pipeline.run(job)
                except ModelNotExecuted as e:
                    def refund(s):
                        s["attempts"] -= 1
                        s["unavailable"] = s.get("unavailable", 0) + 1
                        return s
                    state = self.owned(job["key"], owner, refund)
                    self.release(job, owner)
                    return "retry", e.retry_after or unavailable_delay(state["unavailable"])
                except ModelRejected as e:
                    self.finish(job["key"], owner, "FAILED", reason=e.reason)
                    return "quarantine", 90
                def save(s):
                    s.update(result=result, phase="RESULT")
                    return s
                self.owned(job["key"], owner, save)
            # Reserve a shared per-channel slot BEFORE recording post intent.
            if not healthy():
                raise LostLease()
            # Local channel contention waits in the occupied slot with the heartbeat running.
            # Re-enqueueing every blocked job burns ReceiveCount and can DLQ a healthy burst.
            for _ in range(15):
                if not healthy():
                    raise LostLease()
                try:
                    self.channel_slot(job)
                    break
                except RateLimited as e:
                    self.sleep(min(e.seconds, 2))
            else:
                raise RateLimited(30)
            def intent(s):
                s["phase"] = "POSTING"
                return s
            state = self.owned(job["key"], owner, intent)
            try:
                if not healthy():
                    raise LostLease()
                sent = self.slack.send(job, state["result"]["text"])
            except RateLimited:
                def reset(s):
                    s["phase"] = "RESULT"
                    return s
                self.owned(job["key"], owner, reset)
                raise
            except PermanentDelivery:
                self.finish(job["key"], owner, "FAILED", reason="slack_rejected")
                return "quarantine", 90
            except Exception:
                # Includes timeout, HTTP 5xx, malformed response. Slack may have accepted the post.
                self.finish(job["key"], owner, "AMBIGUOUS", reason="unknown_slack_outcome")
                return "quarantine", 90
            self.finish(job["key"], owner, "SENT", delivery=sent)
            return "ack", 0
        except RateLimited as e:
            self.release(job, owner)
            return "retry", e.seconds
        except Busy:
            return "retry", self.lease
        except Exception:
            # Never expose raw prompts, response URLs, SDK exception strings or tokens in logs.
            self.release(job, owner)
            return "retry", 90

    def mark_progress(self, job, state):
        # Best effort, before the first model attempt only. It must never fail or delay the job.
        if state.get("attempts") or not job.get("message_ts") or not hasattr(self.slack, "react"):
            return
        try:
            self.slack.react(job["channel"], job["message_ts"])
        except Exception:
            pass

    def finish(self, key, owner, phase, **extra):
        def update(s):
            s.update(phase=phase, owner="", until=0, **extra)
            return s
        self.owned(key, owner, update)

    def release(self, job, owner):
        def update(s):
            # POSTING MUST NOT revert on an unknown failure (including failed SENT persistence).
            if s["phase"] != "POSTING":
                s["until"] = 0
                s["owner"] = ""
            return s
        try:
            self.owned(job["key"], owner, update)
        except Exception:
            pass
