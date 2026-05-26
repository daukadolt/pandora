#!/usr/bin/env bash
# Run this on macOS — creates and starts the Lima VM with KVM support.
set -euo pipefail

VM_NAME="${1:-pandora}"

if limactl list -q 2>/dev/null | grep -qx "${VM_NAME}"; then
    echo "Lima VM '${VM_NAME}' already exists."
    echo ""
    echo "Options:"
    echo "  1) Delete and recreate it"
    echo "  2) Start it as-is"
    echo "  3) Use a different name"
    echo ""
    read -rp "Choose [1/2/3]: " choice

    case "${choice}" in
        1)
            echo "Stopping and deleting '${VM_NAME}'..."
            limactl stop "${VM_NAME}" 2>/dev/null || true
            limactl delete "${VM_NAME}"
            ;;
        2)
            echo "Starting '${VM_NAME}'..."
            limactl start "${VM_NAME}" 2>/dev/null || true
            echo ""
            echo "Done. Enter the VM with:"
            echo "  limactl shell ${VM_NAME}"
            exit 0
            ;;
        3)
            read -rp "Enter new VM name: " VM_NAME
            if [[ -z "${VM_NAME}" ]]; then
                echo "error: name cannot be empty" >&2
                exit 1
            fi
            ;;
        *)
            echo "error: invalid choice" >&2
            exit 1
            ;;
    esac
fi

echo "Creating Lima VM '${VM_NAME}'..."
limactl create --name="${VM_NAME}" \
    --vm-type=vz \
    --mount-writable \
    --set '.nestedVirtualization = true' \
    template:ubuntu-24.04
limactl start "${VM_NAME}"

echo ""
echo "Done. Enter the VM with:"
echo "  limactl shell ${VM_NAME}"
