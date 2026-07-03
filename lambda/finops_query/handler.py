import json
import boto3
from datetime import datetime, timedelta

ce = boto3.client("ce", region_name="us-east-1")  # Cost Explorer is us-east-1 only


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


TOOLS = {
    "get_cost_by_service": get_cost_by_service,
    "get_daily_cost_trend": get_daily_cost_trend,
    "get_cost_forecast": get_cost_forecast,
    "get_cost_by_tag": get_cost_by_tag,
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
