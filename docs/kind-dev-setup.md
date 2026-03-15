# Kind Development & CI Setup

Local development and CI environment using Kind with NodePort and host port mapping.

---

## Architecture

```
Host machine (localhost)
  :8000 ──► Docker bridge "kind" (172.18.0.0/16)
               └── kind-control-plane (172.18.0.2)
                     :30800 (NodePort) ──► network-monitor pod :8000
```

Kind maps a host port to a container port on the control-plane node. The NodePort service inside the cluster completes the path. No LoadBalancer, no Cilium LB IPAM, no MetalLB needed.

---

## Local Development

### 1. Create the Cluster

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

Wait for readiness:

```bash
cilium status --wait
```

### 3. Build and Load the Image

Kind nodes cannot pull from remote registries by default. Build locally and load into the cluster:

```bash
docker build -t network-monitor:dev .
kind load docker-image network-monitor:dev --name network-monitor
```

### 4. Deploy Network Monitor

```bash
helm install network-monitor helm/network-monitor \
  --namespace network-monitor \
  --create-namespace \
  --set image.repository=network-monitor \
  --set image.tag=dev \
  --set image.pullPolicy=Never \
  --set service.type=NodePort \
  --set service.nodePort=30800
```

### 5. Verify

```bash
kubectl get pods -n network-monitor
curl http://localhost:8000/health
curl http://localhost:8000/api/topology
```

### Rebuild After Code Changes

```bash
docker build -t network-monitor:dev .
kind load docker-image network-monitor:dev --name network-monitor
kubectl rollout restart deployment/network-monitor -n network-monitor
kubectl rollout status deployment/network-monitor -n network-monitor
curl http://localhost:8000/health
```

### Run Demo Scripts Against Local Cluster

```bash
export API_URL=http://localhost:8000

./demo/show-bandwidth.sh $API_URL
./demo/list-labs.sh $API_URL
./demo/dashboard.sh $API_URL
API_URL=$API_URL ./demo/traffic.sh dc2 200 30
```

### Cleanup

```bash
kind delete cluster --name network-monitor
```

---

## CI Pipeline (GitHub Actions)

```yaml
# .github/workflows/e2e.yaml
name: E2E Tests

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  e2e:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Create Kind cluster
        uses: helm/kind-action@v1
        with:
          cluster_name: e2e
          config: kind-config.yaml

      - name: Install Cilium
        run: |
          helm repo add cilium https://helm.cilium.io/
          helm install cilium cilium/cilium \
            --namespace kube-system \
            --set image.pullPolicy=IfNotPresent \
            --set ipam.mode=kubernetes \
            --set hubble.enabled=true \
            --set hubble.relay.enabled=true \
            --wait

      - name: Build and load image
        run: |
          docker build -t network-monitor:e2e .
          kind load docker-image network-monitor:e2e --name e2e

      - name: Deploy Network Monitor
        run: |
          helm install network-monitor helm/network-monitor \
            --namespace network-monitor \
            --create-namespace \
            --set image.repository=network-monitor \
            --set image.tag=e2e \
            --set image.pullPolicy=Never \
            --set service.type=NodePort \
            --set service.nodePort=30800 \
            --wait

      - name: Wait for pod ready
        run: |
          kubectl wait --for=condition=ready pod \
            -l app.kubernetes.io/name=network-monitor \
            -n network-monitor \
            --timeout=120s

      - name: Health check
        run: curl -sf http://localhost:8000/health

      - name: Run API tests
        run: |
          curl -sf http://localhost:8000/api/topology
          curl -sf http://localhost:8000/api/links
          curl -sf http://localhost:8000/api/labs

      - name: Helm test
        run: helm test network-monitor -n network-monitor
```

## CI Pipeline (GitLab CI)

```yaml
# In .gitlab-ci.yml, add this job
e2e:
  stage: test
  image: docker:27
  services:
    - docker:27-dind
  variables:
    DOCKER_TLS_CERTDIR: "/certs"
    KIND_VERSION: "v0.24.0"
    HELM_VERSION: "v3.14.0"
  before_script:
    # Install kind
    - wget -qO /usr/local/bin/kind
        https://kind.sigs.k8s.io/dl/${KIND_VERSION}/kind-linux-amd64
    - chmod +x /usr/local/bin/kind
    # Install kubectl
    - wget -qO /usr/local/bin/kubectl
        https://dl.k8s.io/release/$(wget -qO- https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl
    - chmod +x /usr/local/bin/kubectl
    # Install helm
    - wget -qO- https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz
        | tar xz -C /usr/local/bin --strip-components=1 linux-amd64/helm
  script:
    - kind create cluster --name e2e --config kind-config.yaml
    - export KUBECONFIG=$(kind get kubeconfig-path --name e2e 2>/dev/null || echo "$HOME/.kube/config")

    # Install Cilium
    - helm repo add cilium https://helm.cilium.io/
    - helm install cilium cilium/cilium
        --namespace kube-system
        --set image.pullPolicy=IfNotPresent
        --set ipam.mode=kubernetes
        --set hubble.enabled=true
        --set hubble.relay.enabled=true
        --wait

    # Build and load
    - docker build -t network-monitor:e2e .
    - kind load docker-image network-monitor:e2e --name e2e

    # Deploy
    - helm install network-monitor helm/network-monitor
        --namespace network-monitor
        --create-namespace
        --set image.repository=network-monitor
        --set image.tag=e2e
        --set image.pullPolicy=Never
        --set service.type=NodePort
        --set service.nodePort=30800
        --wait

    # Wait and test
    - kubectl wait --for=condition=ready pod
        -l app.kubernetes.io/name=network-monitor
        -n network-monitor
        --timeout=120s
    - curl -sf http://localhost:8000/health
    - curl -sf http://localhost:8000/api/topology
    - curl -sf http://localhost:8000/api/links
  after_script:
    - kind delete cluster --name e2e 2>/dev/null || true
```

---

## Exposing Multiple Ports

If you need additional port mappings (e.g., Hubble UI, Cilium Hubble Relay):

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
      - containerPort: 31234
        hostPort: 8080
        protocol: TCP
  - role: worker
  - role: worker
```

Each mapping follows the same pattern: `hostPort` on your machine forwards to `containerPort` on the Kind control-plane node, which must match a NodePort in the cluster.

---

## Troubleshooting

### Port mapping not working

Verify the NodePort service is bound:

```bash
kubectl get svc -n network-monitor
# Should show NodePort 30800
```

Verify the Kind node is listening:

```bash
docker exec network-monitor-control-plane ss -tlnp | grep 30800
```

### Image not found after `kind load`

```bash
# Verify the image is available inside the node
docker exec network-monitor-control-plane crictl images | grep network-monitor
```

If missing, reload:

```bash
kind load docker-image network-monitor:dev --name network-monitor
```

### Cilium pods not ready

Kind uses a Docker bridge network which can be slow to initialize. Give it time:

```bash
kubectl get pods -n kube-system -l app.kubernetes.io/part-of=cilium -w
```

If stuck, restart the Cilium agent:

```bash
kubectl rollout restart daemonset/cilium -n kube-system
```
