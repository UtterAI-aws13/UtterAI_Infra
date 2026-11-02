#!/usr/bin/env python3
"""Observe real Deployment watch events during Argo sync with actual KEDA/HPA + ElasticMQ."""
import argparse
import copy
import json
from pathlib import Path
import subprocess
import threading
import time
import urllib.request

import boto3
from botocore.config import Config

from lab import Lab, run, wait


class DeploymentWatch:
    def __init__(self, lab, namespace):
        self.rows = []
        self.started = time.monotonic()
        self.proc = subprocess.Popen([v for v in lab.kube if not v.startswith('--request-timeout=')] + ['-n', namespace, 'get', 'deployment', 'worker', '--watch', '-o', 'json'],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.thread = threading.Thread(target=self.read, daemon=True)
        self.thread.start()
        wait(lambda: self.rows, timeout=15, description='Deployment watch first event')

    def read(self):
        buffer = ''
        decoder = json.JSONDecoder()
        while chunk := self.proc.stdout.read1(65536):
            buffer += chunk.decode()
            while buffer.strip():
                buffer = buffer.lstrip()
                try:
                    event, end = decoder.raw_decode(buffer)
                except json.JSONDecodeError:
                    break
                buffer = buffer[end:]
                self.rows.append({'seconds': round(time.monotonic() - self.started, 4),
                                  'resourceVersion': event['metadata']['resourceVersion'],
                                  'generation': event['metadata']['generation'],
                                  'replicas': event['spec'].get('replicas'),
                                  'image': event['spec']['template']['spec']['containers'][0]['image'],
                                  'readyReplicas': event.get('status', {}).get('readyReplicas', 0)})

    def close(self):
        self.proc.terminate()
        self.proc.wait(timeout=10)
        self.thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kubeconfig', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--trials', type=int, default=3)
    args = parser.parse_args()
    lab = Lab(args.kubeconfig, args.output)
    suffix = lab.repo_name[-8:]
    container = f'utterai-gitops-sqs-{suffix}'
    run(['docker','run','-d','--name',container,'--network','kind','-p','127.0.0.1::9324','softwaremill/elasticmq:1.6.9'])
    network = json.loads(run(['docker','inspect',container]))[0]['NetworkSettings']
    port = network['Ports']['9324/tcp'][0]['HostPort']
    endpoint = f'http://127.0.0.1:{port}'
    cluster_endpoint = f'http://{network["Networks"]["kind"]["IPAddress"]}:9324'
    sqs = boto3.client('sqs', region_name='us-east-1', endpoint_url=endpoint,
                      aws_access_key_id='local-only', aws_secret_access_key='local-only',
                      config=Config(connect_timeout=2, read_timeout=3, retries={'max_attempts': 0}))
    def reachable():
        try:
            sqs.list_queues()
            return True
        except Exception:
            return False
    wait(reachable, timeout=90, description='local ElasticMQ SQS API readiness')
    lab.report['queueFixture'] = {'container':container, 'scope':'local Docker, accessed by kind KEDA over kind Docker network'}
    try:
        for mode in ('no-ignore', 'diff-only', 'respect'):
            namespace = f'gitops-keda-{mode}-{suffix}'
            lab.namespace(namespace)
            queue_name = f'{mode}-{suffix}'
            sqs.create_queue(QueueName=queue_name)
            queue_url = f'{endpoint}/000000000000/{queue_name}'
            deployment = lab.deployment('worker', namespace)
            deployment['spec']['progressDeadlineSeconds'] = 60
            secret = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'local-sqs', 'namespace': namespace},
                      'stringData': {'access': 'local-only', 'secret': 'local-only'}}
            auth = {'apiVersion': 'keda.sh/v1alpha1', 'kind': 'TriggerAuthentication',
                    'metadata': {'name': 'local-sqs', 'namespace': namespace},
                    'spec': {'secretTargetRef': [{'parameter': 'awsAccessKeyID', 'name': 'local-sqs', 'key': 'access'},
                                               {'parameter': 'awsSecretAccessKey', 'name': 'local-sqs', 'key': 'secret'}]}}
            scaled = {'apiVersion': 'keda.sh/v1alpha1', 'kind': 'ScaledObject',
                      'metadata': {'name': 'worker', 'namespace': namespace},
                      'spec': {'scaleTargetRef': {'name': 'worker'}, 'minReplicaCount': 1, 'maxReplicaCount': 4,
                               'pollingInterval': 2, 'cooldownPeriod': 5,
                               'triggers': [{'type': 'aws-sqs-queue', 'authenticationRef': {'name': 'local-sqs'},
                                             'metadata': {'queueURL': f'{cluster_endpoint}/000000000000/{queue_name}',
                                                          'awsEndpoint': cluster_endpoint,
                                                          'awsRegion': 'us-east-1', 'queueLength': '1'}}]}}
            path = f'workloads/{mode}'
            documents = [deployment, secret, auth, scaled]
            lab.write(f'{path}/resources.yaml', documents)
            initial = lab.commit(f'{mode}: baseline deployment and KEDA')
            name = f'keda-{mode}-{suffix}'
            app = lab.app(name, path, namespace, revision=initial)
            if mode != 'no-ignore':
                app['spec']['ignoreDifferences'] = [{'group': 'apps', 'kind': 'Deployment', 'name': 'worker',
                                                    'namespace': namespace, 'jsonPointers': ['/spec/replicas']}]
            if mode == 'respect':
                app['spec']['syncPolicy'] = {'syncOptions': ['RespectIgnoreDifferences=true']}
            lab.apply(app)
            lab.sync(name, initial)
            lab.k('-n', namespace, 'rollout', 'status', 'deployment/worker', '--timeout=120s')
            first = lab.get('deployment', 'worker', namespace)['spec']['replicas']
            assert first == 1, 'first creation did not apply Git replicas'
            sqs.send_message_batch(QueueUrl=queue_url, Entries=[{'Id': str(i), 'MessageBody': '{}'} for i in range(4)])
            def scaled_four():
                d = lab.get('deployment', 'worker', namespace)
                return d if d['spec']['replicas'] == 4 and d.get('status', {}).get('readyReplicas') == 4 else None
            wait(scaled_four, timeout=180, description=f'{mode} scale to four')
            lab.refresh(name)
            expected_sync = 'OutOfSync' if mode == 'no-ignore' else 'Synced'
            wait(lambda: lab.get('application', name).get('status', {}).get('sync', {}).get('status') == expected_sync,
                 description=f'{mode} replica diff status')
            lab.record(f'keda-{mode}-baseline', firstCreationReplicas=first, scaledReplicas=4,
                       application=lab.snapshot(name), passed=True)
            for trial in range(args.trials):
                observer = DeploymentWatch(lab, namespace)
                try:
                    deployment['spec']['template']['spec']['containers'][0]['image'] = 'nginx:gitops-v2' if trial % 2 == 0 else 'nginx:alpine'
                    lab.write(f'{path}/resources.yaml', documents)
                    revision = lab.commit(f'{mode}: image rollout trial {trial + 1}')
                    app['spec']['source']['targetRevision'] = revision
                    lab.apply(app)
                    start = time.monotonic()
                    operation = lab.sync(name, revision)
                    used_options = operation['status']['operationState']['operation']['sync'].get('syncOptions', [])
                    assert ('RespectIgnoreDifferences=true' in used_options) == (mode == 'respect')
                    wait(scaled_four, timeout=180, description=f'{mode} HPA restores four')
                    lab.k('-n', namespace, 'rollout', 'status', 'deployment/worker', '--timeout=120s')
                    time.sleep(3)
                finally:
                    observer.close()
                values = [row['replicas'] for row in observer.rows]
                assert values[-1] == 4, 'watch did not capture the final restored replica count'
                if mode == 'respect':
                    assert set(values) == {4}, f'respect overwrote HPA replicas: {values}'
                else:
                    assert 1 in values and 4 in values, f'expected overwrite was not observed: {values}'
                lab.record(f'keda-{mode}-rollout-{trial+1}', mode=mode, revision=revision,
                           operationSyncOptions=used_options, elapsedSeconds=round(time.monotonic()-start, 3), minimumReplicas=min(values),
                           finalReplicas=values[-1], observations=observer.rows, passed=True)
            # Preserve other Git-controlled fields: ignoreDifferences is scoped to replicas only.
            if mode == 'respect':
                app['spec']['syncPolicy']['automated'] = {'selfHeal': True, 'prune': False}
                lab.apply(app)
                expected_image = deployment['spec']['template']['spec']['containers'][0]['image']
                # Change an explicitly Git-managed image to exercise automatic correction.
                lab.k('-n', namespace, 'set', 'image', 'deployment/worker', 'worker=nginx:utterai-invalid-20261028')
                lab.refresh(name)
                wait(lambda: lab.get('deployment', 'worker', namespace)['spec']['template']['spec']['containers'][0]['image'] == expected_image,
                     timeout=120, description='self-heal managed image')
                assert lab.get('deployment', 'worker', namespace)['spec']['replicas'] == 4
                lab.record('keda-respect-self-heal-image', expectedImage=expected_image, replicas=4, passed=True)
            sqs.purge_queue(QueueUrl=queue_url)
        lab.report['passed'] = True
        lab.report['versions'] = {'keda': '2.16.1', 'elasticmq': '1.6.9', 'argocd': '3.4.3'}
    finally:
        lab.save()


if __name__ == '__main__':
    main()
