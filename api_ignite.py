"""
Pandora – Ignite-backed API Server  (Phase 1 → Phase 2)

Drop-in replacement for api.py that mirrors all pool and sandbox state into
Apache Ignite distributed caches.  In Phase 1 (single node) the behaviour is
identical to api.py; in Phase 2 (multi-node) any node can claim warm VMs from
any other node and exec requests are routed via Ignite to the owner node.

Start with:
    sudo bash -c 'exec .venv/bin/uvicorn api_ignite:app --host 0.0.0.0 --port 8000'

Required env vars:
    IGNITE_HOSTS          Comma-separated "host:port".  Default: localhost:10800
    PANDORA_NODE_ID       Unique node label.  Default: random 8-hex string
    PANDORA_POOL_SIZE     Number of warm VMs to maintain.  Default: 4
    PANDORA_IDLE_TIMEOUT  Default sandbox idle timeout in seconds.  Default: 60

Phase 1 / Phase 2 differences
──────────────────────────────
Phase 1 (this file, single node):
  - asyncio.Queue still drives local claiming — no network round-trip on the
    hot path.
  - Ignite is written on every state change so the cluster has a live view.
  - owner_node_id is always NODE_ID; no cross-node routing needed.

Phase 2 (future, multi-node):
  - claim_warm() will also scan remote-node keys via SQL query.
  - exec requests arriving at the wrong node will be forwarded to the owner
    node via the Ignite compute grid (see VISION.md §Open Questions).
  - The idle reaper will check owner_node_id and only reap local sandboxes.
  - A cluster-event listener will tombstone sandboxes whose owner node died.
"""

import asyncio
import logging
import os
import time
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    CONTENT_TYPE_LATEST,
)
from pydantic import BaseModel, Field

from vm_manager import (
    FirecrackerVM,
    VMSlot,
    setup_network,
    teardown_network,
)
from ignite_state import IgnitePoolState, NODE_ID, _parse_hosts

log = logging.getLogger("pandora.api_ignite")

POOL_SIZE = int(os.environ.get("PANDORA_POOL_SIZE", "4"))
DEFAULT_IDLE_TIMEOUT = float(os.environ.get("PANDORA_IDLE_TIMEOUT", "60"))
REAPER_INTERVAL = 5.0
WARMER_INTERVAL = 1.0
IGNITE_HOSTS_RAW = os.environ.get("IGNITE_HOSTS", "localhost:10800")

# ---------------------------------------------------------------------------
# Prometheus metrics  (same set as api.py, no changes)
# ---------------------------------------------------------------------------
VM_BOOT_SECONDS = Histogram(
    "pandora_vm_boot_seconds",
    "Time to boot a Firecracker microVM (FC start through SSH ready)",
    buckets=(0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 15.0, 30.0, 60.0),
)
VM_EXEC_SECONDS = Histogram(
    "pandora_vm_exec_seconds",
    "Wall-clock time to execute a command over SSH",
    buckets=(0.05, 0.1, 0.5, 1.0, 5.0, 10.0, 30.0),
)
VM_TEARDOWN_SECONDS = Histogram(
    "pandora_vm_teardown_seconds",
    "Time to terminate Firecracker and clean up",
    buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 5.0),
)
SANDBOX_LIFETIME_SECONDS = Histogram(
    "pandora_sandbox_lifetime_seconds",
    "Total wall-clock lifetime of a sandbox (create to destroy)",
    buckets=(5.0, 15.0, 30.0, 60.0, 120.0, 300.0),
)
SANDBOX_ACQUIRE_SECONDS = Histogram(
    "pandora_sandbox_acquire_seconds",
    "Time a client waits to get a sandbox from the warm pool",
    buckets=(0.001, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 30.0, 60.0),
)
EXECUTIONS_TOTAL = Counter(
    "pandora_executions_total",
    "Total code executions by outcome",
    ["status"],
)
SANDBOXES_CREATED = Counter(
    "pandora_sandboxes_created_total",
    "Total sandboxes created",
)
SANDBOXES_REAPED = Counter(
    "pandora_sandboxes_reaped_total",
    "Sandboxes killed by idle timeout reaper",
)
WARM_BOOT_FAILURES = Counter(
    "pandora_warm_boot_failures_total",
    "Pre-warm boot attempts that failed",
)
ACTIVE_VMS = Gauge(
    "pandora_active_vms",
    "Number of microVMs currently running (active sandboxes)",
)
WARM_VMS = Gauge(
    "pandora_warm_vms",
    "Number of pre-booted VMs ready to be claimed",
)
IGNITE_WARM_VMS = Gauge(
    "pandora_ignite_warm_vms",
    "Warm VM count as seen in the Ignite warm_pool cache (this node)",
)


