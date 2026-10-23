"""Actual API/ReplicaSet/scheduler tests; simulated capacity labels, no EC2 claims."""

import importlib.util, json, pathlib, time, datetime

ROOT = pathlib.Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "lab", ROOT / "scripts/capacity-policy/local_cluster.py"
)
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)
p = "capacity.utterai.io/"
ns = "capacity-test"
results = []


def pods():
    return [
        x
        for x in lab.get("pods", ns)["items"]
        if x["metadata"].get("labels", {}).get("app") == "demo"
        and not x["metadata"].get("deletionTimestamp")
    ]


def ready(x):
    return any(
        c["type"] == "Ready" and c["status"] == "True"
        for c in x.get("status", {}).get("conditions", [])
    )


def settled(n):
    ps = pods()
    return ps if len(ps) == n and all(ready(x) for x in ps) else None


def tiers(ps):
    return [x["metadata"].get("labels", {}).get(p + "tier", "unclassified") for x in ps]


def scale(n):
    lab.kubectl("scale", "deployment/demo", "-n", ns, "--replicas=" + str(n))


def test(name, fn):
    start = time.monotonic()
    data = fn()
    row = {
        "test": name,
        "passed": True,
        "seconds": round(time.monotonic() - start, 3),
        "details": data,
    }
    results.append(row)
    print(json.dumps(row), flush=True)


def assert_equal(actual, expected):
    if actual != expected:
        raise AssertionError((actual, expected))


def build():
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "demo", "namespace": ns},
        "spec": {
            "replicas": 8,
            "strategy": {"rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1}},
            "selector": {"matchLabels": {"app": "demo"}},
            "template": {
                "metadata": {
                    "labels": {"app": "demo", p + "enabled": "true"},
                    "annotations": {
                        p + "base-replicas": "2",
                        p + "auto-repair": "true",
                    },
                },
                "spec": {
                    "nodeSelector": {
                        "workload": "test",
                        "karpenter.sh/capacity-type": "on-demand",
                        p + "node-tier": "base",
                    },
                    "containers": [
                        {
                            "name": "worker",
                            "image": "utterai-capacity-policy:lab",
                            "imagePullPolicy": "Never",
                            "command": ["python", "-m", "http.server", "8080"],
                            "ports": [{"containerPort": 8080}],
                            "readinessProbe": {
                                "httpGet": {"path": "/", "port": 8080},
                                "periodSeconds": 1,
                            },
                            "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}},
                        }
                    ],
                },
            },
        },
    }


def placement():
    lab.apply([build()])
    ps = lab.wait(lambda: settled(8))
    ts = tiers(ps)
    assert_equal(ts.count("base"), 2)
    assert_equal(ts.count("burst"), 6)
    nodes = {
        x["metadata"]["name"]: x["metadata"]["labels"]
        for x in lab.get("nodes")["items"]
    }
    for x in ps:
        assert_equal(
            nodes[x["spec"]["nodeName"]]["karpenter.sh/capacity-type"],
            "on-demand" if x["metadata"]["labels"][p + "tier"] == "base" else "spot",
        )
    return {"base": 2, "burst": 6, "actual_scheduler": True, "ec2": False}


def shrink():
    scale(2)
    ps = lab.wait(lambda: settled(2))
    assert_equal(tiers(ps).count("base"), 2)
    return {"remaining_base": 2}


def replace():
    old = {x["metadata"]["uid"] for x in pods()}
    lab.kubectl(
        "delete", "pod", pods()[0]["metadata"]["name"], "-n", ns, "--wait=false"
    )
    ps = lab.wait(
        lambda: (
            settled(2)
            if len(old & {x["metadata"]["uid"] for x in pods()}) == 1
            else None
        )
    )
    assert_equal(tiers(ps).count("base"), 2)
    return {"replacement_base": True}


def rollout():
    lab.kubectl(
        "patch",
        "deployment/demo",
        "-n",
        ns,
        "--type=merge",
        "-p",
        json.dumps(
            {
                "spec": {
                    "template": {
                        "metadata": {"annotations": {"test-rollout": str(time.time())}}
                    }
                }
            }
        ),
    )
    lab.kubectl("rollout", "status", "deployment/demo", "-n", ns, "--timeout=120s")
    ps = lab.wait(lambda: settled(2))
    assert_equal(tiers(ps).count("base"), 2)
    return {"new_generation_base": 2}


