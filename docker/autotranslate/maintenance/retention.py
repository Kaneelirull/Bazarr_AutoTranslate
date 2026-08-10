from __future__ import annotations

from typing import Callable


def run_retention(housekeeping: Callable[[], dict]) -> dict:
    return housekeeping()
