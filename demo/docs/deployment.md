# Deployment Guide

Platform-specific deployment instructions for Network Monitor.

## Prerequisites

All platforms require:
- Kubernetes 1.28+
- Cilium CNI 1.14+ with Hubble enabled
- Helm 3.x

Verify Hubble is running:

```bash
cilium status
cilium hubble status
```

If Hubble is not enabled:

```bash
helm upgrade cilium cilium/cilium \
  --namespace kube-system \
  --set hubble.enabled=true \
  --set hubble.relay.enabled=true
```

---

## Google Kubernetes Engine (GKE)

GKE Dataplane V2 uses Cilium with Hubble built-in.

```bash
# Create cluster with Dataplane V2
gcloud container clusters create network-monitor-cluster \
  --enable-dataplane-v2 \
  --zone us-central1-a \
  --num-nodes 3

# Enable Hubble flow observability
gcloud container clusters update network-monitor-cluster \
  --zone us-central1-a \
  --enable-dataplane-v2-flow-observability

# Install Network Monitor
helm install network-monitor helm/network-monitor \
  --namespace network-monitor \
  --create-namespace \
  --set config.hubbleRelayAddr=hubble-relay.kube-system.svc.cluster.local:4245
```

Expose via LoadBalancer with static IP:

```bash
helm upgrade network-monitor helm/network-monitor \
  --namespace network-monitor \
  --set service.type=LoadBalancer \
  --set service.annotations."cloud\.google\.com/load-balancer-type"=Internal \
  --set service.annotations."networking\.gke\.io/load-balancer-ip-address"=my-reserved-ip
```

Or via GKE Ingress:

```bash
helm upgrade network-monitor helm/network-monitor \
  --namespace network-monitor \
  --set ingress.enabled=true \
  --set ingress.className=gce \
  --set ingress.host=network-monitor.example.com
```

---

## Amazon EKS

EKS requires installing Cilium as a replacement or overlay CNI.

```bash
# Create EKS cluster
eksctl create cluster --name network-monitor-cluster --region us-east-1 --nodes 3

# Install Cilium with Hubble
helm repo add cilium https://helm.cilium.io/
helm install cilium cilium/cilium \
  --namespace kube-system \
  --set eni.enabled=true \
  --set ipam.mode=eni \
  --set egressMasqueradeInterfaces=eth0 \
  --set hubble.enabled=true \
  --set hubble.relay.enabled=true

# Install Network Monitor
helm install network-monitor helm/network-monitor \
  --namespace network-monitor \
  --create-namespace
```

Expose via NLB with Elastic IP:

```bash
helm upgrade network-monitor helm/network-monitor \
  --namespace network-monitor \
  --set service.type=LoadBalancer \
  --set service.annotations."service\.beta\.kubernetes\.io/aws-load-balancer-type"=nlb \
  --set service.annotations."service\.beta\.kubernetes\.io/aws-load-balancer-scheme"=internet-facing \
  --set service.annotations."service\.beta\.kubernetes\.io/aws-load-balancer-eip-allocations"=eipalloc-xxx
```

Or via ALB Ingress:

```bash
helm upgrade network-monitor helm/network-monitor \
  --namespace network-monitor \
  --set ingress.enabled=true \
  --set ingress.className=alb \
  --set ingress.annotations."alb\.ingress\.kubernetes\.io/scheme"=internet-facing \
  --set ingress.host=network-monitor.example.com
```

---

## Azure AKS

AKS supports Azure CNI Powered by Cilium natively.

```bash
# Create AKS cluster with Cilium
az aks create \
  --resource-group myResourceGroup \
  --name network-monitor-cluster \
  --network-plugin azure \
  --network-plugin-mode overlay \
  --network-dataplane cilium \
  --node-count 3

# Enable Hubble
az aks update \
  --resource-group myResourceGroup \
  --name network-monitor-cluster \
  --enable-hubble

# Install Network Monitor
az aks get-credentials --resource-group myResourceGroup --name network-monitor-cluster
helm install network-monitor helm/network-monitor \
  --namespace network-monitor \
  --create-namespace
```

Expose via LoadBalancer:

```bash
helm upgrade network-monitor helm/network-monitor \
  --namespace network-monitor \
  --set service.type=LoadBalancer \
  --set service.annotations."service\.beta\.kubernetes\.io/azure-load-balancer-internal"=true \
  --set service.annotations."service\.beta\.kubernetes\.io/azure-pip-name"=my-pip
```

---

## On-Premises / Bare Metal

For on-prem clusters with Cilium already installed.

```bash
# Verify Cilium + Hubble
cilium status
cilium hubble status

# Install Network Monitor
helm install network-monitor helm/network-monitor \
  --namespace network-monitor \
  --create-namespace
```

Expose options:

```bash
# LoadBalancer (requires MetalLB or similar)
helm upgrade network-monitor helm/network-monitor \
  --namespace network-monitor \
  --set service.type=LoadBalancer

# NodePort
helm upgrade network-monitor helm/network-monitor \
  --namespace network-monitor \
  --set service.type=NodePort

# Ingress (nginx)
helm upgrade network-monitor helm/network-monitor \
  --namespace network-monitor \
  --set ingress.enabled=true \
  --set ingress.className=nginx \
  --set ingress.host=network-monitor.internal.example.com
```

---

## Interface Metrics

### Sidecar Agent (Recommended)

The sidecar is injected into every Clabernetes topology pod via `extraContainers`. It reads `/sys/class/net/*/statistics/` directly — zero K8s API overhead, captures **all** traffic (ping, ssh, scp, routing protocols), and sees every interface (linecards, CPM, mgmt).

Add to your Clabernetes Helm values:

```yaml
globalConfig:
  deployment:
    extraContainers:
      - name: netmon-sidecar
        image: ghcr.io/bayars/netmon-sidecar:latest
        env:
          - name: API_URL
            value: "http://network-monitor.network-monitor.svc:8000"
          - name: POLL_INTERVAL_MS
            value: "2000"
          - name: POD_NAME
            valueFrom:
              fieldRef:
                fieldPath: metadata.name
          - name: POD_NAMESPACE
            valueFrom:
              fieldRef:
                fieldPath: metadata.namespace
```

| Variable | Default | Description |
|----------|---------|-------------|
| `API_URL` | (required) | Network Monitor API URL |
| `POLL_INTERVAL_MS` | `2000` | Collection interval in milliseconds |
| `EXCLUDE_IFACES` | `lo` | Comma-separated interfaces to skip |

Lower `POLL_INTERVAL_MS` for more responsive updates (e.g., `500` for near-real-time), or raise it to reduce load.

### Standalone Collector (Fallback)

For environments where sidecar injection is not possible, the standalone collector uses `kubectl exec` to read `/proc/net/dev` from each pod:

```bash
kubectl apply -f k8s/collector.yaml
```

---

## k3s / k3d

```bash
# Create k3s cluster without default CNI
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--flannel-backend=none --disable-network-policy" sh -

# Install Cilium with Hubble
helm install cilium cilium/cilium \
  --namespace kube-system \
  --set hubble.enabled=true \
  --set hubble.relay.enabled=true

# Install Network Monitor
helm install network-monitor helm/network-monitor \
  --namespace network-monitor \
  --create-namespace \
  --set service.type=NodePort
```
