# Kind & Bare Metal Deployment

Step-by-step guide for deploying Network Monitor on Kind clusters and bare-metal Kubernetes.

---

## Kind

### 1. Create the Cluster

Disable the default CNI so Cilium can manage networking.

```yaml
# kind-config.yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
networking:
  disableDefaultCNI: true
  podSubnet: 10.244.0.0/16
  serviceSubnet: 10.96.0.0/16
nodes:
  - role: control-plane
    extraPortMappings:
      - containerPort: 30800
        hostPort: 8000
        protocol: TCP
  - role: worker
  - role: worker
```

```bash
kind create cluster --name network-monitor --config kind-config.yaml
```

### 2. Install Cilium with Hubble

```bash
helm repo add cilium https://helm.cilium.io/
helm repo update

helm install cilium cilium/cilium \
  --namespace kube-system \
  --set image.pullPolicy=IfNotPresent \
  --set ipam.mode=kubernetes \
  --set hubble.enabled=true \
  --set hubble.relay.enabled=true \
  --set hubble.ui.enabled=true
```

Wait for Cilium to be ready:

```bash
cilium status --wait
cilium hubble status
```

### 3. Install Network Monitor

```bash
helm install network-monitor helm/network-monitor \
  --namespace network-monitor \
  --create-namespace \
  --set service.type=NodePort \
  --set service.nodePort=30800
```

Access via the port mapping defined in `kind-config.yaml`:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/api/topology
```

### 4. (Optional) LoadBalancer with Cilium LB IPAM

Kind does not support LoadBalancer services out of the box. Cilium LB IPAM with L2 announcements works inside Kind using the Docker network range.

Find the Kind Docker network subnet:

```bash
docker network inspect kind -f '{{(index .IPAM.Config 0).Subnet}}'
```

Create a pool from an unused range within that subnet (example for `172.18.0.0/16`):

```yaml
# kind-cilium-lb.yaml
apiVersion: cilium.io/v2alpha1
kind: CiliumLoadBalancerIPPool
metadata:
  name: kind-pool
spec:
  blocks:
    - start: 172.18.255.200
      stop: 172.18.255.250
---
apiVersion: cilium.io/v2alpha1
kind: CiliumL2AnnouncementPolicy
metadata:
  name: default-l2
spec:
  loadBalancerIPs: true
```

```bash
kubectl apply -f kind-cilium-lb.yaml

helm upgrade network-monitor helm/network-monitor \
  --namespace network-monitor \
  --set service.type=LoadBalancer
