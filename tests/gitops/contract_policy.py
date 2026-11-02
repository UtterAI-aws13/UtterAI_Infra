"""A declared compatibility gate; it does not infer retained schemas from SQS queue depth."""
READERS = {'v1': {1}, 'v2': {1, 2}, 'v1-bridge': {1, 2}}


def assess_rollback(reader, retained_versions):
    if reader not in READERS:
        raise ValueError(f'Unknown reader version: {reader}')
    unsupported = set(retained_versions) - READERS[reader]
    return {'reader': reader, 'retainedSchemas': sorted(retained_versions),
            'supportedSchemas': sorted(READERS[reader]), 'allowed': not unsupported,
            'unsupportedSchemas': sorted(unsupported)}


def decode_payload(payload, reader):
    version = payload.get('schema_version')
    if version not in READERS[reader]:
        raise ValueError(f'unsupported schema {version} for {reader}')
    value = payload['value'] if version == 1 else payload['input']['value']
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError('value must be an integer')
    return payload['job_id'], value
