import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "docker"))

from autotranslate.config import Config, ConfigError  # noqa: E402
from autotranslate.lifecycle import LifecycleController  # noqa: E402
from autotranslate.models import MaintenanceResult  # noqa: E402
from autotranslate.services.lingarr import (  # noqa: E402
    ProviderResponseError,
    parse_cue_response,
)
from clean_et_subs import (  # noqa: E402
    build_detector,
    cue_source_signature,
    parse_srt_cues,
    repair_subtitle_file,
    target_language_for_code,
)
from state_store import StateStore  # noqa: E402


def make_srt(*texts: str) -> str:
    return "\n\n".join(
        f"{index}\n00:00:{index:02d},000 --> 00:00:{index:02d},900\n{text}"
        for index, text in enumerate(texts, start=1)
    ) + "\n"


class ArchitectureUpgradeTests(unittest.TestCase):
    def test_typed_config_preserves_required_inputs_and_shutdown_default(self):
        config = Config.from_env({
            "BAZARR_URL": "bazarr:6767/",
            "BAZARR_API_KEY": "secret",
            "LINGARR_URL": "http://lingarr:8080/",
        })
        self.assertEqual(config.bazarr_url, "http://bazarr:6767")
        self.assertEqual(config.languages, ("en", "et", "sv"))
        self.assertEqual(config.repair_shutdown_grace_seconds, 30)
        with self.assertRaises(ConfigError):
            Config.from_env({"LINGARR_URL": "lingarr:8080"})

    def test_lingarr_cue_parser_accepts_contract_and_classifies_shape(self):
        self.assertEqual(parse_cue_response(" Tere "), "Tere")
        for key in ("translatedSubtitle", "translatedLine", "translation", "text"):
            self.assertEqual(parse_cue_response({key: " Tere "}), "Tere")
        with self.assertRaises(ProviderResponseError) as raised:
            parse_cue_response({"result": {"text": "private dialogue"}})
        self.assertEqual(raised.exception.shape, {"result": "dict"})
        self.assertNotIn("private dialogue", str(raised.exception.shape))

    def test_cycle_persists_health_before_maintenance_and_full_cooldown(self):
        events = []
        controller = LifecycleController(
            run_cycle=lambda cycle: events.append(("cycle", cycle)) or True,
            advance_completed_cycle=lambda: events.append(("advance", 1)) or 1,
            run_maintenance=lambda: (
                events.append(("maintenance", None))
                or MaintenanceResult(False, ("retention",), ("retention",))
            ),
            set_phase=lambda phase, **_kwargs: events.append(("phase", phase)),
            refresh_diagnostics=lambda: events.append(("diagnostics", None)),
            sleep_interruptibly=lambda seconds: events.append(("sleep", seconds)) or False,
            check_interval=1200,
        )
        healthy, maintenance = controller.run_iteration(7)
        self.assertTrue(healthy)
        self.assertFalse(maintenance.healthy)
        self.assertLess(events.index(("advance", 1)), events.index(("maintenance", None)))
        self.assertEqual(events[-1], ("sleep", 1200))

    def test_schema_ledger_is_additive_and_legacy_marker_stays_rollback_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            store = StateStore(database, validator_version="v2", config_fingerprint="cfg")
            try:
                versions = {
                    row[0] for row in store._connection.execute(
                        "SELECT version FROM schema_migrations"
                    )
                }
                self.assertTrue({9, 10, 11, 12, 13}.issubset(versions))
                self.assertEqual(store._connection.execute("PRAGMA user_version").fetchone()[0], 8)
                tables = {
                    row[0] for row in store._connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertTrue({
                    "maintenance_runs", "repair_jobs", "partial_candidates",
                    "cue_recoveries", "legacy_quarantine_index", "donor_events",
                    "failure_fingerprints", "retry_admission_events", "provider_events",
                }.issubset(tables))
                self.assertEqual(store._connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
                self.assertEqual(store._connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            finally:
                store.close()

    def test_partial_repair_keeps_successful_cue_without_publishing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "episode.eng.srt"
            target = root / "episode.et.srt"
            source.write_text(make_srt("First source", "Second source"), encoding="utf-8")
            original = make_srt(
                "[SOURCE] first leak [/SOURCE]",
                "[SOURCE] second leak [/SOURCE]",
            )
            target.write_text(original, encoding="utf-8")

            def translator(line, _before, _after):
                return (
                    "Teine parandatud rida"
                    if line == "Second source"
                    else "[SOURCE] still leaked [/SOURCE]"
                )

            result = repair_subtitle_file(
                source,
                target,
                build_detector(),
                target_language_for_code("et"),
                translator,
                target_lang="et",
                max_attempts=2,
            )
            self.assertFalse(result.success)
            self.assertEqual(result.repaired_cues, [2])
            self.assertEqual(result.unresolved_cues, [1])
            self.assertIsNotNone(result.partial_raw)
            partial, errors = parse_srt_cues(result.partial_raw)
            self.assertEqual(errors, [])
            self.assertEqual(partial[1].text, "Teine parandatud rida")
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_cue_recovery_is_durable_and_private(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(
                Path(directory) / "state.sqlite3",
                validator_version="validator-v2",
                config_fingerprint="config-v2",
            )
            try:
                candidate_id = store.record_partial_candidate(
                    item_type="episodes", item_id=42, target_language="et",
                    source_hash="source", target_hash="partial", changed_cues=[338],
                    unresolved_cues=[176], provenance=[{"cueNumber": 338}],
                    artifact_path=Path(directory) / "private.srt",
                )
                store.record_cue_recovery(
                    item_type="episodes", item_id=42, target_language="et",
                    source_file_hash="source", source_cue_number=338,
                    source_cue_hash="cue-source", source_signature={"number": 338},
                    target_text="Salajane parandatud dialoog", target_hash="cue-target",
                    recovery_stage="context_free", partial_candidate_id=candidate_id,
                )
                recovered = store.cue_recoveries(
                    "episodes", 42, "et", source_file_hash="source"
                )
                self.assertEqual(recovered[0]["sourceCueNumber"], 338)
                self.assertEqual(recovered[0]["targetText"], "Salajane parandatud dialoog")
                public = json.dumps(store.diagnostic_aggregates())
                self.assertNotIn("Salajane parandatud dialoog", public)
                self.assertNotIn("private.srt", public)
            finally:
                store.close()

    def test_exhausted_strategies_enter_manual_review_without_provider_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "episode.eng.srt"
            target = root / "episode.et.srt"
            source.write_text(make_srt("Only source cue"), encoding="utf-8")
            target.write_text(
                make_srt("[SOURCE] leaked [/SOURCE]"), encoding="utf-8"
            )
            source_cues, errors = parse_srt_cues(source.read_text(encoding="utf-8"))
            self.assertEqual(errors, [])
            cue_hash = cue_source_signature(source_cues[0])["sourceHash"]
            calls = []
            result = repair_subtitle_file(
                source,
                target,
                build_detector(),
                target_language_for_code("et"),
                lambda *_args: calls.append(True) or "unused",
                target_lang="et",
                max_attempts=3,
                exhausted_strategies={cue_hash: {"context_free", "strict_isolated"}},
            )
            self.assertFalse(result.success)
            self.assertTrue(result.manual_review)
            self.assertEqual(result.attempts, 0)
            self.assertEqual(calls, [])

    def test_manual_review_is_not_due_until_configuration_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            try:
                plan, _ = store.schedule_retry_plan(
                    item_type="episodes", item_id=5, target_language="et",
                    source_hash="source", failure_class="whole_file",
                    rules=["garbage"], state="regeneration_waiting",
                    failed_output_hash="bad", eligible_completed_cycle=0,
                )
                store.record_failure_fingerprint(
                    item_type="episodes", item_id=5, target_language="et",
                    source_file_hash="source", source_cue_hash="cue",
                    strategy_key="strict_isolated", provider="lingarr",
                    config_fingerprint="old-config", output_fingerprint="same",
                    failure_class="garbage",
                )
                store.reschedule_retry_no_progress(
                    plan["id"], completed_cycle=0,
                    deferral_class="manual_review", reason="exhausted",
                )
                self.assertEqual(store.due_retry_count(99), 0)
                self.assertEqual(
                    store.reactivate_changed_manual_reviews("old-config"), 0
                )
                self.assertEqual(
                    store.reactivate_changed_manual_reviews("new-config"), 1
                )
                self.assertEqual(store.due_retry_count(99), 1)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
