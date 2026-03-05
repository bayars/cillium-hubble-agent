# Network Monitor

Real-time network topology and link state monitoring for Kubernetes environments with Cilium/Hubble.

## Features

- Real-time link state tracking (active/idle/down)
- Bandwidth metrics (rx/tx bps, packets, utilization)
- REST API and WebSocket streaming for Cytoscape visualization
- Cilium Hubble integration for eBPF-based flow visibility
- Multi-lab topology support with Clabernetes

## Quick Start

### Helm (Recommended)

```bash
helm install network-monitor oci://ghcr.io/bayars/charts/network-monitor \
  --namespace network-monitor \
  --create-namespace
```

Or from source:

```bash
helm install network-monitor helm/network-monitor \
  --namespace network-monitor \
  --create-namespace
```

### kubectl

```bash
kubectl apply -f k8s/deployment.yaml
```

### Verify

```bash
kubectl get pods -n network-monitor
curl http://<SERVICE_URL>/health
curl http://<SERVICE_URL>/api/topology
```

## Configuration

Key Helm values:

```bash
helm install network-monitor helm/network-monitor \
  --namespace network-monitor \
  --create-namespace \
  --set config.discoveryMode=hubble \
  --set config.demoMode="true" \
  --set service.type=LoadBalancer
```

| Variable | Default | Description |
|----------|---------|-------------|
| `config.discoveryMode` | `hubble` | `hubble` or `sysfs` |
| `config.demoMode` | `"false"` | Initialize demo topology |
| `config.logLevel` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `service.type` | `ClusterIP` | Service exposure type |
| `ingress.enabled` | `false` | Enable Ingress |

See [docs/helm.md](docs/helm.md) for full values reference.

## Development

```bash
uv sync --dev
uv run uvicorn api.main:app --reload    # Run API
uv run pytest tests/ -v                  # Run tests
```

## Documentation

| Topic | Link |
|-------|------|
| Helm Chart | [docs/helm.md](docs/helm.md) |
| Platform Deployment (GKE, EKS, AKS, on-prem) | [docs/deployment.md](docs/deployment.md) |
| API Reference | [docs/api.md](docs/api.md) |
| Architecture & Discovery Modes | [docs/README.md](docs/README.md) |
| Demo Scripts | [demo/README.md](demo/README.md) |

## License

MIT License
