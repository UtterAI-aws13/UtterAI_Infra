import importlib.util, pathlib, unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
s = importlib.util.spec_from_file_location(
    "measure", ROOT / "tests/capacity-policy/repeated_measurement.py"
)
m = importlib.util.module_from_spec(s)
s.loader.exec_module(m)


class Tests(unittest.TestCase):
    def test_p95_uses_nearest_rank(self):
        self.assertEqual(m.percentile(list(range(1, 31)), 0.95), 29)

    def test_empty_distribution_is_not_reported_as_zero(self):
        self.assertIsNone(m.percentile([], 0.95))

    def test_failures_stay_in_denominator(self):
        common = {
            "scenario": "test",
            "min_ready_base": 2,
            "min_ready_on_demand": 2,
            "snapshots": 2,
            "observed_deficit_seconds": 0,
        }
        result = m.summarize(
            [
                dict(common, passed=True, seconds=1),
                dict(common, passed=False, seconds=90),
            ]
        )["test"]
        self.assertEqual(result["trials"], 2)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(
            result["latency_seconds_successful_trials"]["p95_nearest_rank"], 1
        )


if __name__ == "__main__":
    unittest.main()
