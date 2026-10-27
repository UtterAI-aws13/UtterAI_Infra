from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading

import pytest

from slack_worker.local import Clock, FakeSlack, LocalQueue
from slack_worker.pipeline import FakeModel, Pipeline
from slack_worker.store import SQLiteStore
from slack_worker.worker import Busy, LostLease, ModelNotExecuted, ModelRejected, RateLimited, Worker


@pytest.fixture
def setup(policy):
    clock, store, model, slack = Clock(), SQLiteStore(), FakeModel(), FakeSlack()
    worker = Worker(store, Pipeline(model), slack, policy, clock, sleep=clock.advance)
    return worker, clock, store, model, slack


def test_duplicate_sqs_delivery_runs_model_once(setup, job):
    w, _, store, model, slack = setup
    assert w.handle(job)[0] == "ack"
    assert w.handle(job)[0] == "ack"
    assert model.calls == len(slack.messages) == 1
    assert store.get(job["key"])["phase"] == "SENT"


def test_same_event_concurrent_fanout(setup, job):
    w, _, _, model, slack = setup
    with ThreadPoolExecutor(max_workers=16) as p:
        results = list(p.map(lambda _: w.handle(job), range(100)))
    assert model.calls == len(slack.messages) == 1
    assert any(r[0] == "ack" for r in results)


def test_success_then_delete_failure_redelivery(setup, job):
    w, clock, _, model, slack = setup
    q = LocalQueue(clock); q.send(job)
    raw = q.receive()[0]
    assert w.handle(job)[0] == "ack"  # crash before DeleteMessage
    clock.advance(91)
    assert q.receive()[0]["ReceiptHandle"] != raw["ReceiptHandle"]
    assert w.handle(job)[0] == "ack"
    assert len(slack.messages) == model.calls == 1


def test_slack_accepted_but_timeout_quarantines(setup, job):
    w, clock, store, model, slack = setup
    def timeout(job, text):
        slack.messages.append(text)  # remote side effect happened
        raise TimeoutError()
    slack.send = timeout
    assert w.handle(job)[0] == "quarantine"
    clock.advance(91)
    assert w.handle(job)[0] == "quarantine"
    assert store.get(job["key"])["phase"] == "AMBIGUOUS"
    assert model.calls == len(slack.messages) == 1


def test_stale_posting_no_auto_resend(setup, job):
    w, clock, store, model, slack = setup
    w.claim(job, "dead")
    def intent(s): s.update(phase="POSTING", result={"text": "answer"}); return s
    w.owned(job["key"], "dead", intent)
    clock.advance(61)
    assert w.handle(job)[0] == "quarantine"
    assert store.get(job["key"])["phase"] == "AMBIGUOUS"
    assert not slack.messages and model.calls == 0


def test_429_preserves_result_and_honors_retry_after(setup, job):
    w, clock, store, model, slack = setup
    send = slack.send
    def throttled(*_): raise RateLimited(42)
    slack.send = throttled
    assert w.handle(job) == ("retry", 42)
    assert store.get(job["key"])["phase"] == "RESULT"
    clock.advance(42); slack.send = send
    assert w.handle(job)[0] == "ack"
    assert model.calls == 1


def test_fencing_stops_old_owner(setup, job):
    w, clock, _, _, slack = setup
    w.claim(job, "old"); clock.advance(61); w.claim(job, "new")
    with pytest.raises(LostLease): w.heartbeat(job["key"], "old")
    with pytest.raises(Busy): w.claim(job, "third")
    assert not slack.messages


def test_heartbeat_extends_lease_and_queue_visibility(setup, job):
    w, clock, _, _, _ = setup
    q = LocalQueue(clock); q.send(job); raw = q.receive()[0]; w.claim(job, "owner")
    clock.advance(50); w.heartbeat(job["key"], "owner"); q.visibility(raw["ReceiptHandle"], 90)
    clock.advance(45)
    assert not q.receive()
    with pytest.raises(Busy): w.claim(job, "other")


def test_model_failure_has_hard_attempt_cap(setup, job):
    w, clock, store, model, _ = setup
    calls = []
    def fail(_): calls.append(1); raise TimeoutError()
    w.pipeline.run = fail
    for _ in range(8):
        w.handle(job); clock.advance(2)
    assert len(calls) == 2
    assert store.get(job["key"])["phase"] == "FAILED"


