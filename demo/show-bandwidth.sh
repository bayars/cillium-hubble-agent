#!/bin/bash
# Show bandwidth on all links
# Usage: ./show-bandwidth.sh [API_URL]

API_URL="${1:-http://10.0.0.108}"

echo "=== Network Link Bandwidth ==="
echo ""

# Get links and format output
curl -s "$API_URL/api/links" | jq -r '
  ["LINK", "STATE", "RX", "TX", "UTIL"],
  ["----", "-----", "--", "--", "----"],
  (.links[] | [
    .id,
    .state,
    (if .metrics.rx_bps >= 1073741824 then "\(.metrics.rx_bps / 1073741824 | . * 10 | floor / 10)_Gbps"
     elif .metrics.rx_bps >= 1048576 then "\(.metrics.rx_bps / 1048576 | floor)_Mbps"
     elif .metrics.rx_bps >= 1024 then "\(.metrics.rx_bps / 1024 | floor)_Kbps"
     else "\(.metrics.rx_bps | floor)_bps" end),
    (if .metrics.tx_bps >= 1073741824 then "\(.metrics.tx_bps / 1073741824 | . * 10 | floor / 10)_Gbps"
     elif .metrics.tx_bps >= 1048576 then "\(.metrics.tx_bps / 1048576 | floor)_Mbps"
     elif .metrics.tx_bps >= 1024 then "\(.metrics.tx_bps / 1024 | floor)_Kbps"
     else "\(.metrics.tx_bps | floor)_bps" end),
    "\(.metrics.utilization * 100 | floor)%"
  ]) | @tsv
' | column -t | sed 's/_/ /g'

echo ""
echo "Legend: RX=Receive, TX=Transmit, UTIL=Utilization"
