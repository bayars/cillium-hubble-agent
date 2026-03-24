#!/bin/bash
# Continuous Traffic Generator with Real-time Metrics from iperf3
# Usage: ./continuous-traffic.sh [bandwidth_mbps]
#
# Runs real iperf3 traffic between tgen1 and tgen2 pods, parses the
# actual measured throughput from iperf3 output, and pushes real
# measurements to the Network Monitor API.
#
# Press Ctrl+C to stop

BANDWIDTH="${1:-50}"  # Default 50 Mbps target
API_URL="${API_URL:-http://localhost:8000}"
NAMESPACE="clab"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

# Links in the traffic path
TRAFFIC_LINKS="leaf1-tgen1 spine1-leaf1 spine1-leaf2 leaf2-tgen2"

cleanup() {
    echo ""
    echo -e "${YELLOW}Stopping traffic...${NC}"

    # Kill iperf processes
    kubectl exec -n $NAMESPACE $TGEN1 -- pkill iperf3 2>/dev/null || true
    kubectl exec -n $NAMESPACE $TGEN2 -- pkill iperf3 2>/dev/null || true

    # Set links to idle with zeroed metrics
    for link in $TRAFFIC_LINKS; do
        curl -s -X PUT "$API_URL/api/links/$link/state?state=idle" > /dev/null 2>&1
        curl -s -X PUT "$API_URL/api/links/$link/metrics" \
            -H "Content-Type: application/json" \
            -d '{"rx_bps": 0, "tx_bps": 0, "rx_pps": 0, "tx_pps": 0, "utilization": 0, "data_source": "iperf3"}' > /dev/null 2>&1
    done

    echo -e "${GREEN}Traffic stopped. Links set to idle.${NC}"
    exit 0
}

trap cleanup SIGINT SIGTERM

echo -e "${BLUE}╔════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║       CONTINUOUS TRAFFIC (REAL iperf3 DATA)            ║${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════════════════╝${NC}"
echo ""

# Get pod names
TGEN1=$(kubectl get pods -n $NAMESPACE --no-headers 2>/dev/null | grep tgen1 | awk '{print $1}' | head -1)
TGEN2=$(kubectl get pods -n $NAMESPACE --no-headers 2>/dev/null | grep tgen2 | awk '{print $1}' | head -1)

if [ -z "$TGEN1" ] || [ -z "$TGEN2" ]; then
    echo -e "${RED}Error: Could not find tgen1 or tgen2 pods in namespace '$NAMESPACE'${NC}"
    echo -e "${RED}Make sure Clabernetes topology is deployed: kubectl get pods -n $NAMESPACE${NC}"
    exit 1
fi

TGEN2_IP=$(kubectl get pod -n $NAMESPACE $TGEN2 -o jsonpath='{.status.podIP}')

if [ -z "$TGEN2_IP" ]; then
    echo -e "${RED}Error: Could not get IP for $TGEN2${NC}"
    exit 1
fi

echo -e "Pods:       tgen1=${GREEN}$TGEN1${NC}, tgen2=${GREEN}$TGEN2${NC}"
echo -e "Target IP:  ${GREEN}$TGEN2_IP${NC}"
echo -e "Target BW:  ${GREEN}${BANDWIDTH} Mbps${NC}"
echo -e "API:        ${BLUE}$API_URL${NC}"
echo -e "Data:       ${GREEN}Real iperf3 measurements${NC}"
echo ""
echo -e "${YELLOW}Press Ctrl+C to stop${NC}"
echo ""

# Start iperf3 server on tgen2
kubectl exec -n $NAMESPACE $TGEN2 -- pkill iperf3 2>/dev/null || true
kubectl exec -n $NAMESPACE $TGEN2 -- iperf3 -s -D -p 5201 2>/dev/null
sleep 1

# Set links to active
for link in $TRAFFIC_LINKS; do
    curl -s -X PUT "$API_URL/api/links/$link/state?state=active" > /dev/null 2>&1
done

# Push measured metrics to the API for all links in the path
push_metrics() {
    local bw_bps=$1
    local util=$(awk "BEGIN {printf \"%.4f\", $bw_bps / 1000000000}" 2>/dev/null || echo "0")

    # Ingress side: leaf1-tgen1, spine1-leaf1 (rx = measured, tx = ack traffic ~5%)
    for link in leaf1-tgen1 spine1-leaf1; do
        curl -s -X PUT "$API_URL/api/links/$link/metrics" \
            -H "Content-Type: application/json" \
            -d "{\"rx_bps\": $bw_bps, \"tx_bps\": $((bw_bps / 20)), \"utilization\": $util, \"data_source\": \"iperf3\"}" > /dev/null 2>&1 &
    done

    # Egress side: spine1-leaf2, leaf2-tgen2 (tx = measured, rx = ack traffic ~5%)
    for link in spine1-leaf2 leaf2-tgen2; do
        curl -s -X PUT "$API_URL/api/links/$link/metrics" \
            -H "Content-Type: application/json" \
            -d "{\"rx_bps\": $((bw_bps / 20)), \"tx_bps\": $bw_bps, \"utilization\": $util, \"data_source\": \"iperf3\"}" > /dev/null 2>&1 &
    done
}

# Start iperf3 client and parse real output
kubectl exec -n $NAMESPACE $TGEN1 -- iperf3 -c $TGEN2_IP -p 5201 -t 3600 -b ${BANDWIDTH}M -i 1 --forceflush 2>&1 | \
while IFS= read -r line; do
    # Parse iperf3 interval output lines containing "bits/sec"
    if echo "$line" | grep -qE '[0-9.]+ [MGK]bits/sec' && ! echo "$line" | grep -q "sender\|receiver"; then
        # Extract bandwidth value and unit
        BW_RAW=$(echo "$line" | grep -oE '[0-9]+\.?[0-9]* [MGK]bits' | tail -1)
        BW_NUM=$(echo "$BW_RAW" | awk '{print $1}')
        BW_UNIT=$(echo "$BW_RAW" | awk '{print $2}')

        if [ -n "$BW_NUM" ]; then
            # Convert to bits per second
            case "$BW_UNIT" in
                Gbits) BW_BPS=$(awk "BEGIN {printf \"%.0f\", $BW_NUM * 1000000000}") ;;
                Mbits) BW_BPS=$(awk "BEGIN {printf \"%.0f\", $BW_NUM * 1000000}") ;;
                Kbits) BW_BPS=$(awk "BEGIN {printf \"%.0f\", $BW_NUM * 1000}") ;;
                *)     BW_BPS=$(awk "BEGIN {printf \"%.0f\", $BW_NUM}") ;;
            esac

            # Convert to bytes per second for the API
            BW_BYTES=$((BW_BPS / 8))

            # Push real measurements
            push_metrics $BW_BYTES

            # Display with unit
            echo -e "${GREEN}[$(date +%H:%M:%S)]${NC} Measured: ${CYAN}${BW_NUM} ${BW_UNIT}/sec${NC} (real iperf3 data)"
        fi
    fi
done
