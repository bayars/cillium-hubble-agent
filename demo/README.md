# Network Monitor Demo

Demo topology and scripts for testing the Network Monitor API.

See the main [README.md](../README.md) for full documentation.

## Quick Start

```bash
# Set API URL (Gateway LoadBalancer IP)
export API_URL=http://10.0.0.108

# Run traffic simulation
./start-traffic.sh 100 30

# View live dashboard
./dashboard.sh $API_URL 2
```

## Topology

```
                    ┌─────────────┐
                    │   spine1    │
                    │  (SR Linux) │
                    └──┬───────┬──┘
                       │       │
           ┌───────────┘       └───────────┐
           │                               │
      ┌────┴────┐                     ┌────┴────┐
      │  leaf1  │◄───────────────────►│  leaf2  │
      │  (FRR)  │                     │  (FRR)  │
      └────┬────┘                     └────┬────┘
           │                               │
      ┌────┴────┐                     ┌────┴────┐
      │  tgen1  │ ─ ─ ─ traffic ─ ─ ─►│  tgen2  │
      │ (iperf) │                     │ (iperf) │
      └─────────┘                     └─────────┘
```

## Scripts

| Script | Description |
|--------|-------------|
| `start-traffic.sh [mbps] [seconds]` | Simulate traffic with progress bar |
| `dashboard.sh [url] [refresh]` | Visual bandwidth dashboard |
| `show-bandwidth.sh [url]` | Simple bandwidth table |
| `continuous-traffic.sh [mbps]` | Continuous iperf3 traffic |

## Deploy Clabernetes Topology

```bash
kubectl apply -f clabernetes-topology.yaml
kubectl get pods -n clab -w
```

## Cleanup

```bash
kubectl delete -f clabernetes-topology.yaml
```
