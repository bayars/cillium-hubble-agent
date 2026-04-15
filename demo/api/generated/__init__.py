"""
Generated Hubble gRPC stubs from Cilium proto files.

Proto source: github.com/cilium/cilium/api/v1/
Compiled with: grpc_tools.protoc

The generated code uses absolute imports (e.g. `from flow import flow_pb2`)
which require this package's directory to be on sys.path.
"""
import sys
import os

# Add this directory to sys.path so generated imports resolve correctly.
# The protoc-generated files use `from flow import flow_pb2` etc.
_generated_dir = os.path.dirname(os.path.abspath(__file__))
if _generated_dir not in sys.path:
    sys.path.insert(0, _generated_dir)
