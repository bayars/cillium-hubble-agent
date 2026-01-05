#!/bin/bash
# List available labs
# Usage: ./list-labs.sh [API_URL]

API_URL="${1:-${API_URL:-http://10.0.0.109:8000}}"

echo "=== Available Labs ==="
echo "API: $API_URL"
echo ""
echo "NAME                      NODES  LINKS  STATUS"
echo "----                      -----  -----  ------"

curl -s "$API_URL/api/labs" | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    if not d.get('labs'):
        print('  (no labs registered)')
    for l in d.get('labs', []):
        print(f\"{l['name']:25} {l['nodes_count']:5}  {l['links_count']:5}  {l['status']}\")
except:
    print('  Error: Could not connect to API')
"

echo ""
echo "Commands:"
echo "  ./traffic.sh <lab_name> [mbps] [seconds]  - Generate traffic"
echo "  ./dashboard.sh \$API_URL                   - View dashboard"
