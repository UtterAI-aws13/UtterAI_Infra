import json
from dataclasses import asdict

import pytest

from slack_worker.forge import image_tag, with_tag
from slack_worker.gitops import GitOpsPolicy
from slack_worker.ingress import Job, Policy
from slack_worker.local import Clock
from slack_worker.ops import Ops, OpsPolicy
from slack_worker.store import SQLiteStore
from conftest import NOW
from test_ops import RecordingSlack

KUST = "images:\n  - name: utterai/batch-worker\n    newTag: {}\n"
APP = {"path": "apps/batch-worker/kustomization.yaml", "namespace": "utterai-batch",
       "deployment": "batch-worker", "argocd_app": "batch-worker"}


def iso(ts):
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class FakeForge:
    """Deploy branch with a linear history of tag commits, plus PRs and statuses."""
    def __init__(self):
        self.history = [("c0", "prod-aa314fc", NOW - 86400)]
        self.branches, self.prs, self.statuses, self.comments = {}, [], [], []

    def commit(self, sha, tag, at):
        self.history.append((sha, tag, at))

    def head(self, branch):
        return self.history[-1][0]

    def file(self, path, ref):
        if ref in self.branches:
            return self.branches[ref], "b"
        if ref == "main":
            return KUST.format(self.history[-1][1]), "s"
        tag = next(t for s, t, _ in self.history if s == ref)
        return KUST.format(tag), "s"

    def last_tag_change(self, path, branch, depth=10):
        for (sha, tag, at), (_, old, _) in zip(reversed(self.history), reversed(self.history[:-1])):
            if tag != old:
                return {"sha": sha, "date": iso(at), "author": "ops-admin", "message": f"ci: {tag}", "old": old, "new": tag}
        return None

    def ensure_branch(self, branch, from_sha):
        self.branches.setdefault(branch, self.file(None, "main")[0])

    def ensure_file(self, path, branch, content, message):
        self.branches[branch] = content

    def find_pull(self, head):
        return next((p for p in self.prs if p["head"]["ref"] == head), None)

    def ensure_pull(self, head, base, title, body):
        pr = self.find_pull(head)
        if not pr:
            pr = {"number": len(self.prs) + 1, "head": {"ref": head, "sha": "h" + head[-7:]}, "merged": False}
            self.prs.append(pr)
        return pr

    def pull(self, number):
        return self.prs[number - 1]

    def set_status(self, sha, state, context, description, target_url=""):
        self.statuses.append((sha, state))

    def comment(self, number, body):
        self.comments.append(number)

    def pull_url(self, number):
        return f"http://forge/pulls/{number}"


class FakeKube:
    def __init__(self, tag="prod-6e134f1", healthy=False):
        self.tag, self.healthy = tag, healthy

    def owner_deployment(self, ns, pod):
        return "batch-worker"

    def deployment(self, ns, name):
        n = 2 if self.healthy else 1
        return {"name": name, "replicas": 2, "ready": n, "updated": n, "generation": 3, "observed": 3,
                "selector": {"app": name}}

    def pod_states(self, ns, labels):
        bad = {"pod": "p-new", "phase": "Running", "image": "utterai/batch-worker:" + self.tag, "ready": self.healthy,
               "restarts": 0 if self.healthy else 4, "waiting": None if self.healthy else "CrashLoopBackOff",
               "last_exit": None if self.healthy else 1, "last_reason": None if self.healthy else "Error"}
        return [bad, dict(bad, pod="p-2")] if self.healthy else [bad, {**bad, "pod": "p-old", "ready": True,
                                                                         "image": "utterai/batch-worker:prod-aa314fc",
                                                                         "waiting": None, "restarts": 0}]

    def warning_events(self, ns, involved, limit=5):
        return [{"reason": "BackOff", "message": "Back-off restarting failed container", "count": 3}]

    def argocd_app(self, name):
        return {"sync": "Synced", "health": "Healthy" if self.healthy else "Progressing", "revision": "abcdef1234"}


@pytest.fixture
def env():
    policy = Policy("A1", "T1", frozenset({"CALERT"}), frozenset({"U1", "U2", "U9"}), "B1", alert_channel="CALERT")
    clock, store, slack, forge = Clock(NOW), SQLiteStore(), RecordingSlack(), FakeForge()
    forge.commit("c1", "prod-6e134f1", NOW - 600)          # CI bumped the tag 10 minutes before the alert
    ops = Ops(store, FakeKube(), slack, policy, OpsPolicy({}, frozenset({"U1", "U2"}), verify_interval=15, verify_checks=3),
              clock=clock, forge=forge, gitops=GitOpsPolicy({"batch-worker": APP}))
    return ops, clock, store, slack, forge


seq = iter(range(10 ** 6))


def job(kind, text, user="alertmanager", thread="", created=NOW):
    return asdict(Job(f"{kind}:{next(seq)}", "T1", "CALERT", user, text, created, kind, thread))


def crash_alert(starts=NOW):
    return job("alert", json.dumps({"status": "firing", "alerts": [{
        "labels": {"alertname": "UtterAIPodCrashLoopBackOff", "severity": "critical", "namespace": "utterai-batch",
                   "pod": "p-new", "container": "worker"},
        "annotations": {"summary": "Pod CrashLoopBackOff: p-new", "runbook_url": "https://runbooks/crashloop"},
        "startsAt": iso(starts)}]}))


