#!/usr/bin/env bash
# Run inside Lima as root. Starts the Pandora API server.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"

if [[ $EUID -ne 0 ]]; then
    echo "error: must run with sudo (needs root for TAP devices and iptables)" >&2
    echo "  sudo $0" >&2
    exit 1
fi

cd "${PROJECT_DIR}"
exec .venv/bin/uvicorn api:app --host 0.0.0.0 --port 8000
