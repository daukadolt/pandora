"""
Pandora – Ephemeral Interpreter

Low-level Firecracker microVM lifecycle management with SSH-based command
execution.  Supports multiple concurrent VMs, each on its own TAP device
and IP address.

This module is imported by the API server — it is not meant to be run
directly (though a quick self-test is available via ``__main__``).
"""

import logging
import platform
import shutil
import subprocess
import time
import uuid
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
NETWORK_SCRIPT = BASE_DIR / "setup_network.sh"
SSH_KEY_PATH = BASE_DIR / "keys" / "pandora"

SOCKET_POLL_INTERVAL: float = 0.05
SOCKET_POLL_TIMEOUT: float = 5.0
SSH_POLL_INTERVAL: float = 0.5
SSH_POLL_TIMEOUT: float = 60.0


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
# VM slot – per-VM network identity
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class VMSlot:
    """A numbered slot that provides a unique TAP device, IP pair, and socket.

    Addressing scheme (each slot gets a /30 subnet):
        slot 0 → tap0,  host 172.16.0.1,  guest 172.16.0.2
        slot 1 → tap1,  host 172.16.0.5,  guest 172.16.0.6
        slot 2 → tap2,  host 172.16.0.9,  guest 172.16.0.10
        ...
    A /30 gives exactly 2 usable addresses per slot.
    """

    slot_id: int

    @property
    def tap_name(self) -> str:
        return f"tap{self.slot_id}"

    @property
    def host_ip(self) -> str:
        return f"172.16.0.{self.slot_id * 4 + 1}"

    @property
    def guest_ip(self) -> str:
        return f"172.16.0.{self.slot_id * 4 + 2}"

    @property
    def host_cidr(self) -> str:
        return f"{self.host_ip}/30"

    @property
    def socket_path(self) -> Path:
        return Path(f"/tmp/firecracker-{self.slot_id}.socket")

    @property
    def boot_args(self) -> str:
        base = (
            f"console=ttyS0 reboot=k panic=1 pci=off "
            f"ip={self.guest_ip}::{self.host_ip}:255.255.255.252::eth0:off"
        )
        if platform.machine() == "aarch64":
            return f"keep_bootcon {base}"
        return base

    # -- TAP lifecycle -------------------------------------------------------

    def create_tap(self) -> None:
        """Create and bring up the TAP device for this slot."""
        self.destroy_tap()
        subprocess.run(
            ["ip", "tuntap", "add", "dev", self.tap_name, "mode", "tap"],
            check=True,
        )
        subprocess.run(
            ["ip", "addr", "add", self.host_cidr, "dev", self.tap_name],
            check=True,
        )
        subprocess.run(
            ["ip", "link", "set", self.tap_name, "up"],
            check=True,
        )
        log.info("slot %d: %s up with %s", self.slot_id, self.tap_name, self.host_cidr)

    def destroy_tap(self) -> None:
        """Tear down the TAP device (idempotent)."""
        subprocess.run(
            ["ip", "link", "set", self.tap_name, "down"],
            capture_output=True,
        )
        subprocess.run(
            ["ip", "tuntap", "del", "dev", self.tap_name, "mode", "tap"],
            capture_output=True,
        )


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------
def setup_network(pool_size: int = 4) -> list[VMSlot]:
    """Set up subnet-level rules and create TAP devices for *pool_size* slots."""
    log.info("setting up subnet networking + %d TAP slots", pool_size)
    subprocess.run(["sudo", str(NETWORK_SCRIPT)], check=True)

    slots: list[VMSlot] = []
    for i in range(pool_size):
        slot = VMSlot(i)
        slot.create_tap()
        slots.append(slot)
    return slots


