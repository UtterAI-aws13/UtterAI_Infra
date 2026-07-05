import os
import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import boto3


ATHENA_DATABASE = os.environ.get("ATHENA_DATABASE", "")
ATHENA_TABLE = os.environ.get("ATHENA_TABLE", "")
ATHENA_WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "")
SPOT_TRACKING_START_DATE = os.environ.get("SPOT_TRACKING_START_DATE", "2026-07-01")

SEOUL = ZoneInfo("Asia/Seoul")
MAX_RANGE_DAYS = 90
ALLOWED_GROUP_BY = {"none", "instance_type", "nodepool"}
_athena = None
_cloudwatch = None


def _athena_client():
    global _athena
    if _athena is None:
        _athena = boto3.client("athena")
    return _athena


def _emit_metrics(values: dict[str, float]) -> None:
    global _cloudwatch
    try:
        if _cloudwatch is None:
            _cloudwatch = boto3.client("cloudwatch")
        _cloudwatch.put_metric_data(
            Namespace="UtterAI/FinOps",
            MetricData=[{"MetricName": name, "Value": value} for name, value in values.items()],
        )
    except Exception as exc:
        # 비용 조회 자체가 CloudWatch 일시 장애로 실패하면 안 된다.
        print(f"[WARN] failed to publish FinOps metrics: {exc}")


