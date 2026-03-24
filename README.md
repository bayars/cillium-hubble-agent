# Network Monitor

Real-time network topology and link state monitoring for Kubernetes environments with Cilium/Hubble.

## Features

- Real-time link state tracking (active/idle/down)
- Per-interface rx/tx metrics from real kernel counters — captures all traffic (ping, ssh, scp, routing protocols, etc.)
- Sidecar agent injected via Clabernetes `extraContainers` — sees all interfaces (linecards, CPM, mgmt)
- Configurable collection interval (`POLL_INTERVAL_MS`)
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

### Interface Metrics (Sidecar)

The sidecar agent is injected into every Clabernetes topology pod via `extraContainers`. Set it once in your Clabernetes Helm values — all topology pods get the sidecar automatically:

```yaml
# In Clabernetes Helm values:
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

See [k8s/sidecar-example.yaml](k8s/sidecar-example.yaml) for full examples.

### Network Monitor Helm Values

| Variable | Default | Description |
|----------|---------|-------------|
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
