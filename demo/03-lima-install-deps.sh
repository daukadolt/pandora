#!/usr/bin/env bash
# Run inside Lima. Installs Python dependencies via uv.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"

cd "${PROJECT_DIR}"

if ! command -v uv &>/dev/null; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

if [[ -d "${PROJECT_DIR}/.venv" ]]; then
    echo "Removing existing .venv (may be from a different OS)..."
    rm -rf "${PROJECT_DIR}/.venv"
fi

echo "Syncing Python dependencies..."
uv sync

echo "Done. Virtual env at ${PROJECT_DIR}/.venv"
