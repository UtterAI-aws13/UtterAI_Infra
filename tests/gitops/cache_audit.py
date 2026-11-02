#!/usr/bin/env python3
"""Read-only audit: compare cache-based synced live ConfigMaps with an independent, exact-revision render."""
import argparse
import json
import time
from pathlib import Path

import yaml

from lab import Lab, wait
from manifest_equivalence import compare, fingerprint
from manifest_cache import builds_since
from datetime import datetime, timezone

PATHS = 'argocd.argoproj.io/manifest-generate-paths'


def independent_render(lab, revision, path):
    # Use the exact same Kustomize binary as repo-server, with a fresh checkout outside its Git/manifest caches.
    script = '''set -eu
checkout=$(mktemp -d /tmp/utterai-audit.XXXXXX)
trap 'rm -rf "$checkout"' EXIT
git clone --quiet --no-checkout "$1" "$checkout/source"
git -C "$checkout/source" checkout --quiet "$2"
kustomize build "$checkout/source/$3"
'''
    return lab.k('-n', 'argocd', 'exec', 'deployment/argocd-repo-server', '-c', 'argocd-repo-server',
                 '--', 'sh', '-c', script, 'independent-audit', lab.url, revision, path)


def synced(lab, name, revision):
    lab.refresh(name)
    def done():
        app = lab.get('application', name)
        status = app.get('status', {})
        return app if status.get('sync', {}).get('revision') == revision and not status.get('conditions') else None
    return wait(done, description=f'{name} compare {revision}')


def audit(lab, name, revision, path, namespace, config_name, label):
    before = lab.get('configmap', config_name, namespace)
    started = time.monotonic()
    fresh = independent_render(lab, revision, path)
    live_yaml = yaml.safe_dump(before)
    result = compare(live_yaml, fresh)
    after = lab.get('configmap', config_name, namespace)
    assert fingerprint(live_yaml) == fingerprint(yaml.safe_dump(after)), 'read-only audit changed live ConfigMap'
    (lab.work / f'{label}-fresh.yaml').write_text(fresh)
    (lab.work / f'{label}-live.yaml').write_text(live_yaml)
    return {**result, 'revision': revision, 'auditSeconds': round(time.monotonic()-started, 4),
            'readOnly': True, 'application': lab.snapshot(name),
            'comparison': 'cache-based synced live ConfigMap vs independent exact-revision render; raw Redis cache is not read'}


def local_dependency_case(lab):
    suffix = lab.repo_name[-8:]
    ns, name, path = f'gitops-audit-local-{suffix}', f'audit-local-{suffix}', 'audit/apps/worker'
    lab.namespace(ns)
    base = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'settings'}, 'data': {'version': 'v1'}}
    lab.write('audit/base/settings.yaml', [base])
    lab.write('audit/base/kustomization.yaml', [{'apiVersion': 'kustomize.config.k8s.io/v1beta1', 'kind': 'Kustomization', 'resources': ['settings.yaml']}])
    lab.write(f'{path}/kustomization.yaml', [{'apiVersion': 'kustomize.config.k8s.io/v1beta1', 'kind': 'Kustomization',
                                             'namespace': ns, 'resources': ['../../base']}])
    revision = lab.commit('audit baseline with shared base')
    app = lab.app(name, path, ns)
    app['metadata']['annotations'] = {PATHS: '.'}  # Intentionally wrong: excludes shared base.
    lab.apply(app)
    lab.sync(name, revision)
    for trial in range(1, 4):
        app['metadata']['annotations'][PATHS] = '.'
        lab.apply(app)
        base['data']['version'] = f'v{trial+1}'
        lab.write('audit/base/settings.yaml', [base])
        since = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        revision = lab.commit(f'base update {trial}: omitted dependency')
        synced(lab, name, revision)
        lab.sync(name, revision)
        assert lab.get('application', name)['status']['sync']['status'] == 'Synced'
        assert lab.get('configmap', 'settings', ns)['data']['version'] != base['data']['version']
        builds = builds_since(lab, since, path)
        assert not builds, 'missing-path condition unexpectedly regenerated manifest'
        result = audit(lab, name, revision, path, ns, 'settings', f'local-{trial}')
        assert not result['equivalent']
        lab.record(f'missing-base-audit-{trial}', **result, actualKustomizeGenerationsBeforeAudit=len(builds),
                   decision='block promotion; review dependency scope', passed=True)
        # Repair scope and force regeneration explicitly. The audit itself never applies.
        app['metadata']['annotations'][PATHS] = '.;../../base'
        lab.apply(app)
        lab.refresh(name, hard=True)
        wait(lambda: lab.get('application', name)['status']['sync']['status'] == 'OutOfSync', description='fresh desired state reveals drift')
        lab.sync(name, revision)
        repaired = audit(lab, name, revision, path, ns, 'settings', f'local-{trial}-repaired')
        assert repaired['equivalent']
        lab.record(f'corrected-base-audit-{trial}', **repaired, passed=True)


