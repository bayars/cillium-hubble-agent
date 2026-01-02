# Network Monitor

Real-time network topology and link state monitoring system for Kubernetes environments with Cilium CNI.

## Overview

Network Monitor provides visibility into network link states and bandwidth utilization through:
- **Real-time link state tracking** (active/idle/down)
- **Bandwidth metrics** (rx/tx bps, packets, utilization)
- **REST API** for topology queries and updates
- **WebSocket streaming** for live UI updates
- **Cilium Hubble integration** for eBPF-based flow visibility

---

## Architecture

### System Components

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              KUBERNETES CLUSTER                              │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │                         DATA PLANE (per node)                        │    │
│  │  ┌─────────┐    ┌─────────┐    ┌─────────┐    ┌─────────┐          │    │
│  │  │  Pod A  │    │  Pod B  │    │  Pod C  │    │  Pod D  │          │    │
│  │  └────┬────┘    └────┬────┘    └────┬────┘    └────┬────┘          │    │
│  │       │              │              │              │                │    │
│  │       └──────────────┴──────────────┴──────────────┘                │    │
│  │                            │                                         │    │
│  │                    ┌───────┴───────┐                                │    │
│  │                    │  Cilium eBPF  │  ← Kernel-level packet tracing │    │
│  │                    └───────┬───────┘                                │    │
│  │                            │                                         │    │
│  │                    ┌───────┴───────┐                                │    │
│  │                    │ Hubble Agent  │  ← Per-node flow aggregation   │    │
│  │                    │  (DaemonSet)  │                                │    │
│  │                    └───────┬───────┘                                │    │
│  └────────────────────────────┼─────────────────────────────────────────┘    │
│                               │ gRPC                                         │
│                               ▼                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │                        CONTROL PLANE                                 │    │
│  │                                                                      │    │
│  │   ┌────────────────┐          ┌─────────────────────────────────┐   │    │
│  │   │  Hubble Relay  │  gRPC    │      Network Monitor API        │   │    │
│  │   │  (Deployment)  │─────────►│        (Deployment)             │   │    │
│  │   │                │          │                                  │   │    │
│  │   │  Aggregates    │          │  ┌───────────┐  ┌────────────┐  │   │    │
│  │   │  flows from    │          │  │  Link     │  │  Event     │  │   │    │
│  │   │  all nodes     │          │  │  State    │  │  Bus       │  │   │    │
│  │   │                │          │  │  Service  │  │  (pub/sub) │  │   │    │
│  │   └────────────────┘          │  └───────────┘  └────────────┘  │   │    │
│  │                               │         │              │         │   │    │
│  │                               │         ▼              ▼         │   │    │
│  │                               │  ┌─────────────────────────┐    │   │    │
│  │                               │  │   REST API + WebSocket  │    │   │    │
│  │                               │  │   /api/*    /ws/*       │    │   │    │
│  │                               │  └───────────┬─────────────┘    │   │    │
│  │                               └──────────────┼──────────────────┘   │    │
│  └──────────────────────────────────────────────┼───────────────────────┘    │
│                                                 │                            │
│  ┌──────────────────────────────────────────────┼───────────────────────┐    │
│  │                      GATEWAY API                                      │    │
│  │   ┌──────────────┐                           │                       │    │
│  │   │   Gateway    │◄──────────────────────────┘                       │    │
│  │   │   (envoy)    │     HTTPRoute: /api/*, /ws/*, /docs               │    │
│  │   │              │                                                    │    │
│  │   │  LoadBalancer│                                                    │    │
│  │   │  10.0.0.108  │                                                    │    │
│  │   └──────┬───────┘                                                    │    │
│  └──────────┼────────────────────────────────────────────────────────────┘    │
│             │                                                                 │
└─────────────┼─────────────────────────────────────────────────────────────────┘
              │
              ▼
    ┌─────────────────┐
    │   External      │
    │   Clients       │
    │   (curl, UI)    │
    └─────────────────┘
```

### Data Flow

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   Cilium     │     │   Hubble     │     │   Hubble     │     │   Network    │
│   eBPF       │────►│   Agent      │────►│   Relay      │────►│   Monitor    │
│   (kernel)   │     │   (per-node) │     │   (central)  │     │   API        │
└──────────────┘     └──────────────┘     └──────────────┘     └──────────────┘
                                                                      │
                                                                      ▼
                                                               ┌──────────────┐
                                                               │  WebSocket   │
                                                               │  Clients     │
                                                               │  (Cytoscape) │
                                                               └──────────────┘
```

### Link States

| State | Description | Trigger |
|-------|-------------|---------|
| `active` | Traffic flowing | Packets observed in last N seconds |
| `idle` | Link up, no traffic | No packets for N seconds |
| `down` | Link failure | Interface down or endpoint deleted |

---

## Example Topology

The demo includes a spine-leaf topology with traffic generators:

```
                              ┌─────────────────┐
                              │     spine1      │
                              │   (SR Linux)    │
                              │   10Gbps uplinks│
                              └────┬───────┬────┘
                                   │       │
                    ┌──────────────┘       └──────────────┐
                    │ e1-1                          e1-2  │
                    │ spine1-leaf1              spine1-leaf2
                    │                                     │
               eth1 │                                eth1 │
              ┌─────┴─────┐                        ┌─────┴─────┐
              │   leaf1   │◄──────────────────────►│   leaf2   │
              │   (FRR)   │ eth2    leaf1-leaf2 eth2   (FRR)   │
              │  1Gbps    │                        │  1Gbps    │
              └─────┬─────┘                        └─────┬─────┘
                    │ eth3                          eth3 │
                    │ leaf1-tgen1              leaf2-tgen2
                    │                                     │
               eth1 │                                eth1 │
              ┌─────┴─────┐                        ┌─────┴─────┐
              │   tgen1   │                        │   tgen2   │
              │  (iperf3) │ ─ ─ ─ traffic ─ ─ ─ ─► │  (iperf3) │
              │  Alpine   │                        │  Alpine   │
              └───────────┘                        └───────────┘

Traffic Path: tgen1 → leaf1 → spine1 → leaf2 → tgen2
```

### Topology Nodes

| Node | Type | Platform | Role | Interfaces |
|------|------|----------|------|------------|
| spine1 | router | SR Linux | Spine | e1-1, e1-2 |
| leaf1 | router | FRR | Leaf | eth1, eth2, eth3 |
| leaf2 | router | FRR | Leaf | eth1, eth2, eth3 |
| tgen1 | host | Alpine/iperf3 | Traffic Generator | eth1 |
| tgen2 | host | Alpine/iperf3 | Traffic Generator | eth1 |

### Topology Links

| Link ID | Source | Target | Speed | Description |
|---------|--------|--------|-------|-------------|
| spine1-leaf1 | spine1:e1-1 | leaf1:eth1 | 10Gbps | Spine to Leaf uplink |
| spine1-leaf2 | spine1:e1-2 | leaf2:eth1 | 10Gbps | Spine to Leaf uplink |
| leaf1-leaf2 | leaf1:eth2 | leaf2:eth2 | 1Gbps | Leaf-to-Leaf cross-link |
| leaf1-tgen1 | leaf1:eth3 | tgen1:eth1 | 1Gbps | Leaf to Traffic Gen |
| leaf2-tgen2 | leaf2:eth3 | tgen2:eth1 | 1Gbps | Leaf to Traffic Gen |

---

## Requirements

### Kubernetes Components

| Component | Version | Purpose |
|-----------|---------|---------|
| Kubernetes | 1.28+ | Container orchestration |
| Cilium CNI | 1.14+ | eBPF-based networking |
| Hubble | Enabled | Flow visibility |
| Gateway API | 1.0+ | External access (optional) |
| Clabernetes | 0.5+ | Lab topology (optional) |

### Cilium Configuration

Hubble must be enabled in your Cilium installation:

```bash
# Check Cilium status
cilium status

# Verify Hubble is enabled
cilium hubble status

# If not enabled, upgrade Cilium with Hubble
helm upgrade cilium cilium/cilium \
  --namespace kube-system \
  --set hubble.enabled=true \
  --set hubble.relay.enabled=true \
  --set hubble.ui.enabled=true
```

### RBAC Requirements

The Network Monitor requires the following permissions:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: network-monitor
rules:
  # Access to CiliumEndpoint resources for topology discovery
  - apiGroups: ["cilium.io"]
    resources: ["ciliumendpoints"]
    verbs: ["get", "list", "watch"]

  # Access to pods for metadata correlation
  - apiGroups: [""]
    resources: ["pods", "nodes"]
    verbs: ["get", "list", "watch"]
```

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: network-monitor
subjects:
  - kind: ServiceAccount
    name: network-monitor
    namespace: network-monitor
roleRef:
  kind: ClusterRole
  name: network-monitor
  apiGroup: rbac.authorization.k8s.io
```

### Gateway API (Optional)

For external access via Gateway API:

```yaml
# ReferenceGrant allows cross-namespace backend references
apiVersion: gateway.networking.k8s.io/v1beta1
kind: ReferenceGrant
metadata:
  name: allow-default-to-network-monitor
  namespace: network-monitor
spec:
  from:
    - group: gateway.networking.k8s.io
      kind: HTTPRoute
      namespace: default
  to:
    - group: ""
      kind: Service
      name: network-monitor

---
# HTTPRoute for API endpoints
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: network-monitor-api
  namespace: default
spec:
  parentRefs:
    - name: gateway
      namespace: default
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /api
      backendRefs:
        - name: network-monitor
          namespace: network-monitor
          port: 8000
    - matches:
        - path:
            type: PathPrefix
            value: /ws
      backendRefs:
        - name: network-monitor
          namespace: network-monitor
          port: 8000
```

---

## Installation

### 1. Deploy Network Monitor

```bash
# Create namespace and deploy
kubectl apply -f k8s/deployment.yaml

# Verify deployment
kubectl get pods -n network-monitor
kubectl logs -n network-monitor -l app.kubernetes.io/name=network-monitor
```

### 2. Configure Gateway API (Optional)

```bash
# Apply HTTPRoute for external access
kubectl apply -f k8s/gateway.yaml

# Get Gateway LoadBalancer IP
kubectl get gateway -n default gateway -o jsonpath='{.status.addresses[0].value}'
```

### 3. Deploy Demo Topology (Optional)

```bash
# Deploy Clabernetes topology
kubectl apply -f demo/clabernetes-topology.yaml

# Watch pods come up
kubectl get pods -n clab -w
```

---

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_LEVEL` | INFO | Logging level (DEBUG, INFO, WARNING, ERROR) |
| `DEMO_MODE` | false | Initialize with demo topology |
| `DISCOVERY_MODE` | sysfs | Discovery mode: `sysfs` or `hubble` |
| `HUBBLE_RELAY_ADDR` | hubble-relay:4245 | Hubble Relay gRPC address |
| `IDLE_TIMEOUT_SECONDS` | 5 | Seconds before marking link as idle |
| `POLL_INTERVAL_MS` | 100 | Polling interval for sysfs mode |

### Discovery Modes

**sysfs mode** (standalone/VM):
- Uses Linux Netlink for link up/down events
- Polls `/sys/class/net/<iface>/statistics/` for traffic bytes
- Requires `NET_ADMIN` capability

**hubble mode** (Kubernetes with Cilium):
- Connects to Hubble Relay for eBPF flow data
- Watches CiliumEndpoint CRD for topology
- Recommended for Kubernetes deployments

---

## API Reference

### Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/topology` | Full topology (nodes + edges) |
| POST | `/api/topology/nodes` | Add a node |
| DELETE | `/api/topology/nodes/{id}` | Remove a node |
| POST | `/api/topology/links` | Add a link |
| DELETE | `/api/topology/links/{id}` | Remove a link |
| GET | `/api/links` | All links with metrics |
| GET | `/api/links?state=active` | Filter links by state |
| GET | `/api/links/{id}` | Single link details |
| PUT | `/api/links/{id}/state?state=X` | Update link state |
| PUT | `/api/links/{id}/metrics` | Update link metrics |
| POST | `/api/events` | Submit link state event |
| GET | `/api/events/history` | Event history |
| GET | `/health` | Health check |
| GET | `/docs` | OpenAPI documentation |
| WS | `/ws/events` | Stream events to clients |
| WS | `/ws/agent` | Agent bidirectional connection |

### Example: Update Link Metrics

```bash
curl -X PUT "http://10.0.0.108/api/links/spine1-leaf1/metrics" \
  -H "Content-Type: application/json" \
  -d '{
    "rx_bps": 100000000,
    "tx_bps": 5000000,
    "rx_pps": 82000,
    "tx_pps": 4100,
    "utilization": 0.1
  }'
```

### Example: Get Topology

```bash
curl -s http://10.0.0.108/api/topology | jq '.nodes[].id, .edges[].id'
```

---

## Demo Scripts

Located in the `demo/` directory:

| Script | Usage | Description |
|--------|-------|-------------|
| `start-traffic.sh` | `./start-traffic.sh [mbps] [seconds]` | Simulate traffic with progress bar |
| `dashboard.sh` | `./dashboard.sh [url] [refresh]` | Visual bandwidth dashboard |
| `show-bandwidth.sh` | `./show-bandwidth.sh [url]` | Simple bandwidth table |
| `continuous-traffic.sh` | `./continuous-traffic.sh [mbps]` | Continuous traffic with real iperf3 |

---

## Example Demo Outputs

### Traffic Generator Output

```
╔════════════════════════════════════════════════════════╗
║           NETWORK MONITOR - TRAFFIC GENERATOR          ║
╚════════════════════════════════════════════════════════╝

Bandwidth: 100 Mbps
Duration:  30 seconds
API:       http://10.0.0.108

Traffic Path: tgen1 → leaf1 → spine1 → leaf2 → tgen2

[1/3] Starting traffic simulation...
      ✓ Links activated
      ✓ Metrics updated (100 Mbps)

[2/3] Traffic flowing for 30 seconds...
      [████████████████████] 100% | 98 Mbps

[3/3] Traffic complete. Setting links to idle...
      ✓ Links set to idle

═══════════════════════════════════════════════════════════
  Traffic simulation complete!
═══════════════════════════════════════════════════════════
```

### Dashboard Output (Idle State)

```
╔══════════════════════════════════════════════════════════════════════╗
║                  NETWORK MONITOR - LINK BANDWIDTH                    ║
╠══════════════════════════════════════════════════════════════════════╣
║ LINK           │ STATE    │          RX │          TX │    UTIL ║
╠══════════════════════════════════════════════════════════════════════╣
║ spine1-leaf1   │ ○ idle   │     500 bps │     500 bps │      0% ║
║ spine1-leaf2   │ ○ idle   │     500 bps │     500 bps │      0% ║
║ leaf1-leaf2    │ ○ idle   │    1000 bps │    1000 bps │      0% ║
║ leaf1-tgen1    │ ○ idle   │     500 bps │     500 bps │      0% ║
║ leaf2-tgen2    │ ○ idle   │     500 bps │     500 bps │      0% ║
╚══════════════════════════════════════════════════════════════════════╝

API: http://10.0.0.108  |  2026-01-01 14:45:56
States: ● active  ○ idle  ✗ down
```

### Dashboard Output (Active Traffic)

```
╔══════════════════════════════════════════════════════════════════════╗
║                  NETWORK MONITOR - LINK BANDWIDTH                    ║
╠══════════════════════════════════════════════════════════════════════╣
║ LINK           │ STATE    │          RX │          TX │    UTIL ║
╠══════════════════════════════════════════════════════════════════════╣
║ spine1-leaf1   │ ● active │    100 Mbps │      5 Mbps │      1% ║
║ spine1-leaf2   │ ● active │      5 Mbps │    100 Mbps │      1% ║
║ leaf1-leaf2    │ ○ idle   │    1000 bps │    1000 bps │      0% ║
║ leaf1-tgen1    │ ● active │    100 Mbps │      5 Mbps │     10% ║
║ leaf2-tgen2    │ ● active │      5 Mbps │    100 Mbps │     10% ║
╚══════════════════════════════════════════════════════════════════════╝

API: http://10.0.0.108  |  2026-01-01 14:50:23
States: ● active  ○ idle  ✗ down
```

### API Response Example

```bash
$ curl -s http://10.0.0.108/api/links | jq '.links[0]'
```

```json
{
  "id": "spine1-leaf1",
  "source": "spine1",
  "target": "leaf1",
  "source_interface": "e1-1",
  "target_interface": "eth1",
  "state": "active",
  "metrics": {
    "rx_bps": 100000000,
    "tx_bps": 5000000,
    "rx_pps": 82000,
    "tx_pps": 4100,
    "rx_bytes_total": 0,
    "tx_bytes_total": 0,
    "utilization": 0.01,
    "latency_ms": null,
    "packet_loss": null
  },
  "speed_mbps": 10000,
  "mtu": 1500,
  "last_updated": "2026-01-01T19:50:23.456789",
  "metadata": {}
}
```

### WebSocket Event Stream

```bash
$ websocat ws://10.0.0.108/ws/events
```

```json
{"event_type":"link_state_change","link_id":"spine1-leaf1","old_state":"idle","new_state":"active","timestamp":"2026-01-01T19:50:00Z"}
{"event_type":"metrics_update","link_id":"spine1-leaf1","metrics":{"rx_bps":100000000,"tx_bps":5000000},"timestamp":"2026-01-01T19:50:01Z"}
{"event_type":"link_state_change","link_id":"leaf1-tgen1","old_state":"idle","new_state":"active","timestamp":"2026-01-01T19:50:00Z"}
```

---

## Troubleshooting

### Check Hubble Connectivity

```bash
# Verify Hubble Relay is running
kubectl get pods -n kube-system -l k8s-app=hubble-relay

# Test Hubble Relay from Network Monitor pod
kubectl exec -n network-monitor deploy/network-monitor -- \
  curl -s hubble-relay.kube-system:4245
```

### Check Network Monitor Logs

```bash
kubectl logs -n network-monitor -l app.kubernetes.io/name=network-monitor -f
```

### Verify RBAC Permissions

```bash
kubectl auth can-i list ciliumendpoints --as=system:serviceaccount:network-monitor:network-monitor
kubectl auth can-i list pods --as=system:serviceaccount:network-monitor:network-monitor
```

---

## License

MIT License
