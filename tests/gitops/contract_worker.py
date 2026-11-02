"""Synthetic versioned reader, actual SQS-compatible queue; no ML, user data, or AWS calls."""
import json
import os
from pathlib import Path
import time

import boto3
from botocore.config import Config
from contract_policy import decode_payload

reader = os.environ['READER_VERSION']
sqs = boto3.client('sqs', endpoint_url=os.environ['SQS_ENDPOINT'], region_name='us-east-1',
                   aws_access_key_id='local-only', aws_secret_access_key='local-only',
                   config=Config(connect_timeout=2, read_timeout=5, retries={'max_attempts': 2}))
input_url, output_url, dlq_url = [os.environ[key] for key in ('INPUT_QUEUE', 'OUTPUT_QUEUE', 'DLQ_QUEUE')]
sqs.get_queue_attributes(QueueUrl=input_url, AttributeNames=['ApproximateNumberOfMessages'])
Path('/tmp/ready').touch()
while True:
    if os.environ.get('CONSUME', 'true') == 'false':
        time.sleep(1)
        continue
    messages = sqs.receive_message(QueueUrl=input_url, MaxNumberOfMessages=1, WaitTimeSeconds=1,
                                   VisibilityTimeout=2, AttributeNames=['ApproximateReceiveCount']).get('Messages', [])
    for message in messages:
        payload = json.loads(message['Body'])
        try:
            job_id, value = decode_payload(payload, reader)
            result = {'job_id': job_id, 'schema_version': payload['schema_version'], 'reader': reader, 'result': value * 2}
            sqs.send_message(QueueUrl=output_url, MessageBody=json.dumps(result))
            sqs.delete_message(QueueUrl=input_url, ReceiptHandle=message['ReceiptHandle'])
            print(json.dumps({'event': 'processed', **result}), flush=True)
        except ValueError as error:
            attempt = int(message['Attributes']['ApproximateReceiveCount'])
            if attempt >= 3:
                sqs.send_message(QueueUrl=dlq_url, MessageBody=json.dumps({'original': payload, 'reader': reader,
                                                                          'attempt': attempt, 'error': str(error)}))
                sqs.delete_message(QueueUrl=input_url, ReceiptHandle=message['ReceiptHandle'])
                print(json.dumps({'event': 'quarantined', 'job_id': payload['job_id'], 'reader': reader, 'attempt': attempt}), flush=True)
        time.sleep(0.05)
