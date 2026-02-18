#!/usr/bin/env python3
"""
Pandora – Ephemeral Interpreter

Orchestrates Firecracker microVM lifecycles: boots a sandboxed guest with
strict network isolation, captures timing telemetry, and guarantees cleanup
on every exit path.

Designed for Ubuntu 24.04 hosts running inside a Lima envelope on macOS.
"""

import json
import logging
import platform
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests_unixsocket  # speaks HTTP over a UNIX domain socket

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
FIRECRACKER_BIN = BASE_DIR / "bin" / "firecracker"
KERNEL_IMAGE = BASE_DIR / "images" / "hello-vmlinux.bin"
ROOTFS_IMAGE = BASE_DIR / "images" / "hello-rootfs.ext4"
API_SOCKET = Path("/tmp/firecracker.socket")
METRICS_PATH = BASE_DIR / "logs" / "vm_metrics.json"
NETWORK_SCRIPT = BASE_DIR / "setup_network.sh"

# Boot arguments configure the guest kernel's console, static IP, and panic
# behaviour.  The ip= parameter follows the kernel's ip= autoconfig syntax:
#   ip=<client-ip>::<gateway>:<netmask>::<device>:<autoconf>
# Setting autoconf to "off" disables DHCP – the microVM comes up instantly
# with a deterministic address, which matters for sub-second boot targets.
#
# On aarch64, "keep_bootcon" must be prepended so the early boot console
# isn't torn down before the kernel finishes printing — without it the
# guest may appear to hang on ARM64 hosts.
_BOOT_ARGS_BASE = (
    "console=ttyS0 reboot=k panic=1 pci=off "
    "ip=172.16.0.2::172.16.0.1:255.255.255.0::eth0:off"
)
BOOT_ARGS = (
    f"keep_bootcon {_BOOT_ARGS_BASE}"
    if platform.machine() == "aarch64"
    else _BOOT_ARGS_BASE
)

SOCKET_POLL_INTERVAL: float = 0.05   # 50 ms between socket-ready polls
SOCKET_POLL_TIMEOUT: float = 5.0     # give up after 5 s

log = logging.getLogger("pandora")


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
class MetricsLogger:
    """Collects timestamped lifecycle events and flushes them as JSON.

    Every microVM boot produces a single metrics document written to
    ``logs/vm_metrics.json`` so downstream tooling (Prometheus node-exporter
    textfile collector, Datadog agent, or a simple ``jq`` pipeline) can
    ingest it without parsing unstructured logs.
    """

    def __init__(self, path: Path = METRICS_PATH) -> None:
        self._path = path
        self._events: dict[str, float] = {}
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def mark(self, label: str) -> None:
        """Record a monotonic timestamp for *label*."""
        self._events[label] = time.monotonic()
        log.info("metric  %-20s  t=%.4f", label, self._events[label])

    def _epoch(self, label: str) -> float | None:
        return self._events.get(label)

    def boot_latency_ms(self) -> float | None:
        """Wall-clock milliseconds from process start to VM instance-start."""
        t0 = self._epoch("t_start")
        t1 = self._epoch("t_booted")
        if t0 is not None and t1 is not None:
            return round((t1 - t0) * 1000, 2)
        return None

    def socket_ready_latency_ms(self) -> float | None:
        """Milliseconds from Firecracker launch to the API socket responding."""
        t0 = self._epoch("t_start")
        t1 = self._epoch("t_socket_ready")
        if t0 is not None and t1 is not None:
            return round((t1 - t0) * 1000, 2)
        return None

    def flush(self) -> None:
        """Persist the collected metrics as a JSON document."""
        doc: dict[str, Any] = {
            "events": {k: v for k, v in self._events.items()},
            "boot_latency_ms": self.boot_latency_ms(),
            "socket_ready_latency_ms": self.socket_ready_latency_ms(),
        }
        self._path.write_text(json.dumps(doc, indent=2) + "\n")
        log.info("metrics flushed to %s", self._path)


