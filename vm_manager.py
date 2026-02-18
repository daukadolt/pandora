"""
Pandora – Ephemeral Interpreter

Low-level Firecracker microVM lifecycle management with SSH-based command
execution.  This module is imported by the API server — it is not meant to
be run directly (though a quick self-test is available via ``__main__``).
"""

import logging
import platform
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import paramiko
import requests_unixsocket

log = logging.getLogger("pandora.vm")

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
FIRECRACKER_BIN = BASE_DIR / "bin" / "firecracker"
KERNEL_IMAGE = BASE_DIR / "images" / "hello-vmlinux.bin"
ROOTFS_IMAGE = BASE_DIR / "images" / "hello-rootfs.ext4"
API_SOCKET = Path("/tmp/firecracker.socket")
NETWORK_SCRIPT = BASE_DIR / "setup_network.sh"
SSH_KEY_PATH = BASE_DIR / "keys" / "pandora"
GUEST_IP = "172.16.0.2"

_BOOT_ARGS_BASE = (
    "console=ttyS0 reboot=k panic=1 pci=off "
    f"ip={GUEST_IP}::172.16.0.1:255.255.255.0::eth0:off"
)
BOOT_ARGS = (
    f"keep_bootcon {_BOOT_ARGS_BASE}"
    if platform.machine() == "aarch64"
    else _BOOT_ARGS_BASE
)

SOCKET_POLL_INTERVAL: float = 0.05
SOCKET_POLL_TIMEOUT: float = 5.0
SSH_POLL_INTERVAL: float = 0.5
SSH_POLL_TIMEOUT: float = 30.0


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ExecResult:
    """Captured output from a command executed inside a microVM."""

    exit_code: int
    stdout: str
    stderr: str


# ---------------------------------------------------------------------------
# Network helpers (called once by the API server, not per-VM)
# ---------------------------------------------------------------------------
def setup_network() -> None:
    """Create tap0, assign gateway IP, configure NAT."""
    log.info("setting up host network (tap0, NAT)")
    subprocess.run(["sudo", str(NETWORK_SCRIPT)], check=True)


def teardown_network() -> None:
    """Remove tap0 and associated iptables rules."""
    log.info("tearing down host network")
    try:
        subprocess.run(
            ["sudo", str(NETWORK_SCRIPT), "teardown"],
            check=True,
        )
    except subprocess.CalledProcessError:
        log.exception("network teardown failed")


