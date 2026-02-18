# Pandora

Ephemeral sandboxed code execution inside Firecracker microVMs with network isolation, pre-warmed VM pools, and end-to-end observability via Prometheus and Grafana.

<p align="center">
  <img src="pandora.gif" alt="Pandora demo" width="720" />
</p>

Pandora manages a pool of pre-booted Firecracker microVMs. Clients create sandboxes instantly from the warm pool, execute arbitrary commands over SSH, and tear them down when done. Idle sandboxes are automatically reaped. Every lifecycle phase — boot, acquire, exec, teardown — is instrumented with Prometheus histograms, counters, and gauges, and visualized on a pre-built Grafana dashboard.

## Architecture

```
Client (SDK / curl)
   │
   │  POST /sandboxes           GET /metrics
   ▼                                 │
┌─────────────────────────────────┐  │  ┌─────────────┐   ┌─────────┐
│  api.py  (FastAPI + Uvicorn)    │◄─┘  │ Prometheus  │──▶│ Grafana │
│                                 │────▶│   :9090     │   │  :3000  │
│  Warm pool   ◄── Warmer task    │     └─────────────┘   └─────────┘
│  Idle reaper    Metrics export  │
└────────┬────────────────────────┘
         │  per-VM lifecycle
┌────────▼────────────────────────┐
│  vm_manager.py                  │
│  VMSlot (tap, IP, socket)       │
│  FirecrackerVM                  │
│  boot → SSH → execute → shutdown│
└────────┬────────────────────────┘
         │  REST over UNIX socket
┌────────▼────────────────────────┐
│  Firecracker VMM (KVM)          │
│  per-VM rootfs copy             │
│  per-VM socket                  │
└────────┬────────────────────────┘
         │  virtio-net
┌────────▼────────────────────────┐
│  tap0…tapN  (172.16.0.0/24)    │
│  iptables MASQUERADE → eth0     │
│  IP forwarding + NAT            │
└─────────────────────────────────┘
```

## Key Features

- **Pre-warmed VM pool** — VMs are booted in the background so `POST /sandboxes` returns in milliseconds, not seconds. Pool size is configurable via `PANDORA_POOL_SIZE`.
- **Session-based sandboxes** — Create a sandbox once, run many commands against it (state persists), close when done. No boot penalty per command.
- **Automatic idle reaping** — Sandboxes that sit unused past their timeout are killed automatically, freeing slots for new work. Configurable per sandbox.
- **Per-VM isolation** — Each VM gets its own rootfs copy, TAP device, IP address, and Firecracker socket. VMs cannot see each other.
- **Network isolation** — iptables rules allow outbound NAT but block unsolicited inbound traffic. Each VM sits on its own /30 subnet behind the host gateway.
- **Prometheus instrumentation** — Histograms for boot, acquire, exec, teardown, and sandbox lifetime. Counters for executions by outcome (success/error/timeout), sandboxes created, and idle reaps. Gauges for active VMs, warm VMs.
- **Grafana dashboard** — Pre-built dashboard with latency percentiles (p50/p90/p99), throughput rates, pool utilization, execution outcomes, and resource lifecycle panels. Auto-provisioned via Docker Compose.
- **Stress testing** — Included load generator that creates parallel sandboxes, runs command batches, and reports latency breakdowns and throughput.
- **Python SDK** — `PandoraClient` with context-manager support for clean sandbox lifecycle management.

## Quick Start

```bash
# 1. Install dependencies
uv sync

# 2. Inject SSH key into rootfs (one-time)
sudo ./prepare_rootfs.sh

# 3. Start the API server (inside Lima)
sudo bash -c 'exec .venv/bin/uvicorn api:app --host 0.0.0.0 --port 8000'

# 4. Start monitoring stack (from macOS, needs Docker)
docker compose up -d
```

Wait ~30s for the warm pool to fill (watch the server logs), then:

```bash
# Create a sandbox, run commands, tear it down
python example.py

# Stress test — 10 sandboxes, 4 parallel, 3 execs each
python stress_test.py -n 10 -p 4 -e 3
```

Open Grafana at `http://localhost:3000` (admin / pandora) to see the dashboard.

## API

