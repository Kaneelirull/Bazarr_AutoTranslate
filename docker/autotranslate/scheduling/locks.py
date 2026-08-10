from __future__ import annotations

import os
import threading
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


def _key(value: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(value)))


@dataclass
class _Entry:
    lock: threading.RLock
    users: int = 0


class KeyedLockRegistry:
    """Reference-counted keyed locks with deterministic entry eviction."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._entries: dict[str, _Entry] = {}

    @contextmanager
    def hold(self, value: str | Path) -> Iterator[None]:
        key = _key(value)
        with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                entry = _Entry(threading.RLock())
                self._entries[key] = entry
            entry.users += 1
        entry.lock.acquire()
        try:
            yield
        finally:
            entry.lock.release()
            with self._guard:
                entry.users -= 1
                if entry.users == 0 and self._entries.get(key) is entry:
                    self._entries.pop(key, None)

    @property
    def size(self) -> int:
        with self._guard:
            return len(self._entries)


class ArtifactAccessCoordinator:
    """Serializes readers, writers, and retention per normalized artifact."""

    def __init__(self, registry: KeyedLockRegistry | None = None) -> None:
        self.registry = registry or KeyedLockRegistry()

    @contextmanager
    def hold(self, *paths: str | Path | None) -> Iterator[None]:
        ordered = sorted({_key(path) for path in paths if path is not None})
        with ExitStack() as stack:
            for path in ordered:
                stack.enter_context(self.registry.hold(path))
            yield

    @property
    def registry_size(self) -> int:
        return self.registry.size
