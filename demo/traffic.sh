#!/bin/bash
# Network Monitor Traffic Generator
# Usage: ./traffic.sh [lab_name] [bandwidth_mbps] [duration_seconds]
#
# Generates simulated traffic for a specific lab by updating API metrics

LAB="${1:-network-monitor-demo}"
BANDWIDTH="${2:-100}"
DURATION="${3:-30}"
API_URL="${API_URL:-http://10.0.0.109:8000}"

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
echo -e "Bandwidth: ${GREEN}${BANDWIDTH} Mbps${NC}"
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
LINK_COUNT=$(echo "$LINKS" | wc -l)

if [ -z "$LINKS" ] || [ "$LINK_COUNT" -eq 0 ]; then
    echo -e "${RED}Error: No links found for lab '$LAB'${NC}"
    exit 1
fi

echo -e "${CYAN}Traffic Path: tgen1 → leaf1 → spine1 → leaf2 → tgen2${NC}"
echo -e "Links: ${LINK_COUNT} found"
echo ""

# Calculate metrics
BANDWIDTH_BPS=$((BANDWIDTH * 1000000))
UTIL_ACCESS=$(awk "BEGIN {printf \"%.2f\", $BANDWIDTH / 1000}")  # 1Gbps access
UTIL_SPINE=$(awk "BEGIN {printf \"%.4f\", $BANDWIDTH / 10000}")  # 10Gbps spine

# Traffic path links (adjust based on topology)
TRAFFIC_LINKS="leaf1-tgen1 spine1-leaf1 spine1-leaf2 leaf2-tgen2"

echo -e "${YELLOW}[1/3] Starting traffic simulation...${NC}"

# Activate all links in the lab
for link_id in $LINKS; do
    curl -s -X PUT "$API_URL/api/links/$link_id/state?state=active" > /dev/null 2>&1
done
echo -e "      ${GREEN}✓${NC} All links activated"

# Update metrics for traffic path
for link in $TRAFFIC_LINKS; do
    FULL_LINK="$LAB/$link"

    # Determine direction based on link
    case "$link" in
        leaf1-tgen1|spine1-leaf1)
            # Ingress traffic (rx high)
            curl -s -X PUT "$API_URL/api/links/$FULL_LINK/metrics" \
                -H "Content-Type: application/json" \
                -d "{\"rx_bps\": $BANDWIDTH_BPS, \"tx_bps\": $((BANDWIDTH_BPS / 20)), \"rx_pps\": $((BANDWIDTH * 820)), \"tx_pps\": $((BANDWIDTH * 41)), \"utilization\": $UTIL_ACCESS}" > /dev/null 2>&1
            ;;
        spine1-leaf2|leaf2-tgen2)
            # Egress traffic (tx high)
            curl -s -X PUT "$API_URL/api/links/$FULL_LINK/metrics" \
                -H "Content-Type: application/json" \
                -d "{\"rx_bps\": $((BANDWIDTH_BPS / 20)), \"tx_bps\": $BANDWIDTH_BPS, \"rx_pps\": $((BANDWIDTH * 41)), \"tx_pps\": $((BANDWIDTH * 820)), \"utilization\": $UTIL_ACCESS}" > /dev/null 2>&1
            ;;
    esac
done
echo -e "      ${GREEN}✓${NC} Metrics updated (${BANDWIDTH} Mbps)"

# Show progress
echo ""
echo -e "${YELLOW}[2/3] Traffic flowing for ${DURATION} seconds...${NC}"

# Progress bar with variance
for ((i=0; i<DURATION; i++)); do
    PCT=$((i * 100 / DURATION))
    FILLED=$((PCT / 5))
    EMPTY=$((20 - FILLED))

    BAR=""
    for ((j=0; j<FILLED; j++)); do BAR+="█"; done
    for ((j=0; j<EMPTY; j++)); do BAR+="░"; done

    # Add variance (±10%)
    VARIANCE=$((RANDOM % 20 - 10))
    CURRENT_BW=$((BANDWIDTH + BANDWIDTH * VARIANCE / 100))
    CURRENT_BPS=$((CURRENT_BW * 1000000))

    # Update metrics with variance
    for link in $TRAFFIC_LINKS; do
        FULL_LINK="$LAB/$link"
        case "$link" in
            leaf1-tgen1|spine1-leaf1)
                curl -s -X PUT "$API_URL/api/links/$FULL_LINK/metrics" \
                    -H "Content-Type: application/json" \
                    -d "{\"rx_bps\": $CURRENT_BPS, \"tx_bps\": $((CURRENT_BPS / 20)), \"utilization\": $UTIL_ACCESS}" > /dev/null 2>&1 &
                ;;
            spine1-leaf2|leaf2-tgen2)
                curl -s -X PUT "$API_URL/api/links/$FULL_LINK/metrics" \
                    -H "Content-Type: application/json" \
                    -d "{\"rx_bps\": $((CURRENT_BPS / 20)), \"tx_bps\": $CURRENT_BPS, \"utilization\": $UTIL_ACCESS}" > /dev/null 2>&1 &
                ;;
        esac
    done

    printf "\r      [${GREEN}${BAR}${NC}] ${PCT}%% | ${CYAN}${CURRENT_BW} Mbps${NC}   "
    sleep 1
done
printf "\r      [${GREEN}████████████████████${NC}] 100%% | ${CYAN}${BANDWIDTH} Mbps${NC}   \n"

# Set to idle
echo ""
echo -e "${YELLOW}[3/3] Traffic complete. Setting links to idle...${NC}"

for link_id in $LINKS; do
    curl -s -X PUT "$API_URL/api/links/$link_id/metrics" \
        -H "Content-Type: application/json" \
        -d '{"rx_bps": 500, "tx_bps": 500, "rx_pps": 5, "tx_pps": 5, "utilization": 0}' > /dev/null 2>&1
    curl -s -X PUT "$API_URL/api/links/$link_id/state?state=idle" > /dev/null 2>&1
done
echo -e "      ${GREEN}✓${NC} Links set to idle"

echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Traffic simulation complete!${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo ""
echo -e "Commands:"
echo -e "  ${BLUE}./dashboard.sh $API_URL${NC}       - View link states"
echo -e "  ${BLUE}./traffic.sh $LAB 500 60${NC}      - Run again (500Mbps, 60s)"
echo -e "  ${BLUE}curl $API_URL/api/links${NC}       - API query"