def test_undelivered_model_request_does_not_spend_attempts(setup, job):
    w, clock, store, model, slack = setup
    real = w.pipeline.run
    outage = {"left": 5}
    def gateway_down(j):
        if outage["left"]:
            outage["left"] -= 1
            raise ModelNotExecuted()
        return real(j)
    w.pipeline.run = gateway_down
    delays = [w.handle(job) for _ in range(5)]
    assert [d for _, d in delays] == [15, 30, 60, 120, 240]
    s = store.get(job["key"])
    assert s["phase"] == "MODEL" and s["attempts"] == 0 and s["unavailable"] == 5
    assert w.handle(job)[0] == "ack"  # gateway back: the job still has its full attempt budget
    assert model.calls == len(slack.messages) == 1


def test_gateway_retry_after_is_honored(setup, job):
    w, *_ = setup
    def limited(_): raise ModelNotExecuted(retry_after=7)
    w.pipeline.run = limited
    assert w.handle(job) == ("retry", 7)


def test_permanent_gateway_refusal_fails_fast(setup, job):
    w, _, store, model, slack = setup
    def denied(_): raise ModelRejected("model_access_denied")
    w.pipeline.run = denied
    assert w.handle(job)[0] == "quarantine"
    s = store.get(job["key"])
    assert s["phase"] == "FAILED" and s["reason"] == "model_access_denied"
    assert not slack.messages


def test_lost_response_keeps_spent_attempt(setup, job):
    w, clock, store, *_ = setup
    def lost(_): raise TimeoutError()  # request left; the model may have run
    w.pipeline.run = lost
    w.handle(job)
    assert store.get(job["key"])["attempts"] == 1


def test_local_queue_redrive_after_receives(setup, job):
    _, clock, _, _, _ = setup
    q = LocalQueue(clock, max_receives=3); q.send(job)
    for _ in range(3):
        assert q.receive(); clock.advance(91)
    assert not q.receive() and len(q.dlq) == 1


def test_policy_rechecked_before_model(setup, job):
    w, _, _, model, slack = setup
    job["user"] = "unauthorized"
    assert w.handle(job)[0] == "quarantine"
    assert model.calls == 0 and not slack.messages


def test_shared_channel_rate_slot(setup, job):
    w, clock, _, model, slack = setup
    assert w.handle(job)[0] == "ack"
    second = dict(job, key="event:T1:Ev2")
    assert w.handle(second)[0] == "ack"
    assert clock() >= job["created"] + 1.1
    assert model.calls == len(slack.messages) == 2


def test_heartbeat_failure_prevents_post(setup, job):
    w, _, store, model, slack = setup
    assert w.handle(job, healthy=lambda: False)[0] == "retry"
    assert not slack.messages
    assert store.get(job["key"])["phase"] == "RESULT"


def test_sqlite_persistence_across_restart(tmp_path, policy, job):
    path = str(tmp_path / "state.db")
    model, slack, clock = FakeModel(), FakeSlack(), Clock()
    assert Worker(SQLiteStore(path), Pipeline(model), slack, policy, clock).handle(job)[0] == "ack"
    assert Worker(SQLiteStore(path), Pipeline(model), slack, policy, clock).handle(job)[0] == "ack"
    assert model.calls == 1


def test_local_retrieval_and_no_evidence(tmp_path, job):
    (tmp_path / "guide.md").write_text("SQS visibility timeout은 처리 중 메시지를 숨깁니다.")
    model = FakeModel(); pipeline = Pipeline(model, tmp_path)
    result = pipeline.run(dict(job, text="kb SQS visibility"))
    assert "guide.md" in result["text"] and model.calls == 1
    assert "찾지 못" in pipeline.run(dict(job, text="kb unrelated"))["text"]
    assert model.calls == 1


def test_crash_after_post_before_checkpoint_is_quarantined(setup, job):
    w, clock, store, model, slack = setup
    real_cas = store.cas
    def fail_sent(key, rev, value):
        if value.get("phase") == "SENT": raise OSError("storage unavailable")
        return real_cas(key, rev, value)
    store.cas = fail_sent
    assert w.handle(job)[0] == "retry"
    assert store.get(job["key"])["phase"] == "POSTING"
    clock.advance(61); store.cas = real_cas
    assert w.handle(job)[0] == "quarantine"
    assert model.calls == len(slack.messages) == 1
