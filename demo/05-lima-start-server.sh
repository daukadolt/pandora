#!/usr/bin/env bash
# Run inside Lima as root. Starts the Pandora API server.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"

if [[ $EUID -ne 0 ]]; then
    echo "Re-running with sudo..."
    exec sudo "$0" "$@"
fi

cd "${PROJECT_DIR}"
exec .venv/bin/uvicorn api:app --host 0.0.0.0 --port 8000
