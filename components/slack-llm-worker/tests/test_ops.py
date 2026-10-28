import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from urllib.parse import urlencode

import pytest

from slack_worker.ingress import Ingress, Job, Policy
from slack_worker.k8s import KubeForbidden
from slack_worker.local import Clock, LocalQueue
from slack_worker.ops import Advisor, Ops, OpsPolicy
from slack_worker.store import SQLiteStore
from conftest import NOW, SECRET, signed

TARGETS = {"api": {"namespace": "utterai", "deployment": "api"}}


class FakeKube:
    def __init__(self):
        self.patches, self.fail, self.lock = [], [], threading.Lock()
        self.generation, self.stamp = 1, None

    def restart(self, namespace, name, stamp):
        with self.lock:
            if self.fail:
                raise self.fail.pop(0)
            self.patches.append(stamp)
            if stamp != self.stamp:  # the API server only bumps generation on a template change
                self.stamp, self.generation = stamp, self.generation + 1
            return {"generation": self.generation}

    def deployment(self, namespace, name):
        return {"name": name, "replicas": 2, "ready": 2, "updated": 2, "generation": self.generation,
                "observed": self.generation, "restarted_at": self.stamp, "selector": {"app": name}}

    def pods(self, namespace, labels):
        return [{"name": "api-1", "phase": "Running", "ready": True, "restarts": 0}]


class RecordingSlack:
    def __init__(self):
        self.calls, self.lock = [], threading.Lock()

    def _record(self, method, **kw):
        with self.lock:
            self.calls.append({"method": method, **kw})
            return {"ts": f"{len(self.calls)}.0"}

    def post(self, channel, text, blocks=None, thread_ts=""):
        return self._record("post", channel=channel, text=text, blocks=blocks, thread_ts=thread_ts)

    def update(self, channel, ts, text, blocks=None):
        return self._record("update", channel=channel, ts=ts, text=text)

    def ephemeral(self, channel, user, text, thread_ts=""):
        return self._record("ephemeral", channel=channel, user=user, text=text)

    def of(self, method):
        return [c for c in self.calls if c["method"] == method]


@pytest.fixture
def env():
    policy = Policy("A1", "T1", frozenset({"C1", "CALERT"}), frozenset({"U1", "U2", "U3", "U9"}), "B1",
                    alert_channel="CALERT")
    clock, store, kube, slack = Clock(NOW), SQLiteStore(), FakeKube(), RecordingSlack()
    ops = Ops(store, kube, slack, policy, OpsPolicy(TARGETS, frozenset({"U1", "U2", "U3"}), 600), clock=clock)
    return ops, clock, store, kube, slack


seq = iter(range(10 ** 6))


def ops_job(user, text, kind="ops", channel="C1", thread_ts="", created=NOW):
    return asdict(Job(f"{kind}:{next(seq)}", "T1", channel, user, text, created, kind, thread_ts))


def click(user, action, value, message_ts="1.0", at=NOW):
    return ops_job(user, json.dumps({"action": action, "value": value, "message_ts": message_ts}), "ops_click",
                   created=at)


def request(ops, slack, user="U1"):
    assert ops.handle(ops_job(user, "restart api"), "o")[0] == "ack"
    post = slack.of("post")[-1]
    return post["blocks"][1]["elements"][0]["value"]


def test_restart_needs_a_second_operator(env):
    ops, _, store, kube, slack = env
    aid = request(ops, slack, "U1")
    assert ops.handle(click("U1", "ops_approve", aid), "o")[0] == "ack"   # self-approval
    assert ops.handle(click("U9", "ops_approve", aid), "o")[0] == "ack"   # allowed user, not an operator
    assert kube.patches == [] and store.get("action:" + aid)["phase"] == "OPEN"
    assert len(slack.of("ephemeral")) == 2
    assert ops.handle(click("U2", "ops_approve", aid), "o")[0] == "ack"
    record = store.get("action:" + aid)
    assert record["phase"] == "DONE" and record["approver"] == "U2" and kube.patches == [record["stamp"]]
    assert "재시작 완료" in slack.of("update")[-1]["text"]


def test_concurrent_approvals_execute_once(env):
    ops, _, store, kube, slack = env
    aid = request(ops, slack)
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda u: ops.handle(click(u, "ops_approve", aid), "o-" + u), ["U2", "U3"] * 4))
    assert len(kube.patches) == 1 and kube.generation == 2
    assert store.get("action:" + aid)["phase"] == "DONE"


