import os
import re as _re
import sys
import json
import hashlib
import signal
import subprocess
import time
import threading
import tempfile
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import requests
from status_dashboard import StatusTracker, build_cycle_jobs, start_status_server
from state_store import StateStore, StateStoreError

# Unbuffered output
sys.stdout = os.fdopen(sys.stdout.fileno(), "w", buffering=1)
sys.stderr = os.fdopen(sys.stderr.fileno(), "w", buffering=1)


class _DailyLogSink:
    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self._lock = threading.Lock()
        self._date = ""
        self._file = None
        self.current_path: Path | None = None

    def write(self, value: str) -> None:
        if not value:
            return
        with self._lock:
            current_date = time.strftime("%Y-%m-%d")
            if self._file is None or current_date != self._date:
                if self._file is not None:
                    self._file.close()
                self.log_dir.mkdir(parents=True, exist_ok=True)
                self.current_path = self.log_dir / f"bazarr-autotranslate-{current_date}.log"
                self._file = self.current_path.open("a", encoding="utf-8", buffering=1)
                self._date = current_date
            self._file.write(value)

    def flush(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.flush()


class _TeeStream:
    def __init__(self, primary, sink: _DailyLogSink):
        self.primary = primary
        self.sink = sink

    def write(self, value: str) -> int:
        written = self.primary.write(value)
        self.sink.write(value)
        return written

    def flush(self) -> None:
        self.primary.flush()
        self.sink.flush()

    def fileno(self):
        return self.primary.fileno()

    def isatty(self) -> bool:
        return self.primary.isatty()

    @property
    def encoding(self):
        return self.primary.encoding

# ANSI colors (disabled outside TTY)
_tty = sys.stdout.isatty()
GREEN = "\033[92m" if _tty else ""
YELLOW = "\033[93m" if _tty else ""
RED = "\033[91m" if _tty else ""
CYAN = "\033[96m" if _tty else ""
BOLD = "\033[1m" if _tty else ""
RESET = "\033[0m" if _tty else ""

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _require(var: str) -> str:
    val = os.getenv(var, "").strip()
    if not val:
        print(f"{RED}[ERROR] {var} environment variable is required{RESET}")
        sys.exit(1)
    return val


def _normalize_url(raw: str) -> str:
    raw = raw.strip().rstrip("/")
    if not raw.startswith(("http://", "https://")):
        raw = f"http://{raw}"
    return raw


_raw_languages = os.getenv("LANGUAGES", "en,et,sv")
LANGUAGES = [l.strip().lower() for l in _raw_languages.split(",") if l.strip()]
BAZARR_URL = _normalize_url(_require("BAZARR_URL"))
BAZARR_API_KEY = _require("BAZARR_API_KEY")
LINGARR_URL = _normalize_url(_require("LINGARR_URL"))
LINGARR_API_KEY = os.getenv("LINGARR_API_KEY", "").strip()
PARALLEL_TRANSLATES = max(1, int(os.getenv("PARALLEL_TRANSLATES", "1")))
CHECK_INTERVAL = max(10, int(os.getenv("CHECK_INTERVAL", "1200")))
CONNECT_TIMEOUT = max(5, int(os.getenv("CONNECT_TIMEOUT", "10")))
POLL_INTERVAL = max(5, int(os.getenv("POLL_INTERVAL", "20")))
POLL_TIMEOUT = max(30, int(os.getenv("POLL_TIMEOUT", "900")))
RESUBMIT_COOLDOWN = max(60, int(os.getenv("RESUBMIT_COOLDOWN", "3600")))
SYNC_TIMEOUT = max(30, int(os.getenv("SYNC_TIMEOUT", "600")))
SYNC_POLL_INTERVAL = max(5, int(os.getenv("SYNC_POLL_INTERVAL", "15")))
SYNC_START_TIMEOUT = max(5, int(os.getenv("SYNC_START_TIMEOUT", "30")))
CLEANUP_MIN_CONFIDENCE = float(os.getenv("CLEANUP_MIN_CONFIDENCE", "0.70"))
CLEANUP_MIN_CHARS = int(os.getenv("CLEANUP_MIN_CHARS", "200"))
CLEANUP_MAX_UNIQUE_RATIO = float(os.getenv("CLEANUP_MAX_UNIQUE_RATIO", "0.15"))
CLEANUP_MAX_CYRILLIC_RATIO = float(os.getenv("CLEANUP_MAX_CYRILLIC_RATIO", "0.05"))
CLEANUP_MAX_CJK_RATIO = float(os.getenv("CLEANUP_MAX_CJK_RATIO", "0.05"))
CLEANUP_MAX_LATIN_RATIO = float(os.getenv("CLEANUP_MAX_LATIN_RATIO", "0.80"))
CLEANUP_MIN_LETTERS_FOR_SCRIPT = int(os.getenv("CLEANUP_MIN_LETTERS_FOR_SCRIPT", "20"))
CLEANUP_MAX_CUE_LINES = max(1, int(os.getenv("CLEANUP_MAX_CUE_LINES", "4")))
CLEANUP_MAX_CUE_CHARS = max(50, int(os.getenv("CLEANUP_MAX_CUE_CHARS", "500")))
CLEANUP_MAX_EXPANSION_RATIO = max(1.0, float(os.getenv("CLEANUP_MAX_EXPANSION_RATIO", "4.0")))
CLEANUP_MAX_EXPANSION_CHARS = max(50, int(os.getenv("CLEANUP_MAX_EXPANSION_CHARS", "300")))
CLEANUP_MAX_SOURCE_SIMILARITY = min(1.0, max(0.5, float(os.getenv("CLEANUP_MAX_SOURCE_SIMILARITY", "0.92"))))
CLEANUP_REPAIR_ENABLED = os.getenv("CLEANUP_REPAIR_ENABLED", "true").lower() in ("1", "true", "yes")
CLEANUP_MAX_REPAIR_ATTEMPTS = max(1, int(os.getenv("CLEANUP_MAX_REPAIR_ATTEMPTS", "2")))
CLEANUP_REPAIR_CONTEXT_LINES = max(0, int(os.getenv("CLEANUP_REPAIR_CONTEXT_LINES", "5")))
CLEANUP_FORMAT_REPAIR_ENABLED = os.getenv("CLEANUP_FORMAT_REPAIR_ENABLED", "true").lower() in ("1", "true", "yes")
CLEANUP_REPAIR_WORKERS = max(1, int(os.getenv("CLEANUP_REPAIR_WORKERS", "1")))
CLEANUP_REPAIR_QUEUE_MAX = max(1, int(os.getenv("CLEANUP_REPAIR_QUEUE_MAX", "100")))
CLEANUP_UNDERSIZED_ENABLED = os.getenv("CLEANUP_UNDERSIZED_ENABLED", "true").lower() in ("1", "true", "yes")
CLEANUP_MIN_MEDIA_DURATION = max(0.0, float(os.getenv("CLEANUP_MIN_MEDIA_DURATION", "900")))
CLEANUP_MIN_CUES_PER_MINUTE = max(0.0, float(os.getenv("CLEANUP_MIN_CUES_PER_MINUTE", "1.5")))
CLEANUP_MIN_TEXT_CHARS_PER_MINUTE = max(0.0, float(os.getenv("CLEANUP_MIN_TEXT_CHARS_PER_MINUTE", "40")))
CLEANUP_MIN_BYTES_PER_MINUTE = max(0.0, float(os.getenv("CLEANUP_MIN_BYTES_PER_MINUTE", "100")))
CLEANUP_MIN_TIMELINE_COVERAGE = min(1.0, max(0.0, float(os.getenv("CLEANUP_MIN_TIMELINE_COVERAGE", "0.60"))))
CLEANUP_UNDERSIZED_REQUIRED_SIGNALS = min(4, max(1, int(os.getenv("CLEANUP_UNDERSIZED_REQUIRED_SIGNALS", "3"))))
CLEANUP_FFPROBE_TIMEOUT = max(1, int(os.getenv("CLEANUP_FFPROBE_TIMEOUT", "15")))
CLEANUP_SCAN_EXISTING = os.getenv("CLEANUP_SCAN_EXISTING", "true").lower() in ("1", "true", "yes")
CLEANUP_SCAN_INTERVAL = max(300, int(os.getenv("CLEANUP_SCAN_INTERVAL", "21600")))
CLEANUP_SCAN_DRY_RUN = os.getenv("CLEANUP_SCAN_DRY_RUN", "false").lower() in ("1", "true", "yes")
CLEANUP_PRUNE_EXTRA_LANGUAGES = os.getenv("CLEANUP_PRUNE_EXTRA_LANGUAGES", "true").lower() in ("1", "true", "yes")
CLEANUP_PRUNE_ACTION = os.getenv("CLEANUP_PRUNE_ACTION", "quarantine").strip().lower()
CLEANUP_PRUNE_SPECIAL_SIDECARS = os.getenv("CLEANUP_PRUNE_SPECIAL_SIDECARS", "true").lower() in ("1", "true", "yes")
CLEANUP_PRUNE_UNKNOWN_SIDECARS = os.getenv("CLEANUP_PRUNE_UNKNOWN_SIDECARS", "false").lower() in ("1", "true", "yes")
CLEANUP_SOURCELESS_LINE_ONLY_ACTION = os.getenv(
    "CLEANUP_SOURCELESS_LINE_ONLY_ACTION", "warn"
).strip().lower()
CLEANUP_QUARANTINE_HOLD_DAYS = max(
    1, int(os.getenv("CLEANUP_QUARANTINE_HOLD_DAYS", "30"))
)
CLEANUP_ROOT_RAW = os.getenv("CLEANUP_ROOT", "/media").strip() or "/media"
CLEANUP_ROOTS = [Path(value.strip()) for value in CLEANUP_ROOT_RAW.split(os.pathsep) if value.strip()]
CLEANUP_ACTION = os.getenv("CLEANUP_ACTION", "quarantine").strip().lower()
_raw_cleanup_langs = os.getenv("CLEANUP_LANGUAGES", "et")
CLEANUP_LANGUAGES = {l.strip() for l in _raw_cleanup_langs.split(",") if l.strip()}
STATE_DIR = os.getenv("STATE_DIR", "/config").strip() or "/config"
SUBMIT_CACHE_FILE = os.path.join(STATE_DIR, "submitted_cache.json")
CLEANUP_QUARANTINE_DIR = Path(os.getenv("CLEANUP_QUARANTINE_DIR", f"{STATE_DIR}/quarantine"))
VALIDATION_STATE_FILE = Path(STATE_DIR) / "validation_state.json"
STATE_DB_FILE = Path(STATE_DIR) / "bazarr-autotranslate.sqlite3"
LOG_DIR = Path(os.getenv("LOG_DIR", "/var/log/bazarr-autotranslate"))
RETENTION_DAYS = max(1, int(os.getenv("RETENTION_DAYS", "30")))
RETENTION_CHECK_INTERVAL = max(300, int(os.getenv("RETENTION_CHECK_INTERVAL", "3600")))
STATUS_ENABLED = os.getenv("STATUS_ENABLED", "true").lower() in ("1", "true", "yes")
STATUS_BIND = os.getenv("STATUS_BIND", "0.0.0.0").strip() or "0.0.0.0"
STATUS_PORT = int(os.getenv("STATUS_PORT", "8765"))
STATUS_HISTORY_RETENTION_DAYS = max(
    7, int(os.getenv("STATUS_HISTORY_RETENTION_DAYS", "30"))
)
STATUS_RECENT_LIMIT = max(1, int(os.getenv("STATUS_RECENT_LIMIT", "20")))
STATUS_SNAPSHOT_FILE = Path(STATE_DIR) / "status.json"
STATUS_HISTORY_FILE = Path(STATE_DIR) / "status_history.jsonl"
DEBUG = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")

if not LANGUAGES:
    print(f"{RED}[ERROR] LANGUAGES must contain at least one language code{RESET}")
    sys.exit(1)
if CLEANUP_ACTION not in ("quarantine", "delete", "report"):
    print(f"{RED}[ERROR] CLEANUP_ACTION must be quarantine, delete, or report{RESET}")
    sys.exit(1)
if CLEANUP_PRUNE_ACTION not in ("quarantine", "delete", "report"):
    print(f"{RED}[ERROR] CLEANUP_PRUNE_ACTION must be quarantine, delete, or report{RESET}")
    sys.exit(1)
if CLEANUP_SOURCELESS_LINE_ONLY_ACTION not in ("warn", "quarantine"):
    print(
        f"{RED}[ERROR] CLEANUP_SOURCELESS_LINE_ONLY_ACTION must be warn or quarantine{RESET}"
    )
    sys.exit(1)
if not 1 <= STATUS_PORT <= 65535:
    print(f"{RED}[ERROR] STATUS_PORT must be between 1 and 65535{RESET}")
    sys.exit(1)

_app_log_sink = _DailyLogSink(LOG_DIR)
sys.stdout = _TeeStream(sys.stdout, _app_log_sink)
sys.stderr = _TeeStream(sys.stderr, _app_log_sink)

BAZARR_HEADERS: dict = {"Accept": "application/json", "X-API-KEY": BAZARR_API_KEY}
LINGARR_HEADERS: dict = {"Accept": "application/json", "Content-Type": "application/json"}
if LINGARR_API_KEY:
    LINGARR_HEADERS["X-Api-Key"] = LINGARR_API_KEY

_cleanup_detector = None
_cleanup_detector_lock = threading.Lock()
_validation_state = None
_validation_state_lock = threading.Lock()
_cleanup_scan_lock = threading.Lock()
_repair_executor = None
_repair_executor_lock = threading.Lock()
_repair_capacity = threading.BoundedSemaphore(CLEANUP_REPAIR_WORKERS + CLEANUP_REPAIR_QUEUE_MAX)
_pending_repairs: dict[Future, dict] = {}
_pending_repairs_lock = threading.Lock()
_repair_keys: set[tuple] = set()
_target_repair_locks: dict[str, threading.Lock] = {}
_target_repair_locks_lock = threading.Lock()
_duration_cache: dict[tuple[str, int, int], float | None] = {}
_duration_cache_lock = threading.Lock()
_pending_prune_videos: dict[str, str | None] = {}
_pending_prune_lock = threading.Lock()
_status_tracker: StatusTracker | None = None

_episode_cache: dict[int, int] = {}
_movie_cache: dict[int, int] = {}
_media_cache_lock = threading.Lock()


@dataclass
class RepairJobResult:
    action: str
    report: object
    title: str
    target_lang: str
    item_type: str | None
    item_id: int | None
    attempts: int = 0
    second_attempts: int = 0
    target_path: str = ""


@dataclass(frozen=True)
class LingarrSourceLanguage:
    name: str
    code: str
    targets: tuple[str, ...]


@dataclass(frozen=True)
class LingarrActiveTranslation:
    media_id: int | None
    media_type: str
    status: str

    @property
    def media_key(self) -> tuple[int, str] | None:
        if self.media_id is None:
            return None
        return self.media_id, self.media_type.lower()


class ServiceRequestError(RuntimeError):
    def __init__(self, service: str, operation: str, message: str):
        super().__init__(f"{service} {operation}: {message}")
        self.service = service
        self.operation = operation


class TranslationCapacityGate:
    def __init__(self, limit: int):
        self.limit = max(1, limit)
        self._condition = threading.Condition()
        self._next_token = 1
        self._reservations: dict[int, tuple[int, str]] = {}

    def _effective_count(
        self, active: list[LingarrActiveTranslation]
    ) -> int:
        active_keys = Counter(
            entry.media_key for entry in active if entry.media_key is not None
        )
        reservation_keys = Counter(self._reservations.values())
        visible_reservations = sum(
            min(count, active_keys.get(key, 0))
            for key, count in reservation_keys.items()
        )
        return len(active) + len(self._reservations) - visible_reservations

    def acquire(self, media_id: int, media_type: str) -> int | None:
        media_key = (int(media_id), media_type.lower())
        while not shutdown_requested:
            try:
                active = lingarr_get_active_translations()
            except ServiceRequestError as exc:
                print(
                    f"{YELLOW}[DEFER] Cannot verify Lingarr capacity: {exc}{RESET}"
                )
                return None

            with self._condition:
                effective = self._effective_count(active)
                active_keys = {
                    entry.media_key
                    for entry in active
                    if entry.media_key is not None
                }
                if effective < self.limit and media_key not in active_keys:
                    token = self._next_token
                    self._next_token += 1
                    self._reservations[token] = media_key
                    return token
                print(
                    f"[INFO] Lingarr queue full ({effective}/{self.limit}) "
                    f"— waiting {POLL_INTERVAL}s..."
                )
                self._condition.wait(timeout=POLL_INTERVAL)
        return None

    def release(self, token: int | None) -> None:
        if token is None:
            return
        with self._condition:
            self._reservations.pop(token, None)
            self._condition.notify_all()

    def reset(self) -> None:
        with self._condition:
            self._reservations.clear()
            self._condition.notify_all()


_translation_capacity = TranslationCapacityGate(PARALLEL_TRANSLATES)


def dbg(msg: str) -> None:
    if DEBUG:
        print(f"[DEBUG] {msg}")


def _status_transition(
    item_type: str | None,
    item_id: int | None,
    target_lang: str,
    state: str,
    *,
    repaired: bool = False,
    reason: str | None = None,
) -> bool:
    if _status_tracker is None:
        return False
    try:
        return _status_tracker.transition_for(
            item_type,
            item_id,
            target_lang,
            state,
            repaired=repaired,
            reason=reason,
        )
    except OSError as exc:
        print(f"{YELLOW}[STATUS] Could not persist job update: {exc}{RESET}")
        return False


def _status_set_phase(phase: str, *, next_cycle_at: float | None = None) -> None:
    if _status_tracker is None:
        return
    try:
        _status_tracker.set_phase(phase, next_cycle_at=next_cycle_at)
    except OSError as exc:
        print(f"{YELLOW}[STATUS] Could not persist service phase: {exc}{RESET}")


def _status_start_cycle(cycle_id: str, cycle_number: int, jobs: list[dict]) -> None:
    if _status_tracker is None:
        return
    try:
        _status_tracker.start_cycle(cycle_id, cycle_number, jobs)
    except OSError as exc:
        print(f"{YELLOW}[STATUS] Could not persist cycle start: {exc}{RESET}")


def _status_finish_cycle() -> None:
    if _status_tracker is None:
        return
    try:
        _status_tracker.finish_cycle()
    except OSError as exc:
        print(f"{YELLOW}[STATUS] Could not persist cycle completion: {exc}{RESET}")


def _status_record_maintenance(metrics: dict) -> None:
    if _status_tracker is None:
        return
    try:
        _status_tracker.record_maintenance(metrics)
    except OSError as exc:
        print(f"{YELLOW}[STATUS] Could not persist maintenance status: {exc}{RESET}")


def _status_compact_history() -> int:
    if _status_tracker is None:
        return 0
    try:
        return _status_tracker.compact_history()
    except OSError as exc:
        print(f"{YELLOW}[STATUS] Could not compact status history: {exc}{RESET}")
        return 0


def _status_finish_validation(
    item_type: str,
    item_id: int,
    target_lang: str,
    action: str,
) -> None:
    if action in ("valid", "valid-warning", "formatted", "repaired"):
        _status_transition(
            item_type,
            item_id,
            target_lang,
            "accepted",
            repaired=action in ("formatted", "repaired"),
        )
    elif action in ("repair-queued", "repair-duplicate"):
        _status_transition(item_type, item_id, target_lang, "repairing")
    elif action == "repair-deferred":
        _status_transition(
            item_type, item_id, target_lang, "deferred", reason="repair deferred"
        )
    elif action in ("quarantined", "deleted"):
        _status_transition(
            item_type, item_id, target_lang, "quarantined", reason=action
        )
    else:
        _status_transition(
            item_type, item_id, target_lang, "failed", reason=f"validation {action}"
        )


def _get_cleanup_detector():
    global _cleanup_detector
    if not CLEANUP_LANGUAGES:
        return None
    with _cleanup_detector_lock:
        if _cleanup_detector is None:
            print("[INFO] Loading language detector for per-file cleanup...")
            from clean_et_subs import build_detector
            _cleanup_detector = build_detector()
        return _cleanup_detector


def _get_validation_state():
    global _validation_state
    with _validation_state_lock:
        if _validation_state is None:
            from clean_et_subs import VALIDATOR_VERSION
            _validation_state = StateStore(
                STATE_DB_FILE, validator_version=VALIDATOR_VERSION
            )
        return _validation_state


def _initialize_state_store() -> StateStore:
    global _validation_state
    with _validation_state_lock:
        if _validation_state is not None:
            return _validation_state
        from clean_et_subs import VALIDATOR_VERSION
        store = StateStore(
            STATE_DB_FILE,
            acquire_process_lock=True,
            validator_version=VALIDATOR_VERSION,
        )
        migration = store.migrate_legacy(
            SUBMIT_CACHE_FILE,
            VALIDATION_STATE_FILE,
            cooldown_seconds=RESUBMIT_COOLDOWN,
        )
        reconciliation = store.reconcile_pending_operations()
        _validation_state = store
    imported = sum(migration[key] for key in ("submissions", "artifacts", "holds"))
    if imported or migration["skipped"]:
        print(
            f"[STATE] Migrated {migration['submissions']} cooldown(s), "
            f"{migration['artifacts']} artifact(s), {migration['holds']} hold(s); "
            f"skipped {migration['skipped']} malformed record(s)"
        )
    print(f"[STATE] SQLite state ready at {STATE_DB_FILE}")
    if reconciliation["completed"] or reconciliation["abandoned"]:
        print(
            f"[STATE] Reconciled {reconciliation['completed']} pending "
            f"operation(s); abandoned {reconciliation['abandoned']}"
        )
    return store


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------

shutdown_requested = False


def _handle_signal(signum, frame):
    global shutdown_requested
    shutdown_requested = True
    print(f"\n{YELLOW}[WARNING] Signal {signum} received — finishing current jobs then stopping.{RESET}")
    sys.stdout.flush()


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)

