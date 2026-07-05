import importlib.util
import os
import pathlib
import sys
import unittest


AGENT_DIR = pathlib.Path(__file__).parents[2] / "lambda" / "finops_agent"
sys.path.insert(0, str(AGENT_DIR))
os.environ.update({
    "AWS_EC2_METADATA_DISABLED": "true",
    "AWS_DEFAULT_REGION": "ap-northeast-2",
    "FINOPS_QUERY_LAMBDA_ARN": "arn:aws:lambda:ap-northeast-2:123456789012:function:test",
})
SPEC = importlib.util.spec_from_file_location("finops_agent_handler", AGENT_DIR / "handler.py")
agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent)


class SpotSavingsFormatterTest(unittest.TestCase):
    def test_spot_tool_supports_tracking_window_and_safe_grouping(self):
        tool = next(item for item in agent.TOOL_DEFINITIONS if item["name"] == "get_spot_savings")
        properties = tool["input_schema"]["properties"]

        self.assertIn("since_tracking", properties["window"]["description"])
        self.assertEqual(["none", "instance_type", "nodepool"], properties["group_by"]["enum"])
        self.assertIn("window=since_tracking", agent.SYSTEM_PROMPT)

    def test_complete_result_uses_only_structured_values(self):
        response = {"result": {
            "status": "complete",
            "period": {"start": "2026-07-01", "end": "2026-07-06"},
            "spot_actual_usd": 8.41,
            "on_demand_equivalent_usd": 29.74,
            "savings_usd": 21.33,
            "savings_pct": 71.7,
            "spot_node_hours": 92.4,
            "spot_resource_count": 3,
            "data_updated_at": "2026-07-05 12:00:00.000",
            "breakdown": [],
        }}

        text = agent._format_spot_savings(response)

        self.assertIn("$29.74", text)
        self.assertIn("$8.41", text)
        self.assertIn("$21.33", text)
        self.assertIn("71.7%", text)
        self.assertIn("리소스 3개", text)

    def test_unavailable_result_never_renders_zero_savings(self):
        response = {"result": {
            "status": "data_unavailable",
            "period": {"start": "2026-07-01", "end": "2026-07-06"},
            "reason": "CUR data not delivered",
        }}

        text = agent._format_spot_savings(response)

        self.assertIn("아직 준비되지 않았습니다", text)
        self.assertNotIn("$0.00", text)
        self.assertNotIn("0%", text)

    def test_partial_result_does_not_claim_savings(self):
        response = {"result": {
            "status": "partial",
            "period": {"start": "2026-07-01", "end": "2026-07-06"},
            "reason": "baseline missing",
            "missing_baseline_items": 2,
            "savings_usd": 99.99,
        }}

        text = agent._format_spot_savings(response)

        self.assertIn("확정할 수 없습니다", text)
        self.assertNotIn("$99.99", text)


if __name__ == "__main__":
    unittest.main()
