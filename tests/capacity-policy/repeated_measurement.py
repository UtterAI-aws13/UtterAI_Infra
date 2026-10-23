"""Repeated local policy measurements. No inference, EC2, or cloud API calls.

Every trial uses a fresh Deployment so the production 30-minute repair cooldown
is respected. Keep production controller resources, probes and 5s reconcile loop.
Only the synthetic HTTP workload has a 1s termination grace to avoid spending
30s waiting for Python's PID-1 default signal behavior between observations.
"""

import argparse
import datetime
import hashlib
import importlib.util
import json
import math
import pathlib
import statistics
import subprocess
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "integration", ROOT / "tests/capacity-policy/integration.py"
)
integration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(integration)
lab = integration.lab
P = "capacity.utterai.io/"
CAP = "karpenter.sh/capacity-type"
NS = "capacity-test"


def percentile(values, q):
    return sorted(values)[max(0, math.ceil(len(values) * q) - 1)] if values else None


def summarize(rows):
    output = {}
    for scenario in sorted({x["scenario"] for x in rows}):
        group = [x for x in rows if x["scenario"] == scenario]
        passed = [x for x in group if x["passed"]]
        durations = [x["seconds"] for x in passed]
        deficits = [x["observed_deficit_seconds"] for x in passed]
        output[scenario] = {
            "trials": len(group),
            "passed": len(passed),
            "failed": len(group) - len(passed),
            "latency_seconds_successful_trials": {
                "min": min(durations) if durations else None,
                "p50": statistics.median(durations) if durations else None,
                "p95_nearest_rank": percentile(durations, 0.95),
                "max": max(durations) if durations else None,
            },
            "min_observed_ready_base": min(
                (x["min_ready_base"] for x in group), default=None
            ),
            "min_observed_ready_on_demand": min(
                (x["min_ready_on_demand"] for x in group), default=None
            ),
            "observed_deficit_seconds_p95": percentile(deficits, 0.95),
            "observation_intervals": sum(x["snapshots"] for x in group),
        }
    return output


