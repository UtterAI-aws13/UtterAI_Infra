"""Check policy against the actual rendered scaler targets and local Kustomize dependency graph."""
from pathlib import Path
import subprocess
import unittest

import yaml

REPO = Path(__file__).resolve().parents[2]


def render(path):
    raw = subprocess.check_output(['kubectl', 'kustomize', str(path)], text=True)
    return [d for d in yaml.safe_load_all(raw) if d]


def inputs(path, seen=None):
    seen = set() if seen is None else seen
    path = path.resolve()
    if path in seen:
        return seen
    seen.add(path)
    if path.is_dir():
        candidates = [path / name for name in ('kustomization.yaml', 'kustomization.yml', 'Kustomization')]
        config = next(p for p in candidates if p.exists())
        seen.add(config)
        data = yaml.safe_load(config.read_text())
        references = data.get('resources', []) + data.get('bases', []) + data.get('components', [])
        for patch in data.get('patches', []) + data.get('patchesStrategicMerge', []):
            if isinstance(patch, str):
                references.append(patch)
            elif patch.get('path'):
                references.append(patch['path'])
        for patch in data.get('patchesJson6902', []):
            if patch.get('path'):
                references.append(patch['path'])
        for generator in data.get('configMapGenerator', []) + data.get('secretGenerator', []):
            references.extend(generator.get('envs', []))
            references.extend(f.split('=', 1)[-1] for f in generator.get('files', []))
        for reference in references:
            if '://' in reference or reference.startswith('git@'):
                raise AssertionError(f'External input requires separate cache policy: {reference}')
            inputs(path / reference, seen)
    return seen


class Policies(unittest.TestCase):
    def test_replica_ownership_matches_rendered_scalers(self):
        for env in ('dev', 'prod'):
            for component in ('ai-worker', 'ai-service'):
                app = yaml.safe_load((REPO / f'deploy/argocd/{env}/{component}-{env}.yaml').read_text())
                resources = render(REPO / app['spec']['source']['path'])
                targets = {(r['spec']['scaleTargetRef']['name'], r['metadata']['namespace'])
                           for r in resources if r['kind'] in ('ScaledObject', 'HorizontalPodAutoscaler')}
                ignored = {(d.get('name'), d.get('namespace')) for d in app['spec']['ignoreDifferences']
                           if d['group'] == 'apps' and d['kind'] == 'Deployment' and d['jsonPointers'] == ['/spec/replicas']}
                self.assertTrue(targets)
                self.assertEqual(targets, ignored, f'{component}/{env}: replica ignore must match scaler targets exactly')
                self.assertIn('RespectIgnoreDifferences=true', app['spec']['syncPolicy']['syncOptions'])

    def test_manifest_paths_cover_all_local_kustomize_inputs(self):
        applications = []
        for env in ('dev', 'prod'):
            applications.extend(render(REPO / f'deploy/argocd/{env}'))
            applications.append(yaml.safe_load((REPO / f'deploy/argocd/bootstrap/{env}/root.yaml').read_text()))
        for app in applications:
            source = REPO / app['spec']['source']['path']
            annotation = app['metadata']['annotations']['argocd.argoproj.io/manifest-generate-paths']
            paths = [(REPO / p.lstrip('/') if p.startswith('/') else source / p).resolve()
                     for p in annotation.split(';')]
            dependencies = inputs(source)
            for dependency in dependencies:
                if dependency.is_file():
                    self.assertTrue(any(dependency == path or dependency.is_relative_to(path) for path in paths),
                                    f'{app["metadata"]["name"]}: uncovered input {dependency.relative_to(REPO)}')

    def test_prod_service_sync_remains_manual(self):
        for component in ('backend', 'ai-service', 'ai-worker'):
            app = yaml.safe_load((REPO / f'deploy/argocd/prod/{component}-prod.yaml').read_text())
            self.assertNotIn('automated', app['spec']['syncPolicy'])


if __name__ == '__main__':
    unittest.main()
