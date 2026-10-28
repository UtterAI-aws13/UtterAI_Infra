"""Deploy-repository client (Gitea API; GitHub's has the same shapes for what is used here).

The bot never merges and never writes to the deploy branch. It reads history, opens a rollback
pull request from its own branch, and sets a commit status that branch protection requires.
Every write is safe to repeat: an existing branch, an already-applied file change or an already
open pull request is found and reused, so a retry after an unknown outcome cannot open a second PR.
"""
import base64
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

TAG_LINE = re.compile(r"^(\s*newTag:\s*)(\S+)\s*$", re.M)


class ForgeConflict(Exception):
    """The deploy branch moved or the request no longer applies."""


class ForgeUnavailable(Exception):
    """The request never reached the forge."""


def image_tag(kustomization):
    m = TAG_LINE.search(kustomization)
    return m.group(2) if m else None


def with_tag(kustomization, tag):
    return TAG_LINE.sub(lambda m: m.group(1) + tag, kustomization, count=1)


class GiteaForge:
    def __init__(self, base, repo, token_file, opener=None, timeout=10, web_base=""):
        p = urllib.parse.urlsplit(base)
        if p.scheme not in ("http", "https") or not p.hostname or "/" not in repo:
            raise ValueError("forge base URL and owner/repo required")
        self.api = base.rstrip("/") + "/api/v1/repos/" + repo
        self.web = (web_base or base).rstrip("/") + "/" + repo
        self.token_file, self.timeout = token_file, timeout
        self.opener = opener or urllib.request.build_opener()

    def _call(self, method, path, body=None, ok_missing=False):
        with open(self.token_file, encoding="utf-8") as f:
            token = f.read().strip()
        req = urllib.request.Request(self.api + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": "token " + token, "Content-Type": "application/json",
                                              "Accept": "application/json"})
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                raw = r.read(2_000_000)
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 404 and ok_missing:
                return None
            if e.code in (409, 422):
                raise ForgeConflict(e.code) from None
            raise
        except urllib.error.URLError as e:
            if isinstance(e.reason, (ConnectionRefusedError, socket.gaierror)):
                raise ForgeUnavailable() from None
            raise

    # ── reads ───────────────────────────────────────────────────────────────────────────
    def head(self, branch):
        return self._call("GET", f"/branches/{q(branch)}")["commit"]["id"]

    def file(self, path, ref):
        d = self._call("GET", f"/contents/{q(path)}?ref={q(ref)}", ok_missing=True)
        return (base64.b64decode(d["content"]).decode(), d["sha"]) if d else (None, None)

    def last_tag_change(self, path, branch, depth=10):
        """Newest commit on the branch that changed the image tag in `path`, with both tags."""
        commits = self._call("GET", f"/commits?sha={q(branch)}&path={q(path)}&limit={depth}") or []
        for c in commits:
            parents = c.get("parents") or []
            new, _ = self.file(path, c["sha"])
            old, _ = self.file(path, parents[0]["sha"]) if parents else (None, None)
            if new and old and image_tag(new) != image_tag(old):
                return {"sha": c["sha"], "date": c["commit"]["committer"]["date"],
                        "author": c["commit"]["author"]["name"], "message": c["commit"]["message"].split("\n")[0][:120],
                        "old": image_tag(old), "new": image_tag(new)}
        return None

    def pull(self, number):
        return self._call("GET", f"/pulls/{int(number)}")

    def find_pull(self, head_branch):
        for pr in self._call("GET", "/pulls?state=all&limit=50") or []:
            if pr["head"]["ref"] == head_branch:
                return pr
        return None

    # ── writes (each one idempotent) ────────────────────────────────────────────────────
    def ensure_branch(self, branch, from_sha):
        try:
            self._call("POST", "/branches", {"new_branch_name": branch, "old_ref_name": from_sha})
        except ForgeConflict:
            pass  # already created by an earlier attempt

    def ensure_file(self, path, branch, content, message):
        for _ in range(2):
            current, sha = self.file(path, branch)
            if current == content:
                return  # the change is already on the branch (ours, or a concurrent attempt's)
            try:
                self._call("PUT", f"/contents/{q(path)}", {"content": base64.b64encode(content.encode()).decode(),
                                                            "sha": sha, "branch": branch, "message": message})
                return
            except ForgeConflict:
                continue  # someone wrote the branch between our read and write: read again
        raise ForgeConflict("branch keeps changing")

    def ensure_pull(self, head, base, title, body):
        pr = self.find_pull(head)
        if pr:
            return pr
        return self._call("POST", "/pulls", {"head": head, "base": base, "title": title, "body": body})

    def set_status(self, sha, state, context, description, target_url=""):
        self._call("POST", f"/statuses/{q(sha)}", {"state": state, "context": context,
                                                   "description": description[:140], "target_url": target_url})

    def comment(self, number, body):
        self._call("POST", f"/issues/{int(number)}/comments", {"body": body})

    def pull_url(self, number):
        return f"{self.web}/pulls/{int(number)}"


def q(value):
    return urllib.parse.quote(str(value), safe="")
