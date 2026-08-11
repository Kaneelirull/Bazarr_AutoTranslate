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

class SubmissionsRepositoryMixin:
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
