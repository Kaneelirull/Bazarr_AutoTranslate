from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .config import Config


@dataclass
class Application:
    """Production composition root and top-level resource owner."""

    config: Config
    run_lifecycle: Callable[[], int]
    close_resources: Callable[[], None] | None = None

    def run(self) -> int:
        try:
            return self.run_lifecycle()
        finally:
            if self.close_resources is not None:
                self.close_resources()


def build_application(config: Config | None = None) -> Application:
    """Construct the production application without executing it."""
    resolved = config or Config.from_env()
    # Imported only while composing the compatibility-backed domain services;
    # the executable itself and all resource lifetime remain owned here.
    from . import runtime

    return Application(config=resolved, run_lifecycle=runtime.main)


def main() -> int:
    return build_application().run()
