"""UtterAI admission policy and reconciliation. Python stdlib; no cloud credentials.

The admission path reserves slots with Kubernetes resourceVersion CAS. ConfigMaps
are namespace/ReplicaSet scoped, not process-local counters. Failures leave the
original On-Demand Pod template intact (failurePolicy=Ignore).
"""

import base64
import copy
import hashlib
import json
import logging
import os
import signal
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PREFIX = "capacity.utterai.io/"
CAPACITY = "karpenter.sh/capacity-type"
MANAGED = PREFIX + "enabled"
TIER = PREFIX + "tier"
RESERVATION = PREFIX + "reservation"
BASE = PREFIX + "base-replicas"
REPAIR = PREFIX + "repair-at"
LOG = logging.getLogger("capacity-policy")


class APIError(Exception):
    def __init__(self, status, message=""):
        self.status = status
        super().__init__(f"Kubernetes HTTP {status}: {message[:300]}")


class API:
    def __init__(self):
        directory = "/var/run/secrets/kubernetes.io/serviceaccount"
        self.url = os.environ.get("KUBE_API", "https://kubernetes.default.svc")
        self.directory = directory
        self.context = ssl.create_default_context(cafile=directory + "/ca.crt")

    def call(self, method, path, obj=None, content_type="application/json"):
        # Read the projected token on each call, so token rotation works.
        with open(self.directory + "/token") as f:
            token = f.read().strip()
        request = urllib.request.Request(
            self.url + path,
            method=method,
            data=json.dumps(obj).encode() if obj is not None else None,
            headers={"Authorization": "Bearer " + token, "Content-Type": content_type},
        )
        try:
            with urllib.request.urlopen(
                request, context=self.context, timeout=0.7
            ) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise APIError(e.code, e.read().decode()) from e


def collection(resource, ns=None):
    group = "/apis/apps/v1" if resource in ("replicasets", "deployments") else "/api/v1"
    return group + ("/namespaces/" + ns if ns else "") + "/" + resource


def controller_owner(obj, kind):
    return next(
        (
            o
            for o in obj.get("metadata", {}).get("ownerReferences", [])
            if o.get("controller") and o["kind"] == kind
        ),
        None,
    )


def live(pod):
    return not pod["metadata"].get("deletionTimestamp") and pod.get("status", {}).get(
        "phase"
    ) not in ("Succeeded", "Failed")


def ready(pod):
    return live(pod) and any(
        c["type"] == "Ready" and c["status"] == "True"
        for c in pod.get("status", {}).get("conditions", [])
    )


def clean_slots(slots, pods, now):
    by_reservation = {
        p["metadata"].get("annotations", {}).get(RESERVATION): p for p in pods
    }
    cleaned = {}
    for key, value in slots.items():
        pod = by_reservation.get(key)
        if pod:
            if live(pod):
                cleaned[key] = dict(value, bound=pod["metadata"]["uid"])
        elif not value.get("bound") and now - value["at"] < 120:
            cleaned[key] = value
    return cleaned


def base_count(pod):
    number = int(pod["metadata"].get("annotations", {}).get(BASE, "0"))
    if not 0 <= number <= 20:
        raise ValueError("base-replicas must be between 0 and 20")
    return number


def validate_template(pod):
    # This exact owned selector is removed ONLY for burst. Other affinity stays.
    if pod["spec"].get("nodeSelector", {}).get(CAPACITY) != "on-demand":
        raise ValueError("opt-in template must default to On-Demand")
    base_count(pod)


def mutate(pod, tier, reservation):
    result = copy.deepcopy(pod)
    metadata = result["metadata"]
    metadata.setdefault("labels", {})[TIER] = tier
    annotations = metadata.setdefault("annotations", {})
    annotations[RESERVATION] = reservation
    annotations["controller.kubernetes.io/pod-deletion-cost"] = (
        "1000" if tier == "base" else "-1000"
    )
    if tier == "burst":
        result["spec"]["nodeSelector"].pop(CAPACITY, None)
        result["spec"]["nodeSelector"][PREFIX + "node-tier"] = "burst"
        node_affinity = (
            result["spec"].setdefault("affinity", {}).setdefault("nodeAffinity", {})
        )
        preferred = node_affinity.setdefault(
            "preferredDuringSchedulingIgnoredDuringExecution", []
        )
        preference = {
            "weight": 100,
            "preference": {
                "matchExpressions": [
                    {"key": CAPACITY, "operator": "In", "values": ["spot"]}
                ]
            },
        }
        if preference not in preferred:
            preferred.append(preference)
    # Patch only owned maps plus spec fields, preserving existing array contents.
    patches = []
    for key in ("labels", "annotations"):
        patches.append(
            {"op": "add", "path": "/metadata/" + key, "value": metadata[key]}
        )
    if tier == "burst":
        for key in ("nodeSelector", "affinity"):
            patches.append(
                {"op": "add", "path": "/spec/" + key, "value": result["spec"][key]}
            )
    return patches


