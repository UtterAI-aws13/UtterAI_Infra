"""Minimal Kubernetes API client for the ChatOps actions: read a Deployment and its Pods,
and restart a Deployment. Standard library only; the worker image carries no kubectl.

Auth is a bearer token read from a file on every call (projected ServiceAccount tokens
rotate). Locally that is a kind ServiceAccount token; on EKS it would be an access entry
for the worker task role. Permissions are enforced by Kubernetes RBAC, not by this code.
"""
import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

RESTARTED_AT = "kubectl.kubernetes.io/restartedAt"


class KubeForbidden(Exception):
    """RBAC refused the call. Permanent until someone changes the Role."""


class KubeNotFound(Exception):
    pass


class KubeUnavailable(Exception):
    """The request never reached the API server (refused, name resolution)."""


class Kube:
    def __init__(self, api, token_file, ca_file, opener=None, timeout=10):
        p = urllib.parse.urlsplit(api)
        if p.scheme != "https" or not p.hostname:
            raise ValueError("Kubernetes API must be https")
        self.api, self.token_file, self.timeout = api.rstrip("/"), token_file, timeout
        self.opener = opener or urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca_file)))

    def _request(self, method, path, body=None, content_type="application/json"):
        with open(self.token_file, encoding="utf-8") as f:
            token = f.read().strip()
        req = urllib.request.Request(self.api + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": "Bearer " + token, "Accept": "application/json",
                                              "Content-Type": content_type})
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                return json.loads(r.read(2_000_000))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise KubeForbidden() from None
            if e.code == 404:
                raise KubeNotFound() from None
            raise
        except urllib.error.URLError as e:
            if isinstance(e.reason, (ConnectionRefusedError, socket.gaierror)):
                raise KubeUnavailable() from None
            raise

    def deployment(self, namespace, name):
        d = self._request("GET", f"/apis/apps/v1/namespaces/{q(namespace)}/deployments/{q(name)}")
        spec, status = d.get("spec", {}), d.get("status", {})
        return {"name": name, "replicas": spec.get("replicas", 0), "ready": status.get("readyReplicas", 0),
                "updated": status.get("updatedReplicas", 0), "generation": d["metadata"].get("generation"),
                "observed": status.get("observedGeneration"),
                "restarted_at": spec.get("template", {}).get("metadata", {}).get("annotations", {}).get(RESTARTED_AT),
                "selector": spec.get("selector", {}).get("matchLabels", {})}

    def pods(self, namespace, match_labels):
        selector = ",".join(f"{k}={v}" for k, v in sorted(match_labels.items()))
        items = self._request("GET", f"/api/v1/namespaces/{q(namespace)}/pods?labelSelector={q(selector)}")["items"]
        out = []
        for p in items:
            statuses = p.get("status", {}).get("containerStatuses", [])
            out.append({"name": p["metadata"]["name"], "phase": p.get("status", {}).get("phase"),
                        "ready": bool(statuses) and all(s.get("ready") for s in statuses),
                        "restarts": sum(s.get("restartCount", 0) for s in statuses)})
        return out

    def owner_deployment(self, namespace, pod):
        """Pod -> ReplicaSet -> Deployment, from ownerReferences. Alerts name pods; actions need the Deployment."""
        p = self._request("GET", f"/api/v1/namespaces/{q(namespace)}/pods/{q(pod)}")
        rs = next((o["name"] for o in p["metadata"].get("ownerReferences", []) if o["kind"] == "ReplicaSet"), None)
        if not rs:
            return None
        r = self._request("GET", f"/apis/apps/v1/namespaces/{q(namespace)}/replicasets/{q(rs)}")
        return next((o["name"] for o in r["metadata"].get("ownerReferences", []) if o["kind"] == "Deployment"), None)

    def pod_states(self, namespace, match_labels):
        """Why each container is unhealthy, in the words Kubernetes uses (CrashLoopBackOff, exit code...)."""
        selector = ",".join(f"{k}={v}" for k, v in sorted(match_labels.items()))
        items = self._request("GET", f"/api/v1/namespaces/{q(namespace)}/pods?labelSelector={q(selector)}")["items"]
        out = []
        for p in items:
            # The image the Pod was asked to run. status.image is what the runtime resolved and can
            # show another tag of the same build, which made a recovered rollout look unrecovered.
            spec = {c["name"]: c.get("image", "") for c in p.get("spec", {}).get("containers", [])}
            for c in p.get("status", {}).get("containerStatuses", []):
                waiting = (c.get("state") or {}).get("waiting") or {}
                last = (c.get("lastState") or {}).get("terminated") or {}
                out.append({"pod": p["metadata"]["name"], "phase": p.get("status", {}).get("phase"),
                            "image": spec.get(c["name"]) or c.get("image", ""), "ready": bool(c.get("ready")),
                            "restarts": c.get("restartCount", 0), "waiting": waiting.get("reason"),
                            "last_exit": last.get("exitCode"), "last_reason": last.get("reason")})
            if not p.get("status", {}).get("containerStatuses"):
                out.append({"pod": p["metadata"]["name"], "phase": p.get("status", {}).get("phase"),
                            "image": next(iter(spec.values()), ""), "ready": False, "restarts": 0, "waiting": None,
                            "last_exit": None, "last_reason": None})
        return out

    def warning_events(self, namespace, involved, limit=5):
        items = self._request("GET", f"/api/v1/namespaces/{q(namespace)}/events?fieldSelector="
                              + q(f"involvedObject.name={involved},type=Warning"))["items"]
        items.sort(key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "", reverse=True)
        return [{"reason": e.get("reason"), "message": (e.get("message") or "")[:200], "count": e.get("count", 1)}
                for e in items[:limit]]

    def argocd_app(self, name, namespace="argocd"):
        a = self._request("GET", f"/apis/argoproj.io/v1alpha1/namespaces/{q(namespace)}/applications/{q(name)}")
        st = a.get("status", {})
        return {"sync": st.get("sync", {}).get("status"), "revision": st.get("sync", {}).get("revision"),
                "health": st.get("health", {}).get("status")}

    def restart(self, namespace, name, stamp):
        """Same mechanism as `kubectl rollout restart`, with a caller-fixed timestamp.

        Re-sending the same stamp leaves the pod template unchanged, so the API server does not
        bump the generation or start a second rollout. That makes a retry after an unknown
        outcome safe, which a fresh timestamp per call would not be.
        """
        body = {"spec": {"template": {"metadata": {"annotations": {RESTARTED_AT: stamp}}}}}
        d = self._request("PATCH", f"/apis/apps/v1/namespaces/{q(namespace)}/deployments/{q(name)}",
                          body, "application/strategic-merge-patch+json")
        return {"generation": d["metadata"].get("generation")}


def q(value):
    return urllib.parse.quote(str(value), safe="")