# ---------------------------------------------------------------------------
# Transactional submission state
# ---------------------------------------------------------------------------

def _load_submit_cache() -> None:
    """Compatibility entry point; initialization performs legacy migration."""
    _get_validation_state()


def _save_submit_cache() -> None:
    """Deprecated compatibility shim; SQLite commits each mutation."""
    return None


def _check_cooldown(
    item_id: int, target_lang: str, item_type: str = "legacy"
) -> int | None:
    return _get_validation_state().check_cooldown(
        item_type, item_id, target_lang
    )


def _record_submission(
    item_id: int,
    target_lang: str,
    target_path: str | None = None,
    *,
    expected_target_path: str | None = None,
    actual_target_path: str | None = None,
    video_path: str | None = None,
    source_path: str | None = None,
    source_hash: str | None = None,
    source_language: str | None = None,
    item_type: str | None = None,
    target_variant: str | None = None,
    lingarr_job_id: int | None = None,
    status: str = "submitted",
) -> int:
    identity = (
        _target_identity_from_sidecar(target_path, target_lang)
        if target_path else None
    )
    return _get_validation_state().record_submission(
        item_type or "legacy",
        item_id,
        target_lang,
        cooldown_seconds=RESUBMIT_COOLDOWN,
        target_identity=identity,
        target_path=target_path,
        expected_target_path=expected_target_path or target_path,
        actual_target_path=actual_target_path,
        video_path=video_path,
        source_path=source_path,
        source_hash=source_hash,
        source_language=source_language,
        target_variant=target_variant,
        lingarr_job_id=lingarr_job_id,
        status=status,
    )


def _mark_submission_submitted(attempt_id: int, job_id: int) -> None:
    _get_validation_state().mark_submission_submitted(attempt_id, job_id)


def _mark_submission_failed(attempt_id: int) -> None:
    _get_validation_state().mark_submission_failed(attempt_id)


def _update_submission_actual_path(
    item_id: int,
    target_lang: str,
    actual_target_path: str,
    target_variant: str,
    item_type: str = "legacy",
) -> None:
    _get_validation_state().update_submission_actual_path(
        item_type, item_id, target_lang, actual_target_path, target_variant
    )


def _clear_submission(
    item_id: int, target_lang: str, item_type: str | None = None
) -> None:
    """Remove cooldown entry so a cleaned (deleted) file can be re-translated next cycle."""
    removed = _get_validation_state().clear_submission(
        item_type, item_id, target_lang
    )
    if removed:
        dbg(f"_clear_submission({item_id}, {target_lang!r}): cleared")


def _clear_submission_for_path(target_path: str | Path, target_lang: str) -> int:
    identity = _target_identity_from_sidecar(target_path, target_lang)
    removed = _get_validation_state().clear_submissions_for_identity(
        identity, target_path, target_lang
    )
    if removed:
        dbg(
            f"Cleared {removed} cooldown entr{'y' if removed == 1 else 'ies'} "
            f"for {target_path}"
        )
    return removed

# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def bazarr_url(endpoint: str) -> str:
    return f"{BAZARR_URL}/api/{endpoint}"


def lingarr_url(endpoint: str) -> str:
    return f"{LINGARR_URL}/api/{endpoint}"


def _request_json(
    method: str,
    url: str,
    *,
    service: str,
    operation: str,
    **kwargs,
):
    request = getattr(requests, method.lower())
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            response = request(url, **kwargs)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            last_error = exc
            status = getattr(getattr(exc, "response", None), "status_code", None)
            retryable = status is None or status == 429 or status >= 500
            if not retryable or attempt == 3:
                break
        except ValueError as exc:
            raise ServiceRequestError(
                service, operation, f"invalid JSON response: {exc}"
            ) from exc

        delay = attempt
        print(
            f"{YELLOW}[WARNING] {service} {operation} failed "
            f"(attempt {attempt}/3); retrying in {delay}s{RESET}"
        )
        time.sleep(delay)

    raise ServiceRequestError(service, operation, str(last_error)) from last_error


# ---------------------------------------------------------------------------
# Bazarr API
# ---------------------------------------------------------------------------

def fetch_wanted(item_type: str) -> list:
    url = bazarr_url(f"{item_type}/wanted")
    dbg(f"fetch_wanted({item_type}): GET {url}")
    payload = _request_json(
        "get",
        url,
        service="Bazarr",
        operation=f"fetch {item_type} wanted queue",
        headers=BAZARR_HEADERS,
        params={"start": 0, "length": -1},
        timeout=CONNECT_TIMEOUT,
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("data", []), list):
        raise ServiceRequestError(
            "Bazarr", f"fetch {item_type} wanted queue", "unexpected response schema"
        )
    result = payload.get("data", [])
    dbg(f"fetch_wanted({item_type}): {len(result)} item(s)")
    return result


def fetch_subtitles(item_type: str, item_id: int) -> tuple[str, list]:
    if item_type == "episodes":
        url = bazarr_url("episodes")
        params = {"episodeid[]": item_id}
    else:
        url = bazarr_url("movies")
        params = {"radarrid[]": item_id}
    payload = _request_json(
        "get",
        url,
        service="Bazarr",
        operation=f"fetch {item_type} subtitles for {item_id}",
        headers=BAZARR_HEADERS,
        params=params,
        timeout=CONNECT_TIMEOUT,
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("data", []), list):
        raise ServiceRequestError(
            "Bazarr",
            f"fetch {item_type} subtitles for {item_id}",
            "unexpected response schema",
        )
    data = payload.get("data", [])
    if data:
        if not isinstance(data[0], dict):
            raise ServiceRequestError(
                "Bazarr",
                f"fetch {item_type} subtitles for {item_id}",
                "unexpected item schema",
            )
        vp = data[0].get("path", "")
        subs = data[0].get("subtitles", [])
        if not isinstance(subs, list):
            raise ServiceRequestError(
                "Bazarr",
                f"fetch {item_type} subtitles for {item_id}",
                "unexpected subtitles schema",
            )
        dbg(f"fetch_subtitles({item_type}, {item_id}): video_path={vp!r}")
        return vp, subs
    return "", []


def trigger_bazarr_sync(had_episodes: bool, had_movies: bool) -> None:
    tasks = []
    if had_episodes:
        tasks.append("series_full_scan_subtitles")
    if had_movies:
        tasks.append("movies_full_scan_subtitles")
    for taskid in tasks:
        try:
            r = requests.post(
                bazarr_url("system/tasks"),
                headers=BAZARR_HEADERS,
                params={"taskid": taskid},
                timeout=CONNECT_TIMEOUT,
            )
            if r.status_code == 204:
                print(f"[INFO] Triggered Bazarr task: {taskid}")
            else:
                print(f"{YELLOW}[WARNING] Bazarr task {taskid} returned {r.status_code}{RESET}")
        except Exception as e:
            print(f"{RED}[ERROR] Failed to trigger Bazarr task {taskid}: {e}{RESET}")


def _job_matches_scan(job: dict, had_episodes: bool, had_movies: bool) -> bool:
    name = (job.get("job_name") or "").lower()
    status = (job.get("status") or "").lower()
    if status != "running":
        return False
    if had_episodes and "episode" in name and "subtitle" in name:
        return True
    if had_movies and "movie" in name and "subtitle" in name:
        return True
    if had_episodes and "series" in name and "subtitle" in name:
        return True
    return False


def wait_for_bazarr_sync(had_episodes: bool, had_movies: bool, timeout: int) -> bool:
    if not had_episodes and not had_movies:
        return True

    print(f"[INFO] Waiting for Bazarr subtitle scan to complete (timeout {timeout}s)...")
    deadline = time.time() + timeout
    start_deadline = min(deadline, time.time() + SYNC_START_TIMEOUT)
    logged_jobs: set[int] = set()
    observed_running = False

    while not shutdown_requested:
        try:
            r = requests.get(bazarr_url("system/jobs"), headers=BAZARR_HEADERS, timeout=CONNECT_TIMEOUT)
            r.raise_for_status()
            jobs = r.json().get("data", [])
        except Exception as e:
            print(f"{YELLOW}[WARNING] Could not poll Bazarr jobs: {e}{RESET}")
            jobs = []

        active = [j for j in jobs if _job_matches_scan(j, had_episodes, had_movies)]
        if not active:
            if observed_running:
                print(f"{GREEN}[OK] Bazarr subtitle scan completed{RESET}")
                return True
            if time.time() >= start_deadline:
                print(
                    f"{YELLOW}[WARNING] Bazarr subtitle scan did not appear within "
                    f"{SYNC_START_TIMEOUT}s{RESET}"
                )
                return False
        else:
            observed_running = True

        for job in active:
            jid = job.get("job_id")
            if jid not in logged_jobs:
                logged_jobs.add(jid)
                print(f"[INFO] Bazarr scan running: {job.get('job_name', 'unknown')}")
            if job.get("is_progress"):
                pv = job.get("progress_value", 0)
                pm = job.get("progress_max", 0)
                msg = job.get("progress_message", "")
                print(f"[SYNC] {job.get('job_name')}: {pv}/{pm} — {msg}")

        if time.time() >= deadline:
            print(f"{YELLOW}[WARNING] Bazarr sync timed out after {timeout}s — continuing anyway{RESET}")
            return False

        for _ in range(SYNC_POLL_INTERVAL):
            if shutdown_requested:
                return False
            time.sleep(1)

    return False

# ---------------------------------------------------------------------------
# Lingarr API
# ---------------------------------------------------------------------------

def lingarr_get_languages() -> list[LingarrSourceLanguage]:
    try:
        payload = _request_json(
            "get",
            lingarr_url("Translate/languages"),
            service="Lingarr",
            operation="fetch languages",
            headers=LINGARR_HEADERS,
            timeout=CONNECT_TIMEOUT,
        )
    except ServiceRequestError as exc:
        print(f"{YELLOW}[WARNING] Could not fetch Lingarr languages: {exc}{RESET}")
        return []
    if not isinstance(payload, list):
        print(
            f"{YELLOW}[WARNING] Lingarr languages response has an unexpected schema{RESET}"
        )
        return []

    languages: list[LingarrSourceLanguage] = []
    for index, entry in enumerate(payload):
        if not isinstance(entry, dict):
            print(
                f"{YELLOW}[WARNING] Ignoring malformed Lingarr language entry "
                f"at index {index}{RESET}"
            )
            continue
        name = entry.get("name")
        code = entry.get("code")
        targets = entry.get("targets")
        if targets is None:
            targets = []
        if (
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(code, str)
            or not code.strip()
            or not isinstance(targets, list)
            or not all(isinstance(target, str) and target.strip() for target in targets)
        ):
            print(
                f"{YELLOW}[WARNING] Ignoring malformed Lingarr language entry "
                f"at index {index}{RESET}"
            )
            continue
        languages.append(
            LingarrSourceLanguage(
                name=name.strip(),
                code=code.strip(),
                targets=tuple(target.strip() for target in targets),
            )
        )
    return languages


def lingarr_build_media_cache() -> None:
    global _episode_cache, _movie_cache
    episode_cache: dict[int, int] = {}
    movie_cache: dict[int, int] = {}

    page = 1
    while not shutdown_requested:
        try:
            r = requests.get(
                lingarr_url("Media/movies"),
                headers=LINGARR_HEADERS,
                params={"pageNumber": page, "pageSize": 100},
                timeout=CONNECT_TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"{RED}[ERROR] lingarr_build_media_cache movies page {page}: {e}{RESET}")
            break

        for movie in data.get("items", []):
            rid = movie.get("radarrId")
            mid = movie.get("id")
            if rid is not None and mid is not None:
                movie_cache[int(rid)] = int(mid)

        total = data.get("totalCount", 0)
        page_size = data.get("pageSize", 100) or 100
        if page * page_size >= total or not data.get("items"):
            break
        page += 1

    page = 1
    while not shutdown_requested:
        try:
            r = requests.get(
                lingarr_url("Media/shows"),
                headers=LINGARR_HEADERS,
                params={"pageNumber": page, "pageSize": 50},
                timeout=CONNECT_TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"{RED}[ERROR] lingarr_build_media_cache shows page {page}: {e}{RESET}")
            break

        for show in data.get("items", []):
            for season in show.get("seasons", []) or []:
                for ep in season.get("episodes", []) or []:
                    sid = ep.get("sonarrId")
                    eid = ep.get("id")
                    if sid is not None and eid is not None:
                        episode_cache[int(sid)] = int(eid)

        total = data.get("totalCount", 0)
        page_size = data.get("pageSize", 50) or 50
        if page * page_size >= total or not data.get("items"):
            break
        page += 1

    with _media_cache_lock:
        _episode_cache = episode_cache
        _movie_cache = movie_cache

    print(f"[INFO] Lingarr media cache: {len(movie_cache)} movie(s), {len(episode_cache)} episode(s)")


def lingarr_resolve_media_id(item_type: str, item_id: int) -> int | None:
    with _media_cache_lock:
        if item_type == "episodes":
            return _episode_cache.get(item_id)
        return _movie_cache.get(item_id)


def lingarr_get_active_translations() -> list[LingarrActiveTranslation]:
    try:
        payload = _request_json(
            "get",
            lingarr_url("TranslationRequest/active"),
            service="Lingarr",
            operation="fetch active translations",
            headers=LINGARR_HEADERS,
            timeout=CONNECT_TIMEOUT,
        )
    except ServiceRequestError:
        raise
    if not isinstance(payload, list):
        raise ServiceRequestError(
            "Lingarr", "fetch active translations", "unexpected response schema"
        )

    active: list[LingarrActiveTranslation] = []
    for entry in payload:
        if not isinstance(entry, dict):
            raise ServiceRequestError(
                "Lingarr", "fetch active translations", "malformed active entry"
            )
        media_id = entry.get("mediaId")
        media_type = entry.get("mediaType")
        status = entry.get("status")
        if (
            (media_id is not None and not isinstance(media_id, int))
            or not isinstance(media_type, str)
            or not media_type
            or not isinstance(status, str)
            or not status
        ):
            raise ServiceRequestError(
                "Lingarr", "fetch active translations", "malformed active entry"
            )
        active.append(LingarrActiveTranslation(media_id, media_type, status))
    return active


def lingarr_submit_file(
    media_id: int,
    subtitle_path: str,
    source_lang: str,
    target_lang: str,
    media_type: str,
) -> int | None:
    body = {
        "mediaId": media_id,
        "subtitlePath": subtitle_path,
        "sourceLanguage": source_lang,
        "targetLanguage": target_lang,
        "mediaType": media_type,
        "subtitleFormat": "srt",
    }
    dbg(f"lingarr_submit_file: POST {body}")
    try:
        r = requests.post(
            lingarr_url("Translate/file"),
            headers=LINGARR_HEADERS,
            json=body,
            timeout=CONNECT_TIMEOUT,
        )
        r.raise_for_status()
        job_id = r.json().get("jobId")
        if job_id is not None:
            return int(job_id)
        print(f"{RED}[ERROR] lingarr_submit_file: no jobId in response{RESET}")
    except Exception as e:
        print(f"{RED}[ERROR] lingarr_submit_file: {e}{RESET}")
    return None


