#!/usr/bin/env python3
"""Read-only gate for blue/green promotion. Never switches traffic itself."""

import argparse, json, subprocess, sys

p = argparse.ArgumentParser()
p.add_argument("--context", required=True)
p.add_argument("--namespace", required=True)
p.add_argument("--deployment", required=True)
p.add_argument("--base", type=int, required=True)
a = p.parse_args()


def get(kind):
    return json.loads(
        subprocess.check_output(
            [
                "kubectl",
                "--context",
                a.context,
                "-n",
                a.namespace,
                "get",
                kind,
                "-o",
                "json",
            ],
            text=True,
        )
    )


dep = get("deployment/" + a.deployment)
uid = dep["metadata"]["uid"]
owned = [
    r
    for r in get("replicasets")["items"]
    if any(o.get("uid") == uid for o in r["metadata"].get("ownerReferences", []))
    and r["spec"].get("replicas", 0) > 0
]
latest = max(
    owned,
    key=lambda r: int(
        r["metadata"]
        .get("annotations", {})
        .get("deployment.kubernetes.io/revision", "0")
    ),
    default=None,
)
# Old Ready base Pods must not satisfy a gate for the newly deployed version.
rsuids = {latest["metadata"]["uid"]} if latest else set()
nodes = {n["metadata"]["name"]: n for n in get("nodes")["items"]}
count = 0
for pod in get("pods")["items"]:
    if pod["metadata"].get("deletionTimestamp"):
        continue
    if not any(
        o.get("uid") in rsuids for o in pod["metadata"].get("ownerReferences", [])
    ):
        continue
    if pod["metadata"].get("labels", {}).get("capacity.utterai.io/tier") != "base":
        continue
    if (
        nodes.get(pod["spec"].get("nodeName"), {})
        .get("metadata", {})
        .get("labels", {})
        .get("karpenter.sh/capacity-type")
        != "on-demand"
    ):
        continue
    if any(
        c["type"] == "Ready" and c["status"] == "True"
        for c in pod.get("status", {}).get("conditions", [])
    ):
        count += 1
stable = (
    dep.get("status", {}).get("observedGeneration", 0) >= dep["metadata"]["generation"]
    and dep.get("status", {}).get("updatedReplicas", 0) == dep["spec"]["replicas"]
)
print(
    json.dumps(
        {
            "ready_on_demand_base": count,
            "required": a.base,
            "deployment_observed": stable,
        }
    )
)
sys.exit(0 if stable and count >= a.base else 1)
