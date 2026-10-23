import io, json, unittest
from unittest.mock import Mock, patch
from test_policy import m


class Tests(unittest.TestCase):
    def run_repair(self, state):
        api = Mock()
        engine = m.Policy(api, now=lambda: 10000)
        dep = {
            "metadata": {
                "name": "worker",
                "namespace": "test",
                "generation": 1,
                "resourceVersion": "42",
            },
            "spec": {
                "replicas": 1,
                "template": {
                    "metadata": {"annotations": {m.PREFIX + "worker": "true"}}
                },
            },
            "status": {
                "observedGeneration": 1,
                "updatedReplicas": 1,
                "availableReplicas": 1,
            },
        }
        pods = [{"status": {"podIP": "127.0.0.1"}}]
        with patch.object(
            m.urllib.request,
            "urlopen",
            return_value=io.BytesIO(json.dumps(state).encode()),
        ):
            engine.repair(dep, pods, 1)
        return api

    def test_busy_worker_does_not_start_policy_rollout(self):
        self.run_repair(
            {"ready": True, "active": 1, "draining": False}
        ).call.assert_not_called()

    def test_draining_worker_does_not_start_policy_rollout(self):
        self.run_repair(
            {"ready": False, "active": 0, "draining": True}
        ).call.assert_not_called()

    def test_idle_repair_uses_cas_without_taking_replica_ownership(self):
        api = self.run_repair({"ready": True, "active": 0, "draining": False})
        api.call.assert_called_once()
        body = api.call.call_args.args[2]
        self.assertEqual(body["metadata"]["resourceVersion"], "42")
        self.assertNotIn("replicas", body["spec"])
        self.assertIn(m.REPAIR, body["spec"]["template"]["metadata"]["annotations"])


if __name__ == "__main__":
    unittest.main()