def lingarr_translate_line(
    subtitle_line: str,
    source_lang: str,
    target_lang: str,
    context_before: list[str],
    context_after: list[str],
    *,
    repair_label: str = "",
    cue_number: int | None = None,
    attempt: int | None = None,
    outcome_meta: dict | None = None,
) -> str | None:
    body = {
        "subtitleLine": subtitle_line,
        "sourceLanguage": source_lang,
        "targetLanguage": target_lang,
        "contextLinesBefore": context_before,
        "contextLinesAfter": context_after,
    }
    dbg(
        f"lingarr_translate_line: POST source={source_lang} target={target_lang} "
        f"before={len(context_before)} after={len(context_after)} chars={len(subtitle_line)}"
    )
    started = time.monotonic()
    try:
        r = requests.post(
            lingarr_url("Translate/line"),
            headers=LINGARR_HEADERS,
            json=body,
            timeout=max(CONNECT_TIMEOUT, 120),
        )
        elapsed = time.monotonic() - started
        if outcome_meta is not None:
            outcome_meta.update({"httpStatus": r.status_code, "httpDurationSeconds": round(elapsed, 3)})
        identity = f"{repair_label} cue {cue_number}".strip() if cue_number is not None else "line repair"
        attempt_label = f" attempt {attempt}" if attempt is not None else ""
        print(f"[REPAIR] Lingarr HTTP {r.status_code} for {identity}{attempt_label} after {elapsed:.1f}s")
        r.raise_for_status()
        try:
            payload = r.json()
        except ValueError:
            payload = r.text

        if isinstance(payload, str) and payload.strip():
            return payload.strip()
        if isinstance(payload, dict):
            for key in ("translatedSubtitle", "translatedLine", "translation", "text"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        print(f"{RED}[ERROR] lingarr_translate_line: unexpected response shape{RESET}")
    except Exception as e:
        elapsed = time.monotonic() - started
        if outcome_meta is not None:
            outcome_meta.update({"httpStatus": getattr(getattr(e, "response", None), "status_code", None), "httpDurationSeconds": round(elapsed, 3)})
        print(f"{RED}[ERROR] lingarr_translate_line failed after {elapsed:.1f}s: {e}{RESET}")
    return None


def lingarr_get_job(job_id: int) -> dict | None:
    try:
        r = requests.get(
            lingarr_url(f"TranslationRequest/{job_id}"),
            headers=LINGARR_HEADERS,
            timeout=CONNECT_TIMEOUT,
        )
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        dbg(f"lingarr_get_job({job_id}): {e}")
    return None


def lingarr_poll_job(job_id: int, deadline: float, label: str) -> str | None:
    last_progress = -1
    while not shutdown_requested:
        job = lingarr_get_job(job_id)
        if job:
            status = job.get("status", "")
            progress = job.get("progress", 0)
            if progress != last_progress:
                last_progress = progress
                dbg(f"{label} job {job_id}: status={status} progress={progress}")
            if status == "Completed":
                return "Completed"
            if status in ("Failed", "Cancelled", "Interrupted"):
                err = job.get("errorMessage", "")
                print(f"{RED}[FAIL] {label} Lingarr job {job_id}: {status}" +
                      (f" — {err}" if err else "") + RESET)
                return status

        if time.time() >= deadline:
            print(f"{YELLOW}[TIMEOUT] {label} Lingarr job {job_id} not completed in time{RESET}")
            return None

        for _ in range(POLL_INTERVAL):
            if shutdown_requested:
                return None
            time.sleep(1)

    return None

# ---------------------------------------------------------------------------
# Subtitle helpers
# ---------------------------------------------------------------------------

_TIMESTAMP_RE = _re.compile(r"^\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}$")

_LANGUAGE_ALIASES = {
    "en": {"en", "eng"}, "et": {"et", "est"}, "sv": {"sv", "swe"},
    "de": {"de", "deu", "ger"}, "fr": {"fr", "fra", "fre"},
    "es": {"es", "spa"}, "nl": {"nl", "nld", "dut"},
    "no": {"no", "nor", "nob"}, "fi": {"fi", "fin"},
    "da": {"da", "dan"}, "pl": {"pl", "pol"}, "pt": {"pt", "por"},
    "ru": {"ru", "rus"}, "lv": {"lv", "lav"}, "lt": {"lt", "lit"},
    "uk": {"uk", "ukr"}, "tr": {"tr", "tur"}, "it": {"it", "ita"},
    "cs": {"cs", "ces", "cze"}, "sk": {"sk", "slk", "slo"},
    "hu": {"hu", "hun"}, "ro": {"ro", "ron", "rum"},
    "el": {"el", "ell", "gre"}, "ar": {"ar", "ara"},
    "he": {"he", "heb"}, "ja": {"ja", "jpn"}, "ko": {"ko", "kor"},
    "zh": {"zh", "zho", "chi"},
}
_ALIAS_TO_LANGUAGE = {
    alias: code for code, aliases in _LANGUAGE_ALIASES.items() for alias in aliases
}


@dataclass(frozen=True)
class SidecarClassification:
    path: Path
    kind: str
    language: str | None
    tokens: tuple[str, ...]


def _sub_priority(path: str, lang_code2: str) -> int:
    stem = os.path.basename(path).lower().removesuffix(".srt")
    for code in sorted(
        _LANGUAGE_ALIASES.get(lang_code2, {lang_code2}),
        key=len,
        reverse=True,
    ):
        idx = stem.rfind(f".{code}")
        if idx == -1:
            continue
        suffix = stem[idx + len(code) + 1:]
        if suffix == "":
            return 0
        if suffix in ("hi", "sdh"):
            return 1
        if suffix.isdigit():
            return 1 + int(suffix)
        return 10
    return 99


def _target_suffix(path: str | Path, target_lang: str) -> tuple[str, str] | None:
    name = Path(path).name
    aliases = sorted(
        _LANGUAGE_ALIASES.get(target_lang, {target_lang}),
        key=len,
        reverse=True,
    )
    for alias in aliases:
        match = _re.search(
            rf"\.{_re.escape(alias)}(?P<variant>\.(?:hi|sdh|\d+))?\.srt$",
            name,
            _re.IGNORECASE,
        )
        if match:
            return name[:match.start()], (match.group("variant") or "").lower()
    return None


def _target_identity_from_sidecar(
    target_path: str | Path,
    target_lang: str,
) -> str | None:
    suffix = _target_suffix(target_path, target_lang)
    if suffix is None:
        return None
    base_name, _ = suffix
    return os.path.normcase(
        os.path.abspath(os.path.join(os.path.dirname(str(target_path)), base_name))
    )


def _submission_identity(metadata: dict, target_lang: str) -> str | None:
    video_path = metadata.get("videoPath")
    if isinstance(video_path, str) and video_path:
        return os.path.normcase(os.path.abspath(os.path.splitext(video_path)[0]))
    for field in ("actualTargetPath", "expectedTargetPath", "targetPath"):
        value = metadata.get(field)
        if isinstance(value, str) and value:
            identity = _target_identity_from_sidecar(value, target_lang)
            if identity is not None:
                return identity
    return None


def _find_target_sidecars(video_path: str, target_lang: str) -> list[str]:
    video = Path(video_path)
    matches: list[str] = []
    try:
        entries = video.parent.iterdir()
    except OSError:
        return matches
    for candidate in entries:
        if not candidate.is_file() or candidate.suffix.casefold() != ".srt":
            continue
        suffix = _target_suffix(candidate, target_lang)
        if suffix is None or suffix[0].casefold() != video.stem.casefold():
            continue
        matches.append(str(candidate))
    return sorted(matches, key=lambda path: (_sub_priority(path, target_lang), path.casefold()))


def _find_existing_target(video_path: str, target_lang: str) -> str | None:
    return next(iter(_find_target_sidecars(video_path, target_lang)), None)


def _snapshot_target_sidecars(video_path: str, target_lang: str) -> dict[str, str | None]:
    return {
        os.path.normcase(os.path.abspath(path)): _file_hash_or_none(path)
        for path in _find_target_sidecars(video_path, target_lang)
    }


def _discover_completed_target(
    video_path: str,
    target_lang: str,
    expected_target_path: str,
    before: dict[str, str | None],
) -> str | None:
    expected = os.path.normcase(os.path.abspath(expected_target_path))
    changed: list[str] = []
    for path in _find_target_sidecars(video_path, target_lang):
        normalized = os.path.normcase(os.path.abspath(path))
        current_hash = _file_hash_or_none(path)
        if normalized not in before or before[normalized] != current_hash:
            changed.append(path)
    if changed:
        selected = next(
            (
                path
                for path in changed
                if os.path.normcase(os.path.abspath(path)) == expected
            ),
            changed[0],
        )
        print(
            f"[TRANSLATE] Discovered Lingarr output {os.path.basename(selected)} "
            f"(expected {os.path.basename(expected_target_path)})"
        )
        return selected
    if os.path.exists(expected_target_path):
        return expected_target_path
    return None


_VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts", ".webm"}
_NON_FULL_SUBTITLE_TOKENS = {"forced", "foreign", "signs", "commentary"}


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes")


def _sidecar_tokens(video_path: str | Path, subtitle_path: str | Path) -> list[str]:
    video_stem = Path(video_path).stem
    subtitle_stem = Path(subtitle_path).stem
    if subtitle_stem.casefold() == video_stem.casefold():
        return []
    prefix = f"{video_stem}."
    if not subtitle_stem.casefold().startswith(prefix.casefold()):
        return []
    return [token.casefold() for token in subtitle_stem[len(prefix):].split(".") if token]


def _explicit_non_full_sidecar(video_path: str | Path, subtitle_path: str | Path) -> str | None:
    return next((token for token in _sidecar_tokens(video_path, subtitle_path)
                 if token in _NON_FULL_SUBTITLE_TOKENS), None)


def _classify_sidecar(video_path: str | Path, subtitle_path: str | Path) -> SidecarClassification:
    path = Path(subtitle_path)
    tokens = tuple(_sidecar_tokens(video_path, path))
    language = next((_ALIAS_TO_LANGUAGE[token] for token in tokens if token in _ALIAS_TO_LANGUAGE), None)
    managed = {code.casefold() for code in LANGUAGES}
    if language in managed:
        kind = "managed"
    elif language is not None:
        kind = "nonmanaged"
    elif any(token in _NON_FULL_SUBTITLE_TOKENS for token in tokens):
        kind = "special"
    else:
        kind = "unknown"
    return SidecarClassification(path, kind, language, tokens)


def _find_sidecar_video(subtitle_path: str | Path) -> Path | None:
    subtitle = Path(subtitle_path)
    subtitle_stem = subtitle.stem.casefold()
    try:
        candidates = [
            path for path in subtitle.parent.iterdir()
            if path.is_file() and path.suffix.casefold() in _VIDEO_EXTENSIONS
            and (subtitle_stem == path.stem.casefold()
                 or subtitle_stem.startswith(f"{path.stem.casefold()}."))
        ]
    except OSError:
        return None
    return max(candidates, key=lambda path: len(path.stem), default=None)


def _quarantine_identity(
    target_lang: str,
    *,
    video_path: str | Path | None = None,
    target_path: str | Path | None = None,
) -> str | None:
    if video_path is not None:
        base = os.path.normcase(os.path.abspath(os.path.splitext(str(video_path))[0]))
    elif target_path is not None:
        base = _target_identity_from_sidecar(target_path, target_lang)
    else:
        return None
    return f"{base}|{target_lang.casefold()}" if base is not None else None


def _active_quarantine_hold(
    video_path: str | Path,
    target_lang: str,
) -> dict | None:
    identity = _quarantine_identity(target_lang, video_path=video_path)
    if identity is None:
        return None
    return _get_validation_state().active_quarantine_tombstone(identity)


def _clear_quarantine_hold(
    target_lang: str,
    *,
    video_path: str | Path | None = None,
    target_path: str | Path | None = None,
) -> bool:
    identity = _quarantine_identity(
        target_lang, video_path=video_path, target_path=target_path
    )
    if identity is None:
        return False
    return _get_validation_state().clear_quarantine_tombstone(identity)


def _probe_media_duration(video_path: str | Path) -> float | None:
    video = Path(video_path)
    try:
        stat = video.stat()
    except (OSError, StateStoreError) as e:
        dbg(f"Could not stat media for duration {video}: {e}")
        return None
    key = (os.path.normcase(os.path.abspath(str(video))), stat.st_size, stat.st_mtime_ns)
    with _duration_cache_lock:
        if key in _duration_cache:
            return _duration_cache[key]
    try:
        completed = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(video),
            ],
            capture_output=True,
            text=True,
            timeout=CLEANUP_FFPROBE_TIMEOUT,
            check=False,
        )
        duration = float(completed.stdout.strip()) if completed.returncode == 0 else 0.0
        if duration <= 0:
            error = completed.stderr.strip().splitlines()[-1:] or ["invalid duration"]
            print(f"{YELLOW}[SIZE] ffprobe unavailable for {video.name}: {error[0]}{RESET}")
            result = None
        else:
            result = duration
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        print(f"{YELLOW}[SIZE] ffprobe unavailable for {video.name}: {e}{RESET}")
        result = None
    if result is not None:
        with _duration_cache_lock:
            _duration_cache[key] = result
    return result


def _completeness_kwargs() -> dict:
    return {
        "min_media_duration": CLEANUP_MIN_MEDIA_DURATION,
        "min_cues_per_minute": CLEANUP_MIN_CUES_PER_MINUTE,
        "min_text_chars_per_minute": CLEANUP_MIN_TEXT_CHARS_PER_MINUTE,
        "min_bytes_per_minute": CLEANUP_MIN_BYTES_PER_MINUTE,
        "min_timeline_coverage": CLEANUP_MIN_TIMELINE_COVERAGE,
        "required_signals": CLEANUP_UNDERSIZED_REQUIRED_SIGNALS,
    }


def _evaluate_completeness(subtitle_path: str | Path, media_duration: float | None):
    if not CLEANUP_UNDERSIZED_ENABLED or media_duration is None:
        return None
    from clean_et_subs import evaluate_subtitle_completeness
    return evaluate_subtitle_completeness(
        subtitle_path, media_duration, **_completeness_kwargs()
    )


def _add_completeness_issue(report, completeness) -> None:
    if completeness is None:
        return
    from clean_et_subs import completeness_issue
    issue = completeness_issue(completeness)
    if issue is not None:
        report.issues.append(issue)


def _count_dialogue_lines(path: str) -> int | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        count = 0
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.isdigit() or _TIMESTAMP_RE.match(stripped):
                continue
            count += 1
        return count
    except OSError:
        return None


def _estimate_timeout(source_path: str) -> int:
    n = _count_dialogue_lines(source_path)
    if n is None:
        return POLL_TIMEOUT
    base = n * 1.8
    estimated = int(base * 1.3)
    hard_cap = max(POLL_TIMEOUT, CHECK_INTERVAL - 60)
    timeout = min(max(POLL_TIMEOUT, estimated), hard_cap)
    print(f"[INFO] Source has {n} dialogue lines — base ~{int(base)}s, "
          f"timeout set to {timeout}s (floor {POLL_TIMEOUT}s, cap {hard_cap}s)")
    return timeout


def _derive_target_path(source_path: str, source_lang: str, target_lang: str) -> str | None:
    path = Path(source_path)
    stem_tokens = path.stem.split(".")
    aliases = {
        alias.casefold()
        for alias in _LANGUAGE_ALIASES.get(source_lang, {source_lang})
    }
    language_index = next(
        (
            index
            for index in range(len(stem_tokens) - 1, -1, -1)
            if stem_tokens[index].casefold() in aliases
        ),
        None,
    )
    if language_index is None:
        return None
    stem_tokens[language_index] = target_lang
    return str(path.with_name(".".join(stem_tokens) + path.suffix))


def _validation_kwargs() -> dict:
    return {
        "min_chars": CLEANUP_MIN_CHARS,
        "min_confidence": CLEANUP_MIN_CONFIDENCE,
        "max_unique_ratio": CLEANUP_MAX_UNIQUE_RATIO,
        "max_cyrillic_ratio": CLEANUP_MAX_CYRILLIC_RATIO,
        "max_cjk_ratio": CLEANUP_MAX_CJK_RATIO,
        "max_latin_ratio": CLEANUP_MAX_LATIN_RATIO,
        "min_letters_for_script": CLEANUP_MIN_LETTERS_FOR_SCRIPT,
        "max_cue_lines": CLEANUP_MAX_CUE_LINES,
        "max_cue_chars": CLEANUP_MAX_CUE_CHARS,
        "max_expansion_ratio": CLEANUP_MAX_EXPANSION_RATIO,
        "max_expansion_chars": CLEANUP_MAX_EXPANSION_CHARS,
        "max_source_similarity": CLEANUP_MAX_SOURCE_SIMILARITY,
    }


def _source_less_line_only_warning(report) -> bool:
    return (
        CLEANUP_SOURCELESS_LINE_ONLY_ACTION == "warn"
        and bool(report.issues)
        and all(issue.rule == "excessive_lines" for issue in report.issues)
    )


def _file_hash_or_none(path: str | Path | None) -> str | None:
    if path is None:
        return None
    try:
        from clean_et_subs import file_sha256
        return file_sha256(path)
    except OSError as e:
        dbg(f"Could not hash {path}: {e}")
        return None


def _record_validation_result(
    target_path: str | Path,
    source_hash: str | None,
    target_hash: str | None,
    result: str,
    report,
    origin: str | None = None,
    **extra,
) -> bool:
    try:
        from clean_et_subs import VALIDATOR_VERSION

        details = {"validation": report.to_dict(), **extra}
        if details.get("completeness") is not None:
            details.setdefault("filenameClassification", "regular")
        target_language = extra.get("targetLanguage")
        if target_language is None:
            target_language = next(
                (
                    language
                    for language in LANGUAGES
                    if _target_suffix(target_path, language) is not None
                ),
                None,
            )
        target_suffix = (
            _target_suffix(target_path, target_language)
            if target_language is not None else None
        )
        trusted_source_hash = source_hash if origin == "lingarr" else None
        _get_validation_state().record(
            target_path,
            source_hash=trusted_source_hash,
            target_hash=target_hash,
            result=result,
            origin=origin,
            details=details,
            source_path=extra.get("sourcePath"),
            source_language=extra.get("sourceLanguage"),
            target_language=target_language,
            target_identity=(
                extra.get("targetIdentity")
                or (
                    _target_identity_from_sidecar(target_path, target_language)
                    if target_language is not None else None
                )
            ),
            target_variant=(
                extra.get("targetVariant")
                if extra.get("targetVariant") is not None
                else (target_suffix[1] if target_suffix is not None else None)
            ),
            operation=extra.get("operation", "validation"),
            parent_artifact_id=extra.get("parentArtifactId"),
            attempt_id=extra.get("attemptId"),
            validation_mode=(
                "source-aware"
                if origin == "lingarr" and trusted_source_hash
                else "target-only"
            ),
            validator_version=VALIDATOR_VERSION,
            item_type=extra.get("itemType"),
            item_id=extra.get("itemId"),
        )
        if result in ("valid", "valid_with_warnings"):
            for language in LANGUAGES:
                if _target_suffix(target_path, language) is not None:
                    _clear_quarantine_hold(language, target_path=target_path)
                    break
        return True
    except (OSError, StateStoreError) as e:
        print(f"{YELLOW}[WARNING] Could not persist validation state: {e}{RESET}")
        return False


def _record_pending_lingarr_output(
    source_path: str,
    target_path: str,
    source_lang: str,
    target_lang: str,
    item_type: str,
    item_id: int,
) -> bool:
    source_hash = _file_hash_or_none(source_path)
    target_hash = _file_hash_or_none(target_path)
    if target_hash is None:
        return False
    try:
        identity = _target_identity_from_sidecar(target_path, target_lang)
        suffix = _target_suffix(target_path, target_lang)
        submission = (
            _get_validation_state().find_submission(identity, target_lang)
            if identity is not None else None
        )
        _get_validation_state().record(
            target_path,
            source_hash=source_hash,
            target_hash=target_hash,
            result="pending_validation",
            origin="lingarr",
            details={
                "sourcePath": source_path,
                "sourceLanguage": source_lang,
                "targetLanguage": target_lang,
                "itemType": item_type,
                "itemId": item_id,
            },
            source_path=source_path,
            source_language=source_lang,
            target_language=target_lang,
            target_identity=identity,
            target_variant=suffix[1] if suffix is not None else "",
            operation="translation",
            attempt_id=(
                submission.get("attemptId") if submission is not None else None
            ),
            validation_mode="source-aware",
        )
        return True
    except (OSError, StateStoreError) as exc:
        print(
            f"{YELLOW}[WARNING] Could not persist pending Lingarr provenance: "
            f"{exc}{RESET}"
        )
        return False


def _find_submission_for_target(
    target_path: str | Path,
    target_lang: str,
) -> dict | None:
    identity = _target_identity_from_sidecar(target_path, target_lang)
    if identity is None:
        return None
    return _get_validation_state().find_submission(identity, target_lang)


def _submission_matches_source(
    metadata: dict | None,
    source_path: str,
    source_language: str | None = None,
    target_path: str | Path | None = None,
    target_language: str | None = None,
) -> bool:
    if metadata is None:
        return False
    recorded_source = metadata.get("sourcePath")
    if not isinstance(recorded_source, str) or not recorded_source:
        return False
    recorded_language = metadata.get("sourceLanguage")
    if (
        recorded_language
        and source_language
        and str(recorded_language).casefold() != source_language.casefold()
    ):
        return False
    recorded_hash = metadata.get("sourceHash")
    if not recorded_hash:
        return False
    current_hash = _file_hash_or_none(source_path)
    if recorded_hash != current_hash:
        return False
    same_path = os.path.normcase(os.path.abspath(recorded_source)) == os.path.normcase(
        os.path.abspath(source_path)
    )
    return same_path or (
        target_path is not None
        and target_language is not None
        and _is_variant_aware_adjacent_source(
            source_path, source_language, target_path, target_language
        )
    )


def _is_variant_aware_adjacent_source(
    source_path: str | Path,
    source_language: str | None,
    target_path: str | Path,
    target_language: str,
) -> bool:
    if not source_language:
        return False
    source = Path(source_path)
    target = Path(target_path)
    if os.path.normcase(os.path.abspath(source.parent)) != os.path.normcase(
        os.path.abspath(target.parent)
    ):
        return False
    suffix = _target_suffix(target, target_language)
    if suffix is None:
        return False
    base_name, target_variant = suffix
    aliases = _LANGUAGE_ALIASES.get(source_language, {source_language})
    acceptable = {
        f"{base_name}.{alias}{variant}.srt".casefold()
        for alias in aliases
        for variant in ({target_variant, ""} if target_variant else {""})
    }
    return source.name.casefold() in acceptable


def _record_quarantine_hold(
    target_path: str | Path,
    target_lang: str,
    target_hash: str | None,
    report,
    origin: str | None,
) -> tuple[dict | None, bool]:
    if target_hash is None:
        return None, False
    identity = _quarantine_identity(target_lang, target_path=target_path)
    if identity is None:
        return None, False
    entry, repeated = _get_validation_state().record_quarantine_tombstone(
        identity,
        target_path=target_path,
        target_hash=target_hash,
        target_language=target_lang,
        rules=(issue.rule for issue in report.issues),
        origin=origin,
        hold_days=CLEANUP_QUARANTINE_HOLD_DAYS,
    )
    if repeated:
        print(
            f"[CLEANUP] Repeat offender hash for {os.path.basename(str(target_path))}; "
            f"occurrence {entry['occurrences']}, translation held until {entry['holdUntil']}"
        )
    return entry, repeated


