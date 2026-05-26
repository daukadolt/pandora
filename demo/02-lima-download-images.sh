#!/usr/bin/env bash
# Run inside Lima. Downloads kernel and rootfs, converts squashfs to ext4.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"
IMAGES_DIR="${PROJECT_DIR}/images"

ARCH="$(uname -m)"
CI_VERSION="v1.14"

mkdir -p "${IMAGES_DIR}"

# --- Kernel ---
if [[ -f "${IMAGES_DIR}/hello-vmlinux.bin" ]]; then
    echo "Kernel already exists, skipping."
else
    echo "Discovering latest kernel for ${ARCH}..."
    latest_kernel_key=$(curl -s \
        "http://spec.ccfc.min.s3.amazonaws.com/?prefix=firecracker-ci/${CI_VERSION}/${ARCH}/vmlinux-&list-type=2" \
        | grep -oP "(?<=<Key>)(firecracker-ci/${CI_VERSION}/${ARCH}/vmlinux-[0-9]+\.[0-9]+\.[0-9]{1,3})(?=</Key>)" \
        | sort -V | tail -1)

    if [[ -z "${latest_kernel_key}" ]]; then
        echo "error: could not discover kernel from S3" >&2
        exit 1
    fi

    echo "Downloading ${latest_kernel_key}..."
    curl -L -o "${IMAGES_DIR}/hello-vmlinux.bin" \
        "https://s3.amazonaws.com/spec.ccfc.min/${latest_kernel_key}"
fi

# --- Rootfs ---
if [[ -f "${IMAGES_DIR}/hello-rootfs.ext4" ]]; then
    echo "Rootfs already exists, skipping."
else
    sudo apt-get update -qq && sudo apt-get install -y -qq squashfs-tools e2fsprogs

    echo "Discovering latest Ubuntu rootfs for ${ARCH}..."
    latest_ubuntu_key=$(curl -s \
        "http://spec.ccfc.min.s3.amazonaws.com/?prefix=firecracker-ci/${CI_VERSION}/${ARCH}/ubuntu-&list-type=2" \
        | grep -oP "(?<=<Key>)(firecracker-ci/${CI_VERSION}/${ARCH}/ubuntu-[0-9]+\.[0-9]+\.squashfs)(?=</Key>)" \
        | sort -V | tail -1)

    if [[ -z "${latest_ubuntu_key}" ]]; then
        echo "error: could not discover rootfs from S3" >&2
        exit 1
    fi

    echo "Downloading ${latest_ubuntu_key}..."
    curl -L -o /tmp/rootfs.squashfs \
        "https://s3.amazonaws.com/spec.ccfc.min/${latest_ubuntu_key}"

    echo "Converting squashfs -> ext4..."
    cd /tmp
    sudo unsquashfs -f rootfs.squashfs
    sudo chown -R root:root squashfs-root
    truncate -s 1G rootfs.ext4
    sudo mkfs.ext4 -d squashfs-root -F rootfs.ext4
    cp rootfs.ext4 "${IMAGES_DIR}/hello-rootfs.ext4"
    sudo rm -rf /tmp/squashfs-root /tmp/rootfs.squashfs /tmp/rootfs.ext4
    cd "${PROJECT_DIR}"
fi

echo ""
echo "Images ready:"
ls -lh "${IMAGES_DIR}/"
