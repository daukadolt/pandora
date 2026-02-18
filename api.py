"""
Pandora – API Server

FastAPI application that exposes ephemeral code execution via Firecracker
microVMs.  Every request boots a fresh VM, runs the command over SSH, tears
it down, and records Prometheus metrics for each lifecycle phase.

Start with:  sudo .venv/bin/uvicorn api:app --host 0.0.0.0 --port 8000
"""

import asyncio
import logging
import time
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

from vm_manager import FirecrackerVM, setup_network, teardown_network

log = logging.getLogger("pandora.api")

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------
VM_BOOT_SECONDS = Histogram(
    "pandora_vm_boot_seconds",
    "Time to boot a Firecracker microVM (FC start through InstanceStart)",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0),
)
VM_SSH_READY_SECONDS = Histogram(
    "pandora_vm_ssh_ready_seconds",
    "Time from InstanceStart until sshd accepts connections",
    buckets=(0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 15.0),
)
VM_EXEC_SECONDS = Histogram(
    "pandora_vm_exec_seconds",
    "Wall-clock time to execute the user command over SSH",
    buckets=(0.05, 0.1, 0.5, 1.0, 5.0, 10.0, 30.0),
)
VM_TEARDOWN_SECONDS = Histogram(
    "pandora_vm_teardown_seconds",
    "Time to terminate Firecracker and clean up the socket",
    buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 5.0),
)
VM_E2E_SECONDS = Histogram(
    "pandora_vm_e2e_seconds",
    "Total wall-clock time for a complete /execute request",
    buckets=(1.0, 2.0, 5.0, 10.0, 30.0, 60.0),
)
EXECUTIONS_TOTAL = Counter(
    "pandora_executions_total",
    "Total code executions by outcome",
    ["status"],
)
ACTIVE_VMS = Gauge(
    "pandora_active_vms",
    "Number of microVMs currently running",
)

# One VM at a time — we have a single TAP device and a single guest IP.
_exec_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------
class ExecuteRequest(BaseModel):
    code: str = Field(..., description="Shell command(s) to execute in the microVM")
    timeout: float = Field(
        default=30.0, ge=1, le=300,
        description="Max seconds for the command itself (excludes boot/teardown)",
    )


class ExecuteResponse(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    boot_ms: float
    ssh_ready_ms: float
    exec_ms: float
    teardown_ms: float
    total_ms: float


class HealthResponse(BaseModel):
    status: str
    version: str = "0.1.0"


# ---------------------------------------------------------------------------
# Application lifecycle
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    setup_network()
    log.info("network ready — accepting requests")
    yield
    teardown_network()
    log.info("shutdown complete")


app = FastAPI(
    title="Pandora",
    description="Ephemeral code execution inside Firecracker microVMs",
    version="0.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(status="ok")


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics():
    return PlainTextResponse(
        content=generate_latest().decode(),
        media_type=CONTENT_TYPE_LATEST,
    )


@app.post("/execute", response_model=ExecuteResponse)
async def execute(req: ExecuteRequest):
    async with _exec_lock:
        return await asyncio.to_thread(_execute_sync, req.code, req.timeout)


# ---------------------------------------------------------------------------
# Synchronous execution (runs in a thread, behind the lock)
# ---------------------------------------------------------------------------
def _execute_sync(code: str, timeout: float) -> ExecuteResponse:
    t_total = time.monotonic()
    vm = FirecrackerVM()

    try:
        ACTIVE_VMS.inc()

        t0 = time.monotonic()
        vm.boot()
        boot_s = time.monotonic() - t0
        VM_BOOT_SECONDS.observe(boot_s)

        t0 = time.monotonic()
        vm.wait_for_ssh()
        ssh_ready_s = time.monotonic() - t0
        VM_SSH_READY_SECONDS.observe(ssh_ready_s)

        t0 = time.monotonic()
        result = vm.execute(code, timeout=timeout)
        exec_s = time.monotonic() - t0
        VM_EXEC_SECONDS.observe(exec_s)

        status = "success" if result.exit_code == 0 else "error"
        EXECUTIONS_TOTAL.labels(status=status).inc()

    except TimeoutError as exc:
        EXECUTIONS_TOTAL.labels(status="timeout").inc()
        raise HTTPException(status_code=504, detail=str(exc)) from exc

    except HTTPException:
        raise

    except Exception as exc:
        EXECUTIONS_TOTAL.labels(status="error").inc()
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    finally:
        t0 = time.monotonic()
        vm.shutdown()
        teardown_s = time.monotonic() - t0
        VM_TEARDOWN_SECONDS.observe(teardown_s)
        ACTIVE_VMS.dec()

    total_s = time.monotonic() - t_total
    VM_E2E_SECONDS.observe(total_s)

    return ExecuteResponse(
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        boot_ms=round(boot_s * 1000, 2),
        ssh_ready_ms=round(ssh_ready_s * 1000, 2),
        exec_ms=round(exec_s * 1000, 2),
        teardown_ms=round(teardown_s * 1000, 2),
        total_ms=round(total_s * 1000, 2),
    )
