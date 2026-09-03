import json
import ast
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import os
from concurrent.futures import Future
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "docker"))

from autotranslate.config import Config, ConfigError  # noqa: E402
from autotranslate.app import build_application  # noqa: E402
import autotranslate.composition as composition  # noqa: E402
from autotranslate.lifecycle import LifecycleController, ShutdownController  # noqa: E402
from autotranslate.persistence.migrations import LATEST_SCHEMA_VERSION  # noqa: E402
from autotranslate.scheduling.locks import KeyedLockRegistry  # noqa: E402
from autotranslate.scheduling.locks import ArtifactAccessCoordinator  # noqa: E402
from autotranslate.scheduling.repairs import RepairCoordinator  # noqa: E402
from autotranslate.scheduling.retries import RetryQueueProcessor  # noqa: E402
from autotranslate.scheduling.executor import (  # noqa: E402
    DaemonExecutor,
    completed_futures,
)
from autotranslate.models import MaintenanceResult  # noqa: E402
from autotranslate.maintenance.coordinator import (  # noqa: E402
    MaintenanceCoordinator,
    MaintenanceOperation,
)
from autotranslate.status.facade import sanitize_public  # noqa: E402
from autotranslate.subtitles.library import purge_old_files  # noqa: E402
from autotranslate.services.lingarr import (  # noqa: E402
    LingarrClient,
    ProviderResponseError,
    parse_cue_response,
)
from autotranslate.subtitles.library import (  # noqa: E402
    build_detector,
    cue_source_signature,
    parse_srt_cues,
    repair_subtitle_file,
    target_language_for_code,
)
from autotranslate.persistence.state_store import StateStore  # noqa: E402


def make_srt(*texts: str) -> str:
    return "\n\n".join(
        f"{index}\n00:00:{index:02d},000 --> 00:00:{index:02d},900\n{text}"
        for index, text in enumerate(texts, start=1)
    ) + "\n"


