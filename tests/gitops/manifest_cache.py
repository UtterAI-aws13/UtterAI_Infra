#!/usr/bin/env python3
"""Measure actual Kustomize executions, not just GenerateManifest RPCs, for a synthetic monorepo."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time

from lab import Lab, wait

PATHS = 'argocd.argoproj.io/manifest-generate-paths'


def metric_snapshot(lab):
    raw = lab.k('get', '--raw', '/api/v1/namespaces/argocd/services/argocd-repo-server:8084/proxy/metrics')
    values = {'gitFetch': 0.0, 'gitLsRemote': 0.0, 'processCPUSeconds': 0.0, 'generateManifestRPCs': 0.0}
    for line in raw.splitlines():
        if line.startswith('process_cpu_seconds_total '):
            values['processCPUSeconds'] = float(line.split()[-1])
        if line.startswith('argocd_git_request_total{') and f'repo="{lab.url}"' in line:
            key = 'gitFetch' if 'request_type="fetch"' in line else 'gitLsRemote'
            values[key] += float(line.split()[-1])
        if line.startswith('grpc_server_handled_total{') and 'grpc_method="GenerateManifest"' in line:
            values['generateManifestRPCs'] += float(line.split()[-1])
    return values


def builds_since(lab, timestamp, prefix):
    raw = lab.k('-n', 'argocd', 'logs', 'deployment/argocd-repo-server', '--since-time', timestamp)
    builds = []
    for line in raw.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = row.get('msg', '')
        if message.startswith('kustomize build ') and prefix in message:
            builds.append({'time': row.get('time'), 'command': message, 'execID': row.get('execID')})
    return builds


def experiment(kubeconfig, count, output, trials):
    lab = Lab(kubeconfig, output)
    suffix = lab.repo_name[-8:]
    ns = f'gitops-bench-{count}-{suffix}'
    lab.namespace(ns)
    prefix = f'bench/{suffix}'
    base = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'shared'}, 'data': {'version': 'baseline'}}
    lab.write(f'{prefix}/base/shared.yaml', [base])
    lab.write(f'{prefix}/base/kustomization.yaml', [{'apiVersion': 'kustomize.config.k8s.io/v1beta1',
                                                    'kind': 'Kustomization', 'resources': ['shared.yaml']}])
    overlays, apps = [], []
    for i in range(count):
        overlay = {'apiVersion': 'kustomize.config.k8s.io/v1beta1', 'kind': 'Kustomization', 'namespace': ns,
                   'nameSuffix': f'-{i:02}', 'resources': ['../../base'],
                   'configMapGenerator': [{'name': 'local', 'literals': ['version=baseline']}],
                   'generatorOptions': {'disableNameSuffixHash': True}}
        overlays.append(overlay)
        path = f'{prefix}/apps/app-{i:02}'
        lab.write(f'{path}/kustomization.yaml', [overlay])
        apps.append(lab.app(f'bench-{count}-{i:02}-{suffix}', path, ns))
    initial = lab.commit(f'seed {count} Kustomize applications')
    lab.apply({'apiVersion': 'v1', 'kind': 'List', 'items': apps})

    def all_at(revision, operations=False):
        items = json.loads(lab.k('-n', 'argocd', 'get', 'applications', '-l',
                                f'gitops.utterai.io/experiment={lab.project}', '-o', 'json'))['items']
        if len(items) != count:
            return None
        for item in items:
            status = item.get('status', {})
            if status.get('sync', {}).get('revision') != revision or status.get('conditions'):
                return None
            if operations and (item.get('operation') or status.get('operationState', {}).get('phase') != 'Succeeded'):
                return None
        return items

    def sync_all(revision):
        for app in apps:
            lab.start_sync(app['metadata']['name'], revision)
        wait(lambda: all_at(revision, operations=True), timeout=240, description=f'{count} app sync')

    sync_all(initial)
    lab.report['benchmark'] = {'applications': count, 'trialsPerScenario': trials,
                               'trigger': 'normal refresh after each commit; no webhook; no hard refresh in measured cases',
                               'workload': 'two ConfigMaps per Kustomize app, shared base, no running application Pods',
                               'counters': 'Kustomize exec log count is repository/path-scoped; RPC and process CPU are global diagnostics'}
    current = initial
    try:
        for optimized in (False, True):
            for app in apps:
                app['metadata'].setdefault('annotations', {})[PATHS] = f'.;/{prefix}/base' if optimized else None
                lab.apply(app)
            # Warm caches and establish the previous synced revision under this annotation setting.
            for app in apps:
                lab.refresh(app['metadata']['name'])
            sync_all(current)
            for scenario in ('unrelated', 'leaf', 'shared'):
                for trial in range(trials):
                    marker = f'{optimized}-{scenario}-{trial}'
                    if scenario == 'unrelated':
                        file = lab.source / 'notes/change.txt'
                        file.parent.mkdir(parents=True, exist_ok=True)
                        file.write_text(marker)
                    elif scenario == 'leaf':
                        overlays[0]['configMapGenerator'][0]['literals'] = [f'version={marker}']
                        lab.write(f'{prefix}/apps/app-00/kustomization.yaml', [overlays[0]])
                    else:
                        base['data']['version'] = marker
                        lab.write(f'{prefix}/base/shared.yaml', [base])
                    before = metric_snapshot(lab)
                    since = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
                    started = time.monotonic()
                    revision = lab.commit(marker)
                    pushed = time.monotonic()
                    lab.k('-n', 'argocd', 'annotate', 'applications', *[a['metadata']['name'] for a in apps],
                          'argocd.argoproj.io/refresh=normal', '--overwrite')
                    wait(lambda: all_at(revision), timeout=240, description=f'{scenario} comparison')
                    elapsed = time.monotonic()-started
                    generated = builds_since(lab, since, prefix)
                    after = metric_snapshot(lab)
                    expected = count if not optimized or scenario == 'shared' else (1 if scenario == 'leaf' else 0)
                    assert len(generated) == expected, f'actual builds={len(generated)}, expected={expected}; {optimized}/{scenario}'
                    lab.record(f'manifest-cache-{optimized}-{scenario}-{trial+1}', optimized=optimized, scenario=scenario,
                               trial=trial+1, revision=revision, kustomizeExecutions=len(generated),
                               commitToComparisonSeconds=round(elapsed, 4), pushSeconds=round(pushed-started, 4),
                               metricDeltas={k: round(after[k]-before[k], 6) for k in before}, buildLogs=generated, passed=True)
                    sync_all(revision)
                    current = revision
                    # A correct shared-base path must not hide the desired ConfigMap update.
                    if scenario == 'shared':
                        for i in range(count):
                            assert lab.get('configmap', f'shared-{i:02}', ns)['data']['version'] == marker
        lab.report['passed'] = True
    finally:
        lab.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kubeconfig', required=True)
    parser.add_argument('--applications', type=int, default=4)
    parser.add_argument('--trials', type=int, default=3)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    experiment(args.kubeconfig, args.applications, args.output, args.trials)


if __name__ == '__main__':
    main()
