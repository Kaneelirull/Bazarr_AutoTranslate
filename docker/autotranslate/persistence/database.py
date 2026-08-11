from __future__ import annotations

import json
import os
import shutil
import sqlite3
import statistics
import threading
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from .common import ACTIVE_RETRY_STATES, SCHEMA_VERSION, StateStoreError, _path_key, _utc_iso


class Database:
    """Injectable SQLite transaction boundary for focused repositories."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            self.path, timeout=30, check_same_thread=False, isolation_level=None
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self._lock = threading.RLock()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
            except Exception:
                self.connection.execute("ROLLBACK")
                raise
            else:
                self.connection.execute("COMMIT")

    def close(self) -> None:
        self.connection.close()


class DatabaseState:
    def __init__(
        self,
        path: str | Path,
        *,
        acquire_process_lock: bool = False,
        validator_version: str = "1",
        config_fingerprint: str = "",
    ):
        self.path = Path(path)
        self.validator_version = str(validator_version)
        self.config_fingerprint = str(config_fingerprint)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._process_lock_handle = None
        self._connection = None
        if acquire_process_lock:
            self._acquire_process_lock()
        try:
            self._connection = sqlite3.connect(
                self.path,
                timeout=30,
                check_same_thread=False,
                isolation_level=None,
            )
            self._connection.row_factory = sqlite3.Row
            self._configure()
            self._migrate_schema()
            self._verify()
        except (OSError, sqlite3.Error, StateStoreError) as exc:
            if self._connection is not None:
                self._connection.close()
            self.release_process_lock()
            raise StateStoreError(f"could not initialize {self.path}: {exc}") from exc

    def _acquire_process_lock(self) -> None:
        lock_path = self.path.parent / "bazarr-autotranslate.lock"
        handle = lock_path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as exc:
            handle.close()
            raise StateStoreError(
                f"another Bazarr AutoTranslate instance is using {self.path.parent}"
            ) from exc
        self._process_lock_handle = handle

    def release_process_lock(self) -> None:
        handle = self._process_lock_handle
        self._process_lock_handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def _configure(self) -> None:
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 30000")
        self._connection.execute("PRAGMA synchronous = FULL")
        try:
            mode = self._connection.execute(
                "PRAGMA journal_mode = WAL"
            ).fetchone()[0]
        except sqlite3.Error as exc:
            mode = ""
            print(
                f"[WARNING] SQLite WAL unavailable for {self.path} ({exc}); "
                "using rollback journal"
            )
        if str(mode).lower() != "wal":
            self._connection.execute("PRAGMA journal_mode = DELETE")
            if mode:
                print(
                    f"[WARNING] SQLite WAL unavailable for {self.path}; "
                    "using rollback journal"
                )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                yield self._connection
                self._connection.execute("COMMIT")
            except Exception as exc:
                try:
                    self._connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                if isinstance(exc, sqlite3.Error):
                    raise StateStoreError(f"SQLite transaction failed: {exc}") from exc
                raise

    def close(self) -> None:
        with self._lock:
            try:
                self._connection.close()
            finally:
                self.release_process_lock()

    def _metadata(self, key: str) -> str | None:
        row = self._fetchone(
            "SELECT value FROM state_metadata WHERE key = ?", (key,)
        )
        return str(row["value"]) if row else None

    def _fetchone(
        self, query: str, parameters: Iterable[object] = ()
    ) -> sqlite3.Row | None:
        with self._lock:
            try:
                return self._connection.execute(query, tuple(parameters)).fetchone()
            except sqlite3.Error as exc:
                raise StateStoreError(f"SQLite read failed: {exc}") from exc

    def _set_metadata(self, db: sqlite3.Connection, key: str, value: str) -> None:
        db.execute(
            """
            INSERT INTO state_metadata(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    @staticmethod
    def _hash_file(path: Path) -> str | None:
        import hashlib

        try:
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        except OSError:
            return None

    @staticmethod
    def _read_json_object(path: Path) -> dict:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            print(f"[WARNING] Skipping malformed legacy state {path}: {exc}")
            return {}
        if not isinstance(payload, dict):
            print(f"[WARNING] Skipping non-object legacy state {path}")
            return {}
        return payload
