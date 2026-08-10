import hashlib
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "docker"))

from autotranslate.maintenance.legacy_index import LegacyQuarantineIndexer  # noqa: E402
from state_store import StateStore, StateStoreError  # noqa: E402


class OverhaulReliabilityFixTests(unittest.TestCase):
    def make_store(self, root: Path) -> StateStore:
        return StateStore(root / "state.sqlite3")

    def test_terminal_repair_state_cannot_be_overwritten_by_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(Path(directory))
            try:
                job_id = store.enqueue_repair_job(
                    dedupe_key="repair", target_language="et"
                )
                self.assertTrue(store.transition_repair_job(
                    job_id, "active", expected_states=("queued",)
                ))
                self.assertTrue(store.transition_repair_job(
                    job_id, "completed", expected_states=("active",)
                ))
                self.assertFalse(store.transition_repair_job(
                    job_id, "persisted_for_restart", expected_states=("queued", "active")
                ))
                self.assertEqual(store.repair_jobs_for_restart(), [])
            finally:
                store.close()

    def test_retry_claims_preserve_cycle_wide_series_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(Path(directory))
            try:
                for item_id, series in ((1, "sonarr:1"), (2, "sonarr:1"), (3, "sonarr:2")):
                    store.schedule_retry_plan(
                        item_type="episodes", item_id=item_id,
                        target_language="et", source_hash=f"source-{item_id}",
                        failure_class="validation", rules=["invalid"],
                        state="regeneration_waiting", eligible_completed_cycle=0,
                        series_key=series,
                    )
                first = store.claim_due_retry_plans(0, limit=1, per_series_limit=1)
                key = first[0].get("canonicalSeriesKey") or first[0]["seriesKey"]
                second = store.claim_due_retry_plans(
                    0, limit=2, per_series_limit=1,
                    excluded_plan_ids={first[0]["id"]}, series_admissions={key: 1},
                )
                self.assertEqual(len(second), 1)
                self.assertNotEqual(second[0]["seriesKey"], key)
            finally:
                store.close()

    def test_half_open_circuit_accepts_only_bound_trial_outcome(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(Path(directory))
            try:
                store.record_circuit_outcome(
                    series_key="sonarr:1", series_title="Show", success=False,
                    reason="failure", threshold=1, open_cycles=1,
                    config_fingerprint="config",
                )
                trial = store.circuit_permission(
                    series_key="sonarr:1", series_title="Show",
                    config_fingerprint="config", completed_cycle=1,
                    trial_owner="attempt:7",
                )
                self.assertTrue(store.bind_circuit_trial_job(
                    "sonarr:1", "attempt:7", 99, trial_plan_id=7,
                    lease_generation=trial["leaseGeneration"],
                ))
                ignored = store.record_circuit_outcome(
                    series_key="sonarr:1", series_title="Show", success=True,
                    reason=None, threshold=1, open_cycles=1,
                    config_fingerprint="config", trial_owner="attempt:other",
                    trial_job_id=99, trial_plan_id=7,
                    lease_generation=trial["leaseGeneration"],
                )
                self.assertTrue(ignored["ignored"])
                accepted = store.record_circuit_outcome(
                    series_key="sonarr:1", series_title="Show", success=True,
                    reason=None, threshold=1, open_cycles=1,
                    config_fingerprint="config", trial_owner="attempt:7",
                    trial_job_id=99, trial_plan_id=7,
                    lease_generation=trial["leaseGeneration"],
                )
                self.assertEqual(accepted["state"], "closed")
            finally:
                store.close()

    def test_pending_partial_quarantine_resumes_both_moves(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "show.et.srt"
            candidate = root / ".show.et.partial.srt"
            destination = root / "quarantine" / "show.et.srt"
            input_destination = root / "quarantine" / "show.et.input.srt"
            target.write_text("invalid", encoding="utf-8")
            candidate.write_text("improved", encoding="utf-8")
            candidate_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
            store = self.make_store(root)
            try:
                store.record_artifact_version(
                    target, target_hash=candidate_hash, source_path=None,
                    source_hash=None, source_language=None, target_language="et",
                    origin="repair", operation="quarantine",
                    disposition="quarantine_pending", pending_destination=destination,
                    pending_metadata={
                        "candidatePath": str(candidate),
                        "candidateHash": candidate_hash,
                        "inputDestination": str(input_destination),
                        "phase": "intent",
                    },
                )
                result = store.reconcile_pending_operations()
                self.assertEqual(result["completed"], 1)
                self.assertFalse(target.exists())
                self.assertEqual(input_destination.read_text(encoding="utf-8"), "invalid")
                self.assertEqual(destination.read_text(encoding="utf-8"), "improved")
            finally:
                store.close()

    def test_pending_quarantine_rejects_destination_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "show.et.srt"
            destination = root / "quarantine" / "show.et.srt"
            target.write_text("expected", encoding="utf-8")
            expected_hash = hashlib.sha256(target.read_bytes()).hexdigest()
            destination.parent.mkdir()
            destination.write_text("different", encoding="utf-8")
            target.unlink()
            store = self.make_store(root)
            try:
                artifact_id = store.record_artifact_version(
                    target, target_hash=expected_hash, source_path=None,
                    source_hash=None, source_language=None, target_language="et",
                    origin="repair", operation="quarantine",
                    disposition="quarantine_pending",
                    pending_destination=destination,
                )
                result = store.reconcile_pending_operations()
                self.assertEqual(result["completed"], 0)
                self.assertEqual(result["abandoned"], 1)
                row = store._fetchone(
                    "SELECT disposition FROM subtitle_artifacts WHERE id=?",
                    (artifact_id,),
                )
                self.assertEqual(row["disposition"], "quarantine_pending")
            finally:
                store.close()

    def test_pending_quarantine_resumes_report_and_donor_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "show.et.srt"
            destination = root / "quarantine" / "show.et.srt"
            destination.parent.mkdir()
            destination.write_text("improved", encoding="utf-8")
            target_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
            store = self.make_store(root)
            try:
                partial_id = store.record_partial_candidate(
                    item_type="episodes", item_id=7, target_language="et",
                    source_hash="source-hash", target_hash=target_hash,
                    changed_cues=[1], unresolved_cues=[2], provenance=[],
                    artifact_path=None,
                )
                artifact_id = store.record_artifact_version(
                    target, target_hash=target_hash, source_path=None,
                    source_hash=None, source_language=None, target_language="et",
                    origin="repair", operation="quarantine",
                    disposition="quarantine_pending",
                    pending_destination=destination,
                    pending_metadata={
                        "phase": "candidate_archived",
                        "audit": {"targetHash": target_hash},
                        "partialCandidateId": partial_id,
                        "quarantineAttempt": {
                            "item_type": "episodes", "item_id": 7,
                            "target_language": "et",
                            "source_hash": "source-hash",
                            "target_hash": target_hash,
                            "attempt_number": 1,
                            "artifact_path": str(destination),
                            "report_path": f"{destination}.validation.json",
                            "failure_rules": ["garbage"],
                            "cue_signatures": [],
                            "repair_provenance": [],
                            "donor_provenance": [],
                        },
                    },
                )

                result = store.reconcile_pending_operations()

                self.assertEqual(result["completed"], 1)
                self.assertTrue(Path(f"{destination}.validation.json").exists())
                artifact = store._fetchone(
                    "SELECT disposition FROM subtitle_artifacts WHERE id=?",
                    (artifact_id,),
                )
                self.assertEqual(artifact["disposition"], "quarantined")
                partial = store._fetchone(
                    "SELECT quarantine_attempt_id, artifact_path "
                    "FROM partial_candidates WHERE id=?",
                    (partial_id,),
                )
                self.assertIsNotNone(partial["quarantine_attempt_id"])
                self.assertEqual(
                    partial["artifact_path"],
                    os.path.normcase(os.path.abspath(destination)),
                )
            finally:
                store.close()

    def test_pending_quarantine_hold_is_not_double_counted_on_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "show.et.srt"
            destination = root / "quarantine" / "show.et.srt"
            destination.parent.mkdir()
            destination.write_text("invalid", encoding="utf-8")
            target_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
            store = self.make_store(root)
            try:
                artifact_id = store.record_artifact_version(
                    target, target_hash=target_hash, source_path=None,
                    source_hash=None, source_language=None, target_language="et",
                    origin="repair", operation="quarantine",
                    target_identity="show|et",
                    disposition="quarantine_pending",
                    pending_destination=destination,
                    pending_metadata={
                        "rules": ["garbage"], "holdIdentity": "show|et",
                        "audit": {"targetHash": target_hash},
                    },
                )
                store.record_pending_quarantine_hold(
                    artifact_id,
                    identity="show|et",
                    target_path=target,
                    target_hash=target_hash,
                    target_language="et",
                    rules=["garbage"],
                    origin="repair",
                )

                result = store.reconcile_pending_operations()

                self.assertEqual(result["completed"], 1)
                self.assertEqual(
                    store.quarantine_event("show|et")["occurrences"], 1
                )
            finally:
                store.close()

    def test_pending_quarantine_rewrites_stale_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "show.et.srt"
            destination = root / "quarantine" / "show.et.srt"
            destination.parent.mkdir()
            destination.write_text("invalid", encoding="utf-8")
            target_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
            report_path = Path(f"{destination}.validation.json")
            report_path.write_text(
                '{"targetHash": "stale"}', encoding="utf-8"
            )
            store = self.make_store(root)
            try:
                store.record_artifact_version(
                    target, target_hash=target_hash, source_path=None,
                    source_hash=None, source_language=None, target_language="et",
                    origin="repair", operation="quarantine",
                    disposition="quarantine_pending",
                    pending_destination=destination,
                    pending_metadata={
                        "audit": {"targetHash": target_hash},
                    },
                )

                result = store.reconcile_pending_operations()

                self.assertEqual(result["completed"], 1)
                self.assertIn(
                    target_hash, report_path.read_text(encoding="utf-8")
                )
                self.assertNotIn("stale", report_path.read_text(encoding="utf-8"))
            finally:
                store.close()

    def test_legacy_index_identity_includes_path_and_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            try:
                for name in ("one.srt", "two.srt"):
                    path = root / name
                    path.write_text("same", encoding="utf-8")
                    store.record_legacy_quarantine_entry(
                        artifact_path=path, artifact_hash="same-hash",
                        state="indexed",
                    )
                self.assertIsNotNone(store.legacy_quarantine_entry(
                    root / "one.srt", "same-hash"
                ))
                self.assertIsNotNone(store.legacy_quarantine_entry(
                    root / "two.srt", "same-hash"
                ))
                count = store._fetchone(
                    "SELECT COUNT(*) AS count FROM legacy_quarantine_index"
                )["count"]
                self.assertEqual(count, 2)
            finally:
                store.close()

    def test_hash_only_legacy_index_migrates_without_losing_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.sqlite3"
            artifact = os.path.normcase(os.path.abspath(root / "legacy.srt"))
            connection = sqlite3.connect(database)
            connection.execute(
                """
                CREATE TABLE legacy_quarantine_index (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    artifact_path TEXT NOT NULL,
                    artifact_hash TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    reason_code TEXT,
                    quarantine_attempt_id INTEGER,
                    partial_candidate_id INTEGER,
                    scanned_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO legacy_quarantine_index(
                    artifact_path, artifact_hash, state, scanned_at, updated_at
                ) VALUES(?, 'same-hash', 'indexed', 1, 1)
                """,
                (artifact,),
            )
            connection.commit()
            connection.close()

            store = self.make_store(root)
            try:
                self.assertIsNotNone(store.legacy_quarantine_entry(
                    artifact, "same-hash"
                ))
                store.record_legacy_quarantine_entry(
                    artifact_path=root / "copy.srt",
                    artifact_hash="same-hash", state="indexed",
                )
                count = store._fetchone(
                    "SELECT COUNT(*) AS count FROM legacy_quarantine_index"
                )["count"]
                self.assertEqual(count, 2)
            finally:
                store.close()

    def test_retention_prunes_new_terminal_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            try:
                old = time.time() - 10 * 86400
                store.record_provider_event(
                    provider="lingarr", operation="line",
                    classification="transport", retryable=True,
                )
                run_id = store.start_maintenance_run("retention", now=old)
                store.finish_maintenance_run(run_id, success=True, now=old)
                repair_id = store.enqueue_repair_job(
                    dedupe_key="old-repair", target_language="et"
                )
                store.transition_repair_job(
                    repair_id, "failed", expected_states=("queued",)
                )
                missing = root / "missing.srt"
                store.record_legacy_quarantine_entry(
                    artifact_path=missing, artifact_hash="missing-hash",
                    state="unresolved", reason_code="artifact_unavailable",
                )
                with store._transaction() as db:
                    db.execute("UPDATE provider_events SET created_at=?", (old,))
                    db.execute("UPDATE repair_jobs SET updated_at=?", (old,))
                    db.execute(
                        "UPDATE legacy_quarantine_index SET updated_at=?", (old,)
                    )

                removed = store.prune_older_than(5)

                self.assertGreaterEqual(removed, 4)
                for table in (
                    "provider_events", "maintenance_runs", "repair_jobs",
                    "legacy_quarantine_index",
                ):
                    count = store._fetchone(
                        f"SELECT COUNT(*) AS count FROM {table}"
                    )["count"]
                    self.assertEqual(count, 0, table)
            finally:
                store.close()

    def test_migration_checkpoints_roll_back_and_resume(self):
        checkpoints = (
            "schema_objects", "legacy_index", "quarantine_columns",
            "retry_columns", "circuit_columns", "attempt_columns",
            "migration_ledger", "schema_markers",
        )
        for checkpoint in checkpoints:
            with self.subTest(checkpoint=checkpoint), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                database = root / "state.sqlite3"
                store = StateStore(database)
                with store._transaction() as db:
                    db.execute(
                        "INSERT OR REPLACE INTO state_metadata(key, value) "
                        "VALUES('sentinel', 'preserved')"
                    )
                store.close()

                connection = sqlite3.connect(database)
                connection.executescript(
                    """
                    DROP TABLE provider_events;
                    DROP TABLE legacy_quarantine_index;
                    CREATE TABLE legacy_quarantine_index (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        artifact_path TEXT NOT NULL,
                        artifact_hash TEXT NOT NULL UNIQUE,
                        state TEXT NOT NULL,
                        reason_code TEXT,
                        quarantine_attempt_id INTEGER,
                        partial_candidate_id INTEGER,
                        scanned_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    ALTER TABLE quarantine_holds DROP COLUMN resolved_at;
                    ALTER TABLE retry_plans DROP COLUMN source_cue_count;
                    ALTER TABLE circuit_breakers DROP COLUMN trial_plan_id;
                    ALTER TABLE translation_attempts DROP COLUMN failure_category;
                    DELETE FROM schema_migrations WHERE version = 13;
                    DELETE FROM state_metadata WHERE key = 'app_schema_version';
                    PRAGMA user_version = 8;
                    """
                )
                connection.commit()
                connection.close()

                def fail_at(_self, name):
                    if name == checkpoint:
                        raise StateStoreError(f"interrupted at {name}")

                with patch.object(StateStore, "_migration_checkpoint", fail_at):
                    with self.assertRaises(StateStoreError):
                        StateStore(database)

                interrupted = sqlite3.connect(database)
                try:
                    self.assertIsNone(interrupted.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='table' AND name='provider_events'"
                    ).fetchone())
                    legacy_sql = interrupted.execute(
                        "SELECT sql FROM sqlite_master "
                        "WHERE type='table' AND name='legacy_quarantine_index'"
                    ).fetchone()[0]
                    self.assertIn("artifact_hash TEXT NOT NULL UNIQUE", legacy_sql)
                    self.assertIsNone(interrupted.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE name='legacy_quarantine_index_hash_only'"
                    ).fetchone())
                finally:
                    interrupted.close()

                reopened = StateStore(database)
                try:
                    self.assertEqual(reopened._metadata("sentinel"), "preserved")
                    self.assertEqual(
                        reopened._connection.execute(
                            "PRAGMA quick_check"
                        ).fetchone()[0],
                        "ok",
                    )
                    self.assertEqual(
                        reopened._connection.execute(
                            "PRAGMA foreign_key_check"
                        ).fetchall(),
                        [],
                    )
                    temporary = reopened._fetchone(
                        "SELECT name FROM sqlite_master "
                        "WHERE name='legacy_quarantine_index_hash_only'"
                    )
                    self.assertIsNone(temporary)
                    self.assertIsNotNone(reopened._fetchone(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='table' AND name='provider_events'"
                    ))
                    for table, column in (
                        ("quarantine_holds", "resolved_at"),
                        ("retry_plans", "source_cue_count"),
                        ("circuit_breakers", "trial_plan_id"),
                        ("translation_attempts", "failure_category"),
                    ):
                        columns = {
                            row["name"] for row in reopened._connection.execute(
                                f"PRAGMA table_info({table})"
                            )
                        }
                        self.assertIn(column, columns, table)
                finally:
                    reopened.close()

    def test_unreadable_legacy_artifact_is_checkpointed(self):
        class FakeState:
            def __init__(self):
                self.entries = {}

            def legacy_quarantine_entry(self, artifact_path, artifact_hash):
                return self.entries.get((str(artifact_path), artifact_hash))

            def record_legacy_quarantine_entry(self, **values):
                self.entries[(
                    str(values["artifact_path"]), values["artifact_hash"]
                )] = values

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "legacy.srt"
            artifact.write_text("legacy", encoding="utf-8")
            state = FakeState()
            indexer = LegacyQuarantineIndexer(
                state=state, root=root,
                inspect_artifact=lambda *_args: {},
                shutdown_requested=lambda: False,
            )
            with patch(
                "autotranslate.maintenance.legacy_index._sha256",
                side_effect=OSError("unreadable"),
            ):
                first = indexer.run()
                second = indexer.run()
            self.assertEqual(first["unresolved"], 1)
            self.assertEqual(second["skipped"], 1)
            entry = next(iter(state.entries.values()))
            self.assertEqual(entry["reason_code"], "artifact_unavailable")
            self.assertTrue(entry["artifact_hash"].startswith("unreadable:"))


if __name__ == "__main__":
    unittest.main()
