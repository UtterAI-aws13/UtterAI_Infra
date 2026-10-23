import contextlib, io, json, pathlib, runpy, sys, unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[2]


class Tests(unittest.TestCase):
    def gate(self, new_ready):
        def pod(owner, ready):
            return {
                "metadata": {
                    "ownerReferences": [{"uid": owner}],
                    "labels": {"capacity.utterai.io/tier": "base"},
                },
                "spec": {"nodeName": "od"},
                "status": {
                    "conditions": [
                        {"type": "Ready", "status": "True" if ready else "False"}
                    ]
                },
            }

        dep = {
            "metadata": {"uid": "dep", "generation": 2},
            "spec": {"replicas": 2},
            "status": {"observedGeneration": 2, "updatedReplicas": 2},
        }
        rs = {
            "items": [
                {
                    "metadata": {
                        "uid": uid,
                        "ownerReferences": [{"uid": "dep"}],
                        "annotations": {"deployment.kubernetes.io/revision": str(rev)},
                    },
                    "spec": {"replicas": 2},
                }
                for uid, rev in [("old", 1), ("new", 2)]
            ]
        }
        nodes = {
            "items": [
                {
                    "metadata": {
                        "name": "od",
                        "labels": {"karpenter.sh/capacity-type": "on-demand"},
                    }
                }
            ]
        }
        pods = {
            "items": [
                pod("old", True),
                pod("old", True),
                pod("new", True),
                pod("new", new_ready),
            ]
        }
        values = iter([dep, rs, nodes, pods])
        argv = [
            "gate",
            "--context",
            "local",
            "--namespace",
            "test",
            "--deployment",
            "demo",
            "--base",
            "2",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch(
                "subprocess.check_output",
                side_effect=lambda *a, **kw: json.dumps(next(values)),
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as e:
                runpy.run_path(
                    str(ROOT / "scripts/capacity-policy/rollout_gate.py"),
                    run_name="__main__",
                )
        return e.exception.code

    def test_old_ready_base_does_not_mask_new_base_shortage(self):
        self.assertEqual(self.gate(False), 1)

    def test_new_ready_base_passes(self):
        self.assertEqual(self.gate(True), 0)


if __name__ == "__main__":
    unittest.main()
