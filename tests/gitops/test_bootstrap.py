import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('bootstrap', REPO / 'scripts/gitops/bootstrap.py')
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


class Manifests(unittest.TestCase):
    def test_both_environments_preserve_child_specs_and_membership(self):
        for env in ('dev', 'prod'):
            children = b.render(REPO / 'deploy/argocd' / env)
            self.assertEqual({f'utterai-{name}-{env}' for name in ('platform', 'backend', 'ai-service', 'ai-worker')},
                             {d['metadata']['name'] for d in children})
            for child in children:
                name = child['metadata']['name'].removeprefix('utterai-')
                original = yaml.safe_load((REPO / f'deploy/argocd/{env}/{name}.yaml').read_text())
                self.assertEqual(original['spec'], child['spec'])
                self.assertFalse(child['metadata'].get('finalizers'))
                self.assertEqual(child['metadata']['annotations']['argocd.argoproj.io/sync-options'], 'Prune=false,Delete=false')

    def test_bootstrap_is_separate_manual_and_limited_to_applications(self):
        for env in ('dev', 'prod'):
            docs = b.render(REPO / 'deploy/argocd/bootstrap' / env)
            self.assertEqual(len(docs), 2)
            root = next(d for d in docs if d['kind'] == 'Application')
            project = next(d for d in docs if d['kind'] == 'AppProject')
            self.assertEqual(root['spec']['source']['path'], f'deploy/argocd/{env}')
            self.assertEqual(root['spec']['source']['targetRevision'], 'main')
            self.assertNotIn('automated', root['spec']['syncPolicy'])
            self.assertFalse(root['metadata'].get('finalizers'))
            self.assertIn('FailOnSharedResource=true', root['spec']['syncPolicy']['syncOptions'])
            self.assertEqual(project['spec']['namespaceResourceWhitelist'], [{'group': 'argoproj.io', 'kind': 'Application'}])
            self.assertEqual(project['spec']['clusterResourceBlacklist'], [{'group': '*', 'kind': '*'}])


class Adoption(unittest.TestCase):
    def setUp(self):
        self.desired = yaml.safe_load((REPO / 'deploy/argocd/dev/backend-dev.yaml').read_text())
        self.live = copy.deepcopy(self.desired)
        self.live['metadata']['uid'] = 'preserved-uid'

    def check(self):
        b.check_child(self.desired, self.live, 'utterai-dev-root')

    def test_unchanged_child_can_be_adopted(self):
        self.live['status'] = {'health': {'status': 'Degraded'}}
        self.check()  # Degraded health alone must not force a different deployment policy.

    def test_spec_drift_is_rejected(self):
        self.live['spec']['source']['targetRevision'] = 'unexpected-branch'
        with self.assertRaisesRegex(ValueError, 'live spec differs'):
            self.check()

    def test_finalizers_owners_and_deletion_are_rejected(self):
        for key, value in [('finalizers', ['resources-finalizer.argocd.argoproj.io']),
                           ('ownerReferences', [{'name': 'another-controller'}]), ('deletionTimestamp', 'now')]:
            with self.subTest(key=key):
                self.live = copy.deepcopy(self.desired)
                self.live['metadata'][key] = value
                with self.assertRaises(ValueError):
                    self.check()

    def test_empty_automated_policy_is_not_treated_as_manual(self):
        self.desired['spec']['syncPolicy'].pop('automated')
        self.live = copy.deepcopy(self.desired)
        self.live['spec']['syncPolicy']['automated'] = {}
        with self.assertRaisesRegex(ValueError, 'live spec differs'):
            self.check()

    def test_existing_other_tracking_is_rejected(self):
        self.live['metadata']['annotations'] = {'argocd.argoproj.io/tracking-id': 'other:argoproj.io/Application:argocd/child'}
        with self.assertRaisesRegex(ValueError, 'another application'):
            self.check()

    def test_matching_root_tracking_is_idempotent(self):
        self.live['metadata']['annotations'] = {'argocd.argoproj.io/tracking-id': 'utterai-dev-root:argoproj.io/Application:argocd/utterai-backend-dev'}
        self.check()

    def test_inflight_sync_is_rejected(self):
        self.live['operation'] = {'sync': {}}
        with self.assertRaisesRegex(ValueError, 'operation is in progress'):
            self.check()

    def test_explicit_context_required(self):
        with patch('sys.argv', ['bootstrap', '--environment', 'dev']):
            with self.assertRaises(SystemExit):
                b.main()

    def test_missing_child_does_not_write_to_cluster(self):
        children = [self.desired]
        bootstrap = [yaml.safe_load((REPO / 'deploy/argocd/bootstrap/dev/root.yaml').read_text())]
        with patch.object(b, 'render', side_effect=[bootstrap, children]), patch.object(b, 'run', return_value='{"items": []}') as run:
            with patch('sys.argv', ['bootstrap', '--environment', 'dev', '--context', 'explicit-test']):
                with self.assertRaisesRegex(ValueError, 'missing'):
                    b.main()
            for call in run.call_args_list:
                self.assertNotIn('apply', call.args[0])
                self.assertIn('--context', call.args[0])

    def test_unpublished_source_rejected(self):
        bootstrap = b.render(REPO / 'deploy/argocd/bootstrap/dev')
        children = b.render(REPO / 'deploy/argocd/dev')
        error = subprocess.CalledProcessError(128, ['git', 'show'])
        with patch.object(b, 'run', side_effect=['abc123 refs/heads/main', error]):
            with self.assertRaisesRegex(ValueError, 'unpublished'):
                b.check_published('dev', children, bootstrap)

    def test_registration_requires_private_external_snapshot(self):
        for arguments in (['--apply'], ['--snapshot', str(REPO / 'unsafe-backup.json')]):
            with self.subTest(arguments=arguments):
                with patch('sys.argv', ['bootstrap', '--environment', 'dev', '--context', 'explicit-test'] + arguments):
                    with self.assertRaises(SystemExit):
                        b.main()

    def test_registration_writes_private_snapshot_then_project_and_manual_root(self):
        bootstrap = b.render(REPO / 'deploy/argocd/bootstrap/dev')
        def fake_run(args, **kwargs):
            if 'applications.argoproj.io' in args:
                return json.dumps({'items': [self.live]})
            if 'appprojects.argoproj.io' in args:
                return ''
            if 'apply' in args:
                return 'configured'
            raise AssertionError(args)
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / 'snapshot.json'
            with patch.object(b, 'render', side_effect=[bootstrap, [self.desired]]):
                with patch.object(b, 'run', side_effect=fake_run) as run:
                    with patch.object(b, 'check_published', return_value='reviewed-sha'):
                        with patch('sys.argv', ['bootstrap', '--environment', 'dev', '--context', 'explicit-test',
                                                '--apply', '--snapshot', str(snapshot)]):
                            b.main()
            self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(snapshot.read_text())['children'][0]['metadata']['uid'], 'preserved-uid')
            writes = [json.loads(c.kwargs['input']) for c in run.call_args_list if 'apply' in c.args[0]]
            self.assertEqual([d['kind'] for d in writes], ['AppProject', 'Application'])
            self.assertNotIn('operation', writes[1])
            self.assertNotIn('automated', writes[1]['spec']['syncPolicy'])


if __name__ == '__main__':
    unittest.main()
