#!/usr/bin/env bash
# Run from any terminal that can reach the API server.
set -euo pipefail

HOST="${1:-localhost}"
BASE="http://${HOST}:8000"

echo "=== Health check ==="
curl -s "${BASE}/health" | python3 -m json.tool
echo ""

echo "=== Create sandbox ==="
SANDBOX=$(curl -s -X POST "${BASE}/sandboxes" \
    -H "Content-Type: application/json")
echo "${SANDBOX}" | python3 -m json.tool

SANDBOX_ID=$(echo "${SANDBOX}" | python3 -c "import sys,json; print(json.load(sys.stdin)['sandbox_id'])")
echo "Sandbox ID: ${SANDBOX_ID}"
echo ""

echo "=== Execute command in sandbox ==="
curl -s -X POST "${BASE}/sandboxes/${SANDBOX_ID}/exec" \
    -H "Content-Type: application/json" \
    -d '{"code": "echo hello from the sandbox && uname -a"}' | python3 -m json.tool
echo ""

echo "=== Delete sandbox ==="
curl -s -X DELETE "${BASE}/sandboxes/${SANDBOX_ID}" -o /dev/null -w "HTTP %{http_code}\n"
echo ""

echo "=== Prometheus metrics (first 20 lines) ==="
curl -s "${BASE}/metrics" | head -20
