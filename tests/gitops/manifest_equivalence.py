"""Semantic ConfigMap comparison for the controlled fixture; not a generic Kubernetes diff engine."""
import hashlib
import json
import yaml


def canonical_configmaps(manifests):
    result = []
    for document in yaml.safe_load_all(manifests):
        if not document:
            continue
        if document.get('kind') != 'ConfigMap':
            raise ValueError('This audit fixture only supports ConfigMaps')
        meta = document['metadata']
        result.append({'apiVersion': document['apiVersion'], 'kind': document['kind'],
                       'namespace': meta.get('namespace', 'default'), 'name': meta['name'],
                       'data': document.get('data', {}), 'binaryData': document.get('binaryData', {}),
                       'immutable': document.get('immutable', False)})
    return sorted(result, key=lambda r: (r['namespace'], r['kind'], r['name']))


def fingerprint(manifests):
    canonical = canonical_configmaps(manifests)
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def compare(cached, independent):
    left, right = canonical_configmaps(cached), canonical_configmaps(independent)
    return {'equivalent': left == right, 'cachedFingerprint': fingerprint(cached),
            'independentFingerprint': fingerprint(independent), 'cachedConfigMaps': left, 'independentConfigMaps': right}