def test_expired_and_rejected_never_execute(env):
    ops, clock, store, kube, slack = env
    aid = request(ops, slack)
    clock.advance(601)
    ops.handle(click("U2", "ops_approve", aid, at=clock.value), "o")
    assert store.get("action:" + aid)["phase"] == "EXPIRED"
    clock.value = NOW
    aid2 = request(ops, slack)
    ops.handle(click("U2", "ops_reject", aid2), "o")
    ops.handle(click("U3", "ops_approve", aid2), "o")
    assert store.get("action:" + aid2)["phase"] == "REJECTED" and kube.patches == []


def test_unknown_outcome_is_retried_with_the_same_stamp(env):
    ops, clock, store, kube, slack = env
    aid = request(ops, slack)
    kube.fail.append(TimeoutError())
    approve = click("U2", "ops_approve", aid)
    assert ops.handle(approve, "o1")[0] == "retry"
    assert store.get("action:" + aid)["phase"] == "EXECUTING"
    clock.advance(31)
    assert ops.handle(approve, "o2")[0] == "ack"   # SQS redelivery of the same click
    record = store.get("action:" + aid)
    assert record["phase"] == "DONE" and kube.generation == 2


def test_crash_after_patch_reapplies_without_second_rollout(env):
    ops, clock, store, kube, slack = env
    aid = request(ops, slack)
    def crash():
        raise SystemExit()
    ops.after_patch = crash
    approve = click("U2", "ops_approve", aid)
    with pytest.raises(SystemExit):
        ops.handle(approve, "o1")
    ops.after_patch = lambda: None
    clock.advance(61)                          # old lease expires
    assert ops.handle(approve, "o2")[0] == "ack"
    assert len(kube.patches) == 2 and len(set(kube.patches)) == 1 and kube.generation == 2


def test_rbac_refusal_fails_the_action(env):
    ops, _, store, kube, slack = env
    aid = request(ops, slack)
    kube.fail.append(KubeForbidden())
    ops.handle(click("U2", "ops_approve", aid), "o")
    record = store.get("action:" + aid)
    assert record["phase"] == "FAILED" and record["reason"] == "k8s_forbidden"


def test_non_operator_cannot_request_and_unknown_target_is_refused(env):
    ops, _, store, kube, slack = env
    ops.handle(ops_job("U9", "restart api"), "o")
    ops.handle(ops_job("U1", "restart db"), "o")
    assert slack.of("post") == [] and len(slack.of("ephemeral")) == 2


def test_status_is_read_only(env):
    ops, _, _, kube, slack = env
    ops.handle(ops_job("U9", "status api"), "o")
    assert "Ready 2/2" in slack.of("post")[0]["text"] and kube.patches == []


ALERT = {"status": "firing", "alerts": [{"labels": {"alertname": "PodCrashLooping", "severity": "critical",
                                                    "namespace": "utterai", "deployment": "api"},
                                         "annotations": {"summary": "api pod restarting"}, "startsAt": "t"}]}


class StubModel:
    def __init__(self, text=None, error=None):
        self.text, self.error, self.calls = text, error, 0

    def generate(self, text, context, user=""):
        self.calls += 1
        if self.error:
            raise self.error
        return {"text": self.text, "usage": {}}


def alert_job(body=ALERT):
    return asdict(Job(f"alert:{next(seq)}", "T1", "CALERT", "alertmanager", json.dumps(body), NOW, "alert"))


def buttons(post):
    return [e["action_id"] for b in post["blocks"] if b["type"] == "actions" for e in b["elements"]]


def test_alert_with_valid_proposal_offers_a_request_button(env):
    ops, _, _, kube, slack = env
    ops.advisor = Advisor(StubModel('{"summary": "api 재시작 반복", "action": "restart", "reason": "런북 1단계"}'))
    assert ops.handle(alert_job(), "o")[0] == "ack"
    assert buttons(slack.of("post")[0]) == ["ops_status", "ops_request"] and kube.patches == []


@pytest.mark.parametrize("model", [StubModel("재시작하세요"), StubModel('{"summary": "x", "action": "delete"}'),
                                   StubModel(error=TimeoutError())])
def test_alert_goes_out_without_enrichment_when_the_model_fails(env, model):
    ops, _, store, _, slack = env
    ops.advisor = Advisor(model)
    job = alert_job()
    assert ops.handle(job, "o")[0] == "ack"
    assert buttons(slack.of("post")[0]) == ["ops_status"]
    assert store.get(job["key"])["result"]["advice"].split(":")[0] in ("invalid", "unavailable")