# ---------------------------------------------------------------------------
# Firecracker VM
# ---------------------------------------------------------------------------
@dataclass
class FirecrackerVM:
    """Manages a single Firecracker microVM through its full lifecycle.

    Lifecycle:  setup_network → start firecracker process → wait for socket
    → configure_boot_source → configure_drive → configure_network
    → start_instance → … → cleanup
    """

    kernel: Path = KERNEL_IMAGE
    rootfs: Path = ROOTFS_IMAGE
    socket_path: Path = API_SOCKET
    boot_args: str = BOOT_ARGS
    metrics: MetricsLogger = field(default_factory=MetricsLogger)

    _fc_process: subprocess.Popen[bytes] | None = field(
        default=None, init=False, repr=False
    )
    _session: requests_unixsocket.Session | None = field(
        default=None, init=False, repr=False
    )

    # -- internal helpers ----------------------------------------------------

    def _api_url(self, path: str) -> str:
        """Build a requests-unixsocket URL.

        The library encodes the socket path inside the URL's host component
        using percent-encoding, so ``/tmp/firecracker.socket`` becomes
        ``http+unix://%2Ftmp%2Ffirecracker.socket/…``.
        """
        encoded = str(self.socket_path).replace("/", "%2F")
        return f"http+unix://{encoded}{path}"

    def _put(self, path: str, payload: dict[str, Any]) -> requests_unixsocket.Session:
        """PUT JSON to the Firecracker API and assert success."""
        assert self._session is not None
        resp = self._session.put(self._api_url(path), json=payload)
        if resp.status_code not in (200, 204):
            raise RuntimeError(
                f"Firecracker API error on PUT {path}: "
                f"{resp.status_code} – {resp.text}"
            )
        log.info("PUT %-30s  → %s", path, resp.status_code)
        return resp

    # -- lifecycle methods ---------------------------------------------------

    def setup_network(self) -> None:
        """Invoke setup_network.sh to create the TAP device and NAT rules.

        We shell out here because network plumbing requires CAP_NET_ADMIN
        and raw iptables calls.  Keeping it in a separate bash script makes
        it easy to audit and to run/teardown independently of the Python
        orchestrator.
        """
        log.info("setting up host network (tap0, NAT)")
        subprocess.run(
            ["sudo", str(NETWORK_SCRIPT)],
            check=True,
        )

    def _launch_firecracker(self) -> None:
        """Start the Firecracker VMM process in the background.

        Firecracker listens on a UNIX domain socket for its REST API.
        We remove any stale socket file first – if one is left over from a
        previous crashed run, Firecracker will refuse to start.
        """
        if self.socket_path.exists():
            log.warning("removing stale socket %s", self.socket_path)
            self.socket_path.unlink()

        log.info("launching firecracker → %s", FIRECRACKER_BIN)
        self._fc_process = subprocess.Popen(
            [
                str(FIRECRACKER_BIN),
                "--api-sock",
                str(self.socket_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _wait_for_socket(self) -> None:
        """Block until the Firecracker API socket is accepting connections.

        Firecracker creates the socket file almost instantly, but it may not
        be *accepting* connections yet.  We poll with a short backoff so the
        first successful PUT doesn't race with socket bind().
        """
        deadline = time.monotonic() + SOCKET_POLL_TIMEOUT
        self._session = requests_unixsocket.Session()

        while time.monotonic() < deadline:
            if self.socket_path.exists():
                try:
                    # A lightweight GET that returns machine configuration.
                    resp = self._session.get(self._api_url("/"))
                    if resp.status_code in (200, 400):
                        # 400 is fine – it means the API is up but no config
                        # has been pushed yet.
                        self.metrics.mark("t_socket_ready")
                        log.info("API socket ready")
                        return
                except Exception:
                    pass
            time.sleep(SOCKET_POLL_INTERVAL)

        raise TimeoutError(
            f"Firecracker API socket not ready after {SOCKET_POLL_TIMEOUT}s"
        )

    def configure_boot_source(self) -> None:
        """Tell Firecracker which kernel to load and how to boot it.

        The boot_args string is passed verbatim to the guest kernel's
        command line.  The ``ip=`` stanza sets a static network config so
        the guest doesn't need a DHCP server – shaving ~2 s off boot time.
        """
        self._put(
            "/boot-source",
            {
                "kernel_image_path": str(self.kernel),
                "boot_args": self.boot_args,
            },
        )

    def configure_drive(self) -> None:
        """Attach the root filesystem as a virtio block device.

        ``is_root_device=True`` tells the guest kernel to mount this as ``/``.
        ``is_read_only=False`` allows the guest to write – useful for scratch
        space during code execution, though ephemeral by design.
        """
        self._put(
            "/drives/rootfs",
            {
                "drive_id": "rootfs",
                "path_on_host": str(self.rootfs),
                "is_root_device": True,
                "is_read_only": False,
            },
        )

    def configure_network_interface(self) -> None:
        """Attach the TAP device as the guest's sole NIC.

        Firecracker exposes this to the guest as a virtio-net device.  The
        guest sees it as ``eth0`` and applies the static IP from boot_args.
        The host side of the TAP is managed by setup_network.sh.
        """
        self._put(
            "/network-interfaces/eth0",
            {
                "iface_id": "eth0",
                "host_dev_name": "tap0",
            },
        )

    def start_instance(self) -> None:
        """Issue the InstanceStart action.

        After this call Firecracker loads the kernel, mounts the rootfs, and
        boots into the guest init process.  The API is synchronous – when
        the PUT returns, the guest is running.
        """
        self._put("/actions", {"action_type": "InstanceStart"})
        self.metrics.mark("t_booted")
        log.info(
            "instance started  (boot_latency=%.2f ms)",
            self.metrics.boot_latency_ms() or 0,
        )

    def cleanup(self) -> None:
        """Tear down everything: kill Firecracker, remove socket, drop TAP.

        Called from a ``finally`` block so it runs even on unhandled
        exceptions or SIGINT.  Order matters: stop the process first, then
        clean up its artefacts, then remove the network plumbing.
        """
        log.info("cleanup: starting teardown sequence")

        if self._fc_process is not None:
            log.info("cleanup: terminating firecracker (pid %d)", self._fc_process.pid)
            self._fc_process.terminate()
            try:
                self._fc_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log.warning("cleanup: firecracker did not exit, sending SIGKILL")
                self._fc_process.kill()
                self._fc_process.wait()
            self._fc_process = None

        if self.socket_path.exists():
            self.socket_path.unlink()
            log.info("cleanup: removed socket %s", self.socket_path)

        # Tear down the TAP interface and iptables rules.
        try:
            subprocess.run(
                ["sudo", str(NETWORK_SCRIPT), "teardown"],
                check=True,
            )
        except subprocess.CalledProcessError:
            log.exception("cleanup: network teardown failed")

        log.info("cleanup: done")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    vm = FirecrackerVM()
    vm.metrics.mark("t_start")

    # Register SIGINT/SIGTERM so Ctrl-C during a long boot still cleans up.
    def _signal_handler(signum: int, _frame: Any) -> None:
        log.warning("caught signal %s – cleaning up", signal.Signals(signum).name)
        vm.cleanup()
        sys.exit(128 + signum)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    try:
        vm.setup_network()
        vm._launch_firecracker()
        vm._wait_for_socket()
        vm.configure_boot_source()
        vm.configure_drive()
        vm.configure_network_interface()
        vm.start_instance()

        log.info("microVM is live – press Ctrl-C to tear down")
        # Poll instead of blocking on wait() — a blocking waitpid() can
        # prevent Python from dispatching SIGINT when running under sudo.
        # Short sleeps let the interpreter check for pending signals.
        while vm._fc_process and vm._fc_process.poll() is None:
            time.sleep(0.5)

    except KeyboardInterrupt:
        log.info("Ctrl-C received – shutting down")

    except Exception:
        log.exception("fatal error during VM lifecycle")
        raise

    finally:
        vm.metrics.flush()
        vm.cleanup()


if __name__ == "__main__":
    main()
