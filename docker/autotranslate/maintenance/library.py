"""Existing-library maintenance adapter for coordinated production scans."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class ExistingLibraryMaintenance:
    scan: Callable[[], dict | None]

    def run(self) -> dict:
        return self.scan() or {}