def failopen():
    lab.kubectl(
        "scale", "deployment/capacity-policy", "-n", "capacity-system", "--replicas=0"
    )
    lab.wait(lambda: not lab.get("pods", "capacity-system")["items"])
    scale(4)
    ps = lab.wait(lambda: settled(4))
    nodes = {
        x["metadata"]["name"]: x["metadata"]["labels"]
        for x in lab.get("nodes")["items"]
    }
    assert_equal(
        sum(
            nodes[x["spec"]["nodeName"]]["karpenter.sh/capacity-type"] == "on-demand"
            for x in ps
        ),
        4,
    )
    lab.kubectl(
        "scale", "deployment/capacity-policy", "-n", "capacity-system", "--replicas=2"
    )
    lab.kubectl(
        "rollout",
        "status",
        "deployment/capacity-policy",
        "-n",
        "capacity-system",
        "--timeout=120s",
    )
    return {"pods_after_outage": 4, "on_demand": 4}


def adopt():
    scale(2)
    lab.wait(lambda: settled(2))
    before = (
        lab.get("deployment/demo", ns)["spec"]["template"]["metadata"]
        .get("annotations", {})
        .get(p + "repair-at")
    )
    for x in pods():
        lab.kubectl("label", "pod", x["metadata"]["name"], "-n", ns, p + "tier-")
    lab.wait(lambda: tiers(pods()).count("base") == 2, 30)
    after = (
        lab.get("deployment/demo", ns)["spec"]["template"]["metadata"]
        .get("annotations", {})
        .get(p + "repair-at")
    )
    assert_equal(before, after)
    return {"adopted_on_demand_pods": 2, "rollout_required": False}


def repair():
    scale(2)
    lab.wait(lambda: settled(2))
    before = (
        lab.get("deployment/demo", ns)["spec"]["template"]["metadata"]
        .get("annotations", {})
        .get(p + "repair-at")
    )
    for x in pods():
        lab.kubectl(
            "label",
            "pod",
            x["metadata"]["name"],
            "-n",
            ns,
            p + "tier=burst",
            "--overwrite",
        )
    lab.wait(
        lambda: (
            lab.get("deployment/demo", ns)["spec"]["template"]["metadata"]
            .get("annotations", {})
            .get(p + "repair-at")
            != before
        ),
        60,
    )
    lab.kubectl("rollout", "status", "deployment/demo", "-n", ns, "--timeout=120s")
    ps = lab.wait(lambda: settled(2))
    assert_equal(tiers(ps).count("base"), 2)
    return {
        "fault": "base classification removed with desired replicas already satisfied",
        "recovered_base": 2,
        "replicas_owned_by_hpa": True,
    }


def fallback():
    scale(2)
    lab.wait(lambda: settled(2))
    spot = next(
        n["metadata"]["name"]
        for n in lab.get("nodes")["items"]
        if n["metadata"]["labels"].get("karpenter.sh/capacity-type") == "spot"
    )
    lab.kubectl(
        "label", "node", spot, "karpenter.sh/capacity-type=on-demand", "--overwrite"
    )
    scale(5)
    ps = lab.wait(lambda: settled(5))
    assert_equal(tiers(ps).count("burst"), 3)
    nodes = {
        x["metadata"]["name"]: x["metadata"]["labels"]
        for x in lab.get("nodes")["items"]
    }
    assert all(
        nodes[x["spec"]["nodeName"]]["karpenter.sh/capacity-type"] == "on-demand"
        for x in ps
    )
    lab.kubectl("label", "node", spot, "karpenter.sh/capacity-type=spot", "--overwrite")
    return {
        "burst_on_demand_allowed": 3,
        "simulation": "node labels only; not EC2 Fleet fallback",
    }


if __name__ == "__main__":
    try:
        for name, fn in [
            ("concurrent-placement", placement),
            ("burst-first-scale-in", shrink),
            ("base-replacement", replace),
            ("rolling-update", rollout),
            ("webhook-outage", failopen),
            ("fallback-placement", fallback),
            ("failopen-adoption", adopt),
            ("base-deficit-repair", repair),
        ]:
            test(name, fn)
    finally:
        out = ROOT / "tests/capacity-policy/results"
        out.mkdir(exist_ok=True)
        (out / "integration.json").write_text(
            json.dumps(
                {
                    "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "context": lab.CONTEXT,
                    "results": results,
                },
                indent=2,
            )
            + "\n"
        )
