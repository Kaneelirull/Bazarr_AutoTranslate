import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "docker"))

from autotranslate.persistence.state_store import StateStore  # noqa: E402


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



if __name__ == "__main__":
    unittest.main()
