# Network Monitor Demo

Demo topology and scripts for testing the Network Monitor API.

See the main [README.md](../README.md) for full documentation.

## Quick Start

```bash
# Set API URL (Gateway LoadBalancer IP)
export API_URL=http://10.0.0.108

# Run real iperf3 traffic (requires Clabernetes pods)
./start-traffic.sh 100 30

# Or continuous real traffic
./continuous-traffic.sh 200

# View live dashboard (auto-refresh every 2s)
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
      │  leaf1  │                     │  leaf2  │
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
| `start-traffic.sh [mbps] [seconds]` | Run iperf3 traffic, report real measurements |
| `continuous-traffic.sh [mbps]` | Continuous iperf3 with live metric updates |
| `traffic.sh [lab] [mbps] [seconds]` | Lab-aware traffic generator |
| `dashboard.sh [url] [refresh]` | Live dashboard with data source attribution |
| `show-bandwidth.sh [url]` | Simple metrics table |
| `list-labs.sh [url]` | List deployed labs |

## Data Sources

The dashboard shows a **SOURCE** column so you always know where metrics came from:

| Source | What it means |
|--------|---------------|
| `hubble` | Real Hubble flow data (flow counts, NOT bandwidth) |
| `iperf3` | Real measured throughput from iperf3 between pods |
| `sysfs` | Real kernel interface counters |
| `external` | External collector (gNMI, SNMP, Prometheus) |

Scripts require real tgen pods to be available and will exit with an error if not found.

## Interface Metrics

The sidecar agent is injected into every Clabernetes topology pod via `extraContainers`. It reads kernel counters directly and captures **all traffic** — ping, ssh, scp, routing protocols, iperf3 — on every interface (linecards, CPM, mgmt).

### Setup (in Clabernetes Helm values)

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

### View metrics

```bash
# View all nodes with interface metrics
curl -s http://$API_URL/api/interfaces/all | jq '.[].node_id'

# View interfaces for a specific node
curl -s "http://$API_URL/api/interfaces?node_id=<NODE_ID>" | jq '.interfaces[] | {name, state, rx_bps, tx_bps}'
```

Configure the collection interval via `POLL_INTERVAL_MS` (default: 2000ms). Set to `500` for near-real-time updates.

## Deploy Clabernetes Topology

```bash
kubectl apply -f clabernetes-topology.yaml
kubectl get pods -n clab -w
```

## Cleanup

```bash
kubectl delete -f clabernetes-topology.yaml
```