# ---------------------------------------------------------------------------
# In-process state  (node-local references to actual VM objects)
# ---------------------------------------------------------------------------
@dataclass
class WarmVM:
    """A pre-booted VM held locally, also advertised in Ignite."""
    vm: FirecrackerVM
    slot: VMSlot
    ignite_key: str   # key used in pandora_warm_pool
    booted_at: float


class SandboxEntry:
    __slots__ = ("vm", "slot", "created_at", "last_activity", "idle_timeout",
                 "exec_count", "_lock")

    def __init__(self, vm: FirecrackerVM, slot: VMSlot,
                 idle_timeout: float) -> None:
        self.vm = vm
        self.slot = slot
        self.created_at = time.monotonic()
        self.last_activity = time.monotonic()
        self.idle_timeout = idle_timeout
        self.exec_count = 0
        self._lock = threading.Lock()

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_activity

    @property
    def is_expired(self) -> bool:
        return self.idle_seconds > self.idle_timeout

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self.created_at


# Node-local VM object maps — not serialisable, never leave this process.
_local_warm: dict[str, WarmVM] = {}        # ignite_key → WarmVM
_sandboxes: dict[str, SandboxEntry] = {}   # sandbox_id → SandboxEntry
_sandbox_lock = threading.Lock()

# Local queues (same as api.py)
_warm_queue: asyncio.Queue[WarmVM] = asyncio.Queue()
_free_slots: asyncio.Queue[VMSlot] = asyncio.Queue()
_all_slots: list[VMSlot] = []
_shutting_down = False

# Ignite state — initialised in lifespan
_ignite: IgnitePoolState | None = None


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------
class CreateSandboxRequest(BaseModel):
    idle_timeout: float = Field(
        default=DEFAULT_IDLE_TIMEOUT, ge=10, le=600,
        description="Seconds of inactivity before the sandbox is reaped",
    )


class SandboxResponse(BaseModel):
    sandbox_id: str
    slot_id: int
    guest_ip: str
    owner_node_id: str
    warm: bool
    acquire_ms: float
    idle_timeout: float


class ExecRequest(BaseModel):
    code: str = Field(..., description="Shell command(s) to execute")
    timeout: float = Field(
        default=30.0, ge=1, le=300,
        description="Max seconds for the command",
    )


class ExecResponse(BaseModel):
    sandbox_id: str
    exit_code: int
    stdout: str
    stderr: str
    exec_ms: float


class SandboxInfo(BaseModel):
    sandbox_id: str
    slot_id: int
    guest_ip: str
    owner_node_id: str
    age_seconds: float
    idle_seconds: float
    idle_timeout: float
    exec_count: int


class HealthResponse(BaseModel):
    status: str
    version: str = "0.3.0"
    node_id: str
    pool_size: int
    warm_vms: int            # local asyncio.Queue depth
    ignite_warm_vms: int     # Ignite cache count (this node)
    active_sandboxes: int


# ---------------------------------------------------------------------------
# Pre-warmer
# ---------------------------------------------------------------------------
def _boot_vm_on_slot(slot: VMSlot) -> FirecrackerVM:
    """Boot a Firecracker VM and wait for SSH.  Returns the live VM object."""
    vm = FirecrackerVM(slot=slot)
    t0 = time.monotonic()
    try:
        vm.boot()
        vm.wait_for_ssh()
    except Exception:
        vm.shutdown()
        raise
    boot_s = time.monotonic() - t0
    VM_BOOT_SECONDS.observe(boot_s)
    log.info("[%s] warm VM ready on slot %d (%.1fs)", vm.vm_id, slot.slot_id, boot_s)
    return vm


async def _warmer_loop() -> None:
    """Pull free slots, boot VMs, advertise in Ignite, enqueue locally."""
    global _ignite
    while not _shutting_down:
        try:
            slot = await asyncio.wait_for(_free_slots.get(), timeout=WARMER_INTERVAL)
        except asyncio.TimeoutError:
            continue

        if _shutting_down:
            break

        try:
            vm = await asyncio.to_thread(_boot_vm_on_slot, slot)

            # Publish to Ignite warm pool (distributed, visible to all nodes)
            ignite_key = await asyncio.to_thread(
                _ignite.advertise_warm, slot.slot_id, slot.guest_ip
            )
            IGNITE_WARM_VMS.inc()

            warm = WarmVM(vm=vm, slot=slot, ignite_key=ignite_key,
                          booted_at=time.monotonic())
            _local_warm[ignite_key] = warm

            # Also enqueue locally for zero-latency same-node claiming
            await _warm_queue.put(warm)
            WARM_VMS.inc()

        except Exception:
            WARM_BOOT_FAILURES.inc()
            log.exception("warm boot failed on slot %d, returning slot", slot.slot_id)
            await _free_slots.put(slot)
            await asyncio.sleep(2.0)


