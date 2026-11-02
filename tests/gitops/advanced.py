#!/usr/bin/env python3
"""Reproduce Parent health and cross-Application wave gates on the dedicated kind lab."""
import argparse
from pathlib import Path
import time

from lab import HEALTH_KEY, Lab, wait


def customization(lab, script):
    lab.patch('configmap', 'argocd-cm', {'data': {HEALTH_KEY: script}})


def pinned_root(lab, name, child_apps):
    lab.write(f'roots/{name}/children.yaml', child_apps)
    revision = lab.commit(f'{name}: child membership')
    root = lab.app(name, f'roots/{name}', 'argocd', revision=revision)
    lab.apply(root)
    return root, revision


def stop_operation(lab, name):
    app = lab.get('application', name)
    if app.get('operation'):
        lab.patch('application', name, {'status': {'operationState': {'phase': 'Terminating'}}})
        wait(lambda: not lab.get('application', name).get('operation'), description='terminate blocked Root operation')


def health_case(lab, script):
    suffix = lab.repo_name[-8:]
    ns = f'gitops-health-{suffix}'
    lab.namespace(ns)
    name, root_name = f'health-worker-{suffix}', f'health-root-{suffix}'
    deployment = lab.deployment('worker', ns, bad=True)
    lab.write(f'workloads/{name}/resources.yaml', [deployment])
    bad = lab.commit('health: invalid image')
    child = lab.app(name, f'workloads/{name}', ns, revision=bad, automated=True)
    child['metadata']['labels']['gitops.utterai.io/propagate-health'] = 'true'
    customization(lab, None)
    root, revision = pinned_root(lab, root_name, [child])
    lab.sync(root_name, revision)
    lab.health(name, 'Degraded')
    lab.refresh(root_name)
    lab.health(root_name, 'Healthy')
    lab.record('parent-health-default', parent=lab.snapshot(root_name), child=lab.snapshot(name),
               passed=True, conclusion='Parent Healthy while Child Synced/Degraded')
    customization(lab, script)
    lab.refresh(root_name)
    lab.health(root_name, 'Degraded')
    lab.record('parent-health-opt-in', parent=lab.snapshot(root_name), child=lab.snapshot(name), passed=True)
    deployment['spec']['template']['spec']['containers'][0]['image'] = 'nginx:alpine'
    lab.write(f'workloads/{name}/resources.yaml', [deployment])
    fixed = lab.commit('health: restore valid image')
    child['spec']['source']['targetRevision'] = fixed
    lab.write(f'roots/{root_name}/children.yaml', [child])
    revision = lab.commit('health: point Child at fixed revision')
    root['spec']['source']['targetRevision'] = revision
    lab.apply(root)
    lab.sync(root_name, revision)
    lab.health(name, 'Healthy')
    lab.refresh(root_name)
    lab.health(root_name, 'Healthy')
    lab.record('parent-health-recovery', parent=lab.snapshot(root_name), child=lab.snapshot(name), passed=True)


def wave_case(lab, script, mode):
    suffix = lab.repo_name[-8:]
    ns = f'gitops-wave-{mode}-{suffix}'
    lab.namespace(ns)
    addon_name, dependent_name = f'addon-{mode}-{suffix}', f'dependent-{mode}-{suffix}'
    root_name = f'wave-{mode}-{suffix}'
    addon = lab.deployment('addon', ns, bad=True)
    dependent = lab.deployment('dependent', ns)
    lab.write(f'workloads/{addon_name}/resources.yaml', [addon])
    lab.write(f'workloads/{dependent_name}/resources.yaml', [dependent])
    revision = lab.commit(f'waves {mode}: seed workloads')
    waves = mode != 'parallel'
    first = lab.app(addon_name, f'workloads/{addon_name}', ns, revision=revision, automated=True, wave=0 if waves else None)
    second = lab.app(dependent_name, f'workloads/{dependent_name}', ns, revision=revision, automated=True, wave=1 if waves else None)
    for child in (first, second):
        child['metadata']['labels']['gitops.utterai.io/propagate-health'] = 'true'
    customization(lab, None if mode == 'plain' else script)
    root, revision = pinned_root(lab, root_name, [first, second])
    lab.start_sync(root_name, revision)
    lab.health(addon_name, 'Degraded')
    if mode == 'gated':
        start = time.monotonic()
        while time.monotonic() - start < 10:
            assert lab.get('application', dependent_name, optional=True) is None, 'dependent unexpectedly created'
            time.sleep(1)
        assert lab.get('application', root_name)['status']['operationState']['phase'] == 'Running'
        lab.record('waves-with-health-block', mode=mode, observedSeconds=round(time.monotonic()-start, 2),
                   dependentCreated=False, parent=lab.snapshot(root_name), first=lab.snapshot(addon_name), passed=True)
    else:
        lab.health(dependent_name, 'Healthy')
        lab.record('waves-without-health' if mode == 'plain' else 'independent-apps-without-waves', mode=mode,
                   dependentCreated=True, parent=lab.snapshot(root_name), first=lab.snapshot(addon_name),
                   second=lab.snapshot(dependent_name), passed=True)
    # The blocked operation is pinned to the old Git commit. Stop it before syncing a fixed revision.
    stop_operation(lab, root_name)
    addon['spec']['template']['spec']['containers'][0]['image'] = 'nginx:alpine'
    lab.write(f'workloads/{addon_name}/resources.yaml', [addon])
    fixed = lab.commit(f'waves {mode}: restore addon')
    first['spec']['source']['targetRevision'] = fixed
    lab.write(f'roots/{root_name}/children.yaml', [first, second])
    revision = lab.commit(f'waves {mode}: recover dependency')
    root['spec']['source']['targetRevision'] = revision
    lab.apply(root)
    lab.sync(root_name, revision)
    lab.health(addon_name, 'Healthy')
    lab.health(dependent_name, 'Healthy')
    lab.refresh(root_name)
    lab.health(root_name, 'Healthy')
    lab.record(f'waves-{mode}-recovery', parent=lab.snapshot(root_name), passed=True,
               actions=['terminate old operation if running', 'commit fix', 'sync new Root revision', 'refresh Parent after Child recovery'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kubeconfig', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--case', choices=['health', 'waves', 'all'], default='all')
    args = parser.parse_args()
    lab = Lab(args.kubeconfig, args.output)
    original_data = lab.get('configmap', 'argocd-cm').get('data', {})
    original = original_data.get(HEALTH_KEY)
    original_ignore = original_data.get('resource.ignoreResourceUpdatesEnabled')
    script = (Path(__file__).parent / 'application-health.lua').read_text()
    try:
        if args.case in ('health', 'all'):
            health_case(lab, script)
        if args.case in ('waves', 'all'):
            # Keep this setting identical in all wave arms, so health customization is the changing factor.
            lab.patch('configmap', 'argocd-cm', {'data': {'resource.ignoreResourceUpdatesEnabled': 'false'}})
            lab.report['waveControl'] = {'resource.ignoreResourceUpdatesEnabled': False}
            for mode in ('plain', 'gated', 'parallel'):
                wave_case(lab, script, mode)
        lab.report['passed'] = True
    finally:
        customization(lab, original)
        lab.patch('configmap', 'argocd-cm', {'data': {'resource.ignoreResourceUpdatesEnabled': original_ignore}})
        lab.report['originalHealthCustomizationRestored'] = True
        lab.save()


if __name__ == '__main__':
    main()
