"""
Pandora – Apache Ignite distributed state layer

Replaces the in-process Python dicts from api.py with two distributed Ignite
caches that every node in the cluster can read and write:

  pandora_warm_pool
      Warm VM entries advertised by each node.
      Key:   "{node_id}:{slot_id}"
      Value: {node_id, slot_id, guest_ip, booted_at_ms}

  pandora_sandbox_registry
      Active sandbox metadata — readable by every node, updated on exec.
      Key:   sandbox_id  (UUID hex string)
      Value: {sandbox_id, owner_node_id, slot_id, guest_ip,
              created_at_ms, idle_timeout_s, last_activity_ms, exec_count}

Phase 1 (single node):
    The local asyncio.Queue in api_ignite.py still drives coordination.
    Ignite is written in parallel and acts as the authoritative state visible
    to monitoring tools and future nodes.  No behaviour change for clients.

Phase 2 (multi-node):
    claim_warm() will scan remote-node keys via SQL query;
    exec routing will check owner_node_id against the local NODE_ID and
    forward via the Ignite compute grid (or a REST bridge) if needed.

Configuration (env vars):
    PANDORA_NODE_ID   Unique identifier for this node.  Defaults to an 8-hex
                      random string generated at process start.
    IGNITE_HOSTS      Comma-separated "host:port" list.  Default: localhost:10800
"""

import os
import threading
import time
import uuid

from pyignite import Client as IgniteClient

# ---------------------------------------------------------------------------
# Node identity
# ---------------------------------------------------------------------------
NODE_ID: str = os.environ.get("PANDORA_NODE_ID", uuid.uuid4().hex[:8])


def _parse_hosts(raw: str) -> list[tuple[str, int]]:
    """Parse "host1:port,host2:port" into a list of (host, port) tuples."""
    result: list[tuple[str, int]] = []
    for part in raw.split(","):
        part = part.strip()
        if ":" in part:
            host, port_s = part.rsplit(":", 1)
            result.append((host, int(port_s)))
        else:
            result.append((part, 10800))
    return result


# ---------------------------------------------------------------------------
# IgnitePoolState
# ---------------------------------------------------------------------------
class IgnitePoolState:
    """Distributed state store backed by Apache Ignite thin client.

    Thread-safe: all public methods can be called from any thread or via
    asyncio.to_thread().
    """

    def __init__(self, hosts: list[tuple[str, int]]) -> None:
        self.node_id = NODE_ID
        self._client = IgniteClient()
        self._client.connect(hosts)
        self._warm_pool = self._client.get_or_create_cache("pandora_warm_pool")
        self._sandbox_reg = self._client.get_or_create_cache(
            "pandora_sandbox_registry"
        )
        # Local set of warm keys *this node* has advertised.  Used to track
        # how many warm VMs this node is contributing to the global pool.
        self._warm_keys: set[str] = set()
        self._lock = threading.Lock()

    # -----------------------------------------------------------------------
    # Warm pool
    # -----------------------------------------------------------------------

    def warm_key(self, slot_id: int) -> str:
        """Canonical Ignite key for a warm VM on this node."""
        return f"{self.node_id}:{slot_id}"

    def advertise_warm(self, slot_id: int, guest_ip: str) -> str:
        """Publish a freshly-booted VM into the distributed warm pool.

        Called by the warmer loop after SSH is confirmed ready.
        Returns the cache key so the caller can pass it to claim_warm().
        """
        key = self.warm_key(slot_id)
        self._warm_pool.put(key, {
            "node_id": self.node_id,
            "slot_id": slot_id,
            "guest_ip": guest_ip,
            "booted_at_ms": int(time.time() * 1000),
        })
        with self._lock:
            self._warm_keys.add(key)
        return key

    def claim_warm(self, key: str) -> bool:
        """Atomically remove a warm pool entry.

        Returns True if the entry existed and was removed (claimed by us),
        False if another node already claimed it (Phase 2 race condition).

        Uses get_and_remove() which is atomic on the Ignite server side,
        making this safe for concurrent claiming from multiple nodes.
        """
        record = self._warm_pool.get_and_remove(key)
        with self._lock:
            self._warm_keys.discard(key)
        return record is not None

    def retract_warm(self, key: str) -> None:
        """Remove a warm pool entry without claiming it (e.g. on shutdown)."""
        self._warm_pool.remove_key(key)
        with self._lock:
            self._warm_keys.discard(key)

    def local_warm_count(self) -> int:
        """Number of warm VMs this node has advertised in Ignite."""
        with self._lock:
            return len(self._warm_keys)

    # -----------------------------------------------------------------------
    # Sandbox registry
    # -----------------------------------------------------------------------

    def register_sandbox(
        self,
        sandbox_id: str,
        slot_id: int,
        guest_ip: str,
        idle_timeout: float,
    ) -> None:
        """Write a new sandbox entry into the distributed registry."""
        self._sandbox_reg.put(sandbox_id, {
            "sandbox_id": sandbox_id,
            "owner_node_id": self.node_id,
            "slot_id": slot_id,
            "guest_ip": guest_ip,
            "created_at_ms": int(time.time() * 1000),
            "idle_timeout_s": idle_timeout,
            "last_activity_ms": int(time.time() * 1000),
            "exec_count": 0,
        })

    def touch_sandbox(self, sandbox_id: str, exec_count: int) -> None:
        """Refresh last_activity_ms and exec_count after a successful exec.

        This is a read-modify-write and is not atomic.  For Phase 1 (single
        owner), this is fine.  Phase 2 can switch to an Ignite EntryProcessor
        to make it atomic.
        """
        record = self._sandbox_reg.get(sandbox_id)
        if record is not None:
            record["last_activity_ms"] = int(time.time() * 1000)
            record["exec_count"] = exec_count
            self._sandbox_reg.put(sandbox_id, record)

    def get_sandbox_record(self, sandbox_id: str) -> dict | None:
        """Look up sandbox metadata.  Returns None if sandbox is not found."""
        return self._sandbox_reg.get(sandbox_id)

    def unregister_sandbox(self, sandbox_id: str) -> None:
        """Remove a sandbox from the registry (called on teardown/reap)."""
        self._sandbox_reg.remove_key(sandbox_id)

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    def close(self) -> None:
        self._client.close()
