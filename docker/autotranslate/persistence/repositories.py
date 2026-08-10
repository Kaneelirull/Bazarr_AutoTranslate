from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Repositories:
    """Named repository façade over the backward-compatible StateStore."""

    state: Any

    @property
    def retries(self) -> Any:
        return self.state

    @property
    def repairs(self) -> Any:
        return self.state

    @property
    def recovery(self) -> Any:
        return self.state

    @property
    def circuits(self) -> Any:
        return self.state

    @property
    def maintenance(self) -> Any:
        return self.state
