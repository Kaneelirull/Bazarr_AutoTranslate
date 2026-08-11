from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 16

ACTIVE_RETRY_STATES = {
    "repair_retry_queued",
    "regeneration_waiting",
    "regeneration_queued",
    "retry_in_progress",
}


class StateStoreError(RuntimeError):
    """Raised when correctness-critical persistent state is unavailable."""


def _utc_iso(timestamp: float | None = None) -> str:
    return datetime.fromtimestamp(
        time.time() if timestamp is None else timestamp, timezone.utc
    ).isoformat()


def _path_key(path: str | Path | None) -> str | None:
    if path is None:
        return None
    return os.path.normcase(os.path.abspath(str(path)))
