"""Existing-library maintenance adapters.

The production scanner is still exposed by the compatibility module while its
call site is migrated to :class:`MaintenanceCoordinator`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class ExistingLibraryMaintenance:
    scan: Callable[[], dict | None]

    def run(self) -> dict:
        return self.scan() or {}
