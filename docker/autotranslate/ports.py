from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, Sequence


class Clock(Protocol):
    def time(self) -> float: ...
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class BazarrGateway(Protocol):
    def fetch_wanted(self, item_type: str) -> list[dict]: ...
    def fetch_subtitles(self, item_type: str, item_id: int) -> tuple[str, list[dict]]: ...
    def synchronize(self, episodes: bool, movies: bool, timeout: int) -> bool: ...


class TranslationProvider(Protocol):
    name: str
    def translate_cue(
        self,
        source_text: str,
        source_language: str,
        target_language: str,
        context_before: Sequence[str] = (),
        context_after: Sequence[str] = (),
        *,
        strict: bool = False,
    ) -> str: ...


class StatusSink(Protocol):
    def set_phase(self, phase: str, *, next_cycle_at: float | None = None) -> None: ...
    def set_diagnostics(self, **values: Any) -> None: ...


class FileSystem(Protocol):
    def read_text(self, path: Path) -> str: ...
    def replace(self, source: Path, destination: Path) -> None: ...
    def sha256(self, path: Path) -> str: ...
