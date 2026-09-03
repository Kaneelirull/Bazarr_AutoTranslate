import hashlib
import json
import sqlite3
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "docker"))

from autotranslate.manual_review import (  # noqa: E402
    ManualReviewConflict,
    ManualReviewDisabled,
    ManualReviewService,
    ManualReviewUnavailable,
    RecheckResult,
)
from autotranslate.persistence.common import SCHEMA_VERSION  # noqa: E402
from autotranslate.persistence.common import StateStoreError  # noqa: E402
from autotranslate.persistence.state_store import StateStore  # noqa: E402
from autotranslate.scheduling.locks import ArtifactAccessCoordinator  # noqa: E402
from autotranslate.services.bazarr import BazarrClient  # noqa: E402
from autotranslate.status.server import start_status_server  # noqa: E402
from autotranslate.status.tracker import StatusTracker  # noqa: E402


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ManualReviewTests(unittest.TestCase):
    def make_store(self, root: Path) -> StateStore:
        return StateStore(root / "state.sqlite3", validator_version="v", config_fingerprint="c")

    def make_plan(self, store: StateStore, root: Path, *, failed_hash: str = "failed") -> dict:
        source = root / "Top Gear S14E01.en.srt"
        target = root / "Top Gear S14E01.et.srt"
        source.write_text("source", encoding="utf-8")
        target.write_text("target", encoding="utf-8")
        plan, _ = store.schedule_retry_plan(
            item_type="episodes", item_id=443, target_language="et",
            source_hash=file_hash(source), source_path=source,
            source_language="en", target_path=target,
            series_key="sonarr:77", series_title="Top Gear",
            media_title="Top Gear S14E01", failure_class="whole_file",
            rules=["copied_source"], state="regeneration_waiting",
            failed_output_hash=failed_hash, eligible_completed_cycle=3,
        )
        return store.reschedule_retry_no_progress(
            plan["id"], completed_cycle=3, deferral_class="manual_review",
            reason="no materially new recovery strategy remains",
        )

    def make_service(self, store, root, *, valid=True, scans=None, enabled=True, seen=None):
        scans = scans if scans is not None else []
        seen = seen if seen is not None else []

        def validate(plan, source, target, media):
            seen.append((source, target, media))
            return RecheckResult(
                valid, "valid_with_warnings" if valid else "invalid",
                "validation_passed" if valid else "copied_source",
                details={"issueRules": [] if valid else ["copied_source"], "observationCount": 1 if valid else 0},
            )

        return ManualReviewService(
            store, managed_roots=[root], quarantine_root=root / "quarantine",
            artifact_access=ArtifactAccessCoordinator(), validate=validate,
            dispatch_scan=lambda plan: scans.append(plan["id"]) or True,
            completed_cycle=lambda: 9, actions_enabled=enabled,
        )

    def test_schema_v16_preserves_v15_normalization_and_creates_scan_outbox(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            plan = self.make_plan(store, root)
            store.update_retry_plan(
                plan["id"], state="retry_exhausted",
                final_outcome="manual_review", reason="legacy terminal",
            )
            store._connection.execute("DELETE FROM schema_migrations WHERE version>=15")
            store._connection.execute("DROP TABLE manual_review_actions")
            store._connection.execute("DROP TABLE manual_review_scan_outbox")
            store._connection.execute("PRAGMA user_version=14")
            store.close()
            reopened = self.make_store(root)
            try:
                migrated = reopened.manual_review_plan(plan["id"])
                self.assertEqual(SCHEMA_VERSION, 19)
                self.assertEqual(migrated["state"], "regeneration_waiting")
                self.assertEqual(migrated["lastDeferralClass"], "manual_review")
                self.assertIsNone(migrated["finalOutcome"])
                self.assertEqual(
                    reopened._connection.execute("PRAGMA user_version").fetchone()[0], 19
                )
                self.assertIsNotNone(reopened._fetchone(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='manual_review_scan_outbox'"
                ))
            finally:
                reopened.close()

    def test_schema_v19_saves_cue_decisions_and_finish_requires_all_cues(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            try:
                plan = self.make_plan(store, root)
                cue = {"cueNumber": 815, "timestamp": "00:32:46,501 --> 00:32:48,266",
                       "sourceCueHash": "a" * 64, "targetCueHash": "b" * 64,
                       "sourceText": "[leesie] oh. Gun, billie.", "targetText": "[leesie] Åh. Gun, Billie.",
                       "rules": ["copied_source"]}
                saved = store.save_cue_decision(plan["id"], plan["updatedAt"], 0, cue=cue,
                                                decision="approve", remember_phrase=True)
                snapshot = store.cue_decision_snapshot(saved)
                self.assertEqual(snapshot["revision"], 1)
                self.assertEqual(snapshot["approved"][0]["cueNumber"], 815)
                with self.assertRaises(RuntimeError):
                    store.finish_cue_review(plan["id"], saved["updatedAt"], 1, [815, 816], 9)
                finished = store.finish_cue_review(plan["id"], saved["updatedAt"], 1, [815], 9)
                self.assertIsNone(finished["lastDeferralClass"])
                self.assertEqual(store.name_approval_snapshot(finished)["pairs"],
                                 [["[leesie] oh. gun, billie.", "[leesie] åh. gun, billie."]])
            finally:
                store.close()

    def test_ignored_review_can_be_reopened_without_queueing_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            try:
                plan = self.make_plan(store, root)
                candidate = root / "candidate.srt"
                candidate.write_text("retained", encoding="utf-8")
                publication_id = store.record_publication(target=root / "Top Gear S14E01.et.srt",
                    candidate=candidate, candidate_hash=file_hash(candidate),
                    source_path=root / "Top Gear S14E01.en.srt", source_hash=plan["sourceHash"],
                    expected_target_hash=None, payload={})
                store._connection.execute("UPDATE subtitle_publications SET state='manual_review' WHERE id=?", (publication_id,))
                ignored = store.dismiss_manual_review(plan["id"], plan["updatedAt"])
                self.assertEqual(store.publication_for_target(root / "Top Gear S14E01.et.srt")["state"], "manual_review")
                source = root / "Top Gear S14E01.en.srt"
                self.assertTrue(any(path.exists() and path.samefile(source) for path in store.protected_artifact_paths()))
                reopened = store.reopen_manual_review(plan["id"], ignored["updatedAt"])
                self.assertEqual(reopened["lastDeferralClass"], "manual_review")
                self.assertEqual(reopened["state"], "regeneration_waiting")
                self.assertEqual(store.manual_review_actions(plan["id"])[0]["action"], "reopen")
                finished = store.finish_cue_review(plan["id"], reopened["updatedAt"], 0, [], 9)
                self.assertEqual(store.publication_for_target(root / "Top Gear S14E01.et.srt")["state"], "pending")
                self.assertIsNone(finished["lastDeferralClass"])
            finally:
                store.close()

    def test_repository_queue_is_atomic_stale_safe_and_does_not_increment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            try:
                plan = self.make_plan(store, root)
                queued = store.queue_manual_retry(plan["id"], plan["updatedAt"], 9)
                self.assertEqual(queued["state"], "regeneration_waiting")
                self.assertIsNone(queued["lastDeferralClass"])
                self.assertEqual(queued["attemptCount"], plan["attemptCount"])
                self.assertEqual(queued["eligibleCompletedCycle"], 9)
                with self.assertRaises(RuntimeError):
                    store.queue_manual_retry(plan["id"], plan["updatedAt"], 9)
                actions = store.manual_review_actions(plan["id"])
                self.assertEqual([(row["action"], row["outcome"]) for row in actions], [("queue_retry", "queued")])
            finally:
                store.close()

    def test_repository_clamps_out_of_range_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            try:
                plan = self.make_plan(store, root)
                page = store.manual_review_page({"page": 99, "pageSize": 20})
                self.assertEqual(page["page"], 1)
                self.assertEqual(page["total"], 1)
                self.assertEqual([item["id"] for item in page["plans"]], [plan["id"]])
            finally:
                store.close()

    def test_dismissed_unchanged_inputs_stay_terminal_and_changed_output_reopens(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            try:
                plan = self.make_plan(store, root)
                dismissed = store.dismiss_manual_review(plan["id"], plan["updatedAt"])
                self.assertEqual(dismissed["state"], "manual_dismissed")
                unchanged, repeated = store.schedule_retry_plan(
                    item_type="episodes", item_id=443, target_language="et",
                    source_hash=plan["sourceHash"], failure_class="whole_file",
                    rules=["copied_source"], state="regeneration_waiting",
                    failed_output_hash="failed", eligible_completed_cycle=10,
                )
                self.assertTrue(repeated)
                self.assertEqual(unchanged["state"], "manual_dismissed")
                changed, repeated = store.schedule_retry_plan(
                    item_type="episodes", item_id=443, target_language="et",
                    source_hash=plan["sourceHash"], failure_class="whole_file",
                    rules=["copied_source"], state="regeneration_waiting",
                    failed_output_hash="changed", eligible_completed_cycle=10,
                )
                self.assertFalse(repeated)
                self.assertEqual(changed["state"], "regeneration_waiting")
            finally:
                store.close()

    def test_service_rechecks_under_lock_sanitizes_and_dispatches_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            scans, seen = [], []
            try:
                plan = self.make_plan(store, root)
                service = self.make_service(store, root, scans=scans, seen=seen)
                status, result = service.perform_action(plan["id"], "recheck", plan["updatedAt"])
                self.assertEqual(status, 200)
                self.assertEqual(result["outcome"], "resolved")
                self.assertFalse(result["scanPending"])
                self.assertEqual(scans, [plan["id"]])
                self.assertTrue(seen[0][0].samefile(root / "Top Gear S14E01.en.srt"))
                listing = service.list_reviews({"page": 1, "pageSize": 20})
                encoded = json.dumps(listing)
                self.assertEqual(listing["counts"]["resolved"], 1)
                self.assertEqual(
                    listing["items"][0]["validationFeedback"]["validationResult"],
                    "valid_with_warnings",
                )
                self.assertEqual(listing["items"][0]["sourceAvailabilityReason"], "available")
                self.assertEqual(listing["items"][0]["targetAvailabilityReason"], "available")
                self.assertEqual(listing["items"][0]["artifactAvailabilityReason"], "not_found")
                self.assertEqual(listing["items"][0]["mediaAvailabilityReason"], "resolver_unavailable")
                self.assertFalse(listing["items"][0]["scanPending"])
                self.assertNotIn(str(root), encoded)
                self.assertNotIn(plan["sourceHash"], encoded)
                self.assertNotIn("sourceHash", encoded)
            finally:
                store.close()

    def test_service_resolves_locks_and_sanitizes_media_completeness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            media = root / "Top Gear S14E01.mkv"
            media.write_bytes(b"media")
            locked = []
            seen = []

            class Access:
                def hold(self, *paths):
                    locked.append(paths)
                    return nullcontext()

            def validate(_plan, _source, _target, media_path):
                seen.append(media_path)
                return RecheckResult(
                    True, "valid_with_warnings", "validation_passed",
                    details={
                        "mediaAvailable": True,
                        "completeness": {
                            "evaluated": True, "undersized": False,
                            "reason": "0/3 completeness signals failed; accepted",
                            "mediaDurationSeconds": 3600.0, "subtitleBytes": 50000,
                            "cueCount": 443, "dialogueChars": 12000,
                            "cuesPerMinute": 7.383, "textCharsPerMinute": 200.0,
                            "bytesPerMinute": 833.333, "timelineCoverage": 0.98,
                            "failedSignals": [],
                            "thresholds": {"requiredSignals": 3, "secret": "drop"},
                            "absolutePath": str(root),
                        },
                        "absolutePath": str(root),
                    },
                )

            try:
                plan = self.make_plan(store, root)
                service = ManualReviewService(
                    store, managed_roots=[root], quarantine_root=root / "quarantine",
                    artifact_access=Access(), validate=validate,
                    resolve_media=lambda _plan, _target: media,
                    dispatch_scan=lambda _plan: True,
                )
                before = service.list_reviews()["items"][0]
                self.assertTrue(before["mediaAvailable"])
                self.assertEqual(before["mediaRelativePath"], media.name)
                service.perform_action(plan["id"], "recheck", plan["updatedAt"])
                self.assertEqual(seen, [media.resolve()])
                self.assertIn(media.resolve(), locked[0])
                item = service.list_reviews()["items"][0]
                completeness = item["validationFeedback"]["completeness"]
                self.assertEqual(completeness["cueCount"], 443)
                self.assertEqual(completeness["thresholds"], {"requiredSignals": 3})
                encoded = json.dumps(item)
                self.assertNotIn("absolutePath", encoded)
                self.assertNotIn(str(root), encoded)
            finally:
                store.close()

    def test_media_resolver_rejects_paths_outside_managed_roots(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            store = self.make_store(root)
            external = Path(outside) / "Top Gear S14E01.mkv"
            external.write_bytes(b"media")
            seen = []
            try:
                plan = self.make_plan(store, root)
                service = ManualReviewService(
                    store, managed_roots=[root], quarantine_root=root / "quarantine",
                    validate=lambda _plan, _source, _target, media: seen.append(media)
                    or RecheckResult(False, "invalid", "validation_failed"),
                    resolve_media=lambda _plan, _target: external,
                )
                item = service.list_reviews()["items"][0]
                self.assertFalse(item["mediaAvailable"])
                self.assertIsNone(item["mediaRelativePath"])
                service.perform_action(plan["id"], "recheck", plan["updatedAt"])
                self.assertEqual(seen, [None])
            finally:
                store.close()

    def test_media_resolver_rejects_managed_symlink_escaping_root(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            external = Path(outside) / "episode.mkv"
            external.write_bytes(b"media")
            link = root / "episode.mkv"
            try:
                link.symlink_to(external)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            store = self.make_store(root)
            try:
                self.make_plan(store, root)
                service = ManualReviewService(
                    store, managed_roots=[root], quarantine_root=root / "quarantine",
                    validate=lambda *_args: RecheckResult(False, "invalid", "validation_failed"),
                    resolve_media=lambda _plan, _target: link,
                )
                item = service.list_reviews()["items"][0]
                self.assertFalse(item["mediaAvailable"])
                self.assertIsNone(item["mediaRelativePath"])
            finally:
                store.close()

    def test_changed_source_falls_back_to_target_only_and_invalid_stays_held(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            seen = []
            try:
                plan = self.make_plan(store, root)
                Path(plan["sourcePath"]).write_text("changed", encoding="utf-8")
                service = self.make_service(store, root, valid=False, seen=seen)
                status, result = service.perform_action(plan["id"], "recheck", plan["updatedAt"])
                self.assertEqual(status, 200)
                self.assertEqual(result["outcome"], "invalid")
                self.assertIsNone(seen[0][0])
                current = store.manual_review_plan(plan["id"])
                self.assertEqual(current["lastDeferralClass"], "manual_review")
                self.assertEqual(current["state"], "regeneration_waiting")
            finally:
                store.close()

    def test_failed_scan_is_durable_and_retried_at_least_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            calls = []
            try:
                plan = self.make_plan(store, root)
                service = ManualReviewService(
                    store, managed_roots=[root], quarantine_root=root,
                    validate=lambda *_args: RecheckResult(True, "valid", "validation_passed"),
                    dispatch_scan=lambda current: calls.append(current["id"]) or len(calls) > 1,
                )
                status, result = service.perform_action(plan["id"], "recheck", plan["updatedAt"])
                self.assertEqual(status, 202)
                self.assertTrue(result["scanPending"])
                pending = store._fetchone(
                    "SELECT * FROM manual_review_scan_outbox WHERE retry_plan_id=?",
                    (plan["id"],),
                )
                self.assertEqual(pending["state"], "pending")
                self.assertEqual(
                    service.dispatch_pending_scans(now=float(pending["next_attempt_at"])),
                    {"examined": 1, "dispatched": 1},
                )
                dispatched = store._fetchone(
                    "SELECT * FROM manual_review_scan_outbox WHERE retry_plan_id=?",
                    (plan["id"],),
                )
                self.assertEqual(dispatched["state"], "dispatched")
                self.assertEqual(calls, [plan["id"], plan["id"]])
                listing = service.list_reviews()
                self.assertFalse(listing["items"][0]["scanPending"])
                self.assertEqual(listing["items"][0]["scanState"], "dispatched")
            finally:
                store.close()

    def test_current_outbox_state_overrides_historical_dispatched_action(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            try:
                plan = self.make_plan(store, root)
                service = self.make_service(store, root)
                service.perform_action(plan["id"], "recheck", plan["updatedAt"])
                with store._transaction() as db:
                    db.execute(
                        "UPDATE manual_review_scan_outbox SET state='pending', "
                        "next_attempt_at=?, last_error_code='scan_dispatch_failed' "
                        "WHERE retry_plan_id=?",
                        (time.time() + 300, plan["id"]),
                    )
                    store._insert_manual_action(
                        db, plan["id"], "bazarr_scan", "failed",
                        "scan_dispatch_failed", {}, time.time(),
                    )
                item = service.list_reviews()["items"][0]
                self.assertTrue(item["scanPending"])
                self.assertEqual(item["scanState"], "pending")
            finally:
                store.close()

    def test_post_commit_scan_persistence_and_publish_failures_remain_resolved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            emitted = []
            try:
                plan = self.make_plan(store, root)
                original_record_outcome = store.record_manual_scan_outcome
                store.record_manual_scan_outcome = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    StateStoreError("unsafe path C:/private/media.srt")
                )
                service = ManualReviewService(
                    store, managed_roots=[root], quarantine_root=root,
                    validate=lambda *_args: RecheckResult(True, "valid", "validation_passed"),
                    dispatch_scan=lambda _plan: True,
                    publish_validation=lambda *_args: (_ for _ in ()).throw(RuntimeError("publish failed")),
                    on_change=lambda: (_ for _ in ()).throw(RuntimeError("refresh failed")),
                    emit=emitted.append,
                )
                status, result = service.perform_action(plan["id"], "recheck", plan["updatedAt"])
                self.assertEqual(status, 202)
                self.assertTrue(result["scanPending"])
                self.assertEqual(store.manual_review_plan(plan["id"])["state"], "accepted_after_manual_recheck")
                self.assertIn("event=observation_publish_failed", "\n".join(emitted))
                self.assertIn("event=scan_outcome_persist_failed", "\n".join(emitted))
                self.assertIn("event=status_publish_failed", "\n".join(emitted))
                self.assertNotIn("C:/private", "\n".join(emitted))
                store.record_manual_scan_outcome = original_record_outcome
            finally:
                store.close()

    def test_successful_recheck_rolls_back_when_validation_persistence_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            try:
                plan = self.make_plan(store, root)
                service = self.make_service(store, root, valid=True)
                original_record = store.record

                def fail_record(**_values):
                    raise StateStoreError("validation write failed")

                store.record = fail_record
                with self.assertRaises(ManualReviewUnavailable):
                    service.perform_action(plan["id"], "recheck", plan["updatedAt"])
                store.record = original_record
                current = store.manual_review_plan(plan["id"])
                self.assertEqual(current["state"], "regeneration_waiting")
                self.assertEqual(current["lastDeferralClass"], "manual_review")
                self.assertIsNone(store._fetchone(
                    "SELECT * FROM manual_review_scan_outbox WHERE retry_plan_id=?",
                    (plan["id"],),
                ))
                self.assertEqual(store.manual_review_actions(plan["id"]), [])
            finally:
                store.close()

    def test_successful_recheck_rolls_back_when_exact_quarantine_resolution_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            try:
                plan = self.make_plan(store, root)
                service = self.make_service(store, root, valid=True)
                service.quarantine_identity = lambda _plan: ("episodes:443:et", "failed")
                original_resolve = store.resolve_quarantine_events
                store.resolve_quarantine_events = lambda *_args, **_kwargs: 0

                with self.assertRaises(ManualReviewUnavailable):
                    service.perform_action(plan["id"], "recheck", plan["updatedAt"])

                store.resolve_quarantine_events = original_resolve
                current = store.manual_review_plan(plan["id"])
                self.assertEqual(current["state"], "regeneration_waiting")
                self.assertEqual(current["lastDeferralClass"], "manual_review")
                self.assertIsNone(store._fetchone(
                    "SELECT * FROM manual_review_scan_outbox WHERE retry_plan_id=?",
                    (plan["id"],),
                ))
                self.assertEqual(store.manual_review_actions(plan["id"]), [])
                target = root / "Top Gear S14E01.et.srt"
                self.assertIsNone(store.matching_record(target, file_hash(target)))
            finally:
                store.close()

    def test_scan_outbox_prioritizes_new_delivery_and_recovers_expired_leases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            try:
                ids = []
                for item_id in range(14):
                    plan, _ = store.schedule_retry_plan(
                        item_type="episodes", item_id=1000 + item_id,
                        target_language="et", source_hash=f"source-{item_id}",
                        failure_class="whole_file", rules=["copied_source"],
                        state="regeneration_waiting", eligible_completed_cycle=0,
                    )
                    ids.append(plan["id"])
                with store._transaction() as db:
                    now = 1000.0
                    for index, plan_id in enumerate(ids):
                        attempts = 3 if index < 6 else 0
                        db.execute(
                            "UPDATE retry_plans SET state='accepted_after_manual_recheck', "
                            "final_outcome='accepted_after_manual_recheck' WHERE id=?",
                            (plan_id,),
                        )
                        db.execute(
                            "INSERT INTO manual_review_scan_outbox(" 
                            "retry_plan_id,state,attempt_count,next_attempt_at,created_at,updated_at" 
                            ") VALUES(?,'pending',?,?,?,?)",
                            (plan_id, attempts, now, now, now),
                        )
                claimed = store.claim_manual_scans(now=1000.0, owner="worker-a")
                claimed_ids = [plan["id"] for plan in claimed]
                self.assertEqual(len(claimed), 10)
                self.assertTrue(set(ids[6:]).issubset(claimed_ids))
                expired = store.claim_manual_scans(
                    now=1601.0, owner="worker-b", limit=10
                )
                self.assertTrue(set(claimed_ids).issubset(
                    {plan["id"] for plan in expired}
                ))
            finally:
                store.close()

    def test_quarantine_resolution_can_target_only_the_reviewed_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            try:
                identity = str(root / "Top Gear S14E01") + "|et"
                for target_hash in ("reviewed", "newer"):
                    store.record_quarantine_event(
                        identity, target_path=root / "Top Gear S14E01.et.srt",
                        target_hash=target_hash, target_language="et",
                        rules=["copied_source"], origin="test",
                        now=datetime.now(timezone.utc),
                    )
                self.assertTrue(
                    store.resolve_quarantine_events(identity, target_hash="reviewed")
                )
                self.assertIsNotNone(
                    store.quarantine_event(identity, target_hash="reviewed")["resolvedAt"]
                )
                self.assertIsNone(
                    store.quarantine_event(identity, target_hash="newer")["resolvedAt"]
                )
            finally:
                store.close()

    def test_bazarr_client_uses_item_scoped_movie_and_series_scans(self):
        calls = []

        class Response:
            status_code = 204

        client = BazarrClient(
            "http://bazarr", "secret", request_json=lambda *_args, **_kwargs: {"data": [{"sonarrSeriesId": 77}]},
            patch=lambda url, **kwargs: calls.append((url, kwargs["params"])) or Response(),
            emit=lambda _message: None,
        )
        self.assertTrue(client.trigger_item_scan("movies", 5))
        self.assertTrue(client.trigger_item_scan("episodes", 443))
        self.assertEqual(calls[0][1], {"radarrid": 5, "action": "scan-disk"})
        self.assertEqual(calls[1][1], {"seriesid": 77, "action": "scan-disk"})

    def test_http_review_routes_protect_mutations_and_return_json_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = StatusTracker(
                Path(directory) / "status.json", Path(directory) / "history.jsonl"
            )

            class Service:
                actions_enabled = True
                calls = []

                def list_reviews(self, query):
                    return {"counts": {}, "items": [], "pagination": query, "actionsEnabled": True}

                def perform_action(self, plan_id, action, expected):
                    self.calls.append((plan_id, action, expected))
                    return 200, {"outcome": "queued"}

            service = Service()
            server, thread = start_status_server(
                tracker, "127.0.0.1", 0, manual_review_service=service
            )
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with urllib.request.urlopen(f"{base}/review") as response:
                    self.assertIn(b"Manual review", response.read())
                with urllib.request.urlopen(f"{base}/api/manual-reviews?pageSize=20") as response:
                    self.assertEqual(json.loads(response.read())["items"], [])
                body = json.dumps({"action": "queue_retry", "expectedUpdatedAt": 1.5}).encode()
                request = urllib.request.Request(
                    f"{base}/api/manual-reviews/7/actions", data=body, method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(request)
                self.assertEqual(error.exception.code, 403)
                error.exception.close()
                request.add_header("X-Bazarr-Autotranslate-Action", "manual-review")
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(json.loads(response.read())["outcome"], "queued")
                self.assertEqual(service.calls, [(7, "queue_retry", 1.5)])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_http_rejects_cross_origin_non_json_and_oversized_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = StatusTracker(Path(directory) / "s.json", Path(directory) / "h.jsonl")

            class Service:
                actions_enabled = True
                def list_reviews(self, _query): return {"items": []}
                def perform_action(self, *_args): return 200, {"outcome": "queued"}

            server, thread = start_status_server(
                tracker, "127.0.0.1", 0, manual_review_service=Service()
            )
            url = f"http://127.0.0.1:{server.server_address[1]}/api/manual-reviews/1/actions"
            try:
                cases = [
                    ({"Content-Type": "text/plain", "X-Bazarr-Autotranslate-Action": "manual-review"}, b"{}", 415),
                    ({"Content-Type": "application/json", "X-Bazarr-Autotranslate-Action": "manual-review", "Sec-Fetch-Site": "cross-site"}, b"{}", 403),
                    ({"Content-Type": "application/json", "X-Bazarr-Autotranslate-Action": "manual-review"}, b"x" * 4097, 400),
                    ({"Content-Type": "application/json", "X-Bazarr-Autotranslate-Action": "manual-review"}, b'{"action":"recheck","expectedUpdatedAt":NaN}', 400),
                ]
                for headers, body, status in cases:
                    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
                    with self.assertRaises(urllib.error.HTTPError) as raised:
                        urllib.request.urlopen(request)
                    self.assertEqual(raised.exception.code, status)
                    self.assertIn("error", json.loads(raised.exception.read()))
                    raised.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_review_assets_are_semantic_responsive_and_preserve_filter_focus(self):
        source_root = REPO_ROOT / "docker" / "frontend" / "src" / "review"
        script = "\n".join(path.read_text(encoding="utf-8") for path in source_root.glob("*.tsx"))
        stylesheet = (REPO_ROOT / "docker" / "static" / "dashboard.css").read_text(encoding="utf-8")
        for text in ("X-Bazarr-Autotranslate-Action", "expectedUpdatedAt", "No manual reviews match these filters.", "Clear filters", "Technical codes", "Retries completed", "sourceAvailabilityReason", "targetAvailabilityReason", "artifactAvailabilityReason"):
            self.assertIn(text, script)
        for selector in (".review-summary", ".review-table td.cell-actions", "@media (max-width: 920px)", "@media (max-width: 440px)", ".review-managed-path"):
            self.assertIn(selector, stylesheet)
        self.assertFalse((REPO_ROOT / "docker" / "static" / "review.js").exists())

    def test_production_composition_injects_manual_review_service(self):
        host = (REPO_ROOT / "docker" / "autotranslate" / "production.py").read_text(encoding="utf-8")
        startup = (REPO_ROOT / "docker" / "autotranslate" / "startup.py").read_text(encoding="utf-8")
        server = (REPO_ROOT / "docker" / "autotranslate" / "status" / "server.py").read_text(encoding="utf-8")
        self.assertIn("manual_review_runtime", host)
        self.assertIn("build_manual_review_service(state_store)", startup)
        self.assertIn("manual_review_service=_runtime._manual_review_service", startup)
        self.assertNotIn("composition", server)
        cycle = (REPO_ROOT / "docker" / "autotranslate" / "scheduling" / "runtime.py").read_text(encoding="utf-8")
        self.assertNotIn("final_outcome='manual_review'", cycle)

    def test_disabled_service_rejects_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            try:
                plan = self.make_plan(store, root)
                service = self.make_service(store, root, enabled=False)
                listing = service.list_reviews()
                self.assertFalse(listing["actionsEnabled"])
                self.assertEqual(
                    listing["items"][0]["allowedActions"],
                    ["recheck", "queue_retry", "dismiss"],
                )
                with self.assertRaises(ManualReviewDisabled):
                    service.perform_action(plan["id"], "dismiss", plan["updatedAt"])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
