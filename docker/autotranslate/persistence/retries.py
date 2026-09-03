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

class RetriesRepositoryMixin:
    def completed_cycle(self) -> int:
        value = self._metadata("completed_cycle")
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _completed_cycle_in(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT value FROM state_metadata WHERE key = 'completed_cycle'"
        ).fetchone()
        try:
            return max(0, int(row["value"])) if row else 0
        except (TypeError, ValueError):
            return 0

    def initialize_cycle_circuits(self, open_cycles: int) -> dict:
        """Migrate legacy time-based circuits and retire unsafe generic keys."""
        with self._transaction() as db:
            completed = self._completed_cycle_in(db)
            retired = db.execute(
                """
                UPDATE circuit_breakers
                SET state='closed', consecutive_failures=0, opened_at=NULL,
                    retry_at=NULL, eligible_after_cycle=NULL,
                    half_open_claimed=0, last_reason='retired generic series identity',
                    updated_at=?
                WHERE lower(trim(series_title)) GLOB 'season [0-9]*'
                   OR lower(series_key) GLOB 'episodes:season [0-9]*'
                """,
                (time.time(),),
            ).rowcount
            eligible = completed + max(1, int(open_cycles))
            migrated = db.execute(
                """
                UPDATE circuit_breakers
                SET eligible_after_cycle=?, retry_at=NULL, updated_at=?
                WHERE state IN ('open', 'half_open')
                  AND eligible_after_cycle IS NULL
                """,
                (eligible, time.time()),
            ).rowcount
        return {
            "completedCycle": completed,
            "migrated": migrated,
            "retiredGeneric": retired,
        }

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
            "canonicalSeriesKey": row["canonical_series_key"],
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
            "lastAdmittedCycle": row["last_admitted_cycle"],
            "admissionCount": int(row["admission_count"] or 0),
            "noProgressCount": int(row["no_progress_count"] or 0),
            "lastDeferralClass": row["last_deferral_class"],
            "claimOwner": row["claim_owner"],
            "claimedAt": row["claimed_at"],
            "submissionAttemptId": row["submission_attempt_id"],
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
                        end_cycle_repair_attempted = CASE
                            WHEN NOT ? AND ? = 'repair_retry_queued' THEN 0
                            ELSE end_cycle_repair_attempted END,
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
                        repeated, state,
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

    def recover_stale_end_cycle_repair_attempts(self) -> int:
        """Re-enable repair rows left queued behind a completed attempt marker."""
        with self._transaction() as db:
            protected_plan_ids: set[int] = set()
            durable_repairs = db.execute(
                """
                SELECT payload_json FROM repair_jobs
                WHERE state IN ('queued', 'active', 'persisted_for_restart')
                """
            ).fetchall()
            for repair in durable_repairs:
                try:
                    retry_plan_id = json.loads(
                        repair["payload_json"] or "{}"
                    ).get("retryPlanId")
                    if retry_plan_id is not None:
                        protected_plan_ids.add(int(retry_plan_id))
                except (AttributeError, TypeError, ValueError):
                    continue
            protection = ""
            parameters: list[object] = [time.time()]
            if protected_plan_ids:
                protection = " AND id NOT IN ({})".format(
                    ",".join("?" for _value in protected_plan_ids)
                )
                parameters.extend(sorted(protected_plan_ids))
            cursor = db.execute(
                f"""
                UPDATE retry_plans
                SET end_cycle_repair_attempted=0, updated_at=?
                WHERE state='repair_retry_queued'
                  AND end_cycle_repair_attempted=1
                  {protection}
                """,
                parameters,
            )
            return max(0, int(cursor.rowcount))

    def register_series_alias(
        self, alias_key: str, canonical_key: str, series_title: str | None = None
    ) -> int:
        alias = str(alias_key or "").strip()
        canonical = str(canonical_key or "").strip()
        if not alias or not canonical:
            return 0
        now = time.time()
        with self._transaction() as db:
            db.execute(
                """
                INSERT INTO series_identity_aliases(
                    alias_key, canonical_key, series_title, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(alias_key) DO UPDATE SET
                    canonical_key=excluded.canonical_key,
                    series_title=COALESCE(excluded.series_title, series_title),
                    updated_at=excluded.updated_at
                """,
                (alias, canonical, series_title, now),
            )
            updated = db.execute(
                """
                UPDATE retry_plans
                SET canonical_series_key=?, series_title=COALESCE(?, series_title),
                    updated_at=?
                WHERE series_key=? OR canonical_series_key=?
                """,
                (canonical, series_title, now, alias, alias),
            ).rowcount
            alias_circuit = db.execute(
                "SELECT * FROM circuit_breakers WHERE series_key=?", (alias,)
            ).fetchone()
            canonical_circuit = db.execute(
                "SELECT * FROM circuit_breakers WHERE series_key=?", (canonical,)
            ).fetchone()
            if alias != canonical and alias_circuit is not None:
                if canonical_circuit is None:
                    db.execute(
                        """
                        UPDATE circuit_breakers
                        SET series_key=?, series_title=COALESCE(?, series_title),
                            updated_at=?
                        WHERE series_key=?
                        """,
                        (canonical, series_title, now, alias),
                    )
                else:
                    active_rows = [
                        row for row in (alias_circuit, canonical_circuit)
                        if row["state"] in ("open", "half_open")
                    ]
                    bound_rows = [
                        row for row in active_rows
                        if row["state"] == "half_open"
                        and row["trial_job_id"] is not None
                    ]
                    if len(bound_rows) > 1:
                        return max(0, int(updated))
                    bound = bound_rows[0] if bound_rows else None
                    merged_state = (
                        "half_open"
                        if bound is not None
                        else "open"
                        if active_rows
                        else "closed"
                    )
                    eligible_values = [
                        int(row["eligible_after_cycle"])
                        for row in active_rows
                        if row["eligible_after_cycle"] is not None
                    ]
                    db.execute(
                        """
                    UPDATE circuit_breakers SET
                            series_title=COALESCE(?, series_title),
                            consecutive_failures=?,
                            state=?, eligible_after_cycle=?,
                            half_open_claimed=?, trial_owner=?,
                            trial_claimed_cycle=?, trial_claimed_at=?,
                            trial_job_id=?, trial_lease_state=?,
                            last_reason=COALESCE(?, last_reason),
                            updated_at=?
                        WHERE series_key=?
                        """,
                        (
                            series_title,
                            max(
                                int(alias_circuit["consecutive_failures"] or 0),
                                int(canonical_circuit["consecutive_failures"] or 0),
                            ),
                            merged_state,
                            (
                                max(eligible_values)
                                if eligible_values
                                else self._completed_cycle_in(db)
                                if active_rows
                                else None
                            ),
                            1 if bound is not None else 0,
                            bound["trial_owner"] if bound is not None else None,
                            (
                                bound["trial_claimed_cycle"]
                                if bound is not None else None
                            ),
                            (
                                bound["trial_claimed_at"]
                                if bound is not None else None
                            ),
                            bound["trial_job_id"] if bound is not None else None,
                            (
                                bound["trial_lease_state"]
                                if bound is not None else None
                            ),
                            alias_circuit["last_reason"],
                            now,
                            canonical,
                        ),
                    )
                    db.execute(
                        """
                        UPDATE circuit_breakers SET state='closed',
                            half_open_claimed=0, trial_owner=NULL,
                            trial_claimed_cycle=NULL, trial_claimed_at=NULL,
                        trial_job_id=NULL, trial_lease_state=NULL,
                        trial_plan_id=NULL, lease_expires_at=NULL,
                            last_reason=?, updated_at=?
                        WHERE series_key=?
                        """,
                        (f"canonical alias: {canonical}", now, alias),
                    )
        return max(0, int(updated))

    def _canonical_series_key_in(
        self, db: sqlite3.Connection, series_key: str | None, item_type: str, item_id: int
    ) -> str:
        current = str(series_key or f"{item_type}:{item_id}")
        seen: set[str] = set()
        for _ in range(8):
            if current in seen:
                break
            seen.add(current)
            row = db.execute(
                "SELECT canonical_key FROM series_identity_aliases WHERE alias_key=?",
                (current,),
            ).fetchone()
            if row is None or not row["canonical_key"]:
                break
            current = str(row["canonical_key"])
        return current

    def claim_due_retry_plans(
        self,
        completed_cycle: int,
        *,
        limit: int,
        per_series_limit: int = 1,
        excluded_plan_ids: Iterable[int] = (),
        series_admissions: dict[str, int] | None = None,
    ) -> list[dict]:
        selected: list[tuple[sqlite3.Row, str]] = []
        excluded_ids = {int(plan_id) for plan_id in excluded_plan_ids}
        series_counts = {
            str(key): max(0, int(value))
            for key, value in (series_admissions or {}).items()
        }
        with self._transaction() as db:
            rows = db.execute(
                """
                SELECT * FROM retry_plans
                WHERE state = 'regeneration_waiting'
                  AND eligible_completed_cycle <= ?
                  AND COALESCE(last_deferral_class, '') <> 'manual_review'
                  AND NOT EXISTS (SELECT 1 FROM subtitle_publications publication WHERE publication.target_path=retry_plans.target_path AND publication.state IN ('pending','published','manual_review'))
                ORDER BY CASE WHEN last_admitted_cycle IS NULL THEN 0 ELSE 1 END,
                         last_admitted_cycle,
                         eligible_completed_cycle,
                         CAST(first_failure_at / 86400 AS INTEGER),
                         CASE WHEN source_cue_count IS NULL THEN 1 ELSE 0 END,
                         source_cue_count, attempt_count, first_failure_at,
                         item_type, item_id, target_language
                """,
                (max(0, int(completed_cycle)),),
            ).fetchall()
            for row in rows:
                if int(row["id"]) in excluded_ids:
                    continue
                series_bucket = self._canonical_series_key_in(
                    db,
                    row["canonical_series_key"] or row["series_key"],
                    row["item_type"],
                    row["item_id"],
                )
                if series_counts.get(series_bucket, 0) >= max(1, per_series_limit):
                    continue
                selected.append((row, series_bucket))
                series_counts[series_bucket] = series_counts.get(series_bucket, 0) + 1
                if len(selected) >= max(1, limit):
                    break
            for row, series_bucket in selected:
                owner = (
                    f"cycle:{max(0, int(completed_cycle))}:plan:{int(row['id'])}:"
                    f"admission:{int(row['admission_count'] or 0) + 1}"
                )
                db.execute(
                    """
                    UPDATE retry_plans
                    SET state='regeneration_queued', canonical_series_key=?,
                        last_admitted_cycle=?, admission_count=admission_count+1,
                        claim_owner=?, claimed_at=?, last_deferral_class=NULL,
                        submission_attempt_id=NULL, updated_at=?
                    WHERE id = ? AND state = 'regeneration_waiting'
                    """,
                    (
                        series_bucket,
                        max(0, int(completed_cycle)),
                        owner,
                        time.time(),
                        time.time(),
                        row["id"],
                    ),
                )
            claimed = [
                db.execute(
                    "SELECT * FROM retry_plans WHERE id = ?", (row["id"],)
                ).fetchone()
                for row, _series_bucket in selected
            ]
        return [self._retry_plan_dict(row) for row in claimed if row is not None]

    def due_retry_count(self, completed_cycle: int) -> int:
        row = self._fetchone(
            """
            SELECT COUNT(*) AS count
            FROM retry_plans
            WHERE state = 'regeneration_waiting'
              AND eligible_completed_cycle <= ?
              AND COALESCE(last_deferral_class, '') <> 'manual_review'
                  AND NOT EXISTS (SELECT 1 FROM subtitle_publications publication WHERE publication.target_path=retry_plans.target_path AND publication.state IN ('pending','published','manual_review'))
            """,
            (max(0, int(completed_cycle)),),
        )
        return int(row["count"]) if row else 0

    def recover_retry_claims(self) -> int:
        """Release crash-orphaned claims while moving them behind other due work."""
        with self._transaction() as db:
            completed = self._completed_cycle_in(db)
            cursor = db.execute(
                """
                UPDATE retry_plans
                SET state = 'regeneration_waiting',
                    last_reason = 'recovered after service restart',
                    last_deferral_class='restart_recovery',
                    no_progress_count=no_progress_count+1,
                    eligible_completed_cycle=MAX(eligible_completed_cycle, ?),
                    claim_owner=NULL, claimed_at=NULL,
                    submission_attempt_id=NULL,
                    updated_at = ?
                WHERE state = 'regeneration_queued'
                  AND NOT EXISTS (
                    SELECT 1 FROM translation_attempts attempt
                    WHERE attempt.id=retry_plans.submission_attempt_id
                      AND attempt.status='submitted'
                      AND attempt.lingarr_job_id IS NOT NULL
                  )
                """,
                (completed + 1, time.time()),
            )
            return max(0, int(cursor.rowcount))

    def reactivate_changed_manual_reviews(self, config_fingerprint: str) -> int:
        """Reopen manual reviews when the active recovery configuration changed."""
        changed = 0
        with self._lock:
            holds = self._connection.execute('SELECT * FROM recovery_review_holds').fetchall()
        for hold in holds:
            plan = self.retry_plan(hold['retry_plan_id'])
            if plan and plan['state'] == 'regeneration_waiting' and plan.get('lastDeferralClass') == 'manual_review' and self.recovery_policy_key(plan) != hold['policy_key'] and not self.publication_for_target(plan.get('targetPath')):
                with self._transaction() as db:
                    db.execute("UPDATE retry_plans SET last_deferral_class=NULL, updated_at=? WHERE id=?", (time.time(), plan['id']))
                    db.execute('DELETE FROM recovery_review_holds WHERE retry_plan_id=?', (plan['id'],))
                    changed += 1
        with self._transaction() as db:
            cursor = db.execute(
                """
                UPDATE retry_plans
                SET last_deferral_class=NULL,
                    last_reason='recovery configuration changed', updated_at=?
                WHERE state='regeneration_waiting'
                  AND last_deferral_class='manual_review'
                  AND NOT EXISTS (SELECT 1 FROM recovery_review_holds h WHERE h.retry_plan_id=retry_plans.id)
                  AND NOT EXISTS (SELECT 1 FROM subtitle_publications p WHERE p.target_path=retry_plans.target_path AND p.state IN ('pending','published','manual_review'))
                  AND NOT EXISTS (
                    SELECT 1 FROM failure_fingerprints failure
                    WHERE failure.item_type=retry_plans.item_type
                      AND failure.item_id=retry_plans.item_id
                      AND failure.target_language=retry_plans.target_language
                      AND failure.source_file_hash=retry_plans.source_hash
                      AND failure.config_fingerprint=?
                  )
                """,
                (time.time(), str(config_fingerprint)),
            )
            return changed + max(0, int(cursor.rowcount))

    def retry_claims_with_submissions(self) -> list[dict]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT plan.*,
                    (
                        SELECT attempt.lingarr_job_id
                        FROM translation_attempts attempt
                        WHERE attempt.id=plan.submission_attempt_id
                          AND attempt.status='submitted'
                    ) AS retry_job_id
                FROM retry_plans plan
                WHERE plan.state IN ('regeneration_queued', 'retry_in_progress')
                """
            ).fetchall()
        result = []
        for row in rows:
            plan = self._retry_plan_dict(row)
            plan["lingarrJobId"] = row["retry_job_id"]
            result.append(plan)
        return result

    def bind_retry_submission(
        self, plan_id: int, claim_owner: str, attempt_id: int
    ) -> bool:
        with self._transaction() as db:
            cursor = db.execute(
                """
                UPDATE retry_plans SET submission_attempt_id=?, updated_at=?
                WHERE id=? AND state='regeneration_queued'
                  AND claim_owner=?
                """,
                (
                    int(attempt_id),
                    time.time(),
                    int(plan_id),
                    str(claim_owner),
                ),
            )
            return cursor.rowcount == 1

    def reschedule_retry_no_progress(
        self,
        plan_id: int,
        *,
        completed_cycle: int,
        deferral_class: str,
        reason: str,
        delay_cycles: int = 1,
        lease_generation: int | None = None,
    ) -> dict | None:
        eligible = max(0, int(completed_cycle)) + max(1, int(delay_cycles))
        with self._transaction() as db:
            cursor = db.execute(
                """
                UPDATE retry_plans SET
                    state='regeneration_waiting',
                    eligible_completed_cycle=MAX(eligible_completed_cycle, ?),
                    no_progress_count=no_progress_count+1,
                    last_deferral_class=?, last_reason=?,
                    claim_owner=NULL, claimed_at=NULL,
                    submission_attempt_id=NULL, updated_at=?
                WHERE id=?
                  AND state IN (
                    'repair_retry_queued', 'regeneration_waiting',
                    'regeneration_queued', 'retry_in_progress'
                  )
                  AND (
                    ? IS NULL OR EXISTS (
                      SELECT 1 FROM circuit_breakers circuit
                      WHERE circuit.state='half_open'
                        AND circuit.trial_plan_id=retry_plans.id
                        AND circuit.lease_generation=?
                    )
                  )
                """,
                (
                    eligible,
                    str(deferral_class)[:80],
                    str(reason)[:500],
                    time.time(),
                    int(plan_id),
                    lease_generation,
                    lease_generation,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = db.execute(
                "SELECT * FROM retry_plans WHERE id=?", (int(plan_id),)
            ).fetchone()
        return self._retry_plan_dict(row)

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
                    claim_owner=CASE
                        WHEN ?='regeneration_waiting' THEN NULL
                        ELSE claim_owner END,
                    claimed_at=CASE
                        WHEN ?='regeneration_waiting' THEN NULL
                        ELSE claimed_at END,
                    submission_attempt_id=CASE
                        WHEN ?='regeneration_waiting' THEN NULL
                        ELSE submission_attempt_id END,
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
                    state, state, state, time.time(), int(plan_id),
                ),
            )
            row = db.execute(
                "SELECT * FROM retry_plans WHERE id = ?", (int(plan_id),)
            ).fetchone()
        return self._retry_plan_dict(row)

    def resolve_retry_plan(
        self,
        plan_id: int,
        expected_source_hash: str,
        *,
        outcome: str = "accepted_after_retry",
        lease_generation: int | None = None,
    ) -> bool:
        with self._transaction() as db:
            cursor = db.execute(
                """
                UPDATE retry_plans
                SET state = ?, final_outcome = ?, claim_owner=NULL,
                    claimed_at=NULL, submission_attempt_id=NULL,
                    last_deferral_class=NULL, updated_at = ?
                WHERE id = ? AND source_hash = ?
                  AND state IN (
                    'repair_retry_queued', 'regeneration_waiting',
                    'regeneration_queued', 'retry_in_progress'
                  )
                  AND (
                    ? IS NULL OR EXISTS (
                      SELECT 1 FROM circuit_breakers circuit
                      WHERE circuit.state='half_open'
                        AND circuit.trial_plan_id=retry_plans.id
                        AND circuit.lease_generation=?
                    )
                  )
                """,
                (
                    outcome, outcome, time.time(), int(plan_id),
                    str(expected_source_hash),
                    lease_generation, lease_generation,
                ),
            )
            return cursor.rowcount == 1

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

    def retry_plan(self, plan_id: int) -> dict | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM retry_plans WHERE id=?", (int(plan_id),)
            ).fetchone()
        return self._retry_plan_dict(row)

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

    def record_retry_admission(
        self,
        plan_id: int,
        completed_cycle: int,
        classification: str,
        reason_code: str | None = None,
    ) -> None:
        with self._transaction() as db:
            db.execute(
                """
                INSERT INTO retry_admission_events(
                    retry_plan_id, completed_cycle, classification,
                    reason_code, created_at
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (
                    int(plan_id), max(0, int(completed_cycle)),
                    str(classification), reason_code, time.time(),
                ),
            )

    def record_failure_fingerprint(
        self,
        *,
        item_type: str,
        item_id: int,
        target_language: str,
        source_file_hash: str,
        source_cue_hash: str,
        strategy_key: str,
        provider: str,
        config_fingerprint: str,
        output_fingerprint: str,
        failure_class: str,
        model: str | None = None,
    ) -> int:
        timestamp = time.time()
        with self._transaction() as db:
            db.execute(
                """
                INSERT INTO failure_fingerprints(
                    item_type, item_id, target_language, source_file_hash,
                    source_cue_hash, strategy_key, provider, model,
                    config_fingerprint, output_fingerprint, failure_class,
                    first_seen_at, last_seen_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_type, item_id, target_language,
                            source_file_hash, source_cue_hash, strategy_key,
                            provider, config_fingerprint, output_fingerprint)
                DO UPDATE SET occurrences=occurrences+1,
                              last_seen_at=excluded.last_seen_at,
                              failure_class=excluded.failure_class,
                              model=COALESCE(excluded.model, model)
                """,
                (
                    str(item_type), int(item_id), str(target_language),
                    str(source_file_hash), str(source_cue_hash), str(strategy_key),
                    str(provider), model, str(config_fingerprint),
                    str(output_fingerprint), str(failure_class), timestamp, timestamp,
                ),
            )
            row = db.execute(
                """
                SELECT occurrences FROM failure_fingerprints
                WHERE item_type=? AND item_id=? AND target_language=?
                  AND source_file_hash=? AND source_cue_hash=?
                  AND strategy_key=? AND provider=? AND config_fingerprint=?
                  AND output_fingerprint=?
                """,
                (
                    str(item_type), int(item_id), str(target_language),
                    str(source_file_hash), str(source_cue_hash), str(strategy_key),
                    str(provider), str(config_fingerprint), str(output_fingerprint),
                ),
            ).fetchone()
            return int(row["occurrences"])

    def exhausted_recovery_strategies(
        self,
        *,
        item_type: str,
        item_id: int,
        target_language: str,
        source_file_hash: str,
        provider: str,
        config_fingerprint: str,
        minimum_occurrences: int = 2,
    ) -> dict[str, set[str]]:
        """Return strategies with durable equivalent failures, keyed by cue hash."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT source_cue_hash, strategy_key
                FROM failure_fingerprints
                WHERE item_type=? AND item_id=? AND target_language=?
                  AND source_file_hash=? AND provider=? AND config_fingerprint=?
                GROUP BY source_cue_hash, strategy_key
                HAVING MAX(occurrences) >= ?
                """,
                (
                    str(item_type), int(item_id), str(target_language),
                    str(source_file_hash), str(provider), str(config_fingerprint),
                    max(1, int(minimum_occurrences)),
                ),
            ).fetchall()
        exhausted: dict[str, set[str]] = {}
        for row in rows:
            exhausted.setdefault(str(row["source_cue_hash"]), set()).add(
                str(row["strategy_key"])
            )
        return exhausted
