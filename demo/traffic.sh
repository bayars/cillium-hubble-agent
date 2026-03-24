#!/bin/bash
# Network Monitor Traffic Generator (Lab-aware)
# Usage: ./traffic.sh [lab_name] [bandwidth_mbps] [duration_seconds]
#
# Generates real iperf3 traffic for a specific lab and pushes measured
# bandwidth to the API.

LAB="${1:-network-monitor-demo}"
BANDWIDTH="${2:-100}"
DURATION="${3:-30}"
API_URL="${API_URL:-http://10.0.0.109:8000}"
NAMESPACE="clab"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${BLUE}╔════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║           NETWORK MONITOR - TRAFFIC GENERATOR              ║${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "Lab:       ${GREEN}${LAB}${NC}"
echo -e "Target BW: ${GREEN}${BANDWIDTH} Mbps${NC}"
echo -e "Duration:  ${GREEN}${DURATION} seconds${NC}"
echo -e "API:       ${BLUE}${API_URL}${NC}"
echo ""

# Check if lab exists
LAB_CHECK=$(curl -s "$API_URL/api/labs/$LAB/topology" 2>/dev/null)
if echo "$LAB_CHECK" | grep -q "not found"; then
    echo -e "${RED}Error: Lab '$LAB' not found${NC}"
    echo ""
    echo "Available labs:"
    curl -s "$API_URL/api/labs" | jq -r '.labs[].name' 2>/dev/null | sed 's/^/  - /'
    exit 1
fi

# Get links for this lab
LINKS=$(curl -s "$API_URL/api/links" | jq -r ".links[] | select(.lab == \"$LAB\") | .id" 2>/dev/null)
LINK_COUNT=$(echo "$LINKS" | grep -c .)

if [ -z "$LINKS" ] || [ "$LINK_COUNT" -eq 0 ]; then
    echo -e "${RED}Error: No links found for lab '$LAB'${NC}"
    exit 1
fi

# Traffic path links (prefixed with lab name)
TRAFFIC_LINKS="leaf1-tgen1 spine1-leaf1 spine1-leaf2 leaf2-tgen2"

echo -e "${CYAN}Traffic Path: tgen1 → leaf1 → spine1 → leaf2 → tgen2${NC}"
echo -e "Links: ${LINK_COUNT} found"
echo ""

# Find iperf pods
TGEN1=$(kubectl get pods -n $NAMESPACE --no-headers 2>/dev/null | grep tgen1 | awk '{print $1}' | head -1)
TGEN2=$(kubectl get pods -n $NAMESPACE --no-headers 2>/dev/null | grep tgen2 | awk '{print $1}' | head -1)

if [ -z "$TGEN1" ] || [ -z "$TGEN2" ]; then
    echo -e "${RED}Error: Could not find tgen1 or tgen2 pods in namespace '$NAMESPACE'${NC}"
    echo -e "${RED}Deploy the Clabernetes topology first.${NC}"
    exit 1
fi

TGEN2_IP=$(kubectl get pod -n $NAMESPACE $TGEN2 -o jsonpath='{.status.podIP}' 2>/dev/null)
DATA_SOURCE="iperf3"

echo -e "Mode:      ${GREEN}REAL (iperf3 between pods)${NC}"
echo -e "Pods:      tgen1=${GREEN}$TGEN1${NC}, tgen2=${GREEN}$TGEN2${NC}"
echo ""

# Push measured metrics for all links in the path
push_metrics() {
    local bw_bps=$1
    local source=$2
    local util=$(awk "BEGIN {printf \"%.4f\", $bw_bps / 1000000000}" 2>/dev/null || echo "0")

    for link in $TRAFFIC_LINKS; do
        local full_link="$LAB/$link"
        case "$link" in
            leaf1-tgen1|spine1-leaf1)
                curl -s -X PUT "$API_URL/api/links/$full_link/metrics" \
                    -H "Content-Type: application/json" \
                    -d "{\"rx_bps\": $bw_bps, \"tx_bps\": $((bw_bps / 20)), \"utilization\": $util, \"data_source\": \"$source\"}" > /dev/null 2>&1 &
                ;;
            spine1-leaf2|leaf2-tgen2)
                curl -s -X PUT "$API_URL/api/links/$full_link/metrics" \
                    -H "Content-Type: application/json" \
                    -d "{\"rx_bps\": $((bw_bps / 20)), \"tx_bps\": $bw_bps, \"utilization\": $util, \"data_source\": \"$source\"}" > /dev/null 2>&1 &
                ;;
        esac
    done
    wait
}