def _apply_cleanup_action(
    target_path: str | Path,
    source_path: str | Path | None,
    target_lang: str,
    report,
    *,
    repair_attempts: int = 0,
    lingarr_outcome: str = "not attempted",
    attempt_history: list[dict] | None = None,
    format_fixes: list[str] | None = None,
    format_recovered_cues: list[int] | None = None,
    completeness=None,
    origin: str | None = None,
    dry_run: bool = False,
) -> str:
    from clean_et_subs import (
        quarantine_destination,
        quarantine_subtitle,
        write_validation_report,
    )

    target = Path(target_path)
    source_hash = _file_hash_or_none(source_path)
    target_hash = _file_hash_or_none(target)
    audit = {
        "sourcePath": str(source_path) if source_path is not None else None,
        "targetPath": str(target),
        "sourceHash": source_hash,
        "targetHash": target_hash,
        "targetLanguage": target_lang,
        "repairAttempts": repair_attempts,
        "repairAttemptHistory": attempt_history or [],
        "formatFixes": format_fixes or [],
        "formatRecoveredCues": format_recovered_cues or [],
        "lingarrOutcome": lingarr_outcome,
        "origin": origin or "unknown",
        "filenameClassification": "regular" if completeness is not None else None,
        "completeness": completeness.to_dict() if completeness is not None else None,
        "validation": report.to_dict(),
        "recordedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    if dry_run or CLEANUP_ACTION == "report":
        print(f"[CLEANUP] {'DRYRUN' if dry_run else 'REPORT'}: would remove {target}")
        _record_validation_result(
            target,
            source_hash,
            target_hash,
            "dry-run-invalid" if dry_run else "reported-invalid",
            report,
            origin=origin,
            repairAttempts=repair_attempts,
            repairAttemptHistory=attempt_history or [],
            formatFixes=format_fixes or [],
            formatRecoveredCues=format_recovered_cues or [],
            lingarrOutcome=lingarr_outcome,
            completeness=completeness.to_dict() if completeness is not None else None,
        )
        return "dry-run" if dry_run else "reported"

    if CLEANUP_ACTION == "quarantine":
        try:
            if target_hash is None:
                raise StateStoreError("target hash unavailable before quarantine")
            destination = quarantine_destination(
                target, CLEANUP_ROOTS, CLEANUP_QUARANTINE_DIR
            )
            artifact = _get_validation_state().latest_artifact(target, target_hash)
            if artifact is None:
                artifact_id = _get_validation_state().record_artifact_version(
                    target,
                    target_hash=target_hash,
                    source_path=source_path,
                    source_hash=source_hash if origin == "lingarr" else None,
                    source_language=None,
                    target_language=target_lang,
                    origin=origin or "external",
                    operation="quarantine",
                    target_identity=_target_identity_from_sidecar(
                        target, target_lang
                    ),
                    disposition="quarantine_pending",
                    pending_destination=destination,
                    pending_metadata={
                        "rules": [issue.rule for issue in report.issues],
                        "holdDays": CLEANUP_QUARANTINE_HOLD_DAYS,
                        "holdIdentity": _quarantine_identity(
                            target_lang, target_path=target
                        ),
                    },
                )
            else:
                artifact_id = int(artifact["id"])
                _get_validation_state().set_artifact_disposition(
                    artifact_id,
                    "quarantine_pending",
                    pending_destination=destination,
                    pending_metadata={
                        "rules": [issue.rule for issue in report.issues],
                        "holdDays": CLEANUP_QUARANTINE_HOLD_DAYS,
                        "holdIdentity": _quarantine_identity(
                            target_lang, target_path=target
                        ),
                    },
                )
            destination = quarantine_subtitle(
                target,
                CLEANUP_ROOTS,
                CLEANUP_QUARANTINE_DIR,
                destination=destination,
            )
            _get_validation_state().set_artifact_disposition(
                artifact_id, "quarantined"
            )
            hold, repeated = _record_quarantine_hold(
                target, target_lang, target_hash, report, origin
            )
            setattr(report, "repeat_offender", repeated)
            if hold is not None:
                audit["quarantineHold"] = hold
                audit["repeatOffender"] = repeated
            try:
                write_validation_report(destination, audit)
            except OSError as e:
                print(f"{YELLOW}[WARNING] Quarantined file but could not write report: {e}{RESET}")
            _record_validation_result(
                target,
                source_hash,
                target_hash,
                "quarantined",
                report,
                origin=origin,
                quarantinePath=str(destination),
                repairAttempts=repair_attempts,
                repairAttemptHistory=attempt_history or [],
                formatFixes=format_fixes or [],
                formatRecoveredCues=format_recovered_cues or [],
                lingarrOutcome=lingarr_outcome,
                completeness=completeness.to_dict() if completeness is not None else None,
            )
            print(f"[CLEANUP] Quarantined {target} -> {destination}")
            return "quarantined"
        except (OSError, StateStoreError) as e:
            print(f"{RED}[ERROR] Could not quarantine {target}: {e}{RESET}")
            return "action-failed"

    try:
        if target_hash is None:
            raise StateStoreError("target hash unavailable before deletion")
        artifact = _get_validation_state().latest_artifact(target, target_hash)
        if artifact is None:
            artifact_id = _get_validation_state().record_artifact_version(
                target,
                target_hash=target_hash,
                source_path=source_path,
                source_hash=source_hash if origin == "lingarr" else None,
                source_language=None,
                target_language=target_lang,
                origin=origin or "external",
                operation="delete",
                target_identity=_target_identity_from_sidecar(
                    target, target_lang
                ),
                disposition="deletion_pending",
                pending_metadata={
                    "rules": [issue.rule for issue in report.issues],
                    "holdDays": CLEANUP_QUARANTINE_HOLD_DAYS,
                    "holdIdentity": _quarantine_identity(
                        target_lang, target_path=target
                    ),
                },
            )
        else:
            artifact_id = int(artifact["id"])
            _get_validation_state().set_artifact_disposition(
                artifact_id,
                "deletion_pending",
                pending_metadata={
                    "rules": [issue.rule for issue in report.issues],
                    "holdDays": CLEANUP_QUARANTINE_HOLD_DAYS,
                    "holdIdentity": _quarantine_identity(
                        target_lang, target_path=target
                    ),
                },
            )
        target.unlink()
        _get_validation_state().set_artifact_disposition(artifact_id, "deleted")
        _record_quarantine_hold(target, target_lang, target_hash, report, origin)
        _record_validation_result(
            target,
            source_hash,
            target_hash,
            "deleted",
            report,
            origin=origin,
            repairAttempts=repair_attempts,
            repairAttemptHistory=attempt_history or [],
            formatFixes=format_fixes or [],
            formatRecoveredCues=format_recovered_cues or [],
            lingarrOutcome=lingarr_outcome,
            completeness=completeness.to_dict() if completeness is not None else None,
        )
        print(f"[CLEANUP] Deleted {target}")
        return "deleted"
    except (OSError, StateStoreError) as e:
        print(f"{RED}[ERROR] Could not delete {target}: {e}{RESET}")
        return "action-failed"


def _target_repair_lock(target_path: str | Path) -> threading.Lock:
    key = os.path.normcase(os.path.abspath(str(target_path)))
    with _target_repair_locks_lock:
        return _target_repair_locks.setdefault(key, threading.Lock())


def _write_recovery_candidate(
    target_path: str | Path,
    raw: str,
    *,
    same_directory: bool = True,
) -> Path:
    target = Path(target_path)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        prefix=f".{target.name}.recovery.",
        suffix=".srt",
        dir=target.parent if same_directory else None,
        delete=False,
    ) as handle:
        handle.write(raw)
        return Path(handle.name)


def _normalize_managed_output(path: str | Path, label: str) -> bool:
    from clean_et_subs import normalize_managed_file

    try:
        normalize_managed_file(path)
        return True
    except OSError as exc:
        print(
            f"{RED}[ERROR] Could not set managed ownership for {label}: {exc}{RESET}"
        )
        return False


def _replace_managed_file(candidate: str | Path, target: str | Path) -> None:
    from clean_et_subs import normalize_managed_file

    candidate_path = Path(candidate)
    try:
        normalize_managed_file(candidate_path)
        os.replace(candidate_path, target)
    except OSError:
        try:
            candidate_path.unlink()
        except OSError:
            pass
        raise


def _replace_managed_file_if_current(
    candidate: str | Path,
    target: str | Path,
    *,
    source_path: str | Path | None,
    expected_source_hash: str | None,
    expected_target_hash: str | None,
    source_language: str | None,
    target_language: str,
    origin: str | None,
    operation: str,
    parent_artifact_id: int | None,
) -> bool:
    if (
        expected_source_hash is not None
        and _file_hash_or_none(source_path) != expected_source_hash
    ) or (
        expected_target_hash is not None
        and _file_hash_or_none(target) != expected_target_hash
    ):
        try:
            Path(candidate).unlink()
        except OSError:
            pass
        return False
    candidate_hash = _file_hash_or_none(candidate)
    if candidate_hash is None:
        raise OSError(f"could not hash replacement candidate {candidate}")
    suffix = _target_suffix(target, target_language)
    pending_artifact_id = _get_validation_state().record_artifact_version(
        target,
        target_hash=candidate_hash,
        source_path=source_path,
        source_hash=expected_source_hash,
        source_language=source_language,
        target_language=target_language,
        origin=origin or "external",
        operation=operation,
        parent_artifact_id=parent_artifact_id,
        target_identity=_target_identity_from_sidecar(target, target_language),
        target_variant=suffix[1] if suffix is not None else "",
        disposition="replacement_pending",
        pending_destination=target,
    )
    _replace_managed_file(candidate, target)
    _get_validation_state().set_artifact_disposition(
        pending_artifact_id, "active"
    )
    return True


def _perform_repair(
    source_path: str,
    target_path: str,
    source_lang: str,
    target_lang: str,
    item_id: int | None,
    title: str,
    item_type: str | None,
    initial_report,
    expected_target_hash: str | None,
    expected_source_hash: str | None = None,
    recovery_raw: str | None = None,
    format_fixes: list[str] | None = None,
    format_recovered_cues: list[int] | None = None,
    completeness=None,
    origin: str | None = "lingarr",
) -> RepairJobResult:
    from clean_et_subs import repair_subtitle_file, target_language_for_code

    label = title or os.path.basename(target_path)
    detector = _get_cleanup_detector()
    target_language = target_language_for_code(target_lang)
    if detector is None or target_language is None:
        return RepairJobResult(
            "repair-deferred", initial_report, label, target_lang, item_type, item_id,
            target_path=str(target_path),
        )

    with _target_repair_lock(target_path):
        if expected_target_hash is not None and _file_hash_or_none(target_path) != expected_target_hash:
            print(f"[REPAIR] Deferred {label} '{target_lang}': target changed while queued")
            return RepairJobResult(
                "repair-deferred", initial_report, label, target_lang, item_type, item_id,
                target_path=str(target_path),
            )
        if expected_source_hash is not None and _file_hash_or_none(source_path) != expected_source_hash:
            print(f"[REPAIR] Deferred {label} '{target_lang}': source changed while queued")
            return RepairJobResult(
                "repair-deferred", initial_report, label, target_lang, item_type, item_id,
                target_path=str(target_path),
            )

        if recovery_raw is None:
            from clean_et_subs import read_text_best_effort
            recovery_raw = read_text_best_effort(Path(target_path))
            if recovery_raw is None:
                return RepairJobResult(
                    "repair-deferred", initial_report, label, target_lang,
                    item_type, item_id, target_path=str(target_path),
                )
        recovery_temp = _write_recovery_candidate(target_path, recovery_raw)
        working_path = recovery_temp

        attempt_state: dict = {}

        def attempt_logger(event: dict) -> None:
            attempt_state.clear()
            attempt_state.update(event)
            cue = event.get("cueNumber")
            attempt = event.get("attempt")
            maximum = event.get("maxAttempts")
            duration = event.get("durationSeconds", 0)
            http_status = event.get("httpStatus")
            http_label = f" HTTP {http_status}" if http_status is not None else ""
            worker = threading.current_thread().name
            if event["event"] == "sending":
                context = (
                    "without context"
                    if event.get("withoutContext")
                    else f"with context before={event.get('contextBefore', 0)} after={event.get('contextAfter', 0)}"
                )
                print(f"[REPAIR] {worker} sending {label} '{target_lang}' cue {cue} attempt {attempt}/{maximum} {context}")
            elif event["event"] == "accepted":
                print(f"[REPAIR] Cue {cue} attempt {attempt} accepted{http_label} after {duration:.1f}s")
            elif event["event"] == "rejected":
                rules = ",".join(event.get("validationRules", [])) or "validation"
                print(f"[REPAIR] Cue {cue} attempt {attempt} rejected{http_label} after {duration:.1f}s: {rules}")
            else:
                print(f"[REPAIR] Cue {cue} attempt {attempt} failed{http_label} after {duration:.1f}s: {event.get('outcome')}")

        def translator(line: str, before: list[str], after: list[str]):
            outcome_meta: dict = {}
            translated = lingarr_translate_line(
                line,
                source_lang,
                target_lang,
                before,
                after,
                repair_label=label,
                cue_number=attempt_state.get("cueNumber"),
                attempt=attempt_state.get("attempt"),
                outcome_meta=outcome_meta,
            )
            return translated, outcome_meta

        cue_list = ", ".join(str(i + 1) for i in initial_report.repairable_cue_indexes)
        print(f"[REPAIR] Retrying {label} '{target_lang}' cue position(s): {cue_list}")
        try:
            repair = repair_subtitle_file(
                Path(source_path),
                working_path,
                detector,
                target_language,
                translator,
                target_lang=target_lang,
                max_attempts=CLEANUP_MAX_REPAIR_ATTEMPTS,
                context_lines=CLEANUP_REPAIR_CONTEXT_LINES,
                attempt_logger=attempt_logger,
                **_validation_kwargs(),
            )
            second_attempts = sum(
                entry.get("attempt", 0) > 1 and entry.get("withoutContext")
                for entry in repair.attempt_history
            )
            if repair.success:
                if (
                    expected_source_hash is not None
                    and _file_hash_or_none(source_path) != expected_source_hash
                ):
                    print(
                        f"[REPAIR] Deferred {label} '{target_lang}': "
                        "source changed during repair"
                    )
                    return RepairJobResult(
                        "repair-deferred", repair.report, label, target_lang,
                        item_type, item_id, repair.attempts, second_attempts,
                        str(target_path),
                    )
                if (
                    expected_target_hash is not None
                    and _file_hash_or_none(target_path) != expected_target_hash
                ):
                    print(
                        f"[REPAIR] Deferred {label} '{target_lang}': "
                        "target changed during repair"
                    )
                    return RepairJobResult(
                        "repair-deferred", repair.report, label, target_lang,
                        item_type, item_id, repair.attempts, second_attempts,
                        str(target_path),
                    )
                parent = _get_validation_state().latest_artifact(
                    target_path, expected_target_hash
                )
                candidate_hash = _file_hash_or_none(recovery_temp)
                if candidate_hash is None:
                    return RepairJobResult(
                        "repair-deferred", repair.report, label, target_lang,
                        item_type, item_id, repair.attempts, second_attempts,
                        str(target_path),
                    )
                suffix = _target_suffix(target_path, target_lang)
                try:
                    pending_artifact_id = _get_validation_state().record_artifact_version(
                        target_path,
                        target_hash=candidate_hash,
                        source_path=source_path,
                        source_hash=expected_source_hash,
                        source_language=source_lang,
                        target_language=target_lang,
                        origin=origin or "external",
                        operation="cue_repair",
                        parent_artifact_id=parent.get("id") if parent else None,
                        target_identity=_target_identity_from_sidecar(
                            target_path, target_lang
                        ),
                        target_variant=suffix[1] if suffix is not None else "",
                        disposition="replacement_pending",
                        pending_destination=target_path,
                    )
                except StateStoreError as exc:
                    print(
                        f"{YELLOW}[REPAIR] Deferred {label} '{target_lang}': "
                        f"could not persist replacement intent ({exc}){RESET}"
                    )
                    return RepairJobResult(
                        "repair-deferred", repair.report, label, target_lang,
                        item_type, item_id, repair.attempts, second_attempts,
                        str(target_path),
                    )
                _replace_managed_file(recovery_temp, target_path)
                recovery_temp = None
                try:
                    _get_validation_state().set_artifact_disposition(
                        pending_artifact_id, "active"
                    )
                except StateStoreError as exc:
                    print(
                        f"{YELLOW}[REPAIR] Replacement completed but state "
                        f"finalization was deferred: {exc}{RESET}"
                    )
                    return RepairJobResult(
                        "repair-deferred", repair.report, label, target_lang,
                        item_type, item_id, repair.attempts, second_attempts,
                        str(target_path),
                    )
                repaired = ", ".join(str(number) for number in repair.repaired_cues)
                print(f"{GREEN}[REPAIR] Repaired and validated {label} '{target_lang}' cue(s): {repaired}{RESET}")
                if not _record_validation_result(
                    target_path,
                    _file_hash_or_none(source_path),
                    _file_hash_or_none(target_path),
                    "valid",
                    repair.report,
                    origin=origin,
                    repairedCues=repair.repaired_cues,
                    repairAttempts=repair.attempts,
                    repairAttemptHistory=repair.attempt_history,
                    formatFixes=format_fixes or [],
                    formatRecoveredCues=format_recovered_cues or [],
                    lingarrOutcome="repaired",
                    completeness=completeness.to_dict() if completeness is not None else None,
                    sourcePath=source_path,
                    sourceLanguage=source_lang,
                    targetLanguage=target_lang,
                    operation="cue_repair",
                    parentArtifactId=parent.get("id") if parent else None,
                ):
                    return RepairJobResult(
                        "repair-deferred", repair.report, label, target_lang,
                        item_type, item_id, repair.attempts, second_attempts,
                        str(target_path),
                    )
                return RepairJobResult(
                    "repaired", repair.report, label, target_lang, item_type, item_id,
                    repair.attempts, second_attempts, str(target_path),
                )

            print(f"{YELLOW}[REPAIR] Could not repair {label} '{target_lang}': {repair.reason}{RESET}")
            action = _apply_cleanup_action(
                target_path,
                source_path,
                target_lang,
                repair.report,
                repair_attempts=repair.attempts,
                lingarr_outcome=repair.reason,
                attempt_history=repair.attempt_history,
                format_fixes=format_fixes,
                format_recovered_cues=format_recovered_cues,
                completeness=completeness,
                origin=origin,
            )
            if action in ("quarantined", "deleted") and item_id is not None:
                _clear_submission(item_id, target_lang, item_type)
                print(f"[CLEANUP] Cleared cooldown for retry: {label} '{target_lang}'")
            return RepairJobResult(
                action, repair.report, label, target_lang, item_type, item_id,
                repair.attempts, second_attempts, str(target_path),
            )
        finally:
            if recovery_temp is not None:
                try:
                    recovery_temp.unlink()
                except OSError:
                    pass


def _get_repair_executor() -> ThreadPoolExecutor:
    global _repair_executor
    with _repair_executor_lock:
        if _repair_executor is None:
            _repair_executor = ThreadPoolExecutor(
                max_workers=CLEANUP_REPAIR_WORKERS,
                thread_name_prefix="repair-worker",
            )
        return _repair_executor


def _publish_repair_status(future: Future, metadata: dict) -> None:
    """Publish a repair's terminal dashboard state without draining its future."""
    try:
        result = future.result()
    except Exception:
        _status_transition(
            metadata.get("item_type"),
            metadata.get("item_id"),
            metadata.get("target_lang", ""),
            "failed",
            reason="repair worker failed",
        )
        return

    if result.action == "repaired":
        _status_transition(
            result.item_type,
            result.item_id,
            result.target_lang,
            "accepted",
            repaired=True,
        )
    elif result.action in ("quarantined", "deleted"):
        _status_transition(
            result.item_type,
            result.item_id,
            result.target_lang,
            "quarantined",
            reason=result.action,
        )
    elif result.action == "repair-deferred":
        _status_transition(
            result.item_type,
            result.item_id,
            result.target_lang,
            "deferred",
            reason="repair deferred",
        )
    else:
        _status_transition(
            result.item_type,
            result.item_id,
            result.target_lang,
            "failed",
            reason=f"repair {result.action}",
        )


def _queue_repair(repair_key: tuple, job_kwargs: dict, report, label: str, target_lang: str) -> str:
    with _pending_repairs_lock:
        if repair_key in _repair_keys:
            print(f"[REPAIR] Duplicate repair suppressed for {label} '{target_lang}'")
            return "repair-duplicate"
        if not _repair_capacity.acquire(blocking=False):
            print(f"[REPAIR] Queue full; deferred {label} '{target_lang}' to the next scan")
            return "repair-deferred"
        _repair_keys.add(repair_key)
        try:
            future = _get_repair_executor().submit(_perform_repair, **job_kwargs)
        except Exception:
            _repair_keys.discard(repair_key)
            _repair_capacity.release()
            raise
        metadata = {
            "key": repair_key,
            "report": report,
            "target_path": job_kwargs.get("target_path"),
            "item_type": job_kwargs.get("item_type"),
            "item_id": job_kwargs.get("item_id"),
            "target_lang": job_kwargs.get("target_lang"),
        }
        _pending_repairs[future] = metadata
    future.add_done_callback(
        lambda completed, repair_metadata=metadata: _publish_repair_status(
            completed, repair_metadata
        )
    )
    for index in report.repairable_cue_indexes:
        print(f"[REPAIR] Queued {label} '{target_lang}' cue position {index + 1}")
    return "repair-queued"


