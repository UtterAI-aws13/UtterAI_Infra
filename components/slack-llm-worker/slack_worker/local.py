"""Deterministic queue simulation: not a replacement for AWS integration tests."""
import copy
import json
import threading
import uuid


class Clock:
    def __init__(self, value=1800000000):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class LocalQueue:
    def __init__(self, clock, max_receives=8):
        self.clock, self.max_receives = clock, max_receives
        self.messages, self.dlq = [], []
        self.lock = threading.Lock()

    def send(self, job):
        with self.lock:
            self.messages.append({"Body": json.dumps(job), "visible": self.clock(), "count": 0})

    def receive(self):
        with self.lock:
            for m in list(self.messages):
                if m["visible"] > self.clock():
                    continue
                if m["count"] >= self.max_receives:
                    self.messages.remove(m)
                    self.dlq.append(m)
                    continue
                m.update(count=m["count"] + 1, visible=self.clock() + 90, ReceiptHandle=str(uuid.uuid4()))
                return [dict(copy.deepcopy(m), Attributes={"ApproximateReceiveCount": str(m["count"])})]
        return []

    def visibility(self, receipt, seconds):
        with self.lock:
            for m in self.messages:
                if m.get("ReceiptHandle") == receipt:
                    m["visible"] = self.clock() + seconds
                    return
        raise ValueError("stale receipt")

    def delete(self, receipt):
        with self.lock:
            for m in self.messages:
                if m.get("ReceiptHandle") == receipt:
                    self.messages.remove(m)
                    return
        raise ValueError("stale receipt")


class FakeSlack:
    def __init__(self):
        self.messages = []

    def send(self, job, text):
        self.messages.append({"key": job["key"], "channel": job["channel"],
                              "thread_ts": job["thread_ts"], "text": text})
        return {"ts": str(len(self.messages))}
