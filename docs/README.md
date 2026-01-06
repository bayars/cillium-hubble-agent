# Network Monitor Documentation

Technical documentation for the Network Monitor agent and API.

## Contents

| Document | Description |
|----------|-------------|
| [Discovery Modes](discovery-modes.md) | Detailed comparison of sysfs vs hubble discovery modes |
| [Hubble Integration](hubble-integration.md) | Guide to Cilium Hubble integration and custom agent development |
| [Agent Architecture](agent-architecture.md) | Internal architecture of the monitoring agent |

## Quick Links

- [Main README](../README.md) - Project overview and quick start
- [Demo Scripts](../demo/README.md) - Demo topology and traffic simulation
- [API Reference](../api/README.md) - REST/WebSocket API documentation

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         Network Monitor System                          │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │                        Agent Layer                               │   │
│  │                                                                   │   │
│  │   ┌─────────────────┐           ┌─────────────────┐             │   │
│  │   │   sysfs Mode    │           │   hubble Mode   │             │   │
│  │   │                 │           │                 │             │   │
│  │   │ • NetlinkMonitor│           │ • HubbleMonitor │             │   │
│  │   │ • SysfsPoller   │           │ • CiliumDiscovery             │   │
│  │   └────────┬────────┘           └────────┬────────┘             │   │
│  │            │                              │                      │   │
│  │            └──────────┬───────────────────┘                      │   │
│  │                       ▼                                          │   │
│  │              ┌─────────────────┐                                 │   │
│  │              │ EventPublisher  │                                 │   │
│  │              └────────┬────────┘                                 │   │
│  └───────────────────────┼──────────────────────────────────────────┘   │
│                          │ HTTP/WebSocket                               │
│  ┌───────────────────────▼──────────────────────────────────────────┐   │
│  │                        API Layer                                  │   │
│  │                                                                   │   │
│  │   ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐  │   │
│  │   │  Topology   │  │   Links     │  │   Events / WebSocket    │  │   │
│  │   │  Service    │  │   Service   │  │   Service               │  │   │
│  │   └─────────────┘  └─────────────┘  └─────────────────────────┘  │   │
│  └───────────────────────────────────────────────────────────────────┘   │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

## Deployment Scenarios

### Scenario 1: Standalone/VM Monitoring

Use `sysfs` mode to monitor local network interfaces:

```bash
python -m agent.main \
  --discovery-mode sysfs \
  --api-url http://api-server:8000/api/events \
  --interfaces eth0,eth1
```

### Scenario 2: Kubernetes with Cilium

Use `hubble` mode to monitor pod-to-pod flows:

```bash
python -m agent.main \
  --discovery-mode hubble \
  --hubble-relay hubble-relay.kube-system:4245 \
  --api-url http://network-monitor.monitoring:8000/api/events
```

### Scenario 3: Demo/Testing

Use the API directly without an agent:

```bash
# Setup topology
./demo/setup-topology.sh http://10.0.0.108 --clear

# Simulate traffic
./demo/start-traffic.sh 500 30

# View dashboard
./demo/dashboard.sh http://10.0.0.108 2
```