def _drain_pending_repairs(stats: dict) -> list[RepairJobResult]:
    with _pending_repairs_lock:
        futures = list(_pending_repairs)
    results: list[RepairJobResult] = []
    for future in as_completed(futures):
        with _pending_repairs_lock:
            metadata = _pending_repairs.pop(future, {})
            _repair_keys.discard(metadata.get("key"))
        _repair_capacity.release()
        try:
            result = future.result()
        except Exception as exc:
            print(f"{RED}[ERROR] Repair worker failed: {exc}{RESET}")
            stats["cleanup_repair_failures"] = stats.get("cleanup_repair_failures", 0) + 1
            _publish_repair_status(future, metadata)
            continue
        results.append(result)
        _publish_repair_status(future, metadata)
        stats["cleanup_repair_attempts"] = stats.get("cleanup_repair_attempts", 0) + result.attempts
        stats["cleanup_second_attempts"] = stats.get("cleanup_second_attempts", 0) + result.second_attempts
        _record_cleanup_stats(stats, result.action, result.report)
        if result.action == "repaired":
            stats["completed"] += 1
            stats["translations"].append(f"{result.title}: repaired {result.target_lang}")
            if result.item_type:
                _mark_activity(stats, result.item_type)
            else:
                stats["episode_activity"] = True
                stats["movie_activity"] = True
        elif result.action in ("quarantined", "deleted"):
            stats["failed"] += 1
            stats["cleaned"] = stats.get("cleaned", 0) + 1
            if result.item_type:
                _mark_activity(stats, result.item_type)
            else:
                stats["episode_activity"] = True
                stats["movie_activity"] = True
        elif result.action == "repair-deferred":
            stats["cleanup_repair_deferred"] = stats.get("cleanup_repair_deferred", 0) + 1
    return results


def _shutdown_repair_executor() -> None:
    global _repair_executor
    with _repair_executor_lock:
        executor = _repair_executor
        _repair_executor = None
    if executor is not None:
        print("[REPAIR] Waiting for active repair worker(s) to stop")
        executor.shutdown(wait=True, cancel_futures=False)


def _validate_translated_file(
    source_path: str,
    target_path: str,
    source_lang: str,
    target_lang: str,
    item_id: int | None,
    title: str = "",
    dry_run: bool = False,
    *,
    defer_repair: bool = False,
    item_type: str | None = None,
    media_duration: float | None = None,
    origin: str | None = None,
    provenance_source_hash: str | None = None,
) -> tuple[str, object]:
    if target_lang not in CLEANUP_LANGUAGES:
        from clean_et_subs import validate_srt_structure

        report = validate_srt_structure(target_path)
        completeness = _evaluate_completeness(target_path, media_duration)
        _add_completeness_issue(report, completeness)
        if report.valid:
            if not _record_validation_result(
                target_path,
                _file_hash_or_none(source_path),
                _file_hash_or_none(target_path),
                "valid",
                report,
                origin=origin,
                completeness=completeness.to_dict() if completeness is not None else None,
            ):
                return "repair-deferred", report
            return "valid", report
        label = title or os.path.basename(target_path)
        print(f"{YELLOW}[CLEANUP] Invalid translation {label} '{target_lang}': {report.summary()}{RESET}")
        action = _apply_cleanup_action(
            target_path,
            source_path,
            target_lang,
            report,
            lingarr_outcome="not attempted: file-level issue is not cue-repairable",
            completeness=completeness,
            origin=origin,
            dry_run=dry_run,
        )
        return action, report

    from clean_et_subs import (
        recover_subtitle_pair,
        target_language_for_code,
        validate_subtitle_pair,
        validate_subtitle_without_source,
    )

    target_language = target_language_for_code(target_lang)
    detector = _get_cleanup_detector()
    if target_language is None or detector is None:
        return "valid", None

    source_hash = _file_hash_or_none(source_path)
    expected_target_hash = _file_hash_or_none(target_path)
    target_suffix = _target_suffix(target_path, target_lang)
    target_identity = _target_identity_from_sidecar(target_path, target_lang)
    target_variant = target_suffix[1] if target_suffix is not None else None
    recorded = (
        _get_validation_state().matching_record(
            target_path,
            expected_target_hash,
            target_identity=target_identity,
            target_language=target_lang,
            target_variant=target_variant,
        )
        if expected_target_hash is not None else None
    )
    recorded_origin = recorded.get("origin") if recorded is not None else None
    recorded_source_aligned = bool(
        recorded_origin == "lingarr"
        and source_hash is not None
        and recorded.get("sourceHash") is not None
        and recorded.get("sourceHash") == source_hash
        and (
            not recorded.get("sourceLanguage")
            or recorded.get("sourceLanguage") == source_lang
        )
        and (
            not recorded.get("sourcePath")
            or os.path.normcase(os.path.abspath(recorded["sourcePath"]))
            == os.path.normcase(os.path.abspath(source_path))
            or _is_variant_aware_adjacent_source(
                source_path, source_lang, target_path, target_lang
            )
        )
    )
    explicit_source_aligned = bool(
        origin == "lingarr"
        and provenance_source_hash is not None
        and provenance_source_hash == source_hash
        and recorded_source_aligned
    )
    if recorded_origin == "lingarr" and not recorded_source_aligned:
        print(
            f"{YELLOW}[CLEANUP] Lingarr provenance source changed for "
            f"{os.path.basename(target_path)}; using conservative target-only "
            f"validation{RESET}"
        )
    if origin == "lingarr" and not explicit_source_aligned:
        print(
            f"{YELLOW}[CLEANUP] Unverified Lingarr provenance for "
            f"{os.path.basename(target_path)}; using conservative target-only "
            f"validation{RESET}"
        )
    source_aligned = recorded_source_aligned
    effective_origin = "lingarr" if source_aligned else (
        origin if origin != "lingarr" else None
    )
    if source_aligned:
        report = validate_subtitle_pair(
            Path(source_path), Path(target_path), detector, target_language,
            target_lang=target_lang, **_validation_kwargs(),
        )
    else:
        report = validate_subtitle_without_source(
            Path(target_path), detector, target_language,
            target_lang=target_lang, **_validation_kwargs(),
        )
    completeness = _evaluate_completeness(target_path, media_duration)
    _add_completeness_issue(report, completeness)
    if (
        not source_aligned
        and _source_less_line_only_warning(report)
    ):
        print(
            f"{YELLOW}[CLEANUP] Retained {os.path.basename(target_path)} with "
            f"source-less line-count warning: {report.summary()}{RESET}"
        )
        if not _record_validation_result(
            target_path,
            source_hash,
            expected_target_hash,
            "valid_with_warnings",
            report,
            origin=effective_origin,
            warningRules=["excessive_lines"],
            completeness=completeness.to_dict() if completeness is not None else None,
        ):
            return "repair-deferred", report
        return "valid-warning", report
    if report.valid:
        if CLEANUP_FORMAT_REPAIR_ENABLED and source_aligned:
            recovery = recover_subtitle_pair(source_path, target_path)
            if recovery.safe and recovery.changed and recovery.raw is not None:
                candidate = _write_recovery_candidate(target_path, recovery.raw, same_directory=False)
                try:
                    normalized_report = validate_subtitle_pair(
                        Path(source_path), candidate, detector, target_language,
                        target_lang=target_lang, **_validation_kwargs(),
                    )
                    _add_completeness_issue(normalized_report, completeness)
                finally:
                    try:
                        candidate.unlink()
                    except OSError:
                        pass
                if not normalized_report.valid:
                    print(
                        f"{YELLOW}[FORMAT] Normalized candidate rejected for "
                        f"{os.path.basename(target_path)}: {normalized_report.summary()}{RESET}"
                    )
                    recovery = None
                if recovery is None:
                    print(f"[CLEANUP] OK {os.path.basename(target_path)} (original retained)")
                    if not _record_validation_result(
                        target_path, source_hash, expected_target_hash, "valid", report,
                        origin=effective_origin,
                        completeness=completeness.to_dict() if completeness is not None else None,
                    ):
                        return "repair-deferred", report
                    return "valid", report
                if dry_run:
                    print(f"[FORMAT] DRYRUN: would normalize {target_path}")
                    return "dry-run", report
                try:
                    with _target_repair_lock(target_path):
                        temp = _write_recovery_candidate(target_path, recovery.raw)
                        replaced = _replace_managed_file_if_current(
                            temp,
                            target_path,
                            source_path=source_path,
                            expected_source_hash=source_hash,
                            expected_target_hash=expected_target_hash,
                            source_language=source_lang,
                            target_language=target_lang,
                            origin=effective_origin,
                            operation="format_repair",
                            parent_artifact_id=(
                                recorded.get("artifactId") if recorded else None
                            ),
                        )
                except (OSError, StateStoreError) as exc:
                    print(
                        f"{RED}[ERROR] Could not normalize {target_path}: {exc}{RESET}"
                    )
                    return "action-failed", report
                if not replaced:
                    print(
                        f"{YELLOW}[FORMAT] Deferred {target_path}: "
                        "source or target changed during normalization"
                        f"{RESET}"
                    )
                    return "repair-deferred", report
                print(
                    f"{GREEN}[FORMAT] Normalized {os.path.basename(target_path)} without AI: "
                    f"{', '.join(recovery.fixes) or 'canonicalized'}{RESET}"
                )
                if not _record_validation_result(
                    target_path,
                    source_hash,
                    _file_hash_or_none(target_path),
                    "valid",
                    normalized_report,
                    origin=effective_origin,
                    formatFixes=recovery.fixes,
                    formatRecoveredCues=recovery.recovered_cues,
                    completeness=completeness.to_dict() if completeness is not None else None,
                    sourcePath=source_path,
                    sourceLanguage=source_lang,
                    targetLanguage=target_lang,
                    operation="format_repair",
                    parentArtifactId=recorded.get("artifactId") if recorded else None,
                ):
                    return "repair-deferred", normalized_report
                return "formatted", normalized_report
        mode = "source-aware" if source_aligned else "independent target"
        print(f"[CLEANUP] OK {os.path.basename(target_path)} ({mode} validation passed)")
        if not _record_validation_result(
            target_path, source_hash, expected_target_hash, "valid", report,
            origin=effective_origin,
            completeness=completeness.to_dict() if completeness is not None else None,
        ):
            return "repair-deferred", report
        return "valid", report

    label = title or os.path.basename(target_path)
    print(f"{YELLOW}[CLEANUP] Invalid translation {label} '{target_lang}': {report.summary()}{RESET}")
    quarantine_identity = _quarantine_identity(target_lang, target_path=target_path)
    active_tombstone = (
        _get_validation_state().active_quarantine_tombstone(
            quarantine_identity, target_hash=expected_target_hash
        )
        if quarantine_identity is not None else None
    )
    repeat_invalid_hash = bool(
        active_tombstone
        and expected_target_hash
        and active_tombstone.get("targetHash") == expected_target_hash
    )
    if repeat_invalid_hash:
        setattr(report, "ai_repair_suppressed", True)
        print(
            f"[CLEANUP] Known invalid hash reappeared for {label}; "
            "skipping duplicate AI repair"
        )
    recovery_raw = None
    format_fixes: list[str] = []
    format_recovered_cues: list[int] = []
    if CLEANUP_FORMAT_REPAIR_ENABLED and source_aligned:
        recovery = recover_subtitle_pair(source_path, target_path)
        if recovery.safe and recovery.changed and recovery.raw is not None:
            candidate = _write_recovery_candidate(target_path, recovery.raw, same_directory=False)
            try:
                recovered_report = validate_subtitle_pair(
                    Path(source_path), candidate, detector, target_language,
                    target_lang=target_lang, **_validation_kwargs(),
                )
                _add_completeness_issue(recovered_report, completeness)
            finally:
                try:
                    candidate.unlink()
                except OSError:
                    pass
            format_fixes = recovery.fixes
            format_recovered_cues = recovery.recovered_cues
            print(
                f"[FORMAT] Source-anchored recovery prepared for {label} '{target_lang}': "
                f"{', '.join(format_fixes) or 'canonicalized'}"
            )
            if recovered_report.valid:
                if dry_run:
                    print(f"[FORMAT] DRYRUN: would atomically repair {target_path}")
                    return "dry-run", report
                try:
                    with _target_repair_lock(target_path):
                        temp = _write_recovery_candidate(target_path, recovery.raw)
                        replaced = _replace_managed_file_if_current(
                            temp,
                            target_path,
                            source_path=source_path,
                            expected_source_hash=source_hash,
                            expected_target_hash=expected_target_hash,
                            source_language=source_lang,
                            target_language=target_lang,
                            origin=effective_origin,
                            operation="format_repair",
                            parent_artifact_id=(
                                recorded.get("artifactId") if recorded else None
                            ),
                        )
                except (OSError, StateStoreError) as exc:
                    print(
                        f"{RED}[ERROR] Could not repair {target_path}: {exc}{RESET}"
                    )
                    return "action-failed", report
                if not replaced:
                    print(
                        f"{YELLOW}[FORMAT] Deferred {target_path}: "
                        "source or target changed during format repair"
                        f"{RESET}"
                    )
                    return "repair-deferred", report
                print(f"{GREEN}[FORMAT] Repaired and validated {label} '{target_lang}' without AI{RESET}")
                if not _record_validation_result(
                    target_path, source_hash, _file_hash_or_none(target_path), "valid", recovered_report,
                    origin=effective_origin,
                    formatFixes=format_fixes, formatRecoveredCues=format_recovered_cues,
                    completeness=completeness.to_dict() if completeness is not None else None,
                    sourcePath=source_path,
                    sourceLanguage=source_lang,
                    targetLanguage=target_lang,
                    operation="format_repair",
                    parentArtifactId=recorded.get("artifactId") if recorded else None,
                ):
                    return "repair-deferred", recovered_report
                return "formatted", recovered_report
            report = recovered_report
            recovery_raw = recovery.raw
        elif not recovery.safe:
            dbg(f"Format recovery unsafe for {label}: {recovery.reason}")

    if (
        source_aligned
        and CLEANUP_REPAIR_ENABLED
        and report.repairable_cue_indexes
        and not dry_run
        and not repeat_invalid_hash
    ):
        job_kwargs = {
            "source_path": source_path,
            "target_path": target_path,
            "source_lang": source_lang,
            "target_lang": target_lang,
            "item_id": item_id,
            "title": label,
            "item_type": item_type,
            "initial_report": report,
            "expected_target_hash": expected_target_hash,
            "expected_source_hash": source_hash,
            "recovery_raw": recovery_raw,
            "format_fixes": format_fixes,
            "format_recovered_cues": format_recovered_cues,
            "completeness": completeness,
            "origin": effective_origin,
        }
        if defer_repair:
            repair_key = (
                os.path.normcase(os.path.abspath(target_path)), source_hash, expected_target_hash,
                target_lang, tuple(report.repairable_cue_indexes),
            )
            return _queue_repair(repair_key, job_kwargs, report, label, target_lang), report
        result = _perform_repair(**job_kwargs)
        return result.action, result.report

    action = _apply_cleanup_action(
        target_path,
        source_path,
        target_lang,
        report,
        format_fixes=format_fixes,
        format_recovered_cues=format_recovered_cues,
        completeness=completeness,
        origin=effective_origin,
        dry_run=dry_run,
    )
    if action in ("quarantined", "deleted") and item_id is not None:
        _clear_submission(item_id, target_lang)
        print(
            f"[CLEANUP] Cleared submission cooldown for {label} '{target_lang}'; "
            f"quarantine hold remains active"
        )
    return action, report

# ---------------------------------------------------------------------------
# Per-item processor
# ---------------------------------------------------------------------------

def _item_title(item: dict, item_type: str) -> str:
    if item_type == "episodes":
        return item.get("seriesTitle", item.get("series_title", "Unknown"))
    return item.get("title", "Unknown")


def _mark_activity(stats: dict, item_type: str) -> None:
    if item_type == "episodes":
        stats["episode_activity"] = True
    else:
        stats["movie_activity"] = True


def _bazarr_has_repaired_path(result: RepairJobResult) -> bool:
    if result.item_id is None or result.item_type not in ("episodes", "movies"):
        return True
    try:
        _, subtitles = fetch_subtitles(result.item_type, result.item_id)
    except ServiceRequestError as exc:
        print(f"{YELLOW}[WARNING] Could not verify repaired path: {exc}{RESET}")
        return False
    expected = os.path.normcase(os.path.normpath(result.target_path))
    return any(
        os.path.normcase(os.path.normpath(str(subtitle.get("path", "")))) == expected
        for subtitle in subtitles
    )


def _record_cleanup_stats(stats: dict, action: str, report) -> None:
    if report is None:
        return
    stats["cleanup_checked"] = stats.get("cleanup_checked", 0) + 1
    excessive = sum(issue.rule == "excessive_lines" for issue in report.issues)
    undersized = sum(issue.rule == "undersized_subtitle" for issue in report.issues)
    stats["cleanup_excessive_lines"] = stats.get("cleanup_excessive_lines", 0) + excessive
    stats["cleanup_undersized_targets"] = stats.get("cleanup_undersized_targets", 0) + undersized
    other = max(0, len(report.issues) - excessive - undersized)
    stats["cleanup_other_issues"] = stats.get("cleanup_other_issues", 0) + other
    stats["cleanup_repeat_quarantines"] = stats.get(
        "cleanup_repeat_quarantines", 0
    ) + int(bool(getattr(report, "repeat_offender", False)))
    stats["cleanup_ai_repairs_suppressed"] = stats.get(
        "cleanup_ai_repairs_suppressed", 0
    ) + int(bool(getattr(report, "ai_repair_suppressed", False)))
    if action == "valid-warning":
        stats["cleanup_source_less_warnings"] = stats.get(
            "cleanup_source_less_warnings", 0
        ) + 1
    elif action == "formatted":
        stats["cleanup_formatted"] = stats.get("cleanup_formatted", 0) + 1
    elif action == "repaired":
        stats["cleanup_repaired"] = stats.get("cleanup_repaired", 0) + 1
    elif action in ("quarantined", "deleted", "reported", "dry-run"):
        stats[f"cleanup_{action}"] = stats.get(f"cleanup_{action}", 0) + 1
    elif action == "action-failed":
        stats["cleanup_action_failed"] = stats.get("cleanup_action_failed", 0) + 1


def _source_is_usable(
    source_path: str,
    source_lang: str,
    media_duration: float | None,
    title: str,
    item_type: str,
    stats: dict,
    stats_lock: threading.Lock,
) -> bool:
    from clean_et_subs import validate_srt_structure

    report = validate_srt_structure(source_path)
    completeness = _evaluate_completeness(source_path, media_duration)
    _add_completeness_issue(report, completeness)
    if report.valid:
        return True
    print(f"{YELLOW}[SOURCE] Rejected {title} '{source_lang}': {report.summary()}{RESET}")
    action = _apply_cleanup_action(
        source_path,
        None,
        source_lang,
        report,
        completeness=completeness,
        origin="bazarr",
        lingarr_outcome="not attempted: source is not suitable for full translation",
    )
    with stats_lock:
        stats["cleanup_undersized_sources"] = stats.get("cleanup_undersized_sources", 0) + int(
            completeness is not None and completeness.undersized
        )
        _record_cleanup_stats(stats, action, report)
        if action in ("quarantined", "deleted"):
            _mark_activity(stats, item_type)
    return False


