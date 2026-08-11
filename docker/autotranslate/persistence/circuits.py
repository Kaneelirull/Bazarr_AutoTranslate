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

class CircuitsRepositoryMixin:
    def circuit_permission(
        self,
        *,
        series_key: str,
        series_title: str,
        config_fingerprint: str,
        claim: bool = True,
        trial_owner: str | None = None,
        completed_cycle: int | None = None,
        now: float | None = None,
    ) -> dict:
        now = time.time() if now is None else now
        with self._transaction() as connection:
            current_cycle = (
                self._completed_cycle_in(connection)
                if completed_cycle is None
                else max(0, int(completed_cycle))
            )
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
                        retry_at=NULL, eligible_after_cycle=NULL, half_open_claimed=0,
                        trial_owner=NULL, trial_claimed_cycle=NULL,
                        trial_claimed_at=NULL, trial_job_id=NULL,
                        trial_lease_state=NULL, trial_plan_id=NULL,
                        lease_expires_at=NULL,
                        config_fingerprint=excluded.config_fingerprint,
                        last_reason=NULL, updated_at=excluded.updated_at
                    """,
                    (series_key, series_title, config_fingerprint, now),
                )
                return {"allowed": True, "state": "closed", "failures": 0}
            if series_title and series_title != row["series_title"]:
                connection.execute(
                    "UPDATE circuit_breakers SET series_title=?, updated_at=? "
                    "WHERE series_key=?",
                    (series_title, now, series_key),
                )
            state = row["state"]
            eligible = row["eligible_after_cycle"]
            remaining = max(0, int(eligible or 0) - current_cycle)
            if (
                state == "open"
                and eligible is not None
                and current_cycle >= int(eligible)
            ):
                if not claim:
                    return {
                        "allowed": True,
                        "state": "eligible",
                        "failures": row["consecutive_failures"],
                        "eligibleAfterCycle": int(eligible),
                        "completedCyclesRemaining": 0,
                    }
                owner = str(trial_owner or f"series:{series_key}:{now}")
                claimed = connection.execute(
                    """
                    UPDATE circuit_breakers
                    SET state='half_open', half_open_claimed=1,
                        trial_owner=?, trial_claimed_cycle=?,
                        trial_claimed_at=?, trial_job_id=NULL,
                        trial_lease_state='claimed', trial_plan_id=NULL,
                        lease_expires_at=?,
                        lease_generation=lease_generation+1, updated_at=?
                    WHERE series_key=? AND state='open' AND half_open_claimed=0
                    """,
                    (
                        owner,
                        current_cycle,
                        now,
                        now + 10800,
                        now,
                        series_key,
                    ),
                ).rowcount
                return {
                    "allowed": bool(claimed),
                    "state": "half_open" if claimed else "open",
                    "failures": row["consecutive_failures"],
                    "eligibleAfterCycle": int(eligible),
                    "completedCyclesRemaining": 0,
                    "trialOwner": owner if claimed else None,
                    "leaseGeneration": (
                        int(row["lease_generation"] or 0) + 1 if claimed else None
                    ),
                }
            allowed = state == "closed"
            return {
                "allowed": allowed,
                "state": state,
                "failures": row["consecutive_failures"],
                "eligibleAfterCycle": eligible,
                "completedCyclesRemaining": remaining,
                "reason": row["last_reason"],
            }

    def reset_circuit(self, series_key: str, reason: str) -> bool:
        with self._transaction() as db:
            cursor = db.execute(
                """
                UPDATE circuit_breakers SET
                    consecutive_failures=0, state='closed', opened_at=NULL,
                    retry_at=NULL, eligible_after_cycle=NULL,
                    half_open_claimed=0, trial_owner=NULL,
                    trial_claimed_cycle=NULL, trial_claimed_at=NULL,
                    trial_job_id=NULL, trial_lease_state=NULL,
                    trial_plan_id=NULL, lease_expires_at=NULL,
                    last_reason=?, updated_at=?
                WHERE series_key=?
                """,
                (str(reason)[:500], time.time(), str(series_key)),
            )
            return cursor.rowcount == 1

    def bind_circuit_trial_job(
        self,
        series_key: str,
        trial_owner: str,
        job_id: int,
        trial_plan_id: int | None = None,
        lease_generation: int | None = None,
    ) -> bool:
        with self._transaction() as db:
            cursor = db.execute(
                """
                UPDATE circuit_breakers
                SET trial_job_id=?, trial_plan_id=?, trial_lease_state='bound',
                    updated_at=?
                 WHERE series_key=? AND state='half_open' AND trial_owner=?
                   AND trial_job_id IS NULL
                   AND (? IS NULL OR lease_generation=?)
                """,
                (
                    int(job_id), trial_plan_id, time.time(), series_key,
                    str(trial_owner), lease_generation, lease_generation,
                ),
            )
            return cursor.rowcount == 1

    def mark_circuit_trial_validation_pending(
        self, series_key: str, trial_owner: str | None
    ) -> bool:
        with self._transaction() as db:
            cursor = db.execute(
                """
                UPDATE circuit_breakers
                SET trial_lease_state='validation_pending', updated_at=?
                WHERE series_key=? AND state='half_open'
                  AND (? IS NULL OR trial_owner=?)
                """,
                (time.time(), series_key, trial_owner, trial_owner),
            )
            return cursor.rowcount == 1

    def release_circuit_trial(
        self, series_key: str, trial_owner: str | None, reason: str
    ) -> bool:
        with self._transaction() as db:
            cursor = db.execute(
                """
                UPDATE circuit_breakers
                SET state='open', half_open_claimed=0, trial_owner=NULL,
                    trial_claimed_cycle=NULL, trial_claimed_at=NULL,
                    trial_job_id=NULL, trial_lease_state=NULL,
                    trial_plan_id=NULL, lease_expires_at=NULL,
                    last_reason=?, updated_at=?
                WHERE series_key=? AND state='half_open'
                  AND (? IS NULL OR trial_owner=?)
                """,
                (
                    str(reason)[:500],
                    time.time(),
                    series_key,
                    trial_owner,
                    trial_owner,
                ),
            )
            return cursor.rowcount == 1

    def recover_abandoned_circuit_trials(
        self, *, max_age_seconds: float, now: float | None = None
    ) -> int:
        timestamp = time.time() if now is None else float(now)
        cutoff = timestamp - max(0.0, float(max_age_seconds))
        with self._transaction() as db:
            cursor = db.execute(
                """
                UPDATE circuit_breakers
                SET state='open', half_open_claimed=0, trial_owner=NULL,
                    trial_claimed_cycle=NULL, trial_claimed_at=NULL,
                    trial_job_id=NULL, trial_lease_state=NULL,
                    trial_plan_id=NULL, lease_expires_at=NULL,
                    last_reason='recovered abandoned half-open trial',
                    updated_at=?
                WHERE state='half_open'
                  AND trial_job_id IS NULL
                  AND (
                      (lease_expires_at IS NOT NULL AND lease_expires_at <= ?)
                      OR (lease_expires_at IS NULL AND
                          (trial_claimed_at IS NULL OR trial_claimed_at <= ?))
                  )
                """,
                (timestamp, timestamp, cutoff),
            )
            return max(0, int(cursor.rowcount))

    def circuit_trial_leases(self) -> list[dict]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT series_key, series_title, trial_owner, trial_job_id,
                       trial_claimed_at, trial_lease_state, trial_plan_id,
                       lease_expires_at, lease_generation
                FROM circuit_breakers
                WHERE state='half_open'
                """
            ).fetchall()
        return [
            {
                "seriesKey": row["series_key"],
                "seriesTitle": row["series_title"],
                "trialOwner": row["trial_owner"],
                "trialJobId": row["trial_job_id"],
                "trialClaimedAt": row["trial_claimed_at"],
                "trialLeaseState": row["trial_lease_state"],
                "trialPlanId": row["trial_plan_id"],
                "leaseExpiresAt": row["lease_expires_at"],
                "leaseGeneration": int(row["lease_generation"] or 0),
            }
            for row in rows
        ]

    def record_circuit_outcome(
        self,
        *,
        series_key: str,
        series_title: str,
        success: bool,
        reason: str | None,
        threshold: int,
        open_cycles: int,
        config_fingerprint: str,
        trial_owner: str | None = None,
        trial_job_id: int | None = None,
        trial_plan_id: int | None = None,
        lease_generation: int | None = None,
        now: float | None = None,
    ) -> dict:
        now = time.time() if now is None else now
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM circuit_breakers WHERE series_key=?",
                (series_key,),
            ).fetchone()
            if row is not None and row["state"] == "half_open":
                bound = (
                    trial_owner is not None
                    and row["trial_owner"] == str(trial_owner)
                    and trial_job_id is not None
                    and row["trial_job_id"] == int(trial_job_id)
                    and trial_plan_id is not None
                    and row["trial_plan_id"] == int(trial_plan_id)
                    and lease_generation is not None
                    and int(row["lease_generation"] or 0) == int(lease_generation)
                )
                if not bound:
                    return {
                        "state": "half_open",
                        "failures": int(row["consecutive_failures"] or 0),
                        "eligibleAfterCycle": row["eligible_after_cycle"],
                        "completedCyclesRemaining": 0,
                        "reason": "ignored outcome from unbound circuit trial",
                        "ignored": True,
                    }
            failures = 0 if success else int(row["consecutive_failures"] if row else 0) + 1
            state = "closed"
            opened_at = None
            eligible_after_cycle = None
            if not success and failures >= max(1, threshold):
                state = "open"
                opened_at = now
                current_cycle = self._completed_cycle_in(connection)
                eligible_after_cycle = current_cycle + max(1, int(open_cycles))
            connection.execute(
                """
                INSERT INTO circuit_breakers(
                    series_key, series_title, consecutive_failures, state,
                    opened_at, retry_at, eligible_after_cycle, half_open_claimed, config_fingerprint,
                    last_reason, updated_at
                ) VALUES (?, ?, ?, ?, ?, NULL, ?, 0, ?, ?, ?)
                ON CONFLICT(series_key) DO UPDATE SET
                    series_title=excluded.series_title,
                    consecutive_failures=excluded.consecutive_failures,
                    state=excluded.state, opened_at=excluded.opened_at,
                    retry_at=NULL, eligible_after_cycle=excluded.eligible_after_cycle,
                    half_open_claimed=0,
                    trial_owner=NULL, trial_claimed_cycle=NULL,
                    trial_claimed_at=NULL, trial_job_id=NULL,
                    trial_lease_state=NULL, trial_plan_id=NULL,
                    lease_expires_at=NULL,
                    config_fingerprint=excluded.config_fingerprint,
                    last_reason=excluded.last_reason, updated_at=excluded.updated_at
                """,
                (
                    series_key,
                    series_title,
                    failures,
                    state,
                    opened_at,
                    eligible_after_cycle,
                    config_fingerprint,
                    None if success else reason,
                    now,
                ),
            )
        return {
            "state": state,
            "failures": failures,
            "eligibleAfterCycle": eligible_after_cycle,
            "completedCyclesRemaining": max(
                0, int(eligible_after_cycle or 0) - self.completed_cycle()
            ),
            "reason": None if success else reason,
        }

    def circuit_breakers(self) -> list[dict]:
        with self._lock:
            completed_cycle = self._completed_cycle_in(self._connection)
            rows = self._connection.execute(
                """
                SELECT series_key, series_title, consecutive_failures, state,
                       eligible_after_cycle, last_reason, trial_job_id,
                       trial_claimed_at, lease_expires_at
                FROM circuit_breakers
                WHERE state IN ('open', 'half_open')
                  AND trim(series_title) != ''
                  AND lower(trim(series_title)) NOT GLOB 'season [0-9]*'
                ORDER BY updated_at DESC
                """
            ).fetchall()
        return [
            {
                "seriesKey": row["series_key"],
                "seriesTitle": row["series_title"],
                "failures": row["consecutive_failures"],
                "state": (
                    "eligible"
                    if row["state"] == "open"
                    and max(
                        0,
                        int(row["eligible_after_cycle"] or 0) - completed_cycle,
                    ) == 0
                    else row["state"]
                ),
                "eligibleAfterCycle": row["eligible_after_cycle"],
                "completedCyclesRemaining": max(
                    0,
                    int(row["eligible_after_cycle"] or 0)
                    - completed_cycle,
                ),
                "reason": row["last_reason"],
                "trialJobId": row["trial_job_id"],
                "trialReady": bool(
                    row["state"] == "open"
                    and max(
                        0,
                        int(row["eligible_after_cycle"] or 0) - completed_cycle,
                    ) == 0
                ),
                "leaseAgeSeconds": (
                    max(0, time.time() - float(row["trial_claimed_at"]))
                    if row["trial_claimed_at"] is not None else None
                ),
                "leaseExpiresAt": row["lease_expires_at"],
            }
            for row in rows
        ]
