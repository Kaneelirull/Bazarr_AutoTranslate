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

class OperationsRepositoryMixin:
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

    def start_maintenance_run(
        self,
        operation: str,
        *,
        due_reason: str | None = None,
        completed_cycle: int | None = None,
        now: float | None = None,
    ) -> int:
        timestamp = time.time() if now is None else float(now)
        with self._transaction() as db:
            cursor = db.execute(
                """
                INSERT INTO maintenance_runs(
                    operation, due_reason, completed_cycle, state,
                    metrics_json, started_at
                ) VALUES(?, ?, ?, 'active', '{}', ?)
                """,
                (str(operation), due_reason, completed_cycle, timestamp),
            )
            return int(cursor.lastrowid)

    def finish_maintenance_run(
        self,
        run_id: int,
        *,
        success: bool,
        metrics: dict | None = None,
        failure_code: str | None = None,
        now: float | None = None,
    ) -> bool:
        timestamp = time.time() if now is None else float(now)
        with self._transaction() as db:
            cursor = db.execute(
                """
                UPDATE maintenance_runs SET state=?, metrics_json=?,
                    failure_code=?, completed_at=?
                WHERE id=? AND state='active'
                """,
                (
                    "completed" if success else "failed",
                    json.dumps(metrics or {}, sort_keys=True),
                    failure_code,
                    timestamp,
                    int(run_id),
                ),
            )
            return cursor.rowcount == 1

    def record_source_readiness(
        self,
        *,
        media_identity: str,
        source_path: str | Path,
        source_language: str,
        source_hash: str,
        target_language: str,
        video_path: str | Path | None = None,
        media_duration_seconds: float | None = None,
        target_artifact_id: int | None = None,
        now: float | None = None,
    ) -> int:
        """Trust one exact source hash after a translated target validates."""
        timestamp = time.time() if now is None else float(now)
        duration = (
            float(media_duration_seconds)
            if media_duration_seconds is not None else None
        )
        with self._transaction() as db:
            db.execute(
                """
                INSERT INTO source_readiness(
                    media_identity, video_path, source_path, source_language,
                    source_hash, media_duration_seconds, validator_fingerprint,
                    config_fingerprint, target_artifact_id, target_language,
                    trusted_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    media_identity, source_language, source_hash,
                    validator_fingerprint, config_fingerprint
                ) DO UPDATE SET
                    video_path=excluded.video_path,
                    source_path=excluded.source_path,
                    media_duration_seconds=excluded.media_duration_seconds,
                    target_artifact_id=excluded.target_artifact_id,
                    target_language=excluded.target_language,
                    trusted_at=excluded.trusted_at
                """,
                (
                    str(media_identity), _path_key(video_path),
                    _path_key(source_path), str(source_language).lower(),
                    str(source_hash), duration, self.validator_version,
                    self.config_fingerprint, target_artifact_id,
                    str(target_language).lower(), timestamp,
                ),
            )
            row = db.execute(
                """
                SELECT id FROM source_readiness
                WHERE media_identity=? AND source_language=? AND source_hash=?
                  AND validator_fingerprint=? AND config_fingerprint=?
                """,
                (
                    str(media_identity), str(source_language).lower(),
                    str(source_hash), self.validator_version,
                    self.config_fingerprint,
                ),
            ).fetchone()
            if row is None:
                raise StateStoreError("could not persist source readiness")
            return int(row["id"])

    def source_readiness(
        self,
        *,
        media_identity: str,
        source_language: str,
        source_hash: str,
        media_duration_seconds: float | None = None,
        duration_tolerance: float = 0.5,
    ) -> dict | None:
        row = self._fetchone(
            """
            SELECT * FROM source_readiness
            WHERE media_identity=? AND source_language=? AND source_hash=?
              AND validator_fingerprint=? AND config_fingerprint=?
            ORDER BY trusted_at DESC LIMIT 1
            """,
            (
                str(media_identity), str(source_language).lower(),
                str(source_hash), self.validator_version,
                self.config_fingerprint,
            ),
        )
        if row is None:
            return None
        recorded_duration = row["media_duration_seconds"]
        if (
            media_duration_seconds is not None
            and recorded_duration is not None
            and abs(float(recorded_duration) - float(media_duration_seconds))
            > max(0.0, float(duration_tolerance))
        ):
            return None
        return {
            "id": int(row["id"]),
            "mediaIdentity": row["media_identity"],
            "sourcePath": row["source_path"],
            "sourceLanguage": row["source_language"],
            "sourceHash": row["source_hash"],
            "mediaDurationSeconds": recorded_duration,
            "targetArtifactId": row["target_artifact_id"],
            "targetLanguage": row["target_language"],
            "trustedAt": float(row["trusted_at"]),
        }

    def backfill_source_readiness(self) -> int:
        """Import successful legacy Lingarr attempts as hash-scoped evidence."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT attempt.id, attempt.video_path, attempt.target_identity,
                       attempt.source_path, attempt.source_hash,
                       attempt.source_language, attempt.target_language,
                       artifact.id AS artifact_id
                FROM translation_attempts AS attempt
                LEFT JOIN subtitle_artifacts AS artifact
                  ON artifact.attempt_id=attempt.id
                LEFT JOIN validation_results AS validation
                  ON validation.id=(
                      SELECT latest.id FROM validation_results AS latest
                      WHERE latest.artifact_id=artifact.id
                      ORDER BY latest.id DESC LIMIT 1
                  )
                WHERE attempt.status='completed'
                  AND attempt.source_path IS NOT NULL
                  AND attempt.source_hash IS NOT NULL
                  AND attempt.source_language IS NOT NULL
                  AND validation.result IN ('valid', 'valid_with_warnings')
                ORDER BY attempt.id
                """
            ).fetchall()
        inserted = 0
        for row in rows:
            video_path = row["video_path"]
            identity = row["target_identity"]
            if not identity and video_path:
                identity = os.path.normcase(
                    os.path.abspath(os.path.splitext(str(video_path))[0])
                )
            if not identity:
                continue
            before = self.source_readiness(
                media_identity=identity,
                source_language=row["source_language"],
                source_hash=row["source_hash"],
            )
            self.record_source_readiness(
                media_identity=identity,
                video_path=video_path,
                source_path=row["source_path"],
                source_language=row["source_language"],
                source_hash=row["source_hash"],
                target_language=row["target_language"],
                target_artifact_id=row["artifact_id"],
            )
            inserted += int(before is None)
        return inserted

    def record_provider_event(
        self,
        *,
        provider: str,
        operation: str,
        classification: str,
        retryable: bool,
        http_status: int | None = None,
        response_shape: dict | None = None,
        model: str | None = None,
    ) -> None:
        with self._transaction() as db:
            db.execute(
                """
                INSERT INTO provider_events(
                    provider, operation, classification, http_status,
                    retryable, response_shape_json, model, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(provider), str(operation), str(classification), http_status,
                    int(bool(retryable)), json.dumps(response_shape or {}, sort_keys=True),
                    model, time.time(),
                ),
            )
