#!/usr/bin/env python3
"""Read-only adoption preflight by default; --apply registers a manual Root, never syncs it."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

import yaml

REPO = Path(__file__).resolve().parents[2]


def run(args, **kwargs):
    return subprocess.check_output(args, text=True, timeout=60, **kwargs)


def render(path):
    return [d for d in yaml.safe_load_all(run(['kubectl', 'kustomize', str(path)])) if d]


def normalize(value):
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [normalize(v) for v in value]
    return value


def check_child(desired, live, root_name):
    name = desired['metadata']['name']
    meta = live['metadata']
    if meta.get('deletionTimestamp') or meta.get('finalizers') or meta.get('ownerReferences'):
        raise ValueError(f'{name}: deletion/finalizer/ownerReference must be reviewed before adoption')
    if live.get('operation') or live.get('status', {}).get('operationState', {}).get('phase') in ('Running', 'Terminating'):
        raise ValueError(f'{name}: an operation is in progress')
    annotations = meta.get('annotations', {})
    tracking = annotations.get('argocd.argoproj.io/tracking-id')
    expected = f'{root_name}:argoproj.io/Application:argocd/{name}'
    instance = meta.get('labels', {}).get('app.kubernetes.io/instance')
    if tracking not in (None, expected) or instance not in (None, root_name):
        raise ValueError(f'{name}: already tracked by another application')
    if normalize(desired['spec']) != normalize(live['spec']):
        raise ValueError(f'{name}: live spec differs from Git; review drift before adoption')


def check_published(environment, children, bootstrap):
    """Refuse to register a Root whose source is absent/different on actual remote main."""
    root = next(d for d in bootstrap if d['kind'] == 'Application')
    source = root['spec']['source']
    revision = source['targetRevision']
    line = run(['git', 'ls-remote', '--exit-code', source['repoURL'], f'refs/heads/{revision}']).strip()
    sha = line.split()[0]
    paths = [REPO / source['path'] / 'kustomization.yaml']
    paths += [REPO / source['path'] / (d['metadata']['name'].removeprefix('utterai-') + '.yaml') for d in children]
    paths += list((REPO / 'deploy/argocd/bootstrap' / environment).glob('*.yaml'))
    for path in paths:
        relative = path.relative_to(REPO).as_posix()
        try:
            content = run(['git', '-C', str(REPO), 'show', f'{sha}:{relative}'], stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            raise ValueError(f'{relative}: remote main unavailable locally or file unpublished; merge then fetch origin main') from None
        if content != path.read_text():
            raise ValueError(f'{relative}: local file differs from published main; publish reviewed changes first')
    return sha


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment', required=True, choices=['dev', 'prod'])
    parser.add_argument('--context', required=True, help='Explicit kubectl context; current context is never used implicitly')
    parser.add_argument('--apply', action='store_true', help='Register project and manual Root after preflight; does not sync')
    parser.add_argument('--snapshot', type=Path, help='Required with --apply; new private JSON snapshot outside repository')
    args = parser.parse_args()
    if args.apply and not args.snapshot:
        parser.error('--apply requires --snapshot')
    if args.snapshot and (args.snapshot.resolve().is_relative_to(REPO) or args.snapshot.exists()):
        parser.error('--snapshot must be a new file outside the repository')
    environment = args.environment
    bootstrap = render(REPO / 'deploy/argocd/bootstrap' / environment)
    children = render(REPO / 'deploy/argocd' / environment)
    root = next(d for d in bootstrap if d['kind'] == 'Application')
    root_name = root['metadata']['name']
    kubectl = ['kubectl', '--context', args.context, '--request-timeout=15s', '-n', 'argocd']
    live = json.loads(run(kubectl + ['get', 'applications.argoproj.io', '-o', 'json']))['items']
    by_name = {d['metadata']['name']: d for d in live}
    snapshots = []
    for child in children:
        name = child['metadata']['name']
        if name not in by_name:
            raise ValueError(f'{name}: missing; initial deployment requires separate review, not automatic adoption')
        check_child(child, by_name[name], root_name)
        item = by_name[name]
        snapshots.append({'apiVersion': item['apiVersion'], 'kind': item['kind'],
                          'metadata': {k: v for k, v in item['metadata'].items() if k != 'managedFields'}, 'spec': item['spec']})
    if root_name in by_name:
        existing = by_name[root_name]
        if normalize(existing['spec']) != normalize(root['spec']) or existing['metadata'].get('finalizers'):
            raise ValueError(f'{root_name}: existing Root configuration requires review')
    project = next(d for d in bootstrap if d['kind'] == 'AppProject')
    existing_project = json.loads(run(kubectl + ['get', 'appprojects.argoproj.io', project['metadata']['name'],
                                                '--ignore-not-found', '-o', 'json']) or '{}')
    if existing_project and normalize(existing_project['spec']) != normalize(project['spec']):
        raise ValueError('Existing bootstrap AppProject differs; refusing to overwrite')
    print(json.dumps({'context': args.context, 'environment': environment, 'root': root_name,
                      'children': [{'name': d['metadata']['name'], 'uid': by_name[d['metadata']['name']]['metadata']['uid']}
                                   for d in children], 'mode': 'register' if args.apply else 'read-only preflight'}, indent=2))
    if not args.apply:
        print('Preflight passed. No cluster resources changed; --apply also verifies published main.')
        return
    sha = check_published(environment, children, bootstrap)
    with open(args.snapshot, 'x', opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
        json.dump({'context': args.context, 'environment': environment, 'sourceRevision': sha, 'children': snapshots}, stream, indent=2)
    for document in sorted(bootstrap, key=lambda d: d['kind'] != 'AppProject'):
        print(run(kubectl + ['apply', '-f', '-'], input=json.dumps(document)).strip())
    print(f'Root registered without sync. Review `argocd app diff {root_name}` before manual sync; do not prune.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, subprocess.SubprocessError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        sys.exit(1)
