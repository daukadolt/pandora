# Pandora

Ephemeral code execution inside Firecracker microVMs with strict network isolation and built-in observability.

Pandora spins up a short-lived Firecracker microVM, runs untrusted code inside it, and tears the whole thing down — network plumbing included — when it's done. Each execution gets its own kernel, rootfs, and isolated network namespace. Nothing persists; nothing leaks.

## Architecture

```
                  ┌─────────────────────────┐
                  │  vm_manager.py           │
                  │  (Python orchestrator)   │
                  └────────┬────────────────┘
                           │  REST over UNIX socket
                           ▼
                  ┌─────────────────────────┐
                  │  Firecracker VMM         │
                  │  /tmp/firecracker.socket │
                  └────────┬────────────────┘
                           │  virtio-net
                           ▼
            ┌──────────────────────────────────┐
            │  tap0 (172.16.0.1/24)            │
            │  iptables MASQUERADE → eth0      │
            └──────────────────────────────────┘
```

The guest boots with a static IP (`172.16.0.2`) and reaches the internet through host-side NAT. Inbound connections from the WAN are blocked — only `RELATED,ESTABLISHED` return traffic is permitted.

## Requirements

- Linux host (tested on Ubuntu 24.04 inside Lima)
- Firecracker binary in `./bin/firecracker`
- Kernel + rootfs images in `./images/`
- Python 3.13+ (managed via [uv](https://docs.astral.sh/uv/))
- `iptables`, `ip`, `sysctl` (standard on Ubuntu)

## Quick start

```bash
# Install deps and create .venv
uv sync

# Bring up the network (needs root for TAP + iptables)
sudo ./setup_network.sh

# Launch a microVM
sudo .venv/bin/python vm_manager.py
```

Tear down the network manually if needed:

```bash
sudo ./setup_network.sh teardown
```

## Observability

Every boot writes a metrics document to `logs/vm_metrics.json`:

```json
{
  "events": {
    "t_start": 123456.0001,
    "t_socket_ready": 123456.0423,
    "t_booted": 123456.1890
  },
  "boot_latency_ms": 188.90,
  "socket_ready_latency_ms": 42.20
}
```

This file is designed for ingestion by a Prometheus node-exporter textfile collector, a Datadog custom check, or a simple `jq` pipeline.

## Cleanup guarantees

`vm_manager.py` wraps the entire lifecycle in `try/finally` and registers `SIGINT`/`SIGTERM` handlers. On any exit path:

1. The Firecracker process is terminated (escalating to `SIGKILL` after 5 s).
2. The API socket is removed.
3. The TAP interface and iptables rules are torn down via `setup_network.sh teardown`.

## Project layout

```
.
├── setup_network.sh      # TAP + NAT plumbing (bash)
├── vm_manager.py         # Orchestrator + metrics (Python)
├── pyproject.toml
├── bin/
│   └── firecracker       # Firecracker binary (not checked in)
├── images/
│   ├── hello-vmlinux.bin # Guest kernel
│   └── hello-rootfs.ext4 # Root filesystem
└── logs/
    └── vm_metrics.json   # Written on each run
```

## License

MIT