def process_item(item: dict, item_type: str, id_field: str,
                 stats: dict, stats_lock: threading.Lock) -> None:
    if shutdown_requested:
        return

    item_id = item.get(id_field)
    if item_id is None:
        return
    title = _item_title(item, item_type)
    lingarr_media_type = "Episode" if item_type == "episodes" else "Movie"

    missing_raw = {
        str(s.get("code2")).strip().lower()
        for s in item.get("missing_subtitles", [])
        if s.get("code2")
    }
    missing = {l for l in LANGUAGES if l in missing_raw}

    if not missing:
        return

    try:
        video_path, subs = fetch_subtitles(item_type, item_id)
    except ServiceRequestError as exc:
        print(f"{YELLOW}[DEFER] {title}: {exc}{RESET}")
        with stats_lock:
            stats["api_errors"] = stats.get("api_errors", 0) + 1
            stats["deferred"] = stats.get("deferred", 0) + len(missing)
        for target_lang in missing:
            _status_transition(
                item_type,
                item_id,
                target_lang,
                "deferred",
                reason="Bazarr subtitle lookup unavailable",
            )
        return
    if video_path:
        _queue_video_for_pruning(video_path, item_type)
    available_by_lang: dict[str, list[str]] = {}
    for s in subs:
        code, path = s.get("code2"), s.get("path", "")
        if not code or not path:
            continue
        code = str(code).strip().lower()
        if _truthy(s.get("forced")) or (
            video_path and _explicit_non_full_sidecar(video_path, path) is not None
        ):
            with stats_lock:
                stats["cleanup_forced_sources_skipped"] = stats.get("cleanup_forced_sources_skipped", 0) + 1
            continue
        available_by_lang.setdefault(code, []).append(path)
    for code, paths in available_by_lang.items():
        paths.sort(key=lambda path: _sub_priority(path, code))

    target_langs = [l for l in LANGUAGES if l in missing and l not in available_by_lang]
    source_langs = [l for l in LANGUAGES if l in available_by_lang]
    for already_available in missing - set(target_langs):
        _status_transition(
            item_type,
            item_id,
            already_available,
            "deferred",
            reason="subtitle already reported on disk",
        )

    if not source_langs:
        print(f"[SKIP] {title}: no source subtitle available from {LANGUAGES}")
        for target_lang in target_langs:
            _status_transition(
                item_type, item_id, target_lang, "deferred", reason="no source subtitle"
            )
        return
    if not target_langs:
        return

    media_duration = _probe_media_duration(video_path) if video_path and CLEANUP_UNDERSIZED_ENABLED else None
    source_lang = ""
    source_path = ""
    rejected_sources = 0
    for candidate_lang in source_langs:
        for candidate_path in available_by_lang[candidate_lang]:
            if _source_is_usable(
                candidate_path, candidate_lang, media_duration, title, item_type, stats, stats_lock
            ):
                source_lang = candidate_lang
                source_path = candidate_path
                break
            rejected_sources += 1
        if source_path:
            break
    if not source_path:
        print(f"{YELLOW}[SKIP] {title}: no complete source subtitle available{RESET}")
        for target_lang in target_langs:
            _status_transition(
                item_type,
                item_id,
                target_lang,
                "deferred",
                reason="no complete source subtitle",
            )
        return
    if rejected_sources:
        with stats_lock:
            stats["cleanup_alternative_sources"] = stats.get("cleanup_alternative_sources", 0) + 1
        print(f"[SOURCE] {title}: selected fallback '{source_lang}' after rejecting {rejected_sources} source(s)")
    if item_type == "episodes":
        _se = _re.search(r"[Ss](\d{1,2})[Ee](\d{1,2})", os.path.basename(source_path))
        if _se:
            title = f"{title} S{int(_se.group(1)):02d}E{int(_se.group(2)):02d}"
    item_timeout = _estimate_timeout(source_path)
    print(f"[INFO] {title}: source={source_lang}, targets={target_langs}")

    media_id = lingarr_resolve_media_id(item_type, item_id)
    if media_id is None:
        print(f"{YELLOW}[SKIP] {title}: not found in Lingarr media cache (id={item_id}){RESET}")
        for target_lang in target_langs:
            _status_transition(
                item_type,
                item_id,
                target_lang,
                "deferred",
                reason="media missing from Lingarr cache",
            )
        return

    for target_lang in target_langs:
        if shutdown_requested:
            break

        target_path = _derive_target_path(source_path, source_lang, target_lang)
        if not target_path and video_path:
            target_path = os.path.splitext(video_path)[0] + f".{target_lang}.srt"
        if not target_path:
            print(f"{YELLOW}[SKIP] {title} '{target_lang}': could not derive target path{RESET}")
            _status_transition(
                item_type,
                item_id,
                target_lang,
                "deferred",
                reason="target path unavailable",
            )
            continue
        target_suffix = _target_suffix(target_path, target_lang)
        target_variant = target_suffix[1] if target_suffix is not None else ""
        print(
            f"[TRANSLATE] Expected target for {title} '{target_lang}': "
            f"{os.path.basename(target_path)}"
        )

        existing = _find_existing_target(video_path, target_lang) if video_path else (
            target_path if os.path.exists(target_path) else None
        )
        if existing:
            print(f"[DISK] {title} '{target_lang}': {os.path.basename(existing)} already on disk")
            submission = _find_submission_for_target(existing, target_lang)
            recovered_origin = (
                "lingarr"
                if _submission_matches_source(
                    submission,
                    source_path,
                    source_lang,
                    existing,
                    target_lang,
                )
                else None
            )
            if recovered_origin:
                with stats_lock:
                    stats["recovered_pending_outputs"] = (
                        stats.get("recovered_pending_outputs", 0) + 1
                    )
                print(
                    f"[TRANSLATE] Recovered pending Lingarr output "
                    f"{os.path.basename(existing)}"
                )
                if not _normalize_managed_output(existing, title):
                    with stats_lock:
                        stats["deferred"] = stats.get("deferred", 0) + 1
                    _status_transition(
                        item_type,
                        item_id,
                        target_lang,
                        "deferred",
                        reason="managed file ownership failed",
                    )
                    continue
            _status_transition(item_type, item_id, target_lang, "validating")
            validation_action, validation_report = _validate_translated_file(
                source_path, existing, source_lang, target_lang, item_id, title=title,
                defer_repair=True, item_type=item_type, media_duration=media_duration,
                origin=recovered_origin,
                provenance_source_hash=(
                    submission.get("sourceHash") if recovered_origin else None
                ),
            )
            if validation_action in ("valid", "valid-warning", "formatted", "repaired"):
                with stats_lock:
                    stats["completed"] += 1
                    stats["translations"].append(f"{title}: {source_lang} -> {target_lang} (on disk)")
                    _record_cleanup_stats(stats, validation_action, validation_report)
                    _mark_activity(stats, item_type)
            elif validation_action.startswith("repair-"):
                with stats_lock:
                    stats["cleanup_repair_queued"] = stats.get("cleanup_repair_queued", 0) + (
                        validation_action == "repair-queued"
                    )
                    stats["cleanup_repair_deferred"] = stats.get("cleanup_repair_deferred", 0) + (
                        validation_action == "repair-deferred"
                    )
            else:
                with stats_lock:
                    stats["failed"] += 1
                    stats.setdefault("cleaned", 0)
                    stats["cleaned"] += 1
                    _record_cleanup_stats(stats, validation_action, validation_report)
                    if validation_action in ("quarantined", "deleted"):
                        _mark_activity(stats, item_type)
            _status_finish_validation(item_type, item_id, target_lang, validation_action)
            continue

        if video_path:
            hold = _active_quarantine_hold(video_path, target_lang)
            if hold is not None:
                with stats_lock:
                    stats["quarantine_holds"] = stats.get("quarantine_holds", 0) + 1
                    stats["deferred"] = stats.get("deferred", 0) + 1
                print(
                    f"[SKIP] {title} '{target_lang}': quarantine hold until "
                    f"{hold.get('holdUntil')} after {hold.get('occurrences', 1)} "
                    "invalid occurrence(s)"
                )
                _status_transition(
                    item_type,
                    item_id,
                    target_lang,
                    "deferred",
                    reason="quarantine hold",
                )
                continue

        try:
            age = _check_cooldown(item_id, target_lang, item_type)
        except StateStoreError as exc:
            print(f"{YELLOW}[DEFER] State unavailable for cooldown check: {exc}{RESET}")
            with stats_lock:
                stats["deferred"] = stats.get("deferred", 0) + 1
            _status_transition(
                item_type, item_id, target_lang, "deferred",
                reason="persistent state unavailable",
            )
            continue
        if age is not None:
            cooldown_remaining = RESUBMIT_COOLDOWN - age
            print(f"[SKIP] {title} '{target_lang}': submitted {age}s ago, "
                  f"cooldown {cooldown_remaining}s remaining")
            _status_transition(
                item_type,
                item_id,
                target_lang,
                "deferred",
                reason="resubmit cooldown",
            )
            continue

        capacity_token = _translation_capacity.acquire(media_id, lingarr_media_type)
        if capacity_token is None:
            with stats_lock:
                stats["api_errors"] = stats.get("api_errors", 0) + 1
                stats["deferred"] = stats.get("deferred", 0) + 1
            _status_transition(
                item_type,
                item_id,
                target_lang,
                "deferred",
                reason="Lingarr capacity unavailable",
            )
            continue

        appeared = _find_existing_target(video_path, target_lang) if video_path else (
            target_path if os.path.exists(target_path) else None
        )
        if appeared:
            _translation_capacity.release(capacity_token)
            capacity_token = None
            appeared_submission = _find_submission_for_target(appeared, target_lang)
            appeared_origin = (
                "lingarr"
                if _submission_matches_source(
                    appeared_submission,
                    source_path,
                    source_lang,
                    appeared,
                    target_lang,
                )
                else None
            )
            if appeared_origin and not _normalize_managed_output(appeared, title):
                with stats_lock:
                    stats["deferred"] = stats.get("deferred", 0) + 1
                _status_transition(
                    item_type,
                    item_id,
                    target_lang,
                    "deferred",
                    reason="managed file ownership failed",
                )
                continue
            print(f"[DISK] {title} '{target_lang}': appeared during queue wait")
            _status_transition(item_type, item_id, target_lang, "validating")
            validation_action, validation_report = _validate_translated_file(
                source_path, appeared, source_lang, target_lang, item_id, title=title,
                defer_repair=True, item_type=item_type, media_duration=media_duration,
                origin=appeared_origin,
                provenance_source_hash=(
                    appeared_submission.get("sourceHash")
                    if appeared_origin else None
                ),
            )
            if validation_action in ("valid", "valid-warning", "formatted", "repaired"):
                with stats_lock:
                    stats["completed"] += 1
                    stats["translations"].append(f"{title}: {source_lang} -> {target_lang} (on disk)")
                    _record_cleanup_stats(stats, validation_action, validation_report)
                    _mark_activity(stats, item_type)
            elif validation_action.startswith("repair-"):
                with stats_lock:
                    stats["cleanup_repair_queued"] = stats.get("cleanup_repair_queued", 0) + (
                        validation_action == "repair-queued"
                    )
                    stats["cleanup_repair_deferred"] = stats.get("cleanup_repair_deferred", 0) + (
                        validation_action == "repair-deferred"
                    )
            else:
                with stats_lock:
                    stats["failed"] += 1
                    _record_cleanup_stats(stats, validation_action, validation_report)
                    if validation_action in ("quarantined", "deleted"):
                        _mark_activity(stats, item_type)
            _status_finish_validation(item_type, item_id, target_lang, validation_action)
            continue

        src_lines = _count_dialogue_lines(source_path)
        if src_lines is None:
            _translation_capacity.release(capacity_token)
            print(f"{YELLOW}[SKIP] {title} '{target_lang}': source not readable — deferring{RESET}")
            with stats_lock:
                stats.setdefault("deferred", 0)
                stats["deferred"] += 1
            _status_transition(
                item_type, item_id, target_lang, "deferred", reason="source unreadable"
            )
            continue
        if src_lines == 0:
            _translation_capacity.release(capacity_token)
            print(f"{YELLOW}[SKIP] {title} '{target_lang}': source has no dialogue lines{RESET}")
            with stats_lock:
                stats.setdefault("deferred", 0)
                stats["deferred"] += 1
            _status_transition(
                item_type, item_id, target_lang, "deferred", reason="source has no dialogue"
            )
            continue

        target_snapshot = (
            _snapshot_target_sidecars(video_path, target_lang)
            if video_path else {}
        )
        source_hash = _file_hash_or_none(source_path)
        print(f"[TRANSLATE] {title}: {source_lang} -> {target_lang} ({src_lines} lines)")
        try:
            attempt_id = _record_submission(
                item_id,
                target_lang,
                target_path,
                expected_target_path=target_path,
                video_path=video_path or None,
                source_path=source_path,
                source_hash=source_hash,
                source_language=source_lang,
                item_type=item_type,
                target_variant=target_variant,
                status="reserved",
            )
        except (StateStoreError, OSError) as exc:
            _translation_capacity.release(capacity_token)
            print(
                f"{YELLOW}[DEFER] Could not reserve durable translation "
                f"state for {title} '{target_lang}': {exc}{RESET}"
            )
            with stats_lock:
                stats["deferred"] = stats.get("deferred", 0) + 1
            _status_transition(
                item_type, item_id, target_lang, "deferred",
                reason="could not persist translation reservation",
            )
            continue
        status: str | None = None
        try:
            job_id = lingarr_submit_file(
                media_id,
                source_path,
                source_lang,
                target_lang,
                lingarr_media_type,
            )
            if job_id is None:
                _mark_submission_failed(attempt_id)
                with stats_lock:
                    stats["failed"] += 1
                _status_transition(
                    item_type,
                    item_id,
                    target_lang,
                    "failed",
                    reason="Lingarr submission failed",
                )
                continue

            try:
                _mark_submission_submitted(attempt_id, job_id)
            except StateStoreError as exc:
                print(
                    f"{YELLOW}[DEFER] Lingarr accepted {title} '{target_lang}' "
                    f"but its job state could not be persisted: {exc}{RESET}"
                )
                with stats_lock:
                    stats["deferred"] = stats.get("deferred", 0) + 1
                _status_transition(
                    item_type, item_id, target_lang, "deferred",
                    reason="Lingarr job persistence failed",
                )
                continue
            _status_transition(item_type, item_id, target_lang, "translating")
            with stats_lock:
                stats["submitted"] += 1
                _mark_activity(stats, item_type)

            deadline = time.time() + item_timeout
            status = lingarr_poll_job(job_id, deadline, title)
        finally:
            _translation_capacity.release(capacity_token)

        if status != "Completed":
            with stats_lock:
                if status is None:
                    stats["timed_out"] += 1
                else:
                    stats["failed"] += 1
            if status is None and shutdown_requested:
                _status_transition(
                    item_type,
                    item_id,
                    target_lang,
                    "deferred",
                    reason="service shutdown",
                )
            elif status is None:
                _status_transition(
                    item_type, item_id, target_lang, "timed_out", reason="Lingarr timeout"
                )
            else:
                _status_transition(
                    item_type,
                    item_id,
                    target_lang,
                    "failed",
                    reason=f"Lingarr job {status.lower()}",
                )
            continue

        actual_target_path = (
            _discover_completed_target(
                video_path,
                target_lang,
                target_path,
                target_snapshot,
            )
            if video_path
            else (target_path if os.path.exists(target_path) else None)
        )
        if actual_target_path is None:
            print(
                f"{YELLOW}[WARNING] {title} '{target_lang}': Lingarr completed "
                f"but no new target-language sidecar was found "
                f"(expected {target_path}){RESET}"
            )
            with stats_lock:
                stats["timed_out"] += 1
            _status_transition(
                item_type,
                item_id,
                target_lang,
                "timed_out",
                reason="completed output missing",
            )
            continue
        if not _normalize_managed_output(actual_target_path, title):
            with stats_lock:
                stats["deferred"] = stats.get("deferred", 0) + 1
            _status_transition(
                item_type,
                item_id,
                target_lang,
                "deferred",
                reason="managed file ownership failed",
            )
            continue
        actual_suffix = _target_suffix(actual_target_path, target_lang)
        actual_variant = actual_suffix[1] if actual_suffix is not None else ""
        _update_submission_actual_path(
            item_id, target_lang, actual_target_path, actual_variant, item_type
        )
        if os.path.normcase(os.path.abspath(actual_target_path)) != os.path.normcase(
            os.path.abspath(target_path)
        ):
            with stats_lock:
                stats["variant_outputs_discovered"] = (
                    stats.get("variant_outputs_discovered", 0) + 1
                )
        if not _record_pending_lingarr_output(
            source_path,
            actual_target_path,
            source_lang,
            target_lang,
            item_type,
            item_id,
        ):
            with stats_lock:
                stats["deferred"] = stats.get("deferred", 0) + 1
            _status_transition(
                item_type, item_id, target_lang, "deferred",
                reason="completed output provenance persistence failed",
            )
            continue

        _status_transition(item_type, item_id, target_lang, "validating")
        validation_action, validation_report = _validate_translated_file(
            source_path, actual_target_path, source_lang, target_lang, item_id, title=title,
            defer_repair=True, item_type=item_type, media_duration=media_duration,
            origin="lingarr",
            provenance_source_hash=source_hash,
        )
        if validation_action in ("valid", "valid-warning", "formatted", "repaired"):
            print(
                f"{GREEN}[OK] {title} '{target_lang}' translated to "
                f"{os.path.basename(actual_target_path)}{RESET}"
            )
            with stats_lock:
                stats["completed"] += 1
                stats["translations"].append(f"{title}: {source_lang} -> {target_lang}")
                _record_cleanup_stats(stats, validation_action, validation_report)
                _mark_activity(stats, item_type)
        elif validation_action.startswith("repair-"):
            with stats_lock:
                stats["cleanup_repair_queued"] = stats.get("cleanup_repair_queued", 0) + (
                    validation_action == "repair-queued"
                )
                stats["cleanup_repair_deferred"] = stats.get("cleanup_repair_deferred", 0) + (
                    validation_action == "repair-deferred"
                )
        else:
            with stats_lock:
                stats["failed"] += 1
                stats.setdefault("cleaned", 0)
                stats["cleaned"] += 1
                _record_cleanup_stats(stats, validation_action, validation_report)
        _status_finish_validation(item_type, item_id, target_lang, validation_action)

# ---------------------------------------------------------------------------
# Existing-library cleanup
# ---------------------------------------------------------------------------

def _scan_undersized_sidecars(stats: dict) -> bool:
    """Validate regular subtitle density for every language using sibling media duration."""
    if not CLEANUP_UNDERSIZED_ENABLED:
        return False
    from clean_et_subs import file_sha256, validate_srt_structure

    changed = False
    seen: set[Path] = set()
    for root in CLEANUP_ROOTS:
        if not root.exists():
            continue
        for subtitle in root.rglob("*.srt"):
            if shutdown_requested:
                return changed
            if not subtitle.is_file() or subtitle in seen:
                continue
            seen.add(subtitle)
            video = _find_sidecar_video(subtitle)
            if video is None:
                continue
            exempt_token = _explicit_non_full_sidecar(video, subtitle)
            if exempt_token is not None:
                stats["undersized_forced_exempt"] += 1
                dbg(f"Completeness exempt {subtitle.name}: explicit {exempt_token} track")
                continue

            stats["undersized_checked"] += 1
            duration = _probe_media_duration(video)
            if duration is None:
                stats["undersized_duration_unavailable"] += 1
                continue
            report = validate_srt_structure(subtitle)
            if not report.valid:
                dbg(
                    f"Completeness deferred {subtitle.name}: structural validation must handle "
                    f"{report.summary()}"
                )
                continue
            completeness = _evaluate_completeness(subtitle, duration)
            _add_completeness_issue(report, completeness)
            if completeness is not None and completeness.undersized:
                stats["undersized_detected"] += 1
                print(
                    f"{YELLOW}[SIZE] Undersized {subtitle.name}: "
                    f"{completeness.cue_count} cues, {completeness.subtitle_bytes} bytes, "
                    f"{completeness.media_duration_seconds / 60:.1f} min, "
                    f"failed={','.join(completeness.failed_signals)}{RESET}"
                )
            if report.valid:
                continue

            try:
                subtitle_hash = file_sha256(subtitle)
            except OSError:
                subtitle_hash = None
            origin = (
                _get_validation_state().matching_origin(subtitle, subtitle_hash)
                if subtitle_hash is not None else None
            )
            tokens = _sidecar_tokens(video, subtitle)
            language = next((token for token in tokens if len(token) in (2, 3) and token.isalpha()), "unknown")
            action = _apply_cleanup_action(
                subtitle,
                None,
                language,
                report,
                completeness=completeness,
                origin=origin,
                lingarr_outcome="not attempted: whole-file completeness failure",
                dry_run=CLEANUP_SCAN_DRY_RUN,
            )
            if action == "quarantined":
                if completeness is not None and completeness.undersized:
                    stats["undersized_quarantined"] += 1
                else:
                    stats["quarantined_files"] += 1
                changed = True
            elif action == "deleted":
                stats["deleted_files"] += 1
                changed = True
            elif action == "reported":
                stats["reported_files"] += 1
            elif action == "dry-run":
                stats["dry_run_files"] += 1
            elif action == "action-failed":
                stats["action_failures"] += 1
    return changed


