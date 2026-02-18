#!/usr/bin/env bash
# ------------------------------------------------------------------------------
# Pandora – prepare_rootfs.sh
#
# Generates an SSH key pair (if needed) and injects the public key into the
# rootfs image so the API server can execute commands inside the microVM.
#
# Must be run as root (or via sudo) because mounting a loop device requires
# elevated privileges.
#
# Usage:  sudo ./prepare_rootfs.sh
# ------------------------------------------------------------------------------

set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOTFS="${BASE_DIR}/images/hello-rootfs.ext4"
KEY_DIR="${BASE_DIR}/keys"
MOUNT_DIR="/tmp/pandora-rootfs-mount"

log() { printf '[pandora:rootfs] %s\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
    echo "error: must run as root (need to mount loop device)" >&2
    exit 1
fi

if [[ ! -f "${ROOTFS}" ]]; then
    echo "error: rootfs not found at ${ROOTFS}" >&2
    echo "       run the image download steps from SETUP.md first" >&2
    exit 1
fi

# ---- generate SSH key pair ------------------------------------------------
if [[ ! -f "${KEY_DIR}/pandora" ]]; then
    mkdir -p "${KEY_DIR}"
    # Generate as the calling user (not root) so file ownership is correct.
    ssh-keygen -t ed25519 -f "${KEY_DIR}/pandora" -N "" -C "pandora@ephemeral"
    log "generated SSH key pair in ${KEY_DIR}/"
else
    log "SSH key already exists at ${KEY_DIR}/pandora — skipping keygen"
fi

# ---- mount rootfs and inject key ------------------------------------------
log "mounting ${ROOTFS}"
mkdir -p "${MOUNT_DIR}"
mount -o loop "${ROOTFS}" "${MOUNT_DIR}"

# Ensure cleanup on any exit
cleanup() {
    umount "${MOUNT_DIR}" 2>/dev/null || true
    rmdir "${MOUNT_DIR}" 2>/dev/null || true
}
trap cleanup EXIT

mkdir -p "${MOUNT_DIR}/root/.ssh"
cp "${KEY_DIR}/pandora.pub" "${MOUNT_DIR}/root/.ssh/authorized_keys"
chmod 700 "${MOUNT_DIR}/root/.ssh"
chmod 600 "${MOUNT_DIR}/root/.ssh/authorized_keys"
log "injected public key into /root/.ssh/authorized_keys"

# ---- ensure sshd starts on boot ------------------------------------------
# Systemd-based images (Ubuntu 22.04+)
if [[ -d "${MOUNT_DIR}/etc/systemd/system" ]]; then
    mkdir -p "${MOUNT_DIR}/etc/systemd/system/multi-user.target.wants"
    # Try ssh.service first, fall back to sshd.service
    for svc in ssh sshd; do
        SVC_PATH="/usr/lib/systemd/system/${svc}.service"
        if [[ -f "${MOUNT_DIR}${SVC_PATH}" ]]; then
            ln -sf "${SVC_PATH}" \
                "${MOUNT_DIR}/etc/systemd/system/multi-user.target.wants/${svc}.service"
            log "enabled ${svc}.service via systemd symlink"
            break
        fi
    done
fi

# Allow root login via SSH (some images disable it by default)
SSHD_CONFIG="${MOUNT_DIR}/etc/ssh/sshd_config"
if [[ -f "${SSHD_CONFIG}" ]]; then
    if ! grep -q "^PermitRootLogin yes" "${SSHD_CONFIG}"; then
        echo "PermitRootLogin yes" >> "${SSHD_CONFIG}"
        log "added PermitRootLogin yes to sshd_config"
    fi
fi

log "rootfs preparation complete"
