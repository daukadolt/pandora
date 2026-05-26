# Pandora – Demo Guide

Run a sandboxed code execution engine on your Mac using Firecracker microVMs.

## Prerequisites

- macOS on Apple Silicon
- [Lima](https://lima-vm.io/) installed (`brew install lima`)
- Docker (for optional monitoring stack)

## Why Lima?

Firecracker needs `/dev/kvm` — Linux's hardware virtualization interface. macOS doesn't have it. Lima creates a lightweight Linux VM with nested virtualization enabled, which exposes `/dev/kvm` inside it. On bare-metal Linux, Lima isn't needed.

## Steps

All scripts are in the `demo/` folder. The prefix (`mac` or `lima`) tells you where to run each one.

### Step 0 — Create the Lima VM (macOS)

```bash
./demo/00-mac-create-lima.sh
```

Creates a Lima VM named `pandora` with Apple Virtualization.framework, writable mounts, and nested virtualization. If a VM with that name already exists, you'll be prompted to delete it, reuse it, or pick a new name.

You can also pass a custom name: `./demo/00-mac-create-lima.sh my-vm`

### Step 1 — Enter Lima

```bash
limactl shell pandora
cd ~/Desktop/pandora
```

Everything from here runs inside the Lima VM.

### Step 2 — Install Firecracker (Lima)

```bash
./demo/01-lima-install-firecracker.sh
```

Downloads Firecracker v1.14.1 and puts it in `bin/firecracker`. Skips if already installed.

### Step 3 — Download kernel and rootfs (Lima)

```bash
./demo/02-lima-download-images.sh
```

Discovers the latest kernel and Ubuntu rootfs from Firecracker's CI bucket, downloads them, and converts the rootfs from squashfs to ext4. This is the slowest step (~1 GB download). Skips files that already exist.

### Step 4 — Install Python dependencies (Lima)

```bash
./demo/03-lima-install-deps.sh
```

Installs `uv` if needed, then runs `uv sync` to create `.venv/` with all dependencies.

### Step 5 — Prepare the rootfs (Lima)

```bash
./demo/04-lima-prepare-rootfs.sh
```

Generates an SSH key pair and injects the public key into the rootfs image. This is how the API server runs commands inside the microVM. Requires sudo (the script re-runs itself with sudo automatically).

### Step 6 — Start the server (Lima)

```bash
./demo/05-lima-start-server.sh
```

Starts the Pandora API server on port 8000. The warm pool pre-boots microVMs on startup, so the first request is fast. Requires sudo for network setup (TAP devices, iptables).

### Step 7 — Test it (macOS)

In a new terminal on your Mac:

```bash
./demo/06-mac-test.sh
```

Runs a health check, executes a command inside a sandbox, and prints Prometheus metrics. You can also pass a custom host: `./demo/06-mac-test.sh 192.168.1.50`

## Quick test without the API server

To verify the VM lifecycle works end-to-end without starting the full server:

```bash
# Inside Lima
sudo .venv/bin/python vm_manager.py
```

This boots one VM, runs a test command over SSH, prints the output, and tears down.

## Cleanup

If something goes wrong inside Lima:

```bash
sudo pkill -9 firecracker || true
sudo rm -f /tmp/firecracker.socket
sudo ./setup_network.sh teardown
```