def _video_sidecars(video: Path) -> list[Path]:
    """Return SRTs belonging to exactly this video stem, excluding overlapping names."""
    try:
        return sorted(
            (
                path for path in video.parent.iterdir()
                if path.is_file() and path.suffix.casefold() == ".srt"
                and _find_sidecar_video(path) == video
            ),
            key=lambda path: path.name.casefold(),
        )
    except OSError:
        return []


def _queue_video_for_pruning(video_path: str | Path, item_type: str | None = None) -> None:
    key = os.path.normcase(os.path.abspath(str(video_path)))
    with _pending_prune_lock:
        _pending_prune_videos[key] = item_type


def _take_pending_prune_videos() -> list[tuple[Path, str | None]]:
    with _pending_prune_lock:
        pending = [(Path(path), item_type) for path, item_type in _pending_prune_videos.items()]
        _pending_prune_videos.clear()
    return pending


def _video_has_pending_repair(video: Path) -> bool:
    with _pending_repairs_lock:
        target_paths = [metadata.get("target_path") for metadata in _pending_repairs.values()]
    return any(
        target_path and _find_sidecar_video(target_path) == video
        for target_path in target_paths
    )


def _prune_stats() -> dict:
    return {
        "prune_videos_checked": 0,
        "prune_ready": 0,
        "prune_deferred": 0,
        "prune_missing_languages": 0,
        "prune_invalid_languages": 0,
        "prune_duration_unavailable": 0,
        "prune_retained_unknown": 0,
        "prune_candidates": 0,
        "prune_quarantined": 0,
        "prune_deleted": 0,
        "prune_reported": 0,
        "prune_failures": 0,
        "prune_bazarr_rescan_batches": 0,
    }


def _candidate_videos() -> list[Path]:
    videos: set[Path] = set()
    for root in CLEANUP_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.casefold() in _VIDEO_EXTENSIONS:
                videos.add(path)
    return sorted(videos, key=lambda path: str(path).casefold())


def _managed_sidecar_is_valid(
    classification: SidecarClassification,
    duration: float,
    detector,
) -> tuple[bool, dict]:
    from clean_et_subs import file_sha256, target_language_for_code, validate_subtitle_without_source

    language = classification.language
    evidence = {"path": str(classification.path), "language": language, "valid": False}
    if language is None or any(
        token in _NON_FULL_SUBTITLE_TOKENS for token in classification.tokens
    ):
        evidence["reason"] = "special-purpose track"
        return False, evidence
    target_language = target_language_for_code(language)
    if target_language is None or detector is None:
        evidence["reason"] = "unsupported validation language"
        return False, evidence
    try:
        target_hash = file_sha256(classification.path)
    except OSError as exc:
        evidence["reason"] = f"hash unavailable: {exc}"
        return False, evidence
    evidence["hash"] = target_hash
    cached = _get_validation_state().current_valid_details(classification.path, target_hash)
    cached_completeness = cached.get("completeness") if cached is not None else None
    cached_duration = (
        cached_completeness.get("mediaDurationSeconds")
        if isinstance(cached_completeness, dict) else None
    )
    if (
        isinstance(cached_duration, (int, float))
        and abs(float(cached_duration) - duration) <= 0.5
        and not cached_completeness.get("undersized", False)
    ):
        evidence.update({"valid": True, "cached": True})
        return True, evidence

    report = validate_subtitle_without_source(
        classification.path,
        detector,
        target_language,
        target_lang=language,
        **_validation_kwargs(),
    )
    completeness = _evaluate_completeness(classification.path, duration)
    _add_completeness_issue(report, completeness)
    evidence["validation"] = report.to_dict()
    evidence["completeness"] = completeness.to_dict() if completeness is not None else None
    if completeness is None:
        evidence["reason"] = "completeness validation unavailable"
        return False, evidence
    if report.valid:
        evidence["valid"] = True
        _record_validation_result(
            classification.path,
            None,
            target_hash,
            "valid",
            report,
            completeness=evidence["completeness"],
            validationScope="prune-target-only",
        )
    else:
        evidence["reason"] = report.summary()
    return report.valid, evidence


