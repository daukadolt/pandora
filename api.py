"""
Pandora – API Server

Session-based ephemeral code execution inside Firecracker microVMs.

Lifecycle:
    POST   /sandboxes              → boot a sandbox, get an ID back
    POST   /sandboxes/{id}/exec    → run a command in the living sandbox
    DELETE /sandboxes/{id}          → tear it down
    GET    /sandboxes              → list active sandboxes
    GET    /health                 → pool status
    GET    /metrics                → Prometheus format

Sandboxes that sit idle for longer than their configured timeout are
reaped automatically by a background task.

Start with:  sudo .venv/bin/uvicorn api:app --host 0.0.0.0 --port 8000
"""

import asyncio
import logging
import os
import time
import threading
from contextlib import asynccontextmanager

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

log = logging.getLogger("pandora.api")

POOL_SIZE = int(os.environ.get("PANDORA_POOL_SIZE", "4"))
DEFAULT_IDLE_TIMEOUT = float(os.environ.get("PANDORA_IDLE_TIMEOUT", "60"))
REAPER_INTERVAL = 5.0

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------
VM_BOOT_SECONDS = Histogram(
    "pandora_vm_boot_seconds",
    "Time to boot a Firecracker microVM (FC start through SSH ready)",
    buckets=(0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 15.0),
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
ACTIVE_VMS = Gauge(
    "pandora_active_vms",
    "Number of microVMs currently running",
)
POOL_AVAILABLE = Gauge(
    "pandora_pool_available_slots",
    "Number of VM slots currently idle in the pool",
)


# ---------------------------------------------------------------------------
# Sandbox state
# ---------------------------------------------------------------------------
class SandboxEntry:
    """Server-side state for one living sandbox."""

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


_sandboxes: dict[str, SandboxEntry] = {}
_sandbox_lock = threading.Lock()


# ---------------------------------------------------------------------------
# VM slot pool
# ---------------------------------------------------------------------------
class VMPool:
    def __init__(self, slots: list[VMSlot]) -> None:
        self._queue: asyncio.Queue[VMSlot] = asyncio.Queue()
        for s in slots:
            self._queue.put_nowait(s)
        self.size = len(slots)
        POOL_AVAILABLE.set(self.size)

    async def acquire(self) -> VMSlot:
        slot = await self._queue.get()
        POOL_AVAILABLE.dec()
        return slot

    async def release(self, slot: VMSlot) -> None:
        await self._queue.put(slot)
        POOL_AVAILABLE.inc()

    def release_sync(self, slot: VMSlot) -> None:
        """Thread-safe release used by the reaper (runs outside event loop)."""
        loop = _event_loop
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(self.release(slot), loop)
        else:
            self._queue.put_nowait(slot)
            POOL_AVAILABLE.inc()


_pool: VMPool | None = None
_slots: list[VMSlot] = []
_event_loop: asyncio.AbstractEventLoop | None = None


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
    boot_ms: float
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
    age_seconds: float
    idle_seconds: float
    idle_timeout: float
    exec_count: int


class HealthResponse(BaseModel):
    status: str
    version: str = "0.1.0"
    pool_size: int
    available_slots: int
    active_sandboxes: int


# ---------------------------------------------------------------------------
# Idle reaper
# ---------------------------------------------------------------------------
async def _reaper_loop() -> None:
    """Periodically kill sandboxes that have been idle too long."""
    while True:
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
    global _pool, _slots, _event_loop
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    _event_loop = asyncio.get_running_loop()
    _slots = setup_network(pool_size=POOL_SIZE)
    _pool = VMPool(_slots)
    reaper_task = asyncio.create_task(_reaper_loop())
    log.info("pool ready — %d slots, idle timeout %ds", POOL_SIZE, DEFAULT_IDLE_TIMEOUT)
    yield
    reaper_task.cancel()
    for sid in list(_sandboxes):
        await _destroy_sandbox(sid)
    teardown_network(_slots)
    log.info("shutdown complete")


app = FastAPI(
    title="Pandora",
    description="Session-based ephemeral code execution in Firecracker microVMs",
    version="0.2.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _boot_sandbox(slot: VMSlot, idle_timeout: float) -> tuple[SandboxEntry, float]:
    """Synchronous: boot VM + wait for SSH. Returns entry and boot time in seconds."""
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
    entry = SandboxEntry(vm, slot, idle_timeout)
    return entry, boot_s


def _exec_in_sandbox(entry: SandboxEntry, code: str, timeout: float) -> ExecResponse:
    """Synchronous: run a command inside an existing sandbox."""
    with entry._lock:
        entry.touch()
        t0 = time.monotonic()
        result = entry.vm.execute(code, timeout=timeout)
        exec_s = time.monotonic() - t0
        entry.exec_count += 1

    VM_EXEC_SECONDS.observe(exec_s)
    status = "success" if result.exit_code == 0 else "error"
    EXECUTIONS_TOTAL.labels(status=status).inc()

    return ExecResponse(
        sandbox_id=entry.vm.vm_id,
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        exec_ms=round(exec_s * 1000, 2),
    )


async def _destroy_sandbox(sandbox_id: str, reaped: bool = False) -> None:
    """Shut down a sandbox and release its slot back to the pool."""
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

    await asyncio.to_thread(_teardown)
    assert _pool is not None
    await _pool.release(entry.slot)
    log.info("sandbox %s destroyed%s", sandbox_id, " (reaped)" if reaped else "")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health", response_model=HealthResponse)
async def health():
    assert _pool is not None
    return HealthResponse(
        status="ok",
        pool_size=_pool.size,
        available_slots=_pool._queue.qsize(),
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
            age_seconds=round(e.age_seconds, 1),
            idle_seconds=round(e.idle_seconds, 1),
            idle_timeout=e.idle_timeout,
            exec_count=e.exec_count,
        )
        for sid, e in entries
    ]


@app.post("/sandboxes", response_model=SandboxResponse, status_code=201)
async def create_sandbox(req: CreateSandboxRequest | None = None):
    assert _pool is not None
    idle_timeout = req.idle_timeout if req else DEFAULT_IDLE_TIMEOUT

    try:
        slot = await asyncio.wait_for(_pool.acquire(), timeout=5.0)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=503,
            detail=f"No VM slots available (pool size {_pool.size})",
        )

    try:
        entry, boot_s = await asyncio.to_thread(_boot_sandbox, slot, idle_timeout)
    except Exception as exc:
        await _pool.release(slot)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    with _sandbox_lock:
        _sandboxes[entry.vm.vm_id] = entry
    ACTIVE_VMS.inc()
    SANDBOXES_CREATED.inc()
    log.info("sandbox %s created on slot %d", entry.vm.vm_id, slot.slot_id)

    return SandboxResponse(
        sandbox_id=entry.vm.vm_id,
        slot_id=slot.slot_id,
        guest_ip=slot.guest_ip,
        boot_ms=round(boot_s * 1000, 2),
        idle_timeout=idle_timeout,
    )


@app.post("/sandboxes/{sandbox_id}/exec", response_model=ExecResponse)
async def exec_in_sandbox(sandbox_id: str, req: ExecRequest):
    with _sandbox_lock:
        entry = _sandboxes.get(sandbox_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Sandbox not found")

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
