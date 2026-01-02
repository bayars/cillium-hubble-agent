#!/bin/bash
# Network Monitor Dashboard
# Usage: ./dashboard.sh [API_URL] [refresh_seconds]

API_URL="${1:-http://10.0.0.108}"
REFRESH="${2:-0}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
GRAY='\033[0;90m'
NC='\033[0m'
BOLD='\033[1m'

show_dashboard() {
    echo -e "${BOLD}╔══════════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}║                  NETWORK MONITOR - LINK BANDWIDTH                    ║${NC}"
    echo -e "${BOLD}╠══════════════════════════════════════════════════════════════════════╣${NC}"
    printf "${BOLD}║ %-14s │ %-8s │ %11s │ %11s │ %7s ║${NC}\n" "LINK" "STATE" "RX" "TX" "UTIL"
    echo -e "${BOLD}╠══════════════════════════════════════════════════════════════════════╣${NC}"

    curl -s "$API_URL/api/links" | jq -r '
      .links[] |
      [
        .id,
        .state,
        (if .metrics.rx_bps >= 1073741824 then "\(.metrics.rx_bps / 1073741824 * 10 | floor / 10) Gbps"
         elif .metrics.rx_bps >= 1048576 then "\(.metrics.rx_bps / 1048576 | floor) Mbps"
         elif .metrics.rx_bps >= 1024 then "\(.metrics.rx_bps / 1024 | floor) Kbps"
         else "\(.metrics.rx_bps | floor) bps" end),
        (if .metrics.tx_bps >= 1073741824 then "\(.metrics.tx_bps / 1073741824 * 10 | floor / 10) Gbps"
         elif .metrics.tx_bps >= 1048576 then "\(.metrics.tx_bps / 1048576 | floor) Mbps"
         elif .metrics.tx_bps >= 1024 then "\(.metrics.tx_bps / 1024 | floor) Kbps"
         else "\(.metrics.tx_bps | floor) bps" end),
        "\(.metrics.utilization * 100 | floor)%"
      ] | @tsv
    ' | while IFS=$'\t' read -r id state rx tx util; do
        # Color based on state
        case "$state" in
            active) STATE="${GREEN}● active${NC}" ;;
            idle)   STATE="${GRAY}○ idle${NC}  " ;;
            down)   STATE="${RED}✗ down${NC}  " ;;
            *)      STATE="${YELLOW}? unkn${NC}  " ;;
        esac

        # Color utilization if high
        util_num="${util%\%}"
        if [ "$util_num" -gt 80 ] 2>/dev/null; then
            util="${RED}${util}${NC}"
        elif [ "$util_num" -gt 50 ] 2>/dev/null; then
            util="${YELLOW}${util}${NC}"
        fi

        printf "║ %-14s │ ${STATE} │ %11s │ %11s │ %7s ║\n" "$id" "$rx" "$tx" "$util"
    done

    echo -e "${BOLD}╚══════════════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "API: ${BLUE}$API_URL${NC}  |  $(date '+%Y-%m-%d %H:%M:%S')"
    echo -e "${GRAY}States: ${GREEN}● active${NC}  ${GRAY}○ idle${NC}  ${RED}✗ down${NC}"
}

if [ "$REFRESH" -gt 0 ]; then
    while true; do
        clear
        show_dashboard
        echo -e "\n${GRAY}Refreshing every ${REFRESH}s... (Ctrl+C to stop)${NC}"
        sleep "$REFRESH"
    done
else
    show_dashboard
fi
