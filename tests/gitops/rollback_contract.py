#!/usr/bin/env python3
"""Git revert succeeds while retained messages can be incompatible with the rolled-back reader."""
import argparse
import json
from pathlib import Path
import time

import boto3
from botocore.config import Config

from contract_policy import assess_rollback
from lab import Lab, run, wait

IMAGE = 'utterai/gitops-contract-reader:lab'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kubeconfig', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    lab = Lab(args.kubeconfig, args.output)
    suffix = lab.repo_name[-8:]
    ns, name = f'gitops-contract-{suffix}', f'contract-{suffix}'
    lab.namespace(ns)
    fixture = f'utterai-contract-sqs-{suffix}'
    run(['docker', 'run', '-d', '--name', fixture, '--network', 'kind', '-p', '127.0.0.1::9324', 'softwaremill/elasticmq:1.6.9'])
    network = json.loads(run(['docker', 'inspect', fixture]))[0]['NetworkSettings']
    endpoint = 'http://127.0.0.1:' + network['Ports']['9324/tcp'][0]['HostPort']
    internal = 'http://' + network['Networks']['kind']['IPAddress'] + ':9324'
    sqs = boto3.client('sqs', endpoint_url=endpoint, region_name='us-east-1', aws_access_key_id='local-only',
                       aws_secret_access_key='local-only', config=Config(connect_timeout=5, read_timeout=20, retries={'max_attempts': 0}))
    def ready():
        try:
            sqs.list_queues()
            return True
        except Exception:
            return False
    wait(ready, timeout=60, description='local SQS readiness')
    names = {key: f'{suffix}-{key}' for key in ('input', 'output', 'dlq')}
    for queue in names.values():
        sqs.create_queue(QueueName=queue)
    host_urls = {key: f'{endpoint}/000000000000/{queue}' for key, queue in names.items()}
    internal_urls = {key: f'{internal}/000000000000/{queue}' for key, queue in names.items()}
    code = {file: (Path(__file__).parent / file).read_text() for file in ('contract_policy.py', 'contract_worker.py')}
    config = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'reader-code', 'namespace': ns}, 'data': code}
    deployment = lab.deployment('worker', ns)
    deployment['spec']['strategy'] = {'type': 'Recreate'}
    deployment['spec']['progressDeadlineSeconds'] = 90
    pod = deployment['spec']['template']['spec']
    pod['volumes'] = [{'name': 'code', 'configMap': {'name': 'reader-code'}}]
    container = {'name': 'worker', 'image': IMAGE, 'imagePullPolicy': 'IfNotPresent',
                 'command': ['python3', '-u', '/lab/contract_worker.py'],
                 'volumeMounts': [{'name': 'code', 'mountPath': '/lab', 'readOnly': True}],
                 'env': [{'name': 'READER_VERSION', 'value': 'v1'}, {'name': 'CONSUME', 'value': 'true'},
                         {'name': 'SQS_ENDPOINT', 'value': internal},
                         {'name': 'INPUT_QUEUE', 'value': internal_urls['input']},
                         {'name': 'OUTPUT_QUEUE', 'value': internal_urls['output']},
                         {'name': 'DLQ_QUEUE', 'value': internal_urls['dlq']}],
                 'readinessProbe': {'exec': {'command': ['python3', '-c', "from pathlib import Path; exit(0 if Path('/tmp/ready').exists() else 1)"]},
                                    'periodSeconds': 2, 'failureThreshold': 3},
                 'resources': {'requests': {'cpu': '50m', 'memory': '64Mi'}, 'limits': {'memory': '256Mi'}}}
    pod['containers'] = [container]
    documents = [config, deployment]
    path = 'workloads/reader'
    lab.report['scope'] = 'kind Argo CD + real local SQS-compatible messages; synthetic integer jobs; no UtterAI application changes'
    lab.report['queueFixture'] = fixture
    results, quarantined = [], []
    def drain(key, target):
        messages = sqs.receive_message(QueueUrl=host_urls[key], MaxNumberOfMessages=10, WaitTimeSeconds=0).get('Messages', [])
        for message in messages:
            target.append(json.loads(message['Body']))
            sqs.delete_message(QueueUrl=host_urls[key], ReceiptHandle=message['ReceiptHandle'])
        return target
    def write_commit(message):
        lab.write(f'{path}/resources.yaml', documents)
        return lab.commit(message)
    try:
        baseline = write_commit('reader v1 baseline')
        app = lab.app(name, path, ns)
        lab.apply(app)
        lab.sync(name, baseline)
        lab.health(name, 'Healthy')
        container['env'][0]['value'] = 'v2'
        container['env'][1]['value'] = 'false'
        upgrade = write_commit('reader v2, paused consumption for retained-message fixture')
        lab.sync(name, upgrade)
        lab.k('-n', ns, 'rollout', 'status', 'deployment/worker', '--timeout=120s')
        cohort = [{'job_id': f'old-{i}', 'schema_version': 1, 'value': i} for i in range(5)]
        cohort += [{'job_id': f'new-{i}', 'schema_version': 2, 'input': {'value': i}} for i in range(5)]
        sqs.send_message_batch(QueueUrl=host_urls['input'], Entries=[{'Id': str(i), 'MessageBody': json.dumps(job)} for i, job in enumerate(cohort)])
        # Revert the actual v2 Git commit; no direct Deployment patch is used for the rollback.
        run(['git', '-C', lab.source, 'revert', '--no-commit', upgrade])
        rollback = lab.commit('revert reader v2 to v1')
        lab.sync(name, rollback)
        lab.k('-n', ns, 'rollout', 'status', 'deployment/worker', '--timeout=120s')
        lab.health(name, 'Healthy')
        wait(lambda: len(drain('output', results)) >= 5 and len(drain('dlq', quarantined)) >= 5,
             timeout=90, description='retained message outcomes')
        assert {r['job_id'] for r in results} == {f'old-{i}' for i in range(5)}
        assert {r['original']['job_id'] for r in quarantined} == {f'new-{i}' for i in range(5)}
        guard = assess_rollback('v1', {1, 2})
        assert not guard['allowed']
        lab.record('git-revert-healthy-but-contract-incompatible', application=lab.snapshot(name),
                   baselineRevision=baseline, upgradeRevision=upgrade, rollbackRevision=rollback,
                   publishedJobs=10, successfulJobs=5, quarantinedJobs=5, outcomes=results, dlq=quarantined,
                   compatibilityGate=guard, passed=True)
        # A rollback-capable compatibility release preserves old business behavior while reading both envelopes.
        container['env'][0]['value'] = 'v1-bridge'
        container['env'][1]['value'] = 'true'
        compatible = write_commit('compatibility reader for v1 behavior and schema v1/v2')
        lab.sync(name, compatible)
        lab.k('-n', ns, 'rollout', 'status', 'deployment/worker', '--timeout=120s')
        for record in quarantined:
            sqs.send_message(QueueUrl=host_urls['input'], MessageBody=json.dumps(record['original']))
        wait(lambda: len(drain('output', results)) >= 10, timeout=60, description='replay retained v2 messages')
        by_id = {r['job_id']: r for r in results}
        assert set(by_id) == {j['job_id'] for j in cohort}
        for job in cohort:
            value = job.get('value', job.get('input', {}).get('value'))
            assert by_id[job['job_id']]['result'] == value * 2
        assert assess_rollback('v1-bridge', {1, 2})['allowed']
        lab.record('compatible-reader-replays-quarantined-jobs', revision=compatible,
                   recoveredJobIDs=sorted(r['original']['job_id'] for r in quarantined),
                   uniqueCompletedJobs=len(by_id), duplicateOutputCount=len(results)-len(by_id), outcomes=results,
                   compatibilityGate=assess_rollback('v1-bridge', {1, 2}), passed=True)
        lab.report['passed'] = True
    finally:
        lab.save()


if __name__ == '__main__':
    main()
