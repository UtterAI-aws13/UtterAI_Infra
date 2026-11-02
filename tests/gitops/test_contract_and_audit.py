from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent))
from contract_policy import assess_rollback, decode_payload
from manifest_equivalence import compare


class Compatibility(unittest.TestCase):
    def test_old_reader_is_rejected_when_new_messages_can_remain(self):
        self.assertFalse(assess_rollback('v1', {1, 2})['allowed'])
        self.assertTrue(assess_rollback('v1-bridge', {1, 2})['allowed'])

    def test_reader_writer_matrix_and_business_result(self):
        payloads = [{'job_id': 'one', 'schema_version': 1, 'value': 3},
                    {'job_id': 'two', 'schema_version': 2, 'input': {'value': 3}}]
        for reader in ('v2', 'v1-bridge'):
            self.assertEqual([decode_payload(p, reader)[1] for p in payloads], [3, 3])
        self.assertEqual(decode_payload(payloads[0], 'v1'), ('one', 3))
        with self.assertRaises(ValueError):
            decode_payload(payloads[1], 'v1')

    def test_unknown_future_schema_is_not_silently_accepted(self):
        self.assertFalse(assess_rollback('v2', {3})['allowed'])
        with self.assertRaises(ValueError):
            decode_payload({'job_id': 'future', 'schema_version': 3}, 'v2')


class Equivalence(unittest.TestCase):
    first = 'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: settings\n  namespace: demo\ndata:\n  version: v1\n'

    def test_runtime_metadata_does_not_create_false_mismatch(self):
        other = self.first.replace('  name: settings', '  resourceVersion: "123"\n  name: settings')
        self.assertTrue(compare(self.first, other)['equivalent'])

    def test_changed_config_is_detected(self):
        result = compare(self.first, self.first.replace('version: v1', 'version: v2'))
        self.assertFalse(result['equivalent'])
        self.assertNotEqual(result['cachedFingerprint'], result['independentFingerprint'])

    def test_identity_change_is_detected(self):
        self.assertFalse(compare(self.first, self.first.replace('namespace: demo', 'namespace: other'))['equivalent'])

    def test_unsupported_resource_is_not_treated_as_equivalent(self):
        with self.assertRaises(ValueError):
            compare(self.first, self.first.replace('kind: ConfigMap', 'kind: Deployment'))


if __name__ == '__main__':
    unittest.main()
