#!/bin/bash
# Show link metrics from the API
# Usage: ./show-bandwidth.sh [API_URL]
#
# Displays actual data from the API with source attribution.
# The SOURCE column tells you where the data came from:
#   hubble     = Real Hubble flow counts (NOT bandwidth)
#   iperf3     = Real iperf3 measured throughput
#   sysfs      = Real kernel byte counters
#   external   = External collector (gNMI, SNMP)

API_URL="${1:-http://localhost:8000}"

# Format bps to human readable
format_bps() {
    local bps=$1
    if [ -z "$bps" ] || [ "$bps" = "0" ] || [ "$bps" = "0.0" ]; then
        echo "0"
    elif awk "BEGIN {exit !($bps >= 1000000000)}" 2>/dev/null; then
        awk "BEGIN {printf \"%.1f Gbps\", $bps / 1000000000}"
    elif awk "BEGIN {exit !($bps >= 1000000)}" 2>/dev/null; then
        awk "BEGIN {printf \"%.1f Mbps\", $bps / 1000000}"
    elif awk "BEGIN {exit !($bps >= 1000)}" 2>/dev/null; then
        awk "BEGIN {printf \"%.0f Kbps\", $bps / 1000}"
    else
        awk "BEGIN {printf \"%.0f bps\", $bps}"
    fi
}

echo "=== Per-Interface Bandwidth ==="
echo ""

curl -s "$API_URL/api/interfaces/all" | python3 -c "
import json, sys
data = json.load(sys.stdin)
# Filter to clab nodes only
clab_nodes = [n for n in data if 'tgen' in n['node_id'] or 'leaf' in n['node_id'] or 'spine' in n['node_id']]

def fmt(bps):
    if bps >= 1e9: return f'{bps/1e9:.1f} Gbps'
    if bps >= 1e6: return f'{bps/1e6:.1f} Mbps'
    if bps >= 1e3: return f'{bps/1e3:.0f} Kbps'
    if bps > 0: return f'{bps:.0f} bps'
    return '0'

print(f'{\"NODE\":<20s} {\"INTERFACE\":<20s} {\"STATE\":<6s} {\"RX\":<14s} {\"TX\":<14s} {\"RX_TOTAL\":<14s} {\"TX_TOTAL\":<14s}')
print(f'{\"----\":<20s} {\"--------\":<20s} {\"-----\":<6s} {\"---------\":<14s} {\"---------\":<14s} {\"--------\":<14s} {\"--------\":<14s}')

for node in sorted(clab_nodes, key=lambda n: n['node_id']):
    nid = node['node_id'].split('/')[-1]
    # Extract short name (e.g. spine1 from network-monitor-demo-spine1-xxx)
    parts = nid.split('-')
    for i, p in enumerate(parts):
        if any(p.startswith(k) for k in ('spine','leaf','tgen')):
            short = p
            break
    else:
        short = nid[:15]
    for iface in sorted(node['interfaces'], key=lambda i: i['name']):
        if iface['name'] in ('docker0', 'lo') or iface['name'].startswith('veth') or iface['name'].startswith('br-') or iface['name'].startswith('vx-'):
            continue
        rx = fmt(iface['rx_bps'])
        tx = fmt(iface['tx_bps'])
        rx_t = f'{iface[\"rx_bytes_total\"]:,}'
        tx_t = f'{iface[\"tx_bytes_total\"]:,}'
        marker = ' <--' if iface['rx_bps'] > 1000 or iface['tx_bps'] > 1000 else ''
        print(f'{short:<20s} {iface[\"name\"]:<20s} {iface[\"state\"]:<6s} {rx:<14s} {tx:<14s} {rx_t:<14s} {tx_t:<14s}{marker}')
"

echo ""
echo "Source: /proc/net/dev kernel counters (collected every ${POLL_INTERVAL_MS:-2000}ms)"
