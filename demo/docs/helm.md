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

### Image

| Parameter | Default | Description |
|-----------|---------|-------------|
| `replicaCount` | `1` | Number of API replicas |
| `image.repository` | `ghcr.io/bayars/network-monitor` | Container image |
| `image.tag` | `latest` | Image tag |
| `image.pullPolicy` | `Always` | Image pull policy |
| `imagePullSecrets` | `[]` | Docker registry credentials (e.g. `[{name: harbor-creds}]`) |
| `nameOverride` | `""` | Override chart name |
| `fullnameOverride` | `""` | Override full release name |

### Service

| Parameter | Default | Description |
|-----------|---------|-------------|
| `service.type` | `ClusterIP` | `ClusterIP`, `NodePort`, or `LoadBalancer` |
| `service.port` | `8000` | Service port |
| `service.annotations` | `{}` | Service annotations (cloud LB config) |
| `service.loadBalancerIP` | `""` | Static LB IP (legacy, prefer annotations) |
| `service.loadBalancerSourceRanges` | `[]` | CIDR ranges allowed to reach the LB |
| `service.externalTrafficPolicy` | `""` | `Local` (preserve client IP) or `Cluster` |
| `service.nodePort` | `null` | Static NodePort (only for `NodePort` type) |

### Application

| Parameter | Default | Description |
|-----------|---------|-------------|
| `config.logLevel` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `config.hubbleEnabled` | `"true"` | Enable Cilium Hubble integration |
| `config.hubbleRelayAddr` | `hubble-relay.kube-system.svc.cluster.local:4245` | Hubble Relay gRPC address |
| `config.idleTimeoutSeconds` | `"5"` | Seconds before marking link idle |

### Interface Metrics Collector

| Parameter | Default | Description |
|-----------|---------|-------------|
| `collector.enabled` | `false` | Deploy the interface metrics collector |
| `collector.image.repository` | `ghcr.io/bayars/netmon-collector` | Collector image |
| `collector.image.tag` | `latest` | Collector image tag |
| `collector.pollIntervalMs` | `2000` | Collection interval in milliseconds |
| `collector.namespace` | `clab` | Namespace where target pods run |
| `collector.podSelector` | `clabernetes/app=clabernetes` | Label selector for target pods |
| `collector.excludeInterfaces` | `lo` | Comma-separated interfaces to skip |
| `collector.logLevel` | `INFO` | Collector log level |

The collector reads `/proc/net/dev` from each target pod via `kubectl exec` and captures **all** kernel-level traffic (ping, ssh, scp, routing protocols, etc.). Adjust `pollIntervalMs` for faster or slower updates.

### Pod

| Parameter | Default | Description |
|-----------|---------|-------------|
| `podAnnotations` | `{}` | Pod annotations (Prometheus, workload identity) |
| `podLabels` | `{}` | Additional pod labels |
| `resources.requests.cpu` | `100m` | CPU request |
| `resources.requests.memory` | `128Mi` | Memory request |
| `resources.limits.cpu` | `500m` | CPU limit |
| `resources.limits.memory` | `256Mi` | Memory limit |
| `nodeSelector` | `{}` | Node selector |
| `tolerations` | `[]` | Tolerations |
| `affinity` | `{}` | Affinity rules |

### RBAC & ServiceAccount

| Parameter | Default | Description |
|-----------|---------|-------------|
| `rbac.create` | `true` | Create ClusterRole and ClusterRoleBinding |
| `serviceAccount.create` | `true` | Create ServiceAccount |
| `serviceAccount.name` | `""` | Override ServiceAccount name |
| `serviceAccount.annotations` | `{}` | SA annotations (cloud workload identity) |

### Ingress

| Parameter | Default | Description |
|-----------|---------|-------------|
| `ingress.enabled` | `false` | Enable Ingress resource |
| `ingress.className` | `""` | Ingress class (`nginx`, `gce`, `alb`, etc.) |
| `ingress.annotations` | `{}` | Ingress annotations |
| `ingress.host` | `network-monitor.local` | Ingress hostname |
| `ingress.tls` | `[]` | TLS configuration |

