#!/usr/bin/env bash
# Run inside Lima as root. Injects SSH key into the rootfs.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"

if [[ $EUID -ne 0 ]]; then
    echo "Re-running with sudo..."
    exec sudo "$0" "$@"
fi

exec "${PROJECT_DIR}/prepare_rootfs.sh"
