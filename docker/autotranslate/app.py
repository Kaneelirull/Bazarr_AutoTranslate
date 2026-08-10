from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .config import Config


@dataclass
class Application:
    """Small composition-root shell used as legacy code is extracted."""

    config: Config
    run_lifecycle: Callable[[], int]
    close_resources: Callable[[], None] | None = None

    def run(self) -> int:
        try:
            return self.run_lifecycle()
        finally:
            if self.close_resources is not None:
                self.close_resources()
