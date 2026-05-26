#!/usr/bin/env bash
# Run inside Lima. Downloads and installs Firecracker.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"

FC_VERSION="1.14.1"
ARCH="$(uname -m)"
FC_BIN="${PROJECT_DIR}/bin/firecracker"

if [[ -x "${FC_BIN}" ]]; then
    echo "Firecracker already installed: $(${FC_BIN} --version)"
    exit 0
fi

mkdir -p "${PROJECT_DIR}/bin"

echo "Downloading Firecracker v${FC_VERSION} for ${ARCH}..."
curl -L "https://github.com/firecracker-microvm/firecracker/releases/download/v${FC_VERSION}/firecracker-v${FC_VERSION}-${ARCH}.tgz" \
    -o /tmp/firecracker.tgz

tar xzf /tmp/firecracker.tgz -C /tmp
cp "/tmp/release-v${FC_VERSION}-${ARCH}/firecracker-v${FC_VERSION}-${ARCH}" "${FC_BIN}"
chmod +x "${FC_BIN}"
rm -rf /tmp/firecracker.tgz "/tmp/release-v${FC_VERSION}-${ARCH}"

echo "Installed: $(${FC_BIN} --version)"
