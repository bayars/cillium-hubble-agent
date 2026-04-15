#!/bin/bash
# Regenerate Python gRPC stubs from Hubble proto files.
# Proto source: github.com/cilium/cilium/api/v1/
#
# Usage: ./proto/generate.sh
#
# Prerequisites:
#   uv sync  (ensures grpcio-tools is installed)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PROTO_DIR="$SCRIPT_DIR"
OUTPUT_DIR="$PROJECT_DIR/api/generated"

GRPC_INCLUDE=$(uv run python -c "import grpc_tools; import os; print(os.path.join(os.path.dirname(grpc_tools.__file__), '_proto'))")

echo "Proto dir:    $PROTO_DIR"
echo "Output dir:   $OUTPUT_DIR"
echo "gRPC include: $GRPC_INCLUDE"
echo ""

# Clean old generated files (preserve __init__.py)
find "$OUTPUT_DIR" -name "*_pb2*.py" -delete 2>/dev/null || true

# Compile
uv run python -m grpc_tools.protoc \
  -I "$PROTO_DIR" \
  -I "$GRPC_INCLUDE" \
  --python_out="$OUTPUT_DIR" \
  --grpc_python_out="$OUTPUT_DIR" \
  "$PROTO_DIR/flow/flow.proto" \
  "$PROTO_DIR/relay/relay.proto" \
  "$PROTO_DIR/observer/observer.proto" \
  "$PROTO_DIR/peer/peer.proto"

echo ""
echo "Generated files:"
find "$OUTPUT_DIR" -name "*_pb2*.py" | sort
echo ""
echo "Done!"
