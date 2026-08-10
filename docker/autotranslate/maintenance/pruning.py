from __future__ import annotations

from typing import Callable


def run_pruning(prune: Callable[[], dict]) -> dict:
    return prune()
