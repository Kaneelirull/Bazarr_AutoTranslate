from __future__ import annotations

import threading
from concurrent.futures import Future
from typing import Any, Callable, Iterator

from .executor import completed_futures


class RepairCoordinator:
    """Own active repair futures, dedupe keys, and interruptible drainage."""

    def __init__(
        self,
        *,
        state_provider: Callable[[], Any] | None = None,
    ) -> None:
        self.lock = threading.RLock()
        self.pending: dict[Future, dict] = {}
        self.keys: set[tuple] = set()
        self._state_provider = state_provider

    def reserve(self, key: tuple) -> bool:
        """Atomically reserve a dedupe key before persistence/submission."""
        with self.lock:
            if key in self.keys:
                return False
            self.keys.add(key)
            return True

    def release_reservation(self, key: tuple) -> None:
        with self.lock:
            self.keys.discard(key)

    def persist(self, **values: Any) -> int | None:
        """Persist a repair before worker submission when a store is configured."""
        if self._state_provider is None:
            return None
        state = self._state_provider()
        if not hasattr(state, "enqueue_repair_job"):
            return None
        return state.enqueue_repair_job(**values)

    def register(self, future: Future, metadata: dict) -> None:
        with self.lock:
            self.pending[future] = metadata

    def snapshot(self) -> list[tuple[Future, dict]]:
        with self.lock:
            return list(self.pending.items())

    def take(self, future: Future) -> dict:
        with self.lock:
            metadata = self.pending.pop(future, {})
            self.keys.discard(metadata.get("key"))
            return metadata

    def completed(
        self,
        futures: list[Future],
        *,
        stop_requested: Callable[[], bool],
        poll_seconds: float = 0.25,
    ) -> Iterator[Future]:
        yield from completed_futures(
            futures,
            stop_requested=stop_requested,
            poll_seconds=poll_seconds,
        )

    @property
    def active_count(self) -> int:
        with self.lock:
            return len(self.pending)