def test_redelivered_alert_posts_once_and_calls_the_model_once(env):
    ops, clock, _, _, slack = env
    model = StubModel('{"summary": "s", "action": "none"}')
    ops.advisor = Advisor(model)
    job = alert_job()
    ops.handle(job, "o1"); ops.handle(job, "o2")
    assert len(slack.of("post")) == 1 and model.calls == 1


def test_resolved_alert_skips_the_model(env):
    ops, _, _, _, slack = env
    model = StubModel("{}")
    ops.advisor = Advisor(model)
    ops.handle(alert_job({**ALERT, "status": "resolved"}), "o")
    assert model.calls == 0 and buttons(slack.of("post")[0]) == []


# ── ingress ────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def gate():
    policy = Policy("A1", "T1", frozenset({"C1", "CALERT"}), frozenset({"U1"}), "B1", alert_channel="CALERT")
    clock = Clock(NOW + 1)
    queue = LocalQueue(clock)
    return Ingress(SECRET, queue, policy, clock, alert_token="tok"), queue


def am(body, token="tok"):
    return {"rawPath": "/alertmanager", "body": json.dumps(body),
            "headers": {"Content-Type": "application/json", "Authorization": "Bearer " + token}}


AM = {"status": "firing", "groupKey": "g1", "alerts": [{**ALERT["alerts"][0], "fingerprint": "f1",
                                                         "startsAt": "2026-10-27T00:00:00Z"}]}


def test_alertmanager_needs_the_token_and_repeats_share_a_key(gate):
    app, q = gate
    assert app.handle(am(AM, "wrong"))["statusCode"] == 401
    assert app.handle(am(AM))["statusCode"] == 200 and app.handle(am(AM))["statusCode"] == 200
    refire = {**AM, "alerts": [{**AM["alerts"][0], "startsAt": "2026-10-27T01:00:00Z"}]}
    app.handle(am(refire))
    keys = [json.loads(q.receive()[0]["Body"])["key"] for _ in range(3)]
    assert keys[0] == keys[1] != keys[2]
    assert json.loads(q.messages[0]["Body"])["channel"] == "CALERT"


def test_button_click_becomes_a_job(gate):
    app, q = gate
    payload = {"type": "block_actions", "api_app_id": "A1", "team": {"id": "T1"}, "user": {"id": "U1"},
               "channel": {"id": "C1"}, "trigger_id": "tr1", "container": {"message_ts": "9.1"},
               "actions": [{"action_id": "ops_approve", "value": "abc"}]}
    body = urlencode({"payload": json.dumps(payload)})
    assert app.handle(signed(body, kind="application/x-www-form-urlencoded"))["statusCode"] == 200
    job = json.loads(q.receive()[0]["Body"])
    assert job["kind"] == "ops_click" and json.loads(job["text"])["value"] == "abc" and job["thread_ts"] == "9.1"
    payload["actions"][0]["action_id"] = "ops_delete_everything"
    body = urlencode({"payload": json.dumps(payload)})
    assert app.handle(signed(body, kind="application/x-www-form-urlencoded"))["statusCode"] == 400


def test_ops_command_is_public_and_its_own_kind(gate):
    app, q = gate
    body = urlencode({"api_app_id": "A1", "team_id": "T1", "channel_id": "C1", "user_id": "U1",
                      "command": "/ops", "text": "status api", "trigger_id": "t2",
                      "response_url": "https://hooks.slack.com/commands/T1/1/x"})
    r = app.handle(signed(body, kind="application/x-www-form-urlencoded"))
    assert json.loads(r["body"])["response_type"] == "in_channel"
    assert json.loads(q.receive()[0]["Body"])["kind"] == "ops"


# ── Kubernetes client ──────────────────────────────────────────────────────────────────

import io
import urllib.error

from slack_worker.k8s import Kube, KubeNotFound, KubeUnavailable


class KubeOpener:
    def __init__(self, outcome):
        self.outcome, self.requests = outcome, []

    def open(self, req, timeout):
        self.requests.append(req)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return io.BytesIO(json.dumps(self.outcome).encode())


def kube(tmp_path, outcome):
    token = tmp_path / "token"
    token.write_text("sa-token\n")
    opener = KubeOpener(outcome)
    return Kube("https://127.0.0.1:6443", str(token), None, opener=opener), opener


