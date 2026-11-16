"""Render a deployment slice for an already-built internal API image.

python -m deploy.render registry.internal/fde/api@sha256:<reviewed digest>
The referenced API must implement port 8080 and /healthz + /readyz. Platform
services, a verified image, fde-db secret and approved-models PVC are prerequisites.
"""
import re
import sys
import yaml


def render(image):
    if not re.fullmatch(r'[A-Za-z0-9.-]+(?::[0-9]+)?/[A-Za-z0-9_./-]+@sha256:[0-9a-f]{64}', image):
        raise ValueError('a reviewed registry image digest is required')
    ns = 'fde'
    documents = [
        {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': ns, 'labels': {
            'pod-security.kubernetes.io/enforce': 'restricted'}}},
        {'apiVersion': 'v1', 'kind': 'ServiceAccount',
         'metadata': {'name': 'rag-api', 'namespace': ns}, 'automountServiceAccountToken': False},
        {'apiVersion': 'apps/v1', 'kind': 'Deployment',
         'metadata': {'name': 'rag-api', 'namespace': ns}, 'spec': {
             'replicas': 2, 'selector': {'matchLabels': {'app': 'rag-api'}},
             'strategy': {'type': 'RollingUpdate', 'rollingUpdate': {'maxUnavailable': 0, 'maxSurge': 1}},
             'template': {'metadata': {'labels': {'app': 'rag-api'}}, 'spec': {
                 'serviceAccountName': 'rag-api', 'automountServiceAccountToken': False,
                 'terminationGracePeriodSeconds': 30,
                 'securityContext': {'runAsNonRoot': True, 'runAsUser': 10001,
                                     'seccompProfile': {'type': 'RuntimeDefault'}},
                 'containers': [{'name': 'api', 'image': image, 'imagePullPolicy': 'IfNotPresent',
                     'ports': [{'name': 'http', 'containerPort': 8080}],
                     'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                                         'capabilities': {'drop': ['ALL']}},
                     'resources': {'requests': {'cpu': '500m', 'memory': '512Mi'},
                                   'limits': {'cpu': '2', 'memory': '2Gi'}},
                     'startupProbe': {'httpGet': {'path': '/healthz', 'port': 'http'},
                                      'periodSeconds': 5, 'failureThreshold': 24},
                     'livenessProbe': {'httpGet': {'path': '/healthz', 'port': 'http'}, 'periodSeconds': 10},
                     'readinessProbe': {'httpGet': {'path': '/readyz', 'port': 'http'}, 'periodSeconds': 5},
                     'env': [{'name': 'DB_DSN_FILE', 'value': '/var/run/db/dsn'},
                             {'name': 'MODEL_PATH', 'value': '/models'}],
                     'volumeMounts': [{'name': 'tmp', 'mountPath': '/tmp'},
                                      {'name': 'db', 'mountPath': '/var/run/db', 'readOnly': True},
                                      {'name': 'models', 'mountPath': '/models', 'readOnly': True}]}],
                 'volumes': [{'name': 'tmp', 'emptyDir': {'sizeLimit': '128Mi'}},
                             {'name': 'db', 'secret': {'secretName': 'fde-db'}},
                             {'name': 'models', 'persistentVolumeClaim': {'claimName': 'approved-models'}}]
             }}}},
        {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': 'rag-api', 'namespace': ns},
         'spec': {'selector': {'app': 'rag-api'}, 'ports': [{'port': 8080, 'targetPort': 'http'}]}},
        {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
         'metadata': {'name': 'default-deny', 'namespace': ns},
         'spec': {'podSelector': {}, 'policyTypes': ['Ingress', 'Egress']}},
    ]
    def peer(app):
        return {'podSelector': {'matchLabels': {'app': app}}}
    def port(value, protocol='TCP'):
        return {'protocol': protocol, 'port': value}
    def policy(name, app, body):
        return {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
                'metadata': {'name': name, 'namespace': ns},
                'spec': {'podSelector': {'matchLabels': {'app': app}}, **body}}
    documents.append(policy('api-flows', 'rag-api', {
        'policyTypes': ['Ingress', 'Egress'],
        'ingress': [{'from': [peer('gateway')], 'ports': [port(8080)]}],
        'egress': [
            {'to': [peer('postgres')], 'ports': [port(5432)]},
            {'to': [peer('inference')], 'ports': [port(8000)]},
            {'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'observability'}},
                     'podSelector': {'matchLabels': {'app': 'otel-collector'}}}], 'ports': [port(4317)]},
            {'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}},
                     'podSelector': {'matchLabels': {'k8s-app': 'kube-dns'}}}],
             'ports': [port(53), port(53, 'UDP')]},
        ]}))
    for app, target in (('postgres', 5432), ('inference', 8000)):
        documents.append(policy('from-api-' + app, app, {
            'policyTypes': ['Ingress'], 'ingress': [{'from': [peer('rag-api')], 'ports': [port(target)]}]}))
    return documents


if __name__ == '__main__':
    print(yaml.safe_dump_all(render(sys.argv[1]), sort_keys=False))
