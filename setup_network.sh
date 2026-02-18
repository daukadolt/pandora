#!/usr/bin/env bash
# ------------------------------------------------------------------------------
# Pandora – Ephemeral Interpreter
# setup_network.sh
#
# Sets up the subnet-level networking that all microVMs share: IP forwarding,
# NAT (masquerade), and forwarding rules.  Individual TAP devices are created
# per-VM by the Python orchestrator — this script only handles the common
# plumbing.
#
# Topology (N concurrent VMs):
#
#   microVM-0  (172.16.0.2)  ──tap0──┐
#   microVM-1  (172.16.0.6)  ──tap1──┤── Host (gateway) ──eth0── Internet
#   microVM-2  (172.16.0.10) ──tap2──┘
#
# Designed for Ubuntu 24.04 running inside a Lima envelope on macOS.
# ------------------------------------------------------------------------------

set -euo pipefail

# ---------- tunables ---------------------------------------------------------
MICROVM_SUBNET="172.16.0.0/24"
WAN_IF="eth0"          # Lima's outbound NIC
# -----------------------------------------------------------------------------

log() { printf '[pandora:net] %s\n' "$*"; }

# ---- guard ------------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
    echo "error: must run as root (need CAP_NET_ADMIN for iptables)" >&2
    exit 1
fi

# ---- cleanup helper (also callable as `setup_network.sh teardown`) ----------
teardown() {
    log "tearing down subnet rules"

    iptables -t nat -D POSTROUTING \
        -s "${MICROVM_SUBNET}" -o "${WAN_IF}" -j MASQUERADE 2>/dev/null || true

    # Subnet-based forwarding rules (not per-TAP).
    iptables -D FORWARD -s "${MICROVM_SUBNET}" -o "${WAN_IF}" -j ACCEPT 2>/dev/null || true
    iptables -D FORWARD -d "${MICROVM_SUBNET}" -i "${WAN_IF}" \
        -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || true

    log "teardown complete"
}

if [[ "${1:-}" == "teardown" ]]; then
    teardown
    exit 0
fi

# ---- idempotent: remove stale rules from a previous run --------------------
teardown 2>/dev/null || true

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
# Subnet-based rules: cover all current and future TAP devices without
# naming any specific interface.  Any packet from 172.16.0.0/24 heading
# to the WAN is allowed out; only replies are allowed back in.
iptables -A FORWARD -s "${MICROVM_SUBNET}" -o "${WAN_IF}" -j ACCEPT
iptables -A FORWARD -d "${MICROVM_SUBNET}" -i "${WAN_IF}" \
    -m state --state RELATED,ESTABLISHED -j ACCEPT

log "subnet networking ready – microVMs can reach the internet via ${WAN_IF}"
