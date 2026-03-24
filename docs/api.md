# API Reference

## Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check |
| GET | `/docs` | OpenAPI documentation |
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
| PUT | `/api/interfaces` | Push per-interface metrics (node_id in body) |
| GET | `/api/interfaces?node_id=X` | Get interface metrics for a node |
| GET | `/api/interfaces/all` | Get interface metrics for all nodes |
| POST | `/api/events` | Submit link state event |
| GET | `/api/events/history` | Event history |
| WS | `/ws/events` | Stream events to clients |
| WS | `/ws/agent` | Agent bidirectional connection |

## Examples

### Update Link Metrics

```bash
curl -X PUT "http://<SERVICE_URL>/api/links/spine1-leaf1/metrics" \
  -H "Content-Type: application/json" \
  -d '{
    "rx_bps": 100000000,
    "tx_bps": 5000000,
    "rx_pps": 82000,
    "tx_pps": 4100,
    "utilization": 0.1
  }'
```

### Get Topology

```bash
curl -s http://<SERVICE_URL>/api/topology | jq '.nodes[].id, .edges[].id'
```

### Get Active Links

```bash
curl -s http://<SERVICE_URL>/api/links?state=active | jq '.links[]'
```

### WebSocket Event Stream

```bash
websocat ws://<SERVICE_URL>/ws/events
```

Example events:

```json
{"event_type":"link_state_change","link_id":"spine1-leaf1","old_state":"idle","new_state":"active","timestamp":"2026-01-01T19:50:00Z"}
{"event_type":"metrics_update","link_id":"spine1-leaf1","metrics":{"rx_bps":100000000,"tx_bps":5000000},"timestamp":"2026-01-01T19:50:01Z"}
```

### Link Response Schema

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

## Interface Metrics

### Push Interface Metrics

```bash
curl -X PUT "http://<SERVICE_URL>/api/interfaces" \
  -H "Content-Type: application/json" \
  -d '{
    "node_id": "spine1",
    "interfaces": [
      {
        "name": "ethernet-1/1",
        "state": "up",
        "rx_bps": 5000000.0,
        "tx_bps": 1200000.0,
        "rx_pps": 4000.0,
        "tx_pps": 1000.0,
        "rx_bytes_total": 500000000,
        "tx_bytes_total": 120000000,
        "rx_packets_total": 400000,
        "tx_packets_total": 100000,
        "rx_errors": 0,
        "tx_errors": 0,
        "rx_dropped": 0,
        "tx_dropped": 0
      }
    ],
    "poll_interval_ms": 2000,
    "data_source": "sysfs"
  }'
```

### Get Node Interfaces

```bash
curl -s "http://<SERVICE_URL>/api/interfaces?node_id=spine1" | jq '.interfaces[]'
```

### Get All Interfaces

```bash
curl -s http://<SERVICE_URL>/api/interfaces/all | jq '.[].node_id'
```

### Interface Response Schema

```json
{
  "node_id": "spine1",
  "node_label": "SPINE1",
  "interfaces": [
    {
      "name": "ethernet-1/1",
      "state": "up",
      "rx_bps": 5000000.0,
      "tx_bps": 1200000.0,
      "rx_pps": 4000.0,
      "tx_pps": 1000.0,
      "rx_bytes_total": 500000000,
      "tx_bytes_total": 120000000,
      "rx_packets_total": 400000,
      "tx_packets_total": 100000,
      "rx_errors": 0,
      "tx_errors": 0,
      "rx_dropped": 0,
      "tx_dropped": 0
    }
  ],
  "count": 1,
  "timestamp": "2026-01-01T19:50:23.456789"
}
```

## Link States

| State | Description | Trigger |
|-------|-------------|---------|
| `active` | Traffic flowing | Packets observed in last N seconds |
| `idle` | Link up, no traffic | No packets for N seconds |
| `down` | Link failure | Interface down or endpoint deleted |
| `unknown` | Not determined | Initial state |
