from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[object], None]


LATEST_SCHEMA_VERSION = 14


def applied_versions(connection) -> set[int]:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at REAL NOT NULL)"
    )
    return {
        int(row[0])
        for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
    }
