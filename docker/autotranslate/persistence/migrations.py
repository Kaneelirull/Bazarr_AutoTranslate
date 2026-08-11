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

LATEST_SCHEMA_VERSION = SCHEMA_VERSION

class MigrationsRepositoryMixin:
    def _migrate_schema(self) -> None:
        # ``Connection.executescript`` commits implicitly, which used to leave
        # partially upgraded databases when a later ALTER or ledger write
        # failed. Execute complete statements individually inside the same
        # immediate transaction so schema and migration ledger advance together.
        with self._transaction() as db:
            self._execute_migration_script(
                db,
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

                CREATE TABLE IF NOT EXISTS source_readiness (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    media_identity TEXT NOT NULL,
                    video_path TEXT,
                    source_path TEXT NOT NULL,
                    source_language TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    media_duration_seconds REAL,
                    validator_fingerprint TEXT NOT NULL,
                    config_fingerprint TEXT NOT NULL,
                    target_artifact_id INTEGER
                        REFERENCES subtitle_artifacts(id) ON DELETE SET NULL,
                    target_language TEXT NOT NULL,
                    trusted_at REAL NOT NULL,
                    UNIQUE(
                        media_identity, source_language, source_hash,
                        validator_fingerprint, config_fingerprint
                    )
                );
                CREATE INDEX IF NOT EXISTS idx_source_readiness_lookup
                    ON source_readiness(
                        media_identity, source_language, source_hash,
                        validator_fingerprint, config_fingerprint
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
                    eligible_after_cycle INTEGER,
                    half_open_claimed INTEGER NOT NULL DEFAULT 0,
                    trial_owner TEXT,
                    trial_claimed_cycle INTEGER,
                    trial_claimed_at REAL,
                    trial_job_id INTEGER,
                    trial_lease_state TEXT,
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
                    canonical_series_key TEXT,
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
                    last_admitted_cycle INTEGER,
                    admission_count INTEGER NOT NULL DEFAULT 0,
                    no_progress_count INTEGER NOT NULL DEFAULT 0,
                    last_deferral_class TEXT,
                    claim_owner TEXT,
                    claimed_at REAL,
                    submission_attempt_id INTEGER,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(item_type, item_id, target_language, source_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_retry_plans_due
                    ON retry_plans(state, eligible_completed_cycle, first_failure_at);
                CREATE INDEX IF NOT EXISTS idx_retry_plans_identity
                    ON retry_plans(item_type, item_id, target_language, updated_at DESC);

                CREATE TABLE IF NOT EXISTS series_identity_aliases (
                    alias_key TEXT PRIMARY KEY,
                    canonical_key TEXT NOT NULL,
                    series_title TEXT,
                    updated_at REAL NOT NULL
                );

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

                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS maintenance_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation TEXT NOT NULL,
                    due_reason TEXT,
                    completed_cycle INTEGER,
                    state TEXT NOT NULL,
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    failure_code TEXT,
                    started_at REAL NOT NULL,
                    completed_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_maintenance_runs_recent
                    ON maintenance_runs(operation, started_at DESC);

                CREATE TABLE IF NOT EXISTS repair_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dedupe_key TEXT NOT NULL,
                    item_type TEXT,
                    item_id INTEGER,
                    target_language TEXT NOT NULL,
                    source_path TEXT,
                    target_path TEXT,
                    source_hash TEXT,
                    target_hash TEXT,
                    cue_indexes_json TEXT NOT NULL DEFAULT '[]',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    state TEXT NOT NULL,
                    lease_owner TEXT,
                    lease_expires_at REAL,
                    shutdown_classification TEXT,
                    last_error_code TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_repair_jobs_active_dedupe
                    ON repair_jobs(dedupe_key)
                    WHERE state IN ('queued', 'active', 'persisted_for_restart');

                CREATE TABLE IF NOT EXISTS partial_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    retry_plan_id INTEGER REFERENCES retry_plans(id),
                    quarantine_attempt_id INTEGER REFERENCES quarantine_attempts(id),
                    item_type TEXT NOT NULL,
                    item_id INTEGER NOT NULL,
                    source_language TEXT,
                    target_language TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    target_hash TEXT NOT NULL,
                    artifact_path TEXT,
                    validation_level TEXT NOT NULL,
                    validator_fingerprint TEXT NOT NULL,
                    config_fingerprint TEXT NOT NULL,
                    changed_cues_json TEXT NOT NULL,
                    unresolved_cues_json TEXT NOT NULL,
                    provenance_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cue_recoveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    partial_candidate_id INTEGER REFERENCES partial_candidates(id),
                    item_type TEXT NOT NULL,
                    item_id INTEGER NOT NULL,
                    source_language TEXT,
                    target_language TEXT NOT NULL,
                    source_file_hash TEXT NOT NULL,
                    source_cue_number INTEGER NOT NULL,
                    source_cue_hash TEXT NOT NULL,
                    source_signature_json TEXT NOT NULL,
                    cue_start_ms INTEGER,
                    cue_end_ms INTEGER,
                    target_text TEXT NOT NULL,
                    target_hash TEXT NOT NULL,
                    validator_fingerprint TEXT NOT NULL,
                    config_fingerprint TEXT NOT NULL,
                    recovery_stage TEXT NOT NULL,
                    source_attempt_id INTEGER REFERENCES quarantine_attempts(id),
                    validation_result TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(item_type, item_id, target_language, source_file_hash,
                           source_cue_hash, target_hash, validator_fingerprint,
                           config_fingerprint)
                );
                CREATE INDEX IF NOT EXISTS idx_cue_recoveries_lookup
                    ON cue_recoveries(item_type, item_id, target_language,
                                     source_file_hash, source_cue_number, created_at DESC);

                CREATE TABLE IF NOT EXISTS recovery_stage_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    retry_plan_id INTEGER REFERENCES retry_plans(id),
                    repair_job_id INTEGER REFERENCES repair_jobs(id),
                    cue_recovery_id INTEGER REFERENCES cue_recoveries(id),
                    cue_number INTEGER,
                    stage TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    reason_code TEXT,
                    strategy_key TEXT,
                    output_fingerprint TEXT,
                    provider TEXT,
                    model TEXT,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS donor_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    retry_plan_id INTEGER REFERENCES retry_plans(id),
                    item_type TEXT NOT NULL,
                    item_id INTEGER NOT NULL,
                    target_language TEXT NOT NULL,
                    cue_number INTEGER,
                    donor_attempt_id INTEGER REFERENCES quarantine_attempts(id),
                    reason_code TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_donor_events_recent
                    ON donor_events(reason_code, created_at DESC);

                CREATE TABLE IF NOT EXISTS failure_fingerprints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_type TEXT NOT NULL,
                    item_id INTEGER NOT NULL,
                    target_language TEXT NOT NULL,
                    source_file_hash TEXT NOT NULL,
                    source_cue_hash TEXT NOT NULL,
                    strategy_key TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT,
                    config_fingerprint TEXT NOT NULL,
                    output_fingerprint TEXT NOT NULL,
                    failure_class TEXT NOT NULL,
                    occurrences INTEGER NOT NULL DEFAULT 1,
                    first_seen_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    UNIQUE(item_type, item_id, target_language, source_file_hash,
                           source_cue_hash, strategy_key, provider,
                           config_fingerprint, output_fingerprint)
                );

                CREATE TABLE IF NOT EXISTS retry_admission_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    retry_plan_id INTEGER NOT NULL REFERENCES retry_plans(id),
                    completed_cycle INTEGER NOT NULL,
                    classification TEXT NOT NULL,
                    reason_code TEXT,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS provider_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    classification TEXT NOT NULL,
                    http_status INTEGER,
                    retryable INTEGER NOT NULL DEFAULT 0,
                    response_shape_json TEXT NOT NULL DEFAULT '{}',
                    model TEXT,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_provider_events_recent
                    ON provider_events(provider, classification, created_at DESC);

                CREATE TABLE IF NOT EXISTS manual_review_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    retry_plan_id INTEGER NOT NULL REFERENCES retry_plans(id),
                    action TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    reason_code TEXT,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_manual_review_actions_plan_time
                    ON manual_review_actions(retry_plan_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS manual_review_scan_outbox (
                    retry_plan_id INTEGER PRIMARY KEY REFERENCES retry_plans(id),
                    state TEXT NOT NULL DEFAULT 'pending',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL,
                    lease_owner TEXT,
                    lease_expires_at REAL,
                    last_error_code TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_manual_review_scan_due
                    ON manual_review_scan_outbox(
                        state, attempt_count, next_attempt_at, retry_plan_id
                    );
                """
            )
            self._migration_checkpoint("schema_objects")
            db.execute("DROP TABLE IF EXISTS legacy_quarantine_index")
            self._migration_checkpoint("legacy_state_removed")
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
            self._migration_checkpoint("quarantine_columns")
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
            retry_additions = {
                "canonical_series_key": "TEXT",
                "last_admitted_cycle": "INTEGER",
                "admission_count": "INTEGER NOT NULL DEFAULT 0",
                "no_progress_count": "INTEGER NOT NULL DEFAULT 0",
                "last_deferral_class": "TEXT",
                "claim_owner": "TEXT",
                "claimed_at": "REAL",
                "submission_attempt_id": "INTEGER",
            }
            for name, definition in retry_additions.items():
                if name not in retry_columns:
                    self._connection.execute(
                        f"ALTER TABLE retry_plans ADD COLUMN {name} {definition}"
                    )
            self._migration_checkpoint("retry_columns")
            self._connection.execute(
                """
                UPDATE retry_plans
                SET state='regeneration_waiting',
                    last_deferral_class='manual_review',
                    final_outcome=NULL,
                    claim_owner=NULL,
                    claimed_at=NULL,
                    submission_attempt_id=NULL,
                    updated_at=MAX(updated_at, ?)
                WHERE state='retry_exhausted'
                  AND final_outcome='manual_review'
                """,
                (time.time(),),
            )
            outbox_now = time.time()
            self._connection.execute(
                """
                INSERT OR IGNORE INTO manual_review_scan_outbox(
                    retry_plan_id, state, attempt_count, next_attempt_at,
                    lease_owner, lease_expires_at, last_error_code,
                    created_at, updated_at
                )
                SELECT plan.id, 'pending',
                       (SELECT COUNT(*) FROM manual_review_actions failed
                        WHERE failed.retry_plan_id=plan.id
                          AND failed.action='bazarr_scan'
                          AND failed.outcome='failed'),
                       ?, NULL, NULL, NULL, ?, ?
                FROM retry_plans plan
                WHERE plan.state='accepted_after_manual_recheck'
                  AND EXISTS (
                      SELECT 1 FROM manual_review_actions pending
                      WHERE pending.retry_plan_id=plan.id
                        AND pending.action='bazarr_scan'
                        AND pending.outcome='pending'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM manual_review_actions sent
                      WHERE sent.retry_plan_id=plan.id
                        AND sent.action='bazarr_scan'
                        AND sent.outcome='dispatched'
                  )
                """,
                (outbox_now, outbox_now, outbox_now),
            )
            self._migration_checkpoint("manual_review_normalization")
            circuit_columns = {
                row["name"]
                for row in self._connection.execute(
                    "PRAGMA table_info(circuit_breakers)"
                ).fetchall()
            }
            if "eligible_after_cycle" not in circuit_columns:
                self._connection.execute(
                    "ALTER TABLE circuit_breakers "
                    "ADD COLUMN eligible_after_cycle INTEGER"
                )
            circuit_additions = {
                "trial_owner": "TEXT",
                "trial_claimed_cycle": "INTEGER",
                "trial_claimed_at": "REAL",
                "trial_job_id": "INTEGER",
                "trial_lease_state": "TEXT",
                "trial_plan_id": "INTEGER",
                "lease_expires_at": "REAL",
                "lease_generation": "INTEGER NOT NULL DEFAULT 0",
            }
            for name, definition in circuit_additions.items():
                if name not in circuit_columns:
                    self._connection.execute(
                        f"ALTER TABLE circuit_breakers ADD COLUMN {name} {definition}"
                    )
            self._migration_checkpoint("circuit_columns")
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
            self._migration_checkpoint("attempt_columns")
            migration_names = {
                9: "maintenance history and migration ledger",
                10: "durable repairs and cue recovery",
                11: "legacy quarantine index and donor diagnostics",
                12: "failure, admission, and provider diagnostics",
                13: "circuit trial lease ownership",
                14: "successful translation source readiness",
                15: "manual review actions and state normalization",
                16: "atomic manual recovery and fair scan outbox",
            }
            timestamp = time.time()
            for version, name in migration_names.items():
                self._connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, name, applied_at) "
                    "VALUES(?, ?, ?)",
                    (version, name, timestamp),
                )
            self._migration_checkpoint("migration_ledger")
            self._connection.execute(
                "INSERT OR REPLACE INTO state_metadata(key, value) "
                "VALUES('app_schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._migration_checkpoint("schema_markers")

    def _migration_checkpoint(self, _name: str) -> None:
        """Test seam for proving transaction rollback at migration boundaries."""
        return None

    @staticmethod
    def _execute_migration_script(
        connection: sqlite3.Connection, script: str
    ) -> None:
        statement = ""
        for line in script.splitlines():
            statement += line + "\n"
            if not sqlite3.complete_statement(statement):
                continue
            sql = statement.strip()
            statement = ""
            if sql:
                connection.execute(sql)
        if statement.strip():
            raise StateStoreError("incomplete SQL statement in schema migration")

    def _verify(self) -> None:
        result = self._connection.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            raise StateStoreError(
                f"SQLite quick_check failed for {self.path}: {result}"
            )
        foreign_key_errors = self._connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        if foreign_key_errors:
            raise StateStoreError(
                f"SQLite foreign_key_check failed for {self.path}: "
                f"{len(foreign_key_errors)} violation(s)"
            )
