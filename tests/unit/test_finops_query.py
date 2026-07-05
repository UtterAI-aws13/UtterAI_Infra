import importlib
import os
import pathlib
import sys
import unittest
from unittest.mock import patch


QUERY_DIR = pathlib.Path(__file__).parents[2] / "lambda" / "finops_query"
sys.path.insert(0, str(QUERY_DIR))
os.environ.update({
    "ATHENA_DATABASE": "utterai_prod_finops",
    "ATHENA_TABLE": "cur2_spot_costs",
    "ATHENA_WORKGROUP": "utterai-prod-finops",
    "SPOT_TRACKING_START_DATE": "2026-07-01",
})

spot_savings = importlib.import_module("spot_savings")
finops_query = importlib.import_module("handler")


class SpotSavingsQueryTest(unittest.TestCase):
    def test_query_is_partition_pruned_and_spot_only(self):
        start, end = spot_savings._resolve_period(
            start_date="2026-07-01", end_date="2026-07-06"
        )
        query = spot_savings._build_query(start, end, "nodepool")

        self.assertIn("billing_period IN ('2026-07')", query)
        self.assertIn("line_item_usage_type LIKE '%SpotUsage%'", query)
        self.assertIn("karpenter.sh/nodepool", query)
        self.assertIn("pricing_public_on_demand_cost", query)

    def test_rejects_unbounded_or_invalid_ranges(self):
        with self.assertRaisesRegex(ValueError, "between 1d and 90d"):
            spot_savings._resolve_period("91d")
        with self.assertRaisesRegex(ValueError, "provided together"):
            spot_savings._resolve_period(start_date="2026-07-01")
        with self.assertRaisesRegex(ValueError, "group_by"):
            spot_savings.get_spot_savings("7d", group_by="raw_sql")

    def test_since_tracking_uses_configured_cutover_date(self):
        start, end = spot_savings._resolve_period("since_tracking")

        self.assertEqual("2026-07-01", start.isoformat())
        self.assertGreater(end, start)

    def test_calculates_savings_only_from_cur_fields(self):
        rows = [{
            "group_name": "all",
            "spot_line_items": "4",
            "spot_resource_count": "3",
            "spot_node_hours": "92.4",
            "spot_actual_usd": "8.41",
            "on_demand_equivalent_usd": "29.74",
            "missing_baseline_items": "0",
            "data_updated_at": "2026-07-05 12:00:00.000",
        }]

        with patch.object(spot_savings, "_run_query", return_value=rows), patch.object(
            spot_savings, "_emit_metrics"
        ):
            result = spot_savings.get_spot_savings(
                start_date="2026-07-01", end_date="2026-07-06"
            )

        self.assertEqual("complete", result["status"])
        self.assertEqual(8.41, result["spot_actual_usd"])
        self.assertEqual(29.74, result["on_demand_equivalent_usd"])
        self.assertEqual(21.33, result["savings_usd"])
        self.assertEqual(71.7, result["savings_pct"])
        self.assertEqual("AWS_CUR_PUBLIC_ON_DEMAND", result["calculation_method"])
        self.assertNotIn("cluster_total_usd", result)

    def test_missing_cur_data_is_not_converted_to_zero_savings(self):
        with patch.object(spot_savings, "_run_query", return_value=[]), patch.object(
            spot_savings, "_emit_metrics"
        ):
            result = spot_savings.get_spot_savings(
                start_date="2026-07-01", end_date="2026-07-06"
            )

        self.assertEqual("data_unavailable", result["status"])
        self.assertNotIn("savings_usd", result)
        self.assertNotIn("spot_actual_usd", result)


if __name__ == "__main__":
    unittest.main()