```

The assigned IP is reachable from the host since it is on the same Docker bridge network:

```bash
LB_IP=$(kubectl get svc -n network-monitor network-monitor -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
curl http://${LB_IP}:8000/health
```

### 5. (Optional) Install Clabernetes for Lab Topologies

```bash
helm repo add clabernetes https://srl-labs.github.io/clabernetes/
helm repo update

helm install clabernetes clabernetes/clabernetes \
  --namespace clabernetes \
  --create-namespace
```

Deploy a lab topology:

```bash
kubectl apply -f demo/dc2-topology.yaml
```

### 6. Load a Private Image into Kind

Kind nodes cannot pull from private registries without extra setup. Load images directly:

```bash
docker build -t network-monitor:latest .
kind load docker-image network-monitor:latest --name network-monitor

helm install network-monitor helm/network-monitor \
  --namespace network-monitor \
  --create-namespace \
  --set image.repository=network-monitor \
  --set image.tag=latest \
  --set image.pullPolicy=Never \
  --set service.type=NodePort \
  --set service.nodePort=30800
```

### Kind Cleanup

```bash
kind delete cluster --name network-monitor
```

---

## Bare Metal

### Prerequisites

- Kubernetes 1.28+ (kubeadm, kubespray, or similar)
- Cilium CNI 1.14+ with Hubble enabled
- Helm 3.x
- (Optional) Cilium LB IPAM, MetalLB, or kube-vip for LoadBalancer services
- (Optional) Clabernetes for lab topologies

### 1. Install Cilium with Hubble

If your cluster does not already have Cilium:

```bash
helm repo add cilium https://helm.cilium.io/
helm repo update

helm install cilium cilium/cilium \
  --namespace kube-system \
  --set ipam.mode=kubernetes \
  --set hubble.enabled=true \
  --set hubble.relay.enabled=true \
  --set hubble.ui.enabled=true \
  --set kubeProxyReplacement=true
```

If Cilium is already installed but Hubble is not enabled:

```bash
helm upgrade cilium cilium/cilium \
  --namespace kube-system \
  --reuse-values \
  --set hubble.enabled=true \
  --set hubble.relay.enabled=true
```

Verify:

```bash
cilium status --wait
cilium hubble status
kubectl get pods -n kube-system -l app.kubernetes.io/name=hubble-relay
```

### 2. Install Network Monitor

```bash
helm install network-monitor helm/network-monitor \
  --namespace network-monitor \
  --create-namespace
```

Or from the OCI registry:

```bash
helm install network-monitor oci://ghcr.io/bayars/charts/network-monitor \
  --namespace network-monitor \
  --create-namespace
```

### 3. Expose the Service

Choose one of the following based on your environment.

#### Option A: NodePort

Accessible on `<any-node-ip>:30800`:

```bash
helm upgrade network-monitor helm/network-monitor \
  --namespace network-monitor \
  --set service.type=NodePort \
  --set service.nodePort=30800
```

```bash
NODE_IP=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
curl http://${NODE_IP}:30800/health
```

#### Option B: LoadBalancer with Cilium LB IPAM

Cilium 1.13+ includes a built-in LoadBalancer IPAM and L2/BGP announcement capability, eliminating the need for MetalLB.

**Enable LB IPAM and L2 announcements in Cilium:**

```bash
helm upgrade cilium cilium/cilium \
  --namespace kube-system \
  --reuse-values \
  --set l2announcements.enabled=true \
  --set externalIPs.enabled=true
```

**Create an IP pool and L2 announcement policy:**

```yaml
# cilium-lb-pool.yaml
apiVersion: cilium.io/v2alpha1
kind: CiliumLoadBalancerIPPool
metadata:
  name: network-monitor-pool
spec:
  blocks:
    - start: 192.168.1.100
      stop: 192.168.1.110
---
apiVersion: cilium.io/v2alpha1
kind: CiliumL2AnnouncementPolicy
metadata:
  name: default-l2
spec:
  loadBalancerIPs: true
  interfaces:
    - ^eth[0-9]+
    - ^eno[0-9]+
    - ^enp[0-9]+s[0-9]+
```

```bash
kubectl apply -f cilium-lb-pool.yaml
```

**Deploy Network Monitor with LoadBalancer:**

```bash
helm upgrade network-monitor helm/network-monitor \
  --namespace network-monitor \
  --set service.type=LoadBalancer \
  --set service.externalTrafficPolicy=Local
```

Get the assigned IP:

```bash
kubectl get svc -n network-monitor network-monitor -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
```

**Using BGP instead of L2:**

For environments that require BGP peering instead of L2 ARP announcements:

```bash
helm upgrade cilium cilium/cilium \
  --namespace kube-system \
  --reuse-values \
  --set bgpControlPlane.enabled=true
```

```yaml
# cilium-bgp.yaml
apiVersion: cilium.io/v2alpha1
kind: CiliumBGPClusterConfig
metadata:
  name: bgp-config
spec:
  nodeSelector:
    matchLabels:
      kubernetes.io/os: linux
  bgpInstances:
    - name: default
      localASN: 65001
      peers:
        - name: tor-switch
          peerASN: 65000
          peerAddress: 10.0.0.1
          peerConfigRef:
            name: default-peer
---
apiVersion: cilium.io/v2alpha1
kind: CiliumBGPPeerConfig
metadata:
  name: default-peer
spec:
  families:
    - afi: ipv4
      safi: unicast
  gracefulRestart:
    enabled: true
---
apiVersion: cilium.io/v2alpha1
kind: CiliumBGPAdvertisement
metadata:
  name: lb-services
spec:
  advertisements:
    - advertisementType: Service
      service:
        addresses:
          - LoadBalancerIP
      selector:
        matchLabels:
          app.kubernetes.io/name: network-monitor
```

```bash
kubectl apply -f cilium-bgp.yaml
```

#### Option C: LoadBalancer with MetalLB

Install MetalLB if not already present:

```bash
helm repo add metallb https://metallb.github.io/metallb
helm install metallb metallb/metallb --namespace metallb-system --create-namespace
```

Configure an IP pool (adjust the range to your network):

```yaml
# metallb-pool.yaml
apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata:
  name: network-monitor-pool
  namespace: metallb-system
spec:
  addresses:
    - 192.168.1.100-192.168.1.110
---
apiVersion: metallb.io/v1beta1
kind: L2Advertisement
metadata:
  name: l2-advert
  namespace: metallb-system
```

```bash
kubectl apply -f metallb-pool.yaml

helm upgrade network-monitor helm/network-monitor \
  --namespace network-monitor \
  --set service.type=LoadBalancer \
  --set service.externalTrafficPolicy=Local
```

Get the assigned IP:

```bash
kubectl get svc -n network-monitor network-monitor -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
```

#### Option D: Ingress with nginx

```bash
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm install ingress-nginx ingress-nginx/ingress-nginx \
  --namespace ingress-nginx --create-namespace
```

```bash
helm upgrade network-monitor helm/network-monitor \
  --namespace network-monitor \
  --set ingress.enabled=true \
  --set ingress.className=nginx \
  --set ingress.host=network-monitor.example.com \
  --set ingress.annotations."nginx\.ingress\.kubernetes\.io/proxy-read-timeout"=3600 \
  --set ingress.annotations."nginx\.ingress\.kubernetes\.io/proxy-send-timeout"=3600
```

Add a DNS or `/etc/hosts` entry pointing `network-monitor.example.com` to the ingress controller IP.

#### Option E: Port Forward (quick access)

```bash
kubectl port-forward -n network-monitor svc/network-monitor 8000:8000
curl http://localhost:8000/health
```

### 4. Private Registry

If your cluster pulls from a private registry (Harbor, Nexus, etc.):

```bash
# Create the pull secret
kubectl create secret docker-registry registry-creds \
  --docker-server=harbor.internal \
  --docker-username=admin \
  --docker-password=changeme \
  --namespace network-monitor

helm upgrade network-monitor helm/network-monitor \
  --namespace network-monitor \
  --set image.repository=harbor.internal/library/network-monitor \
  --set image.tag=v1.0.0 \
  --set image.pullPolicy=IfNotPresent \
  --set imagePullSecrets[0].name=registry-creds
```

For insecure HTTP registries (e.g., `10.0.0.103:80`), configure containerd on each node:

```bash
# /etc/containerd/certs.d/10.0.0.103:80/hosts.toml
[host."http://10.0.0.103:80"]
  capabilities = ["pull", "resolve"]
  skip_verify = true
```

Then restart containerd and use the port-qualified image name:

```bash
helm upgrade network-monitor helm/network-monitor \
  --namespace network-monitor \
  --set image.repository=10.0.0.103:80/library/network-monitor
```

### 5. (Optional) Install Clabernetes

```bash
helm repo add clabernetes https://srl-labs.github.io/clabernetes/
helm install clabernetes clabernetes/clabernetes \
  --namespace clabernetes \
  --create-namespace
```

Deploy a lab and register it with Network Monitor:

```bash
kubectl apply -f demo/dc2-topology.yaml

# Register topology in the API
curl -X POST http://<NETWORK_MONITOR_URL>/api/labs \
  -H "Content-Type: application/json" \
  -d '{
    "name": "dc2",
    "namespace": "customerb",
    "clabernetes_yaml": "'"$(kubectl get topology dc2 -n customerb -o yaml | sed 's/"/\\"/g')"'"
  }'
