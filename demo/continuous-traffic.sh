#!/bin/bash
# Continuous Traffic Generator with Real-time Metrics Update
# Usage: ./continuous-traffic.sh [bandwidth_mbps]
#
# Runs continuous traffic and updates the API metrics every second
# Press Ctrl+C to stop

BANDWIDTH="${1:-50}"  # Default 50 Mbps
API_URL="${API_URL:-http://10.0.0.108}"
NAMESPACE="clab"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m'

cleanup() {
    echo ""
    echo -e "${YELLOW}Stopping traffic...${NC}"

    # Kill iperf processes
    kubectl exec -n $NAMESPACE $TGEN1 -- pkill iperf3 2>/dev/null || true
    kubectl exec -n $NAMESPACE $TGEN2 -- pkill iperf3 2>/dev/null || true

    # Set links to idle
    for link in leaf1-tgen1 spine1-leaf1 spine1-leaf2 leaf2-tgen2; do
        curl -s -X PUT "$API_URL/api/links/$link/state?state=idle" > /dev/null 2>&1
        curl -s -X PUT "$API_URL/api/links/$link/metrics" \
            -H "Content-Type: application/json" \
            -d '{"rx_bps": 0, "tx_bps": 0, "utilization": 0}' > /dev/null 2>&1
    done

    echo -e "${GREEN}Traffic stopped. Links set to idle.${NC}"
    exit 0
}

trap cleanup SIGINT SIGTERM

echo -e "${BLUE}╔════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║         CONTINUOUS TRAFFIC GENERATOR                   ║${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════════════════╝${NC}"
echo ""

# Get pod names
TGEN1=$(kubectl get pods -n $NAMESPACE --no-headers | grep tgen1 | awk '{print $1}' | head -1)
TGEN2=$(kubectl get pods -n $NAMESPACE --no-headers | grep tgen2 | awk '{print $1}' | head -1)

if [ -z "$TGEN1" ] || [ -z "$TGEN2" ]; then
    echo -e "${RED}Error: Could not find tgen1 or tgen2 pods${NC}"
    exit 1
fi

TGEN2_IP=$(kubectl get pod -n $NAMESPACE $TGEN2 -o jsonpath='{.status.podIP}')

echo -e "Pods:     tgen1=$TGEN1, tgen2=$TGEN2"
echo -e "Target:   $TGEN2_IP"
echo -e "Bandwidth: ${BANDWIDTH} Mbps"
echo -e "API:      $API_URL"
echo ""
echo -e "${YELLOW}Press Ctrl+C to stop${NC}"
echo ""

# Start iperf3 server
kubectl exec -n $NAMESPACE $TGEN2 -- pkill iperf3 2>/dev/null || true
kubectl exec -n $NAMESPACE $TGEN2 -- iperf3 -s -D -p 5201 2>/dev/null
sleep 1

# Set links to active
for link in leaf1-tgen1 spine1-leaf1 spine1-leaf2 leaf2-tgen2; do
    curl -s -X PUT "$API_URL/api/links/$link/state?state=active" > /dev/null 2>&1
done

BANDWIDTH_BPS=$((BANDWIDTH * 1000000))

# Start iperf3 client in background with continuous output
kubectl exec -n $NAMESPACE $TGEN1 -- iperf3 -c $TGEN2_IP -p 5201 -t 3600 -b ${BANDWIDTH}M -i 1 --forceflush 2>&1 | \
while IFS= read -r line; do
    # Parse iperf output for bandwidth
    if echo "$line" | grep -q "Mbits/sec"; then
        # Extract bandwidth value
        BW=$(echo "$line" | grep -oE '[0-9]+\.?[0-9]* Mbits' | head -1 | awk '{print $1}')
        if [ -n "$BW" ]; then
            BW_INT=${BW%.*}
            BW_BPS=$((BW_INT * 1000000))
            UTIL=$(awk "BEGIN {printf \"%.2f\", $BW_INT / 1000}" 2>/dev/null || echo "0.05")

            # Update metrics
            curl -s -X PUT "$API_URL/api/links/leaf1-tgen1/metrics" \
                -H "Content-Type: application/json" \
                -d "{\"rx_bps\": $BW_BPS, \"tx_bps\": $((BW_BPS / 20)), \"utilization\": $UTIL}" > /dev/null 2>&1 &

            curl -s -X PUT "$API_URL/api/links/spine1-leaf1/metrics" \
                -H "Content-Type: application/json" \
                -d "{\"rx_bps\": $BW_BPS, \"tx_bps\": $((BW_BPS / 20)), \"utilization\": 0.01}" > /dev/null 2>&1 &

            curl -s -X PUT "$API_URL/api/links/spine1-leaf2/metrics" \
                -H "Content-Type: application/json" \
                -d "{\"rx_bps\": $((BW_BPS / 20)), \"tx_bps\": $BW_BPS, \"utilization\": 0.01}" > /dev/null 2>&1 &

            curl -s -X PUT "$API_URL/api/links/leaf2-tgen2/metrics" \
                -H "Content-Type: application/json" \
                -d "{\"rx_bps\": $((BW_BPS / 20)), \"tx_bps\": $BW_BPS, \"utilization\": $UTIL}" > /dev/null 2>&1 &

            echo -e "${GREEN}[$(date +%H:%M:%S)]${NC} Traffic: ${BW} Mbps"
        fi
    fi
done
