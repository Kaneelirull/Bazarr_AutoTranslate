from __future__ import annotations

import threading


class CycleSuppressionRegistry:
    """Thread-safe quarantine/delete suppression scoped to the active cycle."""

    def __init__(self):
        self._lock = threading.Lock()
        self._cycle_id: str | None = None
        self._entries: dict[str, dict] = {}

    def begin_cycle(self, cycle_id: str) -> None:
        with self._lock:
            self._cycle_id = str(cycle_id)
            self._entries = {}

    def suppress(self, identity: str | None, *, action: str) -> dict | None:
        if identity is None:
            return None
        with self._lock:
            entry = {
                "identity": identity,
                "action": action,
                "cycleId": self._cycle_id,
            }
            self._entries[identity] = entry
            return dict(entry)

    def get(self, identity: str | None) -> dict | None:
        if identity is None:
            return None
        with self._lock:
            entry = self._entries.get(identity)
            return dict(entry) if entry is not None else None
