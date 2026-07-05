import json
import os
import urllib.request
import boto3
from datetime import datetime, timedelta

from spot_savings import get_spot_savings

ce = boto3.client("ce", region_name="us-east-1")  # Cost Explorer is us-east-1 only
KUBECOST_ENDPOINT = os.environ.get("KUBECOST_ENDPOINT", "").rstrip("/")


# ── AWS Cost Explorer tools ────────────────────────────────────────────────────

def get_cost_by_service(start_date: str, end_date: str) -> dict:
    resp = ce.get_cost_and_usage(
        TimePeriod={"Start": start_date, "End": end_date},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )
    services = []
    for group in resp["ResultsByTime"][0]["Groups"]:
        cost = float(group["Metrics"]["UnblendedCost"]["Amount"])
        if cost >= 0.01:
            services.append({"service": group["Keys"][0], "cost_usd": round(cost, 2)})
    services.sort(key=lambda x: x["cost_usd"], reverse=True)
    return {"period": f"{start_date} ~ {end_date}", "services": services[:15]}


def get_daily_cost_trend(service: str, days: int) -> dict:
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=min(days, 90))).strftime("%Y-%m-%d")
    kwargs = {
        "TimePeriod": {"Start": start, "End": end},
        "Granularity": "DAILY",
        "Metrics": ["UnblendedCost"],
    }
    if service and service.upper() != "ALL":
        kwargs["Filter"] = {"Dimensions": {"Key": "SERVICE", "Values": [service]}}
    resp = ce.get_cost_and_usage(**kwargs)
    daily = [
        {
            "date": day["TimePeriod"]["Start"],
            "cost_usd": round(float(day["Total"]["UnblendedCost"]["Amount"]), 2),
        }
        for day in resp["ResultsByTime"]
    ]
    return {"service": service, "daily": daily}


def get_daily_cost_by_service(start_date: str, end_date: str) -> dict:
    resp = ce.get_cost_and_usage(
        TimePeriod={"Start": start_date, "End": end_date},
        Granularity="DAILY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )
    days = []
    for day in resp["ResultsByTime"]:
        services = []
        for group in day["Groups"]:
            cost = round(float(group["Metrics"]["UnblendedCost"]["Amount"]), 2)
            if cost >= 0.01:
                services.append({"service": group["Keys"][0], "cost_usd": cost})
        services.sort(key=lambda x: x["cost_usd"], reverse=True)
        days.append({"date": day["TimePeriod"]["Start"], "services": services[:10]})
    return {"period": f"{start_date} ~ {end_date}", "daily_breakdown": days}


def get_cost_forecast(days: int) -> dict:
    start = datetime.now().strftime("%Y-%m-%d")
    end = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
    resp = ce.get_cost_forecast(
        TimePeriod={"Start": start, "End": end},
        Metric="UNBLENDED_COST",
        Granularity="MONTHLY",
    )
    total = round(float(resp["Total"]["Amount"]), 2)
    return {
        "period": f"{start} ~ {end}",
        "forecast_usd": total,
        "forecast_krw": round(total * 1400),
    }


def get_cost_by_tag(tag_key: str, tag_value: str, start_date: str, end_date: str) -> dict:
    resp = ce.get_cost_and_usage(
        TimePeriod={"Start": start_date, "End": end_date},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        Filter={"Tags": {"Key": tag_key, "Values": [tag_value]}},
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )
    services = []
    for group in resp["ResultsByTime"][0]["Groups"]:
        cost = float(group["Metrics"]["UnblendedCost"]["Amount"])
        if cost >= 0.01:
            services.append({"service": group["Keys"][0], "cost_usd": round(cost, 2)})
    services.sort(key=lambda x: x["cost_usd"], reverse=True)
    return {
        "tag": f"{tag_key}={tag_value}",
        "period": f"{start_date} ~ {end_date}",
        "services": services,
    }


# ── Kubecost tools ─────────────────────────────────────────────────────────────

def _kubecost_get(path: str) -> dict:
    if not KUBECOST_ENDPOINT:
        raise RuntimeError("KUBECOST_ENDPOINT not configured (Phase 2 setup required)")
    url = f"{KUBECOST_ENDPOINT}{path}"
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


def get_namespace_costs(window: str = "30d") -> dict:
    data = _kubecost_get(f"/model/allocation?window={window}&aggregate=namespace&accumulate=true")
    items = []
    for name, v in (data.get("data") or [{}])[0].items():
        if name == "__idle__":
            continue
        total = v.get("totalCost", 0)
        items.append({
            "namespace": name,
            "total_usd": round(total, 4),
            "cpu_usd": round(v.get("cpuCost", 0), 4),
            "memory_usd": round(v.get("ramCost", 0), 4),
            "gpu_usd": round(v.get("gpuCost", 0), 4),
        })
    items.sort(key=lambda x: x["total_usd"], reverse=True)
    return {"window": window, "namespaces": items[:15]}


def get_workload_costs(namespace: str, window: str = "30d") -> dict:
    path = f"/model/allocation?window={window}&aggregate=deployment&accumulate=true&namespace={namespace}"
    data = _kubecost_get(path)
    items = []
    for name, v in (data.get("data") or [{}])[0].items():
        if name == "__idle__":
            continue
        items.append({
            "workload": name,
            "total_usd": round(v.get("totalCost", 0), 4),
            "cpu_usd": round(v.get("cpuCost", 0), 4),
            "memory_usd": round(v.get("ramCost", 0), 4),
            "gpu_usd": round(v.get("gpuCost", 0), 4),
        })
    items.sort(key=lambda x: x["total_usd"], reverse=True)
    return {"namespace": namespace, "window": window, "workloads": items[:10]}


def get_cluster_cost_summary(window: str = "30d") -> dict:
    data = _kubecost_get(f"/model/allocation?window={window}&aggregate=cluster&accumulate=true")
    cluster = list((data.get("data") or [{}])[0].values())
    if not cluster:
        return {"error": "No cluster data"}
    c = cluster[0]
    return {
        "window": window,
        "total_usd": round(c.get("totalCost", 0), 2),
        "cpu_usd": round(c.get("cpuCost", 0), 2),
        "memory_usd": round(c.get("ramCost", 0), 2),
        "gpu_usd": round(c.get("gpuCost", 0), 2),
        "storage_usd": round(c.get("pvCost", 0), 2),
        "network_usd": round(c.get("networkCost", 0), 2),
    }


# ── Dispatcher ────────────────────────────────────────────────────────────────

TOOLS = {
    "get_cost_by_service": get_cost_by_service,
    "get_daily_cost_trend": get_daily_cost_trend,
    "get_daily_cost_by_service": get_daily_cost_by_service,
    "get_cost_forecast": get_cost_forecast,
    "get_cost_by_tag": get_cost_by_tag,
    "get_namespace_costs": get_namespace_costs,
    "get_workload_costs": get_workload_costs,
    "get_spot_savings": get_spot_savings,
    "get_cluster_cost_summary": get_cluster_cost_summary,
}


def handler(event, context):
    tool = event.get("tool")
    params = event.get("params", {})
    if tool not in TOOLS:
        return {"error": f"Unknown tool: {tool}", "available": list(TOOLS)}
    try:
        return {"result": TOOLS[tool](**params)}
    except Exception as e:
        return {"error": str(e)}
