"""Issue one gateway key per service. Each key carries its own allowed models, request
limit and (test-priced) budget, so one service cannot use another's model or capacity.

Keys are local test credentials written to gateway/.keys.local.json (gitignored).
"""
import json
import urllib.error
import urllib.request
from pathlib import Path

GATEWAY = "http://127.0.0.1:4000"
MASTER = "sk-local-master-test-only"
OUT = Path(__file__).with_name(".keys.local.json")

SERVICES = {
    # Slack bot: cheap, fast answers only.
    "slack-bot": {"models": ["fast"], "rpm_limit": 120, "max_budget": 1.0},
    # Nightly digest job: may use the larger model, but with a smaller share.
    "batch": {"models": ["quality"], "rpm_limit": 20, "max_budget": 0.5},
}


def call(path, payload, key=MASTER):
    req = urllib.request.Request(GATEWAY + path, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def issue(alias, spec):
    try:
        call("/key/delete", {"key_aliases": [alias]})
    except urllib.error.HTTPError:
        pass  # first run: nothing to delete
    return call("/key/generate", {"key_alias": alias, "metadata": {"service": alias}, **spec})["key"]


def main():
    keys = {alias: issue(alias, spec) for alias, spec in SERVICES.items()}
    OUT.write_text(json.dumps(keys, indent=2) + "\n")
    OUT.chmod(0o600)
    print(json.dumps({alias: SERVICES[alias] for alias in keys}, indent=2))


if __name__ == "__main__":
    main()
