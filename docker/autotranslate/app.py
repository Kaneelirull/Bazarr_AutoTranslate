from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

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


class RuntimeHost(Protocol):
    def run(self) -> int: ...
    def close(self) -> None: ...


class PackagedRuntimeHost:
    """Production host for the packaged runtime and its owned resources."""

    def __init__(self, config: Config) -> None:
        from . import runtime

        self.config = config
        self._runtime = runtime

    def run(self) -> int:
        return self._runtime.main(self.config)

    def close(self) -> None:
        self._runtime.close_runtime_resources()


def build_application(
    config: Config | None = None,
    *,
    host_factory: Callable[[Config], RuntimeHost] = PackagedRuntimeHost,
) -> Application:
    """Construct the production application without executing it."""
    resolved = config or Config.from_env()
    host = host_factory(resolved)
    return Application(
        config=resolved,
        run_lifecycle=host.run,
        close_resources=host.close,
    )


def main() -> int:
    return build_application().run()