class Policy:
    def __init__(self, api, now=time.time):
        self.api, self.now = api, now
        self.counters = {}
        self.lock = threading.Lock()
        self.last_reconcile = 0

    def count(self, name):
        with self.lock:
            self.counters[name] = self.counters.get(name, 0) + 1

    def state_name(self, rs_uid):
        return "capacity-" + hashlib.sha256(rs_uid.encode()).hexdigest()[:24]

    def pods(self, ns):
        selector = urllib.parse.quote(MANAGED + "=true")
        return self.api.call(
            "GET", collection("pods", ns) + "?labelSelector=" + selector
        )["items"]

    def state(self, ns, rs):
        path = collection("configmaps", ns) + "/" + self.state_name(rs["uid"])
        try:
            return path, self.api.call("GET", path)
        except APIError as e:
            if e.status != 404:
                raise
        obj = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": self.state_name(rs["uid"]),
                "namespace": ns,
                "labels": {PREFIX + "state": "true"},
                "ownerReferences": [
                    {
                        "apiVersion": "apps/v1",
                        "kind": "ReplicaSet",
                        "name": rs["name"],
                        "uid": rs["uid"],
                    }
                ],
            },
            "data": {"reservations": "{}"},
        }
        try:
            return path, self.api.call("POST", collection("configmaps", ns), obj)
        except APIError as e:
            if e.status != 409:
                raise
            return path, self.api.call("GET", path)

    def reserve(self, pod, request_uid):
        ns = pod["metadata"]["namespace"]
        owner = controller_owner(pod, "ReplicaSet")
        if not owner:
            raise ValueError("only ReplicaSet-owned Pods may opt in")
        rs = self.api.call("GET", collection("replicasets", ns) + "/" + owner["name"])
        if rs["metadata"]["uid"] != owner["uid"] or not controller_owner(
            rs, "Deployment"
        ):
            raise ValueError("owner must be the current Deployment ReplicaSet")
        target = min(base_count(pod), rs["spec"].get("replicas", 0))
        for attempt in range(8):
            path, state = self.state(ns, owner)
            slots = json.loads(state["data"]["reservations"])
            if request_uid in slots:
                return slots[request_uid]["tier"]
            # Read after state: a concurrent reservation/GC will cause a PUT conflict.
            all_pods = [
                p
                for p in self.pods(ns)
                if (controller_owner(p, "ReplicaSet") or {}).get("uid") == owner["uid"]
            ]
            slots = clean_slots(slots, all_pods, self.now())
            pods = [p for p in all_pods if live(p)]
            seen = {p["metadata"].get("annotations", {}).get(RESERVATION) for p in pods}
            active = sum(
                p["metadata"].get("labels", {}).get(TIER) == "base" for p in pods
            )
            # Retain reservations through admission timeout/persistence window. Never
            # expire an existing Pending Pod just because its model/node starts slowly.
            reserved = sum(
                v["tier"] == "base" and k not in seen for k, v in slots.items()
            )
            tier = "base" if active + reserved < target else "burst"
            slots[request_uid] = {"tier": tier, "at": self.now()}
            state["data"]["reservations"] = json.dumps(slots)
            try:
                self.api.call("PUT", path, state)
                return tier
            except APIError as e:
                if e.status != 409:
                    raise
                self.count("reservation_conflicts_total")
                time.sleep(min(0.003 * (attempt + 1), 0.025))
        raise RuntimeError("reservation contention; use original On-Demand template")

    def admit(self, request):
        pod = request["object"]
        if (
            request.get("operation") != "CREATE"
            or pod.get("metadata", {}).get("labels", {}).get(MANAGED) != "true"
        ):
            return {"uid": request["uid"], "allowed": True}
        pod = copy.deepcopy(pod)
        pod["metadata"].setdefault("namespace", request["namespace"])
        # A repeated admission of an already-mutated object must be a no-op.
        if pod["metadata"].get("annotations", {}).get(RESERVATION):
            return {"uid": request["uid"], "allowed": True}
        validate_template(pod)
        if request.get("dryRun"):
            return {
                "uid": request["uid"],
                "allowed": True,
                "warnings": [
                    "Dry-run retains On-Demand default; no capacity slot is reserved."
                ],
            }
        tier = self.reserve(pod, request["uid"])
        self.count("admitted_" + tier + "_total")
        return {
            "uid": request["uid"],
            "allowed": True,
            "patchType": "JSONPatch",
            "patch": base64.b64encode(
                json.dumps(mutate(pod, tier, request["uid"])).encode()
            ).decode(),
        }

    def reconcile(self):
        # API LIST is authoritative; no process-local informer count used for slots.
        deployments = self.api.call("GET", collection("deployments"))["items"]
        nodes = {
            n["metadata"]["name"]: n
            for n in self.api.call("GET", collection("nodes"))["items"]
        }
        with self.lock:
            for key in list(self.counters):
                if key.startswith(("base_target{", "base_ready{")):
                    del self.counters[key]
        for dep in deployments:
            template = dep["spec"]["template"]
            if template["metadata"].get("labels", {}).get(MANAGED) != "true":
                continue
            ns = dep["metadata"]["namespace"]
            rs_list = [
                r
                for r in self.api.call("GET", collection("replicasets", ns))["items"]
                if (controller_owner(r, "Deployment") or {}).get("uid")
                == dep["metadata"]["uid"]
            ]
            rs_uids = {r["metadata"]["uid"] for r in rs_list}
            all_pods = self.pods(ns)
            for rs in rs_list:
                if rs["spec"].get("replicas", 0) == 0:
                    continue
                path, state = self.state(ns, rs["metadata"])
                slots = json.loads(state["data"]["reservations"])
                cleaned = clean_slots(slots, all_pods, self.now())
                if slots != cleaned:
                    state["data"]["reservations"] = json.dumps(cleaned)
                    try:
                        self.api.call("PUT", path, state)
                    except APIError as e:
                        if e.status != 409:
                            raise
            pods = [
                p
                for p in all_pods
                if (controller_owner(p, "ReplicaSet") or {}).get("uid") in rs_uids
                and live(p)
            ]
            target = min(base_count(template), dep["spec"].get("replicas", 0))
            base_pods = [
                p for p in pods if p["metadata"].get("labels", {}).get(TIER) == "base"
            ]
            ready_base = sum(
                ready(p)
                and nodes.get(p["spec"].get("nodeName"), {})
                .get("metadata", {})
                .get("labels", {})
                .get(CAPACITY)
                == "on-demand"
                for p in base_pods
            )
            self.observe(dep, target, ready_base)
            # Adopt fail-open Pods only if the template still requires On-Demand.
            for p in pods:
                if (
                    p["metadata"].get("labels", {}).get(TIER)
                    or p["spec"].get("nodeSelector", {}).get(CAPACITY) != "on-demand"
                ):
                    continue
                siblings = [
                    x
                    for x in base_pods
                    if controller_owner(x, "ReplicaSet")
                    == controller_owner(p, "ReplicaSet")
                ]
                if len(siblings) >= base_count(template):
                    continue
                self.api.call(
                    "PATCH",
                    collection("pods", ns) + "/" + p["metadata"]["name"],
                    {
                        "metadata": {
                            "labels": {TIER: "base"},
                            "annotations": {
                                "controller.kubernetes.io/pod-deletion-cost": "1000"
                            },
                        }
                    },
                    "application/merge-patch+json",
                )
                base_pods.append(p)
                self.count("adopted_total")
            # A recovery rollout is conditional and rate-limited; never change replicas.
            # Default manual: explicitly set auto-repair=true after worker drain readiness.
            annotations = template["metadata"].get("annotations", {})
            if (
                len(base_pods) < target
                and annotations.get(PREFIX + "auto-repair") == "true"
            ):
                self.repair(dep, pods, target)
        self.last_reconcile = self.now()
        with self.lock:
            self.counters["last_reconcile_timestamp_seconds"] = self.last_reconcile

    def observe(self, dep, target, ready_base):
        key = dep["metadata"]["namespace"] + "/" + dep["metadata"]["name"]
        with self.lock:
            self.counters['base_target{workload="' + key + '"}'] = target
            self.counters['base_ready{workload="' + key + '"}'] = ready_base

    def repair(self, dep, pods, target):
        status, spec = dep.get("status", {}), dep["spec"]
        if status.get("observedGeneration", 0) < dep["metadata"].get("generation", 1):
            return
        desired = spec.get("replicas", 0)
        if (
            len(pods) != desired
            or status.get("updatedReplicas", 0) != desired
            or status.get("availableReplicas", 0) < desired
        ):
            return  # Existing rollout/startup/failure recovery owns the next step.
        annotations = spec["template"]["metadata"].get("annotations", {})
        previous = float(annotations.get(REPAIR, "0"))
        if self.now() - previous < 1800:
            return
        if annotations.get(PREFIX + "worker") == "true":
            for p in pods:
                ip = p.get("status", {}).get("podIP")
                if not ip:
                    return
                try:
                    with urllib.request.urlopen(
                        "http://" + ip + ":9091/state", timeout=0.5
                    ) as r:
                        state = json.load(r)
                    if not state["ready"] or state["active"] or state["draining"]:
                        return
                except (OSError, ValueError, KeyError):
                    return
        # Optimistic concurrency prevents two webhook replicas starting two rollouts.
        body = {
            "metadata": {"resourceVersion": dep["metadata"]["resourceVersion"]},
            "spec": {
                "template": {"metadata": {"annotations": {REPAIR: str(self.now())}}}
            },
        }
        self.api.call(
            "PATCH",
            collection("deployments", dep["metadata"]["namespace"])
            + "/"
            + dep["metadata"]["name"],
            body,
            "application/merge-patch+json",
        )
        self.count("repair_rollouts_total")

    def metrics(self):
        with self.lock:
            return "".join(
                "utterai_capacity_" + k + " " + str(v) + "\n"
                for k, v in sorted(self.counters.items())
            )


