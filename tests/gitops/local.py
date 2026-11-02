#!/usr/bin/env python3
"""App of Apps adoption experiment on dedicated kind-utterai-gitops only.

Requires Argo CD v3.4.3 core, nginx:alpine loaded into the cluster, and Docker.
Creates an local Gitea fixture; never reads AWS or the real application sources.
"""
import argparse
import base64
import urllib.request
import urllib.error
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import time

import yaml

REPO = Path(__file__).resolve().parents[2]
SERVER = 'utterai-gitops-git'
URL = f'git://{SERVER}/fixture.git'


def run(*args, **kw):
    return subprocess.check_output(list(args), text=True, timeout=180, **kw).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kubeconfig', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    kube = ['kubectl', '--kubeconfig', args.kubeconfig, '--context', 'kind-utterai-gitops', '--request-timeout=20s']
    assert run(*kube, 'config', 'current-context') == 'kind-utterai-gitops'
    def k(*cmd, **kw):
        return run(*kube, *cmd, **kw)
    def get(kind, name, ns):
        return json.loads(k('-n', ns, 'get', kind, name, '-o', 'json'))
    def apply(doc):
        k('apply', '-f', '-', input=json.dumps(doc))
    def sync(name):
        operation = {'prune': False}
        if name.endswith('-root'):
            operation['revision'] = run('git', '-C', str(source), 'rev-parse', 'HEAD')
        k('-n', 'argocd', 'patch', 'application', name, '--type=merge', '-p', json.dumps({'operation': {'sync': operation}}))
        end = time.monotonic() + 180
        while time.monotonic() < end:
            a = get('application', name, 'argocd')
            state = a.get('status', {}).get('operationState', {})
            phase = state.get('phase')
            if phase == 'Succeeded' and not a.get('operation') and a.get('status', {}).get('sync', {}).get('status') == 'Synced':
                return a
            if phase in ('Failed', 'Error') and not a.get('operation'):
                raise RuntimeError(f'{name}: {state.get("message")}')
            time.sleep(2)
        raise RuntimeError(f'timeout syncing {name}: {json.dumps(a.get("status", {}))}')
    def render(path):
        return [d for d in yaml.safe_load_all(run('kubectl', 'kustomize', str(path))) if d]
    def write(path, docs):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump_all(docs, sort_keys=False))
    def wait_deploy(name, ns):
        k('-n', ns, 'rollout', 'status', f'deployment/{name}', '--timeout=120s')
    def ids(children, ns):
        result = {}
        for child in children:
            name = child['metadata']['name']
            wait_deploy(name, ns)
            pods = json.loads(k('-n', ns, 'get', 'pods', '-l', f'app={name}', '-o', 'json'))['items']
            result[name] = {'application': get('application', name, 'argocd')['metadata']['uid'],
                            'deployment': get('deployment', name, ns)['metadata']['uid'],
                            'pods': sorted(p['metadata']['uid'] for p in pods)}
        return result

    work = Path(tempfile.mkdtemp(prefix='utterai-apps-', dir='/private/tmp'))
    source, served = work / 'source', work / 'served'
    source.mkdir(); served.mkdir()
    run('docker', 'run', '-d', '--name', SERVER, '--network', 'kind',
        '-p', '127.0.0.1:3311:3000', '-e', 'GITEA__security__INSTALL_LOCK=true',
        '-e', 'GITEA__database__DB_TYPE=sqlite3', '-e', 'GITEA__service__DISABLE_REGISTRATION=true',
        'gitea/gitea:1.22.6')
    deadline = time.monotonic() + 60
    while True:
        try:
            urllib.request.urlopen('http://127.0.0.1:3311/api/healthz', timeout=2).close()
            break
        except (OSError, urllib.error.URLError):
            if time.monotonic() > deadline:
                raise RuntimeError('local Gitea did not start')
            time.sleep(1)
    run('docker','exec','-u','git',SERVER,'gitea','admin','user','create','--admin','--username','lab',
        '--password','local-fixture-only','--email','lab@example.test','--must-change-password=false')
    repo_name = 'fixture-' + work.name
    auth = 'Basic ' + base64.b64encode(b'lab:local-fixture-only').decode()
    request = urllib.request.Request('http://127.0.0.1:3311/api/v1/user/repos', method='POST',
        headers={'Authorization':auth,'Content-Type':'application/json'},
        data=json.dumps({'name':repo_name,'private':False,'default_branch':'main'}).encode())
    urllib.request.urlopen(request,timeout=10).close()
    global URL
    server_ip = run('docker','inspect','--format','{{(index .NetworkSettings.Networks "kind").IPAddress}}',SERVER)
    URL = f'http://{server_ip}:3000/lab/{repo_name}.git'
    run('git', 'init', '-b', 'main', str(source))
    for env in ('dev', 'prod'):
        ns = f'gitops-{env}'
        apply({'apiVersion':'v1','kind':'Namespace','metadata':{'name':ns}})
        children = render(REPO / f'deploy/argocd/{env}')
        for c in children:
            name = c['metadata']['name']
            c['spec']['source'] = {'repoURL': URL, 'targetRevision': 'main', 'path': f'workloads/{env}/{name}'}
            c['spec']['destination']['namespace'] = ns
            write(source / f'workloads/{env}/{name}/resources.yaml', [
                {'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':name,'namespace':ns},'data':{'version':'baseline'}},
                {'apiVersion':'apps/v1','kind':'Deployment','metadata':{'name':name,'namespace':ns},'spec':{
                    'replicas':1,'selector':{'matchLabels':{'app':name}},'template':{'metadata':{'labels':{'app':name}},
                    'spec':{'containers':[{'name':'web','image':'nginx:alpine','imagePullPolicy':'IfNotPresent'}]}}}}
            ])
        write(source / f'deploy/argocd/{env}/children.yaml', children)
        write(source / f'deploy/argocd/{env}/kustomization.yaml', [{'apiVersion':'kustomize.config.k8s.io/v1beta1','kind':'Kustomization','resources':['children.yaml']}])
    def commit(message):
        run('git', '-C', str(source), 'add', '.')
        run('git', '-C', str(source), '-c', 'user.name=GitOps Lab', '-c', 'user.email=lab@example.test', 'commit', '-m', message)
        run('git','-C',str(source),'-c',f'http.extraHeader=Authorization: {auth}',
            'push',f'http://127.0.0.1:3311/lab/{repo_name}.git','main',stderr=subprocess.PIPE)
        return run('git', '-C', str(source), 'rev-parse', 'HEAD')
    initial_revision = commit('seed isolated workloads and child Applications')
    report = {'scope':'local kind, mock workloads; not EKS', 'argocd':'v3.4.3',
              'timestamp':datetime.now(timezone.utc).isoformat(),
              'kubernetes':json.loads(k('version','-o','json'))['serverVersion']['gitVersion'],
              'sourceHashes':{str(p.relative_to(REPO)):hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in sorted(list((REPO/'deploy/argocd').rglob('*.yaml')) + [Path(__file__).resolve(), REPO/'scripts/gitops/bootstrap.py'])}, 'fixtureRevision': initial_revision,
              'fixture':str(work), 'environments':{}}
    try:
        apply({'apiVersion':'argoproj.io/v1alpha1','kind':'AppProject','metadata':{'name':'default','namespace':'argocd'},
               'spec':{'sourceRepos':[URL], 'destinations':[{'server':'*','namespace':'*'}],
                       'clusterResourceWhitelist':[{'group':'*','kind':'*'}]}})
        for env in ('dev', 'prod'):
            print(f'Experiment: {env}', flush=True)
            ns = f'gitops-{env}'
            children = list(yaml.safe_load_all((source / f'deploy/argocd/{env}/children.yaml').read_text()))
            for c in children:
                original = copy.deepcopy(c)
                original['metadata'].pop('annotations', None)
                apply(original)
                sync(c['metadata']['name'])
            before = ids(children, ns)
            bootstrap = render(REPO / f'deploy/argocd/bootstrap/{env}')
            root = next(d for d in bootstrap if d['kind'] == 'Application')
            project = next(d for d in bootstrap if d['kind'] == 'AppProject')
            root['spec']['source']['repoURL'] = URL
            project['spec']['sourceRepos'] = [URL]
            apply(project); apply(root)
            root_name = root['metadata']['name']
            sync(root_name)
            after = ids(children, ns)
            assert before == after, 'adoption replaced an Application/Deployment/Pod'
            for c in children:
                live = get('application', c['metadata']['name'], 'argocd')
                assert live['spec'] == c['spec'], 'child deployment policy changed'
                assert live['metadata']['annotations']['argocd.argoproj.io/sync-options'] == 'Prune=false,Delete=false'
            # Change one Child's source revision to the same baseline tree; siblings must stay unchanged.
            changed = copy.deepcopy(children)
            changed[0]['spec']['source']['targetRevision'] = initial_revision
            write(source / f'deploy/argocd/{env}/children.yaml', changed)
            commit(f'test {env} single child change')
            k('-n','argocd','annotate','application',root_name,'argocd.argoproj.io/refresh=hard','--overwrite')
            sync(root_name)
            for child in changed:
                assert get('application',child['metadata']['name'],'argocd')['spec']==child['spec']
            sync(changed[0]['metadata']['name'])
            assert ids(children,ns)==before, 'single Child revision change replaced resources'
            write(source / f'deploy/argocd/{env}/children.yaml', children)
            commit(f'restore {env} child revision')
            k('-n','argocd','annotate','application',root_name,'argocd.argoproj.io/refresh=hard','--overwrite')
            sync(root_name)
            # Remove one member from Git; even explicitly requesting prune must retain the child.
            omitted = children[-1]
            write(source / f'deploy/argocd/{env}/children.yaml', children[:-1])
            removal_revision = commit(f'test {env} membership removal')
            k('-n','argocd','annotate','application',root_name,'argocd.argoproj.io/refresh=hard','--overwrite')
            k('-n','argocd','patch','application',root_name,'--type=merge','-p',json.dumps({'operation':{'sync':{'prune':True,'revision':removal_revision}}}))
            deadline = time.monotonic()+120
            while time.monotonic()<deadline:
                state=get('application',root_name,'argocd')
                if not state.get('operation') and state.get('status',{}).get('operationState',{}).get('phase')=='Succeeded':
                    break
                time.sleep(2)
            else:
                raise RuntimeError('prune protection operation timed out')
            assert state['status']['operationState']['syncResult']['revision']==removal_revision, 'prune did not use removal commit'
            skipped = state['status']['operationState']['syncResult'].get('resources', [])
            assert any(r.get('name')==omitted['metadata']['name'] and r.get('status')=='PruneSkipped' for r in skipped), 'no protected prune was observed'
            assert ids(children,ns)==before, 'protected child was pruned'
            write(source / f'deploy/argocd/{env}/children.yaml',children)
            commit(f'restore {env} membership')
            k('-n','argocd','annotate','application',root_name,'argocd.argoproj.io/refresh=hard','--overwrite')
            sync(root_name)
            # Explicitly non-cascading Root deletion, then individual re-apply.
            assert not get('application',root_name,'argocd')['metadata'].get('finalizers')
            k('-n','argocd','delete','application',root_name,'--cascade=orphan','--wait=true')
            assert ids(children,ns)==before, 'Root deletion removed a child/workload'
            for c in children:
                name = c['metadata']['name']
                live = get('application', name, 'argocd')
                metadata = {'annotations': {'argocd.argoproj.io/tracking-id': None}}
                if live['metadata'].get('labels', {}).get('app.kubernetes.io/instance') == root_name:
                    metadata['labels'] = {'app.kubernetes.io/instance': None}
                k('-n','argocd','patch','application',name,'--type=merge','-p',json.dumps({'metadata':metadata}))
                original = copy.deepcopy(c)
                original['metadata'].pop('annotations',None)
                apply(original)
            assert ids(children,ns)==before, 'individual management rollback replaced resources'
            apply(root); sync(root_name)
            report['environments'][env]={'adoptionPreservedUIDs':True,'childSpecsPreserved':True,'singleChildChangeIsolated':True,
                                        'explicitPruneProtected':True,'protectedRemovalRevision':removal_revision,'rootDeletionPreservedChildren':True,
                                        'individualReapplyPreservedUIDs':True,'rootRecreated':True,'uids':before}
            print(f'Passed: {env}',flush=True)
        report['passed']=True
    finally:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,indent=2)+'\n')
        # Leave the lab and Git fixture running for review; cleanup commands are in the runbook.
        print(f'Evidence: {args.output}',flush=True)


if __name__ == '__main__':
    main()