---

## Cloud Platform Examples

### GKE - External LoadBalancer with Static IP

```yaml
service:
  type: LoadBalancer
  annotations:
    networking.gke.io/load-balancer-ip-address: "my-reserved-ip-name"
    cloud.google.com/load-balancer-type: "External"
  externalTrafficPolicy: Local

serviceAccount:
  annotations:
    iam.gke.io/gsa-email: "network-monitor@my-project.iam.gserviceaccount.com"
```

### GKE - Internal LoadBalancer

```yaml
service:
  type: LoadBalancer
  annotations:
    cloud.google.com/load-balancer-type: "Internal"
    networking.gke.io/internal-load-balancer-subnet: "my-subnet"
  loadBalancerSourceRanges:
    - 10.0.0.0/8
```

### AWS EKS - NLB with Elastic IP

```yaml
service:
  type: LoadBalancer
  annotations:
    service.beta.kubernetes.io/aws-load-balancer-type: "external"
    service.beta.kubernetes.io/aws-load-balancer-nlb-target-type: "ip"
    service.beta.kubernetes.io/aws-load-balancer-scheme: "internet-facing"
    service.beta.kubernetes.io/aws-load-balancer-eip-allocations: "eipalloc-xxx,eipalloc-yyy"
  externalTrafficPolicy: Local

serviceAccount:
  annotations:
    eks.amazonaws.com/role-arn: "arn:aws:iam::123456789:role/network-monitor"
```

### AWS EKS - Internal NLB

```yaml
service:
  type: LoadBalancer
  annotations:
    service.beta.kubernetes.io/aws-load-balancer-type: "external"
    service.beta.kubernetes.io/aws-load-balancer-nlb-target-type: "ip"
    service.beta.kubernetes.io/aws-load-balancer-scheme: "internal"
  loadBalancerSourceRanges:
    - 10.0.0.0/8
    - 172.16.0.0/12
```

### Azure AKS - Public LB with Static IP

```yaml
service:
  type: LoadBalancer
  annotations:
    service.beta.kubernetes.io/azure-pip-name: "my-pip-name"
    service.beta.kubernetes.io/azure-dns-label-name: "network-monitor"

serviceAccount:
  annotations:
    azure.workload.identity/client-id: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

### Azure AKS - Internal LB

```yaml
service:
  type: LoadBalancer
  annotations:
    service.beta.kubernetes.io/azure-load-balancer-internal: "true"
    service.beta.kubernetes.io/azure-load-balancer-internal-subnet: "my-subnet"
  loadBalancerSourceRanges:
    - 10.0.0.0/8
```

### On-Prem with MetalLB

```yaml
service:
  type: LoadBalancer
  loadBalancerIP: "192.168.1.100"
  externalTrafficPolicy: Local
```

### Private Registry (Harbor)

```yaml
image:
  repository: harbor.internal/library/network-monitor
  tag: v1.0.0
  pullPolicy: IfNotPresent

imagePullSecrets:
  - name: harbor-credentials
```

---

## Full Example values.yaml

```yaml
replicaCount: 2

image:
  repository: ghcr.io/bayars/network-monitor
  tag: v1.0.0
  pullPolicy: IfNotPresent

service:
  type: LoadBalancer
  port: 8000
  annotations:
    service.beta.kubernetes.io/aws-load-balancer-type: "external"
    service.beta.kubernetes.io/aws-load-balancer-nlb-target-type: "ip"
    service.beta.kubernetes.io/aws-load-balancer-scheme: "internet-facing"
  externalTrafficPolicy: Local
  loadBalancerSourceRanges:
    - 203.0.113.0/24

config:
  logLevel: INFO
  hubbleEnabled: "true"
  hubbleRelayAddr: hubble-relay.kube-system.svc.cluster.local:4245
  idleTimeoutSeconds: "5"

serviceAccount:
  annotations:
    eks.amazonaws.com/role-arn: "arn:aws:iam::123456789:role/network-monitor"

podAnnotations:
  prometheus.io/scrape: "true"
  prometheus.io/port: "8000"

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
