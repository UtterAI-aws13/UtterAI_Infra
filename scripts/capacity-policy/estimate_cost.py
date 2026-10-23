#!/usr/bin/env python3
"""Use measured EC2 instance-hours and explicitly supplied regional prices.

Do not feed Pod-hours into this calculator. Shared-node cost must be allocated
separately. The On-Demand comparison holds total instance-hours constant.
"""

import argparse, json

p = argparse.ArgumentParser()
p.add_argument("--on-demand-hours", type=float, required=True)
p.add_argument("--spot-hours", type=float, required=True)
p.add_argument("--on-demand-price", type=float, required=True)
p.add_argument("--spot-price", type=float, required=True)
p.add_argument("--completed-jobs", type=int, required=True)
a = p.parse_args()
if (
    min(a.on_demand_hours, a.spot_hours, a.on_demand_price, a.spot_price) < 0
    or a.completed_jobs <= 0
):
    p.error("hours/prices must be nonnegative and jobs positive")
mixed = a.on_demand_hours * a.on_demand_price + a.spot_hours * a.spot_price
reference = (a.on_demand_hours + a.spot_hours) * a.on_demand_price
print(
    json.dumps(
        {
            "mixed_compute_cost": mixed,
            "equivalent_on_demand_compute_cost": reference,
            "compute_cost_per_completed_job": mixed / a.completed_jobs,
            "difference_percent": 100 * (reference - mixed) / reference
            if reference
            else None,
            "excluded": "EKS control plane, EBS, networking, logs, tax; heterogeneous instances require per-type aggregation",
        },
        indent=2,
    )
)