class ArchitectureUpgradeTests(unittest.TestCase):
    def test_package_imports_have_no_process_stream_or_thread_side_effects(self):
        before_threads = {thread.ident for thread in threading.enumerate()}
        stdout, stderr = sys.stdout, sys.stderr
        __import__("autotranslate.app")
        __import__("autotranslate.composition")
        self.assertIs(sys.stdout, stdout)
        self.assertIs(sys.stderr, stderr)
        self.assertEqual(
            {thread.ident for thread in threading.enumerate()}, before_threads
        )

    def test_runtime_log_path_delegates_to_application_logging_resource(self):
        current = Path("/logs/current.log")
        original = composition._logging_resource
        try:
            composition._logging_resource = None
            self.assertIsNone(composition.runtime.current_log_path)
            composition._logging_resource = type(
                "LoggingResource", (), {"current_path": current}
            )()
            self.assertEqual(composition.runtime.current_log_path, current)
        finally:
            composition._logging_resource = original

    def test_only_typed_config_reads_environment(self):
        package_root = REPO_ROOT / "docker" / "autotranslate"
        for path in package_root.rglob("*.py"):
            if path.name == "config.py":
                continue
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("os.getenv", source, str(path))
            self.assertNotIn("os.environ", source, str(path))

    def test_both_existing_output_paths_use_retry_identity_settlement(self):
        path = REPO_ROOT / "docker" / "autotranslate" / "items_workflow.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_resolve_existing_retry_success"
        ]
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(len(call.args) == 3 for call in calls))

    def test_startup_reenables_stale_end_cycle_repairs_before_recovery(self):
        source = (
            REPO_ROOT / "docker" / "autotranslate" / "startup.py"
        ).read_text(encoding="utf-8")
        recovery = source.index("recover_stale_end_cycle_repair_attempts()")
        repairs = source.index("recover_repair_jobs()")
        self.assertLess(recovery, repairs)
        self.assertIn("stale end-cycle repair(s)", source)

    def test_production_runtime_has_no_unbounded_standard_executor(self):
        runtime = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (REPO_ROOT / "docker" / "autotranslate").rglob("*.py")
            if path.name != "executor.py"
        )
        self.assertNotIn("ThreadPoolExecutor", runtime)
        self.assertNotIn("as_completed", runtime)

    def test_daemon_executor_drain_is_interruptible(self):
        release = threading.Event()
        executor = DaemonExecutor(1, "shutdown-test")
        future = executor.submit(release.wait)
        started = time.monotonic()
        completed = list(completed_futures(
            [future], stop_requested=lambda: True, poll_seconds=0.01
        ))
        elapsed = time.monotonic() - started
        executor.shutdown(wait=False, cancel_futures=True)
        release.set()
        self.assertEqual(completed, [])
        self.assertLess(elapsed, 0.5)

    def test_daemon_executor_join_obeys_shared_deadline(self):
        release = threading.Event()
        running = threading.Event()
        executor = DaemonExecutor(1, "deadline-test")
        executor.submit(lambda: running.set() or release.wait())
        self.assertTrue(running.wait(1))
        started = time.monotonic()
        executor.shutdown(wait=True, cancel_futures=True, timeout=0.02)
        elapsed = time.monotonic() - started
        release.set()
        self.assertLess(elapsed, 0.5)

    def test_zero_retry_budget_never_claims_work(self):
        calls = []
        RetryQueueProcessor(
            batch_size=5,
            run_batch=lambda *_args: calls.append(True) or (1, 1),
            shutdown_requested=lambda: False,
        ).process({}, submission_budget=0)
        self.assertEqual(calls, [])

    def test_lingarr_client_rejects_malformed_collection_shapes(self):
        class Response:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return []

        events = []
        client = LingarrClient(
            "http://lingarr",
            {},
            request_json=lambda *_args, **_kwargs: [],
            get=lambda *_args, **_kwargs: Response(),
            post=lambda *_args, **_kwargs: Response(),
            connect_timeout=10,
            emit=events.append,
        )
        self.assertEqual(client.media_cache(), ({}, {}))
        self.assertIsNone(client.get_job(7))
        self.assertTrue(any("response must be an object" in event for event in events))

    def test_status_facade_redacts_private_keys_paths_and_objects(self):
        safe = sanitize_public({
            "source_path": r"C:\\media\\private.srt",
            "requestBody": {"subtitleLine": "private dialogue"},
            "reason": RuntimeError("secret response"),
            "metrics": {"completed": 2},
        })
        encoded = json.dumps(safe)
        self.assertNotIn("private.srt", encoded)
        self.assertNotIn("private dialogue", encoded)
        self.assertNotIn("secret response", encoded)
        self.assertEqual(safe["metrics"]["completed"], 2)

    def test_maintenance_stops_admitting_operations_after_shutdown(self):
        events = []
        stopped = {"value": False}

        def first():
            events.append("first")
            stopped["value"] = True
            return {}

        coordinator = MaintenanceCoordinator(
            (
                MaintenanceOperation("first", lambda: True, first, lambda: None),
                MaintenanceOperation(
                    "second", lambda: True,
                    lambda: events.append("second") or {}, lambda: None,
                ),
            ),
            stop_requested=lambda: stopped["value"],
        )
        result = coordinator.run_due()
        self.assertEqual(events, ["first"])
        self.assertEqual(result.attempted, ("first",))

    def test_keyed_lock_registry_evicts_unique_paths(self):
        registry = KeyedLockRegistry()
        for index in range(1000):
            with registry.hold(f"/media/episode-{index}.et.srt"):
                self.assertGreaterEqual(registry.size, 1)
        self.assertEqual(registry.size, 0)

    def test_retention_waits_for_active_artifact_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "donor.srt"
            artifact.write_text("private donor", encoding="utf-8")
            coordinator = ArtifactAccessCoordinator()
            started = threading.Event()
            finished = threading.Event()

            def retain():
                started.set()
                purge_old_files(
                    directory,
                    1,
                    now_timestamp=time.time() + (2 * 86400),
                    access_coordinator=coordinator,
                )
                finished.set()

            with coordinator.hold(artifact):
                worker = threading.Thread(target=retain)
                worker.start()
                self.assertTrue(started.wait(1))
                self.assertFalse(finished.wait(0.05))
                self.assertTrue(artifact.exists())
            worker.join(1)
            self.assertTrue(finished.is_set())
            self.assertFalse(artifact.exists())
            self.assertEqual(coordinator.registry_size, 0)

    def test_repair_coordinator_persists_before_registration_and_dedupes(self):
        events = []

        class State:
            def enqueue_repair_job(self, **values):
                events.append(("persist", values["dedupe_key"]))
                return 7

        repairs = RepairCoordinator(state_provider=State)
        self.assertTrue(repairs.reserve(("one",)))
        self.assertFalse(repairs.reserve(("one",)))
        self.assertEqual(
            repairs.persist(dedupe_key="one", target_language="et"), 7
        )
        first = Future()
        second = Future()
        first.set_result("one")
        second.set_result("two")
        repairs.register(first, {"key": ("one",)})
        self.assertTrue(repairs.reserve(("two",)))
        repairs.register(second, {"key": ("two",)})
        completed = list(
            repairs.completed(
                [first, second], stop_requested=lambda: False
            )
        )
        self.assertEqual(set(completed), {first, second})
        repairs.take(first)
        repairs.take(second)
        self.assertEqual(repairs.active_count, 0)
        self.assertEqual(repairs.keys, set())
        self.assertEqual(events, [("persist", "one")])

    def test_repair_coordination_adoption_rejects_published_or_unpersisted_work(self):
        repairs = RepairCoordinator()
        published = Future()
        published_metadata = {
            "key": ("published",), "retry_plan_id": 1,
            "trial_generation": 1, "status_lock": threading.Lock(),
            "status_published": True,
        }
        repairs.register(published, published_metadata)
        self.assertIsNone(repairs.adopt_coordination(
            ("published",), {"retry_plan_id": 1, "trial_generation": 2},
            persist=lambda _metadata: True,
        ))
        self.assertEqual(published_metadata["trial_generation"], 1)

        active = Future()
        active_metadata = {
            "key": ("active",), "retry_plan_id": 1,
            "trial_generation": 1, "status_lock": threading.Lock(),
            "status_published": False, "durable_job_id": 7,
        }
        repairs.register(active, active_metadata)
        self.assertIsNone(repairs.adopt_coordination(
            ("active",), {"retry_plan_id": 1, "trial_generation": 2},
            persist=lambda _metadata: False,
        ))
        self.assertEqual(active_metadata["trial_generation"], 1)

    def test_removed_import_surfaces_are_absent_and_bootstraps_are_executable_only(self):
        docker_root = REPO_ROOT / "docker"
        for name in {"Bazarr_AutoTranslate", "clean_et_subs"}:
            wrapper = docker_root / f"{name}.py"
            self.assertLessEqual(
                len(wrapper.read_text(encoding="utf-8").splitlines()), 20, wrapper
            )
        for name in {"state_store", "status_dashboard", "media_identity"}:
            self.assertFalse((docker_root / f"{name}.py").exists())
        legacy_names = {"Bazarr_AutoTranslate", "clean_et_subs", "state_store", "status_dashboard", "media_identity"}
        for module in (docker_root / "autotranslate").rglob("*.py"):
            tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported = {alias.name.split(".", 1)[0] for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    imported = {(node.module or "").split(".", 1)[0]}
                else:
                    continue
                self.assertTrue(
                    imported.isdisjoint(legacy_names),
                    f"{module} imports outward compatibility module(s): {imported}",
                )

    def test_production_composition_does_not_delegate_to_compatibility_runtime(self):
        package_root = REPO_ROOT / "docker" / "autotranslate"
        app_source = (package_root / "app.py").read_text(encoding="utf-8")
        host_source = (package_root / "production.py").read_text(encoding="utf-8")
        self.assertIn("from .production import ProductionRuntimeHost", app_source)
        self.assertNotIn("sys.modules", host_source)
        self.assertEqual(list(package_root.glob("runtime*.py")), [])
        for removed in (
            package_root / "runtime.py", package_root / "runtime_context.py",
            package_root / "status" / "dashboard.py",
            package_root / "subtitles" / "core.py",
            package_root / "subtitles" / "validation.py",
        ):
            self.assertFalse(removed.exists(), removed)

    def test_embedded_source_preparation_is_wired_into_production_item_workflow(self):
        package_root = REPO_ROOT / "docker" / "autotranslate"
        production_source = (package_root / "production.py").read_text(encoding="utf-8")
        item_source = (package_root / "items_workflow.py").read_text(encoding="utf-8")
        source_module = package_root / "subtitles" / "sources.py"
        self.assertTrue(source_module.is_file())
        self.assertIn("from . import items_workflow", production_source)
        self.assertIn("discover_extracted_sources", item_source)
        self.assertIn("prepare_extracted_source", item_source)
        self.assertIn("_publish_canonical_target", item_source)
        for path in package_root.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            self.assertNotRegex(source, r"(?m)^_runtime\.[A-Za-z_]\w*\s*=", str(path))

    def test_subtitle_tests_patch_package_owned_ownership_boundaries(self):
        for name in ("test_subtitle_validation.py", "test_existing_cleanup_pipeline.py"):
            source = (REPO_ROOT / "tests" / name).read_text(encoding="utf-8")
            self.assertNotRegex(
                source,
                r'patch\.object\(\s*cleanup,\s*"normalize_managed_file"',
            )
            self.assertIn("subtitle_foundation", source)
            self.assertIn("subtitle_repair", source)

    def test_release_smoke_check_uses_package_owned_imports(self):
        workflow = (
            REPO_ROOT / ".github" / "workflows" / "docker-build.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "import autotranslate.media_identity, autotranslate.status.server",
            workflow,
        )
        self.assertNotIn("import media_identity, status_dashboard", workflow)
        self.assertIn("hasattr(ApplicationLogging, 'current_path')", workflow)
        self.assertIn(
            "'_app_log_sink' not in run_retention_housekeeping.__code__.co_names",
            workflow,
        )

    def test_startup_tracks_lifecycle_sync_and_retention_work(self):
        source = (
            REPO_ROOT / "docker" / "autotranslate" / "startup.py"
        ).read_text(encoding="utf-8")
        self.assertIn("startup_job_id = _runtime._status_create_maintenance", source)
        self.assertIn("_runtime._tracked_bazarr_sync", source)
        self.assertIn("_runtime._run_retention_housekeeping_tracked", source)
        self.assertNotIn("_runtime.trigger_bazarr_sync(True, True)", source)

    def test_typed_config_preserves_required_inputs_and_shutdown_default(self):
        config = Config.from_env({
            "BAZARR_URL": "bazarr:6767/",
            "BAZARR_API_KEY": "secret",
            "LINGARR_URL": "http://lingarr:8080/",
        })
        self.assertEqual(config.bazarr_url, "http://bazarr:6767")
        self.assertEqual(config.languages, ("en", "et", "sv"))
        self.assertEqual(config.maintenance_workers, 4)
        self.assertEqual(config.repair_shutdown_grace_seconds, 30)
        self.assertTrue(config.status_manual_actions_enabled)
        read_only = Config.from_env({
            "BAZARR_URL": "bazarr:6767", "BAZARR_API_KEY": "secret",
            "LINGARR_URL": "lingarr:8080",
            "STATUS_MANUAL_ACTIONS_ENABLED": "false",
        })
        self.assertFalse(read_only.status_manual_actions_enabled)
        with self.assertRaises(ConfigError):
            Config.from_env({"LINGARR_URL": "lingarr:8080"})
        with self.assertRaises(ConfigError):
            Config.from_env({
                "BAZARR_URL": "bazarr:6767", "BAZARR_API_KEY": "secret",
                "LINGARR_URL": "lingarr:8080", "MAINTENANCE_WORKERS": "33",
            })

    def test_validation_fingerprint_covers_all_worker_policy_inputs(self):
        source = (REPO_ROOT / "docker" / "autotranslate" / "composition.py").read_text(
            encoding="utf-8"
        )
        for field in (
            "cleanup_min_chars", "cleanup_min_confidence",
            "cleanup_max_unique_ratio", "cleanup_min_letters_for_script",
            "cleanup_repair_enabled", "cleanup_max_repair_attempts",
            "cleanup_repair_context_lines", "cleanup_ffprobe_timeout",
        ):
            self.assertIn(field, source)

    def test_application_owns_host_lifecycle_and_cleanup(self):
        events = []

        class Host:
            def __init__(self, config):
                events.append(("build", config.bazarr_url))

            def run(self):
                events.append(("run", None))
                return 7

            def close(self):
                events.append(("close", None))

        config = Config.from_env({
            "BAZARR_URL": "bazarr:6767",
            "BAZARR_API_KEY": "secret",
            "LINGARR_URL": "lingarr:8080",
        })
        application = build_application(config, host_factory=Host)
        self.assertEqual(application.run(), 7)
        self.assertEqual(
            events,
            [("build", "http://bazarr:6767"), ("run", None), ("close", None)],
        )

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

    def test_lifecycle_skips_maintenance_when_cycle_requests_shutdown(self):
        events = []
        stopped = {"value": False}

        def run_cycle(_cycle):
            events.append("cycle")
            stopped["value"] = True
            return False

        controller = LifecycleController(
            run_cycle=run_cycle,
            advance_completed_cycle=lambda: events.append("advance") or 1,
            run_maintenance=lambda: (
                events.append("maintenance") or MaintenanceResult(True)
            ),
            set_phase=lambda phase, **_kwargs: events.append(phase),
            refresh_diagnostics=lambda: events.append("diagnostics"),
            sleep_interruptibly=lambda _seconds: events.append("sleep") or True,
            check_interval=1200,
            shutdown_requested=lambda: stopped["value"],
        )

        healthy, maintenance = controller.run_iteration(1)

        self.assertFalse(healthy)
        self.assertTrue(maintenance.healthy)
        self.assertNotIn("maintenance", events)
        self.assertNotIn("sleep", events)

    def test_shutdown_controller_preserves_first_deadline(self):
        clock = {"now": 10.0}
        shutdown = ShutdownController(30, monotonic=lambda: clock["now"])
        self.assertTrue(shutdown.request())
        clock["now"] = 25.0
        self.assertFalse(shutdown.request())
        self.assertEqual(shutdown.remaining(), 15.0)
        clock["now"] = 50.0
        self.assertEqual(shutdown.remaining(), 0.0)

    def test_schema_v17_is_authoritative_and_removes_legacy_state(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            store = StateStore(database, validator_version="v2", config_fingerprint="cfg")
            try:
                versions = {
                    row[0] for row in store._connection.execute(
                        "SELECT version FROM schema_migrations"
                    )
                }
                self.assertTrue({9, 10, 11, 12, 13, 14, 15, 16, 17}.issubset(versions))
                self.assertEqual(LATEST_SCHEMA_VERSION, max(versions))
                self.assertEqual(store._connection.execute("PRAGMA user_version").fetchone()[0], 19)
                tables = {
                    row[0] for row in store._connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertTrue({
                    "maintenance_runs", "repair_jobs", "partial_candidates",
                    "cue_recoveries", "donor_events",
                    "failure_fingerprints", "retry_admission_events", "provider_events",
                    "manual_review_actions",
                    "manual_review_scan_outbox",
                    "maintenance_validation_cache",
                }.issubset(tables))
                self.assertNotIn("legacy_quarantine_index", tables)
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