# ---------------------------------------------------------------------------
# Idle reaper
# ---------------------------------------------------------------------------
async def _reaper_loop() -> None:
    while not _shutting_down:
        await asyncio.sleep(REAPER_INTERVAL)
        expired: list[str] = []
        with _sandbox_lock:
            for sid, entry in _sandboxes.items():
                if entry.is_expired:
                    expired.append(sid)

        for sid in expired:
            log.info("reaping idle sandbox %s", sid)
            await _destroy_sandbox(sid, reaped=True)


# ---------------------------------------------------------------------------
# Application lifecycle
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _all_slots, _shutting_down, _ignite

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    # Connect to Ignite
    hosts = _parse_hosts(IGNITE_HOSTS_RAW)
    log.info("connecting to Ignite at %s (node_id=%s)", hosts, NODE_ID)
    _ignite = IgnitePoolState(hosts)
    log.info("Ignite connected — caches: pandora_warm_pool, pandora_sandbox_registry")

    _all_slots = setup_network(pool_size=POOL_SIZE)
    for slot in _all_slots:
        await _free_slots.put(slot)

    warmer_tasks = [
        asyncio.create_task(_warmer_loop()) for _ in range(POOL_SIZE)
    ]
    reaper_task = asyncio.create_task(_reaper_loop())

    log.info("warming %d slots — server will accept requests immediately", POOL_SIZE)
    yield

    # ---- shutdown ----
    _shutting_down = True
    reaper_task.cancel()
    for t in warmer_tasks:
        t.cancel()

    # Drain warm queue: retract from Ignite, shut down VMs
    while not _warm_queue.empty():
        try:
            warm = _warm_queue.get_nowait()
            _ignite.retract_warm(warm.ignite_key)
            _local_warm.pop(warm.ignite_key, None)
            warm.vm.shutdown()
            WARM_VMS.dec()
            IGNITE_WARM_VMS.dec()
        except asyncio.QueueEmpty:
            break

    # Destroy active sandboxes
    for sid in list(_sandboxes):
        await _destroy_sandbox(sid)

    teardown_network(_all_slots)
    _ignite.close()
    log.info("shutdown complete")