# Activate all links in the lab
echo -e "${YELLOW}[1/3] Starting traffic...${NC}"
for link_id in $LINKS; do
    curl -s -X PUT "$API_URL/api/links/$link_id/state?state=active" > /dev/null 2>&1
done
echo -e "      ${GREEN}✓${NC} All links activated"

# Start iperf3 server
kubectl exec -n $NAMESPACE $TGEN2 -- pkill iperf3 2>/dev/null || true
kubectl exec -n $NAMESPACE $TGEN2 -- iperf3 -s -D -p 5201 2>/dev/null
sleep 1

echo ""
echo -e "${YELLOW}[2/3] Running iperf3 for ${DURATION}s (real traffic)...${NC}"
echo ""

kubectl exec -n $NAMESPACE $TGEN1 -- iperf3 -c $TGEN2_IP -p 5201 \
    -t $DURATION -b ${BANDWIDTH}M -i 1 --forceflush 2>&1 | \
while IFS= read -r line; do
    if echo "$line" | grep -qE '[0-9.]+ [MGK]bits/sec' && ! echo "$line" | grep -q "sender\|receiver"; then
        BW_RAW=$(echo "$line" | grep -oE '[0-9]+\.?[0-9]* [MGK]bits' | tail -1)
        BW_NUM=$(echo "$BW_RAW" | awk '{print $1}')
        BW_UNIT=$(echo "$BW_RAW" | awk '{print $2}')

        if [ -n "$BW_NUM" ]; then
            case "$BW_UNIT" in
                Gbits) BW_BPS=$(awk "BEGIN {printf \"%.0f\", $BW_NUM * 1000000000}") ;;
                Mbits) BW_BPS=$(awk "BEGIN {printf \"%.0f\", $BW_NUM * 1000000}") ;;
                Kbits) BW_BPS=$(awk "BEGIN {printf \"%.0f\", $BW_NUM * 1000}") ;;
                *)     BW_BPS=$(awk "BEGIN {printf \"%.0f\", $BW_NUM}") ;;
            esac
            BW_BYTES=$((BW_BPS / 8))
            push_metrics $BW_BYTES "$DATA_SOURCE"
            echo -e "      ${GREEN}[$(date +%H:%M:%S)]${NC} Measured: ${CYAN}${BW_NUM} ${BW_UNIT}/sec${NC}"
        fi
    fi

    if echo "$line" | grep -q "sender"; then
        BW_SUMMARY=$(echo "$line" | grep -oE '[0-9]+\.?[0-9]* [MGK]bits' | tail -1)
        echo ""
        echo -e "      ${GREEN}✓${NC} iperf3 summary: ${CYAN}${BW_SUMMARY}/sec${NC} (sender)"
    fi
done

kubectl exec -n $NAMESPACE $TGEN2 -- pkill iperf3 2>/dev/null || true

# Set to idle
echo ""
echo -e "${YELLOW}[3/3] Traffic complete. Setting links to idle...${NC}"

for link_id in $LINKS; do
    curl -s -X PUT "$API_URL/api/links/$link_id/metrics" \
        -H "Content-Type: application/json" \
        -d "{\"rx_bps\": 0, \"tx_bps\": 0, \"rx_pps\": 0, \"tx_pps\": 0, \"utilization\": 0, \"data_source\": \"iperf3\"}" > /dev/null 2>&1
    curl -s -X PUT "$API_URL/api/links/$link_id/state?state=idle" > /dev/null 2>&1
done
echo -e "      ${GREEN}✓${NC} Links set to idle"

echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Traffic complete!${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo ""
echo -e "Commands:"
echo -e "  ${BLUE}./dashboard.sh $API_URL${NC}              - View link states"
echo -e "  ${BLUE}./traffic.sh $LAB 500 60${NC}             - Run again (500Mbps, 60s)"
echo -e "  ${BLUE}./continuous-traffic.sh 200${NC}          - Continuous real traffic"