def external_dependency_case(lab, kubeconfig):
    remote = Lab(kubeconfig, str(lab.work / 'remote-base.json'))
    base = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'external-settings'}, 'data': {'version': 'remote-v1'}}
    remote.write('base/settings.yaml', [base])
    remote.write('base/kustomization.yaml', [{'apiVersion': 'kustomize.config.k8s.io/v1beta1', 'kind': 'Kustomization', 'resources': ['settings.yaml']}])
    initial_remote = remote.commit('external base v1')
    suffix = lab.repo_name[-8:]
    ns, name, path = f'gitops-audit-remote-{suffix}', f'audit-remote-{suffix}', 'remote-consumer'
    lab.namespace(ns)
    overlay = {'apiVersion': 'kustomize.config.k8s.io/v1beta1', 'kind': 'Kustomization', 'namespace': ns,
               'resources': [f'{remote.url}//base?ref=main']}
    lab.write(f'{path}/kustomization.yaml', [overlay])
    revision = lab.commit('external dependency follows main')
    app = lab.app(name, path, ns)
    app['metadata']['annotations'] = {PATHS: '.'}
    lab.apply(app)
    lab.sync(name, revision)
    assert lab.get('configmap', 'external-settings', ns)['data']['version'] == 'remote-v1'
    base['data']['version'] = 'remote-v2'
    remote.write('base/settings.yaml', [base])
    updated_remote = remote.commit('external base changes; application Git SHA unchanged')
    synced(lab, name, revision)
    lab.sync(name, revision)
    result = audit(lab, name, revision, path, ns, 'external-settings', 'remote-mutable')
    assert not result['equivalent']
    assert lab.get('application', name)['status']['sync']['status'] == 'Synced'
    lab.record('same-application-sha-mutable-remote-base', **result, applicationGitSHAUnchanged=True,
               initialRemoteRevision=initial_remote, changedRemoteRevision=updated_remote, passed=True)
    # Pin external inputs, then mutate the remote branch again. The independent render must remain equivalent.
    overlay['resources'] = [f'{remote.url}//base?ref={updated_remote}']
    lab.write(f'{path}/kustomization.yaml', [overlay])
    pinned = lab.commit('pin remote base to immutable Git revision')
    synced(lab, name, pinned)
    lab.sync(name, pinned)
    base['data']['version'] = 'remote-v3'
    remote.write('base/settings.yaml', [base])
    remote.commit('remote branch advances beyond pinned base')
    synced(lab, name, pinned)
    result = audit(lab, name, pinned, path, ns, 'external-settings', 'remote-pinned')
    assert result['equivalent']
    assert lab.get('configmap', 'external-settings', ns)['data']['version'] == 'remote-v2'
    lab.record('pinned-external-input-remains-equivalent', **result, pinnedRemoteRevision=updated_remote, passed=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kubeconfig', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    lab = Lab(args.kubeconfig, args.output)
    lab.report['scope'] = 'kind Argo CD cache correctness; read-only independent rendering of controlled ConfigMaps'
    lab.report['renderer'] = lab.k('-n', 'argocd', 'exec', 'deployment/argocd-repo-server', '-c', 'argocd-repo-server', '--', 'kustomize', 'version')
    try:
        local_dependency_case(lab)
        external_dependency_case(lab, args.kubeconfig)
        lab.report['passed'] = True
    finally:
        lab.save()


if __name__ == '__main__':
    main()
