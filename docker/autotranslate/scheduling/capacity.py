from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class CapacityToken:
    value: int
    kind: str


class CapacityCoordinator:
    """Owns idempotent shared repair/translation reservations."""

    def __init__(self, limit: int):
        self.limit = max(1, int(limit))
        self._condition = threading.Condition()
        self._next = 1
        self._active: dict[int, str] = {}
        self._accepting = True

    def acquire(self, kind: str, *, blocking: bool = True) -> CapacityToken | None:
        with self._condition:
            while self._accepting and len(self._active) >= self.limit:
                if not blocking:
                    return None
                self._condition.wait()
            if not self._accepting:
                return None
            value = self._next
            self._next += 1
            self._active[value] = str(kind)
            return CapacityToken(value, str(kind))

    def release(self, token: CapacityToken | int | None) -> bool:
        value = token.value if isinstance(token, CapacityToken) else token
        if value is None:
            return False
        with self._condition:
            removed = self._active.pop(int(value), None) is not None
            if removed:
                self._condition.notify_all()
            return removed

    def stop_accepting(self) -> None:
        with self._condition:
            self._accepting = False
            self._condition.notify_all()

    @property
    def active_count(self) -> int:
        with self._condition:
            return len(self._active)
