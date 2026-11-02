import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location('gitops_lab', Path(__file__).with_name('lab.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class OperationPolicy(unittest.TestCase):
    def test_manual_operation_inherits_application_sync_options_like_api_server(self):
        lab = object.__new__(module.Lab)
        lab.get = Mock(return_value={'spec': {'syncPolicy': {'syncOptions': ['RespectIgnoreDifferences=true']}}})
        lab.patch = Mock()
        lab.start_sync('worker', 'reviewed-revision')
        operation = lab.patch.call_args.args[2]['operation']['sync']
        self.assertEqual(operation['syncOptions'], ['RespectIgnoreDifferences=true'])
        self.assertEqual(operation['revision'], 'reviewed-revision')
        self.assertFalse(operation['prune'])

    def test_manual_operation_does_not_invent_options(self):
        lab = object.__new__(module.Lab)
        lab.get = Mock(return_value={'spec': {}})
        lab.patch = Mock()
        lab.start_sync('worker')
        self.assertNotIn('syncOptions', lab.patch.call_args.args[2]['operation']['sync'])


if __name__ == '__main__':
    unittest.main()
