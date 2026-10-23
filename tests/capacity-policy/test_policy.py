import copy, importlib.util, json, pathlib, threading, unittest
from concurrent.futures import ThreadPoolExecutor

spec = importlib.util.spec_from_file_location(
    "policy",
    pathlib.Path(__file__).resolve().parents[2]
    / "components/capacity-policy/policy.py",
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
OWNER = {
    "apiVersion": "apps/v1",
    "kind": "ReplicaSet",
    "name": "worker-123",
    "uid": "rs123",
    "controller": True,
}


def pod():
    return {
        "metadata": {
            "namespace": "test",
            "name": "pod",
            "labels": {m.MANAGED: "true"},
            "annotations": {m.BASE: "2"},
            "ownerReferences": [OWNER],
        },
        "spec": {
            "nodeSelector": {
                m.CAPACITY: "on-demand",
                m.PREFIX + "node-tier": "base",
                "workload": "test",
            },
            "tolerations": [{"key": "dedicated", "operator": "Exists"}],
            "affinity": {
                "podAntiAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": []
                }
            },
        },
    }


class Fake:
    def __init__(self):
        self.state = None
        self.lock = threading.Lock()
        self.pods = []

    def call(self, method, path, obj=None, content_type=None):
        with self.lock:
            if "/replicasets/" in path:
                return {
                    "metadata": {
                        "uid": "rs123",
                        "ownerReferences": [
                            {"kind": "Deployment", "controller": True, "uid": "dep"}
                        ],
                    },
                    "spec": {"replicas": 20},
                }
            if "/pods?" in path:
                return {"items": copy.deepcopy(self.pods)}
            if method == "GET":
                if self.state is None:
                    raise m.APIError(404)
                return copy.deepcopy(self.state)
            if method == "POST":
                if self.state:
                    raise m.APIError(409)
                self.state = copy.deepcopy(obj)
                self.state["metadata"]["resourceVersion"] = "1"
                return copy.deepcopy(self.state)
            if method == "PUT":
                if (
                    obj["metadata"]["resourceVersion"]
                    != self.state["metadata"]["resourceVersion"]
                ):
                    raise m.APIError(409)
                self.state = copy.deepcopy(obj)
                self.state["metadata"]["resourceVersion"] = str(
                    int(obj["metadata"]["resourceVersion"]) + 1
                )
                return copy.deepcopy(self.state)


class Tests(unittest.TestCase):
    def test_concurrent_reservation_across_replicas(self):
        api = Fake()
        engines = [m.Policy(api), m.Policy(api)]
        with ThreadPoolExecutor(max_workers=12) as ex:
            tiers = list(
                ex.map(lambda i: engines[i % 2].reserve(pod(), str(i)), range(20))
            )
        self.assertEqual(tiers.count("base"), 2)
        self.assertEqual(tiers.count("burst"), 18)

    def test_conflicting_snapshots_across_webhook_replicas(self):
        barrier = threading.Barrier(8)
        local = threading.local()

        class Contended(Fake):
            def call(self, method, path, obj=None, content_type=None):
                if method == "PUT" and not getattr(local, "waited", False):
                    local.waited = True
                    barrier.wait(timeout=3)
                return super().call(method, path, obj, content_type)

        api = Contended()
        engines = [m.Policy(api), m.Policy(api)]
        with ThreadPoolExecutor(max_workers=8) as ex:
            tiers = list(
                ex.map(lambda i: engines[i % 2].reserve(pod(), str(i)), range(8))
            )
        self.assertEqual(tiers.count("base"), 2)
        self.assertEqual(tiers.count("burst"), 6)
        self.assertGreaterEqual(
            sum(e.counters.get("reservation_conflicts_total", 0) for e in engines), 7
        )

    def test_bound_pending_slot_survives_timeout(self):
        p = pod()
        p["metadata"].update(uid="pod-uid")
        p["metadata"]["annotations"][m.RESERVATION] = "request"
        p["status"] = {"phase": "Pending"}
        slots = {"request": {"tier": "base", "at": 0}}
        cleaned = m.clean_slots(slots, [p], 1000)
        self.assertEqual(cleaned["request"]["bound"], "pod-uid")

    def test_bound_deleted_slot_is_released_immediately(self):
        slots = {"request": {"tier": "base", "at": 1000, "bound": "deleted-uid"}}
        self.assertEqual(m.clean_slots(slots, [], 1001), {})

    def test_retry_is_idempotent(self):
        api = Fake()
        engine = m.Policy(api)
        self.assertEqual(engine.reserve(pod(), "x"), engine.reserve(pod(), "x"))
        self.assertEqual(len(json.loads(api.state["data"]["reservations"])), 1)

    def test_dry_run_does_not_write(self):
        api = Fake()
        r = m.Policy(api).admit(
            {
                "object": pod(),
                "uid": "x",
                "operation": "CREATE",
                "namespace": "test",
                "dryRun": True,
            }
        )
        self.assertTrue(r["allowed"])
        self.assertIsNone(api.state)

    def test_expired_orphan_reclaimed(self):
        api = Fake()
        now = [0]
        engine = m.Policy(api, now=lambda: now[0])
        engine.reserve(pod(), "x")
        engine.reserve(pod(), "y")
        now[0] = 121
        self.assertEqual(engine.reserve(pod(), "z"), "base")

    def test_pending_base_not_expired(self):
        api = Fake()
        engine = m.Policy(api, now=lambda: 1000)
        for uid in ["x", "y"]:
            p = pod()
            p["metadata"]["labels"][m.TIER] = "base"
            p["metadata"]["annotations"][m.RESERVATION] = uid
            p["status"] = {"phase": "Pending"}
            api.pods.append(p)
        self.assertEqual(engine.reserve(pod(), "z"), "burst")

    def test_terminating_base_replaced(self):
        api = Fake()
        p = pod()
        p["metadata"]["labels"][m.TIER] = "base"
        p["metadata"]["deletionTimestamp"] = "now"
        api.pods = [p]
        self.assertEqual(m.Policy(api).reserve(pod(), "z"), "base")

    def test_patch_preserves_affinity_and_tolerations(self):
        p = pod()
        before = copy.deepcopy(p)
        patch = m.mutate(p, "burst", "x")
        self.assertEqual(p, before)
        aff = next(x["value"] for x in patch if x["path"] == "/spec/affinity")
        self.assertIn("podAntiAffinity", aff)
        sel = next(x["value"] for x in patch if x["path"] == "/spec/nodeSelector")
        self.assertNotIn(m.CAPACITY, sel)
        self.assertEqual(sel["workload"], "test")
        self.assertEqual(sel[m.PREFIX + "node-tier"], "burst")
        self.assertFalse(any(x["path"] == "/spec/tolerations" for x in patch))

    def test_existing_mutation_is_noop(self):
        p = pod()
        p["metadata"]["annotations"][m.RESERVATION] = "x"
        p["spec"]["nodeSelector"].pop(m.CAPACITY)
        self.assertNotIn(
            "patch",
            m.Policy(Fake()).admit(
                {"object": p, "uid": "x", "operation": "CREATE", "namespace": "test"}
            ),
        )

    def test_invalid_template_detected(self):
        p = pod()
        p["spec"]["nodeSelector"].pop(m.CAPACITY)
        with self.assertRaises(ValueError):
            m.validate_template(p)

    def test_base_patch_does_not_relax_placement(self):
        self.assertFalse(
            any(x["path"].startswith("/spec") for x in m.mutate(pod(), "base", "x"))
        )


if __name__ == "__main__":
    unittest.main()
