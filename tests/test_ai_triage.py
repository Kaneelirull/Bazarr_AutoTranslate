import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "docker"))

from autotranslate.manual_review import ManualReviewService  # noqa: E402
from autotranslate.manual_review.ai_approval import (  # noqa: E402
    AISettings,
    AITriageEngine,
    AITriageError,
    _parse_response,
)
from autotranslate.persistence.state_store import StateStore  # noqa: E402


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class AITriageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = StateStore(
            self.root / "state.sqlite3", validator_version="v", config_fingerprint="c"
        )
        self.addCleanup(self.store.close)
        self.source = self.root / "episode.en.srt"
        self.candidate = self.root / "candidate.et.srt"
        self.source.write_text("source evidence", encoding="utf-8")
        self.candidate.write_text("candidate evidence", encoding="utf-8")
        self.plan, _ = self.store.schedule_retry_plan(
            item_type="episodes", item_id=1, source_language="en", target_language="et",
            source_path=self.source, source_hash=file_hash(self.source),
            target_path=self.root / "episode.et.srt", series_key="sonarr:1",
            failure_class="whole_file", rules=["copied_source"],
            state="regeneration_waiting", artifact_path=self.candidate,
            failed_output_hash=file_hash(self.candidate), eligible_completed_cycle=0,
        )
        self.store.hold_retry_for_review(self.plan["id"], "unresolved_cues")
        self.cues = [
            {
                "cueNumber": 1, "timestamp": "00:00:01,000 --> 00:00:02,000",
                "sourceText": "You, Christian Bale, Nick Cage, Sienna Miller.",
                "targetText": "Du, Christian Bale, Nick Cage, Sienna Miller.",
                "sourceCueHash": "a" * 64, "targetCueHash": "b" * 64,
                "rules": ["copied_source"], "reason": "source text was copied",
                "canApproveCue": True, "canApproveName": False, "context": [],
            },
            {
                "cueNumber": 2, "timestamp": "00:00:03,000 --> 00:00:04,000",
                "sourceText": "Meet me in Old Town.", "targetText": "Kohtume, Old Town.",
                "sourceCueHash": "c" * 64, "targetCueHash": "d" * 64,
                "rules": ["ambiguous_copied_source"], "reason": "possible place name",
                "canApproveCue": True, "canApproveName": True, "context": [],
            },
        ]

    def service(self, settings=None):
        engine = AITriageEngine(
            self.store,
            settings or AISettings("http://ollama:11434", "test-model", format_retries=0, job_retries=0),
        )
        return ManualReviewService(
            self.store, managed_roots=[self.root], quarantine_root=self.root / "q",
            validate=lambda *_args: None, inspect_cues=lambda *_args: [dict(value) for value in self.cues],
            ai_triage_engine=engine,
        )

    @staticmethod
    def response(cue_numbers=(1, 2)):
        return json.dumps({
            "version": 1,
            "cues": [
                {"cueNumber": number, "verdict": "approve", "confidence": 0.96,
                 "reason": "The unchanged text consists of names or places."}
                for number in cue_numbers
            ],
            "names": [
                {"name": "Christian Bale", "confidence": 0.98, "kind": "name"},
                {"name": "Nick Cage", "confidence": 0.97, "kind": "name"},
                {"name": "Sienna Miller", "confidence": 0.99, "kind": "name"},
                {"name": "Old Town", "confidence": 0.95, "kind": "place"},
            ],
        })

    def payload(self, service):
        detail = service.review_cues(self.plan["id"], 1, 100)
        return {
            "action": "ai_triage", "requestId": "ai_triage_request_0001",
            **{key: detail[key] for key in (
                "expectedUpdatedAt", "decisionRevision", "sourceHash", "candidateHash"
            )},
        }

    def test_strict_contract_rejects_prose_duplicates_and_missing_cues(self):
        with self.assertRaises(ValueError):
            _parse_response("Here is JSON: {}", {1})
        duplicate = json.loads(self.response((1, 1)))
        with self.assertRaises(ValueError):
            _parse_response(json.dumps(duplicate), {1})
        with self.assertRaises(ValueError):
            _parse_response(self.response((1,)), {1, 2})

    def test_missing_cue_retries_three_total_format_attempts(self):
        service = self.service(AISettings(
            "http://ollama:11434", "test-model", format_retries=2, job_retries=0
        ))
        with patch(
            "autotranslate.manual_review.ai_approval._request_ollama",
            side_effect=[self.response((1,)), self.response((1,)), self.response()],
        ) as request:
            status, result = service.perform_cue_action(self.plan["id"], self.payload(service))
        self.assertEqual((status, result["outcome"], result["suggestionCount"]), (200, "suggested", 2))
        self.assertEqual(request.call_count, 3)
        self.assertEqual([call.args[1] for call in request.call_args_list], [True, False, False])

    def test_success_is_durable_idempotent_and_does_not_change_review_state(self):
        service = self.service()
        before = self.store.retry_plan(self.plan["id"])
        payload = self.payload(service)
        with patch("autotranslate.manual_review.ai_approval._request_ollama", return_value=self.response()) as request:
            first = service.perform_cue_action(self.plan["id"], payload)
            replay = service.perform_cue_action(self.plan["id"], payload)
        self.assertEqual(first[0], 200)
        self.assertEqual(replay[1]["outcome"], "suggested")
        self.assertEqual(request.call_count, 1)
        after = self.store.retry_plan(self.plan["id"])
        self.assertEqual(after["updatedAt"], before["updatedAt"])
        self.assertEqual(self.store.cue_decision_snapshot(after)["decisions"], [])
        detail = service.review_cues(self.plan["id"], 1, 100)
        self.assertEqual([cue["ai_triage"]["verdict"] for cue in detail["items"]], ["approve", "approve"])
        self.assertEqual(len(self.store.ai_name_confidence_snapshot()), 4)
        cue = detail["items"][0]
        service.perform_cue_action(self.plan["id"], {
            "action": "approve_cue", "requestId": "approve_after_ai_0001",
            **{key: detail[key] for key in (
                "expectedUpdatedAt", "decisionRevision", "sourceHash", "candidateHash"
            )},
            "cueNumber": cue["cueNumber"], "targetCueHash": cue["targetCueHash"],
            "rememberPhrase": False,
        })
        decision = self.store.cue_decision_snapshot(self.store.retry_plan(self.plan["id"]))["decisions"][0]
        self.assertEqual((decision["ai_verdict"], decision["ai_model"]), ("approve", "test-model"))

    def test_network_failure_persists_nothing(self):
        service = self.service(AISettings(
            "http://ollama:11434", "test-model", format_retries=0, job_retries=2
        ))
        before = self.store.retry_plan(self.plan["id"])
        with patch(
            "autotranslate.manual_review.ai_approval._request_ollama",
            side_effect=requests.ConnectionError("offline"),
        ) as request, self.assertRaises(AITriageError):
            service.perform_cue_action(self.plan["id"], self.payload(service))
        self.assertEqual(request.call_count, 3)
        after = self.store.retry_plan(self.plan["id"])
        self.assertEqual(after["updatedAt"], before["updatedAt"])
        self.assertEqual(self.store.ai_triage_snapshot(
            self.plan["id"], file_hash(self.source), file_hash(self.candidate)
        ), {})
        self.assertEqual(self.store.ai_name_confidence_snapshot(), {})
        self.assertEqual(self.store._fetchone(
            "SELECT COUNT(*) FROM manual_review_action_requests"
        )[0], 0)

    def test_name_confidence_cache_skips_model_call(self):
        now = 1.0
        for name in ("christian bale", "nick cage", "sienna miller", "old town"):
            self.store._connection.execute(
                """INSERT INTO ai_name_confidences(
                       name,confidence,reason,model,times_seen,first_seen_at,last_seen_at)
                   VALUES(?,?,?,?,1,?,?)""",
                (name, 0.97, "cached", "old-model", now, now),
            )
        engine = AITriageEngine(self.store, AISettings("http://ollama:11434", "test-model"))
        with patch("autotranslate.manual_review.ai_approval._request_ollama") as request:
            suggestions, names = engine.triage(self.cues, [])
        self.assertEqual([value["verdict"] for value in suggestions], ["approve", "approve"])
        self.assertEqual(names, [])
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
