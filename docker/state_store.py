from __future__ import annotations

import json
import os
import sqlite3
import statistics
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator


SCHEMA_VERSION = 6

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


class StateStore:
    """Transactional application state shared by translation worker threads."""

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
                    failure_category TEXT,
                    failure_details_json TEXT,
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
                    resolved_at TEXT,
                    PRIMARY KEY(identity, target_hash)
                );

                CREATE TABLE IF NOT EXISTS timing_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    source_language TEXT,
                    target_language TEXT NOT NULL,
                    cue_count INTEGER NOT NULL,
                    elapsed_seconds REAL NOT NULL,
                    seconds_per_cue REAL NOT NULL,
                    outcome TEXT NOT NULL,
                    lingarr_job_id INTEGER,
                    attempts INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_timing_samples_lookup
                    ON timing_samples(
                        kind, source_language, target_language, outcome, created_at DESC
                    );

                CREATE TABLE IF NOT EXISTS circuit_breakers (
                    series_key TEXT PRIMARY KEY,
                    series_title TEXT NOT NULL,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'closed',
                    opened_at REAL,
                    retry_at REAL,
                    half_open_claimed INTEGER NOT NULL DEFAULT 0,
                    config_fingerprint TEXT NOT NULL,
                    last_reason TEXT,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS retry_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_type TEXT NOT NULL,
                    item_id INTEGER NOT NULL,
                    target_language TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    source_path TEXT,
                    source_language TEXT,
                    target_path TEXT,
                    series_key TEXT,
                    series_title TEXT,
                    media_title TEXT,
                    source_cue_count INTEGER,
                    failure_class TEXT NOT NULL,
                    rules_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    first_failure_at REAL NOT NULL,
                    last_failure_at REAL NOT NULL,
                    failed_output_hash TEXT,
                    eligible_completed_cycle INTEGER NOT NULL,
                    last_attempt_cycle INTEGER,
                    end_cycle_repair_attempted INTEGER NOT NULL DEFAULT 0,
                    final_outcome TEXT,
                    artifact_path TEXT,
                    report_path TEXT,
                    last_reason TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(item_type, item_id, target_language, source_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_retry_plans_due
                    ON retry_plans(state, eligible_completed_cycle, first_failure_at);
                CREATE INDEX IF NOT EXISTS idx_retry_plans_identity
                    ON retry_plans(item_type, item_id, target_language, updated_at DESC);

                CREATE TABLE IF NOT EXISTS quarantine_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_type TEXT NOT NULL,
                    item_id INTEGER NOT NULL,
                    target_language TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    target_hash TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    validator_fingerprint TEXT NOT NULL,
                    config_fingerprint TEXT NOT NULL,
                    artifact_path TEXT,
                    report_path TEXT,
                    failure_rules_json TEXT NOT NULL,
                    repair_provenance_json TEXT NOT NULL,
                    donor_provenance_json TEXT NOT NULL,
                    cue_signatures_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(
                        item_type, item_id, target_language,
                        source_hash, target_hash, attempt_number
                    )
                );
                CREATE INDEX IF NOT EXISTS idx_quarantine_attempt_donors
                    ON quarantine_attempts(
                        item_type, item_id, target_language, created_at DESC
                    );
                """
            )
            quarantine_columns = {
                row["name"]
                for row in self._connection.execute(
                    "PRAGMA table_info(quarantine_holds)"
                ).fetchall()
            }
            if "resolved_at" not in quarantine_columns:
                self._connection.execute(
                    "ALTER TABLE quarantine_holds ADD COLUMN resolved_at TEXT"
                )
            retry_columns = {
                row["name"]
                for row in self._connection.execute(
                    "PRAGMA table_info(retry_plans)"
                ).fetchall()
            }
            if "source_language" not in retry_columns:
                self._connection.execute(
                    "ALTER TABLE retry_plans ADD COLUMN source_language TEXT"
                )
            if "source_cue_count" not in retry_columns:
                self._connection.execute(
                    "ALTER TABLE retry_plans ADD COLUMN source_cue_count INTEGER"
                )
            attempt_columns = {
                row["name"]
                for row in self._connection.execute(
                    "PRAGMA table_info(translation_attempts)"
                ).fetchall()
            }
            if "failure_category" not in attempt_columns:
                self._connection.execute(
                    "ALTER TABLE translation_attempts ADD COLUMN failure_category TEXT"
                )
            if "failure_details_json" not in attempt_columns:
                self._connection.execute(
                    "ALTER TABLE translation_attempts ADD COLUMN failure_details_json TEXT"
                )
            self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @staticmethod
    def _quarantine_attempt_dict(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "itemType": row["item_type"],
            "itemId": int(row["item_id"]),
            "targetLanguage": row["target_language"],
            "sourceHash": row["source_hash"],
            "targetHash": row["target_hash"],
            "attemptNumber": int(row["attempt_number"]),
            "validatorFingerprint": row["validator_fingerprint"],
            "configFingerprint": row["config_fingerprint"],
            "artifactPath": row["artifact_path"],
            "reportPath": row["report_path"],
            "failureRules": json.loads(row["failure_rules_json"]),
            "repairProvenance": json.loads(row["repair_provenance_json"]),
            "donorProvenance": json.loads(row["donor_provenance_json"]),
            "cueSignatures": json.loads(row["cue_signatures_json"]),
            "createdAt": float(row["created_at"]),
        }

    def record_quarantine_attempt(
        self,
        *,
        item_type: str,
        item_id: int,
        target_language: str,
        source_hash: str,
        target_hash: str,
        attempt_number: int,
        artifact_path: str | Path | None,
        report_path: str | Path | None,
        failure_rules: Iterable[str],
        cue_signatures: list[dict],
        repair_provenance: list[dict] | None = None,
        donor_provenance: list[dict] | None = None,
        created_at: float | None = None,
    ) -> dict:
        """Append one immutable failed-output record for later donor recovery."""
        timestamp = time.time() if created_at is None else float(created_at)
        values = (
            str(item_type), int(item_id), str(target_language).lower(),
            str(source_hash), str(target_hash), max(1, int(attempt_number)),
            self.validator_version, self.config_fingerprint,
            _path_key(artifact_path), _path_key(report_path),
            json.dumps(sorted({str(rule) for rule in failure_rules if rule})),
            json.dumps(repair_provenance or [], sort_keys=True),
            json.dumps(donor_provenance or [], sort_keys=True),
            json.dumps(cue_signatures, sort_keys=True), timestamp,
        )
        with self._transaction() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO quarantine_attempts(
                    item_type, item_id, target_language, source_hash, target_hash,
                    attempt_number, validator_fingerprint, config_fingerprint,
                    artifact_path, report_path, failure_rules_json,
                    repair_provenance_json, donor_provenance_json,
                    cue_signatures_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            row = db.execute(
                """
                SELECT * FROM quarantine_attempts
                WHERE item_type=? AND item_id=? AND target_language=?
                  AND source_hash=? AND target_hash=? AND attempt_number=?
                """,
                values[:6],
            ).fetchone()
        return self._quarantine_attempt_dict(row)

    def quarantine_attempts(
        self, item_type: str, item_id: int, target_language: str
    ) -> list[dict]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM quarantine_attempts
                WHERE item_type=? AND item_id=? AND target_language=?
                ORDER BY attempt_number DESC, created_at DESC, id DESC
                """,
                (str(item_type), int(item_id), str(target_language).lower()),
            ).fetchall()
        return [self._quarantine_attempt_dict(row) for row in rows]

    def completed_cycle(self) -> int:
        value = self._metadata("completed_cycle")
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    def advance_completed_cycle(self) -> int:
        with self._transaction() as db:
            row = db.execute(
                "SELECT value FROM state_metadata WHERE key = 'completed_cycle'"
            ).fetchone()
            try:
                current = max(0, int(row["value"])) if row else 0
            except (TypeError, ValueError):
                current = 0
            completed = current + 1
            db.execute(
                """
                INSERT INTO state_metadata(key, value) VALUES('completed_cycle', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(completed),),
            )
            return completed

    @staticmethod
    def _retry_plan_dict(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "itemType": row["item_type"],
            "itemId": int(row["item_id"]),
            "targetLanguage": row["target_language"],
            "sourceHash": row["source_hash"],
            "sourcePath": row["source_path"],
            "sourceLanguage": row["source_language"],
            "targetPath": row["target_path"],
            "seriesKey": row["series_key"],
            "seriesTitle": row["series_title"],
            "mediaTitle": row["media_title"],
            "sourceCueCount": row["source_cue_count"],
            "failureClass": row["failure_class"],
            "rules": json.loads(row["rules_json"]),
            "state": row["state"],
            "attemptCount": int(row["attempt_count"]),
            "firstFailureAt": float(row["first_failure_at"]),
            "lastFailureAt": float(row["last_failure_at"]),
            "failedOutputHash": row["failed_output_hash"],
            "eligibleCompletedCycle": int(row["eligible_completed_cycle"]),
            "lastAttemptCycle": row["last_attempt_cycle"],
            "endCycleRepairAttempted": bool(row["end_cycle_repair_attempted"]),
            "finalOutcome": row["final_outcome"],
            "artifactPath": row["artifact_path"],
            "reportPath": row["report_path"],
            "lastReason": row["last_reason"],
            "createdAt": float(row["created_at"]),
            "updatedAt": float(row["updated_at"]),
        }

    def schedule_retry_plan(
        self,
        *,
        item_type: str,
        item_id: int,
        target_language: str,
        source_hash: str,
        failure_class: str,
        rules: Iterable[str],
        eligible_completed_cycle: int,
        state: str,
        failed_output_hash: str | None = None,
        source_path: str | Path | None = None,
        source_language: str | None = None,
        target_path: str | Path | None = None,
        series_key: str | None = None,
        series_title: str | None = None,
        media_title: str | None = None,
        source_cue_count: int | None = None,
        artifact_path: str | Path | None = None,
        report_path: str | Path | None = None,
        reason: str | None = None,
        now: float | None = None,
    ) -> tuple[dict, bool]:
        """Create/update a retry plan and return (plan, repeated_same_output)."""
        timestamp = time.time() if now is None else float(now)
        identity = (str(item_type), int(item_id), str(target_language).lower())
        source_hash = str(source_hash or "")
        if not source_hash:
            raise StateStoreError("retry plans require a source hash")
        rule_values = sorted({str(rule) for rule in rules if rule})
        with self._transaction() as db:
            db.execute(
                """
                UPDATE retry_plans
                SET state = 'superseded', final_outcome = 'source_changed',
                    updated_at = ?
                WHERE item_type = ? AND item_id = ? AND target_language = ?
                  AND source_hash <> ?
                  AND state IN (
                    'repair_retry_queued', 'regeneration_waiting',
                    'regeneration_queued', 'retry_in_progress'
                  )
                """,
                (timestamp, *identity, source_hash),
            )
            previous = db.execute(
                """
                SELECT * FROM retry_plans
                WHERE item_type = ? AND item_id = ? AND target_language = ?
                  AND source_hash = ?
                """,
                (*identity, source_hash),
            ).fetchone()
            repeated = bool(
                previous
                and failed_output_hash
                and previous["failed_output_hash"] == failed_output_hash
            )
            if previous:
                db.execute(
                    """
                    UPDATE retry_plans SET
                        source_path = COALESCE(?, source_path),
                        source_language = COALESCE(?, source_language),
                        target_path = COALESCE(?, target_path),
                        series_key = COALESCE(?, series_key),
                        series_title = COALESCE(?, series_title),
                        media_title = COALESCE(?, media_title),
                        source_cue_count = COALESCE(?, source_cue_count),
                        failure_class = ?, rules_json = ?,
                        state = CASE WHEN ? THEN state ELSE ? END,
                        last_failure_at = ?,
                        failed_output_hash = COALESCE(?, failed_output_hash),
                        eligible_completed_cycle =
                            CASE WHEN ? THEN eligible_completed_cycle ELSE ? END,
                        artifact_path = COALESCE(?, artifact_path),
                        report_path = COALESCE(?, report_path),
                        last_reason = COALESCE(?, last_reason),
                        final_outcome = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        _path_key(source_path), source_language, _path_key(target_path),
                        series_key, series_title, media_title, source_cue_count,
                        failure_class, json.dumps(rule_values), repeated, state,
                        timestamp, failed_output_hash, repeated,
                        max(0, int(eligible_completed_cycle)),
                        _path_key(artifact_path), _path_key(report_path),
                        reason, timestamp, previous["id"],
                    ),
                )
                plan_id = int(previous["id"])
            else:
                cursor = db.execute(
                    """
                    INSERT INTO retry_plans(
                        item_type, item_id, target_language, source_hash,
                        source_path, source_language, target_path, series_key, series_title,
                        media_title, source_cue_count, failure_class, rules_json, state,
                        first_failure_at, last_failure_at, failed_output_hash,
                        eligible_completed_cycle, artifact_path, report_path,
                        last_reason, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        *identity, source_hash, _path_key(source_path),
                        source_language, _path_key(target_path), series_key, series_title,
                        media_title, source_cue_count, failure_class, json.dumps(rule_values), state,
                        timestamp, timestamp, failed_output_hash,
                        max(0, int(eligible_completed_cycle)),
                        _path_key(artifact_path), _path_key(report_path), reason,
                        timestamp, timestamp,
                    ),
                )
                plan_id = int(cursor.lastrowid)
            row = db.execute(
                "SELECT * FROM retry_plans WHERE id = ?", (plan_id,)
            ).fetchone()
        return self._retry_plan_dict(row), repeated

    def claim_due_retry_plans(
        self,
        completed_cycle: int,
        *,
        limit: int,
        per_series_limit: int = 1,
    ) -> list[dict]:
        selected: list[sqlite3.Row] = []
        series_counts: dict[str, int] = {}
        with self._transaction() as db:
            rows = db.execute(
                """
                SELECT * FROM retry_plans
                WHERE state = 'regeneration_waiting'
                  AND eligible_completed_cycle <= ?
                ORDER BY eligible_completed_cycle,
                         CAST(first_failure_at / 86400 AS INTEGER),
                         CASE WHEN source_cue_count IS NULL THEN 1 ELSE 0 END,
                         source_cue_count, attempt_count, first_failure_at,
                         item_type, item_id, target_language
                """,
                (max(0, int(completed_cycle)),),
            ).fetchall()
            for row in rows:
                series_bucket = row["series_key"] or f"{row['item_type']}:{row['item_id']}"
                if series_counts.get(series_bucket, 0) >= max(1, per_series_limit):
                    continue
                selected.append(row)
                series_counts[series_bucket] = series_counts.get(series_bucket, 0) + 1
                if len(selected) >= max(1, limit):
                    break
            for row in selected:
                db.execute(
                    """
                    UPDATE retry_plans
                    SET state = 'regeneration_queued', updated_at = ?
                    WHERE id = ? AND state = 'regeneration_waiting'
                    """,
                    (time.time(), row["id"]),
                )
            claimed = [
                db.execute(
                    "SELECT * FROM retry_plans WHERE id = ?", (row["id"],)
                ).fetchone()
                for row in selected
            ]
        return [self._retry_plan_dict(row) for row in claimed if row is not None]

    def due_retry_count(self, completed_cycle: int) -> int:
        row = self._fetchone(
            """
            SELECT COUNT(*) AS count
            FROM retry_plans
            WHERE state = 'regeneration_waiting'
              AND eligible_completed_cycle <= ?
            """,
            (max(0, int(completed_cycle)),),
        )
        return int(row["count"]) if row else 0

    def recover_retry_claims(self) -> int:
        """Release crash-orphaned claims without changing attempts or eligibility."""
        with self._transaction() as db:
            cursor = db.execute(
                """
                UPDATE retry_plans
                SET state = 'regeneration_waiting',
                    last_reason = 'recovered after service restart',
                    updated_at = ?
                WHERE state IN ('regeneration_queued', 'retry_in_progress')
                """,
                (time.time(),),
            )
            return max(0, int(cursor.rowcount))

    def set_retry_source_cue_count(self, plan_id: int, cue_count: int) -> bool:
        with self._transaction() as db:
            cursor = db.execute(
                """
                UPDATE retry_plans
                SET source_cue_count = ?, updated_at = ?
                WHERE id = ? AND source_cue_count IS NULL
                """,
                (max(1, int(cue_count)), time.time(), int(plan_id)),
            )
            return cursor.rowcount == 1

    def update_retry_plan(
        self,
        plan_id: int,
        *,
        state: str,
        completed_cycle: int | None = None,
        eligible_completed_cycle: int | None = None,
        increment_attempt: bool = False,
        final_outcome: str | None = None,
        reason: str | None = None,
        end_cycle_repair_attempted: bool | None = None,
    ) -> dict | None:
        with self._transaction() as db:
            db.execute(
                """
                UPDATE retry_plans SET
                    state = ?,
                    attempt_count = attempt_count + ?,
                    last_attempt_cycle = COALESCE(?, last_attempt_cycle),
                    eligible_completed_cycle =
                        COALESCE(?, eligible_completed_cycle),
                    final_outcome = ?,
                    last_reason = COALESCE(?, last_reason),
                    end_cycle_repair_attempted =
                        COALESCE(?, end_cycle_repair_attempted),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    state, 1 if increment_attempt else 0, completed_cycle,
                    eligible_completed_cycle, final_outcome, reason,
                    (
                        int(end_cycle_repair_attempted)
                        if end_cycle_repair_attempted is not None else None
                    ),
                    time.time(), int(plan_id),
                ),
            )
            row = db.execute(
                "SELECT * FROM retry_plans WHERE id = ?", (int(plan_id),)
            ).fetchone()
        return self._retry_plan_dict(row)

    def resolve_retry_plans(
        self,
        item_type: str,
        item_id: int,
        target_language: str,
        *,
        outcome: str = "accepted_after_retry",
    ) -> int:
        with self._transaction() as db:
            cursor = db.execute(
                """
                UPDATE retry_plans
                SET state = ?, final_outcome = ?, updated_at = ?
                WHERE item_type = ? AND item_id = ? AND target_language = ?
                  AND state IN (
                    'repair_retry_queued', 'regeneration_waiting',
                    'regeneration_queued', 'retry_in_progress'
                  )
                """,
                (
                    outcome, outcome, time.time(), str(item_type), int(item_id),
                    str(target_language).lower(),
                ),
            )
            return cursor.rowcount

    def retry_plans(self, *, include_terminal: bool = True) -> list[dict]:
        query = "SELECT * FROM retry_plans"
        params: tuple = ()
        if not include_terminal:
            placeholders = ",".join("?" for _ in ACTIVE_RETRY_STATES)
            query += f" WHERE state IN ({placeholders})"
            params = tuple(sorted(ACTIVE_RETRY_STATES))
        query += " ORDER BY updated_at DESC"
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return [self._retry_plan_dict(row) for row in rows]

    def active_retry_plan(
        self, item_type: str, item_id: int, target_language: str
    ) -> dict | None:
        placeholders = ",".join("?" for _ in ACTIVE_RETRY_STATES)
        params = (
            str(item_type), int(item_id), str(target_language).lower(),
            *sorted(ACTIVE_RETRY_STATES),
        )
        with self._lock:
            row = self._connection.execute(
                f"""
                SELECT * FROM retry_plans
                WHERE item_type = ? AND item_id = ? AND target_language = ?
                  AND state IN ({placeholders})
                ORDER BY updated_at DESC LIMIT 1
                """,
                params,
            ).fetchone()
        return self._retry_plan_dict(row)

    def legacy_retry_candidates(self) -> list[dict]:
        """Return legacy holds with enough immutable provenance for safe migration."""
        if self._metadata("quarantine_retry_migrated_v4") == "1":
            return []
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT q.*, a.item_type, a.item_id, a.source_path,
                       a.source_language, a.source_hash
                FROM quarantine_holds q
                JOIN subtitle_artifacts a
                  ON a.target_hash = q.target_hash
                 AND a.target_language = q.target_language
                WHERE q.resolved_at IS NULL
                  AND a.item_type IN ('episodes', 'movies')
                  AND a.item_id IS NOT NULL
                  AND a.source_path IS NOT NULL
                  AND a.source_hash IS NOT NULL
                GROUP BY q.identity, q.target_hash
                ORDER BY q.first_seen
                """
            ).fetchall()
        return [
            {
                "identity": row["identity"],
                "targetPath": row["target_path"],
                "targetHash": row["target_hash"],
                "targetLanguage": row["target_language"],
                "rules": json.loads(row["rules_json"]),
                "origin": row["origin"],
                "itemType": row["item_type"],
                "itemId": int(row["item_id"]),
                "sourcePath": row["source_path"],
                "sourceLanguage": row["source_language"],
                "sourceHash": row["source_hash"],
            }
            for row in rows
        ]

    def mark_legacy_retry_migration_complete(self) -> None:
        with self._transaction() as db:
            self._set_metadata(db, "quarantine_retry_migrated_v4", "1")

    def record_timing_sample(
        self,
        *,
        kind: str,
        source_language: str | None,
        target_language: str,
        cue_count: int,
        elapsed_seconds: float,
        outcome: str,
        lingarr_job_id: int | None = None,
        attempts: int = 1,
    ) -> None:
        if cue_count <= 0 or elapsed_seconds <= 0:
            return
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO timing_samples(
                    kind, source_language, target_language, cue_count,
                    elapsed_seconds, seconds_per_cue, outcome,
                    lingarr_job_id, attempts, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    kind,
                    source_language,
                    target_language,
                    cue_count,
                    elapsed_seconds,
                    elapsed_seconds / cue_count,
                    outcome,
                    lingarr_job_id,
                    max(1, attempts),
                    time.time(),
                ),
            )

    def timing_estimate(
        self,
        *,
        kind: str,
        source_language: str | None,
        target_language: str,
        cold_seconds_per_cue: float,
        alpha: float,
        limit: int = 50,
    ) -> dict:
        """Return a robust EWMA, falling back from language pair to global data."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT seconds_per_cue
                FROM timing_samples
                WHERE kind = ? AND source_language IS ? AND target_language = ?
                  AND outcome = 'accepted'
                ORDER BY created_at DESC LIMIT ?
                """,
                (kind, source_language, target_language, limit),
            ).fetchall()
            scope = "language_pair"
            if not rows:
                rows = self._connection.execute(
                    """
                    SELECT seconds_per_cue
                    FROM timing_samples
                    WHERE kind = ? AND outcome = 'accepted'
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (kind, limit),
                ).fetchall()
                scope = "global" if rows else "cold_start"
        values = [float(row["seconds_per_cue"]) for row in reversed(rows)]
        if not values:
            return {
                "secondsPerCue": float(cold_seconds_per_cue),
                "sampleCount": 0,
                "scope": scope,
            }
        median = statistics.median(values)
        lower = max(0.01, median * 0.25)
        upper = max(lower, median * 4.0)
        estimate = min(max(values[0], lower), upper)
        blend = min(1.0, max(0.01, float(alpha)))
        for value in values[1:]:
            clamped = min(max(value, lower), upper)
            estimate = blend * clamped + (1.0 - blend) * estimate
        return {
            "secondsPerCue": estimate,
            "sampleCount": len(values),
            "scope": scope,
        }

    def circuit_permission(
        self,
        *,
        series_key: str,
        series_title: str,
        config_fingerprint: str,
        claim: bool = True,
        now: float | None = None,
    ) -> dict:
        now = time.time() if now is None else now
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM circuit_breakers WHERE series_key = ?",
                (series_key,),
            ).fetchone()
            if row is None or row["config_fingerprint"] != config_fingerprint:
                if not claim:
                    return {"allowed": True, "state": "closed", "failures": 0}
                connection.execute(
                    """
                    INSERT INTO circuit_breakers(
                        series_key, series_title, config_fingerprint, updated_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(series_key) DO UPDATE SET
                        series_title=excluded.series_title,
                        consecutive_failures=0, state='closed', opened_at=NULL,
                        retry_at=NULL, half_open_claimed=0,
                        config_fingerprint=excluded.config_fingerprint,
                        last_reason=NULL, updated_at=excluded.updated_at
                    """,
                    (series_key, series_title, config_fingerprint, now),
                )
                return {"allowed": True, "state": "closed", "failures": 0}
            state = row["state"]
            if state == "open" and row["retry_at"] and now >= row["retry_at"]:
                if not claim:
                    return {
                        "allowed": True,
                        "state": "half_open",
                        "failures": row["consecutive_failures"],
                        "retryAt": row["retry_at"],
                    }
                connection.execute(
                    """
                    UPDATE circuit_breakers
                    SET state='half_open', half_open_claimed=1, updated_at=?
                    WHERE series_key=?
                    """,
                    (now, series_key),
                )
                return {
                    "allowed": True,
                    "state": "half_open",
                    "failures": row["consecutive_failures"],
                    "retryAt": row["retry_at"],
                }
            allowed = state == "closed"
            return {
                "allowed": allowed,
                "state": state,
                "failures": row["consecutive_failures"],
                "retryAt": row["retry_at"],
                "reason": row["last_reason"],
            }

    def record_circuit_outcome(
        self,
        *,
        series_key: str,
        series_title: str,
        success: bool,
        reason: str | None,
        threshold: int,
        open_seconds: int,
        config_fingerprint: str,
        now: float | None = None,
    ) -> dict:
        now = time.time() if now is None else now
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM circuit_breakers WHERE series_key=?",
                (series_key,),
            ).fetchone()
            failures = 0 if success else int(row["consecutive_failures"] if row else 0) + 1
            state = "closed"
            opened_at = None
            retry_at = None
            if not success and failures >= max(1, threshold):
                state = "open"
                opened_at = now
                retry_at = now + max(1, open_seconds)
            connection.execute(
                """
                INSERT INTO circuit_breakers(
                    series_key, series_title, consecutive_failures, state,
                    opened_at, retry_at, half_open_claimed, config_fingerprint,
                    last_reason, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                ON CONFLICT(series_key) DO UPDATE SET
                    series_title=excluded.series_title,
                    consecutive_failures=excluded.consecutive_failures,
                    state=excluded.state, opened_at=excluded.opened_at,
                    retry_at=excluded.retry_at, half_open_claimed=0,
                    config_fingerprint=excluded.config_fingerprint,
                    last_reason=excluded.last_reason, updated_at=excluded.updated_at
                """,
                (
                    series_key,
                    series_title,
                    failures,
                    state,
                    opened_at,
                    retry_at,
                    config_fingerprint,
                    None if success else reason,
                    now,
                ),
            )
        return {
            "state": state,
            "failures": failures,
            "retryAt": retry_at,
            "reason": None if success else reason,
        }

    def circuit_breakers(self) -> list[dict]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT series_key, series_title, consecutive_failures, state,
                       retry_at, last_reason
                FROM circuit_breakers
                WHERE state != 'closed' OR consecutive_failures > 0
                ORDER BY updated_at DESC
                """
            ).fetchall()
        return [
            {
                "seriesKey": row["series_key"],
                "seriesTitle": row["series_title"],
                "failures": row["consecutive_failures"],
                "state": row["state"],
                "retryAt": row["retry_at"],
                "reason": row["last_reason"],
            }
            for row in rows
        ]

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

    def mark_submission_failed(
        self,
        attempt_id: int,
        *,
        failure_category: str | None = None,
        failure_details: dict | None = None,
    ) -> None:
        with self._transaction() as db:
            db.execute(
                """
                UPDATE translation_attempts
                SET status = 'failed', cooldown_until = 0,
                    failure_category = COALESCE(?, failure_category),
                    failure_details_json = COALESCE(?, failure_details_json),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    failure_category,
                    json.dumps(failure_details, ensure_ascii=False)
                    if failure_details else None,
                    time.time(),
                    int(attempt_id),
                ),
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
                        hold_until = now.isoformat()
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
                                resolved_at = NULL,
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

    def record_quarantine_event(
        self,
        identity: str,
        *,
        target_path: str | Path,
        target_hash: str,
        target_language: str,
        rules: Iterable[str],
        origin: str | None,
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
            # Kept populated for compatibility with the legacy physical schema.
            # It is audit metadata only and is never consulted for eligibility.
            hold_until = timestamp.isoformat()
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
                    resolved_at = NULL,
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
                "occurrences": occurrences,
                "resolvedAt": None,
            },
            repeated,
        )

    def quarantine_event(
        self,
        identity: str,
        *,
        target_hash: str | None = None,
    ) -> dict | None:
        query = "SELECT * FROM quarantine_holds WHERE identity = ?"
        params: list[object] = [str(identity)]
        if target_hash is not None:
            query += " AND target_hash = ?"
            params.append(target_hash)
        query += " ORDER BY last_seen DESC LIMIT 1"
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
            "occurrences": int(row["occurrences"]),
            "resolvedAt": row["resolved_at"],
        }

    def resolve_quarantine_events(
        self, identity: str, *, now: datetime | None = None
    ) -> bool:
        resolved_at = (now or datetime.now(timezone.utc)).isoformat()
        with self._transaction() as db:
            cursor = db.execute(
                """
                UPDATE quarantine_holds
                SET resolved_at = ?
                WHERE identity = ? AND resolved_at IS NULL
                """,
                (resolved_at, str(identity)),
            )
            return cursor.rowcount > 0

    def prune_older_than(
        self, retention_days: int, now: datetime | None = None
    ) -> int:
        timestamp = now or datetime.now(timezone.utc)
        cutoff = datetime.fromtimestamp(
            timestamp.timestamp() - retention_days * 86400, timezone.utc
        ).isoformat()
        cutoff_timestamp = timestamp.timestamp() - retention_days * 86400
        with self._transaction() as db:
            validations = db.execute(
                "DELETE FROM validation_results WHERE validated_at < ?", (cutoff,)
            ).rowcount
            holds = db.execute(
                "DELETE FROM quarantine_holds WHERE last_seen < ?",
                (cutoff,),
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
            timings = db.execute(
                "DELETE FROM timing_samples WHERE created_at < ?",
                (cutoff_timestamp,),
            ).rowcount
        return int(validations + holds + attempts + timings)

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
