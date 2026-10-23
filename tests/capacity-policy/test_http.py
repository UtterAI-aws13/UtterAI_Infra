import json, threading, unittest, urllib.request, urllib.error
from test_policy import m, Fake, pod


class Tests(unittest.TestCase):
    def setUp(self):
        self.policy = m.Policy(Fake())
        self.server = m.serve(self.policy, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_apiserver_timeout_query_and_dry_run(self):
        obj = {
            "request": {
                "object": pod(),
                "uid": "test",
                "operation": "CREATE",
                "namespace": "test",
                "dryRun": True,
            }
        }
        req = urllib.request.Request(
            self.url + "/mutate?timeout=2s",
            data=json.dumps(obj).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as r:
            reply = json.load(r)
        self.assertTrue(reply["response"]["allowed"])
        self.assertIsNone(self.policy.api.state)

    def test_failure_returns_transport_error_for_ignore_policy(self):
        obj = {
            "request": {
                "object": pod(),
                "uid": "test",
                "operation": "CREATE",
                "namespace": "test",
            }
        }
        obj["request"]["object"]["spec"]["nodeSelector"] = {}
        req = urllib.request.Request(
            self.url + "/mutate?timeout=2s", data=json.dumps(obj).encode()
        )
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        self.assertEqual(e.exception.code, 503)

    def test_readiness_requires_reconciliation(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(self.url + "/readyz")
        self.assertEqual(e.exception.code, 503)
        self.policy.last_reconcile = self.policy.now()
        with urllib.request.urlopen(self.url + "/readyz") as r:
            self.assertEqual(r.status, 200)


if __name__ == "__main__":
    unittest.main()
