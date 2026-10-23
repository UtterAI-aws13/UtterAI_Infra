import pathlib, subprocess, unittest, yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]


class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.items = list(
            yaml.safe_load_all(
                subprocess.check_output(
                    [
                        "kubectl",
                        "kustomize",
                        str(ROOT / "k8s/experiments/capacity-lab"),
                    ],
                    text=True,
                )
            )
        )

    def find(self, kind, name):
        return next(
            o for o in self.items if o["kind"] == kind and o["metadata"]["name"] == name
        )

    def test_failopen_templates(self):
        deps = [
            o
            for o in self.items
            if o["kind"] == "Deployment"
            and o["spec"]["template"]["metadata"]
            .get("labels", {})
            .get("capacity.utterai.io/enabled")
            == "true"
        ]
        self.assertEqual(len(deps), 5)
        for o in deps:
            self.assertEqual(
                o["spec"]["template"]["spec"]["nodeSelector"][
                    "karpenter.sh/capacity-type"
                ],
                "on-demand",
            )
            self.assertEqual(
                o["spec"]["template"]["spec"]["nodeSelector"][
                    "capacity.utterai.io/node-tier"
                ],
                "base",
            )

    def test_nodepools(self):
        for name in ["api", "cpu-worker", "gpu"]:
            for tier in ["base", "burst"]:
                o = self.find("NodePool", name if tier == "base" else name + "-burst")
                req = next(
                    r
                    for r in o["spec"]["template"]["spec"]["requirements"]
                    if r["key"] == "karpenter.sh/capacity-type"
                )
                self.assertEqual(
                    req["values"],
                    ["on-demand"] if tier == "base" else ["spot", "on-demand"],
                )
        batch = self.find("NodePool", "batch-worker")
        self.assertEqual(
            next(
                r
                for r in batch["spec"]["template"]["spec"]["requirements"]
                if r["key"] == "karpenter.sh/capacity-type"
            )["values"],
            ["spot"],
        )

    def test_schedule_and_inflight(self):
        s = self.find("ScaledObject", "utterai-ml-gpu-worker-scaledobject")["spec"]
        self.assertEqual(s["minReplicaCount"], 0)
        self.assertEqual(s["triggers"][0]["metadata"]["scaleOnInFlight"], "true")
        self.assertEqual(
            s["triggers"][1]["metadata"],
            {
                "timezone": "Asia/Seoul",
                "start": "30 8 * * 1-5",
                "end": "0 20 * * 1-5",
                "desiredReplicas": "1",
            },
        )

    def test_ai_service_separation(self):
        s = self.find("Deployment", "utterai-ai-service")["spec"]["template"]["spec"]
        self.assertEqual(s["nodeSelector"]["workload"], "api")
        self.assertEqual(
            [t["value"] for t in s["tolerations"] if t["key"] == "dedicated"], ["api"]
        )

    def test_all_workers_have_model_readiness_and_drain(self):
        for name in [
            "utterai-cpu-worker",
            "utterai-ml-gpu-worker",
            "utterai-batch-worker",
        ]:
            c = self.find("Deployment", name)["spec"]["template"]["spec"]["containers"][
                0
            ]
            self.assertEqual(c["startupProbe"]["httpGet"]["path"], "/readyz")
            self.assertEqual(
                c["lifecycle"]["preStop"]["exec"]["command"],
                ["python", "-m", "app.workers.lifecycle"],
            )

    def test_controller_excludes_itself(self):
        self.assertNotIn(
            "capacity.utterai.io/enabled",
            self.find("Deployment", "capacity-policy")["spec"]["template"]["metadata"][
                "labels"
            ],
        )


if __name__ == "__main__":
    unittest.main()
