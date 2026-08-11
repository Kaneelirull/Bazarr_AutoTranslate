from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol


class ActiveTranslation(Protocol):
    @property
    def media_key(self) -> tuple[int, str] | None: ...


@dataclass(frozen=True)
class CapacityToken:
    value: int
    kind: str


class CapacityCoordinator:
    """Own simple idempotent reservations for small package consumers."""

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


class TranslationCapacityGate:
    """Bound local reservations against Lingarr's externally visible queue."""

    def __init__(
        self,
        limit: int,
        *,
        active_translations: Callable[[], Sequence[ActiveTranslation]],
        shutdown_requested: Callable[[], bool],
        poll_interval: float,
        emit: Callable[[str], None] = print,
        service_errors: tuple[type[BaseException], ...] = (Exception,),
    ) -> None:
        self.limit = max(1, int(limit))
        self._active_translations = active_translations
        self._shutdown_requested = shutdown_requested
        self._poll_interval = max(0.01, float(poll_interval))
        self._emit = emit
        self._service_errors = service_errors
        self._condition = threading.Condition()
        self._next_token = 1
        self._reservations: dict[int, tuple[int, str]] = {}

    def _effective_count(self, active: Sequence[ActiveTranslation]) -> int:
        active_keys = Counter(
            entry.media_key for entry in active if entry.media_key is not None
        )
        reservation_keys = Counter(self._reservations.values())
        visible_reservations = sum(
            min(count, active_keys.get(key, 0))
            for key, count in reservation_keys.items()
        )
        return len(active) + len(self._reservations) - visible_reservations

    def acquire(self, media_id: int, media_type: str) -> int | None:
        media_key = (int(media_id), media_type.lower())
        while not self._shutdown_requested():
            try:
                active = self._active_translations()
            except self._service_errors as exc:
                self._emit(f"[DEFER] Cannot verify Lingarr capacity: {exc}")
                return None
            with self._condition:
                effective = self._effective_count(active)
                active_keys = {
                    entry.media_key for entry in active if entry.media_key is not None
                }
                if effective < self.limit and media_key not in active_keys:
                    token = self._next_token
                    self._next_token += 1
                    self._reservations[token] = media_key
                    return token
                self._emit(
                    f"[INFO] Lingarr queue full ({effective}/{self.limit}) "
                    f"— waiting {self._poll_interval:g}s..."
                )
                self._condition.wait(timeout=self._poll_interval)
        return None

    def release(self, token: int | None) -> None:
        if token is None:
            return
        with self._condition:
            self._reservations.pop(token, None)
            self._condition.notify_all()

    def reset(self) -> None:
        with self._condition:
            self._reservations.clear()
            self._condition.notify_all()


class SharedCapacityCoordinator:
    """Coordinate file translations and repairs with repair-first admission."""

    def __init__(self, limit: int, *, shutdown_requested: Callable[[], bool]) -> None:
        self.limit = max(1, int(limit))
        self._shutdown_requested = shutdown_requested
        self._condition = threading.Condition()
        self._active = 0
        self._waiting_repairs = 0
        self._next_token = 1
        self._tokens: dict[int, str] = {}
        self._local = threading.local()

    def acquire_translation(self) -> int | None:
        with self._condition:
            while not self._shutdown_requested():
                if self._active < self.limit and self._waiting_repairs == 0:
                    token = self._next_token
                    self._next_token += 1
                    self._tokens[token] = "translation"
                    self._active += 1
                    self._local.translation_token = token
                    return token
                self._condition.wait(timeout=1)
        return None

    def reserve_repair(self) -> int | None:
        with self._condition:
            translation_token = getattr(self._local, "translation_token", None)
            if self._tokens.get(translation_token) == "translation":
                self._tokens[translation_token] = "repair-reserved"
                self._local.translation_token = None
                return translation_token
            token = self._next_token
            self._next_token += 1
            self._tokens[token] = "repair-waiting"
            self._waiting_repairs += 1
            self._condition.notify_all()
            return token

    def start_repair(self, token: int) -> bool:
        with self._condition:
            while not self._shutdown_requested():
                state = self._tokens.get(token)
                if state == "repair-reserved":
                    self._tokens[token] = "repair"
                    return True
                if state != "repair-waiting":
                    return False
                if self._active < self.limit:
                    self._waiting_repairs -= 1
                    self._active += 1
                    self._tokens[token] = "repair"
                    return True
                self._condition.wait(timeout=1)
            self._cancel_waiter(token)
            return False

    def _cancel_waiter(self, token: int) -> None:
        if self._tokens.pop(token, None) == "repair-waiting":
            self._waiting_repairs -= 1
        self._condition.notify_all()

    def release(self, token: int | None) -> None:
        if token is None:
            return
        with self._condition:
            state = self._tokens.pop(token, None)
            if state in ("translation", "repair", "repair-reserved"):
                self._active = max(0, self._active - 1)
            elif state == "repair-waiting":
                self._waiting_repairs = max(0, self._waiting_repairs - 1)
            if getattr(self._local, "translation_token", None) == token:
                self._local.translation_token = None
            self._condition.notify_all()

    def release_current_translation(self) -> None:
        self.release(getattr(self._local, "translation_token", None))

    def reset(self) -> None:
        with self._condition:
            self._tokens.clear()
            self._active = 0
            self._waiting_repairs = 0
            self._local.translation_token = None
            self._condition.notify_all()