| Endpoint | Method | Description |
|---|---|---|
| `/sandboxes` | POST | Claim a warm VM, return sandbox ID |
| `/sandboxes/{id}/exec` | POST | Run a command inside the sandbox |
| `/sandboxes/{id}` | DELETE | Tear down the sandbox |
| `/sandboxes` | GET | List active sandboxes with age, idle time, exec count |
| `/health` | GET | Pool size, warm count, active count |
| `/metrics` | GET | Prometheus metrics |

## SDK

```python
from client import PandoraClient

client = PandoraClient("http://localhost:8000")

with client.create(idle_timeout=120) as sb:
    r = sb.exec("echo hello && uname -a")
    print(r.stdout)           # "hello\nLinux ... aarch64\n"
    print(r.exec_ms)          # 145.2
    print(sb.acquire_ms)      # 2.1  (warm hit)

    sb.exec("apt install -y cowsay")
    r = sb.exec("/usr/games/cowsay moo")
    print(r.stdout)
# sandbox auto-destroyed on context exit

client.close()
```

## Observability

### Prometheus Metrics

| Metric | Type | What it measures |
|---|---|---|
| `pandora_vm_boot_seconds` | Histogram | Background warm-boot time (FC start → SSH ready) |
| `pandora_sandbox_acquire_seconds` | Histogram | Client wait time to claim a sandbox from the warm pool |
| `pandora_vm_exec_seconds` | Histogram | SSH command execution time |
| `pandora_vm_teardown_seconds` | Histogram | FC termination + socket + rootfs cleanup |
| `pandora_sandbox_lifetime_seconds` | Histogram | Total sandbox lifespan (create → destroy) |
| `pandora_executions_total` | Counter | Executions by status (success / error / timeout) |
| `pandora_sandboxes_created_total` | Counter | Total sandboxes created |
| `pandora_sandboxes_reaped_total` | Counter | Sandboxes killed by idle timeout |
| `pandora_warm_boot_failures_total` | Counter | Background boot failures |
| `pandora_active_vms` | Gauge | Currently running sandboxes |
| `pandora_warm_vms` | Gauge | Pre-booted VMs waiting in the pool |

### Grafana Dashboard

The monitoring stack runs via Docker Compose (Prometheus + Grafana). The dashboard is auto-provisioned with panels for:

- **Throughput** — execution rate and sandbox creation rate over time
- **Pool utilization** — stacked area of active vs warm VMs
- **Latency percentiles** — p50/p90/p99 for acquire, exec, boot, teardown, and sandbox lifetime
- **Execution outcomes** — success/error/timeout breakdown over time
- **Operational gauges** — live counts for active sandboxes, warm VMs, reaps, and boot failures

## Requirements

- Linux host with KVM support (tested on Ubuntu 24.04 inside Lima with nested virtualization)
- Firecracker binary in `./bin/firecracker`
- Kernel + rootfs images in `./images/`
- Python 3.13+ (managed via [uv](https://docs.astral.sh/uv/))
- `iptables`, `ip`, `sysctl` (standard on Ubuntu)
- Docker + Docker Compose (for Prometheus and Grafana)

See [SETUP.md](SETUP.md) for step-by-step instructions.

## Project Layout

```
.
├── api.py                 # FastAPI server — warm pool, sandbox CRUD, metrics
├── vm_manager.py          # FirecrackerVM lifecycle, VMSlot, per-VM rootfs/TAP
├── client.py              # Python SDK (PandoraClient, Sandbox)
├── setup_network.sh       # Subnet-level iptables NAT + IP forwarding
├── prepare_rootfs.sh      # SSH key injection into guest rootfs
├── stress_test.py         # Parallel load generator with latency stats
├── example.py             # SDK usage demo
├── docker-compose.yml     # Prometheus + Grafana
├── monitoring/
│   ├── prometheus.yml     # Scrape config targeting Pandora /metrics
│   └── grafana/
│       ├── provisioning/  # Auto-configured datasource + dashboard provider
│       └── dashboards/    # Pre-built Pandora dashboard JSON
├── bin/
│   └── firecracker        # Firecracker binary (not checked in)
├── images/
│   ├── hello-vmlinux.bin  # Guest kernel
│   └── hello-rootfs.ext4  # Root filesystem
└── keys/
    ├── pandora            # SSH private key (not checked in)
    └── pandora.pub        # SSH public key (injected into rootfs)
```

## License

MIT