def _parse_date(value: str, field: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must use YYYY-MM-DD") from exc


def _resolve_period(
    window: str = "30d",
    start_date: str | None = None,
    end_date: str | None = None,
) -> tuple[date, date]:
    tomorrow = datetime.now(SEOUL).date() + timedelta(days=1)
    tracking_start = _parse_date(SPOT_TRACKING_START_DATE, "SPOT_TRACKING_START_DATE")

    if start_date or end_date:
        if not (start_date and end_date):
            raise ValueError("start_date and end_date must be provided together")
        start = _parse_date(start_date, "start_date")
        end = _parse_date(end_date, "end_date")
    elif window == "since_tracking":
        start, end = tracking_start, tomorrow
    else:
        match = re.fullmatch(r"(\d{1,2})d", window or "")
        if not match:
            raise ValueError("window must be Nd (maximum 90d) or since_tracking")
        days = int(match.group(1))
        if days < 1 or days > MAX_RANGE_DAYS:
            raise ValueError(f"window must be between 1d and {MAX_RANGE_DAYS}d")
        end = tomorrow
        start = end - timedelta(days=days)

    start = max(start, tracking_start)
    if end > tomorrow:
        raise ValueError("end_date cannot be later than tomorrow in Asia/Seoul")
    if start >= end:
        raise ValueError("start_date must be earlier than end_date")
    if (end - start).days > MAX_RANGE_DAYS:
        raise ValueError(f"date range cannot exceed {MAX_RANGE_DAYS} days")
    return start, end


def _billing_periods(start: date, end: date) -> list[str]:
    periods = []
    cursor = start.replace(day=1)
    last = (end - timedelta(days=1)).replace(day=1)
    while cursor <= last:
        periods.append(cursor.strftime("%Y-%m"))
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    return periods


def _group_expression(group_by: str) -> str | None:
    if group_by == "none":
        return None
    if group_by == "instance_type":
        return (
            "COALESCE(element_at(product, 'instance_type'), "
            "element_at(product, 'instanceType'), line_item_usage_type)"
        )
    if group_by == "nodepool":
        return (
            "COALESCE(element_at(resource_tags, 'karpenter.sh/nodepool'), "
            "element_at(resource_tags, 'karpenter_sh_nodepool'), 'unallocated')"
        )
    raise ValueError(f"group_by must be one of {sorted(ALLOWED_GROUP_BY)}")


def _build_query(start: date, end: date, group_by: str) -> str:
    if not ATHENA_DATABASE or not ATHENA_TABLE:
        raise RuntimeError("Athena Spot savings environment is not configured")

    periods = ", ".join(f"'{period}'" for period in _billing_periods(start, end))
    group_expression = _group_expression(group_by)
    group_select = f"{group_expression} AS group_name," if group_expression else "'all' AS group_name,"
    group_clause = f"GROUP BY {group_expression}" if group_expression else ""

    return f"""
SELECT
  {group_select}
  COUNT(*) AS spot_line_items,
  COUNT(DISTINCT line_item_resource_id) AS spot_resource_count,
  SUM(date_diff('second', line_item_usage_start_date, line_item_usage_end_date)) / 3600.0 AS spot_node_hours,
  SUM(line_item_unblended_cost) AS spot_actual_usd,
  SUM(pricing_public_on_demand_cost) AS on_demand_equivalent_usd,
  SUM(CASE WHEN pricing_public_on_demand_cost IS NULL THEN 1 ELSE 0 END) AS missing_baseline_items,
  MAX(line_item_usage_end_date) AS data_updated_at
FROM \"{ATHENA_DATABASE}\".\"{ATHENA_TABLE}\"
WHERE billing_period IN ({periods})
  AND line_item_usage_start_date >= TIMESTAMP '{start.isoformat()} 00:00:00'
  AND line_item_usage_start_date < TIMESTAMP '{end.isoformat()} 00:00:00'
  AND line_item_line_item_type = 'Usage'
  AND line_item_product_code = 'AmazonEC2'
  AND line_item_usage_type LIKE '%SpotUsage%'
{group_clause}
ORDER BY spot_actual_usd DESC
""".strip()


def _run_query(query: str) -> list[dict[str, str | None]]:
    if not ATHENA_WORKGROUP:
        raise RuntimeError("ATHENA_WORKGROUP is not configured")

    client = _athena_client()
    execution_id = client.start_query_execution(
        QueryString=query,
        QueryExecutionContext={"Database": ATHENA_DATABASE},
        WorkGroup=ATHENA_WORKGROUP,
        ResultReuseConfiguration={
            "ResultReuseByAgeConfiguration": {"Enabled": True, "MaxAgeInMinutes": 60}
        },
    )["QueryExecutionId"]

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        execution = client.get_query_execution(QueryExecutionId=execution_id)["QueryExecution"]
        state = execution["Status"]["State"]
        if state == "SUCCEEDED":
            break
        if state in {"FAILED", "CANCELLED"}:
            reason = execution["Status"].get("StateChangeReason", "unknown error")
            raise RuntimeError(f"Athena query {state.lower()}: {reason}")
        time.sleep(0.5)
    else:
        client.stop_query_execution(QueryExecutionId=execution_id)
        raise TimeoutError("Athena query exceeded 20 seconds")

    paginator = client.get_paginator("get_query_results")
    rows = []
    headers = None
    for page in paginator.paginate(QueryExecutionId=execution_id):
        for row in page["ResultSet"]["Rows"]:
            values = [item.get("VarCharValue") for item in row.get("Data", [])]
            if headers is None:
                headers = values
                continue
            values.extend([None] * (len(headers) - len(values)))
            rows.append(dict(zip(headers, values)))
    return rows


def _number(row: dict, key: str, default: float = 0.0) -> float:
    value = row.get(key)
    return default if value in (None, "") else float(value)


def _format_breakdown(rows: list[dict]) -> list[dict]:
    breakdown = []
    for row in rows:
        actual = _number(row, "spot_actual_usd")
        baseline = _number(row, "on_demand_equivalent_usd")
        savings = max(baseline - actual, 0.0)
        breakdown.append({
            "group": row.get("group_name") or "unallocated",
            "spot_actual_usd": round(actual, 4),
            "on_demand_equivalent_usd": round(baseline, 4),
            "savings_usd": round(savings, 4),
            "savings_pct": round(savings / baseline * 100, 1) if baseline > 0 else None,
        })
    return breakdown


def get_spot_savings(
    window: str = "30d",
    start_date: str | None = None,
    end_date: str | None = None,
    group_by: str = "none",
) -> dict:
    if group_by not in ALLOWED_GROUP_BY:
        raise ValueError(f"group_by must be one of {sorted(ALLOWED_GROUP_BY)}")

    start, end = _resolve_period(window, start_date, end_date)
    try:
        rows = _run_query(_build_query(start, end, group_by))
    except Exception:
        _emit_metrics({"SpotSavingsQueryErrors": 1})
        raise
    period = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "end_exclusive": True,
        "timezone": "Asia/Seoul",
    }

    if not rows or sum(int(_number(row, "spot_line_items")) for row in rows) == 0:
        _emit_metrics({"CURDataAvailable": 0, "SpotSavingsQuerySuccess": 1})
        return {
            "status": "data_unavailable",
            "period": period,
            "reason": "No Spot CUR line items are available for this period yet",
            "calculation_method": "AWS_CUR_PUBLIC_ON_DEMAND",
        }

    actual = sum(_number(row, "spot_actual_usd") for row in rows)
    baseline = sum(_number(row, "on_demand_equivalent_usd") for row in rows)
    missing = sum(int(_number(row, "missing_baseline_items")) for row in rows)
    savings = max(baseline - actual, 0.0)
    updated_values = [row.get("data_updated_at") for row in rows if row.get("data_updated_at")]

    result = {
        "status": "complete" if baseline > 0 and missing == 0 else "partial",
        "period": period,
        "spot_actual_usd": round(actual, 4),
        "on_demand_equivalent_usd": round(baseline, 4),
        "savings_usd": round(savings, 4) if baseline > 0 else None,
        "savings_pct": round(savings / baseline * 100, 1) if baseline > 0 else None,
        "spot_node_hours": round(sum(_number(row, "spot_node_hours") for row in rows), 2),
        "spot_resource_count": sum(int(_number(row, "spot_resource_count")) for row in rows),
        "missing_baseline_items": missing,
        "data_updated_at": max(updated_values) if updated_values else None,
        "calculation_method": "AWS_CUR_PUBLIC_ON_DEMAND",
        "breakdown": _format_breakdown(rows) if group_by != "none" else [],
    }
    if result["status"] == "partial":
        result["reason"] = "Some Spot line items have no public On-Demand baseline"
    total_items = sum(int(_number(row, "spot_line_items")) for row in rows)
    coverage = max(total_items - missing, 0) / max(total_items, 1) * 100
    _emit_metrics({
        "CURDataAvailable": 1,
        "SpotBaselineCoveragePct": coverage,
        "SpotSavingsQuerySuccess": 1,
    })
    return result
