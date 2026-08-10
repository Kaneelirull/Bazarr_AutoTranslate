from __future__ import annotations

import os
import tempfile
from pathlib import Path


class QuarantineArchive:
    """Immutable candidate writer; database lineage is owned by repositories."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def archive_bytes(self, relative_path: Path, payload: bytes) -> Path:
        destination = self.root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        counter = 1
        base = destination
        while destination.exists():
            destination = base.with_name(f"{base.stem}.{counter}{base.suffix}")
            counter += 1
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=destination.parent, prefix=f".{destination.name}.",
                suffix=".tmp", delete=False,
            ) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
                temporary = handle.name
            os.replace(temporary, destination)
            temporary = None
            return destination
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
