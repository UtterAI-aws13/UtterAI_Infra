#!/usr/bin/env python3
"""Create no AWS resources. Requires the explicitly named disposable kind cluster."""

import base64, json, os, pathlib, subprocess, tempfile, time
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
KUBECONFIG = os.environ.get(
    "CAPACITY_LAB_KUBECONFIG", "/private/tmp/utterai-capacity-kubeconfig"
)
CONTEXT = "kind-utterai-capacity-lab"


def kubectl(*args, stdin=None):
    return subprocess.check_output(
        ["kubectl", "--kubeconfig", KUBECONFIG, "--context", CONTEXT, *args],
        input=stdin,
        text=True,
    )


def apply(objects):
    return kubectl(
        "apply", "-f", "-", stdin=yaml.safe_dump_all(objects, sort_keys=False)
    )


def get(resource, ns=None):
    return json.loads(
        kubectl("get", resource, *(["-n", ns] if ns else []), "-o", "json")
    )


def wait(fn, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.5)
    raise TimeoutError("condition timed out")


def bootstrap():
    if kubectl("config", "current-context").strip() != CONTEXT:
        raise RuntimeError("dedicated lab kubeconfig required")
    nodes = get("nodes")["items"]
    for n in nodes:
        name = n["metadata"]["name"]
        kind = n["metadata"]["labels"].get("karpenter.sh/capacity-type")
        if "control-plane" in name:
            kind = "on-demand"
        tier = "base" if kind == "on-demand" else "burst"
        kubectl(
            "label",
            "node",
            name,
            "karpenter.sh/capacity-type=" + kind,
            "capacity.utterai.io/node-tier=" + tier,
            "--overwrite",
        )
    objs = list(
        yaml.safe_load_all(
            (ROOT / "k8s/platform/capacity-policy/base/resources.yaml").read_text()
        )
    )
    namespace = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": "capacity-test",
            "labels": {"capacity.utterai.io/enabled": "true"},
        },
    }
    apply([objs[0], namespace])
    with tempfile.TemporaryDirectory(prefix="utterai-capacity-tls-") as tmp:
        key = tmp + "/tls.key"
        crt = tmp + "/tls.crt"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                key,
                "-out",
                crt,
                "-days",
                "7",
                "-subj",
                "/CN=capacity-policy.capacity-system.svc",
                "-addext",
                "subjectAltName=DNS:capacity-policy.capacity-system.svc,DNS:capacity-policy.capacity-system.svc.cluster.local",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        ca = base64.b64encode(pathlib.Path(crt).read_bytes()).decode()
        secret = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "capacity-policy-tls", "namespace": "capacity-system"},
            "type": "kubernetes.io/tls",
            "data": {
                "tls.crt": ca,
                "ca.crt": ca,
                "tls.key": base64.b64encode(pathlib.Path(key).read_bytes()).decode(),
            },
        }
        apply([secret])
    for o in objs:
        if o["kind"] == "Deployment":
            s = o["spec"]["template"]["spec"]
            s["nodeSelector"] = {"karpenter.sh/capacity-type": "on-demand"}
            s["tolerations"] = [{"operator": "Exists", "effect": "NoSchedule"}]
            s["containers"][0]["imagePullPolicy"] = "Never"
        if o["kind"] == "MutatingWebhookConfiguration":
            o["webhooks"][0]["clientConfig"]["caBundle"] = ca
    role = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {"name": "capacity-policy", "namespace": "capacity-test"},
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": "capacity-policy-write",
        },
        "subjects": [
            {
                "kind": "ServiceAccount",
                "name": "capacity-policy",
                "namespace": "capacity-system",
            }
        ],
    }
    print(apply(objs + [role]))
    print(
        kubectl(
            "rollout",
            "status",
            "deployment/capacity-policy",
            "-n",
            "capacity-system",
            "--timeout=120s",
        )
    )
    # Readiness can precede kube-proxy endpoint programming by a few seconds.
    time.sleep(3)


if __name__ == "__main__":
    bootstrap()
