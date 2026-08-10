from __future__ import annotations

import os
from pathlib import Path


def atomic_publish(candidate: str | Path, target: str | Path) -> None:
    os.replace(Path(candidate), Path(target))