```

### 6. Verify

```bash
# Health
curl http://<NETWORK_MONITOR_URL>/health

# Topology
curl http://<NETWORK_MONITOR_URL>/api/topology

# Links
curl http://<NETWORK_MONITOR_URL>/api/links

# Labs
curl http://<NETWORK_MONITOR_URL>/api/labs

# WebSocket (requires wscat: npm install -g wscat)
wscat -c ws://<NETWORK_MONITOR_URL>/ws/events
```

### 7. Demo Traffic Scripts

Run the demo scripts to simulate traffic against the API:

```bash
export API_URL=http://<NETWORK_MONITOR_URL>

# Show bandwidth table
./demo/show-bandwidth.sh $API_URL

# Visual dashboard
./demo/dashboard.sh $API_URL

# Simulate traffic (200 Mbps for 30 seconds)
API_URL=$API_URL ./demo/traffic.sh dc2 200 30

# List labs
./demo/list-labs.sh $API_URL
```

---

## Troubleshooting

### Hubble Relay not reachable

```bash
# Check relay pod
kubectl get pods -n kube-system -l app.kubernetes.io/name=hubble-relay

# Check relay logs
kubectl logs -n kube-system -l app.kubernetes.io/name=hubble-relay

# Verify the relay address matches your helm config
kubectl get svc -n kube-system hubble-relay
```

If the relay service is in a different namespace or has a different name:

```bash
helm upgrade network-monitor helm/network-monitor \
  --namespace network-monitor \
  --set config.hubbleRelayAddr=hubble-relay.kube-system.svc.cluster.local:4245
```

To run without Hubble:

```bash
helm upgrade network-monitor helm/network-monitor \
  --namespace network-monitor \
  --set config.hubbleEnabled="false"
```

### Pod CrashLoopBackOff

```bash
kubectl describe pod -n network-monitor -l app.kubernetes.io/name=network-monitor
kubectl logs -n network-monitor -l app.kubernetes.io/name=network-monitor
```

### Image pull errors

```bash
# Check events
kubectl get events -n network-monitor --sort-by='.lastTimestamp'

# Verify image is accessible from a node
crictl pull <image>
```

### Kind: pods stuck in Pending

Cilium may not be ready. Check:

```bash
kubectl get pods -n kube-system -l app.kubernetes.io/part-of=cilium
cilium status
```