def _apply_prune_action(
    video: Path,
    classification: SidecarClassification,
    readiness: dict,
    *,
    dry_run: bool,
) -> str:
    from clean_et_subs import file_sha256, quarantine_subtitle, write_validation_report

    subtitle = classification.path
    try:
        video_stat = video.stat()
        video_path_hash = hashlib.sha256(
            os.path.normcase(os.path.abspath(str(video))).encode("utf-8")
        ).hexdigest()
        subtitle_hash = file_sha256(subtitle)
    except OSError as exc:
        print(f"{RED}[PRUNE] Could not hash {subtitle}: {exc}{RESET}")
        return "failed"
    audit = {
        "reason": "unmanaged subtitle sidecar",
        "videoPath": str(video),
        "videoPathHash": video_path_hash,
        "videoSize": video_stat.st_size,
        "videoModifiedNs": video_stat.st_mtime_ns,
        "targetPath": str(subtitle),
        "targetHash": subtitle_hash,
        "classification": {
            "kind": classification.kind,
            "language": classification.language,
            "tokens": list(classification.tokens),
        },
        "managedLanguages": LANGUAGES,
        "managedLanguageReadiness": readiness,
        "action": "dry-run" if dry_run else CLEANUP_PRUNE_ACTION,
        "recordedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if dry_run or CLEANUP_PRUNE_ACTION == "report":
        print(f"[PRUNE] {'DRYRUN' if dry_run else 'REPORT'}: would remove {subtitle}")
        return "dry-run" if dry_run else "reported"
    if CLEANUP_PRUNE_ACTION == "quarantine":
        try:
            destination = quarantine_subtitle(subtitle, CLEANUP_ROOTS, CLEANUP_QUARANTINE_DIR)
            audit["quarantinePath"] = str(destination)
            try:
                write_validation_report(destination, audit)
            except OSError as exc:
                print(f"{YELLOW}[PRUNE] Quarantined file but could not write report: {exc}{RESET}")
            print(f"[PRUNE] Quarantined {subtitle} -> {destination}")
            return "quarantined"
        except OSError as exc:
            print(f"{RED}[PRUNE] Could not quarantine {subtitle}: {exc}{RESET}")
            return "failed"
    try:
        subtitle.unlink()
        print(f"[PRUNE] Deleted {subtitle}")
        return "deleted"
    except OSError as exc:
        print(f"{RED}[PRUNE] Could not delete {subtitle}: {exc}{RESET}")
        return "failed"


def run_extra_sidecar_prune(
    videos: list[tuple[Path, str | None]] | None = None,
    *,
    already_locked: bool = False,
) -> tuple[dict, bool, bool]:
    """Prune recognized unmanaged sidecars after all managed languages are ready."""
    stats = _prune_stats()
    if not CLEANUP_PRUNE_EXTRA_LANGUAGES:
        return stats, False, False

    def run() -> tuple[dict, bool, bool]:
        detector = _get_cleanup_detector()
        requested = videos if videos is not None else [(video, None) for video in _candidate_videos()]
        changed_episodes = False
        changed_movies = False
        for video, item_type in requested:
            if shutdown_requested or not video.exists():
                continue
            sidecars = _video_sidecars(video)
            if not sidecars:
                continue
            stats["prune_videos_checked"] += 1
            duration = _probe_media_duration(video)
            if duration is None:
                stats["prune_duration_unavailable"] += 1
                stats["prune_deferred"] += 1
                print(f"{YELLOW}[PRUNE] Deferred {video.name}: media duration unavailable{RESET}")
                continue
            classified = [_classify_sidecar(video, path) for path in sidecars]
            readiness: dict[str, dict] = {}
            ready = True
            for language in [code.casefold() for code in LANGUAGES]:
                candidates = [entry for entry in classified if entry.kind == "managed" and entry.language == language]
                full_candidates = [
                    entry for entry in candidates
                    if _explicit_non_full_sidecar(video, entry.path) is None
                ]
                if not full_candidates:
                    readiness[language] = {"ready": False, "reason": "missing full subtitle"}
                    stats["prune_missing_languages"] += 1
                    ready = False
                    continue
                evidence = []
                language_ready = False
                for entry in full_candidates:
                    valid, candidate_evidence = _managed_sidecar_is_valid(entry, duration, detector)
                    evidence.append(candidate_evidence)
                    language_ready = language_ready or valid
                readiness[language] = {"ready": language_ready, "candidates": evidence}
                if not language_ready:
                    stats["prune_invalid_languages"] += 1
                    ready = False
            if not ready:
                stats["prune_deferred"] += 1
                if videos is None and _video_has_pending_repair(video):
                    _queue_video_for_pruning(video, item_type)
                missing = ",".join(code for code, value in readiness.items() if not value["ready"])
                print(f"{YELLOW}[PRUNE] Deferred {video.name}: managed language(s) not ready: {missing}{RESET}")
                continue
            stats["prune_ready"] += 1
            for entry in classified:
                candidate = (
                    entry.kind == "nonmanaged"
                    or (entry.kind == "special" and CLEANUP_PRUNE_SPECIAL_SIDECARS)
                    or (entry.kind == "unknown" and CLEANUP_PRUNE_UNKNOWN_SIDECARS)
                )
                if entry.kind == "unknown" and not CLEANUP_PRUNE_UNKNOWN_SIDECARS:
                    stats["prune_retained_unknown"] += 1
                if not candidate:
                    continue
                stats["prune_candidates"] += 1
                action = _apply_prune_action(
                    video, entry, readiness, dry_run=CLEANUP_SCAN_DRY_RUN
                )
                if action == "quarantined":
                    stats["prune_quarantined"] += 1
                elif action == "deleted":
                    stats["prune_deleted"] += 1
                elif action == "reported":
                    stats["prune_reported"] += 1
                elif action == "failed":
                    stats["prune_failures"] += 1
                if action in ("quarantined", "deleted"):
                    _clear_submission_for_path(entry.path, entry.language or "unknown")
                    if item_type == "episodes":
                        changed_episodes = True
                    elif item_type == "movies":
                        changed_movies = True
                    else:
                        changed_episodes = changed_movies = True
        print(
            "[PRUNE] Summary: "
            f"videos={stats['prune_videos_checked']} ready={stats['prune_ready']} "
            f"deferred={stats['prune_deferred']} candidates={stats['prune_candidates']} "
            f"quarantined={stats['prune_quarantined']} deleted={stats['prune_deleted']} "
            f"missing={stats['prune_missing_languages']} invalid={stats['prune_invalid_languages']} "
            f"no-duration={stats['prune_duration_unavailable']} "
            f"retained-unknown={stats['prune_retained_unknown']} failures={stats['prune_failures']}"
        )
        return stats, changed_episodes, changed_movies

    if already_locked:
        return run()
    with _cleanup_scan_lock:
        return run()

def run_existing_cleanup_scan() -> dict:
    stats = {
        "files_checked": 0,
        "skipped_unchanged": 0,
        "excessive_line_cues": 0,
        "other_invalid_cues": 0,
        "formatted_files": 0,
        "repaired_files": 0,
        "repair_failures": 0,
        "repair_queued": 0,
        "repair_deferred": 0,
        "quarantined_files": 0,
        "deleted_files": 0,
        "reported_files": 0,
        "dry_run_files": 0,
        "without_source": 0,
        "source_less_warnings": 0,
        "recovered_pending_outputs": 0,
        "repeat_quarantines": 0,
        "ai_repairs_suppressed": 0,
        "action_failures": 0,
        "undersized_checked": 0,
        "undersized_forced_exempt": 0,
        "undersized_duration_unavailable": 0,
        "undersized_detected": 0,
        "undersized_quarantined": 0,
        **_prune_stats(),
    }
    if not CLEANUP_SCAN_EXISTING:
        return stats

    from clean_et_subs import (
        discover_target_subtitles,
        file_sha256,
        find_preferred_source,
        target_language_for_code,
        validate_subtitle_without_source,
    )

    with _cleanup_scan_lock:
        detector = _get_cleanup_detector()
        state = _get_validation_state()
        changed = _scan_undersized_sidecars(stats)
        if detector is None or not CLEANUP_LANGUAGES:
            prune_stats, prune_episodes, prune_movies = run_extra_sidecar_prune(already_locked=True)
            prune_stats["prune_bazarr_rescan_batches"] = int(prune_episodes or prune_movies)
            stats.update(prune_stats)
            changed = changed or prune_episodes or prune_movies
            if changed and not shutdown_requested:
                trigger_bazarr_sync(True, True)
                wait_for_bazarr_sync(True, True, SYNC_TIMEOUT)
            return stats
        candidates = discover_target_subtitles(CLEANUP_ROOTS, CLEANUP_LANGUAGES)
        print(
            f"[SCAN] Existing subtitle cleanup found {len(candidates)} target file(s) "
            f"under {', '.join(str(root) for root in CLEANUP_ROOTS)}"
        )

        for candidate in candidates:
            if shutdown_requested:
                break
            target_language = target_language_for_code(candidate.target_lang)
            if target_language is None:
                print(f"{YELLOW}[SCAN] Unsupported target language for {candidate.path}{RESET}")
                continue

            source_path, source_lang = find_preferred_source(candidate)
            if source_path is not None and candidate.variant:
                print(
                    f"[SCAN] Paired {candidate.path.name} with variant-aware source "
                    f"{source_path.name}"
                )
            try:
                target_hash = file_sha256(candidate.path)
            except OSError as e:
                print(f"{YELLOW}[SCAN] Could not hash {candidate.path}: {e}{RESET}")
                continue
            validation_origin = None
            validation_source_hash = None
            submission = _find_submission_for_target(
                candidate.path, candidate.target_lang
            )
            if source_path is None and submission is not None:
                pending_source = submission.get("sourcePath")
                pending_language = submission.get("sourceLanguage")
                if (
                    isinstance(pending_source, str)
                    and pending_source
                    and os.path.exists(pending_source)
                    and isinstance(pending_language, str)
                    and pending_language
                ):
                    try:
                        pending_hash = file_sha256(pending_source)
                    except OSError as e:
                        print(
                            f"{YELLOW}[SCAN] Could not hash pending source "
                            f"{pending_source}: {e}{RESET}"
                        )
                        pending_hash = None
                    if (
                        pending_hash is not None
                        and submission.get("sourceHash") == pending_hash
                        and _submission_matches_source(
                            submission,
                            pending_source,
                            pending_language,
                            candidate.path,
                            candidate.target_lang,
                        )
                    ):
                        source_path = Path(pending_source)
                        source_lang = pending_language
                        validation_origin = "lingarr"
                        validation_source_hash = pending_hash
                        stats["recovered_pending_outputs"] += 1
                        print(
                            f"[SCAN] Recovered pending Lingarr output "
                            f"{candidate.path.name} with source {source_path.name}"
                        )
            try:
                source_hash = (
                    file_sha256(source_path) if source_path is not None else None
                )
            except OSError as e:
                print(f"{YELLOW}[SCAN] Could not hash {source_path}: {e}{RESET}")
                source_path = None
                source_lang = None
                source_hash = None

            if state.is_unchanged_valid(candidate.path, source_hash, target_hash):
                stats["skipped_unchanged"] += 1
                continue

            stats["files_checked"] += 1
            if source_path is not None and source_lang is not None:
                candidate_video = _find_sidecar_video(candidate.path)
                action, report = _validate_translated_file(
                    str(source_path),
                    str(candidate.path),
                    source_lang,
                    candidate.target_lang,
                    None,
                    title=candidate.path.name,
                    dry_run=CLEANUP_SCAN_DRY_RUN,
                    defer_repair=not CLEANUP_SCAN_DRY_RUN,
                    media_duration=_probe_media_duration(candidate_video)
                    if candidate_video is not None else None,
                    origin=validation_origin,
                    provenance_source_hash=validation_source_hash,
                )
            else:
                stats["without_source"] += 1
                report = validate_subtitle_without_source(
                    candidate.path,
                    detector,
                    target_language,
                    target_lang=candidate.target_lang,
                    **_validation_kwargs(),
                )
                if report.valid:
                    print(f"[SCAN] OK {candidate.path.name} (target-only validation passed)")
                    _record_validation_result(
                        candidate.path, None, target_hash, "valid", report, sourceAvailable=False
                    )
                    action = "valid"
                elif _source_less_line_only_warning(report):
                    print(
                        f"{YELLOW}[SCAN] Retained {candidate.path.name} with "
                        f"source-less line-count warning: {report.summary()}{RESET}"
                    )
                    _record_validation_result(
                        candidate.path,
                        None,
                        target_hash,
                        "valid_with_warnings",
                        report,
                        sourceAvailable=False,
                        warningRules=["excessive_lines"],
                    )
                    stats["source_less_warnings"] += 1
                    action = "valid-warning"
                else:
                    print(
                        f"{YELLOW}[SCAN] Invalid target without source {candidate.path.name}: "
                        f"{report.summary()}{RESET}"
                    )
                    action = _apply_cleanup_action(
                        candidate.path,
                        None,
                        candidate.target_lang,
                        report,
                        lingarr_outcome="not attempted: no source subtitle",
                        dry_run=CLEANUP_SCAN_DRY_RUN,
                    )

            if report is not None:
                excessive = sum(issue.rule == "excessive_lines" for issue in report.issues)
                stats["excessive_line_cues"] += excessive
                stats["other_invalid_cues"] += len(report.issues) - excessive
                stats["repeat_quarantines"] += int(
                    bool(getattr(report, "repeat_offender", False))
                )
                stats["ai_repairs_suppressed"] += int(
                    bool(getattr(report, "ai_repair_suppressed", False))
                )
                if (
                    action not in ("valid", "valid-warning", "formatted", "repaired", "repair-queued", "repair-duplicate", "repair-deferred")
                    and source_path is not None
                    and CLEANUP_REPAIR_ENABLED
                    and report.repairable_cue_indexes
                    and not CLEANUP_SCAN_DRY_RUN
                ):
                    stats["repair_failures"] += 1

            if action == "formatted":
                stats["formatted_files"] += 1
                changed = True
            elif action == "repaired":
                stats["repaired_files"] += 1
                changed = True
            elif action == "repair-queued":
                stats["repair_queued"] += 1
            elif action == "repair-deferred":
                stats["repair_deferred"] += 1
            elif action == "quarantined":
                stats["quarantined_files"] += 1
                _clear_submission_for_path(candidate.path, candidate.target_lang)
                changed = True
            elif action == "deleted":
                stats["deleted_files"] += 1
                _clear_submission_for_path(candidate.path, candidate.target_lang)
                changed = True
            elif action == "reported":
                stats["reported_files"] += 1
            elif action == "dry-run":
                stats["dry_run_files"] += 1
            elif action == "action-failed":
                stats["action_failures"] += 1

        prune_stats, prune_episodes, prune_movies = run_extra_sidecar_prune(already_locked=True)
        prune_stats["prune_bazarr_rescan_batches"] = int(prune_episodes or prune_movies)
        stats.update(prune_stats)
        changed = changed or prune_episodes or prune_movies

        print("[SCAN] Existing subtitle cleanup summary:")
        print(f"  Checked             : {stats['files_checked']}")
        print(f"  Skipped unchanged   : {stats['skipped_unchanged']}")
        print(f"  Excessive-line cues : {stats['excessive_line_cues']}")
        print(f"  Other invalid cues  : {stats['other_invalid_cues']}")
        print(f"  Source-less warnings: {stats['source_less_warnings']}")
        print(f"  Pending recovered   : {stats['recovered_pending_outputs']}")
        print(f"  Repeat quarantines  : {stats['repeat_quarantines']}")
        print(f"  AI repairs skipped  : {stats['ai_repairs_suppressed']}")
        print(f"  Format-only repairs : {stats['formatted_files']}")
        print(f"  Repaired files      : {stats['repaired_files']}")
        print(f"  AI repairs queued   : {stats['repair_queued']}")
        print(f"  AI repairs deferred : {stats['repair_deferred']}")
        print(f"  Repair failures     : {stats['repair_failures']}")
        print(f"  Quarantined files   : {stats['quarantined_files']}")
        print(f"  Regular size checks : {stats['undersized_checked']}")
        print(f"  Forced-track skips  : {stats['undersized_forced_exempt']}")
        print(f"  Undersized detected : {stats['undersized_detected']}")
        print(f"  Undersized quarant. : {stats['undersized_quarantined']}")
        print(f"  Duration unavailable: {stats['undersized_duration_unavailable']}")
        print(f"  Prune videos checked : {stats['prune_videos_checked']}")
        print(f"  Prune ready/deferred : {stats['prune_ready']}/{stats['prune_deferred']}")
        print(f"  Prune candidates     : {stats['prune_candidates']}")
        print(f"  Prune quarantined    : {stats['prune_quarantined']}")
        print(f"  Prune rescan batches : {stats['prune_bazarr_rescan_batches']}")
        if CLEANUP_SCAN_DRY_RUN:
            print(f"  Dry-run files       : {stats['dry_run_files']}")

        if changed and not shutdown_requested:
            trigger_bazarr_sync(True, True)
            wait_for_bazarr_sync(True, True, SYNC_TIMEOUT)
        return stats


def _run_existing_cleanup_scan_safely() -> dict | None:
    try:
        stats = run_existing_cleanup_scan()
        _status_record_maintenance({
            "formatted": stats.get("formatted_files", 0),
            "repaired": stats.get("repaired_files", 0),
            "quarantined": (
                stats.get("quarantined_files", 0)
                + stats.get("undersized_quarantined", 0)
                + stats.get("prune_quarantined", 0)
            ),
            "deleted": stats.get("deleted_files", 0) + stats.get("prune_deleted", 0),
            "undersized": stats.get("undersized_detected", 0),
            "pruned": stats.get("prune_quarantined", 0) + stats.get("prune_deleted", 0),
            "source_less_warnings": stats.get("source_less_warnings", 0),
            "repeat_quarantines": stats.get("repeat_quarantines", 0),
            "quarantine_holds": stats.get("quarantine_holds", 0),
            "variant_outputs": (
                stats.get("variant_outputs_discovered", 0)
                + stats.get("recovered_pending_outputs", 0)
            ),
            "failures": (
                stats.get("repair_failures", 0)
                + stats.get("action_failures", 0)
                + stats.get("prune_failures", 0)
            ),
        })
        return stats
    except Exception as e:
        print(f"{RED}[ERROR] Existing subtitle cleanup scan failed: {e}{RESET}")
        if DEBUG:
            import traceback
            traceback.print_exc()
        return None


def run_retention_housekeeping() -> dict:
    from clean_et_subs import purge_old_files

    current_log = [_app_log_sink.current_path] if _app_log_sink.current_path is not None else []
    quarantine_removed = purge_old_files(CLEANUP_QUARANTINE_DIR, RETENTION_DAYS)
    logs_removed = purge_old_files(LOG_DIR, RETENTION_DAYS, exclude=current_log)
    try:
        state_removed = _get_validation_state().prune_older_than(RETENTION_DAYS)
    except (OSError, StateStoreError) as e:
        print(f"{YELLOW}[WARNING] Could not prune validation state: {e}{RESET}")
        state_removed = 0
    result = {
        "quarantine_files": len(quarantine_removed),
        "log_files": len(logs_removed),
        "state_entries": state_removed,
        "status_events": _status_compact_history(),
    }
    print(
        f"[RETENTION] Removed {result['quarantine_files']} quarantine file(s), "
        f"{result['log_files']} log file(s), and {result['state_entries']} validation state record(s) "
        f"plus {result['status_events']} status event(s) beyond their retention window"
    )
    return result


# ---------------------------------------------------------------------------
# Cycle orchestrator
# ---------------------------------------------------------------------------

def _drain_lingarr_queue() -> bool:
    drain_deadline = time.time() + 2 * CHECK_INTERVAL
    while not shutdown_requested:
        try:
            active = len(lingarr_get_active_translations())
        except ServiceRequestError as exc:
            print(
                f"{YELLOW}[WARNING] Lingarr queue state is unverifiable; "
                f"cycle remains degraded: {exc}{RESET}"
            )
            return False
        if active == 0:
            return True
        if time.time() >= drain_deadline:
            print(f"{YELLOW}[WARNING] Lingarr still has {active} active job(s) after "
                  f"{2 * CHECK_INTERVAL}s — continuing anyway{RESET}")
            return False
        print(f"[INFO] Lingarr has {active} active job(s) — waiting before next cycle...")
        for _ in range(POLL_INTERVAL):
            if shutdown_requested:
                return False
            time.sleep(1)
    return False


def run_cycle(cycle_num: int) -> None:
    print(f"\n{BOLD}{CYAN}===== Cycle #{cycle_num} ====={RESET}")
    _status_set_phase("translating")

    stats: dict = {
        "submitted": 0,
        "completed": 0,
        "timed_out": 0,
        "failed": 0,
        "deferred": 0,
        "api_errors": 0,
        "degraded": False,
        "quarantine_holds": 0,
        "variant_outputs_discovered": 0,
        "recovered_pending_outputs": 0,
        "translations": [],
        "episode_activity": False,
        "movie_activity": False,
    }
    stats_lock = threading.Lock()

    lingarr_build_media_cache()

    try:
        active_before = len(lingarr_get_active_translations())
        print(f"[INFO] Lingarr active queue at cycle start: {active_before}")
    except ServiceRequestError as exc:
        stats["api_errors"] += 1
        stats["degraded"] = True
        print(f"{YELLOW}[WARNING] Cycle starts degraded: {exc}{RESET}")

    work: list[tuple] = []
    queue_errors: list[str] = []
    for item_type, id_field in (
        ("episodes", "sonarrEpisodeId"),
        ("movies", "radarrId"),
    ):
        try:
            wanted = fetch_wanted(item_type)
        except ServiceRequestError as exc:
            stats["api_errors"] += 1
            stats["degraded"] = True
            queue_errors.append(item_type)
            print(f"{YELLOW}[WARNING] Deferring {item_type} queue: {exc}{RESET}")
            continue
        work.extend((item, item_type, id_field) for item in wanted)
    if _status_tracker is not None:
        cycle_id = f"{int(time.time())}-{cycle_num}"
        jobs = build_cycle_jobs(work, LANGUAGES, cycle_id, _item_title)
        _status_start_cycle(cycle_id, cycle_num, jobs)

    if not work and queue_errors:
        print(
            f"{YELLOW}[WARNING] No processable work; unavailable queue(s): "
            f"{', '.join(queue_errors)}{RESET}"
        )
    elif not work:
        print("[INFO] No wanted items found.")
    else:
        print(f"[INFO] Processing {len(work)} item(s) with {PARALLEL_TRANSLATES} worker(s)...")
        with ThreadPoolExecutor(max_workers=PARALLEL_TRANSLATES) as executor:
            futures = {
                executor.submit(process_item, item, itype, ifield, stats, stats_lock):
                (item, itype, ifield)
                for item, itype, ifield in work
            }
            for future in as_completed(futures):
                if shutdown_requested:
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                try:
                    future.result()
                except Exception as e:
                    print(f"{RED}[ERROR] Worker exception: {e}{RESET}")
                    item, item_type, id_field = futures[future]
                    item_id = item.get(id_field)
                    missing = {
                        str(entry.get("code2", "")).strip().lower()
                        for entry in item.get("missing_subtitles", [])
                        if isinstance(entry, dict)
                    }
                    for language in LANGUAGES:
                        if language in missing:
                            _status_transition(
                                item_type,
                                item_id,
                                language,
                                "failed",
                                reason="translation worker exception",
                            )

    repair_results: list[RepairJobResult] = []
    pending_count = len(_pending_repairs)
    if pending_count:
        print(f"[REPAIR] Waiting for {pending_count} queued repair job(s) before Bazarr sync")
        repair_results = _drain_pending_repairs(stats)

    pending_prune = _take_pending_prune_videos()
    if pending_prune:
        print(f"[PRUNE] Checking {len(pending_prune)} translated/repaired video(s) before Bazarr sync")
        prune_stats, prune_episodes, prune_movies = run_extra_sidecar_prune(pending_prune)
        prune_stats["prune_bazarr_rescan_batches"] = int(prune_episodes or prune_movies)
        stats.update(prune_stats)
        stats["episode_activity"] = stats["episode_activity"] or prune_episodes
        stats["movie_activity"] = stats["movie_activity"] or prune_movies

    active_after: int | None = None
    active_after_error: ServiceRequestError | None = None
    try:
        active_after = len(lingarr_get_active_translations())
    except ServiceRequestError as exc:
        stats["degraded"] = True
        stats["api_errors"] += 1
        active_after_error = exc

    print(f"\n{BOLD}===== Cycle #{cycle_num} Summary ====={RESET}")
    print(f"  Submitted  : {stats['submitted']}")
    print(f"  Completed  : {stats['completed']}")
    print(f"  Timed out  : {stats['timed_out']}")
    print(f"  Failed     : {stats['failed']}")
    print(f"  Deferred   : {stats.get('deferred', 0)}")
    print(
        f"  Cycle state: {'degraded' if stats.get('degraded') or stats.get('api_errors') else 'healthy'}"
    )
    if stats.get("api_errors"):
        print(f"  API errors : {stats['api_errors']}")
    print(f"  Variant outputs found : {stats.get('variant_outputs_discovered', 0)}")
    print(f"  Pending outputs found : {stats.get('recovered_pending_outputs', 0)}")
    print(f"  Quarantine holds      : {stats.get('quarantine_holds', 0)}")
    if stats.get("cleaned"):
        print(f"  Cleaned    : {stats['cleaned']}")
    if stats.get("cleanup_checked"):
        print(f"  Cleanup checked       : {stats['cleanup_checked']}")
        print(f"  Excessive-line cues   : {stats.get('cleanup_excessive_lines', 0)}")
        print(f"  Other cleanup issues  : {stats.get('cleanup_other_issues', 0)}")
        print(f"  Format-only repairs   : {stats.get('cleanup_formatted', 0)}")
        print(f"  AI repairs queued     : {stats.get('cleanup_repair_queued', 0)}")
        print(f"  AI repair attempts    : {stats.get('cleanup_repair_attempts', 0)}")
        print(f"  No-context attempts   : {stats.get('cleanup_second_attempts', 0)}")
        print(f"  AI repairs deferred   : {stats.get('cleanup_repair_deferred', 0)}")
        print(f"  Repaired translations : {stats.get('cleanup_repaired', 0)}")
        print(f"  Quarantined files     : {stats.get('cleanup_quarantined', 0)}")
        print(f"  Undersized sources    : {stats.get('cleanup_undersized_sources', 0)}")
        print(f"  Undersized targets    : {stats.get('cleanup_undersized_targets', 0)}")
        print(f"  Forced sources skipped: {stats.get('cleanup_forced_sources_skipped', 0)}")
        print(f"  Alternative sources  : {stats.get('cleanup_alternative_sources', 0)}")
        print(f"  Source-less warnings : {stats.get('cleanup_source_less_warnings', 0)}")
        print(f"  Repeat quarantines   : {stats.get('cleanup_repeat_quarantines', 0)}")
        print(f"  AI repairs suppressed: {stats.get('cleanup_ai_repairs_suppressed', 0)}")
    if stats.get("prune_videos_checked"):
        print(f"  Prune videos checked : {stats['prune_videos_checked']}")
        print(f"  Prune ready/deferred : {stats.get('prune_ready', 0)}/{stats.get('prune_deferred', 0)}")
        print(f"  Prune candidates     : {stats.get('prune_candidates', 0)}")
        print(f"  Prune quarantined    : {stats.get('prune_quarantined', 0)}")
        print(f"  Prune rescan batches : {stats.get('prune_bazarr_rescan_batches', 0)}")
    if stats["translations"]:
        print("  Completed translations:")
        for t in stats["translations"]:
            print(f"    {GREEN}- {t}{RESET}")
    if active_after is not None:
        print(f"  Lingarr active queue now: {active_after}")
    elif active_after_error is not None:
        print(
            f"{YELLOW}  Lingarr active queue unavailable: "
            f"{active_after_error}{RESET}"
        )
    sys.stdout.flush()

    had_activity = (
        stats["submitted"] > 0
        or stats["completed"] > 0
        or stats["episode_activity"]
        or stats["movie_activity"]
    )
    if had_activity and not shutdown_requested:
        _status_set_phase("synchronization")
        had_episodes = stats["episode_activity"]
        had_movies = stats["movie_activity"]
        trigger_bazarr_sync(had_episodes, had_movies)
        wait_for_bazarr_sync(had_episodes, had_movies, SYNC_TIMEOUT)
        repaired_with_ids = [
            result for result in repair_results
            if result.action == "repaired" and result.item_id is not None
        ]
        missing = [result for result in repaired_with_ids if not _bazarr_has_repaired_path(result)]
        if missing and not shutdown_requested:
            retry_episodes = any(result.item_type == "episodes" for result in missing)
            retry_movies = any(result.item_type == "movies" for result in missing)
            print(f"{YELLOW}[WARNING] Bazarr did not register {len(missing)} repaired path(s); retrying scan once{RESET}")
            trigger_bazarr_sync(retry_episodes, retry_movies)
            wait_for_bazarr_sync(retry_episodes, retry_movies, SYNC_TIMEOUT)
            still_missing = [result for result in missing if not _bazarr_has_repaired_path(result)]
            stats["cleanup_bazarr_registration_failures"] = len(still_missing)
            for result in still_missing:
                print(f"{YELLOW}[WARNING] Bazarr still does not list repaired subtitle for {result.title} '{result.target_lang}'{RESET}")

    _drain_lingarr_queue()
    _status_finish_cycle()

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> int:
    global _status_tracker
    state_store = _initialize_state_store()
    status_server = None
    if STATUS_ENABLED:
        try:
            _status_tracker = StatusTracker(
                STATUS_SNAPSHOT_FILE,
                STATUS_HISTORY_FILE,
                retention_days=STATUS_HISTORY_RETENTION_DAYS,
                recent_limit=STATUS_RECENT_LIMIT,
            )
            try:
                status_server, _ = start_status_server(
                    _status_tracker, STATUS_BIND, STATUS_PORT
                )
                print(f"[STATUS] Dashboard listening on http://{STATUS_BIND}:{STATUS_PORT}")
            except OSError as exc:
                print(
                    f"{YELLOW}[STATUS] Dashboard port unavailable "
                    f"({STATUS_BIND}:{STATUS_PORT}): {exc}; translations will continue{RESET}"
                )
        except OSError as exc:
            _status_tracker = None
            print(
                f"{YELLOW}[STATUS] Could not initialize persistent status state: "
                f"{exc}; translations will continue{RESET}"
            )

    print(f"\n{BOLD}Bazarr AutoTranslate starting{RESET}")
    print(f"  Bazarr URL        : {BAZARR_URL}")
    print(f"  Lingarr URL       : {LINGARR_URL}")
    print(f"  Languages         : {', '.join(LANGUAGES)}")
    print(f"  Cleanup languages : {', '.join(sorted(CLEANUP_LANGUAGES)) or '(none)'}")
    print(f"  Existing scan     : {'ON' if CLEANUP_SCAN_EXISTING else 'off'} every {CLEANUP_SCAN_INTERVAL}s")
    print(f"  Cleanup roots     : {', '.join(str(root) for root in CLEANUP_ROOTS)}")
    print(f"  Cleanup action    : {CLEANUP_ACTION}{' (scan dry-run)' if CLEANUP_SCAN_DRY_RUN else ''}")
    print(f"  Source-less lines : {CLEANUP_SOURCELESS_LINE_ONLY_ACTION}")
    print(f"  Quarantine hold   : {CLEANUP_QUARANTINE_HOLD_DAYS} days")
    print(f"  Sidecar pruning   : {'ON' if CLEANUP_PRUNE_EXTRA_LANGUAGES else 'off'} "
          f"({CLEANUP_PRUNE_ACTION}, unknown={'remove' if CLEANUP_PRUNE_UNKNOWN_SIDECARS else 'retain'})")
    print(f"  Max cue lines     : {CLEANUP_MAX_CUE_LINES}")
    print(f"  Format recovery   : {'ON' if CLEANUP_FORMAT_REPAIR_ENABLED else 'off'}")
    print(f"  Repair workers    : {CLEANUP_REPAIR_WORKERS} (+{CLEANUP_REPAIR_WORKERS} beyond file workers)")
    print(f"  Repair queue max  : {CLEANUP_REPAIR_QUEUE_MAX}")
    print(f"  Size validation   : {'ON' if CLEANUP_UNDERSIZED_ENABLED else 'off'} "
          f"({CLEANUP_UNDERSIZED_REQUIRED_SIGNALS}/4 signals, media >= {CLEANUP_MIN_MEDIA_DURATION:.0f}s)")
    print(f"  Size thresholds   : {CLEANUP_MIN_CUES_PER_MINUTE:g} cues/min, "
          f"{CLEANUP_MIN_TEXT_CHARS_PER_MINUTE:g} chars/min, "
          f"{CLEANUP_MIN_BYTES_PER_MINUTE:g} bytes/min, "
          f"{CLEANUP_MIN_TIMELINE_COVERAGE:.0%} timeline")
    print(f"  Retention         : {RETENTION_DAYS} days (checked every {RETENTION_CHECK_INTERVAL}s)")
    print(f"  Status dashboard  : {'ON' if STATUS_ENABLED else 'off'}"
          + (f" on {STATUS_BIND}:{STATUS_PORT}" if STATUS_ENABLED else ""))
    print(f"  Status retention  : {STATUS_HISTORY_RETENTION_DAYS} days")
    print(f"  Parallel workers  : {PARALLEL_TRANSLATES}")
    print(f"  Check interval    : {CHECK_INTERVAL}s (after Bazarr sync)")
    print(f"  Poll interval     : {POLL_INTERVAL}s  (floor {POLL_TIMEOUT}s per translation)")
    print(f"  Sync timeout      : {SYNC_TIMEOUT}s")
    print(f"  Sync start timeout: {SYNC_START_TIMEOUT}s")
    print(f"  Resubmit cooldown : {RESUBMIT_COOLDOWN}s")
    print(f"  Debug mode        : {'ON' if DEBUG else 'off'}")
    sys.stdout.flush()

    langs = lingarr_get_languages()
    if langs:
        mappings = []
        for language in langs:
            targets = ", ".join(language.targets) if language.targets else "none"
            mappings.append(f"{language.name} ({language.code} -> {targets})")
        print(f"[INFO] Lingarr supports languages: {'; '.join(mappings)}")

    run_retention_housekeeping()
    last_retention_check = time.monotonic()

    print("[INFO] Waiting 30s for services to start...")
    sys.stdout.flush()
    for _ in range(30):
        if shutdown_requested:
            break
        time.sleep(1)

    if not shutdown_requested:
        print("[INFO] Running initial Bazarr subtitle synchronization...")
        _status_set_phase("synchronization")
        trigger_bazarr_sync(True, True)
        wait_for_bazarr_sync(True, True, SYNC_TIMEOUT)

    last_cleanup_scan = 0.0
    if not shutdown_requested and CLEANUP_SCAN_EXISTING:
        _status_set_phase("cleanup")
        _run_existing_cleanup_scan_safely()
        last_cleanup_scan = time.monotonic()

    cycle = 1
    while not shutdown_requested:
        if time.monotonic() - last_retention_check >= RETENTION_CHECK_INTERVAL:
            run_retention_housekeeping()
            last_retention_check = time.monotonic()
        if (
            CLEANUP_SCAN_EXISTING
            and last_cleanup_scan > 0
            and time.monotonic() - last_cleanup_scan >= CLEANUP_SCAN_INTERVAL
        ):
            _status_set_phase("cleanup")
            _run_existing_cleanup_scan_safely()
            last_cleanup_scan = time.monotonic()
        run_cycle(cycle)
        cycle += 1
        if shutdown_requested:
            break
        print(f"[INFO] Next cycle in {CHECK_INTERVAL}s...")
        _status_set_phase("sleeping", next_cycle_at=time.time() + CHECK_INTERVAL)
        for _ in range(CHECK_INTERVAL):
            if shutdown_requested:
                break
            time.sleep(1)

    _shutdown_repair_executor()
    _status_set_phase("shutdown")
    if status_server is not None:
        status_server.shutdown()
        status_server.server_close()
    state_store.close()
    print("[INFO] Bazarr AutoTranslate stopped cleanly.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"{RED}[FATAL] {e}{RESET}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