def rollback_value(slack):
    for b in slack.of("post")[-1]["blocks"]:
        for e in b.get("elements", []) if b["type"] == "actions" else []:
            if e["action_id"] == "ops_rollback":
                return e["value"]
    return None


def click(value, user="U1", thread="9.1", created=NOW):
    return job("ops_click", json.dumps({"action": "ops_rollback", "value": value, "message_ts": thread}), user, thread, created)


def test_crash_after_recent_tag_change_offers_a_rollback_bound_to_that_commit(env):
    ops, _, _, slack, _ = env
    assert ops.handle(crash_alert(), "o")[0] == "ack"
    post = slack.of("post")[0]
    evidence = next(b["text"]["text"] for b in post["blocks"] if b["text"]["text"].startswith("수집한 근거") if "text" in b)
    assert "prod-aa314fc → prod-6e134f1" in evidence and "exit 1(Error)" in evidence and "Pod 1/2 Ready" in evidence
    assert rollback_value(slack) == "batch-worker:c1"


def test_old_tag_change_or_other_alerts_offer_no_rollback(env):
    ops, _, _, slack, forge = env
    forge.history[-1] = ("c1", "prod-6e134f1", NOW - 3 * 3600)    # outside the 2h window
    ops.handle(crash_alert(), "o")
    assert rollback_value(slack) is None


def test_rollback_opens_one_pr_that_restores_the_previous_tag(env):
    ops, _, store, slack, forge = env
    ops.handle(crash_alert(), "o")
    value = rollback_value(slack)
    for user in ("U1", "U2", "U1"):
        assert ops.handle(click(value, user), "o-" + user)[0] == "ack"
    assert len(forge.prs) == 1
    assert image_tag(forge.branches[forge.prs[0]["head"]["ref"]]) == "prod-aa314fc"
    assert forge.statuses == [(forge.prs[0]["head"]["sha"], "success")] * 3
    assert sum("되돌리기 PR #1" in p["text"] for p in slack.of("post")) == 1 and len(slack.of("ephemeral")) == 2


def test_click_for_an_earlier_diagnosis_is_refused(env):
    ops, _, _, slack, forge = env
    ops.handle(crash_alert(), "o")
    value = rollback_value(slack)
    forge.commit("c2", "prod-79e4740", NOW - 60)                   # someone deployed a fix meanwhile
    ops.handle(click(value), "o")
    assert forge.prs == [] and "다시 확인" in slack.of("ephemeral")[0]["text"]
    forge.commit("c3", "prod-6e134f1", NOW - 30)                   # and the bad tag came back in a new commit
    ops.handle(click(value), "o2")
    assert forge.prs == []                                          # the old button is still bound to c1


def test_non_operator_cannot_open_a_rollback(env):
    ops, _, _, slack, forge = env
    ops.handle(crash_alert(), "o")
    ops.handle(click(rollback_value(slack), user="U9"), "o")
    assert forge.prs == []


def git(text):
    return job("git_event", json.dumps(text), user="git")


def open_pr(ops, slack, forge):
    ops.handle(crash_alert(), "o")
    ops.handle(click(rollback_value(slack)), "o")
    return forge.prs[0]


def test_push_after_pr_puts_it_on_hold(env):
    ops, _, store, slack, forge = env
    pr = open_pr(ops, slack, forge)
    forge.commit("c2", "prod-79e4740", NOW)
    assert ops.handle(git({"event": "push", "after": "c2", "files": [APP["path"]], "pusher": "ops-admin"}), "o")[0] == "ack"
    assert forge.statuses[-1] == (pr["head"]["sha"], "failure") and forge.comments == [1]
    assert "보류" in slack.of("post")[-1]["text"]


def test_the_merge_push_itself_is_not_a_hold(env):
    ops, _, _, slack, forge = env
    pr = open_pr(ops, slack, forge)
    pr["merged"] = True
    ops.handle(git({"event": "push", "after": "m1", "files": [APP["path"]], "pusher": "ops2"}), "o")
    assert forge.statuses[-1][1] == "success"


def test_merge_is_followed_until_recovery(env):
    ops, clock, store, slack, forge = env
    open_pr(ops, slack, forge)
    merged = git({"event": "pull_request", "number": 1, "merged": True, "merge_sha": "m1", "head_ref": "x"})
    assert ops.handle(merged, "o1") == ("retry", 15)
    clock.advance(15)
    assert ops.handle(merged, "o2") == ("retry", 15)                # Argo CD has not rolled it out yet
    ops.kube = FakeKube(tag="prod-aa314fc", healthy=True)
    clock.advance(15)
    assert ops.handle(merged, "o3")[0] == "ack"
    assert slack.of("post")[-1]["text"].startswith("복구 확인") and slack.of("post")[-1]["thread_ts"] == "9.1"


def test_merge_without_recovery_says_so(env):
    ops, clock, store, slack, forge = env
    open_pr(ops, slack, forge)
    merged = git({"event": "pull_request", "number": 1, "merged": True, "merge_sha": "m1", "head_ref": "x"})
    ops.handle(merged, "o0")
    for i in range(3):
        clock.advance(15)
        out = ops.handle(merged, f"o{i + 1}")
    assert out[0] == "ack" and slack.of("post")[-1]["text"].startswith("복구 안 됨")


def test_tag_helpers():
    assert image_tag(KUST.format("a")) == "a" and image_tag(with_tag(KUST.format("a"), "b")) == "b"
