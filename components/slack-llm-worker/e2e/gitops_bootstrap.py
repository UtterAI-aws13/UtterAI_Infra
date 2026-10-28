"""Seed the local Gitea deploy repository and the Argo CD Applications that watch it.

Users mirror the real roles: ops-admin pushes like CI does (image tag commits), slackops is the
bot (opens rollback PRs only), ops1/ops2 are humans who review and merge. main is protected:
one approval, the bot's "still valid" status check, and an up-to-date branch are required.
Credentials are local test values written to e2e/.gitops.local/ (gitignored).
"""
import base64
import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GITEA = "http://127.0.0.1:3300/api/v1"
ADMIN = ("ops-admin", "local-admin-test-only")
OUT = ROOT / "e2e/.gitops.local"
SEED = ROOT / "e2e/gitops/repo"
KUBECTL = ["kubectl", "--kubeconfig", str(ROOT / "e2e/.kube.local/kubeconfig")]
STATUS_CONTEXT = "slack-ops/still-valid"


def call(method, path, body=None, auth=ADMIN, token=None, ok=(200, 201, 204)):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "token " + token
    else:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
    req = urllib.request.Request(GITEA + path, method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if e.code in ok:
            return {}
        raise RuntimeError(f"{method} {path}: {e.code} {e.read(300)!r}") from None


def tf_output(name):
    return subprocess.check_output(["terraform", f"-chdir={ROOT / 'infra/terraform/envs/local'}",
                                    "output", "-raw", name], text=True).strip()


def main():
    OUT.mkdir(mode=0o700, exist_ok=True)
    users = {"slackops": "local-bot-test-only", "ops1": "local-ops1-test-only", "ops2": "local-ops2-test-only"}
    for name, password in users.items():
        call("POST", "/admin/users", {"username": name, "password": password, "email": f"{name}@example.test",
                                      "must_change_password": False}, ok=(201, 422))
    call("POST", "/orgs", {"username": "utterai"}, ok=(201, 422))
    call("POST", "/orgs/utterai/repos", {"name": "deploy", "private": False, "default_branch": "main",
                                         "auto_init": False}, ok=(201, 409))
    for name in users:
        call("PUT", f"/repos/utterai/deploy/collaborators/{name}", {"permission": "write"})

    # CI-style seed commits, pushed by ops-admin.
    for path in sorted(p for p in SEED.rglob("*") if p.is_file()):
        rel = path.relative_to(SEED).as_posix()
        call("POST", f"/repos/utterai/deploy/contents/{rel}",
             {"content": base64.b64encode(path.read_bytes()).decode(), "message": f"ci: add {rel}",
              "branch": "main", "new_branch": None}, ok=(201, 422))

    call("POST", "/repos/utterai/deploy/branch_protections", {
        "rule_name": "main", "enable_push": True, "enable_push_whitelist": True,
        "push_whitelist_usernames": ["ops-admin"],          # CI pushes tags; people go through PRs
        "required_approvals": 1, "enable_approvals_whitelist": False,
        "enable_status_check": True, "status_check_contexts": [STATUS_CONTEXT],
        "block_on_outdated_branch": True, "dismiss_stale_approvals": True}, ok=(201, 403, 422))

    tokens = {}
    for name, password in users.items():
        tokens[name] = call("POST", f"/users/{name}/tokens",
                            {"name": f"lab-{name}", "scopes": ["write:repository", "write:issue", "read:user"]},
                            auth=(name, password))["sha1"]
    secret = tf_output("git_webhook_secret")
    hook_url = tf_output("function_url").rstrip("/") + "/git"
    call("POST", "/repos/utterai/deploy/hooks", {
        "type": "gitea", "active": True, "events": ["push", "pull_request"],
        "config": {"url": hook_url, "content_type": "json", "secret": secret}})

    for name, token in tokens.items():
        (OUT / f"{name}.token").write_text(token + "\n")
        (OUT / f"{name}.token").chmod(0o600)

    # Argo CD core install has no API server to create the default project.
    project = {"apiVersion": "argoproj.io/v1alpha1", "kind": "AppProject",
               "metadata": {"name": "default", "namespace": "argocd"},
               "spec": {"sourceRepos": ["*"], "destinations": [{"namespace": "*", "server": "*"}],
                        "clusterResourceWhitelist": [{"group": "*", "kind": "*"}]}}
    subprocess.run(KUBECTL + ["apply", "-f", "-"], input=json.dumps(project), text=True, check=True, capture_output=True)
    apps = [("batch-worker", "utterai-batch"), ("cpu-worker", "utterai-ai-cpu")]
    for app, namespace in apps:
        manifest = {"apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
                    "metadata": {"name": app, "namespace": "argocd"},
                    "spec": {"project": "default",
                             "source": {"repoURL": "http://slack-ops-gitea:3000/utterai/deploy.git",
                                        "targetRevision": "main", "path": f"apps/{app}"},
                             "destination": {"server": "https://kubernetes.default.svc", "namespace": namespace},
                             "syncPolicy": {"automated": {"prune": True, "selfHeal": True},
                                            "syncOptions": ["CreateNamespace=true"]}}}
        subprocess.run(KUBECTL + ["apply", "-f", "-"], input=json.dumps(manifest), text=True, check=True,
                       capture_output=True)
    print(json.dumps({"repo": "http://127.0.0.1:3300/utterai/deploy", "webhook": hook_url,
                      "apps": [a for a, _ in apps], "credentials": str(OUT)}, indent=2))


if __name__ == "__main__":
    main()
