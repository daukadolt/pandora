# Pandora

Ephemeral code execution inside Firecracker microVMs with strict network isolation and built-in observability.

Pandora boots a short-lived Firecracker microVM per request, executes arbitrary shell commands inside it over SSH, tears the whole thing down, and records Prometheus metrics for every lifecycle phase. Each execution gets its own kernel, rootfs, and isolated network namespace. Nothing persists; nothing leaks.

## Architecture

```
                    ┌──────────────────────────────┐
  POST /execute     │  api.py (FastAPI)             │    GET /metrics
  ──────────────►   │  Prometheus histograms/       │◄──── Prometheus
                    │  counters per lifecycle phase  │      scrapes
                    └──────────┬───────────────────┘
                               │
                    ┌──────────▼───────────────────┐
                    │  vm_manager.py                 │
                    │  FirecrackerVM class            │
                    │  boot → wait_for_ssh →         │
                    │  execute(SSH) → shutdown        │
                    └──────────┬───────────────────┘
                               │  REST over UNIX socket
                    ┌──────────▼───────────────────┐
                    │  Firecracker VMM               │
                    │  /tmp/firecracker.socket        │
                    └──────────┬───────────────────┘
                               │  virtio-net
                    ┌──────────▼───────────────────┐
                    │  tap0 (172.16.0.1/24)          │
                    │  iptables MASQUERADE → eth0     │
                    └────────────────────────────────┘
```

## Quick start

```bash
uv sync
sudo ./prepare_rootfs.sh
sudo .venv/bin/uvicorn api:app --host 0.0.0.0 --port 8000
```

Then from any client:

```bash
curl -X POST http://localhost:8000/execute \
  -H "Content-Type: application/json" \
  -d '{"code": "echo hello && uname -a"}'
```

```json
{
  "exit_code": 0,
  "stdout": "hello\nLinux ... aarch64 GNU/Linux\n",
  "stderr": "",
  "boot_ms": 102.3,
  "ssh_ready_ms": 1842.1,
  "exec_ms": 45.7,
  "teardown_ms": 12.4,
  "total_ms": 2002.5
}
```

## API

| Endpoint | Method | Description |
|---|---|---|
| `/execute` | POST | Boot a microVM, run code, return output + timing |
| `/metrics` | GET | Prometheus metrics (histograms, counters, gauges) |
| `/health` | GET | Liveness check |

## SDK

```python
from client import PandoraClient

with PandoraClient("http://localhost:8000") as pandora:
    result = pandora.execute("python3 -c 'print(sum(range(100)))'")
    print(result.stdout)       # "4950\n"
    print(result.total_ms)     # 2100.5
```

## Observability

Every `/execute` request records Prometheus metrics:

| Metric | Type | What it measures |
|---|---|---|
| `pandora_vm_boot_seconds` | Histogram | FC launch through InstanceStart |
| `pandora_vm_ssh_ready_seconds` | Histogram | InstanceStart to sshd accepting connections |
| `pandora_vm_exec_seconds` | Histogram | SSH command execution time |
| `pandora_vm_teardown_seconds` | Histogram | FC termination + socket cleanup |
| `pandora_vm_e2e_seconds` | Histogram | Total request latency |
| `pandora_executions_total` | Counter | Executions by status (success/error/timeout) |
| `pandora_active_vms` | Gauge | Currently running VMs |

## Requirements

- Linux host (tested on Ubuntu 24.04 inside Lima)
- Firecracker binary in `./bin/firecracker`
- Kernel + rootfs images in `./images/`
- Python 3.13+ (managed via [uv](https://docs.astral.sh/uv/))
- `iptables`, `ip`, `sysctl` (standard on Ubuntu)

See [SETUP.md](SETUP.md) for full step-by-step instructions.

## Project layout

```
.
├── api.py                # FastAPI server (POST /execute, GET /metrics)
├── vm_manager.py         # FirecrackerVM lifecycle + SSH execution
├── client.py             # Python SDK
├── setup_network.sh      # TAP + NAT plumbing (bash)
├── prepare_rootfs.sh     # SSH key injection into rootfs
├── pyproject.toml
├── bin/
│   └── firecracker       # Firecracker binary (not checked in)
├── images/
│   ├── hello-vmlinux.bin # Guest kernel
│   └── hello-rootfs.ext4 # Root filesystem
├── keys/
│   ├── pandora           # SSH private key (not checked in)
│   └── pandora.pub       # SSH public key (injected into rootfs)
└── logs/
```

## License

MIT
