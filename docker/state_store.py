from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator


SCHEMA_VERSION = 1


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


class StateStore:
    """Transactional application state shared by translation worker threads."""

    def __init__(
        self,
        path: str | Path,
        *,
        acquire_process_lock: bool = False,
        validator_version: str = "1",
    ):
        self.path = Path(path)
        self.validator_version = str(validator_version)
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

    def _migrate_schema(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS state_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS translation_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_type TEXT NOT NULL,
                    item_id INTEGER NOT NULL,
                    target_language TEXT NOT NULL,
                    target_identity TEXT,
                    target_path TEXT,
                    expected_target_path TEXT,
                    actual_target_path TEXT,
                    video_path TEXT,
                    source_path TEXT,
                    source_hash TEXT,
                    source_language TEXT,
                    target_hash TEXT,
                    target_variant TEXT,
                    lingarr_job_id INTEGER,
                    status TEXT NOT NULL,
                    submitted_at REAL NOT NULL,
                    cooldown_until REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_attempt_cooldown
                    ON translation_attempts(
                        item_type, item_id, target_language, cooldown_until
                    );
                CREATE INDEX IF NOT EXISTS idx_attempt_identity
                    ON translation_attempts(
                        target_identity, target_language, submitted_at
                    );

                CREATE TABLE IF NOT EXISTS subtitle_artifacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    attempt_id INTEGER REFERENCES translation_attempts(id),
                    parent_artifact_id INTEGER REFERENCES subtitle_artifacts(id),
                    item_type TEXT,
                    item_id INTEGER,
                    video_path TEXT,
                    target_identity TEXT,
                    target_path TEXT NOT NULL,
                    target_language TEXT,
                    target_variant TEXT,
                    target_hash TEXT,
                    source_path TEXT,
                    source_language TEXT,
                    source_hash TEXT,
                    origin TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    disposition TEXT NOT NULL,
                    pending_destination TEXT,
                    pending_metadata_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_artifact_match
                    ON subtitle_artifacts(
                        target_path, target_hash, updated_at
                    );
                CREATE INDEX IF NOT EXISTS idx_artifact_identity
                    ON subtitle_artifacts(
                        target_identity, target_language, target_variant, target_hash
                    );

                CREATE TABLE IF NOT EXISTS validation_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    artifact_id INTEGER NOT NULL
                        REFERENCES subtitle_artifacts(id) ON DELETE CASCADE,
                    validator_version TEXT NOT NULL,
                    validation_mode TEXT NOT NULL,
                    result TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    validated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_validation_latest
                    ON validation_results(artifact_id, id DESC);

                CREATE TABLE IF NOT EXISTS quarantine_holds (
                    identity TEXT NOT NULL,
                    target_hash TEXT NOT NULL,
                    target_path TEXT NOT NULL,
                    target_language TEXT NOT NULL,
                    rules_json TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    hold_until TEXT NOT NULL,
                    occurrences INTEGER NOT NULL,
                    PRIMARY KEY(identity, target_hash)
                );
                """
            )
            self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _verify(self) -> None:
        result = self._connection.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            raise StateStoreError(
                f"SQLite quick_check failed for {self.path}: {result}"
            )

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

    # ------------------------------------------------------------------
    # Submission cooldowns and pending Lingarr provenance
    # ------------------------------------------------------------------

    def record_submission(
        self,
        item_type: str,
        item_id: int,
        target_language: str,
        *,
        cooldown_seconds: int,
        target_identity: str | None = None,
        target_path: str | None = None,
        expected_target_path: str | None = None,
        actual_target_path: str | None = None,
        video_path: str | None = None,
        source_path: str | None = None,
        source_hash: str | None = None,
        source_language: str | None = None,
        target_hash: str | None = None,
        target_variant: str | None = None,
        lingarr_job_id: int | None = None,
        status: str = "submitted",
        submitted_at: float | None = None,
    ) -> int:
        now = time.time() if submitted_at is None else float(submitted_at)
        with self._transaction() as db:
            cursor = db.execute(
                """
                INSERT INTO translation_attempts(
                    item_type, item_id, target_language, target_identity,
                    target_path, expected_target_path, actual_target_path,
                    video_path, source_path, source_hash, source_language,
                    target_hash, target_variant, lingarr_job_id, status, submitted_at,
                    cooldown_until, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_type or "legacy",
                    int(item_id),
                    target_language,
                    target_identity,
                    _path_key(target_path),
                    _path_key(expected_target_path),
                    _path_key(actual_target_path),
                    _path_key(video_path),
                    _path_key(source_path),
                    source_hash,
                    source_language,
                    target_hash,
                    target_variant,
                    lingarr_job_id,
                    status,
                    now,
                    now + max(0, int(cooldown_seconds)),
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def check_cooldown(
        self,
        item_type: str,
        item_id: int,
        target_language: str,
        *,
        now: float | None = None,
    ) -> int | None:
        timestamp = time.time() if now is None else float(now)
        row = self._fetchone(
                """
                SELECT submitted_at
                FROM translation_attempts
                WHERE item_id = ?
                  AND target_language = ?
                  AND item_type IN (?, 'legacy')
                  AND cooldown_until > ?
                  AND status NOT IN ('cleared', 'failed')
                ORDER BY CASE WHEN item_type = ? THEN 0 ELSE 1 END,
                         submitted_at DESC
                LIMIT 1
                """,
                (int(item_id), target_language, item_type, timestamp, item_type),
            )
        if not row:
            return None
        return max(0, int(timestamp - float(row["submitted_at"])))

    def update_submission_actual_path(
        self,
        item_type: str,
        item_id: int,
        target_language: str,
        actual_target_path: str,
        target_variant: str,
    ) -> bool:
        with self._transaction() as db:
            row = db.execute(
                """
                SELECT id FROM translation_attempts
                WHERE item_type = ? AND item_id = ? AND target_language = ?
                  AND status NOT IN ('cleared', 'failed')
                ORDER BY submitted_at DESC LIMIT 1
                """,
                (item_type, int(item_id), target_language),
            ).fetchone()
            if not row:
                return False
            db.execute(
                """
                UPDATE translation_attempts
                SET actual_target_path = ?, target_path = ?,
                    target_variant = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    _path_key(actual_target_path),
                    _path_key(actual_target_path),
                    target_variant,
                    time.time(),
                    int(row["id"]),
                ),
            )
            return True

    def mark_submission_submitted(
        self, attempt_id: int, lingarr_job_id: int
    ) -> None:
        with self._transaction() as db:
            cursor = db.execute(
                """
                UPDATE translation_attempts
                SET lingarr_job_id = ?, status = 'submitted', updated_at = ?
                WHERE id = ?
                """,
                (int(lingarr_job_id), time.time(), int(attempt_id)),
            )
            if cursor.rowcount != 1:
                raise StateStoreError(
                    f"submission attempt {attempt_id} no longer exists"
                )

    def mark_submission_failed(self, attempt_id: int) -> None:
        with self._transaction() as db:
            db.execute(
                """
                UPDATE translation_attempts
                SET status = 'failed', cooldown_until = 0, updated_at = ?
                WHERE id = ?
                """,
                (time.time(), int(attempt_id)),
            )

    def clear_submission(
        self, item_type: str | None, item_id: int, target_language: str
    ) -> int:
        with self._transaction() as db:
            if item_type is None:
                cursor = db.execute(
                    """
                    UPDATE translation_attempts
                    SET status = 'cleared', cooldown_until = 0, updated_at = ?
                    WHERE item_id = ? AND target_language = ?
                      AND status NOT IN ('cleared', 'failed')
                    """,
                    (time.time(), int(item_id), target_language),
                )
            else:
                cursor = db.execute(
                    """
                    UPDATE translation_attempts
                    SET status = 'cleared', cooldown_until = 0, updated_at = ?
                    WHERE item_type = ? AND item_id = ? AND target_language = ?
                      AND status NOT IN ('cleared', 'failed')
                    """,
                    (time.time(), item_type, int(item_id), target_language),
                )
            return int(cursor.rowcount)

    def clear_submissions_for_identity(
        self, target_identity: str | None, target_path: str | Path, target_language: str
    ) -> int:
        path = _path_key(target_path)
        with self._transaction() as db:
            cursor = db.execute(
                """
                UPDATE translation_attempts
                SET status = 'cleared', cooldown_until = 0, updated_at = ?
                WHERE target_language = ?
                  AND status NOT IN ('cleared', 'failed')
                  AND (
                    target_path = ? OR expected_target_path = ?
                    OR actual_target_path = ?
                    OR (? IS NOT NULL AND target_identity = ?)
                  )
                """,
                (
                    time.time(),
                    target_language,
                    path,
                    path,
                    path,
                    target_identity,
                    target_identity,
                ),
            )
            return int(cursor.rowcount)

    def find_submission(
        self, target_identity: str, target_language: str
    ) -> dict | None:
        row = self._fetchone(
                """
                SELECT * FROM translation_attempts
                WHERE target_identity = ? AND target_language = ?
                  AND status NOT IN ('cleared', 'failed')
                  AND cooldown_until > ?
                ORDER BY submitted_at DESC LIMIT 1
                """,
                (target_identity, target_language, time.time()),
            )
        if not row:
            return None
        return self._submission_dict(row)

    @staticmethod
    def _submission_dict(row: sqlite3.Row) -> dict:
        return {
            "attemptId": int(row["id"]),
            "itemId": int(row["item_id"]),
            "itemType": row["item_type"],
            "targetPath": row["target_path"],
            "expectedTargetPath": row["expected_target_path"],
            "actualTargetPath": row["actual_target_path"],
            "videoPath": row["video_path"],
            "sourcePath": row["source_path"],
            "sourceHash": row["source_hash"],
            "sourceLanguage": row["source_language"],
            "targetHash": row["target_hash"],
            "targetVariant": row["target_variant"],
            "lingarrJobId": row["lingarr_job_id"],
            "submittedAt": float(row["submitted_at"]),
            "status": row["status"],
        }

    # ------------------------------------------------------------------
    # Subtitle artifacts and validation compatibility API
    # ------------------------------------------------------------------

    def record(
        self,
        target_path: str | Path,
        *,
        source_hash: str | None,
        target_hash: str | None,
        result: str,
        origin: str | None = None,
        details: dict | None = None,
        source_path: str | Path | None = None,
        source_language: str | None = None,
        target_language: str | None = None,
        target_identity: str | None = None,
        target_variant: str | None = None,
        operation: str = "validation",
        parent_artifact_id: int | None = None,
        attempt_id: int | None = None,
        validation_mode: str | None = None,
        validator_version: str | None = None,
        item_type: str | None = None,
        item_id: int | None = None,
    ) -> int:
        now = _utc_iso()
        path = _path_key(target_path)
        payload = details or {}
        report = payload.get("validation", {})
        mode = validation_mode or (
            "source-aware" if origin == "lingarr" and source_hash else "target-only"
        )
        with self._transaction() as db:
            desired_origin = origin or "external"
            artifact = db.execute(
                """
                SELECT id, attempt_id FROM subtitle_artifacts
                WHERE target_path = ?
                  AND ((target_hash = ?) OR (target_hash IS NULL AND ? IS NULL))
                  AND origin = ?
                  AND (
                    ? != 'lingarr'
                    OR source_hash = ?
                  )
                ORDER BY id DESC LIMIT 1
                """,
                (
                    path,
                    target_hash,
                    target_hash,
                    desired_origin,
                    desired_origin,
                    source_hash,
                ),
            ).fetchone()
            if artifact:
                artifact_id = int(artifact["id"])
                linked_attempt_id = artifact["attempt_id"]
            else:
                cursor = db.execute(
                    """
                    INSERT INTO subtitle_artifacts(
                        attempt_id, parent_artifact_id, item_type, item_id,
                        target_identity,
                        target_path, target_language, target_variant, target_hash,
                        source_path, source_language, source_hash, origin,
                        operation, disposition, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                    """,
                    (
                        attempt_id,
                        parent_artifact_id,
                        item_type or payload.get("itemType"),
                        (
                            int(item_id if item_id is not None else payload["itemId"])
                            if item_id is not None or payload.get("itemId") is not None
                            else None
                        ),
                        target_identity,
                        path,
                        target_language,
                        target_variant,
                        target_hash,
                        _path_key(source_path),
                        source_language,
                        source_hash,
                        origin or "external",
                        operation,
                        now,
                        now,
                    ),
                )
                artifact_id = int(cursor.lastrowid)
                linked_attempt_id = attempt_id
            db.execute(
                """
                INSERT INTO validation_results(
                    artifact_id, validator_version, validation_mode, result,
                    report_json, details_json, validated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    str(validator_version or self.validator_version),
                    mode,
                    result,
                    json.dumps(report, ensure_ascii=False),
                    json.dumps(payload, ensure_ascii=False),
                    now,
                ),
            )
            if linked_attempt_id is not None:
                attempt_status = {
                    "pending": "output_ready",
                    "valid": "completed",
                    "valid_with_warnings": "completed",
                }.get(result, "validation_failed")
                db.execute(
                    """
                    UPDATE translation_attempts
                    SET actual_target_path = ?, target_path = ?,
                        target_hash = ?, status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        path,
                        path,
                        target_hash,
                        attempt_status,
                        time.time(),
                        int(linked_attempt_id),
                    ),
                )
            return artifact_id

    def record_artifact_version(
        self,
        target_path: str | Path,
        *,
        target_hash: str,
        source_path: str | Path | None,
        source_hash: str | None,
        source_language: str | None,
        target_language: str | None,
        origin: str,
        operation: str,
        parent_artifact_id: int | None = None,
        attempt_id: int | None = None,
        target_identity: str | None = None,
        target_variant: str | None = None,
        disposition: str = "active",
        pending_destination: str | None = None,
        pending_metadata: dict | None = None,
        item_type: str | None = None,
        item_id: int | None = None,
    ) -> int:
        now = _utc_iso()
        with self._transaction() as db:
            if parent_artifact_id is not None:
                parent = db.execute(
                    """
                    SELECT attempt_id, item_type, item_id
                    FROM subtitle_artifacts
                    WHERE id = ?
                    """,
                    (int(parent_artifact_id),),
                ).fetchone()
                if parent is not None:
                    attempt_id = (
                        attempt_id
                        if attempt_id is not None
                        else parent["attempt_id"]
                    )
                    item_type = item_type or parent["item_type"]
                    item_id = (
                        item_id if item_id is not None else parent["item_id"]
                    )
            cursor = db.execute(
                """
                INSERT INTO subtitle_artifacts(
                    attempt_id, parent_artifact_id, item_type, item_id,
                    target_identity,
                    target_path, target_language, target_variant, target_hash,
                    source_path, source_language, source_hash, origin,
                    operation, disposition, pending_destination,
                    pending_metadata_json, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    parent_artifact_id,
                    item_type,
                    int(item_id) if item_id is not None else None,
                    target_identity,
                    _path_key(target_path),
                    target_language,
                    target_variant,
                    target_hash,
                    _path_key(source_path),
                    source_language,
                    source_hash,
                    origin,
                    operation,
                    disposition,
                    _path_key(pending_destination),
                    (
                        json.dumps(pending_metadata, ensure_ascii=False)
                        if pending_metadata is not None else None
                    ),
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def latest_artifact(
        self, target_path: str | Path, target_hash: str | None = None
    ) -> dict | None:
        path = _path_key(target_path)
        query = "SELECT * FROM subtitle_artifacts WHERE target_path = ?"
        params: list[object] = [path]
        if target_hash is not None:
            query += " AND target_hash = ?"
            params.append(target_hash)
        query += " ORDER BY id DESC LIMIT 1"
        row = self._fetchone(query, params)
        return dict(row) if row else None

    def set_artifact_disposition(
        self,
        artifact_id: int,
        disposition: str,
        *,
        pending_destination: str | Path | None = None,
        pending_metadata: dict | None = None,
    ) -> None:
        with self._transaction() as db:
            cursor = db.execute(
                """
                UPDATE subtitle_artifacts
                SET disposition = ?, pending_destination = ?,
                    pending_metadata_json = CASE
                        WHEN ? LIKE '%_pending'
                        THEN COALESCE(?, pending_metadata_json)
                        ELSE NULL
                    END,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    disposition,
                    _path_key(pending_destination),
                    disposition,
                    (
                        json.dumps(pending_metadata, ensure_ascii=False)
                        if pending_metadata is not None else None
                    ),
                    _utc_iso(),
                    int(artifact_id),
                ),
            )
            if cursor.rowcount != 1:
                raise StateStoreError(f"artifact {artifact_id} no longer exists")

    def reconcile_pending_operations(self) -> dict[str, int]:
        stats = {"completed": 0, "abandoned": 0}
        with self._transaction() as db:
            rows = db.execute(
                """
                SELECT id, target_path, target_hash, target_identity,
                       target_language, origin, disposition,
                       pending_destination, pending_metadata_json
                FROM subtitle_artifacts
                WHERE disposition IN (
                    'replacement_pending', 'quarantine_pending',
                    'deletion_pending'
                )
                """
            ).fetchall()
            for row in rows:
                target_path = Path(row["target_path"])
                destination = (
                    Path(row["pending_destination"])
                    if row["pending_destination"] else None
                )
                if row["disposition"] == "replacement_pending":
                    matches = (
                        target_path.exists()
                        and self._hash_file(target_path) == row["target_hash"]
                    )
                    disposition = "active" if matches else "abandoned"
                elif row["disposition"] == "quarantine_pending":
                    moved = bool(
                        destination is not None
                        and destination.exists()
                        and not target_path.exists()
                    )
                    disposition = "quarantined" if moved else "active"
                else:
                    disposition = "deleted" if not target_path.exists() else "active"
                if disposition in ("quarantined", "deleted"):
                    try:
                        metadata = json.loads(
                            row["pending_metadata_json"] or "{}"
                        )
                    except (TypeError, ValueError):
                        metadata = {}
                    hold_days = max(1, int(metadata.get("holdDays", 30)))
                    rules = sorted({
                        str(rule) for rule in metadata.get("rules", []) if rule
                    })
                    identity = (
                        metadata.get("holdIdentity")
                        or row["target_identity"]
                    )
                    language = row["target_language"]
                    if identity and language and row["target_hash"]:
                        now = datetime.now(timezone.utc)
                        hold_until = datetime.fromtimestamp(
                            now.timestamp() + hold_days * 86400,
                            timezone.utc,
                        ).isoformat()
                        db.execute(
                            """
                            INSERT INTO quarantine_holds(
                                identity, target_hash, target_path,
                                target_language, rules_json, origin,
                                first_seen, last_seen, hold_until, occurrences
                            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                            ON CONFLICT(identity, target_hash) DO UPDATE SET
                                last_seen = excluded.last_seen,
                                hold_until = excluded.hold_until,
                                occurrences = quarantine_holds.occurrences + 1,
                                rules_json = excluded.rules_json
                            """,
                            (
                                identity,
                                row["target_hash"],
                                row["target_path"],
                                language,
                                json.dumps(rules),
                                row["origin"],
                                now.isoformat(),
                                now.isoformat(),
                                hold_until,
                            ),
                        )
                db.execute(
                    """
                    UPDATE subtitle_artifacts
                    SET disposition = ?, pending_destination = NULL,
                        pending_metadata_json = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (disposition, _utc_iso(), int(row["id"])),
                )
                stats[
                    "completed"
                    if disposition in ("active", "quarantined", "deleted")
                    else "abandoned"
                ] += 1
        return stats

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

    def matching_record(
        self,
        target_path: str | Path,
        target_hash: str,
        *,
        target_identity: str | None = None,
        target_language: str | None = None,
        target_variant: str | None = None,
    ) -> dict | None:
        path = _path_key(target_path)
        query = """
                SELECT a.*, v.validator_version, v.result, v.details_json,
                       v.validated_at, v.validation_mode
                FROM subtitle_artifacts a
                JOIN validation_results v ON v.artifact_id = a.id
                WHERE a.target_path = ? AND a.target_hash = ?
        """
        parameters: list[object] = [path, target_hash]
        if target_identity is not None:
            query += " AND a.target_identity = ?"
            parameters.append(target_identity)
        if target_language is not None:
            query += " AND a.target_language = ?"
            parameters.append(target_language)
        if target_variant is not None:
            query += " AND a.target_variant = ?"
            parameters.append(target_variant)
        query += " ORDER BY v.id DESC LIMIT 1"
        row = self._fetchone(query, parameters)
        if not row:
            return None
        return {
            "artifactId": int(row["id"]),
            "validatorVersion": str(row["validator_version"]),
            "sourceHash": row["source_hash"],
            "sourcePath": row["source_path"],
            "sourceLanguage": row["source_language"],
            "targetHash": row["target_hash"],
            "result": row["result"],
            "validatedAt": row["validated_at"],
            "origin": row["origin"],
            "validationMode": row["validation_mode"],
            "details": json.loads(row["details_json"]),
        }

    def matching_origin(
        self, target_path: str | Path, target_hash: str
    ) -> str | None:
        entry = self.matching_record(target_path, target_hash)
        return str(entry["origin"]) if entry and entry.get("origin") else None

    def is_unchanged_valid(
        self, target_path: str | Path, source_hash: str | None, target_hash: str
    ) -> bool:
        entry = self.matching_record(target_path, target_hash)
        return bool(
            entry
            and entry.get("validatorVersion") == self.validator_version
            and entry.get("result") in ("valid", "valid_with_warnings")
            and (
                (
                    entry.get("origin") == "lingarr"
                    and entry.get("sourceHash") == source_hash
                )
                or entry.get("origin") != "lingarr"
            )
        )

    def current_valid_details(
        self, target_path: str | Path, target_hash: str
    ) -> dict | None:
        entry = self.matching_record(target_path, target_hash)
        if (
            not entry
            or entry.get("validatorVersion") != self.validator_version
            or entry.get("result") not in ("valid", "valid_with_warnings")
        ):
            return None
        details = entry.get("details")
        return dict(details) if isinstance(details, dict) else {}

    def record_quarantine_tombstone(
        self,
        identity: str,
        *,
        target_path: str | Path,
        target_hash: str,
        target_language: str,
        rules: Iterable[str],
        origin: str | None,
        hold_days: int,
        now: datetime | None = None,
    ) -> tuple[dict, bool]:
        timestamp = now or datetime.now(timezone.utc)
        with self._transaction() as db:
            previous = db.execute(
                """
                SELECT * FROM quarantine_holds
                WHERE identity = ? AND target_hash = ?
                """,
                (str(identity), target_hash),
            ).fetchone()
            repeated = previous is not None
            first_seen = (
                previous["first_seen"] if previous else timestamp.isoformat()
            )
            occurrences = int(previous["occurrences"]) + 1 if previous else 1
            hold_until = datetime.fromtimestamp(
                timestamp.timestamp() + max(1, hold_days) * 86400,
                timezone.utc,
            ).isoformat()
            rule_values = sorted({str(rule) for rule in rules if rule})
            db.execute(
                """
                INSERT INTO quarantine_holds(
                    identity, target_hash, target_path, target_language,
                    rules_json, origin, first_seen, last_seen, hold_until,
                    occurrences
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(identity, target_hash) DO UPDATE SET
                    target_path = excluded.target_path,
                    target_language = excluded.target_language,
                    rules_json = excluded.rules_json,
                    origin = excluded.origin,
                    last_seen = excluded.last_seen,
                    hold_until = excluded.hold_until,
                    occurrences = excluded.occurrences
                """,
                (
                    str(identity),
                    target_hash,
                    _path_key(target_path),
                    target_language,
                    json.dumps(rule_values),
                    origin or "unknown",
                    first_seen,
                    timestamp.isoformat(),
                    hold_until,
                    occurrences,
                ),
            )
        return (
            {
                "identity": str(identity),
                "targetPath": str(target_path),
                "targetHash": target_hash,
                "targetLanguage": target_language,
                "rules": rule_values,
                "origin": origin or "unknown",
                "firstSeen": first_seen,
                "lastSeen": timestamp.isoformat(),
                "holdUntil": hold_until,
                "occurrences": occurrences,
            },
            repeated,
        )

    def active_quarantine_tombstone(
        self,
        identity: str,
        *,
        target_hash: str | None = None,
        now: datetime | None = None,
    ) -> dict | None:
        timestamp = now or datetime.now(timezone.utc)
        query = (
            "SELECT * FROM quarantine_holds "
            "WHERE identity = ? AND hold_until > ?"
        )
        params: list[object] = [str(identity), timestamp.isoformat()]
        if target_hash is not None:
            query += " AND target_hash = ?"
            params.append(target_hash)
        query += " ORDER BY hold_until DESC LIMIT 1"
        row = self._fetchone(query, params)
        if not row:
            return None
        return {
            "identity": row["identity"],
            "targetPath": row["target_path"],
            "targetHash": row["target_hash"],
            "targetLanguage": row["target_language"],
            "rules": json.loads(row["rules_json"]),
            "origin": row["origin"],
            "firstSeen": row["first_seen"],
            "lastSeen": row["last_seen"],
            "holdUntil": row["hold_until"],
            "occurrences": int(row["occurrences"]),
        }

    def clear_quarantine_tombstone(self, identity: str) -> bool:
        with self._transaction() as db:
            cursor = db.execute(
                "DELETE FROM quarantine_holds WHERE identity = ?", (str(identity),)
            )
            return cursor.rowcount > 0

    def prune_older_than(
        self, retention_days: int, now: datetime | None = None
    ) -> int:
        timestamp = now or datetime.now(timezone.utc)
        cutoff = datetime.fromtimestamp(
            timestamp.timestamp() - retention_days * 86400, timezone.utc
        ).isoformat()
        with self._transaction() as db:
            validations = db.execute(
                "DELETE FROM validation_results WHERE validated_at < ?", (cutoff,)
            ).rowcount
            holds = db.execute(
                "DELETE FROM quarantine_holds WHERE hold_until < ?",
                (timestamp.isoformat(),),
            ).rowcount
            attempts = db.execute(
                """
                DELETE FROM translation_attempts
                WHERE cooldown_until < ?
                  AND status IN ('cleared', 'failed', 'legacy')
                  AND id NOT IN (
                    SELECT attempt_id FROM subtitle_artifacts
                    WHERE attempt_id IS NOT NULL
                  )
                """,
                (timestamp.timestamp(),),
            ).rowcount
        return int(validations + holds + attempts)

    # ------------------------------------------------------------------
    # One-time JSON migration
    # ------------------------------------------------------------------

    def migrate_legacy(
        self,
        submit_cache_path: str | Path,
        validation_state_path: str | Path,
        *,
        cooldown_seconds: int,
    ) -> dict[str, int]:
        if self._metadata("legacy_json_migrated") == "1":
            return {"submissions": 0, "artifacts": 0, "holds": 0, "skipped": 0}

        submit_path = Path(submit_cache_path)
        validation_path = Path(validation_state_path)
        stats = {"submissions": 0, "artifacts": 0, "holds": 0, "skipped": 0}
        submit_payload = self._read_json_object(submit_path)
        validation_payload = self._read_json_object(validation_path)
        now = time.time()

        with self._transaction() as db:
            for key, raw in submit_payload.items():
                try:
                    item_id_text, target_language = key.rsplit(":", 1)
                    item_id = int(item_id_text)
                    if isinstance(raw, dict):
                        submitted_at = float(raw["submittedAt"])
                        metadata = raw
                    else:
                        submitted_at = float(raw)
                        metadata = {}
                    cooldown_until = submitted_at + max(0, cooldown_seconds)
                    if cooldown_until <= now:
                        continue
                    item_type = str(metadata.get("itemType") or "legacy")
                    target_path = metadata.get("targetPath")
                    video_path = metadata.get("videoPath")
                    target_identity = (
                        _path_key(Path(video_path).with_suffix(""))
                        if isinstance(video_path, str) and video_path
                        else None
                    )
                    db.execute(
                        """
                        INSERT INTO translation_attempts(
                            item_type, item_id, target_language, target_identity,
                            target_path, expected_target_path, actual_target_path,
                            video_path, source_path, source_hash, source_language,
                            target_hash, target_variant, status, submitted_at, cooldown_until,
                            updated_at
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            item_type,
                            item_id,
                            target_language,
                            target_identity,
                            _path_key(target_path),
                            _path_key(metadata.get("expectedTargetPath")),
                            _path_key(metadata.get("actualTargetPath")),
                            _path_key(video_path),
                            _path_key(metadata.get("sourcePath")),
                            metadata.get("sourceHash"),
                            metadata.get("sourceLanguage"),
                            metadata.get("targetHash"),
                            metadata.get("targetVariant"),
                            "legacy" if item_type == "legacy" else "submitted",
                            submitted_at,
                            cooldown_until,
                            now,
                        ),
                    )
                    stats["submissions"] += 1
                except (KeyError, TypeError, ValueError, AttributeError):
                    stats["skipped"] += 1

            files = validation_payload.get("files", {})
            if isinstance(files, dict):
                for target_path, entry in files.items():
                    try:
                        if not isinstance(entry, dict):
                            raise TypeError
                        target_hash = entry.get("targetHash")
                        source_hash = entry.get("sourceHash")
                        details = entry.get("details")
                        details = details if isinstance(details, dict) else {}
                        origin = (
                            "lingarr"
                            if entry.get("origin") == "lingarr"
                            and target_hash
                            and source_hash
                            else "external"
                        )
                        created = str(
                            entry.get("validatedAt") or _utc_iso(now)
                        )
                        cursor = db.execute(
                            """
                            INSERT INTO subtitle_artifacts(
                                target_path, target_language, target_hash,
                                source_path, source_language, source_hash,
                                origin, operation, disposition, created_at,
                                updated_at
                            ) VALUES(?, ?, ?, ?, ?, ?, ?, 'legacy_migration',
                                     'active', ?, ?)
                            """,
                            (
                                _path_key(target_path),
                                details.get("targetLanguage"),
                                target_hash,
                                _path_key(details.get("sourcePath")),
                                details.get("sourceLanguage"),
                                source_hash if origin == "lingarr" else None,
                                origin,
                                created,
                                created,
                            ),
                        )
                        artifact_id = int(cursor.lastrowid)
                        db.execute(
                            """
                            INSERT INTO validation_results(
                                artifact_id, validator_version, validation_mode,
                                result, report_json, details_json, validated_at
                            ) VALUES(?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                artifact_id,
                                str(entry.get("validatorVersion", "1")),
                                (
                                    "source-aware"
                                    if origin == "lingarr"
                                    else "target-only"
                                ),
                                str(entry.get("result") or "unknown"),
                                json.dumps(details.get("validation", {})),
                                json.dumps(details),
                                created,
                            ),
                        )
                        stats["artifacts"] += 1
                    except (TypeError, ValueError) as exc:
                        stats["skipped"] += 1
                        print(
                            f"[WARNING] Skipping malformed legacy validation "
                            f"record {target_path!r}: {exc}"
                        )

            tombstones = validation_payload.get("quarantineTombstones", {})
            if isinstance(tombstones, dict):
                for entry in tombstones.values():
                    try:
                        if not isinstance(entry, dict):
                            raise TypeError
                        db.execute(
                            """
                            INSERT OR REPLACE INTO quarantine_holds(
                                identity, target_hash, target_path,
                                target_language, rules_json, origin, first_seen,
                                last_seen, hold_until, occurrences
                            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                str(entry["identity"]),
                                str(entry["targetHash"]),
                                _path_key(entry["targetPath"]),
                                str(entry["targetLanguage"]),
                                json.dumps(entry.get("rules", [])),
                                str(entry.get("origin") or "unknown"),
                                str(entry["firstSeen"]),
                                str(entry["lastSeen"]),
                                str(entry["holdUntil"]),
                                int(entry.get("occurrences", 1)),
                            ),
                        )
                        stats["holds"] += 1
                    except (KeyError, TypeError, ValueError) as exc:
                        stats["skipped"] += 1
                        print(
                            f"[WARNING] Skipping malformed legacy quarantine "
                            f"record: {exc}"
                        )
            self._set_metadata(db, "legacy_json_migrated", "1")
            self._set_metadata(db, "legacy_json_migrated_at", _utc_iso())

        for legacy_path in (submit_path, validation_path):
            if not legacy_path.exists():
                continue
            backup = legacy_path.with_name(legacy_path.name + ".migrated.bak")
            try:
                if not backup.exists():
                    os.replace(legacy_path, backup)
            except OSError as exc:
                print(
                    f"[WARNING] Migrated {legacy_path} but could not preserve "
                    f"backup as {backup}: {exc}"
                )
        return stats

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
