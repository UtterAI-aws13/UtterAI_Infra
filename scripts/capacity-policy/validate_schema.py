#!/usr/bin/env python3
"""Validate Karpenter manifests against pinned official CRDs in the local lab."""

import json, pathlib, subprocess, tempfile
import yaml
from local_cluster import ROOT, kubectl

version = "v1.3.3"
with tempfile.TemporaryDirectory(prefix="utterai-capacity-schema-") as tmp:
    for name in [
        "karpenter.sh_nodepools.yaml",
        "karpenter.sh_nodeclaims.yaml",
        "karpenter.k8s.aws_ec2nodeclasses.yaml",
    ]:
        path = str(pathlib.Path(tmp) / name)
        subprocess.run(
            [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--location",
                f"https://raw.githubusercontent.com/aws/karpenter-provider-aws/{version}/pkg/apis/crds/{name}",
                "-o",
                path,
            ],
            check=True,
        )
        kubectl("apply", "--server-side", "-f", path)
    objects = list(
        yaml.safe_load_all(
            subprocess.check_output(
                ["kubectl", "kustomize", str(ROOT / "k8s/experiments/capacity-lab")],
                text=True,
            )
        )
    )
    selected = [o for o in objects if o["kind"] in ("NodePool", "EC2NodeClass")]
    output = kubectl(
        "apply", "--dry-run=server", "-f", "-", stdin=yaml.safe_dump_all(selected)
    )
    report = {
        "schema": "Karpenter 1.3.3 official CRDs",
        "mode": "server dry-run on local Kubernetes 1.31.9",
        "resources": output.splitlines(),
        "passed": True,
        "aws_controller_running": False,
    }
    (ROOT / "tests/capacity-policy/results/schema.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    print(output)
