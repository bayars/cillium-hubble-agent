#!/bin/bash
# Network Monitor Dashboard - Per-Interface Bandwidth
# Usage: ./dashboard.sh [API_URL] [refresh_seconds]
#
# Displays real per-interface bandwidth from kernel counters.
# All metrics are collected from /proc/net/dev via the collector.

API_URL="${1:-http://localhost:8000}"
REFRESH="${2:-2}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
GRAY='\033[0;90m'
NC='\033[0m'
BOLD='\033[1m'

show_dashboard() {
    echo -e "${BOLD}======================== NETWORK MONITOR DASHBOARD ========================${NC}"
    echo -e "API: ${BLUE}$API_URL${NC}  |  $(date '+%H:%M:%S')  |  Refresh: ${REFRESH}s"
    echo -e "${BOLD}===========================================================================${NC}"
    echo ""

    curl -s "$API_URL/api/interfaces/all" 2>/dev/null | python3 -c "
import json, sys

try:
    data = json.load(sys.stdin)
except:
    print('  (no data)')
    sys.exit(0)

# Filter to clab topology nodes
clab = [n for n in data if any(k in n['node_id'] for k in ('tgen','leaf','spine'))]
if not clab:
    print('  No topology nodes found.')
    sys.exit(0)

def fmt(bps):
    if bps >= 1e9: return f'{bps/1e9:.1f} Gbps'
    if bps >= 1e6: return f'{bps/1e6:.1f} Mbps'
    if bps >= 1e3: return f'{bps/1e3:.0f} Kbps'
    if bps > 0: return f'{bps:.0f} bps'
    return '-'

def short_name(nid):
    for part in nid.split('-'):
        if any(part.startswith(k) for k in ('spine','leaf','tgen')):
            return part
    return nid[:12]

for node in sorted(clab, key=lambda n: n['node_id']):
    nid = short_name(node['node_id'].split('/')[-1])
    ifaces = [i for i in node['interfaces']
              if i['name'] not in ('docker0','lo')
              and not i['name'].startswith('veth')
              and not i['name'].startswith('br-')
              and not i['name'].startswith('vx-')]
    if not ifaces:
        continue

    total_rx = sum(i['rx_bps'] for i in ifaces)
    total_tx = sum(i['tx_bps'] for i in ifaces)
    active = '\033[0;32m' if total_rx > 1000 or total_tx > 1000 else '\033[0;90m'
    print(f'{active}{nid}\033[0m')
    for iface in sorted(ifaces, key=lambda i: i['name']):
        rx = iface['rx_bps']
        tx = iface['tx_bps']
        bar_rx = int(min(rx / 1e6, 50))  # 1 char per Mbps, max 50
        bar_tx = int(min(tx / 1e6, 50))
        rx_bar = '\033[0;32m' + '|' * bar_rx + '\033[0m' if bar_rx > 0 else ''
        tx_bar = '\033[0;34m' + '|' * bar_tx + '\033[0m' if bar_tx > 0 else ''
        marker = ''
        if rx > 1e6 or tx > 1e6:
            marker = ' \033[0;33m***\033[0m'
        elif rx > 1000 or tx > 1000:
            marker = ' \033[0;36m*\033[0m'
        print(f'  {iface[\"name\"]:20s} RX={fmt(rx):>12s}  TX={fmt(tx):>12s}  {rx_bar}{tx_bar}{marker}')
    print()
" 2>/dev/null

    echo -e "${GRAY}Legend: ${GREEN}|||${NC}=RX  ${BLUE}|||${NC}=TX  (1 bar = 1 Mbps)  ${YELLOW}***${NC}=Mbps+  ${CYAN}*${NC}=Kbps+${NC}"
}

if [ "$REFRESH" -gt 0 ] 2>/dev/null; then
    while true; do
        clear
        show_dashboard
        echo -e "\n${GRAY}Ctrl+C to stop${NC}"
        sleep "$REFRESH"
    done
else
    show_dashboard
fi
