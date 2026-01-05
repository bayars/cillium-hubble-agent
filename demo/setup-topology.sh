#!/bin/bash
# Setup Demo Topology
# Usage: ./setup-topology.sh [API_URL]
#
# Creates the demo topology (nodes and links) in the Network Monitor API.
# Use --clear to remove existing topology first.

API_URL="${1:-http://10.0.0.108}"
CLEAR=false

# Parse arguments
for arg in "$@"; do
    case $arg in
        --clear) CLEAR=true ;;
        http*) API_URL="$arg" ;;
    esac
done

# Colors
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${BLUE}Network Monitor - Topology Setup${NC}"
echo -e "API: ${BLUE}${API_URL}${NC}"
echo ""

# Check API connectivity
if ! curl -s "$API_URL/api/topology" > /dev/null 2>&1; then
    echo -e "${RED}Error: Cannot connect to API at $API_URL${NC}"
    exit 1
fi

# Clear existing topology if requested
if [ "$CLEAR" = true ]; then
    echo -e "${YELLOW}Clearing existing topology...${NC}"

    # Get and delete existing links
    LINKS=$(curl -s "$API_URL/api/links" | jq -r '.links[].id' 2>/dev/null)
    for link in $LINKS; do
        curl -s -X DELETE "$API_URL/api/topology/links/$link" > /dev/null
        echo -e "  Removed link: $link"
    done

    # Get and delete existing nodes
    NODES=$(curl -s "$API_URL/api/topology" | jq -r '.nodes[].id' 2>/dev/null)
    for node in $NODES; do
        curl -s -X DELETE "$API_URL/api/topology/nodes/$node" > /dev/null
        echo -e "  Removed node: $node"
    done
    echo ""
fi

# Add nodes
echo -e "${YELLOW}Adding nodes...${NC}"

add_node() {
    local id=$1 label=$2 type=$3 platform=$4
    local result=$(curl -s -X POST "$API_URL/api/topology/nodes" \
        -H "Content-Type: application/json" \
        -d "{\"id\":\"$id\",\"label\":\"$label\",\"type\":\"$type\",\"status\":\"up\",\"platform\":\"$platform\"}")

    if echo "$result" | jq -e '.id' > /dev/null 2>&1; then
        echo -e "  ${GREEN}✓${NC} $id ($platform)"
    else
        echo -e "  ${RED}✗${NC} $id - $(echo "$result" | jq -r '.detail // .message // "error"')"
    fi
}

add_node "spine1" "spine1" "router" "srlinux"
add_node "leaf1" "leaf1" "router" "frr"
add_node "leaf2" "leaf2" "router" "frr"
add_node "tgen1" "tgen1" "host" "iperf3"
add_node "tgen2" "tgen2" "host" "iperf3"

echo ""

# Add links
echo -e "${YELLOW}Adding links...${NC}"

add_link() {
    local id=$1 src=$2 dst=$3 src_if=$4 dst_if=$5 speed=$6
    local result=$(curl -s -X POST "$API_URL/api/topology/links" \
        -H "Content-Type: application/json" \
        -d "{\"id\":\"$id\",\"source\":\"$src\",\"target\":\"$dst\",\"source_interface\":\"$src_if\",\"target_interface\":\"$dst_if\",\"state\":\"idle\",\"speed_mbps\":$speed}")

    if echo "$result" | jq -e '.id' > /dev/null 2>&1; then
        echo -e "  ${GREEN}✓${NC} $id ($src ↔ $dst, ${speed}Mbps)"
    else
        echo -e "  ${RED}✗${NC} $id - $(echo "$result" | jq -r '.detail // .message // "error"')"
    fi
}

add_link "leaf1-tgen1" "leaf1" "tgen1" "eth1" "eth0" 1000
add_link "spine1-leaf1" "spine1" "leaf1" "e1-1" "eth2" 10000
add_link "spine1-leaf2" "spine1" "leaf2" "e1-2" "eth2" 10000
add_link "leaf2-tgen2" "leaf2" "tgen2" "eth1" "eth0" 1000

echo ""
echo -e "${GREEN}Topology setup complete!${NC}"
echo ""
echo "Verify with:"
echo -e "  ${BLUE}./show-bandwidth.sh $API_URL${NC}"
echo -e "  ${BLUE}./dashboard.sh $API_URL${NC}"