class FileLaneGate:
    """Prefer dedicated short/long lanes while lending idle capacity."""

    def __init__(self, workers: int, *, shutdown_requested: Callable[[], bool]) -> None:
        self.workers = max(1, int(workers))
        self._shutdown_requested = shutdown_requested
        self.short_capacity = (self.workers + 1) // 2
        self.long_capacity = self.workers // 2
        self._condition = threading.Condition()
        self._active_long = 0
        self._active_short = 0
        self._waiters: dict[int, tuple[bool, float, int]] = {}
        self._next_waiter = 0

    def acquire(self, is_long: bool, estimate_seconds: float = 0.0) -> str | None:
        with self._condition:
            token = self._next_waiter
            self._next_waiter += 1
            self._waiters[token] = (bool(is_long), max(0.0, float(estimate_seconds)), token)
            try:
                while not self._shutdown_requested():
                    long_waiters = sorted(
                        (
                            (key, estimate, sequence)
                            for key, (long_job, estimate, sequence) in self._waiters.items()
                            if long_job
                        ),
                        key=lambda entry: (-entry[1], entry[2]),
                    )
                    short_waiters = sorted(
                        (
                            (key, estimate, sequence)
                            for key, (long_job, estimate, sequence) in self._waiters.items()
                            if not long_job
                        ),
                        key=lambda entry: (-entry[1], entry[2]),
                    )
                    if self.workers == 1:
                        preferred = short_waiters or long_waiters
                        available = (
                            self._active_long + self._active_short == 0
                            and bool(preferred)
                            and preferred[0][0] == token
                        )
                        lane = "long" if is_long else "short"
                    elif is_long:
                        preferred_long = bool(long_waiters) and long_waiters[0][0] == token
                        if preferred_long and self._active_long < self.long_capacity:
                            available, lane = True, "long"
                        else:
                            available = (
                                preferred_long
                                and self._active_short < self.short_capacity
                                and not short_waiters
                            )
                            lane = "long (borrowed)"
                    else:
                        preferred_short = bool(short_waiters) and short_waiters[0][0] == token
                        if preferred_short and self._active_short < self.short_capacity:
                            available, lane = True, "short"
                        else:
                            available = (
                                preferred_short
                                and self._active_long < self.long_capacity
                                and not long_waiters
                            )
                            lane = "short (borrowed)"
                    if available:
                        self._waiters.pop(token, None)
                        if lane in {"long", "short (borrowed)"}:
                            self._active_long += 1
                        else:
                            self._active_short += 1
                        self._condition.notify_all()
                        return lane
                    self._condition.wait(timeout=1)
            finally:
                self._waiters.pop(token, None)
                self._condition.notify_all()
        return None

    def release(self, lane: str | None) -> None:
        if lane is None:
            return
        with self._condition:
            if lane in {"long", "short (borrowed)"}:
                self._active_long = max(0, self._active_long - 1)
            else:
                self._active_short = max(0, self._active_short - 1)
            self._condition.notify_all()
