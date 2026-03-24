#!/bin/bash
#
# Inject netmon-sidecar into Clabernetes pod deployments.
#
# Patches each clab deployment to add a sidecar container that shares
# the pod's network namespace, reads /sys/class/net/*/statistics/,
# and pushes per-interface metrics to the network-monitor API.
#
# Usage: ./inject-sidecar.sh [API_URL] [NAMESPACE]
#
# API_URL:   Network monitor API URL (default: http://network-monitor.network-monitor.svc:8000)
# NAMESPACE: Clabernetes namespace (default: clab)

set -euo pipefail

API_URL="${1:-http://network-monitor.network-monitor.svc:8000}"
NAMESPACE="${2:-clab}"
SIDECAR_IMAGE="ghcr.io/bayars/netmon-sidecar:latest"
POLL_INTERVAL_MS="${POLL_INTERVAL_MS:-1000}"

echo "Injecting sidecar into clab deployments in namespace: ${NAMESPACE}"
echo "API URL: ${API_URL}"
echo "Sidecar image: ${SIDECAR_IMAGE}"
echo ""

# Get all deployments in the clab namespace managed by clabernetes
DEPLOYMENTS=$(kubectl get deployments -n "${NAMESPACE}" -l clabernetes/app=clabernetes -o jsonpath='{.items[*].metadata.name}')

if [ -z "${DEPLOYMENTS}" ]; then
    echo "Error: No clabernetes deployments found in namespace ${NAMESPACE}"
    exit 1
fi

for DEPLOY in ${DEPLOYMENTS}; do
    # Extract the topology node name from labels
    NODE_NAME=$(kubectl get deployment "${DEPLOY}" -n "${NAMESPACE}" \
        -o jsonpath='{.metadata.labels.clabernetes/topologyNode}')

    if [ -z "${NODE_NAME}" ]; then
        echo "  SKIP ${DEPLOY}: no topologyNode label"
        continue
    fi

    # Build node_id matching what Hubble uses: namespace/pod-name
    # But for the sidecar, we use the topology node name directly
    NODE_ID="${NAMESPACE}/${DEPLOY}"

    # Check if sidecar already injected
    EXISTING=$(kubectl get deployment "${DEPLOY}" -n "${NAMESPACE}" \
        -o jsonpath='{.spec.template.spec.containers[?(@.name=="netmon-sidecar")].name}' 2>/dev/null)

    if [ "${EXISTING}" = "netmon-sidecar" ]; then
        echo "  SKIP ${DEPLOY}: sidecar already injected"
        continue
    fi

    echo "  Patching ${DEPLOY} (node: ${NODE_NAME}, id: ${NODE_ID})..."

    # Patch the deployment to add the sidecar container
    kubectl patch deployment "${DEPLOY}" -n "${NAMESPACE}" --type=json -p="[
        {
            \"op\": \"add\",
            \"path\": \"/spec/template/spec/containers/-\",
            \"value\": {
                \"name\": \"netmon-sidecar\",
                \"image\": \"${SIDECAR_IMAGE}\",
                \"imagePullPolicy\": \"IfNotPresent\",
                \"env\": [
                    {\"name\": \"API_URL\", \"value\": \"${API_URL}\"},
                    {\"name\": \"NODE_ID_PREFIX\", \"value\": \"${NAMESPACE}\"},
                    {\"name\": \"POD_NAME\", \"valueFrom\": {\"fieldRef\": {\"fieldPath\": \"metadata.name\"}}},
                    {\"name\": \"POD_NAMESPACE\", \"valueFrom\": {\"fieldRef\": {\"fieldPath\": \"metadata.namespace\"}}},
                    {\"name\": \"POLL_INTERVAL_MS\", \"value\": \"${POLL_INTERVAL_MS}\"},
                    {\"name\": \"EXCLUDE_IFACES\", \"value\": \"lo\"},
                    {\"name\": \"LOG_LEVEL\", \"value\": \"INFO\"}
                ],
                \"resources\": {
                    \"requests\": {\"cpu\": \"10m\", \"memory\": \"16Mi\"},
                    \"limits\": {\"cpu\": \"50m\", \"memory\": \"32Mi\"}
                }
            }
        }
    ]"

    echo "  OK ${DEPLOY}"
done

echo ""
echo "Done. Waiting for rollouts..."

for DEPLOY in ${DEPLOYMENTS}; do
    kubectl rollout status deployment/"${DEPLOY}" -n "${NAMESPACE}" --timeout=120s 2>/dev/null || true
done

echo ""
echo "Sidecar injection complete. Verify with:"
echo "  kubectl get pods -n ${NAMESPACE}"
echo "  kubectl logs -n ${NAMESPACE} <pod> -c netmon-sidecar"