def test_restart_is_a_fixed_stamp_strategic_merge_patch(tmp_path):
    k, opener = kube(tmp_path, {"metadata": {"generation": 7}})
    assert k.restart("utterai", "api", "2026-10-27T00:00:00Z") == {"generation": 7}
    req = opener.requests[0]
    assert req.get_method() == "PATCH" and req.full_url.endswith("/namespaces/utterai/deployments/api")
    assert req.get_header("Content-type") == "application/strategic-merge-patch+json"
    assert req.get_header("Authorization") == "Bearer sa-token"
    annotations = json.loads(req.data)["spec"]["template"]["metadata"]["annotations"]
    assert annotations == {"kubectl.kubernetes.io/restartedAt": "2026-10-27T00:00:00Z"}


@pytest.mark.parametrize("error, expected", [
    (urllib.error.HTTPError("u", 403, "x", {}, io.BytesIO()), KubeForbidden),
    (urllib.error.HTTPError("u", 404, "x", {}, io.BytesIO()), KubeNotFound),
    (urllib.error.URLError(ConnectionRefusedError()), KubeUnavailable),
])
def test_kube_failures_are_classified(tmp_path, error, expected):
    k, _ = kube(tmp_path, error)
    with pytest.raises(expected):
        k.restart("utterai", "api", "s")


def test_kube_requires_https():
    with pytest.raises(ValueError):
        Kube("http://127.0.0.1:8080", "t", None)


# ── pipeline overview and post-restart verification ──────────────────────────────────

from dataclasses import replace


class FakeQueues:
    def __init__(self):
        self.depth = {}

    def stats(self, url):
        return dict(self.depth.get(url, {"visible": 0, "inflight": 0}))


PIPE = ({"stage": "stt", "queue": "q-stt", "dlq": "d-stt", "target": "api"},)


@pytest.fixture
def pipe(env):
    ops, clock, store, kube, slack = env
    queues = FakeQueues()
    ops.queues = queues
    ops.ops = replace(ops.ops, pipeline=PIPE, verify_interval=30, verify_checks=3)
    return ops, clock, store, kube, slack, queues


def test_pipeline_overview_flags_a_stalled_consumer(pipe):
    ops, _, _, _, slack, queues = pipe
    queues.depth["q-stt"] = {"visible": 300, "inflight": 0}
    ops.handle(ops_job("U9", "pipeline"), "o")
    assert "[!] stt: 대기 300, 처리 중 0" in slack.of("post")[0]["text"] and "소비 멈춤 의심" in slack.of("post")[0]["text"]


def approve_and_watch(ops, clock, slack, queues, user="U2"):
    queues.depth["q-stt"] = {"visible": 300, "inflight": 0}
    aid = request(ops, slack)
    approve = click(user, "ops_approve", aid, at=clock.value)
    assert ops.handle(approve, "o1") == ("retry", 30)   # restart done; the same message returns to verify
    return approve


def test_verification_reports_resumed_when_the_queue_drains(pipe):
    ops, clock, store, kube, slack, queues = pipe
    approve = approve_and_watch(ops, clock, slack, queues)
    clock.advance(30)
    assert ops.handle(approve, "o2") == ("retry", 30)   # still backed up
    queues.depth["q-stt"] = {"visible": 40, "inflight": 5}
    clock.advance(30)
    assert ops.handle(approve, "o3")[0] == "ack"
    assert "처리 재개 확인" in slack.of("post")[-1]["text"] and "300 → 40" in slack.of("post")[-1]["text"]
    assert store.get(approve["key"])["outcome"] == "resumed"


def test_verification_reports_not_resumed_after_the_last_check(pipe):
    ops, clock, store, kube, slack, queues = pipe
    approve = approve_and_watch(ops, clock, slack, queues)
    outcomes = []
    for i in range(3):
        clock.advance(30)
        outcomes.append(ops.handle(approve, f"o{i + 2}")[0])
    assert outcomes == ["retry", "retry", "ack"]
    assert "처리 재개 안 됨" in slack.of("post")[-1]["text"] and len(kube.patches) == 1
    assert store.get(approve["key"])["outcome"] == "not_resumed"


def test_restart_outside_the_pipeline_does_not_verify(env):
    ops, clock, store, kube, slack = env
    aid = request(ops, slack)
    assert ops.handle(click("U2", "ops_approve", aid), "o")[0] == "ack"
