#!/usr/bin/env bash
# ------------------------------------------------------------------------------
# Pandora – Ephemeral Interpreter
# setup_network.sh
#
# Creates the Layer-2/3 plumbing that connects a Firecracker microVM to the
# host network.  The microVM sits on a private 172.16.0.0/24 segment behind
# the host, which acts as its default gateway and NATs outbound traffic
# through the WAN interface (eth0).
#
# Topology:
#
#   microVM  (172.16.0.2) ──tap0── Host (172.16.0.1) ──eth0── Internet
#
# Designed for Ubuntu 24.04 running inside a Lima envelope on macOS.
# ------------------------------------------------------------------------------

set -euo pipefail

# ---------- tunables ---------------------------------------------------------
TAP_DEV="tap0"
TAP_IP="172.16.0.1"
TAP_CIDR="${TAP_IP}/24"
MICROVM_SUBNET="172.16.0.0/24"
WAN_IF="eth0"          # Lima's outbound NIC
# -----------------------------------------------------------------------------

log() { printf '[pandora:net] %s\n' "$*"; }

# ---- guard ------------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
    echo "error: must run as root (need CAP_NET_ADMIN for TAP/iptables)" >&2
    exit 1
fi

# ---- cleanup helper (also callable as `setup_network.sh teardown`) ----------
teardown() {
    log "tearing down ${TAP_DEV}"

    # Flush the NAT rule first so we don't leave a dangling MASQUERADE entry.
    iptables -t nat -D POSTROUTING \
        -s "${MICROVM_SUBNET}" -o "${WAN_IF}" -j MASQUERADE 2>/dev/null || true

    # Remove the forwarding rules that pin-hole traffic between tap0 and the WAN.
    iptables -D FORWARD -i "${TAP_DEV}" -o "${WAN_IF}" -j ACCEPT 2>/dev/null || true
    iptables -D FORWARD -i "${WAN_IF}" -o "${TAP_DEV}" -m state \
        --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || true

    ip link set "${TAP_DEV}" down  2>/dev/null || true
    ip tuntap del dev "${TAP_DEV}" mode tap 2>/dev/null || true

    log "teardown complete"
}

if [[ "${1:-}" == "teardown" ]]; then
    teardown
    exit 0
fi

# ---- create TAP device ------------------------------------------------------
# A TAP interface operates at Layer 2 – it delivers raw Ethernet frames to
# user-space (Firecracker).  This is the virtual "cable" between the host
# kernel's network stack and the guest's virtio-net device.
log "creating TAP device ${TAP_DEV}"

# Idempotent: tear down any stale interface from a previous run.
teardown 2>/dev/null || true

ip tuntap add dev "${TAP_DEV}" mode tap

# Assign the gateway IP that the microVM expects as its default route.
# The /24 mask means only addresses in 172.16.0.0–172.16.0.255 are on-link.
ip addr add "${TAP_CIDR}" dev "${TAP_DEV}"
ip link set "${TAP_DEV}" up

log "${TAP_DEV} is up with ${TAP_CIDR}"

# ---- enable IP forwarding ---------------------------------------------------
# Without ip_forward=1 the kernel drops packets that arrive on one interface
# and are destined for another.  This is the minimum requirement for the host
# to act as a router between the microVM subnet and the WAN.
log "enabling IPv4 forwarding"
sysctl -w net.ipv4.ip_forward=1 >/dev/null

# ---- NAT (masquerade) -------------------------------------------------------
# We use MASQUERADE instead of a static SNAT because the WAN address may be
# DHCP-assigned and can change across Lima restarts.  MASQUERADE rewrites the
# source IP of outbound packets from the microVM to the host's current WAN
# address, so the upstream network never sees the private 172.16.0.x range.
# This is the same principle behind any home router's NAT.
log "configuring iptables NAT (MASQUERADE) on ${WAN_IF}"

iptables -t nat -A POSTROUTING \
    -s "${MICROVM_SUBNET}" -o "${WAN_IF}" -j MASQUERADE

# ---- forwarding rules -------------------------------------------------------
# The default FORWARD policy on most distros is DROP.  We need explicit rules
# to allow traffic to flow between the TAP and the WAN interface.
#
# Rule 1: Allow NEW + ESTABLISHED outbound traffic from the microVM.
# Rule 2: Allow only RELATED/ESTABLISHED return traffic back in – this
#          prevents unsolicited inbound connections from the WAN reaching the
#          microVM, which is the "strict network isolation" we advertise.
iptables -A FORWARD -i "${TAP_DEV}" -o "${WAN_IF}" -j ACCEPT
iptables -A FORWARD -i "${WAN_IF}" -o "${TAP_DEV}" \
    -m state --state RELATED,ESTABLISHED -j ACCEPT

log "network plumbing ready – microVM can reach the internet via ${WAN_IF}"
