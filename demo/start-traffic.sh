#!/bin/bash
# Start Traffic Generator (Simulated)
# Usage: ./start-traffic.sh [bandwidth_mbps] [duration_seconds]
#
# Simulates traffic flow and updates the Network Monitor API metrics

BANDWIDTH="${1:-100}"  # Default 100 Mbps
DURATION="${2:-30}"    # Default 30 seconds
API_URL="${API_URL:-http://10.0.0.108}"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${BLUE}╔════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║           NETWORK MONITOR - TRAFFIC GENERATOR          ║${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "Bandwidth: ${GREEN}${BANDWIDTH} Mbps${NC}"
echo -e "Duration:  ${GREEN}${DURATION} seconds${NC}"
echo -e "API:       ${BLUE}${API_URL}${NC}"
echo ""
echo -e "${CYAN}Traffic Path: tgen1 → leaf1 → spine1 → leaf2 → tgen2${NC}"
echo ""

# Calculate metrics
BANDWIDTH_BPS=$((BANDWIDTH * 1000000))
UTIL_TGEN=$(awk "BEGIN {printf \"%.2f\", $BANDWIDTH / 1000}")  # 1Gbps access
UTIL_SPINE=$(awk "BEGIN {printf \"%.4f\", $BANDWIDTH / 10000}") # 10Gbps spine

# Set links to active and update metrics
echo -e "${YELLOW}[1/3] Starting traffic simulation...${NC}"

# Activate links
for link in leaf1-tgen1 spine1-leaf1 spine1-leaf2 leaf2-tgen2; do
    curl -s -X PUT "$API_URL/api/links/$link/state?state=active" > /dev/null 2>&1
done
echo -e "      ${GREEN}✓${NC} Links activated"

# Update metrics for the traffic path
curl -s -X PUT "$API_URL/api/links/leaf1-tgen1/metrics" \
    -H "Content-Type: application/json" \
    -d "{\"rx_bps\": $BANDWIDTH_BPS, \"tx_bps\": $((BANDWIDTH_BPS / 20)), \"rx_pps\": $((BANDWIDTH * 820)), \"tx_pps\": $((BANDWIDTH * 41)), \"utilization\": $UTIL_TGEN}" > /dev/null

curl -s -X PUT "$API_URL/api/links/spine1-leaf1/metrics" \
    -H "Content-Type: application/json" \
    -d "{\"rx_bps\": $BANDWIDTH_BPS, \"tx_bps\": $((BANDWIDTH_BPS / 20)), \"rx_pps\": $((BANDWIDTH * 820)), \"tx_pps\": $((BANDWIDTH * 41)), \"utilization\": $UTIL_SPINE}" > /dev/null

curl -s -X PUT "$API_URL/api/links/spine1-leaf2/metrics" \
    -H "Content-Type: application/json" \
    -d "{\"rx_bps\": $((BANDWIDTH_BPS / 20)), \"tx_bps\": $BANDWIDTH_BPS, \"rx_pps\": $((BANDWIDTH * 41)), \"tx_pps\": $((BANDWIDTH * 820)), \"utilization\": $UTIL_SPINE}" > /dev/null

curl -s -X PUT "$API_URL/api/links/leaf2-tgen2/metrics" \
    -H "Content-Type: application/json" \
    -d "{\"rx_bps\": $((BANDWIDTH_BPS / 20)), \"tx_bps\": $BANDWIDTH_BPS, \"rx_pps\": $((BANDWIDTH * 41)), \"tx_pps\": $((BANDWIDTH * 820)), \"utilization\": $UTIL_TGEN}" > /dev/null

echo -e "      ${GREEN}✓${NC} Metrics updated (${BANDWIDTH} Mbps)"

# Show progress
echo ""
echo -e "${YELLOW}[2/3] Traffic flowing for ${DURATION} seconds...${NC}"

# Progress bar
for ((i=0; i<DURATION; i++)); do
    # Calculate progress
    PCT=$((i * 100 / DURATION))
    FILLED=$((PCT / 5))
    EMPTY=$((20 - FILLED))

    # Build progress bar
    BAR=""
    for ((j=0; j<FILLED; j++)); do BAR+="█"; done
    for ((j=0; j<EMPTY; j++)); do BAR+="░"; done

    # Add some variance to bandwidth (±10%)
    VARIANCE=$((RANDOM % 20 - 10))
    CURRENT_BW=$((BANDWIDTH + BANDWIDTH * VARIANCE / 100))
    CURRENT_BPS=$((CURRENT_BW * 1000000))

    # Update metrics with slight variance
    curl -s -X PUT "$API_URL/api/links/leaf1-tgen1/metrics" \
        -H "Content-Type: application/json" \
        -d "{\"rx_bps\": $CURRENT_BPS, \"tx_bps\": $((CURRENT_BPS / 20)), \"utilization\": $UTIL_TGEN}" > /dev/null 2>&1 &

    printf "\r      [${GREEN}${BAR}${NC}] ${PCT}%% | ${CYAN}${CURRENT_BW} Mbps${NC}   "
    sleep 1
done
printf "\r      [${GREEN}████████████████████${NC}] 100%% | ${CYAN}${BANDWIDTH} Mbps${NC}   \n"

# Set to idle
echo ""
echo -e "${YELLOW}[3/3] Traffic complete. Setting links to idle...${NC}"

for link in leaf1-tgen1 spine1-leaf1 spine1-leaf2 leaf2-tgen2; do
    curl -s -X PUT "$API_URL/api/links/$link/metrics" \
        -H "Content-Type: application/json" \
        -d '{"rx_bps": 500, "tx_bps": 500, "rx_pps": 5, "tx_pps": 5, "utilization": 0}' > /dev/null
    curl -s -X PUT "$API_URL/api/links/$link/state?state=idle" > /dev/null 2>&1
done
echo -e "      ${GREEN}✓${NC} Links set to idle"

echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Traffic simulation complete!${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo ""
echo -e "Commands:"
echo -e "  ${BLUE}./dashboard.sh${NC}              - View link states"
echo -e "  ${BLUE}./dashboard.sh \$API_URL 2${NC}   - Auto-refresh every 2s"
echo -e "  ${BLUE}curl $API_URL/api/links${NC}  - API query"