# ---------------------------------------------------------------------------
# Firecracker VM
# ---------------------------------------------------------------------------
@dataclass
class FirecrackerVM:
    """Manages a single Firecracker microVM through boot → execute → shutdown.

    The network plumbing (tap0, NAT) is assumed to already exist — call
    ``setup_network()`` once before creating any VM instances.
    """

    kernel: Path = KERNEL_IMAGE
    rootfs: Path = ROOTFS_IMAGE
    socket_path: Path = API_SOCKET
    boot_args: str = BOOT_ARGS
    ssh_key_path: Path = SSH_KEY_PATH
    guest_ip: str = GUEST_IP

    _fc_process: subprocess.Popen[bytes] | None = field(
        default=None, init=False, repr=False
    )
    _session: requests_unixsocket.Session | None = field(
        default=None, init=False, repr=False
    )

    # -- Firecracker REST helpers -------------------------------------------

    def _api_url(self, path: str) -> str:
        encoded = str(self.socket_path).replace("/", "%2F")
        return f"http+unix://{encoded}{path}"

    def _put(self, path: str, payload: dict[str, Any]) -> None:
        assert self._session is not None
        resp = self._session.put(self._api_url(path), json=payload)
        if resp.status_code not in (200, 204):
            raise RuntimeError(
                f"Firecracker API error on PUT {path}: "
                f"{resp.status_code} – {resp.text}"
            )
        log.debug("PUT %-30s → %s", path, resp.status_code)

    # -- Lifecycle ----------------------------------------------------------

    def _preflight(self) -> None:
        """Verify that required files exist before attempting a boot."""
        missing = [
            (self.kernel, "kernel image"),
            (self.rootfs, "root filesystem"),
            (Path(FIRECRACKER_BIN), "firecracker binary"),
            (self.ssh_key_path, "SSH private key (run prepare_rootfs.sh)"),
        ]
        for path, label in missing:
            if not path.exists():
                raise FileNotFoundError(
                    f"{label} not found at {path}"
                )

    def boot(self) -> None:
        """Launch Firecracker, push config, start the guest.

        After this method returns the guest kernel is running and init has
        been invoked, but sshd may not be ready yet — call
        ``wait_for_ssh()`` before ``execute()``.
        """
        self._preflight()

        if self.socket_path.exists():
            log.warning("removing stale socket %s", self.socket_path)
            self.socket_path.unlink()

        self._fc_process = subprocess.Popen(
            [str(FIRECRACKER_BIN), "--api-sock", str(self.socket_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._wait_for_api_socket()

        self._put("/boot-source", {
            "kernel_image_path": str(self.kernel),
            "boot_args": self.boot_args,
        })
        self._put("/drives/rootfs", {
            "drive_id": "rootfs",
            "path_on_host": str(self.rootfs),
            "is_root_device": True,
            "is_read_only": False,
        })
        self._put("/network-interfaces/eth0", {
            "iface_id": "eth0",
            "host_dev_name": "tap0",
        })
        self._put("/actions", {"action_type": "InstanceStart"})
        log.info("VM instance started")

    def wait_for_ssh(self) -> None:
        """Block until sshd inside the guest accepts connections."""
        deadline = time.monotonic() + SSH_POLL_TIMEOUT
        key = paramiko.Ed25519Key.from_private_key_file(str(self.ssh_key_path))

        while time.monotonic() < deadline:
            try:
                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                client.connect(
                    self.guest_ip,
                    port=22,
                    username="root",
                    pkey=key,
                    timeout=2,
                    banner_timeout=5,
                    auth_timeout=5,
                )
                client.close()
                log.info("SSH ready on %s", self.guest_ip)
                return
            except Exception:
                time.sleep(SSH_POLL_INTERVAL)

        raise TimeoutError(f"SSH not ready after {SSH_POLL_TIMEOUT}s")

    def execute(self, command: str, timeout: float = 30.0) -> ExecResult:
        """SSH into the guest, run *command*, and return captured output."""
        key = paramiko.Ed25519Key.from_private_key_file(str(self.ssh_key_path))
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            self.guest_ip,
            port=22,
            username="root",
            pkey=key,
            timeout=5,
        )
        try:
            _, stdout, stderr = client.exec_command(command, timeout=timeout)
            exit_code = stdout.channel.recv_exit_status()
            return ExecResult(
                exit_code=exit_code,
                stdout=stdout.read().decode(errors="replace"),
                stderr=stderr.read().decode(errors="replace"),
            )
        finally:
            client.close()

    def shutdown(self) -> None:
        """Terminate Firecracker and remove the API socket."""
        if self._fc_process is not None:
            log.info("terminating firecracker (pid %d)", self._fc_process.pid)
            self._fc_process.terminate()
            try:
                self._fc_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log.warning("SIGKILL escalation")
                self._fc_process.kill()
                self._fc_process.wait()
            self._fc_process = None

        if self.socket_path.exists():
            self.socket_path.unlink()
        log.info("VM shut down")

    # -- Internal -----------------------------------------------------------

    def _wait_for_api_socket(self) -> None:
        deadline = time.monotonic() + SOCKET_POLL_TIMEOUT
        self._session = requests_unixsocket.Session()
        while time.monotonic() < deadline:
            if self.socket_path.exists():
                try:
                    resp = self._session.get(self._api_url("/"))
                    if resp.status_code in (200, 400):
                        log.debug("Firecracker API socket ready")
                        return
                except Exception:
                    pass
            time.sleep(SOCKET_POLL_INTERVAL)
        raise TimeoutError(
            f"Firecracker API socket not ready after {SOCKET_POLL_TIMEOUT}s"
        )


# ---------------------------------------------------------------------------
# Quick self-test (run with:  sudo .venv/bin/python vm_manager.py)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    setup_network()
    vm = FirecrackerVM()
    try:
        vm.boot()
        vm.wait_for_ssh()
        result = vm.execute("echo 'hello from inside the microVM' && uname -a")
        print(f"\nexit_code={result.exit_code}")
        print(f"stdout={result.stdout}")
        print(f"stderr={result.stderr}")
    finally:
        vm.shutdown()
        teardown_network()