class Measurement:
    def __init__(self, trials, out, interval):
        self.trials, self.out, self.interval = trials, out, interval
        self.rows, self.log_indices, self.admissions = [], {}, []
        self.nodes = {
            n["metadata"]["name"]: n["metadata"]["labels"].get(CAP)
            for n in lab.get("nodes")["items"]
        }
        self.trial = 0
        self.out.mkdir(parents=True, exist_ok=False)
        self.trace = (self.out / "observations.jsonl").open("w")
        self.raw = (self.out / "trials.jsonl").open("w")
        self.start = time.monotonic()

    def deployment(self, name, replicas=2):
        obj = integration.build()
        obj["metadata"]["name"] = name
        obj["spec"]["replicas"] = replicas
        obj["spec"]["selector"]["matchLabels"]["app"] = name
        obj["spec"]["template"]["metadata"]["labels"]["app"] = name
        obj["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] = 1
        return obj

    def pods(self, name):
        return json.loads(
            lab.kubectl("get", "pods", "-n", NS, "-l", "app=" + name, "-o", "json")
        )["items"]

    def snapshot(self, name):
        observed = []
        for pod in self.pods(name):
            meta, status = pod["metadata"], pod.get("status", {})
            observed.append(
                {
                    "uid": meta["uid"],
                    "name": meta["name"],
                    "tier": meta.get("labels", {}).get(P + "tier", "unclassified"),
                    "ready": integration.ready(pod),
                    "deleting": bool(meta.get("deletionTimestamp")),
                    "node": pod["spec"].get("nodeName"),
                    "capacity": self.nodes.get(pod["spec"].get("nodeName")),
                    "rollout": meta.get("annotations", {}).get("benchmark-rollout"),
                    "phase": status.get("phase"),
                }
            )
        live = [
            x
            for x in observed
            if not x["deleting"] and x["phase"] not in ("Succeeded", "Failed")
        ]
        ready = [x for x in live if x["ready"]]
        return {
            "live": len(live),
            "ready": len(ready),
            "terminating": len(observed) - len(live),
            "base_assigned": sum(x["tier"] == "base" for x in live),
            "base_ready": sum(
                x["tier"] == "base" and x["capacity"] == "on-demand" for x in ready
            ),
            "burst_ready": sum(x["tier"] == "burst" for x in ready),
            "on_demand_ready": sum(x["capacity"] == "on-demand" for x in ready),
            "spot_ready": sum(x["capacity"] == "spot" for x in ready),
            "unclassified_ready": sum(x["tier"] == "unclassified" for x in ready),
            "pods": observed,
        }

    def settled(self, name, n, classified=True):
        snap = self.snapshot(name)
        valid = snap["live"] == n and snap["ready"] == n and snap["terminating"] == 0
        if classified:
            valid = (
                valid
                and snap["base_ready"] == min(n, 2)
                and snap["burst_ready"] == max(0, n - 2)
            )
        return snap if valid else None

    def wait(self, fn, timeout=90):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = fn()
            if value:
                return value
            time.sleep(self.interval)
        raise TimeoutError("setup/cleanup condition timed out")

    def scale(self, name, n):
        lab.kubectl("scale", "deployment/" + name, "-n", NS, "--replicas=" + str(n))

    def case(self, scenario, name, action, complete, base_floor=2, timeout=90):
        snapshots = [(0.0, self.snapshot(name))]
        started = time.monotonic()
        error = None
        try:
            action()
            while True:
                snap = self.snapshot(name)
                elapsed = time.monotonic() - started
                snapshots.append((elapsed, snap))
                self.trace.write(
                    json.dumps(
                        {
                            "trial": self.trial,
                            "scenario": scenario,
                            "elapsed": round(elapsed, 6),
                            **snap,
                        }
                    )
                    + "\n"
                )
                if complete(snap):
                    break
                if elapsed >= timeout:
                    raise TimeoutError("case completion timed out")
                time.sleep(self.interval)
            if min(s["base_ready"] for _, s in snapshots) < base_floor:
                raise AssertionError("Ready base floor violated in observed samples")
        except Exception as exc:
            error = type(exc).__name__ + ": " + str(exc)
        # Deficit is a discrete observation estimate, not continuous availability.
        deficit = sum(
            t1 - t0
            for (t0, s0), (t1, _) in zip(snapshots, snapshots[1:])
            if s0["base_ready"] < 2
        )
        row = {
            "trial": self.trial,
            "scenario": scenario,
            "passed": error is None,
            "seconds": round(time.monotonic() - started, 6),
            "error": error,
            "snapshots": len(snapshots),
            "min_ready_base": min(x["base_ready"] for _, x in snapshots),
            "min_ready_on_demand": min(x["on_demand_ready"] for _, x in snapshots),
            "observed_deficit_seconds": round(deficit, 6),
            "final": {k: v for k, v in snapshots[-1][1].items() if k != "pods"},
        }
        self.rows.append(row)
        self.raw.write(json.dumps(row) + "\n")
        self.raw.flush()
        self.trace.flush()
        print(json.dumps(row), flush=True)
        self.write_summary()
        if error:
            raise RuntimeError(
                error
            )  # Keep failed trial and stop; never silently drop.
        return snapshots[-1][1]

    def collect_logs(self):
        import re

        for pod in lab.get("pods", "capacity-system")["items"]:
            meta = pod["metadata"]
            if (
                meta.get("deletionTimestamp")
                or meta.get("labels", {}).get("app") != "capacity-policy"
            ):
                continue
            text = lab.kubectl(
                "logs", meta["name"], "-n", "capacity-system", "--tail=-1"
            )
            lines = text.splitlines()
            for index, line in enumerate(
                lines[self.log_indices.get(meta["uid"], 0) :],
                start=self.log_indices.get(meta["uid"], 0),
            ):
                if match := re.search(r"admission_seconds=([0-9.]+)", line):
                    self.admissions.append(
                        {
                            "trial": self.trial,
                            "pod_uid": meta["uid"],
                            "line": index,
                            "seconds": float(match[1]),
                            "log": line,
                        }
                    )
            self.log_indices[meta["uid"]] = len(lines)
        (self.out / "admission.json").write_text(
            json.dumps(self.admissions, indent=2) + "\n"
        )

    def write_summary(self, complete=False):
        milliseconds = [r["seconds"] * 1000 for r in self.admissions]
        summary = {
            "scope": "local kind Kubernetes; synthetic HTTP Pods; simulated capacity labels; no EC2, GPU inference or cloud API",
            "completed": complete,
            "requested_trials_per_scenario": self.trials,
            "poll_sleep_seconds": self.interval,
            "latency_start": "immediately before API mutation command; includes client/API round-trip",
            "latency_end": "first observed state satisfying scenario predicate; excludes later cleanup",
            "percentile": "nearest rank for p95; median for p50; successful trials only with failures counted separately",
            "synthetic_termination_grace_seconds": 1,
            "base_floor_interpretation": "minimum observed Ready base, not continuous availability guarantee",
            "scenarios": summarize(self.rows),
            "admission": {
                "samples": len(milliseconds),
                "p50_ms": statistics.median(milliseconds) if milliseconds else None,
                "p95_ms": percentile(milliseconds, 0.95),
                "max_ms": max(milliseconds) if milliseconds else None,
            },
            "wall_seconds": round(time.monotonic() - self.start, 3),
            "aws_resources_created": 0,
        }
        (self.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    def webhook_ready(self, n):
        pods = [
            p
            for p in lab.get("pods", "capacity-system")["items"]
            if p["metadata"].get("labels", {}).get("app") == "capacity-policy"
        ]
        if n == 0:
            return not pods
        return len(pods) == n and all(integration.ready(p) for p in pods)

    def run_trial(self, i):
        self.trial = i
        name = "repeat-" + str(i).zfill(3)
        outage = name + "-outage"
        lab.apply([self.deployment(name)])
        self.wait(lambda: self.settled(name, 2))
        exact = lambda n: (
            lambda s: (
                s["live"] == n
                and s["ready"] == n
                and s["base_ready"] == min(n, 2)
                and s["burst_ready"] == max(0, n - 2)
            )
        )
        self.case(
            "scale_out_2_to_8",
            name,
            lambda: self.scale(name, 8),
            lambda s: exact(8)(s) and s["spot_ready"] == 6,
        )
        self.wait(lambda: self.settled(name, 8))
        self.case("scale_in_8_to_2", name, lambda: self.scale(name, 2), exact(2))
        self.wait(lambda: self.settled(name, 2))
        self.scale(name, 8)
        self.wait(lambda: self.settled(name, 8))
        nonce = str(time.time_ns())

        def rollout():
            lab.kubectl(
                "patch",
                "deployment/" + name,
                "-n",
                NS,
                "--type=merge",
                "-p",
                json.dumps(
                    {
                        "spec": {
                            "template": {
                                "metadata": {
                                    "annotations": {"benchmark-rollout": nonce}
                                }
                            }
                        }
                    }
                ),
            )

        self.case(
            "rolling_update_8_pods",
            name,
            rollout,
            lambda s: (
                exact(8)(s)
                and all(p["rollout"] == nonce for p in s["pods"] if not p["deleting"])
            ),
        )
        self.wait(lambda: self.settled(name, 8))
        self.scale(name, 2)
        self.wait(lambda: self.settled(name, 2))
        victim = next(
            p
            for p in self.pods(name)
            if not p["metadata"].get("deletionTimestamp")
            and p["metadata"]["labels"].get(P + "tier") == "base"
        )
        self.case(
            "base_pod_replacement",
            name,
            lambda: lab.kubectl(
                "delete", "pod", victim["metadata"]["name"], "-n", NS, "--wait=false"
            ),
            lambda s: (
                exact(2)(s)
                and not any(
                    p["uid"] == victim["metadata"]["uid"] and not p["deleting"]
                    for p in s["pods"]
                )
            ),
            base_floor=0,
        )
        self.wait(lambda: self.settled(name, 2))
        before_uids = {p["metadata"]["uid"] for p in self.pods(name)}

        def damage():
            for pod in self.pods(name):
                lab.kubectl(
                    "label",
                    "pod",
                    pod["metadata"]["name"],
                    "-n",
                    NS,
                    P + "tier=burst",
                    "--overwrite",
                )

        self.case(
            "policy_label_repair",
            name,
            damage,
            lambda s: (
                exact(2)(s)
                and not any(
                    p["uid"] in before_uids and not p["deleting"] for p in s["pods"]
                )
            ),
            base_floor=0,
        )
        self.wait(lambda: self.settled(name, 2))
        self.collect_logs()
        lab.kubectl(
            "scale",
            "deployment/capacity-policy",
            "-n",
            "capacity-system",
            "--replicas=0",
        )
        self.wait(lambda: self.webhook_ready(0))
        self.case(
            "webhook_outage_create_4",
            outage,
            lambda: lab.apply([self.deployment(outage, 4)]),
            lambda s: (
                s["live"] == 4
                and s["ready"] == 4
                and s["on_demand_ready"] == 4
                and s["unclassified_ready"] == 4
            ),
            base_floor=0,
        )
        self.case(
            "webhook_restore_and_adopt",
            outage,
            lambda: lab.kubectl(
                "scale",
                "deployment/capacity-policy",
                "-n",
                "capacity-system",
                "--replicas=2",
            ),
            lambda s: (
                s["live"] == 4
                and s["ready"] == 4
                and s["base_ready"] == 2
                and s["on_demand_ready"] == 4
            ),
            base_floor=0,
        )
        self.wait(lambda: self.webhook_ready(2))
        self.collect_logs()
        for deployment in (outage, name):
            lab.kubectl("delete", "deployment", deployment, "-n", NS, "--wait=false")
            self.wait(lambda: not self.pods(deployment))
        self.write_summary()

    def run(self):
        metadata = {
            "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "context": lab.CONTEXT,
            "python": subprocess.check_output(
                ["python3", "--version"], text=True
            ).strip(),
            "kubectl": json.loads(lab.kubectl("version", "-o", "json")),
            "docker": json.loads(
                subprocess.check_output(
                    ["docker", "info", "--format", "{{json .}}"], text=True
                )
            ),
            "policy_sha256": hashlib.sha256(
                (ROOT / "components/capacity-policy/policy.py").read_bytes()
            ).hexdigest(),
            "policy_manifest": list(
                __import__("yaml").safe_load_all(
                    (
                        ROOT / "k8s/platform/capacity-policy/base/resources.yaml"
                    ).read_text()
                )
            ),
        }
        # Avoid Docker environment/account fields: keep capacity and platform only.
        docker = metadata["docker"]
        metadata["docker"] = {
            k: docker.get(k)
            for k in (
                "ServerVersion",
                "NCPU",
                "MemTotal",
                "Architecture",
                "OperatingSystem",
            )
        }
        (self.out / "environment.json").write_text(
            json.dumps(metadata, indent=2) + "\n"
        )
        try:
            self.wait(lambda: self.webhook_ready(2))
            for i in range(1, self.trials + 1):
                self.run_trial(i)
            self.write_summary(complete=True)
        finally:
            self.raw.close()
            self.trace.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=30)
    parser.add_argument("--interval", type=float, default=0.25)
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args()
    if args.trials < 1 or args.interval < 0.1:
        parser.error("positive trials and interval >= 0.1 required")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = args.output or ROOT / "tests/capacity-policy/results/repeated" / stamp
    print("OUTPUT=" + str(out), flush=True)
    Measurement(args.trials, out, args.interval).run()
