#!/usr/bin/env bash
# Run from any terminal that can reach the API server.
set -euo pipefail

HOST="${1:-localhost}"
BASE="http://${HOST}:8000"

echo "=== Health check ==="
curl -s "${BASE}/health" | python3 -m json.tool
echo ""

echo "=== Execute command in sandbox ==="
curl -s -X POST "${BASE}/execute" \
    -H "Content-Type: application/json" \
    -d '{"code": "echo hello from the sandbox && uname -a"}' | python3 -m json.tool
echo ""

echo "=== Prometheus metrics (first 20 lines) ==="
curl -s "${BASE}/metrics" | head -20