app = FastAPI(
    title="Pandora",
    description="Ignite-backed ephemeral code execution in Firecracker microVMs",
    version="0.3.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _exec_in_sandbox(entry: SandboxEntry, code: str, timeout: float) -> ExecResponse:
    with entry._lock:
        entry.touch()
        t0 = time.monotonic()
        result = entry.vm.execute(code, timeout=timeout)
        exec_s = time.monotonic() - t0
        entry.exec_count += 1

    VM_EXEC_SECONDS.observe(exec_s)
    status = "success" if result.exit_code == 0 else "error"
    EXECUTIONS_TOTAL.labels(status=status).inc()

    # Mirror updated activity to Ignite (best-effort, don't block the response)
    try:
        _ignite.touch_sandbox(entry.vm.vm_id, entry.exec_count)
    except Exception:
        log.warning("ignite touch_sandbox failed for %s", entry.vm.vm_id)

    return ExecResponse(
        sandbox_id=entry.vm.vm_id,
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        exec_ms=round(exec_s * 1000, 2),
    )


async def _destroy_sandbox(sandbox_id: str, reaped: bool = False) -> None:
    with _sandbox_lock:
        entry = _sandboxes.pop(sandbox_id, None)
    if entry is None:
        return

    def _teardown() -> None:
        t0 = time.monotonic()
        entry.vm.shutdown()
        td_s = time.monotonic() - t0
        VM_TEARDOWN_SECONDS.observe(td_s)
        SANDBOX_LIFETIME_SECONDS.observe(entry.age_seconds)
        ACTIVE_VMS.dec()
        if reaped:
            SANDBOXES_REAPED.inc()
        # Remove from Ignite registry
        _ignite.unregister_sandbox(sandbox_id)

    await asyncio.to_thread(_teardown)
    await _free_slots.put(entry.slot)
    log.info("sandbox %s destroyed%s", sandbox_id, " (reaped)" if reaped else "")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(
        status="ok",
        node_id=NODE_ID,
        pool_size=POOL_SIZE,
        warm_vms=_warm_queue.qsize(),
        ignite_warm_vms=_ignite.local_warm_count() if _ignite else 0,
        active_sandboxes=len(_sandboxes),
    )


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics():
    return PlainTextResponse(
        content=generate_latest().decode(),
        media_type=CONTENT_TYPE_LATEST,
    )


@app.get("/sandboxes", response_model=list[SandboxInfo])
async def list_sandboxes():
    with _sandbox_lock:
        entries = list(_sandboxes.items())
    return [
        SandboxInfo(
            sandbox_id=sid,
            slot_id=e.slot.slot_id,
            guest_ip=e.slot.guest_ip,
            owner_node_id=NODE_ID,
            age_seconds=round(e.age_seconds, 1),
            idle_seconds=round(e.idle_seconds, 1),
            idle_timeout=e.idle_timeout,
            exec_count=e.exec_count,
        )
        for sid, e in entries
    ]


@app.post("/sandboxes", response_model=SandboxResponse, status_code=201)
async def create_sandbox(req: CreateSandboxRequest | None = None):
    idle_timeout = req.idle_timeout if req else DEFAULT_IDLE_TIMEOUT
    t0 = time.monotonic()

    # Phase 1: claim from local queue (same node).
    # Phase 2: if local queue is empty, scan Ignite warm_pool for remote nodes.
    try:
        warm = await asyncio.wait_for(_warm_queue.get(), timeout=90.0)
        WARM_VMS.dec()
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=503,
            detail=f"No warm VMs available (pool size {POOL_SIZE})",
        )

    acquire_s = time.monotonic() - t0
    SANDBOX_ACQUIRE_SECONDS.observe(acquire_s)

    # Atomically remove from Ignite so other nodes can't also claim it.
    claimed = await asyncio.to_thread(_ignite.claim_warm, warm.ignite_key)
    _local_warm.pop(warm.ignite_key, None)
    IGNITE_WARM_VMS.dec()
    if not claimed:
        # Should not happen in Phase 1 (we're the only node).
        # In Phase 2 this means a race was lost — put the slot back.
        log.warning("Ignite claim_warm returned False for key %s", warm.ignite_key)
        await _free_slots.put(warm.slot)
        raise HTTPException(status_code=503, detail="Warm VM was already claimed")

    # Register sandbox in Ignite
    entry = SandboxEntry(warm.vm, warm.slot, idle_timeout)
    await asyncio.to_thread(
        _ignite.register_sandbox,
        entry.vm.vm_id, warm.slot.slot_id, warm.slot.guest_ip, idle_timeout,
    )

    with _sandbox_lock:
        _sandboxes[entry.vm.vm_id] = entry
    ACTIVE_VMS.inc()
    SANDBOXES_CREATED.inc()
    log.info(
        "sandbox %s claimed from warm pool (slot %d, wait %.0fms, node=%s)",
        entry.vm.vm_id, warm.slot.slot_id, acquire_s * 1000, NODE_ID,
    )

    return SandboxResponse(
        sandbox_id=entry.vm.vm_id,
        slot_id=warm.slot.slot_id,
        guest_ip=warm.slot.guest_ip,
        owner_node_id=NODE_ID,
        warm=True,
        acquire_ms=round(acquire_s * 1000, 2),
        idle_timeout=idle_timeout,
    )


@app.post("/sandboxes/{sandbox_id}/exec", response_model=ExecResponse)
async def exec_in_sandbox(sandbox_id: str, req: ExecRequest):
    with _sandbox_lock:
        entry = _sandboxes.get(sandbox_id)

    if entry is None:
        # Phase 2: check Ignite registry to find the owner node and forward.
        record = await asyncio.to_thread(_ignite.get_sandbox_record, sandbox_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Sandbox not found")
        owner = record.get("owner_node_id")
        if owner != NODE_ID:
            # TODO Phase 2: forward to owner node via Ignite compute grid.
            raise HTTPException(
                status_code=503,
                detail=f"Sandbox lives on node '{owner}' — cross-node routing "
                       "not yet implemented (Phase 2)",
            )
        raise HTTPException(status_code=404, detail="Sandbox not found locally")

    try:
        return await asyncio.to_thread(
            _exec_in_sandbox, entry, req.code, req.timeout,
        )
    except TimeoutError as exc:
        EXECUTIONS_TOTAL.labels(status="timeout").inc()
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except Exception as exc:
        EXECUTIONS_TOTAL.labels(status="error").inc()
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.delete("/sandboxes/{sandbox_id}", status_code=204)
async def delete_sandbox(sandbox_id: str):
    with _sandbox_lock:
        if sandbox_id not in _sandboxes:
            raise HTTPException(status_code=404, detail="Sandbox not found")
    await _destroy_sandbox(sandbox_id)
