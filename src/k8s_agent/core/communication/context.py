"""Shared context store — scoped key-value storage accessible to all agents.

Scopes:
- global: Survives the entire process lifetime (config, connections)
- session: Survives a user interaction session (preferences)
- task: Survives a single task execution (current pod, namespace, etc.)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _ContextEntry:
    value: Any
    scope: str  # "global" | "session" | "task"
    ttl_ms: int | None = None  # Time-to-live in milliseconds
    created_at: float = field(default_factory=time.monotonic)

    @property
    def expired(self) -> bool:
        if self.ttl_ms is None:
            return False
        return (time.monotonic() - self.created_at) * 1000 > self.ttl_ms


@dataclass
class SharedContext:
    """Scoped key-value store shared across agents.

    Usage:
        ctx = SharedContext()
        ctx.set("current_namespace", "production", scope="session")
        ctx.set("target_pod", "my-pod-abc", scope="task", ttl=300_000)

        ns = ctx.get("current_namespace")  # "production"
    """

    _store: dict[str, _ContextEntry] = field(default_factory=dict)

    def set(
        self,
        key: str,
        value: Any,
        scope: str = "session",
        ttl_ms: int | None = None,
    ) -> None:
        """Set a value in the context store.

        Args:
            key: The key to store under.
            value: Any JSON-serializable value.
            scope: One of "global", "session", "task".
            ttl_ms: Optional TTL in milliseconds.
        """
        self._store[key] = _ContextEntry(
            value=value,
            scope=scope,
            ttl_ms=ttl_ms,
        )

    def get(self, key: str, default: Any = None) -> Any:
        """Get a value from the context store.

        Returns default if key not found or entry expired.
        """
        entry = self._store.get(key)
        if entry is None:
            return default
        if entry.expired:
            del self._store[key]
            return default
        return entry.value

    def delete(self, key: str) -> bool:
        """Delete a key from the store. Returns True if key existed."""
        if key in self._store:
            del self._store[key]
            return True
        return False

    def clear_scope(self, scope: str) -> int:
        """Clear all entries for a given scope.

        Args:
            scope: The scope to clear ("session", "task", etc.).

        Returns:
            Number of entries removed.
        """
        keys_to_delete = [
            k for k, v in self._store.items() if v.scope == scope
        ]
        for k in keys_to_delete:
            del self._store[k]
        return len(keys_to_delete)

    def clear_expired(self) -> int:
        """Remove all expired entries. Returns count removed."""
        keys_to_delete = [
            k for k, v in self._store.items() if v.expired
        ]
        for k in keys_to_delete:
            del self._store[k]
        return len(keys_to_delete)

    def get_all(self, scope: str | None = None) -> dict[str, Any]:
        """Get all entries, optionally filtered by scope."""
        self.clear_expired()
        if scope:
            return {
                k: v.value
                for k, v in self._store.items()
                if v.scope == scope
            }
        return {k: v.value for k, v in self._store.items()}

    def snapshot(self) -> dict[str, Any]:
        """Create a serializable snapshot of the context."""
        self.clear_expired()
        return {
            k: {
                "value": v.value,
                "scope": v.scope,
                "ttl_ms": v.ttl_ms,
            }
            for k, v in self._store.items()
        }

    def restore(self, snapshot: dict[str, Any]) -> None:
        """Restore context from a snapshot."""
        self._store.clear()
        for key, entry_data in snapshot.items():
            self._store[key] = _ContextEntry(
                value=entry_data["value"],
                scope=entry_data.get("scope", "session"),
                ttl_ms=entry_data.get("ttl_ms"),
            )

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None

    def __len__(self) -> int:
        self.clear_expired()
        return len(self._store)
