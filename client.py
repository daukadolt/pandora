"""
Pandora SDK – Python client for the Pandora API.

Usage::

    from client import PandoraClient

    with PandoraClient("http://lima-ip:8000") as pandora:
        result = pandora.execute("echo hello")
        print(result.stdout)
"""

from dataclasses import dataclass

import requests


@dataclass(frozen=True)
class ExecutionResult:
    """Mirrors the API's ExecuteResponse."""

    exit_code: int
    stdout: str
    stderr: str
    boot_ms: float
    ssh_ready_ms: float
    exec_ms: float
    teardown_ms: float
    total_ms: float


class PandoraClient:
    """Thin wrapper around the Pandora HTTP API."""

    def __init__(self, base_url: str = "http://localhost:8000") -> None:
        self._url = base_url.rstrip("/")
        self._session = requests.Session()

    # -- core ---------------------------------------------------------------

    def execute(self, code: str, timeout: float = 30.0) -> ExecutionResult:
        """Submit *code* for execution inside a fresh microVM."""
        resp = self._session.post(
            f"{self._url}/execute",
            json={"code": code, "timeout": timeout},
            timeout=timeout + 60,  # generous HTTP timeout (boot + teardown margin)
        )
        resp.raise_for_status()
        return ExecutionResult(**resp.json())

    def health(self) -> dict:
        resp = self._session.get(f"{self._url}/health", timeout=5)
        resp.raise_for_status()
        return resp.json()

    def metrics_text(self) -> str:
        """Return raw Prometheus metrics text."""
        resp = self._session.get(f"{self._url}/metrics", timeout=5)
        resp.raise_for_status()
        return resp.text

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "PandoraClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
