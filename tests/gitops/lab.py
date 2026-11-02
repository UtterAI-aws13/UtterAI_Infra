"""Shared kind-only lab: explicit kubeconfig, isolated Git repo/project, raw evidence."""
import base64
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.request

import yaml

REPO = Path(__file__).resolve().parents[2]
CONTEXT = 'kind-utterai-gitops'
HEALTH_KEY = 'resource.customizations.health.argoproj.io_Application'


def run(args, **kwargs):
    return subprocess.check_output([str(a) for a in args], text=True, timeout=180, **kwargs).strip()


def wait(check, timeout=150, description='condition', interval=1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = check()
        if result:
            return result
        time.sleep(interval)
    raise TimeoutError(description)


class Lab:
    def __init__(self, kubeconfig, output):
        self.kube = [os.environ.get('GITOPS_KUBECTL', 'kubectl'), '--kubeconfig', str(Path(kubeconfig).resolve()), '--context', CONTEXT, '--request-timeout=20s']
        if self.k('config', 'current-context') != CONTEXT:
            raise ValueError('Only the dedicated kind context is allowed')
        self.output = Path(output)
        self.work = Path(tempfile.mkdtemp(prefix='gitops-advanced-', dir='/private/tmp'))
        self.source = self.work / 'source'
        self.source.mkdir()
        self.repo_name = "gitops-advanced-" + hashlib.sha256(str(self.work).encode()).hexdigest()[:12]
        self.auth = 'Basic ' + base64.b64encode(b'lab:local-fixture-only').decode()
        request = urllib.request.Request('http://127.0.0.1:3311/api/v1/user/repos', method='POST',
            headers={'Authorization': self.auth, 'Content-Type': 'application/json'},
            data=json.dumps({'name': self.repo_name, 'private': False, 'default_branch': 'main'}).encode())
        with urllib.request.urlopen(request, timeout=15):
            pass
        ip = run(['docker', 'inspect', '--format', '{{(index .NetworkSettings.Networks "kind").IPAddress}}', 'utterai-gitops-git'])
        self.url = f'http://{ip}:3000/lab/{self.repo_name}.git'
        self.project = self.repo_name
        run(['git', 'init', '-b', 'main', self.source])
        self.apply({'apiVersion': 'argoproj.io/v1alpha1', 'kind': 'AppProject',
                    'metadata': {'name': self.project, 'namespace': 'argocd'},
                    'spec': {'sourceRepos': [self.url], 'destinations': [{'server': 'https://kubernetes.default.svc', 'namespace': 'gitops-*'},
                           {'server': 'https://kubernetes.default.svc', 'namespace': 'argocd'}],
                           'clusterResourceWhitelist': []}})
        # Explicit type is required by UpdateRevisionForPaths in Argo CD v3.4.3.
        self.apply({'apiVersion':'v1','kind':'Secret',
                    'metadata':{'name':self.repo_name,'namespace':'argocd',
                                'labels':{'argocd.argoproj.io/secret-type':'repository', 'gitops.utterai.io/experiment':self.project}},
                    'stringData':{'type':'git','url':self.url,'project':self.project}})
        self.report = {'scope': 'kind-only; real Argo CD and KEDA/HPA, synthetic workloads/local SQS',
                       'startedAt': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                       'workDirectory': str(self.work), 'repository': self.url, 'cases': [],
                       'sourceHashes': {str(p.relative_to(REPO)): hashlib.sha256(p.read_bytes()).hexdigest()
                                        for p in sorted((REPO / 'tests/gitops').glob('*.py'))}}

    def k(self, *args, **kwargs):
        return run(self.kube + list(args), **kwargs)

    def get(self, kind, name, namespace='argocd', optional=False):
        return json.loads(self.k('-n', namespace, 'get', kind, name, '-o', 'json', *(['--ignore-not-found'] if optional else [])) or 'null')

    def apply(self, document):
        self.k('apply', '-f', '-', input=json.dumps(document))

    def patch(self, kind, name, patch, namespace='argocd'):
        self.k('-n', namespace, 'patch', kind, name, '--type=merge', '-p', json.dumps(patch))

    def namespace(self, name):
        self.apply({'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': name}})

    def write(self, path, docs):
        file = self.source / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(yaml.safe_dump_all(docs, sort_keys=False))

    def commit(self, message):
        run(['git', '-C', self.source, 'add', '.'])
        run(['git', '-C', self.source, '-c', 'user.name=GitOps Lab', '-c', 'user.email=lab@example.test', 'commit', '-m', message])
        run(['git', '-C', self.source, '-c', f'http.extraHeader=Authorization: {self.auth}', 'push',
             f'http://127.0.0.1:3311/lab/{self.repo_name}.git', 'main'], stderr=subprocess.PIPE)
        return run(['git', '-C', self.source, 'rev-parse', 'HEAD'])

    def app(self, name, path, namespace, revision='main', automated=False, wave=None):
        app = {'apiVersion': 'argoproj.io/v1alpha1', 'kind': 'Application',
               'metadata': {'name': name, 'namespace': 'argocd', 'labels': {'gitops.utterai.io/experiment': self.project}},
               'spec': {'project': self.project, 'source': {'repoURL': self.url, 'targetRevision': revision, 'path': path},
                        'destination': {'server': 'https://kubernetes.default.svc', 'namespace': namespace}}}
        if automated:
            app['spec']['syncPolicy'] = {'automated': {'selfHeal': True, 'prune': False}}
        if wave is not None:
            app['metadata']['annotations'] = {'argocd.argoproj.io/sync-wave': str(wave)}
        return app

    def refresh(self, name, hard=False):
        self.k('-n', 'argocd', 'annotate', 'application', name, 'argocd.argoproj.io/refresh=' + ('hard' if hard else 'normal'), '--overwrite')

    def start_sync(self, name, revision=None):
        sync = {'prune': False}
        # Match server/application/application.go: Sync defaults to the Application's syncOptions.
        options = self.get('application', name).get('spec', {}).get('syncPolicy', {}).get('syncOptions', [])
        if options:
            sync['syncOptions'] = list(options)
        if revision:
            sync['revision'] = revision
        self.patch('application', name, {'operation': {'sync': sync}})

    def sync(self, name, revision=None):
        self.start_sync(name, revision)
        def done():
            app = self.get('application', name)
            phase = app.get('status', {}).get('operationState', {}).get('phase')
            if phase in ('Error', 'Failed') and not app.get('operation'):
                raise RuntimeError(f'{name}: {app.get("status", {}).get("operationState", {})}')
            return app if phase == 'Succeeded' and not app.get('operation') else None
        return wait(done, description=f'sync {name}')

    def health(self, name, expected):
        def check():
            app = self.get('application', name, optional=True)
            return app if app and app.get('status', {}).get('health', {}).get('status') == expected else None
        return wait(check, description=f'{name} health={expected}')

    def snapshot(self, name):
        app = self.get('application', name)
        status = app.get('status', {})
        return {'name': name, 'uid': app['metadata']['uid'], 'sync': status.get('sync'), 'health': status.get('health'),
                'operationPhase': status.get('operationState', {}).get('phase'), 'conditions': status.get('conditions', [])}

    def deployment(self, name, namespace, bad=False):
        return {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': {'name': name, 'namespace': namespace},
                'spec': {'replicas': 1, 'progressDeadlineSeconds': 15,
                         'selector': {'matchLabels': {'app': name}},
                         'template': {'metadata': {'labels': {'app': name}},
                                      'spec': {'containers': [{'name': 'worker', 'image': 'nginx:utterai-invalid-20261028' if bad else 'nginx:alpine',
                                                              'imagePullPolicy': 'IfNotPresent'}]}}}}

    def record(self, name, **kwargs):
        self.report['cases'].append({'name': name, **kwargs})
        print(json.dumps({'case': name, **kwargs}), flush=True)
        self.save()

    def save(self):
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_text(json.dumps(self.report, indent=2) + '\n')
