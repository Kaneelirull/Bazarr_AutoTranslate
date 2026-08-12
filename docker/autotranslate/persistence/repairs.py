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

class RepairsRepositoryMixin:
    def enqueue_repair_job(
        self,
        *,
        dedupe_key: str,
        target_language: str,
        item_type: str | None = None,
        item_id: int | None = None,
        source_path: str | Path | None = None,
        target_path: str | Path | None = None,
        source_hash: str | None = None,
        target_hash: str | None = None,
        cue_indexes: Iterable[int] = (),
        payload: dict | None = None,
        now: float | None = None,
    ) -> int:
        timestamp = time.time() if now is None else float(now)
        with self._transaction() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO repair_jobs(
                    dedupe_key, item_type, item_id, target_language,
                    source_path, target_path, source_hash, target_hash,
                    cue_indexes_json, payload_json, state, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                """,
                (
                    str(dedupe_key), item_type, item_id, str(target_language),
                    _path_key(source_path), _path_key(target_path), source_hash,
                    target_hash, json.dumps(list(cue_indexes)),
                    json.dumps(payload or {}, sort_keys=True), timestamp, timestamp,
                ),
            )
            row = db.execute(
                """
                SELECT id FROM repair_jobs
                WHERE dedupe_key=?
                  AND state IN ('queued', 'active', 'persisted_for_restart')
                ORDER BY id DESC LIMIT 1
                """,
                (str(dedupe_key),),
            ).fetchone()
            if row is None:
                raise StateStoreError("could not persist repair job")
            return int(row["id"])

    def transition_repair_job(
        self,
        job_id: int,
        state: str,
        *,
        lease_owner: str | None = None,
        lease_expires_at: float | None = None,
        shutdown_classification: str | None = None,
        error_code: str | None = None,
        expected_states: Iterable[str] | None = None,
    ) -> bool:
        with self._transaction() as db:
            expected = tuple(str(value) for value in (expected_states or ()))
            state_filter = ""
            parameters: list[object] = [
                str(state), lease_owner, lease_expires_at,
                shutdown_classification, error_code, time.time(), int(job_id),
            ]
            if expected:
                state_filter = " AND state IN ({})".format(
                    ",".join("?" for _value in expected)
                )
                parameters.extend(expected)
            cursor = db.execute(
                f"""
                UPDATE repair_jobs SET state=?, lease_owner=?, lease_expires_at=?,
                    shutdown_classification=COALESCE(?, shutdown_classification),
                    last_error_code=COALESCE(?, last_error_code), updated_at=?
                WHERE id=?{state_filter}
                """,
                parameters,
            )
            return cursor.rowcount == 1

    def repair_jobs_for_restart(self) -> list[dict]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM repair_jobs
                WHERE state='persisted_for_restart'
                ORDER BY created_at, id
                """
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "dedupeKey": row["dedupe_key"],
                "itemType": row["item_type"],
                "itemId": row["item_id"],
                "targetLanguage": row["target_language"],
                "sourcePath": row["source_path"],
                "targetPath": row["target_path"],
                "sourceHash": row["source_hash"],
                "targetHash": row["target_hash"],
                "cueIndexes": json.loads(row["cue_indexes_json"] or "[]"),
                "payload": json.loads(row["payload_json"] or "{}"),
            }
            for row in rows
        ]

    def has_durable_repair_for_retry(self, retry_plan_id: int) -> bool:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT payload_json FROM repair_jobs
                WHERE state IN ('queued', 'active', 'persisted_for_restart')
                """
            ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if payload.get("retryPlanId") == int(retry_plan_id):
                return True
        return False

    def update_repair_job_coordination(
        self, job_id: int, *, retry_plan_id: int, trial_owner: str | None,
        trial_job_id: int | None, trial_plan_id: int | None,
        trial_generation: int,
    ) -> bool:
        with self._transaction() as db:
            row = db.execute(
                "SELECT payload_json FROM repair_jobs WHERE id=? AND state IN ('queued', 'active')",
                (int(job_id),),
            ).fetchone()
            if row is None:
                return False
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (TypeError, ValueError):
                payload = {}
            payload.update({
                "retryPlanId": int(retry_plan_id),
                "trialOwner": trial_owner,
                "trialJobId": trial_job_id,
                "trialPlanId": trial_plan_id,
                "trialGeneration": int(trial_generation),
            })
            cursor = db.execute(
                "UPDATE repair_jobs SET payload_json=?, updated_at=? WHERE id=? AND state IN ('queued', 'active')",
                (json.dumps(payload, sort_keys=True), time.time(), int(job_id)),
            )
            return cursor.rowcount == 1

    def recover_repair_jobs(self) -> int:
        with self._transaction() as db:
            cursor = db.execute(
                """
                UPDATE repair_jobs SET state='persisted_for_restart',
                    lease_owner=NULL, lease_expires_at=NULL,
                    shutdown_classification='persisted_for_restart', updated_at=?
                WHERE state IN ('queued', 'active')
                """,
                (time.time(),),
            )
            return max(0, int(cursor.rowcount))

    def record_partial_candidate(
        self,
        *,
        item_type: str,
        item_id: int,
        target_language: str,
        source_hash: str,
        target_hash: str,
        changed_cues: Iterable[int],
        unresolved_cues: Iterable[int],
        provenance: list[dict],
        artifact_path: str | Path | None,
        source_language: str | None = None,
        retry_plan_id: int | None = None,
        quarantine_attempt_id: int | None = None,
        validation_level: str = "partial_file_improved",
    ) -> int:
        with self._transaction() as db:
            cursor = db.execute(
                """
                INSERT INTO partial_candidates(
                    retry_plan_id, quarantine_attempt_id, item_type, item_id,
                    source_language, target_language, source_hash, target_hash,
                    artifact_path, validation_level, validator_fingerprint,
                    config_fingerprint, changed_cues_json, unresolved_cues_json,
                    provenance_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    retry_plan_id, quarantine_attempt_id, str(item_type), int(item_id),
                    source_language, str(target_language), str(source_hash),
                    str(target_hash), _path_key(artifact_path), validation_level,
                    self.validator_version, self.config_fingerprint,
                    json.dumps(sorted(set(int(value) for value in changed_cues))),
                    json.dumps(sorted(set(int(value) for value in unresolved_cues))),
                    json.dumps(provenance, sort_keys=True), time.time(),
                ),
            )
            return int(cursor.lastrowid)

    def finalize_partial_candidate(
        self,
        candidate_id: int,
        *,
        artifact_path: str | Path,
        quarantine_attempt_id: int | None = None,
    ) -> bool:
        with self._transaction() as db:
            cursor = db.execute(
                """
                UPDATE partial_candidates
                SET artifact_path=?, quarantine_attempt_id=COALESCE(?, quarantine_attempt_id)
                WHERE id=?
                """,
                (_path_key(artifact_path), quarantine_attempt_id, int(candidate_id)),
            )
            return cursor.rowcount == 1

    def record_cue_recovery(
        self,
        *,
        item_type: str,
        item_id: int,
        target_language: str,
        source_file_hash: str,
        source_cue_number: int,
        source_cue_hash: str,
        source_signature: dict,
        target_text: str,
        target_hash: str,
        recovery_stage: str,
        partial_candidate_id: int | None = None,
        source_language: str | None = None,
        source_attempt_id: int | None = None,
        cue_start_ms: int | None = None,
        cue_end_ms: int | None = None,
    ) -> int:
        with self._transaction() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO cue_recoveries(
                    partial_candidate_id, item_type, item_id, source_language,
                    target_language, source_file_hash, source_cue_number,
                    source_cue_hash, source_signature_json, cue_start_ms,
                    cue_end_ms, target_text, target_hash, validator_fingerprint,
                    config_fingerprint, recovery_stage, source_attempt_id,
                    validation_result, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                         'cue_candidate_valid', ?)
                """,
                (
                    partial_candidate_id, str(item_type), int(item_id), source_language,
                    str(target_language), str(source_file_hash), int(source_cue_number),
                    str(source_cue_hash), json.dumps(source_signature, sort_keys=True),
                    cue_start_ms, cue_end_ms, str(target_text), str(target_hash),
                    self.validator_version, self.config_fingerprint,
                    str(recovery_stage), source_attempt_id, time.time(),
                ),
            )
            row = db.execute(
                """
                SELECT id FROM cue_recoveries
                WHERE item_type=? AND item_id=? AND target_language=?
                  AND source_file_hash=? AND source_cue_hash=? AND target_hash=?
                  AND validator_fingerprint=? AND config_fingerprint=?
                """,
                (
                    str(item_type), int(item_id), str(target_language),
                    str(source_file_hash), str(source_cue_hash), str(target_hash),
                    self.validator_version, self.config_fingerprint,
                ),
            ).fetchone()
            return int(row["id"])

    def cue_recoveries(
        self,
        item_type: str,
        item_id: int,
        target_language: str,
        *,
        source_file_hash: str | None = None,
    ) -> list[dict]:
        query = """
            SELECT * FROM cue_recoveries
            WHERE item_type=? AND item_id=? AND target_language=?
        """
        params: list[object] = [str(item_type), int(item_id), str(target_language)]
        if source_file_hash is not None:
            query += " AND source_file_hash=?"
            params.append(str(source_file_hash))
        query += " ORDER BY created_at DESC, id DESC"
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return [
            {
                "id": int(row["id"]),
                "partialCandidateId": row["partial_candidate_id"],
                "sourceCueNumber": int(row["source_cue_number"]),
                "sourceCueHash": row["source_cue_hash"],
                "sourceSignature": json.loads(row["source_signature_json"]),
                "cueStartMs": row["cue_start_ms"],
                "targetText": row["target_text"],
                "targetHash": row["target_hash"],
                "validatorFingerprint": row["validator_fingerprint"],
                "configFingerprint": row["config_fingerprint"],
                "recoveryStage": row["recovery_stage"],
                "sourceAttemptId": row["source_attempt_id"],
                "createdAt": float(row["created_at"]),
            }
            for row in rows
        ]

    def record_donor_event(
        self,
        *,
        item_type: str,
        item_id: int,
        target_language: str,
        reason_code: str,
        cue_number: int | None = None,
        retry_plan_id: int | None = None,
        donor_attempt_id: int | None = None,
    ) -> None:
        with self._transaction() as db:
            db.execute(
                """
                INSERT INTO donor_events(
                    retry_plan_id, item_type, item_id, target_language,
                    cue_number, donor_attempt_id, reason_code, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    retry_plan_id, str(item_type), int(item_id),
                    str(target_language), cue_number, donor_attempt_id,
                    str(reason_code), time.time(),
                ),
            )

    def recovery_summary(
        self, item_type: str, item_id: int, target_language: str
    ) -> dict:
        with self._lock:
            recovered = self._connection.execute(
                """
                SELECT COUNT(DISTINCT source_cue_number) AS count
                FROM cue_recoveries
                WHERE item_type=? AND item_id=? AND target_language=?
                """,
                (str(item_type), int(item_id), str(target_language)),
            ).fetchone()
            partial = self._connection.execute(
                """
                SELECT unresolved_cues_json, validation_level
                FROM partial_candidates
                WHERE item_type=? AND item_id=? AND target_language=?
                ORDER BY created_at DESC LIMIT 1
                """,
                (str(item_type), int(item_id), str(target_language)),
            ).fetchone()
        unresolved = [] if partial is None else json.loads(partial["unresolved_cues_json"])
        return {
            "validRecoveredCueCount": int(recovered["count"] or 0),
            "unresolvedCueCount": len(unresolved),
            "latestRecoveryStage": (
                partial["validation_level"] if partial is not None else None
            ),
        }

    def recovery_summaries(
        self, identities: list[tuple[str, int, str]]
    ) -> dict[tuple[str, int, str], dict]:
        keys = [(str(kind), int(item_id), str(language)) for kind, item_id, language in identities]
        if not keys:
            return {}
        where = " OR ".join(
            "(item_type=? AND item_id=? AND target_language=?)" for _ in keys
        )
        params = tuple(value for key in keys for value in key)
        with self._lock:
            recovered_rows = self._connection.execute(
                f"""SELECT item_type, item_id, target_language,
                           COUNT(DISTINCT source_cue_number) AS count
                    FROM cue_recoveries WHERE {where}
                    GROUP BY item_type, item_id, target_language""",
                params,
            ).fetchall()
            partial_rows = self._connection.execute(
                f"""SELECT item_type, item_id, target_language,
                           unresolved_cues_json, validation_level, created_at
                    FROM partial_candidates WHERE {where}
                    ORDER BY created_at DESC""",
                params,
            ).fetchall()
        result = {
            key: {
                "validRecoveredCueCount": 0,
                "unresolvedCueCount": 0,
                "latestRecoveryStage": None,
            }
            for key in keys
        }
        for row in recovered_rows:
            key = (row["item_type"], int(row["item_id"]), row["target_language"])
            result[key]["validRecoveredCueCount"] = int(row["count"] or 0)
        seen: set[tuple[str, int, str]] = set()
        for row in partial_rows:
            key = (row["item_type"], int(row["item_id"]), row["target_language"])
            if key in seen:
                continue
            seen.add(key)
            unresolved = json.loads(row["unresolved_cues_json"] or "[]")
            result[key]["unresolvedCueCount"] = len(unresolved)
            result[key]["latestRecoveryStage"] = row["validation_level"]
        return result

    def diagnostic_aggregates(self) -> dict:
        with self._lock:
            donor_rows = self._connection.execute(
                "SELECT reason_code, COUNT(*) AS count FROM donor_events GROUP BY reason_code"
            ).fetchall()
            repair_rows = self._connection.execute(
                "SELECT state, COUNT(*) AS count FROM repair_jobs GROUP BY state"
            ).fetchall()
            provider_rows = self._connection.execute(
                "SELECT classification, COUNT(*) AS count FROM provider_events "
                "GROUP BY classification"
            ).fetchall()
            admission_rows = self._connection.execute(
                "SELECT classification, COUNT(*) AS count FROM retry_admission_events "
                "WHERE completed_cycle=? GROUP BY classification",
                (self._completed_cycle_in(self._connection),),
            ).fetchall()
            maintenance = self._connection.execute(
                "SELECT operation, state, failure_code, started_at, completed_at "
                "FROM maintenance_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return {
            "donors": {row["reason_code"]: int(row["count"]) for row in donor_rows},
            "repairs": {row["state"]: int(row["count"]) for row in repair_rows},
            "providerHealth": {
                row["classification"]: int(row["count"]) for row in provider_rows
            },
            "retryAdmissions": {
                row["classification"]: int(row["count"]) for row in admission_rows
            },
            "maintenance": (
                None
                if maintenance is None
                else {
                    "operation": maintenance["operation"],
                    "state": maintenance["state"],
                    "failureCode": maintenance["failure_code"],
                    "startedAt": maintenance["started_at"],
                    "completedAt": maintenance["completed_at"],
                    "retryNeeded": maintenance["state"] == "failed",
                }
            ),
        }