def teardown_network(slots: list[VMSlot]) -> None:
    """Destroy all TAP devices and remove subnet-level rules."""
    for slot in slots:
        slot.destroy_tap()
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

    Each VM is bound to a ``VMSlot`` that provides its unique network identity
    (TAP device, IP addresses, socket path).

    The rootfs image is copied to a per-VM temp file so that multiple VMs
    can run concurrently without corrupting each other's filesystems.
    """

    slot: VMSlot
    kernel: Path = KERNEL_IMAGE
    rootfs: Path = ROOTFS_IMAGE
    ssh_key_path: Path = SSH_KEY_PATH

    vm_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    _fc_process: subprocess.Popen[bytes] | None = field(
        default=None, init=False, repr=False
    )
    _session: requests_unixsocket.Session | None = field(
        default=None, init=False, repr=False
    )
    _rootfs_copy: Path | None = field(
        default=None, init=False, repr=False
    )

    # -- Firecracker REST helpers -------------------------------------------

    def _api_url(self, path: str) -> str:
        encoded = str(self.slot.socket_path).replace("/", "%2F")
        return f"http+unix://{encoded}{path}"

    def _put(self, path: str, payload: dict[str, Any]) -> None:
        assert self._session is not None
        resp = self._session.put(self._api_url(path), json=payload)
        if resp.status_code not in (200, 204):
            raise RuntimeError(
                f"[{self.vm_id}] Firecracker API error on PUT {path}: "
                f"{resp.status_code} – {resp.text}"
            )
        log.debug("[%s] PUT %-30s → %s", self.vm_id, path, resp.status_code)

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
                raise FileNotFoundError(f"{label} not found at {path}")

    def _prepare_rootfs(self) -> Path:
        """Copy the golden rootfs image to a per-VM temp file.

        Each Firecracker instance opens its rootfs read-write, so concurrent
        VMs must not share the same file.
        """
        copy_path = Path(f"/tmp/pandora-rootfs-{self.vm_id}.ext4")
        shutil.copy2(self.rootfs, copy_path)
        self._rootfs_copy = copy_path
        log.debug("[%s] rootfs copied to %s", self.vm_id, copy_path)
        return copy_path

    def boot(self) -> None:
        """Launch Firecracker, push config, start the guest."""
        self._preflight()

        sock = self.slot.socket_path
        if sock.exists():
            sock.unlink()

        rootfs_path = self._prepare_rootfs()

        self._fc_process = subprocess.Popen(
            [str(FIRECRACKER_BIN), "--api-sock", str(sock)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._wait_for_api_socket()

        self._put("/boot-source", {
            "kernel_image_path": str(self.kernel),
            "boot_args": self.slot.boot_args,
        })
        self._put("/drives/rootfs", {
            "drive_id": "rootfs",
            "path_on_host": str(rootfs_path),
            "is_root_device": True,
            "is_read_only": False,
        })
        self._put("/network-interfaces/eth0", {
            "iface_id": "eth0",
            "host_dev_name": self.slot.tap_name,
        })
        self._put("/actions", {"action_type": "InstanceStart"})
        log.info("[%s] VM started on slot %d (%s)",
                 self.vm_id, self.slot.slot_id, self.slot.guest_ip)

    def wait_for_ssh(self) -> None:
        """Block until sshd inside the guest accepts connections."""
        deadline = time.monotonic() + SSH_POLL_TIMEOUT
        key = paramiko.Ed25519Key.from_private_key_file(str(self.ssh_key_path))

        # Suppress paramiko's noisy banner-error tracebacks during polling —
        # connection failures are expected while the guest is still booting.
        paramiko_log = logging.getLogger("paramiko.transport")
        orig_level = paramiko_log.level
        paramiko_log.setLevel(logging.CRITICAL)

        try:
            while time.monotonic() < deadline:
                try:
                    client = paramiko.SSHClient()
                    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    client.connect(
                        self.slot.guest_ip,
                        port=22,
                        username="root",
                        pkey=key,
                        timeout=2,
                        banner_timeout=5,
                        auth_timeout=5,
                    )
                    client.close()
                    log.info("[%s] SSH ready on %s", self.vm_id, self.slot.guest_ip)
                    return
                except Exception:
                    time.sleep(SSH_POLL_INTERVAL)
        finally:
            paramiko_log.setLevel(orig_level)

        raise TimeoutError(
            f"[{self.vm_id}] SSH not ready after {SSH_POLL_TIMEOUT}s"
        )

    def execute(self, command: str, timeout: float = 30.0) -> ExecResult:
        """SSH into the guest, run *command*, and return captured output."""
        key = paramiko.Ed25519Key.from_private_key_file(str(self.ssh_key_path))
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            self.slot.guest_ip,
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
        """Terminate Firecracker, remove socket, delete rootfs copy."""
        if self._fc_process is not None:
            log.info("[%s] terminating firecracker (pid %d)",
                     self.vm_id, self._fc_process.pid)
            self._fc_process.terminate()
            try:
                self._fc_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._fc_process.kill()
                self._fc_process.wait()
            self._fc_process = None

        sock = self.slot.socket_path
        if sock.exists():
            sock.unlink()

        if self._rootfs_copy is not None and self._rootfs_copy.exists():
            self._rootfs_copy.unlink()
            log.debug("[%s] rootfs copy removed", self.vm_id)
            self._rootfs_copy = None

        log.info("[%s] VM shut down", self.vm_id)

    # -- Internal -----------------------------------------------------------

    def _wait_for_api_socket(self) -> None:
        deadline = time.monotonic() + SOCKET_POLL_TIMEOUT
        self._session = requests_unixsocket.Session()
        while time.monotonic() < deadline:
            if self.slot.socket_path.exists():
                try:
                    resp = self._session.get(self._api_url("/"))
                    if resp.status_code in (200, 400):
                        return
                except Exception:
                    pass
            time.sleep(SOCKET_POLL_INTERVAL)
        raise TimeoutError(
            f"[{self.vm_id}] API socket not ready after {SOCKET_POLL_TIMEOUT}s"
        )


# ---------------------------------------------------------------------------
# Quick self-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    slots = setup_network(pool_size=1)
    vm = FirecrackerVM(slot=slots[0])
    try:
        vm.boot()
        vm.wait_for_ssh()
        result = vm.execute("echo 'hello from inside the microVM' && uname -a")
        print(f"\nvm_id={vm.vm_id}")
        print(f"exit_code={result.exit_code}")
        print(f"stdout={result.stdout}")
        print(f"stderr={result.stderr}")
    finally:
        vm.shutdown()
        teardown_network(slots)