def serve(policy, port=8443, cert=None, key=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path == "/metrics":
                body = policy.metrics().encode()
                content_type = "text/plain; version=0.0.4"
            elif self.path in ("/healthz", "/readyz"):
                if self.path == "/readyz" and policy.now() - policy.last_reconcile > 30:
                    self.send_error(503, "waiting for successful reconciliation")
                    return
                body, content_type = b"ok", "text/plain"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if urllib.parse.urlsplit(self.path).path != "/mutate":
                self.send_error(404)
                return
            started = time.monotonic()
            try:
                size = int(self.headers.get("Content-Length", 0))
                if not 0 < size <= 2_000_000:
                    raise ValueError("invalid request size")
                review = json.loads(self.rfile.read(size))
                response = policy.admit(review["request"])
                body = json.dumps(
                    {
                        "apiVersion": "admission.k8s.io/v1",
                        "kind": "AdmissionReview",
                        "response": response,
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                policy.count("errors_total")
                LOG.warning("Admission unavailable: %s", type(exc).__name__)
                self.send_error(503, "policy unavailable; retain original template")
            finally:
                policy.count("requests_total")
                LOG.info("admission_seconds=%.6f", time.monotonic() - started)

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    if cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)

        def reload_certificate():
            last = (os.stat(cert).st_mtime_ns, os.stat(key).st_mtime_ns)
            while True:
                time.sleep(10)
                try:
                    stamp = (os.stat(cert).st_mtime_ns, os.stat(key).st_mtime_ns)
                    if stamp != last:
                        ctx.load_cert_chain(cert, key)
                        last = stamp
                        LOG.info("TLS certificate reloaded")
                except (OSError, ssl.SSLError):
                    LOG.warning("TLS reload failed; retaining loaded certificate")

        threading.Thread(target=reload_certificate, daemon=True).start()
    return server


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    policy = Policy(API())
    signal.signal(signal.SIGTERM, lambda *_: exit(0))

    def loop():
        while True:
            try:
                policy.reconcile()
            except Exception as exc:
                policy.count("reconcile_errors_total")
                LOG.warning("Reconcile failed: %s", type(exc).__name__)
            time.sleep(5)

    threading.Thread(target=loop, daemon=True).start()
    serve(policy, cert="/tls/tls.crt", key="/tls/tls.key").serve_forever()
