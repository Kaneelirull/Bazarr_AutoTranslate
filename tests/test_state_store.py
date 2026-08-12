import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "docker"))

from autotranslate.persistence.state_store import StateStore, StateStoreError  # noqa: E402


class StateStoreTests(unittest.TestCase):
    def test_retry_plan_deduplicates_hash_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            store = StateStore(database)
            first, repeated = store.schedule_retry_plan(
                item_type="episodes",
                item_id=42,
                target_language="et",
                source_hash="source-a",
                failure_class="whole_file",
                rules=["target_structure"],
                state="regeneration_waiting",
                failed_output_hash="bad-a",
                eligible_completed_cycle=2,
            )
            second, repeated_again = store.schedule_retry_plan(
                item_type="episodes",
                item_id=42,
                target_language="et",
                source_hash="source-a",
                failure_class="whole_file",
                rules=["target_structure"],
                state="regeneration_waiting",
                failed_output_hash="bad-a",
                eligible_completed_cycle=99,
            )
            self.assertFalse(repeated)
            self.assertTrue(repeated_again)
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(second["eligibleCompletedCycle"], 2)
            store.close()

            reopened = StateStore(database)
            self.assertEqual(
                reopened.active_retry_plan("episodes", 42, "et")["id"],
                first["id"],
            )
            reopened.close()

    def test_retry_claims_are_batched_and_limited_per_series(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            for item_id, series in ((1, "top-gear"), (2, "top-gear"), (3, "other")):
                store.schedule_retry_plan(
                    item_type="episodes",
                    item_id=item_id,
                    target_language="et",
                    source_hash=f"source-{item_id}",
                    failure_class="whole_file",
                    rules=["target_structure"],
                    state="regeneration_waiting",
                    series_key=series,
                    failed_output_hash=f"bad-{item_id}",
                    eligible_completed_cycle=2,
                    now=float(item_id),
                )
            self.assertEqual(store.claim_due_retry_plans(1, limit=5), [])
            claimed = store.claim_due_retry_plans(
                2, limit=5, per_series_limit=1
            )
            self.assertEqual([plan["itemId"] for plan in claimed], [1, 3])
            store.close()

    def test_retry_limit_collapses_legacy_series_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            for item_id, key in ((1, "episodes:title:old"), (2, "season:05")):
                store.schedule_retry_plan(
                    item_type="episodes",
                    item_id=item_id,
                    target_language="et",
                    source_hash=f"source-{item_id}",
                    failure_class="whole_file",
                    rules=["target_structure"],
                    state="regeneration_waiting",
                    series_key=key,
                    eligible_completed_cycle=0,
                )
                store.register_series_alias(key, "sonarr:99", "Example Show")
            claimed = store.claim_due_retry_plans(
                0, limit=5, per_series_limit=1
            )
            self.assertEqual(len(claimed), 1)
            self.assertEqual(claimed[0]["canonicalSeriesKey"], "sonarr:99")
            store.close()

    def test_retry_claims_prefer_smallest_within_oldest_day_and_recover_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            day = 86400.0
            for item_id, seen, cues in (
                (1, day + 10, 900),
                (2, day + 20, 100),
                (3, 2 * day, 10),
            ):
                store.schedule_retry_plan(
                    item_type="movies",
                    item_id=item_id,
                    target_language="et",
                    source_hash=f"source-{item_id}",
                    failure_class="whole_file",
                    rules=["target_structure"],
                    state="regeneration_waiting",
                    source_cue_count=cues,
                    eligible_completed_cycle=2,
                    now=seen,
                )
            claimed = store.claim_due_retry_plans(2, limit=2)
            self.assertEqual([plan["itemId"] for plan in claimed], [2, 1])
            self.assertEqual(store.recover_retry_claims(), 2)
            self.assertEqual(
                [plan["state"] for plan in store.retry_plans()[:2]],
                ["regeneration_waiting", "regeneration_waiting"],
            )
            store.close()

    def test_cycle_two_backlog_admits_only_five_of_eighty_six(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            for item_id in range(1, 87):
                store.schedule_retry_plan(
                    item_type="episodes",
                    item_id=item_id,
                    target_language="et",
                    source_hash=f"source-{item_id}",
                    failure_class="whole_file",
                    rules=["target_structure"],
                    state="regeneration_waiting",
                    series_key=f"series-{item_id}",
                    source_cue_count=1000 - item_id,
                    eligible_completed_cycle=2,
                    now=86400 + item_id,
                )
            claimed = store.claim_due_retry_plans(
                2, limit=5, per_series_limit=1
            )
            self.assertEqual(len(claimed), 5)
            self.assertEqual(len([
                plan for plan in store.retry_plans()
                if plan["state"] == "regeneration_waiting"
            ]), 81)
            store.close()

    def test_no_progress_retry_rotates_behind_never_admitted_work(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            for item_id in range(1, 7):
                store.schedule_retry_plan(
                    item_type="episodes",
                    item_id=item_id,
                    target_language="et",
                    source_hash=f"source-{item_id}",
                    failure_class="whole_file",
                    rules=["target_structure"],
                    state="regeneration_waiting",
                    series_key=f"series-{item_id}",
                    eligible_completed_cycle=2,
                    now=float(item_id),
                )
            first = store.claim_due_retry_plans(2, limit=5)
            for plan in first:
                store.reschedule_retry_no_progress(
                    plan["id"],
                    completed_cycle=2,
                    deferral_class="target_unresolved",
                    reason="target could not be resolved safely",
                )
            second = store.claim_due_retry_plans(3, limit=1)
            self.assertEqual(second[0]["itemId"], 6)
            self.assertEqual(second[0]["lastAdmittedCycle"], 3)
            store.close()

    def test_restart_keeps_retry_claim_with_durable_lingarr_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            store.schedule_retry_plan(
                item_type="episodes",
                item_id=42,
                target_language="et",
                source_hash="source-42",
                failure_class="whole_file",
                rules=["target_structure"],
                state="regeneration_waiting",
                series_key="sonarr:7",
                eligible_completed_cycle=0,
            )
            claimed = store.claim_due_retry_plans(0, limit=1)[0]
            attempt = store.record_submission(
                item_type="episodes",
                item_id=42,
                target_language="et",
                target_identity="episode-42",
                target_path="episode.et.srt",
                cooldown_seconds=3600,
            )
            self.assertTrue(
                store.bind_retry_submission(
                    claimed["id"], claimed["claimOwner"], attempt
                )
            )
            store.mark_submission_submitted(attempt, 9001)
            self.assertEqual(store.recover_retry_claims(), 0)
            claim = store.retry_claims_with_submissions()[0]
            self.assertEqual(claim["state"], "regeneration_queued")
            self.assertEqual(claim["lingarrJobId"], 9001)
            store.close()

    def test_schema_v5_persists_failure_details(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            attempt = store.record_submission(
                item_type="movies",
                item_id=9,
                target_language="et",
                target_identity="movie",
                target_path="movie.et.srt",
                cooldown_seconds=1,
                submitted_at=1,
            )
            store.mark_submission_failed(
                attempt,
                failure_category="context_limit",
                failure_details={"status": "Failed", "category": "context_limit"},
            )
            row = store._fetchone(
                "SELECT failure_category, failure_details_json FROM translation_attempts WHERE id=?",
                (attempt,),
            )
            self.assertEqual(row["failure_category"], "context_limit")
            self.assertEqual(json.loads(row["failure_details_json"])["status"], "Failed")
            self.assertEqual(store._fetchone("PRAGMA user_version")[0], 16)
            store.close()

    def test_schema_v4_migrates_retry_size_and_failure_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            StateStore(database).close()
            connection = sqlite3.connect(database)
            connection.execute("ALTER TABLE retry_plans DROP COLUMN source_cue_count")
            connection.execute("ALTER TABLE translation_attempts DROP COLUMN failure_category")
            connection.execute("ALTER TABLE translation_attempts DROP COLUMN failure_details_json")
            connection.execute("PRAGMA user_version = 4")
            connection.commit()
            connection.close()

            migrated = StateStore(database)
            retry_columns = {
                row["name"]
                for row in migrated._connection.execute(
                    "PRAGMA table_info(retry_plans)"
                )
            }
            attempt_columns = {
                row["name"]
                for row in migrated._connection.execute(
                    "PRAGMA table_info(translation_attempts)"
                )
            }
            self.assertIn("source_cue_count", retry_columns)
            self.assertIn("failure_category", attempt_columns)
            self.assertIn("failure_details_json", attempt_columns)
            self.assertEqual(migrated._fetchone("PRAGMA user_version")[0], 16)
            migrated.close()

    def test_quarantine_attempts_are_immutable_and_privacy_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(
                Path(directory) / "state.sqlite3",
                validator_version="validator-6",
                config_fingerprint="config-a",
            )
            values = {
                "item_type": "episodes",
                "item_id": 74608,
                "target_language": "et",
                "source_hash": "source-a",
                "target_hash": "target-a",
                "attempt_number": 1,
                "artifact_path": "attempt-1.srt",
                "report_path": "attempt-1.srt.validation.json",
                "failure_rules": ["copied_source"],
                "cue_signatures": [{
                    "cueNumber": 25,
                    "startMs": 5000,
                    "tokenHashes": ["a1", "b2"],
                    "sourceHash": "signature-a",
                }],
            }
            first = store.record_quarantine_attempt(**values)
            second = store.record_quarantine_attempt(
                **{**values, "failure_rules": ["prompt_marker"]}
            )

            self.assertEqual(first, second)
            self.assertEqual(second["failureRules"], ["copied_source"])
            self.assertNotIn("dialogue", json.dumps(second).lower())
            self.assertEqual(second["validatorFingerprint"], "validator-6")
            self.assertEqual(second["configFingerprint"], "config-a")
            store.close()

    def test_source_change_supersedes_active_retry_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            old, _ = store.schedule_retry_plan(
                item_type="movies",
                item_id=7,
                target_language="sv",
                source_hash="old",
                failure_class="whole_file",
                rules=["cue_count_mismatch"],
                state="regeneration_waiting",
                eligible_completed_cycle=2,
            )
            new, _ = store.schedule_retry_plan(
                item_type="movies",
                item_id=7,
                target_language="sv",
                source_hash="new",
                failure_class="whole_file",
                rules=["cue_count_mismatch"],
                state="regeneration_waiting",
                eligible_completed_cycle=2,
            )
            plans = {plan["id"]: plan for plan in store.retry_plans()}
            self.assertEqual(plans[old["id"]]["state"], "superseded")
            self.assertEqual(plans[new["id"]]["state"], "regeneration_waiting")
            store.close()

    def test_older_completion_cannot_resolve_newer_source_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            try:
                old, _ = store.schedule_retry_plan(
                    item_type="movies", item_id=7, target_language="sv",
                    source_hash="old", failure_class="whole_file",
                    rules=["cue_count_mismatch"], state="regeneration_waiting",
                    eligible_completed_cycle=0,
                )
                new, _ = store.schedule_retry_plan(
                    item_type="movies", item_id=7, target_language="sv",
                    source_hash="new", failure_class="whole_file",
                    rules=["cue_count_mismatch"], state="regeneration_waiting",
                    eligible_completed_cycle=0,
                )
                self.assertFalse(store.resolve_retry_plan(old["id"], "old"))
                self.assertFalse(store.resolve_retry_plan(new["id"], "old"))
                self.assertEqual(
                    store.manual_review_plan(new["id"])["state"],
                    "regeneration_waiting",
                )
                self.assertTrue(store.resolve_retry_plan(new["id"], "new"))
            finally:
                store.close()

    def test_completed_cycle_counter_is_durable(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            store = StateStore(database)
            self.assertEqual(store.completed_cycle(), 0)
            self.assertEqual(store.advance_completed_cycle(), 1)
            store.close()
            reopened = StateStore(database)
            self.assertEqual(reopened.completed_cycle(), 1)
            reopened.close()

    def make_store(self, root: Path, **kwargs) -> StateStore:
        return StateStore(
            root / "bazarr-autotranslate.sqlite3",
            validator_version="validator-test",
            **kwargs,
        )

    def test_schema_v2_quarantine_hold_migrates_to_nonblocking_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.sqlite3"
            old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
            future = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE quarantine_holds (
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
                PRAGMA user_version = 2;
                """
            )
            connection.execute(
                """
                INSERT INTO quarantine_holds VALUES(
                    'show|et', 'bad-hash', 'show.et.srt', 'et',
                    '["prompt_marker"]', 'lingarr', ?, ?, ?, 2
                )
                """,
                (old, old, future),
            )
            connection.commit()
            connection.close()

            store = StateStore(database)
            try:
                event = store.quarantine_event("show|et")
                self.assertEqual(event["occurrences"], 2)
                self.assertIsNone(event["resolvedAt"])
                removed = store.prune_older_than(30)
                self.assertGreaterEqual(removed, 1)
                self.assertIsNone(store.quarantine_event("show|et"))
            finally:
                store.close()

    def test_schema_and_state_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            store.record_submission(
                "movies", 7, "et", cooldown_seconds=3600,
                source_hash="source", target_path=root / "movie.et.srt",
            )
            store.close()

            reopened = self.make_store(root)
            try:
                self.assertIsNotNone(
                    reopened.check_cooldown("movies", 7, "et")
                )
                result = reopened._fetchone("PRAGMA quick_check")
                self.assertEqual(result[0], "ok")
            finally:
                reopened.close()

    def test_episode_and_movie_ids_have_independent_cooldowns(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(Path(directory))
            try:
                store.record_submission(
                    "episodes", 42, "et", cooldown_seconds=3600
                )
                self.assertIsNotNone(
                    store.check_cooldown("episodes", 42, "et")
                )
                self.assertIsNone(store.check_cooldown("movies", 42, "et"))
                store.record_submission(
                    "movies", 42, "et", cooldown_seconds=3600
                )
                self.assertIsNotNone(
                    store.check_cooldown("movies", 42, "et")
                )
            finally:
                store.close()

    def test_concurrent_submission_writes_are_not_lost(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(Path(directory))
            errors = []

            def writer(item_id: int):
                try:
                    store.record_submission(
                        "episodes",
                        item_id,
                        "et",
                        cooldown_seconds=3600,
                        source_hash=f"source-{item_id}",
                    )
                except Exception as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            threads = [
                threading.Thread(target=writer, args=(item_id,))
                for item_id in range(50)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            try:
                self.assertEqual(errors, [])
                count = store._fetchone(
                    "SELECT COUNT(*) FROM translation_attempts"
                )[0]
                self.assertEqual(count, 50)
                for item_id in range(50):
                    self.assertIsNotNone(
                        store.check_cooldown("episodes", item_id, "et")
                    )
            finally:
                store.close()

    def test_process_lock_rejects_second_instance_and_releases_on_close(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self.make_store(root, acquire_process_lock=True)
            try:
                with self.assertRaises(StateStoreError):
                    self.make_store(root, acquire_process_lock=True)
            finally:
                first.close()
            second = self.make_store(root, acquire_process_lock=True)
            second.close()

    def test_artifacts_are_matched_by_path_and_hash_not_hash_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "one.et.srt"
            second = root / "two.et.srt"
            store = self.make_store(root)
            try:
                store.record(
                    first,
                    source_hash="source-one",
                    target_hash="same-target",
                    result="valid",
                    origin="lingarr",
                    source_path=root / "one.en.srt",
                )
                self.assertIsNone(
                    store.matching_record(second, "same-target")
                )
                matched = store.matching_record(first, "same-target")
                self.assertEqual(matched["sourceHash"], "source-one")
            finally:
                store.close()

    def test_translation_output_updates_attempt_and_keeps_artifacts_immutable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "movie.et.srt"
            source = root / "movie.eng.srt"
            store = self.make_store(root)
            try:
                attempt = store.record_submission(
                    "movies",
                    7,
                    "et",
                    cooldown_seconds=3600,
                    source_path=source,
                    source_hash="source-hash",
                    source_language="en",
                    target_identity="movie",
                    target_variant="",
                    status="submitted",
                )
                first_artifact = store.record(
                    target,
                    source_hash="source-hash",
                    target_hash="target-hash",
                    result="pending",
                    origin="lingarr",
                    source_path=source,
                    source_language="en",
                    target_language="et",
                    target_identity="movie",
                    target_variant="",
                    operation="translation",
                    attempt_id=attempt,
                    item_type="movies",
                    item_id=7,
                )
                attempt_row = store._fetchone(
                    "SELECT * FROM translation_attempts WHERE id = ?",
                    (attempt,),
                )
                self.assertEqual(attempt_row["target_hash"], "target-hash")
                self.assertEqual(attempt_row["status"], "output_ready")
                self.assertEqual(
                    attempt_row["actual_target_path"],
                    os.path.normcase(os.path.abspath(target)),
                )

                completed_artifact = store.record(
                    target,
                    source_hash="source-hash",
                    target_hash="target-hash",
                    result="valid",
                    origin="lingarr",
                )
                self.assertEqual(completed_artifact, first_artifact)
                self.assertEqual(
                    store._fetchone(
                        "SELECT status FROM translation_attempts WHERE id = ?",
                        (attempt,),
                    )["status"],
                    "completed",
                )

                second_artifact = store.record(
                    target,
                    source_hash=None,
                    target_hash="target-hash",
                    result="valid",
                    origin="external",
                )
                self.assertNotEqual(first_artifact, second_artifact)
                first = store._fetchone(
                    "SELECT origin, source_hash FROM subtitle_artifacts WHERE id = ?",
                    (first_artifact,),
                )
                self.assertEqual(tuple(first), ("lingarr", "source-hash"))
            finally:
                store.close()

    def test_replacement_lineage_and_pending_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "movie.et.srt"
            target.write_bytes(b"old")
            store = self.make_store(root)
            try:
                parent = store.record_artifact_version(
                    target,
                    target_hash=store._hash_file(target),
                    source_path=root / "movie.en.srt",
                    source_hash="source",
                    source_language="en",
                    target_language="et",
                    origin="lingarr",
                    operation="translation",
                )
                target.write_bytes(b"new")
                child_hash = store._hash_file(target)
                child = store.record_artifact_version(
                    target,
                    target_hash=child_hash,
                    source_path=root / "movie.en.srt",
                    source_hash="source",
                    source_language="en",
                    target_language="et",
                    origin="lingarr",
                    operation="cue_repair",
                    parent_artifact_id=parent,
                    disposition="replacement_pending",
                    pending_destination=target,
                )
                recovery = store.reconcile_pending_operations()
                self.assertEqual(recovery["completed"], 1)
                artifact = store.latest_artifact(target, child_hash)
                self.assertEqual(artifact["id"], child)
                self.assertEqual(artifact["parent_artifact_id"], parent)
                self.assertEqual(artifact["disposition"], "active")
            finally:
                store.close()

    def test_quarantine_recovery_restores_audit_after_interrupted_move(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "movie.et.srt"
            destination = root / "quarantine" / "movie.et.srt"
            target.write_bytes(b"invalid")
            target_hash = StateStore._hash_file(target)
            store = self.make_store(root)
            try:
                artifact = store.record_artifact_version(
                    target,
                    target_hash=target_hash,
                    source_path=root / "movie.en.srt",
                    source_hash="source",
                    source_language="en",
                    target_language="et",
                    origin="lingarr",
                    operation="quarantine",
                    target_identity="movie|et",
                    disposition="quarantine_pending",
                    pending_destination=destination,
                    pending_metadata={
                        "rules": ["prompt_marker"],
                        "holdIdentity": "movie|et",
                    },
                )
                destination.parent.mkdir()
                os.replace(target, destination)

                recovery = store.reconcile_pending_operations()

                self.assertEqual(recovery["completed"], 1)
                self.assertEqual(
                    store.latest_artifact(
                        target, target_hash
                    )["disposition"],
                    "quarantined",
                )
                event = store.quarantine_event(
                    "movie|et", target_hash=target_hash
                )
                self.assertEqual(event["rules"], ["prompt_marker"])
                self.assertEqual(event["occurrences"], 1)
                self.assertIsNone(event["resolvedAt"])
                self.assertEqual(
                    store.latest_artifact(target, target_hash)["id"], artifact
                )
            finally:
                store.close()

    def test_retention_prunes_validation_but_keeps_artifact_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "movie.et.srt"
            store = self.make_store(root)
            try:
                store.record(
                    target,
                    source_hash="source",
                    target_hash="target",
                    result="valid",
                    origin="lingarr",
                )
                removed = store.prune_older_than(
                    30,
                    now=datetime.now(timezone.utc) + timedelta(days=31),
                )
                self.assertEqual(removed, 1)
                self.assertIsNone(store.matching_record(target, "target"))
                artifact = store.latest_artifact(target, "target")
                self.assertEqual(artifact["source_hash"], "source")
            finally:
                store.close()

    def test_timing_estimate_uses_successful_samples_and_global_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(Path(directory))
            try:
                for elapsed in (10.0, 12.0, 11.0, 5000.0):
                    store.record_timing_sample(
                        kind="file",
                        source_language="en",
                        target_language="et",
                        cue_count=10,
                        elapsed_seconds=elapsed,
                        outcome="accepted",
                    )
                store.record_timing_sample(
                    kind="file",
                    source_language="en",
                    target_language="et",
                    cue_count=10,
                    elapsed_seconds=1.0,
                    outcome="failed",
                )
                estimate = store.timing_estimate(
                    kind="file",
                    source_language="en",
                    target_language="et",
                    cold_seconds_per_cue=1.8,
                    alpha=0.2,
                )
                self.assertEqual(estimate["sampleCount"], 4)
                self.assertEqual(estimate["scope"], "language_pair")
                self.assertLess(estimate["secondsPerCue"], 5.0)
                fallback = store.timing_estimate(
                    kind="file",
                    source_language="sv",
                    target_language="et",
                    cold_seconds_per_cue=1.8,
                    alpha=0.2,
                )
                self.assertEqual(fallback["scope"], "global")
            finally:
                store.close()

    def test_circuit_breaker_opens_and_allows_one_half_open_trial(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(Path(directory))
            try:
                for index in range(3):
                    state = store.record_circuit_outcome(
                        series_key="sonarr:1",
                        series_title="Top Gear",
                        success=False,
                        reason="timeout",
                        threshold=3,
                        open_cycles=3,
                        config_fingerprint="a",
                        now=100 + index,
                    )
                self.assertEqual(state["state"], "open")
                self.assertEqual(state["eligibleAfterCycle"], 3)
                self.assertFalse(store.circuit_permission(
                    series_key="sonarr:1",
                    series_title="Top Gear",
                    config_fingerprint="a",
                    completed_cycle=2,
                )["allowed"])
                preview = store.circuit_permission(
                    series_key="sonarr:1",
                    series_title="Top Gear",
                    config_fingerprint="a",
                    claim=False,
                    completed_cycle=3,
                )
                self.assertTrue(preview["allowed"])
                self.assertEqual(preview["state"], "eligible")
                second_preview = store.circuit_permission(
                    series_key="sonarr:1",
                    series_title="Top Gear",
                    config_fingerprint="a",
                    claim=False,
                    completed_cycle=3,
                )
                self.assertTrue(second_preview["allowed"])
                trial = store.circuit_permission(
                    series_key="sonarr:1",
                    series_title="Top Gear",
                    config_fingerprint="a",
                    completed_cycle=3,
                )
                self.assertTrue(trial["allowed"])
                self.assertEqual(trial["state"], "half_open")
                self.assertFalse(store.circuit_permission(
                    series_key="sonarr:1",
                    series_title="Top Gear",
                    config_fingerprint="a",
                    completed_cycle=3,
                )["allowed"])
                self.assertTrue(
                    store.release_circuit_trial(
                        "sonarr:1", trial["trialOwner"], "no submission"
                    )
                )
                reclaimed = store.circuit_permission(
                    series_key="sonarr:1",
                    series_title="Top Gear",
                    config_fingerprint="a",
                    completed_cycle=3,
                    trial_owner="attempt:2",
                )
                self.assertTrue(reclaimed["allowed"])
                self.assertTrue(
                    store.bind_circuit_trial_job(
                        "sonarr:1", "attempt:2", 4321
                    )
                )
                self.assertFalse(
                    store.release_circuit_trial(
                        "sonarr:1", "wrong-owner", "must not release"
                    )
                )
                store.record_circuit_outcome(
                    series_key="sonarr:1",
                    series_title="Top Gear",
                    success=False,
                    reason="trial failed",
                    threshold=3,
                    open_cycles=3,
                    config_fingerprint="a",
                )
                self.assertEqual(
                    store.circuit_breakers()[0]["eligibleAfterCycle"], 3
                )
            finally:
                store.close()

    def test_retry_linked_circuit_settlement_is_generation_safe_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(Path(directory))
            try:
                plan, _ = store.schedule_retry_plan(
                    item_type="episodes", item_id=42, target_language="et",
                    source_hash="source", failure_class="cue_repairable",
                    rules=["excessive_lines"], eligible_completed_cycle=0,
                    state="repair_retry_queued", series_key="sonarr:1",
                    series_title="Top Gear",
                )
                store.record_circuit_outcome(
                    series_key="sonarr:1", series_title="Top Gear",
                    success=False, reason="invalid", threshold=1,
                    open_cycles=1, config_fingerprint="config",
                )
                store.advance_completed_cycle()
                trial = store.circuit_permission(
                    series_key="sonarr:1", series_title="Top Gear",
                    config_fingerprint="config", trial_owner="attempt:1",
                )
                self.assertTrue(store.bind_circuit_trial_job(
                    "sonarr:1", "attempt:1", 99, trial_plan_id=plan["id"],
                    lease_generation=trial["leaseGeneration"],
                ))
                linked = store.circuit_trial_for_retry_plan(plan["id"])
                self.assertEqual(linked["leaseGeneration"], trial["leaseGeneration"])

                stale = store.settle_circuit_trial_for_retry(
                    plan["id"], lease_generation=trial["leaseGeneration"] + 1,
                    outcome="success", open_cycles=1,
                )
                self.assertFalse(stale["settled"])
                self.assertEqual(store.circuit_breakers()[0]["state"], "half_open")

                settled = store.settle_circuit_trial_for_retry(
                    plan["id"], lease_generation=trial["leaseGeneration"],
                    outcome="success", open_cycles=1,
                )
                self.assertTrue(settled["settled"])
                self.assertEqual(store.circuit_breakers(), [])
                repeated = store.settle_circuit_trial_for_retry(
                    plan["id"], lease_generation=trial["leaseGeneration"],
                    outcome="success", open_cycles=1,
                )
                self.assertFalse(repeated["settled"])
            finally:
                store.close()

    def test_deferred_retry_trial_reopens_without_counting_another_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(Path(directory))
            try:
                plan, _ = store.schedule_retry_plan(
                    item_type="episodes", item_id=42, target_language="et",
                    source_hash="source", failure_class="cue_repairable",
                    rules=["excessive_lines"], eligible_completed_cycle=0,
                    state="repair_retry_queued", series_key="sonarr:1",
                    series_title="Top Gear",
                )
                opened = store.record_circuit_outcome(
                    series_key="sonarr:1", series_title="Top Gear",
                    success=False, reason="invalid", threshold=1,
                    open_cycles=1, config_fingerprint="config",
                )
                store.advance_completed_cycle()
                trial = store.circuit_permission(
                    series_key="sonarr:1", series_title="Top Gear",
                    config_fingerprint="config", trial_owner="attempt:1",
                )
                store.bind_circuit_trial_job(
                    "sonarr:1", "attempt:1", 99, trial_plan_id=plan["id"],
                    lease_generation=trial["leaseGeneration"],
                )
                settled = store.settle_circuit_trial_for_retry(
                    plan["id"], lease_generation=trial["leaseGeneration"],
                    outcome="deferred", open_cycles=2, reason="repair deferred",
                )
                self.assertTrue(settled["settled"])
                self.assertEqual(settled["failures"], opened["failures"])
                circuit = store.circuit_breakers()[0]
                self.assertEqual(circuit["state"], "open")
                self.assertEqual(circuit["completedCyclesRemaining"], 2)
            finally:
                store.close()

    def test_late_retry_reschedule_cannot_revive_superseded_plan_or_stale_trial(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(Path(directory))
            try:
                old, _ = store.schedule_retry_plan(
                    item_type="episodes", item_id=42, target_language="et",
                    source_hash="old", failure_class="cue_repairable",
                    rules=["excessive_lines"], eligible_completed_cycle=0,
                    state="repair_retry_queued", series_key="sonarr:1",
                    series_title="Top Gear",
                )
                store.record_circuit_outcome(
                    series_key="sonarr:1", series_title="Top Gear",
                    success=False, reason="invalid", threshold=1,
                    open_cycles=1, config_fingerprint="config",
                )
                store.advance_completed_cycle()
                trial = store.circuit_permission(
                    series_key="sonarr:1", series_title="Top Gear",
                    config_fingerprint="config", trial_owner="attempt:1",
                )
                store.bind_circuit_trial_job(
                    "sonarr:1", "attempt:1", 99, trial_plan_id=old["id"],
                    lease_generation=trial["leaseGeneration"],
                )
                store.schedule_retry_plan(
                    item_type="episodes", item_id=42, target_language="et",
                    source_hash="new", failure_class="cue_repairable",
                    rules=["excessive_lines"], eligible_completed_cycle=1,
                    state="repair_retry_queued", series_key="sonarr:1",
                    series_title="Top Gear",
                )

                self.assertIsNone(store.reschedule_retry_no_progress(
                    old["id"], completed_cycle=1,
                    deferral_class="worker_exception", reason="late worker",
                    lease_generation=trial["leaseGeneration"],
                ))
                self.assertFalse(store.resolve_retry_plan(
                    old["id"], "old", lease_generation=trial["leaseGeneration"],
                ))
                self.assertEqual(store.retry_plan(old["id"])["state"], "superseded")
            finally:
                store.close()

    def test_legacy_open_circuit_migrates_from_persisted_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(Path(directory))
            try:
                for _ in range(7):
                    store.advance_completed_cycle()
                with store._transaction() as db:
                    db.execute(
                        """
                        INSERT INTO circuit_breakers(
                            series_key, series_title, consecutive_failures, state,
                            opened_at, retry_at, half_open_claimed,
                            config_fingerprint, updated_at
                        ) VALUES (?, ?, 3, 'open', 1, 999999, 0, ?, 1)
                        """,
                        ("sonarr:2", "Example Show", "a"),
                    )
                result = store.initialize_cycle_circuits(3)
                self.assertEqual(result["completedCycle"], 7)
                self.assertEqual(result["migrated"], 1)
                circuit = store.circuit_breakers()[0]
                self.assertEqual(circuit["eligibleAfterCycle"], 10)
                self.assertEqual(circuit["completedCyclesRemaining"], 3)
            finally:
                store.close()

    def test_circuit_title_is_corrected_and_generic_rows_are_retired(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(Path(directory))
            try:
                store.record_circuit_outcome(
                    series_key="sonarr:9",
                    series_title="Season 05",
                    success=False,
                    reason="timeout",
                    threshold=1,
                    open_cycles=3,
                    config_fingerprint="a",
                )
                result = store.initialize_cycle_circuits(3)
                self.assertEqual(result["retiredGeneric"], 1)
                self.assertEqual(store.circuit_breakers(), [])

                store.circuit_permission(
                    series_key="sonarr:9",
                    series_title="Top Gear",
                    config_fingerprint="a",
                )
                row = store._fetchone(
                    "SELECT series_title, state FROM circuit_breakers "
                    "WHERE series_key='sonarr:9'"
                )
                self.assertEqual(row["series_title"], "Top Gear")
                self.assertEqual(row["state"], "closed")
            finally:
                store.close()

    def test_successful_source_readiness_is_hash_duration_and_config_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            store = StateStore(
                database, validator_version="validator-v1",
                config_fingerprint="config-v1",
            )
            try:
                store.record_source_readiness(
                    media_identity="episode-1",
                    video_path=Path(directory) / "episode.mkv",
                    source_path=Path(directory) / "episode.eng.srt",
                    source_language="en",
                    source_hash="source-a",
                    media_duration_seconds=2700,
                    target_language="et",
                )
                self.assertIsNotNone(store.source_readiness(
                    media_identity="episode-1", source_language="en",
                    source_hash="source-a", media_duration_seconds=2700.4,
                ))
                self.assertIsNone(store.source_readiness(
                    media_identity="episode-1", source_language="en",
                    source_hash="source-b", media_duration_seconds=2700,
                ))
                self.assertIsNone(store.source_readiness(
                    media_identity="episode-1", source_language="en",
                    source_hash="source-a", media_duration_seconds=2701,
                ))
            finally:
                store.close()
            changed = StateStore(
                database, validator_version="validator-v1",
                config_fingerprint="config-v2",
            )
            try:
                self.assertIsNone(changed.source_readiness(
                    media_identity="episode-1", source_language="en",
                    source_hash="source-a", media_duration_seconds=2700,
                ))
            finally:
                changed.close()

    def test_retention_protects_nonterminal_repair_and_retry_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = StateStore(root / "state.sqlite3")
            try:
                source = root / "source.eng.srt"
                target = root / "target.et.srt"
                artifact = root / "quarantine" / "target.et.srt"
                store.enqueue_repair_job(
                    dedupe_key="repair", target_language="et",
                    source_path=source, target_path=target,
                )
                store.schedule_retry_plan(
                    item_type="episodes", item_id=1, target_language="et",
                    source_hash="source", failure_class="validation",
                    rules=["invalid"], state="regeneration_waiting",
                    eligible_completed_cycle=0, artifact_path=artifact,
                )
                protected = {
                    os.path.normcase(os.path.abspath(path))
                    for path in store.protected_artifact_paths()
                }
                self.assertTrue({
                    os.path.normcase(os.path.abspath(path))
                    for path in (source, target, artifact)
                }.issubset(protected))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
