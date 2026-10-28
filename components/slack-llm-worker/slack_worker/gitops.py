"""GitOps flows for ChatOps: gather evidence for an alert, offer a rollback pull request when a
recent image-tag change lines up with a crash, and follow the PR through merge, Argo CD sync and
recovery.

The bot only reads the cluster. Its single write path is a pull request in the deploy repository;
a person approves and merges it, and Argo CD applies it. Whether to offer a rollback is a rule
(crash alert + tag change shortly before it + the cluster runs that tag), not a model decision.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .forge import ForgeConflict, image_tag, with_tag

KST = ZoneInfo("Asia/Seoul")


@dataclass(frozen=True)
class GitOpsPolicy:
    # name -> {"path": kustomization path, "namespace", "deployment", "argocd_app"}
    apps: dict
    branch: str = "main"
    rollback_alerts: tuple = ("UtterAIPodCrashLoopBackOff",)
    change_window: int = 7200          # a tag change this long before the alert counts as a suspect
    status_context: str = "slack-ops/still-valid"


def parse_time(value):
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def kst(t):
    return t.astimezone(KST).strftime("%m/%d %H:%M") if t else "?"


class GitOpsFlows:
    """Mixed into Ops; uses its store, kube, slack, lease and job-record helpers."""

    # ── evidence ────────────────────────────────────────────────────────────────────────
    def app_for(self, namespace, deployment):
        if not self.gitops:
            return None, None
        for name, app in self.gitops.apps.items():
            if app["namespace"] == namespace and app["deployment"] == deployment:
                return name, app
        return None, None

    def gather(self, alertname, labels, starts_at):
        """Deterministic evidence: pod states, warning events, the last image-tag change in Git and
        the Argo CD state. Each lookup may fail on its own without losing the others."""
        ns, dep = labels.get("namespace"), labels.get("deployment")
        lines, rollback = [], None
        try:
            if not dep and labels.get("pod"):
                dep = self.kube.owner_deployment(ns, labels["pod"])
        except Exception as e:
            lines.append(f"대상 확인 실패({type(e).__name__})")
        if not dep:
            return {"deployment": None, "app": None, "lines": lines, "rollback": None}
        name, app = self.app_for(ns, dep)
        states = []
        try:
            d = self.kube.deployment(ns, dep)
            states = self.kube.pod_states(ns, d["selector"])
            waiting = sorted({s["waiting"] for s in states if s["waiting"]})
            exits = sorted({f"exit {s['last_exit']}({s['last_reason']})" for s in states if s["last_exit"] is not None})
            images = sorted({s["image"].rsplit(":", 1)[-1] for s in states if s["image"]})
            ready = sum(1 for s in states if s["ready"])
            # Count Pods, not the Deployment's readyReplicas: a stalled rollout keeps old Pods Ready.
            lines.append(f"{ns}/{dep}: Pod {ready}/{len(states)} Ready(목표 {d['replicas']}), "
                         f"재시작 합계 {sum(s['restarts'] for s in states)}"
                         + (f", 상태 {', '.join(waiting)}" if waiting else "") + (f", 마지막 종료 {', '.join(exits)}" if exits else "")
                         + (f", 실행 중 이미지 {', '.join(images)}" if images else ""))
            bad = next((s["pod"] for s in states if not s["ready"]), None)
            if bad:
                seen = {}
                for e in self.kube.warning_events(ns, bad, limit=5):
                    seen[(e["reason"], e["message"])] = seen.get((e["reason"], e["message"]), 0) + e["count"]
                for (reason, message), count in list(seen.items())[:2]:
                    lines.append(f"이벤트 {reason}(x{count}): {message}")
        except Exception as e:
            lines.append(f"클러스터 조회 실패({type(e).__name__})")
        if not app:
            return {"deployment": dep, "app": None, "lines": lines, "rollback": None}
        change = None
        try:
            change = self.forge.last_tag_change(app["path"], self.gitops.branch)
            if change:
                at, alert_at = parse_time(change["date"]), parse_time(starts_at)
                gap = int((alert_at - at).total_seconds() // 60) if at and alert_at else None
                lines.append(f"최근 배포: {kst(at)} 이미지 {change['old']} → {change['new']} ({change['author']}, "
                             f"\"{change['message']}\")" + (f", 알림 {gap}분 전" if gap is not None else ""))
        except Exception as e:
            lines.append(f"배포 이력 조회 실패({type(e).__name__})")
        try:
            a = self.kube.argocd_app(app["argocd_app"])
            lines.append(f"Argo CD: {a['sync']} / {a['health']}, 리비전 {str(a['revision'])[:7]}")
        except Exception as e:
            lines.append(f"Argo CD 조회 실패({type(e).__name__})")
        if change and alertname in self.gitops.rollback_alerts:
            at, alert_at = parse_time(change["date"]), parse_time(starts_at)
            recent = at and alert_at and 0 <= (alert_at - at).total_seconds() <= self.gitops.change_window
            # A stalled rolling update keeps old Pods serving; what matters is that the failing Pods
            # are the ones running the changed tag.
            failing = [s for s in states if not s["ready"] and s["image"]]
            failing_on_new = bool(failing) and all(s["image"].endswith(":" + change["new"]) for s in failing)
            if recent and failing_on_new:
                rollback = {"app": name, "from": change["new"], "to": change["old"], "change": change["sha"],
                            "value": f"{name}:{change['sha'][:12]}"}
        return {"deployment": dep, "app": name, "lines": lines, "rollback": rollback}

    # ── rollback PR ─────────────────────────────────────────────────────────────────────
    def rollback(self, job, owner, value, thread_ts):
        """`value` is "<app>:<diagnosed commit>". A click only acts on the change it was shown:
        a delayed or redelivered click must not open a PR for a later, different incident."""
        if job["user"] not in self.ops.operators:
            return self.deny(job, owner, "not_operator")
        app_name, _, diagnosed = value.partition(":")
        app = self.gitops.apps.get(app_name) if self.gitops else None
        if not app:
            return self.deny(job, owner, "unknown_target")
        # Re-check right before acting: the diagnosis is only valid while the deploy branch still
        # carries the suspect tag. Someone may have rolled back or deployed again since the alert.
        content, _ = self.forge.file(app["path"], self.gitops.branch)
        change = self.forge.last_tag_change(app["path"], self.gitops.branch)
        if (not content or not change or image_tag(content) != change["new"]
                or not diagnosed or not change["sha"].startswith(diagnosed)):
            return self.deny(job, owner, "stale_diagnosis")
        key = f"rollback:{app_name}:{change['sha'][:12]}"
        now = self.clock()
        def create(s):
            if s:
                return None
            return {"phase": "OPEN", "app": app_name, "from": change["new"], "to": change["old"],
                    "change": change["sha"], "requester": job["user"], "channel": job["channel"],
                    "thread": thread_ts, "created": now, "expires": int(now) + 35 * 86400}
        action = self.store.mutate(key, create)
        # One branch per diagnosed change: the same bad tag can come back in a later commit.
        branch = f"rollback/{app_name}-{change['sha'][:7]}"
        base = self.forge.head(self.gitops.branch)
        self.forge.ensure_branch(branch, base)
        self.forge.ensure_file(app["path"], branch, with_tag(content, change["old"]),
                               f"revert({app_name}): roll back image {change['new']} -> {change['old']}")
        body = (f"Slack ChatOps 요청: {job['user']}\n\n"
                f"- 되돌릴 변경: {change['sha'][:7]} \"{change['message']}\" ({change['author']}, {change['date']})\n"
                f"- 이미지: `{change['new']}` → `{change['old']}`\n"
                f"- 진단 기준 main: {base[:7]}. 이후 main이 바뀌면 상태 검사가 실패로 바뀝니다.\n\n"
                "merge하면 Argo CD가 반영하고, 봇이 Slack 스레드에 복구 여부를 남깁니다.")
        try:
            pr = self.forge.ensure_pull(branch, self.gitops.branch, f"Rollback {app_name} to {change['old']}", body)
        except ForgeConflict:
            pr = self.forge.find_pull(branch)  # a concurrent click opened it first
        self.forge.set_status(pr["head"]["sha"], "success", self.gitops.status_context,
                              f"main {base[:7]} 기준 진단, 배포 태그 {change['new']} 유지 중")
        announce = {}
        def record(s):
            announce.clear()
            s.update(pr=pr["number"], head=pr["head"]["sha"], base=base)
            if s["phase"] == "OPEN":
                s["phase"] = "PR_OPEN"
            if not s.get("announced_by"):
                s["announced_by"] = job["key"]
            announce["mine"] = s["announced_by"] == job["key"]
            return s
        action = self.store.mutate(key, record)
        self.store.mutate(f"rbpr:{pr['number']}", lambda s: s or {"action": key, "expires": int(now) + 35 * 86400})
        self.store.mutate(f"rbopen:{app_name}", lambda s: {"action": key, "expires": int(now) + 35 * 86400})
        if not announce.get("mine"):
            # Another click already announced this PR; tell this clicker privately.
            return self.deny(job, owner, f"already_pr #{pr['number']}")
        text = (f"되돌리기 PR #{pr['number']}: {app_name} 이미지 {change['new']} → {change['old']}. "
                f"리뷰 후 merge하면 Argo CD가 반영하고, 반영 결과를 이 스레드에 남깁니다. {self.forge.pull_url(pr['number'])}")
        sent = self.post_once(job, owner, lambda: self.slack.post(job["channel"], text, thread_ts=thread_ts))
        if sent is None:
            return "quarantine", 90
        self.finish(job, owner, "SENT", outcome="rollback_pr", action=key)
        return "ack", 0

    # ── deploy-repository events ────────────────────────────────────────────────────────
    def on_git_event(self, job, owner, state):
        data = json.loads(job["text"])
        if data["event"] == "pull_request":
            return self.on_pull_closed(job, owner, data)
        return self.on_push(job, owner, data)

    def thread_post(self, job, owner, action, text, back_to="RUN"):
        return self.post_once(job, owner, lambda: self.slack.post(action["channel"], text, thread_ts=action["thread"]),
                              back_to=back_to)

    def on_pull_closed(self, job, owner, data):
        index = self.store.get(f"rbpr:{data['number']}")
        if not index:
            self.finish(job, owner, "SENT", outcome="ignored")
            return "ack", 0
        key = index["action"]
        merged = data["merged"]
        def close(s):
            if s["phase"] in ("PR_OPEN", "STALE"):
                s.update(phase="MERGED" if merged else "CLOSED", merge_sha=data["merge_sha"], closed_at=self.clock())
            return s
        action = self.store.mutate(key, close)
        if not merged:
            sent = self.thread_post(job, owner, action, f"되돌리기 PR #{data['number']}이 반영되지 않고 닫혔습니다.")
            if sent is None:
                return "quarantine", 90
            self.finish(job, owner, "SENT", outcome="pr_closed", action=key)
            return "ack", 0
        app = self.gitops.apps[action["app"]]
        d = self.kube.deployment(app["namespace"], app["deployment"])
        def watch(s):
            s.update(phase="VERIFY", owner="", until=0, action=key,
                     verify={"kind": "gitops", "app": action["app"], "expected": action["to"], "pr": data["number"],
                             "merge_sha": data["merge_sha"], "checks": 0, "started": self.clock(),
                             "generation": d["generation"]})
            return s
        self.owned(job["key"], owner, watch)
        return "retry", self.ops.verify_interval

    def on_push(self, job, owner, data):
        """A push to the deploy branch that touches an app with an open rollback PR invalidates
        the diagnosis: fail the required status so the PR cannot merge, and say so in Slack."""
        stale = []
        for name, app in (self.gitops.apps.items() if self.gitops else []):
            if app["path"] not in data["files"]:
                continue
            index = self.store.get(f"rbopen:{name}")
            action = self.store.get(index["action"]) if index else None
            if not action or action["phase"] != "PR_OPEN" or data["after"] == action.get("base"):
                continue
            pr = self.forge.pull(action["pr"])
            if pr.get("merged"):
                continue  # this push is the PR's own merge; the pull_request event handles it
            self.forge.set_status(action["head"], "failure", self.gitops.status_context,
                                  f"진단 이후 main 변경({data['after'][:7]}): 다시 확인 필요")
            try:
                self.forge.comment(action["pr"], f"진단 이후 main에 새 변경({data['after'][:7]}, {data['pusher']})이 있어 "
                                                 "이 PR을 보류합니다. Slack에서 상태를 다시 확인하세요.")
            except Exception:
                pass  # the failed status already blocks the merge; the comment is a courtesy
            self.store.mutate(index["action"], lambda s: {**s, "phase": "STALE"} if s["phase"] == "PR_OPEN" else s)
            stale.append(action)
        if not stale:
            self.finish(job, owner, "SENT", outcome="ignored")
            return "ack", 0
        a = stale[0]
        sent = self.thread_post(job, owner, a, f"되돌리기 PR #{a['pr']} 보류: 진단 이후 main에 새 변경({data['after'][:7]}, "
                                               f"{data['pusher']})이 생겼습니다. 상태를 다시 확인한 뒤 새로 요청하세요.")
        if sent is None:
            return "quarantine", 90
        self.finish(job, owner, "SENT", outcome="pr_stale")
        return "ack", 0

    # ── after merge ─────────────────────────────────────────────────────────────────────
    def verify_gitops(self, job, owner, state):
        v = state["verify"]
        action = self.store.get(state["action"])
        app = self.gitops.apps[v["app"]]
        d = self.kube.deployment(app["namespace"], app["deployment"])
        pods = self.kube.pod_states(app["namespace"], d["selector"])
        argo = self.kube.argocd_app(app["argocd_app"])
        on_tag = [p for p in pods if p["image"].endswith(":" + v["expected"])]
        rolled = (d["observed"] or 0) >= (d["generation"] or 0) and d["updated"] == d["replicas"] == d["ready"]
        all_new = bool(pods) and len(on_tag) == len(pods)
        restarts = sum(p["restarts"] for p in on_tag)
        healthy = argo["sync"] == "Synced" and argo["health"] == "Healthy"
        checks = v["checks"] + 1
        elapsed = int(self.clock() - v["started"])
        if rolled and all_new and healthy and restarts == 0:
            text = (f"복구 확인: PR #{v['pr']} 반영 {elapsed}초 후 Argo CD {argo['sync']}/{argo['health']} "
                    f"(리비전 {str(argo['revision'])[:7]}), Pod {d['ready']}/{d['replicas']} Ready, "
                    f"이미지 {v['expected']}, 새 Pod 재시작 0회.")
            outcome = "recovered"
        elif checks >= self.ops.verify_checks:
            text = (f"복구 안 됨: PR #{v['pr']} 반영 후 {elapsed}초. Argo CD {argo['sync']}/{argo['health']}, "
                    f"Pod {d['ready']}/{d['replicas']} Ready, 새 이미지 Pod {len(on_tag)}/{len(pods)}, 재시작 {restarts}회. "
                    "되돌리기로 해결되지 않았을 수 있습니다. 담당자 확인이 필요합니다.")
            outcome = "not_recovered"
        else:
            def tick(s):
                s["verify"]["checks"] = checks
                s.update(owner="", until=0)
                return s
            self.owned(job["key"], owner, tick)
            return "retry", self.ops.verify_interval
        sent = self.thread_post(job, owner, action, text, back_to="VERIFY")
        if sent is None:
            return "quarantine", 90
        self.store.mutate(state["action"], lambda s: {**s, "phase": outcome.upper()})
        self.finish(job, owner, "SENT", outcome=outcome)
        return "ack", 0


