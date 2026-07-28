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

from state_store import StateStore, StateStoreError  # noqa: E402


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

    def test_legacy_json_migration_is_idempotent_and_preserves_backups(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            submit = root / "submitted_cache.json"
            validation = root / "validation_state.json"
            target = root / "movie.et.srt"
            now = time.time()
            submit.write_text(
                json.dumps({
                    "7:et": {
                        "submittedAt": now,
                        "itemType": "movies",
                        "targetPath": str(target),
                        "sourceHash": "source",
                    },
                    "bad": {"submittedAt": "bad"},
                }),
                encoding="utf-8",
            )
            validation.write_text(
                json.dumps({
                    "files": {
                        str(target): {
                            "validatorVersion": "old",
                            "sourceHash": "source",
                            "targetHash": "target",
                            "result": "valid",
                            "origin": "lingarr",
                            "validatedAt": datetime.now(timezone.utc).isoformat(),
                            "details": {"sourcePath": str(root / "movie.en.srt")},
                        }
                    },
                    "quarantineTombstones": {},
                }),
                encoding="utf-8",
            )
            store = self.make_store(root)
            try:
                first = store.migrate_legacy(
                    submit, validation, cooldown_seconds=3600
                )
                second = store.migrate_legacy(
                    submit, validation, cooldown_seconds=3600
                )
                self.assertEqual(first["submissions"], 1)
                self.assertEqual(first["artifacts"], 1)
                self.assertEqual(first["skipped"], 1)
                self.assertEqual(second["submissions"], 0)
                self.assertTrue(
                    (root / "submitted_cache.json.migrated.bak").exists()
                )
                self.assertTrue(
                    (root / "validation_state.json.migrated.bak").exists()
                )
                record = store.matching_record(target, "target")
                self.assertEqual(record["origin"], "lingarr")
                self.assertEqual(record["sourceHash"], "source")
            finally:
                store.close()

    def test_migration_never_trusts_incomplete_lingarr_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            validation = root / "validation_state.json"
            target = root / "movie.et.srt"
            validation.write_text(
                json.dumps({
                    "files": {
                        str(target): {
                            "targetHash": "target",
                            "sourceHash": None,
                            "result": "valid",
                            "origin": "lingarr",
                            "details": {},
                        }
                    }
                }),
                encoding="utf-8",
            )
            store = self.make_store(root)
            try:
                store.migrate_legacy(
                    root / "submitted_cache.json",
                    validation,
                    cooldown_seconds=3600,
                )
                record = store.matching_record(target, "target")
                self.assertEqual(record["origin"], "external")
                self.assertIsNone(record["sourceHash"])
                self.assertEqual(record["validationMode"], "target-only")
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
                        open_seconds=60,
                        config_fingerprint="a",
                        now=100 + index,
                    )
                self.assertEqual(state["state"], "open")
                self.assertFalse(store.circuit_permission(
                    series_key="sonarr:1",
                    series_title="Top Gear",
                    config_fingerprint="a",
                    now=120,
                )["allowed"])
                preview = store.circuit_permission(
                    series_key="sonarr:1",
                    series_title="Top Gear",
                    config_fingerprint="a",
                    claim=False,
                    now=200,
                )
                self.assertTrue(preview["allowed"])
                self.assertEqual(preview["state"], "half_open")
                second_preview = store.circuit_permission(
                    series_key="sonarr:1",
                    series_title="Top Gear",
                    config_fingerprint="a",
                    claim=False,
                    now=201,
                )
                self.assertTrue(second_preview["allowed"])
                trial = store.circuit_permission(
                    series_key="sonarr:1",
                    series_title="Top Gear",
                    config_fingerprint="a",
                    now=200,
                )
                self.assertTrue(trial["allowed"])
                self.assertEqual(trial["state"], "half_open")
                self.assertFalse(store.circuit_permission(
                    series_key="sonarr:1",
                    series_title="Top Gear",
                    config_fingerprint="a",
                    now=201,
                )["allowed"])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
