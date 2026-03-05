# Helm Chart

## Install

### From OCI Registry

```bash
helm install network-monitor oci://ghcr.io/bayars/charts/network-monitor \
  --namespace network-monitor \
  --create-namespace
```

### From Source

```bash
helm install network-monitor helm/network-monitor \
  --namespace network-monitor \
  --create-namespace
```

### With Custom Values

```bash
helm install network-monitor helm/network-monitor \
  --namespace network-monitor \
  --create-namespace \
  --set config.logLevel=DEBUG \
  --set config.demoMode="true" \
  --set service.type=LoadBalancer

# Or with a values file
helm install network-monitor helm/network-monitor \
  --namespace network-monitor \
  --create-namespace \
  -f my-values.yaml
```

## Upgrade

```bash
helm upgrade network-monitor helm/network-monitor \
  --namespace network-monitor \
  -f my-values.yaml
```

## Uninstall

```bash
helm uninstall network-monitor --namespace network-monitor
```

## Values Reference

| Parameter | Default | Description |
|-----------|---------|-------------|
| `replicaCount` | `1` | Number of API replicas |
| `image.repository` | `ghcr.io/bayars/network-monitor` | Container image |
| `image.tag` | `latest` | Image tag |
| `image.pullPolicy` | `Always` | Image pull policy |
| `nameOverride` | `""` | Override chart name |
| `fullnameOverride` | `""` | Override full release name |
| `service.type` | `ClusterIP` | Service type (`ClusterIP`, `NodePort`, `LoadBalancer`) |
| `service.port` | `8000` | Service port |
| `config.logLevel` | `INFO` | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `config.demoMode` | `"false"` | Initialize with demo topology |
| `config.discoveryMode` | `hubble` | Discovery mode (`hubble` or `sysfs`) |
| `config.hubbleRelayAddr` | `hubble-relay.kube-system.svc.cluster.local:4245` | Hubble Relay gRPC address |
| `config.idleTimeoutSeconds` | `"5"` | Seconds before marking link idle |
| `resources.requests.cpu` | `100m` | CPU request |
| `resources.requests.memory` | `128Mi` | Memory request |
| `resources.limits.cpu` | `500m` | CPU limit |
| `resources.limits.memory` | `256Mi` | Memory limit |
| `rbac.create` | `true` | Create ClusterRole and ClusterRoleBinding |
| `serviceAccount.create` | `true` | Create ServiceAccount |
| `serviceAccount.name` | `""` | Override ServiceAccount name |
| `serviceAccount.annotations` | `{}` | ServiceAccount annotations |
| `ingress.enabled` | `false` | Enable Ingress resource |
| `ingress.className` | `""` | Ingress class name |
| `ingress.annotations` | `{}` | Ingress annotations |
| `ingress.host` | `network-monitor.local` | Ingress hostname |
| `ingress.tls` | `[]` | TLS configuration |
| `nodeSelector` | `{}` | Node selector |
| `tolerations` | `[]` | Tolerations |
| `affinity` | `{}` | Affinity rules |

## Example values.yaml

```yaml
replicaCount: 2

image:
  repository: ghcr.io/bayars/network-monitor
  tag: v1.0.0
  pullPolicy: IfNotPresent

service:
  type: ClusterIP
  port: 8000

config:
  logLevel: INFO
  demoMode: "false"
  discoveryMode: hubble
  hubbleRelayAddr: hubble-relay.kube-system.svc.cluster.local:4245
  idleTimeoutSeconds: "5"

ingress:
  enabled: true
  className: nginx
  host: network-monitor.example.com
  annotations:
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"
  tls:
    - secretName: network-monitor-tls
      hosts:
        - network-monitor.example.com

resources:
  requests:
    cpu: 200m
    memory: 256Mi
  limits:
    cpu: "1"
    memory: 512Mi
```
