#!/usr/bin/env python3
"""Collect local evidence. Does not infer AWS pricing or GPU inference latency."""

import datetime, json, math, re, statistics
from local_cluster import kubectl, ROOT, CONTEXT

logs = kubectl(
    "logs",
    "-n",
    "capacity-system",
    "-l",
    "app=capacity-policy",
    "--tail=-1",
    "--prefix=true",
)
times = [float(x) for x in re.findall(r"admission_seconds=([0-9.]+)", logs)]
report = {
    "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "context": CONTEXT,
    "scope": "local API admission only; includes failure/dry-run if present",
    "admission_samples": len(times),
    "admission_p50_ms": round(statistics.median(times) * 1000, 3) if times else None,
    "admission_p95_ms": round(sorted(times)[math.ceil(len(times) * 0.95) - 1] * 1000, 3)
    if times
    else None,
    "aws_resources_created": 0,
    "aws_cost_measured": False,
    "gpu_inference_measured": False,
}
(ROOT / "tests/capacity-policy/results/measurement.json").write_text(
    json.dumps(report, indent=2) + "\n"
)
print(json.dumps(report, indent=2))
