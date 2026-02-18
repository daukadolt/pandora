"""
Pandora SDK – Python client for the Pandora API.

Usage::

    from client import PandoraClient

    client = PandoraClient("http://lima-ip:8000")

    with client.create() as sb:
        print(sb.exec("echo hello").stdout)
        print(sb.exec("uname -a").stdout)
    # sandbox torn down automatically

    client.close()
"""

from __future__ import annotations

from dataclasses import dataclass

import requests


@dataclass(frozen=True)
class ExecResult:
    """Mirrors the API's ExecResponse."""

    sandbox_id: str
    exit_code: int
    stdout: str
    stderr: str
    exec_ms: float


class Sandbox:
    """A handle to a living sandbox on the server.

    Obtained via ``PandoraClient.create()``.  Use as a context manager
    or call ``.close()`` explicitly when done.
    """

    def __init__(self, session: requests.Session, base_url: str,
                 sandbox_id: str, slot_id: int, guest_ip: str,
                 boot_ms: float, idle_timeout: float) -> None:
        self._session = session
        self._url = base_url
        self.sandbox_id = sandbox_id
        self.slot_id = slot_id
        self.guest_ip = guest_ip
        self.boot_ms = boot_ms
        self.idle_timeout = idle_timeout
        self._closed = False

    def exec(self, code: str, timeout: float = 30.0) -> ExecResult:
        """Run *code* inside this sandbox."""
        if self._closed:
            raise RuntimeError("Sandbox is closed")
        resp = self._session.post(
            f"{self._url}/sandboxes/{self.sandbox_id}/exec",
            json={"code": code, "timeout": timeout},
            timeout=timeout + 60,
        )
        resp.raise_for_status()
        return ExecResult(**resp.json())

    def close(self) -> None:
        """Tear down the sandbox on the server."""
        if self._closed:
            return
        self._closed = True
        self._session.delete(
            f"{self._url}/sandboxes/{self.sandbox_id}",
            timeout=30,
        )

    def __enter__(self) -> Sandbox:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def __repr__(self) -> str:
        state = "closed" if self._closed else "alive"
        return f"Sandbox({self.sandbox_id}, slot={self.slot_id}, {state})"


class PandoraClient:
    """Thin wrapper around the Pandora HTTP API."""

    def __init__(self, base_url: str = "http://localhost:8000") -> None:
        self._url = base_url.rstrip("/")
        self._session = requests.Session()

    def create(self, idle_timeout: float = 60.0) -> Sandbox:
        """Boot a new sandbox and return a handle to it."""
        resp = self._session.post(
            f"{self._url}/sandboxes",
            json={"idle_timeout": idle_timeout},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        return Sandbox(
            session=self._session,
            base_url=self._url,
            sandbox_id=data["sandbox_id"],
            slot_id=data["slot_id"],
            guest_ip=data["guest_ip"],
            boot_ms=data["boot_ms"],
            idle_timeout=data["idle_timeout"],
        )

    def list_sandboxes(self) -> list[dict]:
        resp = self._session.get(f"{self._url}/sandboxes", timeout=5)
        resp.raise_for_status()
        return resp.json()

    def health(self) -> dict:
        resp = self._session.get(f"{self._url}/health", timeout=5)
        resp.raise_for_status()
        return resp.json()

    def metrics_text(self) -> str:
        """Return raw Prometheus metrics text."""
        resp = self._session.get(f"{self._url}/metrics", timeout=5)
        resp.raise_for_status()
        return resp.text

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> PandoraClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
