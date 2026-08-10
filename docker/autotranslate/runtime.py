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
import logging
import logging.handlers
import queue
import atexit
from concurrent.futures import (
    CancelledError,
    Future,
    ThreadPoolExecutor,
    as_completed,
    wait,
)
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import requests
from .status.dashboard import (
    StatusTracker,
    build_cycle_jobs,
    episode_identity_from_path,
    start_status_server,
)
from media_identity import resolve_media_identity, retry_media_identity
from .persistence.state_store import StateStore, StateStoreError
from .scheduling.locks import ArtifactAccessCoordinator
from .cycle import CycleRunner
from .lifecycle import LifecycleController
from .maintenance.coordinator import MaintenanceCoordinator, MaintenanceOperation

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

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None


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


class _DaemonRepairExecutor:
    """Minimal Future executor whose workers cannot block interpreter exit."""

    def __init__(self, max_workers: int, thread_name_prefix: str):
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._stopped = False
        self._threads = [
            threading.Thread(
                target=self._worker,
                name=f"{thread_name_prefix}_{index}",
                daemon=True,
            )
            for index in range(max(1, int(max_workers)))
        ]
        for thread in self._threads:
            thread.start()

    def submit(self, function, /, *args, **kwargs) -> Future:
        future = Future()
        with self._lock:
            if self._stopped:
                raise RuntimeError("repair executor is shut down")
            self._queue.put((future, function, args, kwargs))
        return future

    def _worker(self) -> None:
        while True:
            work = self._queue.get()
            try:
                if work is None:
                    return
                future, function, args, kwargs = work
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    future.set_result(function(*args, **kwargs))
                except BaseException as exc:
                    future.set_exception(exc)
            finally:
                self._queue.task_done()

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            if cancel_futures:
                while True:
                    try:
                        work = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if work is not None:
                        work[0].cancel()
                    self._queue.task_done()
            for _thread in self._threads:
                self._queue.put(None)
        if wait:
            for thread in self._threads:
                thread.join()

class _DailyLogHandler(logging.Handler):
    def __init__(self, sink: _DailyLogSink):
        super().__init__()
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        self.sink.write(self.format(record) + "\n")


class _UtcLogFormatter(logging.Formatter):
    converter = time.gmtime


class _QueuedLogStream:
    """Turn fragmented print writes into one queued record per thread and line."""

    def __init__(self, logger: logging.Logger, level: int, primary):
        self.logger = logger
        self.level = level
        self.primary = primary
        self._local = threading.local()

    def write(self, value: str) -> int:
        if not value:
            return 0
        pending = getattr(self._local, "pending", "") + value
        lines = pending.split("\n")
        self._local.pending = lines.pop()
        for line in lines:
            if line:
                self.logger.log(self.level, line)
        return len(value)

    def flush(self) -> None:
        pending = getattr(self._local, "pending", "")
        if pending:
            self.logger.log(self.level, pending)
            self._local.pending = ""

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
TRANSLATION_TIMEOUT_MULTIPLIER = max(
    1.0, float(os.getenv("TRANSLATION_TIMEOUT_MULTIPLIER", "1.25"))
)
TRANSLATION_TIMEOUT_CAP = max(
    POLL_TIMEOUT, int(os.getenv("TRANSLATION_TIMEOUT_CAP", "10800"))
)
TRANSLATION_COLD_SECONDS_PER_CUE = max(
    0.01, float(os.getenv("TRANSLATION_COLD_SECONDS_PER_CUE", "1.8"))
)
TRANSLATION_TIMING_ALPHA = min(
    1.0, max(0.01, float(os.getenv("TRANSLATION_TIMING_ALPHA", "0.20")))
)
LONG_JOB_THRESHOLD = max(60, int(os.getenv("LONG_JOB_THRESHOLD", "1800")))
REPAIR_TIMEOUT_MULTIPLIER = max(
    1.0, float(os.getenv("REPAIR_TIMEOUT_MULTIPLIER", "2.0"))
)
CIRCUIT_FAILURE_THRESHOLD = max(
    1, int(os.getenv("CIRCUIT_FAILURE_THRESHOLD", "3"))
)
CIRCUIT_OPEN_CYCLES = max(1, int(os.getenv("CIRCUIT_OPEN_CYCLES", "3")))
if os.getenv("CIRCUIT_OPEN_SECONDS"):
    print(
        "[WARNING] CIRCUIT_OPEN_SECONDS is deprecated and has no scheduling "
        "effect; use CIRCUIT_OPEN_CYCLES"
    )
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
CLEANUP_MAX_REPAIR_ATTEMPTS = max(1, int(os.getenv("CLEANUP_MAX_REPAIR_ATTEMPTS", "5")))
CLEANUP_REPAIR_CONTEXT_LINES = max(0, int(os.getenv("CLEANUP_REPAIR_CONTEXT_LINES", "5")))
CLEANUP_FORMAT_REPAIR_ENABLED = os.getenv("CLEANUP_FORMAT_REPAIR_ENABLED", "true").lower() in ("1", "true", "yes")
CLEANUP_REPAIR_QUEUE_MAX = max(1, int(os.getenv("CLEANUP_REPAIR_QUEUE_MAX", "100")))
REPAIR_SHUTDOWN_GRACE_SECONDS = max(
    1, int(os.getenv("REPAIR_SHUTDOWN_GRACE_SECONDS", "30"))
)
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
_legacy_hold_days_raw = os.getenv("CLEANUP_QUARANTINE_HOLD_DAYS", "").strip()
_LEGACY_QUARANTINE_HOLD_DAYS = _legacy_hold_days_raw or None
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
QUARANTINE_ARTIFACT_RETENTION_DAYS = max(
    1, int(os.getenv("QUARANTINE_ARTIFACT_RETENTION_DAYS", str(RETENTION_DAYS)))
)
REGENERATION_INITIAL_DELAY_CYCLES = max(
    1, int(os.getenv("REGENERATION_INITIAL_DELAY_CYCLES", "2"))
)
REGENERATION_MAX_ATTEMPTS = max(
    0, int(os.getenv("REGENERATION_MAX_ATTEMPTS", "0"))
)
REGENERATION_MAX_DELAY_CYCLES = max(
    REGENERATION_INITIAL_DELAY_CYCLES,
    int(os.getenv("REGENERATION_MAX_DELAY_CYCLES", "16")),
)
REGENERATION_BACKOFF_MULTIPLIER = max(
    1.0, float(os.getenv("REGENERATION_BACKOFF_MULTIPLIER", "2.0"))
)
DONOR_RECOVERY_ENABLED = os.getenv(
    "DONOR_RECOVERY_ENABLED", "true"
).lower() in ("1", "true", "yes")
RETRY_BATCH_SIZE_PER_CYCLE = max(
    1, int(os.getenv("RETRY_BATCH_SIZE_PER_CYCLE", "5"))
)
RETRY_MAX_PER_SERIES_PER_CYCLE = max(
    1, int(os.getenv("RETRY_MAX_PER_SERIES_PER_CYCLE", "1"))
)
END_OF_CYCLE_REPAIR_RETRY_ENABLED = os.getenv(
    "END_OF_CYCLE_REPAIR_RETRY_ENABLED", "true"
).lower() in ("1", "true", "yes")
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
_CIRCUIT_CONFIG_FINGERPRINT = hashlib.sha256(
    json.dumps(
        {
            "lingarr": LINGARR_URL,
            "languages": LANGUAGES,
            "timeoutMultiplier": TRANSLATION_TIMEOUT_MULTIPLIER,
            "timeoutCap": TRANSLATION_TIMEOUT_CAP,
            "parallel": PARALLEL_TRANSLATES,
            "openCycles": CIRCUIT_OPEN_CYCLES,
        },
        sort_keys=True,
    ).encode("utf-8")
).hexdigest()[:16]
_VALIDATION_CONFIG_FINGERPRINT = hashlib.sha256(
    json.dumps(
        {
            "maxCueLines": CLEANUP_MAX_CUE_LINES,
            "maxCueChars": CLEANUP_MAX_CUE_CHARS,
            "maxExpansionRatio": CLEANUP_MAX_EXPANSION_RATIO,
            "maxExpansionChars": CLEANUP_MAX_EXPANSION_CHARS,
            "maxSourceSimilarity": CLEANUP_MAX_SOURCE_SIMILARITY,
            "maxCyrillicRatio": CLEANUP_MAX_CYRILLIC_RATIO,
            "maxCjkRatio": CLEANUP_MAX_CJK_RATIO,
            "maxLatinRatio": CLEANUP_MAX_LATIN_RATIO,
            "minMediaDuration": CLEANUP_MIN_MEDIA_DURATION,
            "minCuesPerMinute": CLEANUP_MIN_CUES_PER_MINUTE,
            "minTextCharsPerMinute": CLEANUP_MIN_TEXT_CHARS_PER_MINUTE,
            "minBytesPerMinute": CLEANUP_MIN_BYTES_PER_MINUTE,
            "minTimelineCoverage": CLEANUP_MIN_TIMELINE_COVERAGE,
            "undersizedRequiredSignals": CLEANUP_UNDERSIZED_REQUIRED_SIGNALS,
            "donorEnabled": DONOR_RECOVERY_ENABLED,
            "donorSimilarity": 0.95,
            "donorTimestampToleranceMs": 500,
        },
        sort_keys=True,
    ).encode("utf-8")
).hexdigest()[:16]

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
_log_queue: queue.Queue = queue.Queue()
_app_logger = logging.getLogger("bazarr_autotranslate")
_app_logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)
_app_logger.propagate = False
_queue_handler = logging.handlers.QueueHandler(_log_queue)
_app_logger.addHandler(_queue_handler)
_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(logging.Formatter("%(message)s"))
_daily_handler = _DailyLogHandler(_app_log_sink)
_daily_handler.setFormatter(
    _UtcLogFormatter(
        "%(asctime)s.%(msecs)03dZ %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
)
_log_listener = logging.handlers.QueueListener(
    _log_queue, _console_handler, _daily_handler, respect_handler_level=True
)
_log_listener.start()
atexit.register(_app_log_sink.close)
atexit.register(_log_listener.stop)
sys.stdout = _QueuedLogStream(_app_logger, logging.INFO, _console_handler.stream)
sys.stderr = _QueuedLogStream(_app_logger, logging.ERROR, _console_handler.stream)

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
_repair_shutdown_event = threading.Event()
_repair_capacity = threading.BoundedSemaphore(PARALLEL_TRANSLATES + CLEANUP_REPAIR_QUEUE_MAX)
_pending_repairs: dict[Future, dict] = {}
_pending_repairs_lock = threading.Lock()
_repair_keys: set[tuple] = set()
_artifact_access = ArtifactAccessCoordinator()
_duration_cache: dict[tuple[str, int, int], float | None] = {}
_duration_cache_lock = threading.Lock()
_pending_prune_videos: dict[str, str | None] = {}
_pending_prune_lock = threading.Lock()
_maintenance_scan_contexts: dict[str, dict] = {}
_maintenance_scan_contexts_lock = threading.Lock()
_status_tracker: StatusTracker | None = None
_completed_cycle = 0

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
    donor_source_attempt: int | None = None


class CycleSuppressionRegistry:
    """Thread-safe quarantine/delete suppression scoped to the active cycle."""

    def __init__(self):
        self._lock = threading.Lock()
        self._cycle_id: str | None = None
        self._entries: dict[str, dict] = {}

    def begin_cycle(self, cycle_id: str) -> None:
        with self._lock:
            self._cycle_id = str(cycle_id)
            self._entries = {}

    def suppress(self, identity: str | None, *, action: str) -> dict | None:
        if identity is None:
            return None
        with self._lock:
            entry = {
                "identity": identity,
                "action": action,
                "cycleId": self._cycle_id,
            }
            self._entries[identity] = entry
            return dict(entry)

    def get(self, identity: str | None) -> dict | None:
        if identity is None:
            return None
        with self._lock:
            entry = self._entries.get(identity)
            return dict(entry) if entry is not None else None


_cycle_suppressions = CycleSuppressionRegistry()


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


class SharedCapacityCoordinator:
    """Coordinate file translations and repairs with repair-first admission."""

    def __init__(self, limit: int):
        self.limit = max(1, limit)
        self._condition = threading.Condition()
        self._active = 0
        self._waiting_repairs = 0
        self._next_token = 1
        self._tokens: dict[int, str] = {}
        self._local = threading.local()

    def acquire_translation(self) -> int | None:
        with self._condition:
            while not shutdown_requested:
                if self._active < self.limit and self._waiting_repairs == 0:
                    token = self._next_token
                    self._next_token += 1
                    self._tokens[token] = "translation"
                    self._active += 1
                    self._local.translation_token = token
                    return token
                self._condition.wait(timeout=1)
        return None

    def reserve_repair(self) -> int | None:
        """Reserve repair priority, transferring the caller's file slot if held."""
        with self._condition:
            translation_token = getattr(self._local, "translation_token", None)
            if self._tokens.get(translation_token) == "translation":
                self._tokens[translation_token] = "repair-reserved"
                self._local.translation_token = None
                return translation_token
            token = self._next_token
            self._next_token += 1
            self._tokens[token] = "repair-waiting"
            self._waiting_repairs += 1
            self._condition.notify_all()
            return token

    def start_repair(self, token: int) -> bool:
        with self._condition:
            while not shutdown_requested:
                state = self._tokens.get(token)
                if state == "repair-reserved":
                    self._tokens[token] = "repair"
                    return True
                if state != "repair-waiting":
                    return False
                if self._active < self.limit:
                    self._waiting_repairs -= 1
                    self._active += 1
                    self._tokens[token] = "repair"
                    return True
                self._condition.wait(timeout=1)
            self._cancel_waiter(token)
            return False

    def _cancel_waiter(self, token: int) -> None:
        if self._tokens.pop(token, None) == "repair-waiting":
            self._waiting_repairs -= 1
        self._condition.notify_all()

    def release(self, token: int | None) -> None:
        if token is None:
            return
        with self._condition:
            state = self._tokens.pop(token, None)
            if state in ("translation", "repair", "repair-reserved"):
                self._active = max(0, self._active - 1)
            elif state == "repair-waiting":
                self._waiting_repairs = max(0, self._waiting_repairs - 1)
            if getattr(self._local, "translation_token", None) == token:
                self._local.translation_token = None
            self._condition.notify_all()

    def release_current_translation(self) -> None:
        self.release(getattr(self._local, "translation_token", None))

    def reset(self) -> None:
        with self._condition:
            self._tokens.clear()
            self._active = 0
            self._waiting_repairs = 0
            self._local.translation_token = None
            self._condition.notify_all()


_shared_capacity = SharedCapacityCoordinator(PARALLEL_TRANSLATES)


class FileLaneGate:
    """Prefer dedicated lanes while lending any capacity that would sit idle."""

    def __init__(self, workers: int):
        self.workers = max(1, workers)
        # Lane numbers start at one: odd lanes handle short jobs and even lanes
        # handle long jobs.  A lone worker remains a short lane, although long
        # work may use it when no short job is waiting.
        self.short_capacity = (self.workers + 1) // 2
        self.long_capacity = self.workers // 2
        self._condition = threading.Condition()
        self._active_long = 0
        self._active_short = 0
        self._waiters: dict[int, tuple[bool, float, int]] = {}
        self._next_waiter = 0

    def acquire(self, is_long: bool, estimate_seconds: float = 0.0) -> str | None:
        with self._condition:
            token = self._next_waiter
            self._next_waiter += 1
            self._waiters[token] = (
                bool(is_long),
                max(0.0, float(estimate_seconds)),
                token,
            )
            try:
                while not shutdown_requested:
                    long_waiters = sorted(
                        (
                            (waiter_token, estimate, sequence)
                            for waiter_token, (long_job, estimate, sequence)
                            in self._waiters.items()
                            if long_job
                        ),
                        key=lambda entry: (-entry[1], entry[2]),
                    )
                    short_waiters = sorted(
                        (
                            (waiter_token, estimate, sequence)
                            for waiter_token, (long_job, estimate, sequence)
                            in self._waiters.items()
                            if not long_job
                        ),
                        key=lambda entry: (-entry[1], entry[2]),
                    )
                    if self.workers == 1:
                        preferred = short_waiters or long_waiters
                        available = (
                            self._active_long + self._active_short == 0
                            and bool(preferred)
                            and preferred[0][0] == token
                        )
                        lane = "long" if is_long else "short"
                    elif is_long:
                        preferred_long = (
                            bool(long_waiters) and long_waiters[0][0] == token
                        )
                        if preferred_long and self._active_long < self.long_capacity:
                            available = True
                            lane = "long"
                        else:
                            available = (
                                preferred_long
                                and self._active_short < self.short_capacity
                                and not short_waiters
                            )
                            lane = "long (borrowed)"
                    else:
                        preferred_short = (
                            bool(short_waiters) and short_waiters[0][0] == token
                        )
                        if preferred_short and self._active_short < self.short_capacity:
                            available = True
                            lane = "short"
                        else:
                            available = (
                                preferred_short
                                and self._active_long < self.long_capacity
                                and not long_waiters
                            )
                            lane = "short (borrowed)"
                    if available:
                        self._waiters.pop(token, None)
                        if lane in {"long", "short (borrowed)"}:
                            self._active_long += 1
                        else:
                            self._active_short += 1
                        self._condition.notify_all()
                        return lane
                    self._condition.wait(timeout=1)
            finally:
                self._waiters.pop(token, None)
                self._condition.notify_all()
        return None

    def release(self, lane: str | None) -> None:
        if lane is None:
            return
        with self._condition:
            if lane in {"long", "short (borrowed)"}:
                self._active_long = max(0, self._active_long - 1)
            else:
                self._active_short = max(0, self._active_short - 1)
            self._condition.notify_all()


_file_lane_gate = FileLaneGate(PARALLEL_TRANSLATES)


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
    details: dict | None = None,
) -> bool:
    if _status_tracker is None:
        return False
    try:
        kwargs = {"repaired": repaired, "reason": reason}
        if details is not None:
            kwargs["details"] = details
        return _status_tracker.transition_for(
            item_type, item_id, target_lang, state, **kwargs
        )
    except OSError as exc:
        print(f"{YELLOW}[STATUS] Could not persist job update: {exc}{RESET}")
        return False


def _status_identity(job_kwargs: dict, label: str, target_lang: str) -> dict:
    identity = retry_media_identity({
        "itemType": job_kwargs.get("item_type"),
        "itemId": job_kwargs.get("item_id"),
        "targetLanguage": target_lang,
        "mediaTitle": label,
        "sourcePath": job_kwargs.get("source_path"),
        "seriesTitle": job_kwargs.get("series_title"),
    })
    return {
        "title": identity["displayTitle"],
        "episodeCode": identity.get("episodeCode"),
        "episodeTitle": identity.get("episodeTitle"),
        "itemType": job_kwargs.get("item_type"),
        "itemId": job_kwargs.get("item_id"),
        "targetLanguage": target_lang,
        "sourceLanguage": job_kwargs.get("source_lang"),
    }


def _status_create_repair_ref(
    job_kwargs: dict,
    label: str,
    target_lang: str,
    details: dict,
) -> dict | None:
    if _status_tracker is None:
        return None
    try:
        cycle_key = _status_tracker.active_cycle_job_key(
            job_kwargs.get("item_type"),
            job_kwargs.get("item_id"),
            target_lang,
        )
        if cycle_key:
            _status_tracker.transition(cycle_key, "repair_queued", details=details)
            return {"kind": "cycle", "id": cycle_key}
        job_id = _status_tracker.create_maintenance_job(
            "cue_repair",
            _status_identity(job_kwargs, label, target_lang),
            state="repair_queued",
            details=details,
        )
        return {"kind": "maintenance", "id": job_id}
    except OSError as exc:
        print(f"{YELLOW}[STATUS] Could not persist repair job: {exc}{RESET}")
        return None


def _status_ref_transition(
    status_ref: dict | None,
    state: str,
    *,
    reason: str | None = None,
    details: dict | None = None,
) -> bool:
    if _status_tracker is None or not status_ref:
        return False
    try:
        if status_ref.get("kind") == "cycle":
            return _status_tracker.transition(
                status_ref["id"], state, reason=reason, details=details
            )
        return _status_tracker.transition_maintenance(
            status_ref["id"], state, reason=reason, details=details
        )
    except OSError as exc:
        print(f"{YELLOW}[STATUS] Could not persist repair progress: {exc}{RESET}")
        return False


def _status_ref_complete(
    status_ref: dict | None,
    outcome: str,
    *,
    reason: str | None = None,
    repaired: bool = False,
    details: dict | None = None,
) -> bool:
    if _status_tracker is None or not status_ref:
        return False
    try:
        if status_ref.get("kind") == "cycle":
            cycle_outcome = "accepted" if outcome == "repaired" else (
                "quarantined" if outcome in ("quarantined", "deleted") else outcome
            )
            return _status_tracker.transition(
                status_ref["id"],
                cycle_outcome,
                repaired=repaired or outcome == "repaired",
                reason=reason,
                details=details,
            )
        return _status_tracker.complete_maintenance(
            status_ref["id"], outcome, reason=reason, details=details
        )
    except OSError as exc:
        print(f"{YELLOW}[STATUS] Could not persist repair completion: {exc}{RESET}")
        return False


def _complete_repair_status(
    metadata: dict,
    outcome: str,
    *,
    reason: str | None = None,
    repaired: bool = False,
    details: dict | None = None,
) -> bool:
    status_ref = metadata.get("status_ref")
    if status_ref:
        return _status_ref_complete(
            status_ref,
            outcome,
            reason=reason,
            repaired=repaired,
            details=details,
        )
    # Compatibility for direct callback callers and legacy in-memory jobs.
    cycle_outcome = "accepted" if outcome == "repaired" else (
        "quarantined" if outcome in ("quarantined", "deleted") else outcome
    )
    return _status_transition(
        metadata.get("item_type"),
        metadata.get("item_id"),
        metadata.get("target_lang", ""),
        cycle_outcome,
        repaired=repaired or outcome == "repaired",
        reason=reason,
    )


def _status_set_episode_identity(
    item_type: str | None,
    item_id: int | None,
    path: str | Path | None,
) -> None:
    if _status_tracker is None or item_type != "episodes":
        return
    episode_code = episode_identity_from_path(path)
    if not episode_code:
        return
    try:
        _status_tracker.set_episode_identity(item_type, item_id, episode_code)
    except OSError as exc:
        print(f"{YELLOW}[STATUS] Could not persist episode identity: {exc}{RESET}")


def _status_admit_retry(plan: dict, identity: dict) -> None:
    if _status_tracker is None:
        return
    try:
        _status_tracker.admit_retry(
            plan_id=plan["id"],
            item_type=plan["itemType"],
            item_id=plan["itemId"],
            target_language=plan["targetLanguage"],
            display_title=identity["displayTitle"],
            episode_code=identity.get("episodeCode"),
            episode_title=identity.get("episodeTitle"),
            attempt=int(plan.get("attemptCount", 0)) + 1,
        )
    except OSError as exc:
        print(f"{YELLOW}[STATUS] Could not admit retry job: {exc}{RESET}")


def _refresh_status_diagnostics() -> None:
    if _status_tracker is None or not hasattr(_status_tracker, "set_diagnostics"):
        return
    target = next(iter(CLEANUP_LANGUAGES), LANGUAGES[-1] if LANGUAGES else "et")
    try:
        file_timing = _get_validation_state().timing_estimate(
            kind="file",
            source_language=None,
            target_language=target,
            cold_seconds_per_cue=TRANSLATION_COLD_SECONDS_PER_CUE,
            alpha=TRANSLATION_TIMING_ALPHA,
        )
        repair_timing = _get_validation_state().timing_estimate(
            kind="repair",
            source_language=None,
            target_language=target,
            cold_seconds_per_cue=TRANSLATION_COLD_SECONDS_PER_CUE,
            alpha=TRANSLATION_TIMING_ALPHA,
        )
        retry_plans = _get_validation_state().retry_plans()
        for plan in retry_plans:
            plan.update(retry_media_identity(plan))
            attempts = _get_validation_state().quarantine_attempts(
                plan["itemType"], plan["itemId"], plan["targetLanguage"]
            )
            plan["archivedAttemptCount"] = len(attempts)
            donor_sources = [
                donor.get("sourceAttempt")
                for attempt in attempts
                for donor in attempt.get("donorProvenance", [])
                if donor.get("sourceAttempt") is not None
            ]
            plan["latestDonorAttempt"] = donor_sources[0] if donor_sources else None
            plan.update(
                _get_validation_state().recovery_summary(
                    plan["itemType"], plan["itemId"], plan["targetLanguage"]
                )
            )
            plan["manualReview"] = plan.get("lastDeferralClass") == "manual_review"
        _status_tracker.set_diagnostics(
            timing={"file": file_timing, "repair": repair_timing},
            circuits=_get_validation_state().circuit_breakers(),
            retries=retry_plans,
            completed_cycle=_get_validation_state().completed_cycle(),
            retry_max_attempts=REGENERATION_MAX_ATTEMPTS,
            recovery=_get_validation_state().diagnostic_aggregates(),
        )
    except (OSError, StateStoreError) as exc:
        print(f"{YELLOW}[STATUS] Could not refresh diagnostics: {exc}{RESET}")


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


def _status_finish_cycle(metrics: dict | None = None) -> None:
    if _status_tracker is None:
        return
    try:
        _status_tracker.finish_cycle(metrics)
    except OSError as exc:
        print(f"{YELLOW}[STATUS] Could not persist cycle completion: {exc}{RESET}")


def _status_record_maintenance(metrics: dict) -> None:
    if _status_tracker is None:
        return
    try:
        _status_tracker.record_maintenance(metrics)
    except OSError as exc:
        print(f"{YELLOW}[STATUS] Could not persist maintenance status: {exc}{RESET}")


def _status_create_maintenance(
    operation: str,
    identity: dict | None = None,
    *,
    state: str = "queued",
    details: dict | None = None,
) -> str | None:
    if _status_tracker is None:
        return None
    try:
        return _status_tracker.create_maintenance_job(
            operation, identity, state=state, details=details
        )
    except OSError as exc:
        print(f"{YELLOW}[STATUS] Could not create maintenance job: {exc}{RESET}")
        return None


def _status_update_maintenance(
    job_id: str | None,
    state: str,
    *,
    reason: str | None = None,
    details: dict | None = None,
) -> bool:
    if _status_tracker is None or not job_id:
        return False
    try:
        return _status_tracker.transition_maintenance(
            job_id, state, reason=reason, details=details
        )
    except OSError as exc:
        print(f"{YELLOW}[STATUS] Could not update maintenance job: {exc}{RESET}")
        return False


def _status_complete_maintenance(
    job_id: str | None,
    outcome: str,
    *,
    reason: str | None = None,
    details: dict | None = None,
) -> bool:
    if _status_tracker is None or not job_id:
        return False
    try:
        return _status_tracker.complete_maintenance(
            job_id, outcome, reason=reason, details=details
        )
    except OSError as exc:
        print(f"{YELLOW}[STATUS] Could not complete maintenance job: {exc}{RESET}")
        return False


def _status_record_maintenance_outcome(
    operation: str,
    outcome: str,
    identity: dict | None = None,
    *,
    reason: str | None = None,
) -> None:
    if _status_tracker is None:
        return
    try:
        _status_tracker.record_maintenance_outcome(
            operation, outcome, identity, reason=reason
        )
    except OSError as exc:
        print(f"{YELLOW}[STATUS] Could not record maintenance outcome: {exc}{RESET}")


def _maintenance_file_identity(
    path: str | Path,
    target_language: str | None = None,
) -> dict:
    identity = retry_media_identity({
        "itemType": "media",
        "mediaTitle": Path(path).name,
        "targetLanguage": target_language,
    })
    return {
        "title": identity["displayTitle"],
        "episodeCode": identity.get("episodeCode"),
        "episodeTitle": identity.get("episodeTitle"),
        "targetLanguage": target_language,
    }


def _maintenance_metrics(stats: dict) -> dict:
    return {
        "formatted": stats.get("formatted_files", 0),
        "repaired": stats.get("repaired_files", 0)
        + stats.get("async_repairs_completed", 0),
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
        "cycle_suppressions": stats.get("cycle_suppressions", 0),
        "variant_outputs": (
            stats.get("variant_outputs_discovered", 0)
            + stats.get("recovered_pending_outputs", 0)
        ),
        "failures": (
            stats.get("repair_failures", 0)
            + stats.get("action_failures", 0)
            + stats.get("prune_failures", 0)
            + stats.get("async_repair_failures", 0)
        ),
    }


def _scan_progress_details(context: dict) -> dict:
    stats = context.get("stats", {})
    discovered = int(context.get("files_discovered", 0))
    checked = int(context.get("files_checked", 0))
    elapsed = max(0.001, time.monotonic() - context["started"])
    remaining = max(0, discovered - checked)
    eta = round((elapsed / checked) * remaining, 1) if checked else None
    details = {
        "filesDiscovered": discovered,
        "filesChecked": checked,
        "filesRemaining": remaining,
        "unchangedFilesSkipped": stats.get("skipped_unchanged", 0),
        "validationsPerformed": stats.get("files_checked", 0),
        "formatRepairs": stats.get("formatted_files", 0),
        "cueRepairsQueued": context.get("repairs_queued", 0),
        "cueRepairsCompleted": context.get("repairs_completed", 0),
        "quarantines": (
            stats.get("quarantined_files", 0)
            + stats.get("undersized_quarantined", 0)
            + stats.get("prune_quarantined", 0)
        ),
        "failures": _maintenance_metrics(stats)["failures"],
        "progress": round(checked * 100 / max(1, discovered), 1),
        "estimatedSeconds": round(elapsed + eta, 1) if eta is not None else None,
        "etaSeconds": eta,
    }
    return details


def _publish_scan_progress(scan_job_id: str | None, *, force: bool = False) -> None:
    if not scan_job_id:
        return
    details = None
    with _maintenance_scan_contexts_lock:
        context = _maintenance_scan_contexts.get(scan_job_id)
        if context is None:
            return
        now = time.monotonic()
        should_publish = bool(
            force
            or context["files_checked"] == 1
            or now - context.get("last_publish", 0) >= 0.5
            or context["files_checked"] >= context["files_discovered"]
        )
        if should_publish:
            context["last_publish"] = now
            details = _scan_progress_details(context)
    if details is not None:
        _status_update_maintenance(scan_job_id, "scanning", details=details)


def _scan_child_queued(scan_job_id: str | None) -> None:
    if not scan_job_id:
        return
    with _maintenance_scan_contexts_lock:
        context = _maintenance_scan_contexts.get(scan_job_id)
        if context is None:
            return
        context["pending"] += 1
        context["repairs_queued"] += 1
        details = _scan_progress_details(context)
    _status_update_maintenance(scan_job_id, "scanning", details=details)


def _scan_child_finished(scan_job_id: str | None, outcome: str) -> None:
    if not scan_job_id:
        return
    finalize = False
    with _maintenance_scan_contexts_lock:
        context = _maintenance_scan_contexts.get(scan_job_id)
        if context is None:
            return
        context["pending"] = max(0, context["pending"] - 1)
        context["repairs_completed"] += 1
        if outcome == "repaired":
            context["stats"]["async_repairs_completed"] = (
                context["stats"].get("async_repairs_completed", 0) + 1
            )
        elif outcome not in ("completed", "quarantined", "deleted"):
            context["stats"]["async_repair_failures"] = (
                context["stats"].get("async_repair_failures", 0) + 1
            )
        details = _scan_progress_details(context)
        finalize = context.get("enumeration_done", False) and context["pending"] == 0
        if finalize:
            _maintenance_scan_contexts.pop(scan_job_id, None)
    failed = bool(context["stats"].get("async_repair_failures", 0))
    if finalize:
        _status_complete_maintenance(
            scan_job_id,
            "failed" if failed else "accepted",
            reason="repair worker failed" if failed else None,
            details=details,
        )
        _status_record_maintenance(_maintenance_metrics(context["stats"]))
    else:
        _status_update_maintenance(
            scan_job_id, "waiting_repair_completion", details=details
        )


def _scan_enumeration_finished(scan_job_id: str | None, stats: dict) -> None:
    if not scan_job_id:
        _status_record_maintenance(_maintenance_metrics(stats))
        return
    with _maintenance_scan_contexts_lock:
        context = _maintenance_scan_contexts.get(scan_job_id)
        if context is None:
            return
        context["stats"].update(stats)
        context["enumeration_done"] = True
        details = _scan_progress_details(context)
        finalize = context["pending"] == 0
        if finalize:
            _maintenance_scan_contexts.pop(scan_job_id, None)
    failed = bool(
        context["stats"].get("async_repair_failures", 0)
        or context["stats"].get("cleanup_repair_failures", 0)
    )
    if finalize:
        _status_complete_maintenance(
            scan_job_id,
            "failed" if failed else "accepted",
            reason="repair worker failed" if failed else None,
            details=details,
        )
        _status_record_maintenance(_maintenance_metrics(context["stats"]))
    else:
        _status_update_maintenance(
            scan_job_id, "waiting_repair_completion", details=details
        )


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
        _resolve_retry_success(item_type, item_id, target_lang)
        _status_transition(
            item_type,
            item_id,
            target_lang,
            "accepted",
            repaired=action in ("formatted", "repaired"),
        )
    elif action in ("repair-queued", "repair-duplicate"):
        # The asynchronous repair path owns its exact lifecycle/status reference.
        return
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
            from .subtitles.core import build_detector
            _cleanup_detector = build_detector()
        return _cleanup_detector


def _get_validation_state():
    global _validation_state
    with _validation_state_lock:
        if _validation_state is None:
            from .subtitles.core import VALIDATOR_VERSION
            _validation_state = StateStore(
                STATE_DB_FILE,
                validator_version=VALIDATOR_VERSION,
                config_fingerprint=_VALIDATION_CONFIG_FINGERPRINT,
            )
        return _validation_state


def _initialize_state_store() -> StateStore:
    global _validation_state
    with _validation_state_lock:
        if _validation_state is not None:
            return _validation_state
        from .subtitles.core import VALIDATOR_VERSION
        store = StateStore(
            STATE_DB_FILE,
            acquire_process_lock=True,
            validator_version=VALIDATOR_VERSION,
            config_fingerprint=_VALIDATION_CONFIG_FINGERPRINT,
        )
        migration = store.migrate_legacy(
            SUBMIT_CACHE_FILE,
            VALIDATION_STATE_FILE,
            cooldown_seconds=RESUBMIT_COOLDOWN,
        )
        reconciliation = store.reconcile_pending_operations()
        circuit_migration = store.initialize_cycle_circuits(CIRCUIT_OPEN_CYCLES)
        print(
            "[STATE] Circuit migration: "
            f"completed_cycle={circuit_migration['completedCycle']}, "
            f"migrated={circuit_migration['migrated']}, "
            f"retired_generic={circuit_migration['retiredGeneric']}"
        )
        _validation_state = store
        retry_migration = {"migrated": 0, "unresolved": 0, "ignored": 0}
        candidates = store.legacy_retry_candidates()
        for candidate in candidates:
            source_path = candidate.get("sourcePath")
            if (
                not source_path
                or not os.path.exists(source_path)
                or _file_hash_or_none(source_path) != candidate.get("sourceHash")
            ):
                retry_migration["unresolved"] += 1
                continue
            rules = set(candidate.get("rules") or [])
            if rules & {"source_unreadable", "source_structure", "undersized_source"}:
                retry_migration["ignored"] += 1
                continue
            identity = retry_media_identity(candidate)
            circuit_identity = resolve_media_identity(
                {
                    "seriesTitle": candidate.get("seriesTitle"),
                    "title": identity["displayTitle"],
                },
                candidate["itemType"],
                candidate["itemId"],
                source_path,
            )
            store.schedule_retry_plan(
                item_type=candidate["itemType"],
                item_id=candidate["itemId"],
                target_language=candidate["targetLanguage"],
                source_hash=candidate["sourceHash"],
                source_path=source_path,
                source_language=candidate.get("sourceLanguage"),
                target_path=candidate.get("targetPath"),
                series_key=circuit_identity["key"],
                series_title=circuit_identity["title"],
                media_title=os.path.basename(source_path),
                source_cue_count=_count_srt_cues(source_path),
                failure_class="whole_file",
                rules=rules,
                state="regeneration_waiting",
                failed_output_hash=candidate.get("targetHash"),
                eligible_completed_cycle=(
                    store.completed_cycle() + REGENERATION_INITIAL_DELAY_CYCLES
                ),
                reason="migrated legacy quarantine tombstone",
            )
            retry_migration["migrated"] += 1
        store.mark_legacy_retry_migration_complete()
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
    if any(retry_migration.values()):
        print(
            f"[STATE] Quarantine retry migration: "
            f"{retry_migration['migrated']} scheduled, "
            f"{retry_migration['unresolved']} unresolved, "
            f"{retry_migration['ignored']} ignored; admissions remain limited to "
            f"{RETRY_BATCH_SIZE_PER_CYCLE}/cycle and "
            f"{RETRY_MAX_PER_SERIES_PER_CYCLE}/series"
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


def _mark_submission_failed(
    attempt_id: int,
    *,
    failure_category: str | None = None,
    failure_details: dict | None = None,
) -> None:
    _get_validation_state().mark_submission_failed(
        attempt_id,
        failure_category=failure_category,
        failure_details=failure_details,
    )


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


def _tracked_bazarr_sync(
    had_episodes: bool,
    had_movies: bool,
    timeout: int,
) -> bool:
    scope = (
        "Series and movies" if had_episodes and had_movies
        else "Series" if had_episodes else "Movies"
    )
    job_id = _status_create_maintenance(
        "bazarr_sync",
        {"title": scope},
        state="synchronizing",
    )
    trigger_bazarr_sync(had_episodes, had_movies)
    success = wait_for_bazarr_sync(had_episodes, had_movies, timeout)
    _status_complete_maintenance(
        job_id,
        "accepted" if success else "failed",
        reason=None if success else "Bazarr synchronization did not complete",
    )
    return success

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
    strict: bool = False,
    cancellation_requested=None,
) -> str | None:
    def record_provider_event(
        classification: str,
        *,
        retryable: bool,
        http_status: int | None = None,
        payload=None,
    ) -> None:
        try:
            state = _get_validation_state()
            if not hasattr(state, "record_provider_event"):
                return
            shape = None
            if payload is not None:
                from autotranslate.services.lingarr import response_shape
                shape = response_shape(payload)
            state.record_provider_event(
                provider="lingarr",
                operation="translate_line",
                classification=classification,
                retryable=retryable,
                http_status=http_status,
                response_shape=shape,
            )
        except (OSError, StateStoreError):
            pass

    body = {
        "subtitleLine": subtitle_line,
        "sourceLanguage": source_lang,
        "targetLanguage": target_lang,
        "contextLinesBefore": [] if strict else context_before,
        "contextLinesAfter": [] if strict else context_after,
    }
    if strict:
        body["instructions"] = (
            "Return only the translated subtitle cue in the requested target "
            "language and script. Do not include commentary or surrounding dialogue."
        )
    dbg(
        f"lingarr_translate_line: POST source={source_lang} target={target_lang} "
        f"before={len(context_before)} after={len(context_after)} chars={len(subtitle_line)}"
    )
    started = time.monotonic()
    try:
        request_args = {
            "headers": LINGARR_HEADERS,
            "json": body,
            # Normal provider reliability is independent of shutdown grace.
            "timeout": max(CONNECT_TIMEOUT, 120),
        }
        if cancellation_requested is None:
            r = requests.post(lingarr_url("Translate/line"), **request_args)
        else:
            request_result: queue.Queue = queue.Queue(maxsize=1)

            def run_request() -> None:
                try:
                    request_result.put((requests.post(
                        lingarr_url("Translate/line"), **request_args
                    ), None))
                except Exception as exc:  # forwarded to the repair worker
                    request_result.put((None, exc))

            # A timed-out provider socket must not hold application shutdown.
            # The daemon owns only the HTTP call; all state writes remain here.
            threading.Thread(
                target=run_request,
                name="lingarr-line-request",
                daemon=True,
            ).start()
            while True:
                if cancellation_requested():
                    if outcome_meta is not None:
                        outcome_meta.update({
                            "cancelled": True,
                            "httpDurationSeconds": round(
                                time.monotonic() - started, 3
                            ),
                        })
                    return None
                try:
                    r, request_error = request_result.get(timeout=0.1)
                    break
                except queue.Empty:
                    continue
            if request_error is not None:
                raise request_error
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
            record_provider_event(
                "success", retryable=False, http_status=r.status_code, payload=payload
            )
            return payload.strip()
        if isinstance(payload, dict):
            for key in ("translatedSubtitle", "translatedLine", "translation", "text"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    record_provider_event(
                        "success",
                        retryable=False,
                        http_status=r.status_code,
                        payload=payload,
                    )
                    return value.strip()
        record_provider_event(
            "malformed_response",
            retryable=True,
            http_status=r.status_code,
            payload=payload,
        )
        print(f"{RED}[ERROR] lingarr_translate_line: unexpected response shape{RESET}")
    except Exception as e:
        elapsed = time.monotonic() - started
        status = getattr(getattr(e, "response", None), "status_code", None)
        retryable = status is None or status == 429 or status >= 500
        record_provider_event(
            (
                "transport"
                if status is None
                else "http_retryable" if retryable else "http_permanent"
            ),
            retryable=retryable,
            http_status=status,
        )
        if outcome_meta is not None:
            outcome_meta.update({"httpStatus": status, "httpDurationSeconds": round(elapsed, 3)})
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


def lingarr_cancel_job(job_id: int) -> bool:
    detail = lingarr_get_job(job_id)
    if not detail:
        return False
    try:
        response = requests.post(
            lingarr_url("TranslationRequest/cancel"),
            headers=LINGARR_HEADERS,
            json=detail,
            timeout=CONNECT_TIMEOUT,
        )
        return response.status_code in (200, 202, 204)
    except requests.RequestException as exc:
        print(f"{YELLOW}[WARNING] Could not cancel Lingarr job {job_id}: {exc}{RESET}")
        return False


def _classify_lingarr_failure(status: str | None, text: str) -> str:
    folded = f"{status or ''} {text}".casefold()
    if "cancel" in folded or "interrupt" in folded:
        return "cancelled"
    if any(token in folded for token in ("context length", "context window", "token limit", "too many tokens")):
        return "context_limit"
    if any(token in folded for token in ("parse", "invalid json", "deserialize", "format")):
        return "parser"
    if any(token in folded for token in ("disk", "storage", "permission denied", "no space", "read-only")):
        return "storage"
    if any(token in folded for token in ("model", "provider", "rate limit", "content filter")):
        return "model"
    if any(token in folded for token in ("timeout", "http", "network", "connection", "service unavailable")):
        return "service"
    return "unknown"


def _sanitize_failure_message(value: object, limit: int = 500) -> str:
    message = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    for secret in (BAZARR_API_KEY, LINGARR_API_KEY):
        if secret:
            message = message.replace(secret, "[redacted]")
    message = _re.sub(
        r"(?i)(?:[a-z]:\\|/)(?:[^\\/\s]+[\\/])+[^\\/\s]+",
        "[path]",
        message,
    )
    if any(marker in message.casefold() for marker in (
        "[source]", "[target]", "subtitle text", "translated text", "prompt:",
    )):
        return "[redacted content event]"
    return message[:limit]


def _safe_failure_details(
    job_id: int | None,
    *,
    terminal_job: dict | None = None,
    elapsed_seconds: float | None = None,
) -> dict:
    if job_id is None:
        return {}
    job = terminal_job if isinstance(terminal_job, dict) else (lingarr_get_job(job_id) or {})
    messages = [
        _sanitize_failure_message(event.get("message"))
        for event in job.get("events", [])
        if isinstance(event, dict) and event.get("message")
    ]
    error_message = _sanitize_failure_message(job.get("errorMessage"), 1000) or None
    safe_scalars = {}
    blocked = ("source", "target", "subtitle", "prompt", "text", "path", "key", "token")
    for key, value in job.items():
        if (
            len(safe_scalars) >= 12
            or any(part in str(key).casefold() for part in blocked)
            or key in {"events", "errorMessage", "provider", "model", "status", "progress"}
            or not isinstance(value, (str, int, float, bool, type(None)))
        ):
            continue
        safe_scalars[str(key)[:80]] = (
            _sanitize_failure_message(value, 300)
            if isinstance(value, str) else value
        )
    combined = " ".join(filter(None, [error_message, *messages]))
    return {
        "jobId": job_id,
        "status": job.get("status"),
        "progress": job.get("progress"),
        "category": _classify_lingarr_failure(job.get("status"), combined),
        "errorMessage": error_message,
        "events": messages[-10:],
        "provider": job.get("provider") or "unknown",
        "model": job.get("model") or "unknown",
        "elapsedSeconds": round(elapsed_seconds, 3) if elapsed_seconds is not None else None,
        "safePayload": safe_scalars,
    }


def lingarr_poll_job(
    job_id: int,
    deadline: float,
    label: str,
    progress_callback=None,
) -> str | None:
    last_progress = -1
    while not shutdown_requested:
        job = lingarr_get_job(job_id)
        if job:
            status = job.get("status", "")
            progress = job.get("progress", 0)
            if progress != last_progress:
                last_progress = progress
                dbg(f"{label} job {job_id}: status={status} progress={progress}")
                if progress_callback is not None:
                    progress_callback(progress)
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


def _recover_failed_lingarr_job(
    job_id: int,
    source_path: str,
    target_path: str,
    source_lang: str,
    target_lang: str,
    label: str,
) -> dict:
    """Rebuild a failed file job from completed Lingarr lines and repair gaps."""
    from .subtitles.core import (
        SubtitleCue,
        parse_srt_cues,
        read_text_best_effort,
        render_srt_cues,
    )

    detail = lingarr_get_job(job_id) or {}
    line_rows = detail.get("lines")
    if not isinstance(line_rows, list) or not line_rows:
        return {"recovered": False, "reason": "Lingarr returned no positioned lines"}
    raw = read_text_best_effort(Path(source_path))
    if raw is None:
        return {"recovered": False, "reason": "source unreadable"}
    source_cues, errors = parse_srt_cues(raw)
    if errors or not source_cues:
        return {"recovered": False, "reason": "source SRT is not structurally recoverable"}

    positioned = {
        int(row["position"]): row
        for row in line_rows
        if isinstance(row, dict) and isinstance(row.get("position"), int)
    }
    if not positioned:
        return {"recovered": False, "reason": "Lingarr line positions missing"}
    offset = 0 if 0 in positioned else 1
    recovered: list[SubtitleCue] = []
    unresolved: list[int] = []
    repair_elapsed = 0.0
    repair_attempts = 0
    repaired_cues = 0

    for index, cue in enumerate(source_cues):
        row = positioned.get(index + offset, {})
        translated = row.get("target") if isinstance(row, dict) else None
        translated = translated.strip() if isinstance(translated, str) else ""
        if not translated:
            before = [entry.text for entry in source_cues[max(0, index - 5):index]]
            after = [entry.text for entry in source_cues[index + 1:index + 6]]
            delays = (5, 15, 45)
            for attempt, delay in enumerate(delays, start=1):
                started = time.monotonic()
                translated = lingarr_translate_line(
                    cue.text,
                    source_lang,
                    target_lang,
                    before,
                    after,
                    repair_label=label,
                    cue_number=cue.number,
                    attempt=attempt,
                ) or ""
                repair_elapsed += time.monotonic() - started
                repair_attempts += 1
                if translated.strip():
                    break
                if attempt < len(delays) and not shutdown_requested:
                    time.sleep(delay)
            if translated.strip():
                repaired_cues += 1
        if not translated.strip():
            unresolved.append(cue.number)
            continue
        recovered.append(
            SubtitleCue(cue.number, cue.timestamp, translated.strip().splitlines())
        )

    if unresolved:
        print(
            f"{YELLOW}[RECOVER] {label} job {job_id}: unresolved cue(s) "
            f"{','.join(map(str, unresolved[:20]))}"
            f"{'...' if len(unresolved) > 20 else ''}{RESET}"
        )
        return {
            "recovered": False,
            "reason": "unresolved cues",
            "unresolvedCues": unresolved,
            "attempts": repair_attempts,
        }

    destination = Path(target_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{destination.name}.",
            suffix=".recovering",
            dir=destination.parent,
            delete=False,
        ) as handle:
            handle.write(render_srt_cues(recovered))
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        os.replace(temp_path, destination)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass

    if repair_attempts and repair_elapsed > 0:
        try:
            _get_validation_state().record_timing_sample(
                kind="repair",
                source_language=source_lang,
                target_language=target_lang,
                cue_count=max(1, repaired_cues),
                elapsed_seconds=repair_elapsed,
                outcome="accepted",
                lingarr_job_id=job_id,
                attempts=repair_attempts,
            )
        except StateStoreError as exc:
            print(f"{YELLOW}[TIMING] Could not persist repair timing: {exc}{RESET}")
    print(
        f"{GREEN}[RECOVER] Reconstructed {label} from Lingarr job {job_id}; "
        f"repaired {repair_attempts} cue attempt(s){RESET}"
    )
    return {
        "recovered": True,
        "path": str(destination),
        "attempts": repair_attempts,
        "repairedCues": repaired_cues,
        "repairElapsedSeconds": round(repair_elapsed, 3),
        "eventMessages": [
            str(event.get("message"))
            for event in detail.get("events", [])
            if isinstance(event, dict) and event.get("message")
        ][-5:],
    }

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


def _cycle_quarantine_suppression(
    video_path: str | Path,
    target_lang: str,
) -> dict | None:
    identity = _quarantine_identity(target_lang, video_path=video_path)
    return _cycle_suppressions.get(identity)


def _resolve_quarantine_history(
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
    return _get_validation_state().resolve_quarantine_events(identity)


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
    from .subtitles.core import evaluate_subtitle_completeness
    return evaluate_subtitle_completeness(
        subtitle_path, media_duration, **_completeness_kwargs()
    )


def _add_completeness_issue(report, completeness) -> None:
    if completeness is None:
        return
    from .subtitles.core import completeness_issue
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


def _count_srt_cues(path: str) -> int | None:
    try:
        from .subtitles.core import parse_srt_cues, read_text_best_effort

        raw = read_text_best_effort(Path(path))
        if raw is None:
            return None
        cues, _errors = parse_srt_cues(raw)
        return len(cues)
    except (OSError, ValueError):
        return None


def _timing_estimate(kind: str, source_lang: str | None, target_lang: str) -> dict:
    try:
        return _get_validation_state().timing_estimate(
            kind=kind,
            source_language=source_lang,
            target_language=target_lang,
            cold_seconds_per_cue=TRANSLATION_COLD_SECONDS_PER_CUE,
            alpha=TRANSLATION_TIMING_ALPHA,
        )
    except StateStoreError as exc:
        print(f"{YELLOW}[TIMING] Using cold estimate; state unavailable: {exc}{RESET}")
        return {
            "secondsPerCue": TRANSLATION_COLD_SECONDS_PER_CUE,
            "sampleCount": 0,
            "scope": "cold_start",
        }


def _estimate_timeout(source_path: str, source_lang: str, target_lang: str) -> dict:
    cue_count = _count_srt_cues(source_path)
    if cue_count is None:
        cue_count = _count_dialogue_lines(source_path) or 0
    learned = _timing_estimate("file", source_lang, target_lang)
    base = cue_count * learned["secondsPerCue"]
    timeout = min(
        max(POLL_TIMEOUT, int(base * TRANSLATION_TIMEOUT_MULTIPLIER)),
        TRANSLATION_TIMEOUT_CAP,
    )
    estimate = {
        **learned,
        "cueCount": cue_count,
        "estimatedSeconds": round(base, 3),
        "timeoutSeconds": timeout,
        "lane": "long" if base > LONG_JOB_THRESHOLD else "short",
    }
    print(
        f"[TIMING] Source has {cue_count} cues; "
        f"{learned['secondsPerCue']:.3f}s/cue ({learned['scope']}, "
        f"{learned['sampleCount']} samples) - estimate ~{int(base)}s, "
        f"timeout {timeout}s, lane {estimate['lane']}"
    )
    return estimate


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
        from .subtitles.core import file_sha256
        return file_sha256(path)
    except OSError as e:
        dbg(f"Could not hash {path}: {e}")
        return None


def _media_identity_for_video(video_path: str | Path) -> str:
    video = Path(video_path)
    return os.path.normcase(os.path.abspath(str(video.with_suffix(""))))


def _record_successful_source_readiness(
    source_path: str | Path,
    source_language: str,
    target_path: str | Path,
    target_language: str,
    media_duration: float | None = None,
) -> bool:
    video = _find_sidecar_video(source_path)
    source_hash = _file_hash_or_none(source_path)
    if video is None or source_hash is None:
        return False
    if media_duration is None:
        media_duration = _probe_media_duration(video)
    target_hash = _file_hash_or_none(target_path)
    artifact = (
        _get_validation_state().latest_artifact(target_path, target_hash)
        if target_hash is not None else None
    )
    try:
        _get_validation_state().record_source_readiness(
            media_identity=_media_identity_for_video(video),
            video_path=video,
            source_path=source_path,
            source_language=source_language,
            source_hash=source_hash,
            media_duration_seconds=media_duration,
            target_artifact_id=(int(artifact["id"]) if artifact else None),
            target_language=target_language,
        )
        return True
    except (OSError, StateStoreError) as exc:
        print(f"{YELLOW}[SOURCE] Could not persist successful-use evidence: {exc}{RESET}")
        return False


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
        from .subtitles.core import VALIDATOR_VERSION

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
                    _resolve_quarantine_history(language, target_path=target_path)
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


def _record_quarantine_event(
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
    entry, repeated = _get_validation_state().record_quarantine_event(
        identity,
        target_path=target_path,
        target_hash=target_hash,
        target_language=target_lang,
        rules=(issue.rule for issue in report.issues),
        origin=origin,
    )
    if repeated:
        print(
            f"[CLEANUP] Repeat offender hash for {os.path.basename(str(target_path))}; "
            f"historical occurrence {entry['occurrences']}"
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
    item_type: str | None = None,
    item_id: int | None = None,
    donor_history: list[dict] | None = None,
    candidate_raw: str | None = None,
    partial_candidate_id: int | None = None,
    dry_run: bool = False,
) -> str:
    from .subtitles.core import (
        quarantine_destination,
        quarantine_subtitle,
        write_validation_report,
    )

    target = Path(target_path)
    source_hash = _file_hash_or_none(source_path)
    candidate_temp: Path | None = None
    if candidate_raw is not None:
        candidate_temp = _write_recovery_candidate(target, candidate_raw)
    target_hash = _file_hash_or_none(candidate_temp or target)
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
            input_destination = None
            pending_metadata = {
                "rules": [issue.rule for issue in report.issues],
                "holdIdentity": _quarantine_identity(target_lang, target_path=target),
                "phase": "intent",
                "audit": audit,
            }
            if candidate_temp is not None:
                input_destination = destination.with_name(
                    f"{destination.stem}.input{destination.suffix}"
                )
                counter = 1
                while input_destination.exists():
                    input_destination = destination.with_name(
                        f"{destination.stem}.input.{counter}{destination.suffix}"
                    )
                    counter += 1
                pending_metadata.update({
                    "candidatePath": str(candidate_temp),
                    "candidateHash": target_hash,
                    "inputDestination": str(input_destination),
                })
            attempt_payload = None
            if (
                item_type in ("episodes", "movies")
                and item_id is not None
                and source_hash is not None
            ):
                from .subtitles.core import source_cue_signatures
                state_store = _get_validation_state()
                active_plan = state_store.active_retry_plan(
                    item_type, item_id, target_lang
                )
                attempt_payload = {
                    "item_type": item_type,
                    "item_id": item_id,
                    "target_language": target_lang,
                    "source_hash": source_hash,
                    "target_hash": target_hash,
                    "attempt_number": (
                        int(active_plan["attemptCount"]) + 1
                        if active_plan else 1
                    ),
                    "artifact_path": str(destination),
                    "report_path": f"{destination}.validation.json",
                    "failure_rules": [issue.rule for issue in report.issues],
                    "cue_signatures": source_cue_signatures(source_path),
                    "repair_provenance": attempt_history or [],
                    "donor_provenance": donor_history or [],
                }
                pending_metadata["quarantineAttempt"] = attempt_payload
                pending_metadata["partialCandidateId"] = partial_candidate_id
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
                    pending_metadata=pending_metadata,
                )
            else:
                artifact_id = int(artifact["id"])
                _get_validation_state().set_artifact_disposition(
                    artifact_id,
                    "quarantine_pending",
                    pending_destination=destination,
                    pending_metadata=pending_metadata,
                )
            if candidate_temp is not None:
                candidate_to_move = candidate_temp
                # The persisted operation owns this temporary file now; the
                # local cleanup path must not delete it after an interruption.
                candidate_temp = None
                # Remove the invalid published sidecar first. If the process
                # stops after this move, startup reconciliation can finish the
                # candidate move without republishing the invalid input.
                quarantine_subtitle(
                    target,
                    CLEANUP_ROOTS,
                    CLEANUP_QUARANTINE_DIR,
                    destination=input_destination,
                    access_coordinator=_artifact_access,
                )
                pending_metadata["phase"] = "input_archived"
                _get_validation_state().set_artifact_disposition(
                    artifact_id, "quarantine_pending",
                    pending_destination=destination,
                    pending_metadata=pending_metadata,
                )
                destination = quarantine_subtitle(
                    candidate_to_move, CLEANUP_ROOTS, CLEANUP_QUARANTINE_DIR,
                    destination=destination,
                    access_coordinator=_artifact_access,
                )
                audit["supersededInputArtifact"] = input_destination.name
                audit["partialCandidate"] = True
            else:
                destination = quarantine_subtitle(
                    target, CLEANUP_ROOTS, CLEANUP_QUARANTINE_DIR,
                    destination=destination,
                    access_coordinator=_artifact_access,
                )
            if _file_hash_or_none(destination) != target_hash:
                raise StateStoreError(
                    "quarantine destination hash does not match persisted intent"
                )
            pending_metadata["phase"] = "candidate_archived"
            pending_metadata["audit"] = audit
            _get_validation_state().set_artifact_disposition(
                artifact_id, "quarantine_pending",
                pending_destination=destination,
                pending_metadata=pending_metadata,
            )
            hold_identity = _quarantine_identity(
                target_lang, target_path=target
            )
            if hold_identity is not None:
                event, repeated, pending_metadata = (
                    _get_validation_state().record_pending_quarantine_hold(
                        artifact_id,
                        identity=hold_identity,
                        target_path=target,
                        target_hash=target_hash,
                        target_language=target_lang,
                        rules=(issue.rule for issue in report.issues),
                        origin=origin,
                    )
                )
            else:
                event, repeated = None, False
            suppression = _cycle_suppressions.suppress(
                _quarantine_identity(target_lang, target_path=target),
                action="quarantined",
            )
            setattr(report, "repeat_offender", repeated)
            if event is not None:
                audit["quarantineEvent"] = event
                audit["repeatOffender"] = repeated
                if repeated:
                    print(
                        f"[CLEANUP] Repeat offender hash for "
                        f"{os.path.basename(str(target))}; historical occurrence "
                        f"{event['occurrences']}"
                    )
            if suppression is not None:
                audit["cycleSuppression"] = suppression
            report_path = write_validation_report(destination, audit)
            pending_metadata["phase"] = "report_written"
            pending_metadata["audit"] = audit
            if attempt_payload is not None:
                attempt_payload["report_path"] = str(report_path)
                pending_metadata["quarantineAttempt"] = attempt_payload
            _get_validation_state().set_artifact_disposition(
                artifact_id, "quarantine_pending",
                pending_destination=destination,
                pending_metadata=pending_metadata,
            )
            _get_validation_state().finalize_quarantine_operation(
                artifact_id,
                attempt=attempt_payload,
                partial_candidate_id=partial_candidate_id,
            )
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
        finally:
            if candidate_temp is not None:
                try:
                    candidate_temp.unlink()
                except OSError:
                    pass

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
                    "holdIdentity": _quarantine_identity(
                        target_lang, target_path=target
                    ),
                },
            )
        target.unlink()
        _get_validation_state().set_artifact_disposition(artifact_id, "deleted")
        _record_quarantine_event(target, target_lang, target_hash, report, origin)
        _cycle_suppressions.suppress(
            _quarantine_identity(target_lang, target_path=target),
            action="deleted",
        )
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


def _target_repair_lock(target_path: str | Path):
    return _artifact_access.hold(target_path)


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
    from .subtitles.core import normalize_managed_file

    try:
        normalize_managed_file(path)
        return True
    except OSError as exc:
        print(
            f"{RED}[ERROR] Could not set managed ownership for {label}: {exc}{RESET}"
        )
        return False


def _replace_managed_file(candidate: str | Path, target: str | Path) -> None:
    from .subtitles.core import normalize_managed_file

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
    series_key: str | None = None,
    series_title: str | None = None,
    status_ref: dict | None = None,
    cancellation_requested=None,
    publication_guard=None,
    trial_owner: str | None = None,
    trial_job_id: int | None = None,
    trial_plan_id: int | None = None,
    trial_generation: int | None = None,
) -> RepairJobResult:
    from .subtitles.core import repair_subtitle_file, target_language_for_code

    label = title or os.path.basename(target_path)
    publication_admitted = False

    def admit_publication() -> bool:
        nonlocal publication_admitted
        if publication_admitted:
            return True
        if cancellation_requested is not None and cancellation_requested():
            return False
        if publication_guard is not None and not publication_guard():
            return False
        publication_admitted = True
        return True
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
            from .subtitles.core import read_text_best_effort
            recovery_raw = read_text_best_effort(Path(target_path))
            if recovery_raw is None:
                return RepairJobResult(
                    "repair-deferred", initial_report, label, target_lang,
                    item_type, item_id, target_path=str(target_path),
                )
        recovery_temp = _write_recovery_candidate(target_path, recovery_raw)
        working_path = recovery_temp

        attempt_state: dict = {}
        progress_started = time.monotonic()
        last_progress_signature: tuple | None = None
        last_progress_at = 0.0

        def attempt_logger(event: dict) -> None:
            attempt_state.clear()
            attempt_state.update(event)
            if (
                event.get("event") == "rejected"
                and event.get("outputFingerprint")
                and event.get("sourceCueHash")
                and item_type in ("episodes", "movies")
                and item_id is not None
                and expected_source_hash
            ):
                try:
                    _get_validation_state().record_failure_fingerprint(
                        item_type=item_type,
                        item_id=item_id,
                        target_language=target_lang,
                        source_file_hash=expected_source_hash,
                        source_cue_hash=event["sourceCueHash"],
                        strategy_key=event.get("strategy") or "unknown",
                        provider="lingarr",
                        config_fingerprint=_VALIDATION_CONFIG_FINGERPRINT,
                        output_fingerprint=event["outputFingerprint"],
                        failure_class=",".join(event.get("validationRules") or ["validation"]),
                    )
                except StateStoreError as exc:
                    print(f"{YELLOW}[REPAIR] Could not persist failure fingerprint: {exc}{RESET}")
            if event["event"] == "donor_accepted":
                print(
                    f"[DONOR] Cue {event.get('cueNumber')} recovered from "
                    f"quarantine attempt {event.get('sourceAttempt')}"
                )
                return
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

        def progress_callback(event: dict) -> None:
            nonlocal last_progress_signature, last_progress_at
            stage = event.get("stage")
            state = "repair_validating" if stage == "repair_validating" else "repairing"
            completed = int(event.get("completedCues") or 0)
            total = int(event.get("totalRepairableCues") or 0)
            elapsed = max(0.001, time.monotonic() - progress_started)
            eta = (
                round((elapsed / completed) * max(0, total - completed), 1)
                if completed else None
            )
            details = {
                key: value for key, value in event.items()
                if key in {
                    "totalRepairableCues", "completedCues", "currentCueNumber",
                    "currentCuePosition", "currentCueOrdinal", "currentAttempt",
                    "maxAttempts", "contextEnabled", "lastHttpStatus",
                    "lastRequestDurationSeconds", "rejectedAttempts",
                    "successfulCues", "unresolvedCues", "progress",
                }
            }
            details.update({
                "repairStage": stage,
                "attempts": event.get("currentAttempt"),
            })
            if eta is not None:
                details["etaSeconds"] = eta
                details["estimatedSeconds"] = round(elapsed + eta, 1)
            signature = (
                state, stage, event.get("currentCueOrdinal"),
                event.get("currentAttempt"), completed,
                event.get("rejectedAttempts"), event.get("unresolvedCues"),
            )
            now = time.monotonic()
            if signature == last_progress_signature and now - last_progress_at < 0.5:
                return
            last_progress_signature = signature
            last_progress_at = now
            _status_ref_transition(status_ref, state, details=details)

        def translator(line: str, before: list[str], after: list[str]):
            if cancellation_requested is not None and cancellation_requested():
                return None, {"cancelled": True}
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
                strict=attempt_state.get("strategy") == "strict_isolated",
                cancellation_requested=cancellation_requested,
            )
            return translated, outcome_meta

        def donor_event_logger(event: dict) -> None:
            if item_type not in ("episodes", "movies") or item_id is None:
                return
            try:
                _get_validation_state().record_donor_event(
                    item_type=item_type,
                    item_id=item_id,
                    target_language=target_lang,
                    cue_number=event.get("cueNumber"),
                    donor_attempt_id=event.get("donorAttemptId"),
                    reason_code=event.get("reasonCode") or "current_validation_failed",
                )
            except StateStoreError as exc:
                print(f"{YELLOW}[DONOR] Could not persist donor diagnostic: {exc}{RESET}")

        cue_list = ", ".join(str(i + 1) for i in initial_report.repairable_cue_indexes)
        print(f"[REPAIR] Retrying {label} '{target_lang}' cue position(s): {cue_list}")
        try:
            donor_attempts = []
            cue_recoveries = []
            exhausted_strategies = {}
            if (
                DONOR_RECOVERY_ENABLED
                and item_type in ("episodes", "movies")
                and item_id is not None
            ):
                donor_attempts = _get_validation_state().quarantine_attempts(
                    item_type, item_id, target_lang
                )
                cue_recoveries = _get_validation_state().cue_recoveries(
                    item_type,
                    item_id,
                    target_lang,
                    source_file_hash=expected_source_hash,
                )
                if expected_source_hash and hasattr(
                    _get_validation_state(), "exhausted_recovery_strategies"
                ):
                    exhausted_strategies = (
                        _get_validation_state().exhausted_recovery_strategies(
                            item_type=item_type,
                            item_id=item_id,
                            target_language=target_lang,
                            source_file_hash=expected_source_hash,
                            provider="lingarr",
                            config_fingerprint=_VALIDATION_CONFIG_FINGERPRINT,
                        )
                    )
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
                progress_callback=progress_callback,
                donor_attempts=donor_attempts,
                cue_recoveries=cue_recoveries,
                donor_event_logger=donor_event_logger,
                artifact_access=_artifact_access,
                exhausted_strategies=exhausted_strategies,
                cancellation_requested=lambda: (
                    cancellation_requested is not None and cancellation_requested()
                ) if cancellation_requested is not None else None,
                **_validation_kwargs(),
            )
            second_attempts = sum(
                entry.get("attempt", 0) > 1 and entry.get("withoutContext")
                for entry in repair.attempt_history
            )
            if repair.interrupted or (
                cancellation_requested is not None and cancellation_requested()
            ):
                print(
                    f"[REPAIR] Persisted {label} '{target_lang}' for restart "
                    "after shutdown interruption"
                )
                return RepairJobResult(
                    "repair-deferred", repair.report, label, target_lang,
                    item_type, item_id, repair.attempts, second_attempts,
                    str(target_path),
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
                if not admit_publication():
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
                    donorRecovery=repair.donor_history,
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
                    (
                        repair.donor_history[0].get("sourceAttempt")
                        if repair.donor_history else None
                    ),
                )

            print(f"{YELLOW}[REPAIR] Could not repair {label} '{target_lang}': {repair.reason}{RESET}")
            if repair.manual_review:
                setattr(repair.report, "manual_review", True)
            partial_id = None
            if (
                repair.partial_raw
                and repair.repaired_cues
                and item_type in ("episodes", "movies")
                and item_id is not None
                and expected_source_hash
            ):
                try:
                    from .subtitles.core import (
                        cue_source_signature,
                        parse_srt_cues,
                        read_text_best_effort,
                    )
                    source_raw = read_text_best_effort(Path(source_path)) or ""
                    source_cues, source_errors = parse_srt_cues(source_raw)
                    partial_cues, partial_errors = parse_srt_cues(repair.partial_raw)
                    partial_hash = hashlib.sha256(
                        repair.partial_raw.encode("utf-8")
                    ).hexdigest()
                    if not source_errors and not partial_errors:
                        partial_id = _get_validation_state().record_partial_candidate(
                            item_type=item_type,
                            item_id=item_id,
                            source_language=source_lang,
                            target_language=target_lang,
                            source_hash=expected_source_hash,
                            target_hash=partial_hash,
                            changed_cues=repair.repaired_cues,
                            unresolved_cues=repair.unresolved_cues,
                            provenance=repair.attempt_history + repair.donor_history,
                            artifact_path=None,
                        )
                        changed = set(repair.repaired_cues)
                        by_number = {cue.number: cue for cue in partial_cues}
                        for source_cue in source_cues:
                            target_cue = by_number.get(source_cue.number)
                            if source_cue.number not in changed or target_cue is None:
                                continue
                            signature = cue_source_signature(source_cue)
                            target_text = target_cue.text
                            _get_validation_state().record_cue_recovery(
                                partial_candidate_id=partial_id,
                                item_type=item_type,
                                item_id=item_id,
                                source_language=source_lang,
                                target_language=target_lang,
                                source_file_hash=expected_source_hash,
                                source_cue_number=source_cue.number,
                                source_cue_hash=signature["sourceHash"],
                                source_signature=signature,
                                cue_start_ms=signature.get("startMs"),
                                target_text=target_text,
                                target_hash=hashlib.sha256(target_text.encode("utf-8")).hexdigest(),
                                recovery_stage="cue_repair",
                            )
                except (OSError, StateStoreError) as exc:
                    print(f"{YELLOW}[REPAIR] Could not persist partial progress: {exc}{RESET}")
            if not admit_publication():
                return RepairJobResult(
                    "repair-deferred", repair.report, label, target_lang,
                    item_type, item_id, repair.attempts, second_attempts,
                    str(target_path),
                )
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
                item_type=item_type,
                item_id=item_id,
                donor_history=repair.donor_history,
                candidate_raw=(
                    repair.partial_raw if CLEANUP_ACTION == "quarantine" else None
                ),
                partial_candidate_id=partial_id,
            )
            if action in ("quarantined", "deleted") and item_id is not None:
                _clear_submission(item_id, target_lang, item_type)
                _clear_submission_for_path(target_path, target_lang)
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


def _get_repair_executor() -> _DaemonRepairExecutor:
    global _repair_executor
    with _repair_executor_lock:
        if _repair_executor is None:
            if shutdown_requested:
                raise RuntimeError("repair admission stopped during shutdown")
            _repair_shutdown_event.clear()
            _repair_executor = _DaemonRepairExecutor(
                max_workers=PARALLEL_TRANSLATES,
                thread_name_prefix="repair-worker",
            )
        return _repair_executor


def _run_repair_with_capacity(
    capacity_token: int,
    job_kwargs: dict,
    metadata: dict,
) -> RepairJobResult:
    """Run one repair after its priority reservation obtains shared capacity."""
    status_ref = metadata.get("status_ref")
    durable_job_id = metadata.get("durable_job_id")
    _status_ref_transition(
        status_ref,
        "repair_waiting_capacity",
        details={"repairStage": "waiting_capacity"},
    )
    if not _shared_capacity.start_repair(capacity_token):
        _shared_capacity.release(capacity_token)
        if durable_job_id is not None:
            try:
                _get_validation_state().transition_repair_job(
                    durable_job_id,
                    "persisted_for_restart",
                    shutdown_classification="cancelled_before_start",
                    expected_states=("queued", "persisted_for_restart"),
                )
            except StateStoreError:
                pass
        return RepairJobResult(
            "repair-deferred",
            job_kwargs.get("initial_report"),
            job_kwargs.get("title") or "",
            job_kwargs.get("target_lang") or "",
            job_kwargs.get("item_type"),
            job_kwargs.get("item_id"),
            target_path=str(job_kwargs.get("target_path") or ""),
        )
    try:
        if durable_job_id is not None:
            try:
                _get_validation_state().transition_repair_job(
                    durable_job_id,
                    "active",
                    lease_owner=f"worker:{threading.current_thread().name}",
                    lease_expires_at=time.time() + REPAIR_SHUTDOWN_GRACE_SECONDS,
                    expected_states=("queued", "persisted_for_restart"),
                )
            except StateStoreError as exc:
                print(f"{YELLOW}[REPAIR] Durable lease update failed: {exc}{RESET}")
        _status_ref_transition(
            status_ref, "repairing", details={"repairStage": "starting"}
        )
        repair_kwargs = dict(job_kwargs)
        repair_kwargs.pop("maintenance_scan_job_id", None)
        repair_kwargs["cancellation_requested"] = _repair_shutdown_event.is_set

        def begin_publication() -> bool:
            with metadata["publication_lock"]:
                if _repair_shutdown_event.is_set():
                    return False
                metadata["publication_started"] = True
                return True

        return _perform_repair(
            **repair_kwargs,
            status_ref=status_ref,
            publication_guard=begin_publication,
        )
    finally:
        _shared_capacity.release(capacity_token)


def _publish_repair_status(future: Future, metadata: dict) -> None:
    """Publish a repair's terminal dashboard state without draining its future."""
    status_lock = metadata.get("status_lock")
    if status_lock is not None:
        with status_lock:
            if metadata.get("status_published"):
                return
            metadata["status_published"] = True
    try:
        result = future.result()
    except CancelledError:
        _complete_repair_status(
            metadata,
            "deferred",
            reason="repair persisted for restart during shutdown",
        )
        _scan_child_finished(metadata.get("maintenance_scan_job_id"), "deferred")
        return
    except Exception:
        durable_job_id = metadata.get("durable_job_id")
        if durable_job_id is not None and not metadata.get("shutdown_classification"):
            try:
                _get_validation_state().transition_repair_job(
                    durable_job_id, "failed", error_code="worker_exception",
                    expected_states=("queued", "active"),
                )
            except StateStoreError:
                pass
        _complete_repair_status(
            metadata,
            "failed",
            reason="repair worker failed",
        )
        _scan_child_finished(metadata.get("maintenance_scan_job_id"), "failed")
        return

    durable_job_id = metadata.get("durable_job_id")
    if durable_job_id is not None:
        try:
            if result.action == "repair-deferred":
                _get_validation_state().transition_repair_job(
                    durable_job_id,
                    "persisted_for_restart",
                    shutdown_classification="deferred",
                    expected_states=("queued", "active"),
                )
            else:
                _get_validation_state().transition_repair_job(
                    durable_job_id,
                    "completed" if result.action in ("repaired", "quarantined", "deleted") else "failed",
                    error_code=(None if result.action in ("repaired", "quarantined", "deleted") else result.action),
                    expected_states=("queued", "active"),
                )
        except StateStoreError as exc:
            print(f"{YELLOW}[REPAIR] Durable completion update failed: {exc}{RESET}")

    if result.action in ("repaired", "quarantined", "deleted"):
        series_key = metadata.get("series_key")
        series_title = metadata.get("series_title")
        if series_key and series_title:
            try:
                _get_validation_state().record_circuit_outcome(
                    series_key=series_key,
                    series_title=series_title,
                    success=result.action == "repaired",
                    reason=(
                        None
                        if result.action == "repaired"
                        else f"invalid subtitle {result.action}"
                    ),
                    threshold=CIRCUIT_FAILURE_THRESHOLD,
                    open_cycles=CIRCUIT_OPEN_CYCLES,
                    config_fingerprint=_CIRCUIT_CONFIG_FINGERPRINT,
                    trial_owner=metadata.get("trial_owner"),
                    trial_job_id=metadata.get("trial_job_id"),
                    trial_plan_id=metadata.get("trial_plan_id"),
                    lease_generation=metadata.get("trial_generation"),
                )
                _refresh_status_diagnostics()
            except StateStoreError as exc:
                print(f"{YELLOW}[CIRCUIT] Could not record repair outcome: {exc}{RESET}")

    if result.action == "repaired":
        source_path = metadata.get("source_path")
        source_language = metadata.get("source_lang")
        if source_path and source_language:
            _record_successful_source_readiness(
                source_path,
                source_language,
                result.target_path,
                result.target_lang,
            )
        _resolve_retry_success(
            result.item_type,
            result.item_id,
            result.target_lang,
            outcome=(
                "accepted_after_donor_recovery"
                if result.donor_source_attempt is not None
                else "accepted_after_retry"
            ),
        )
        _complete_repair_status(
            metadata,
            "repaired",
            repaired=True,
            details={"attempts": result.attempts},
        )
    elif result.action in ("quarantined", "deleted"):
        _schedule_validation_retry(
            report=result.report,
            action=result.action,
            source_path=metadata.get("source_path") or "",
            source_lang=metadata.get("source_lang") or "",
            target_path=result.target_path,
            target_lang=result.target_lang,
            item_type=result.item_type,
            item_id=result.item_id,
            title=result.title,
            series_key=metadata.get("series_key"),
            series_title=metadata.get("series_title"),
        )
        _complete_repair_status(
            metadata,
            result.action,
            reason=result.action,
            details={"attempts": result.attempts},
        )
    elif result.action == "repair-deferred":
        _schedule_validation_retry(
            report=result.report,
            action=result.action,
            source_path=metadata.get("source_path") or "",
            source_lang=metadata.get("source_lang") or "",
            target_path=result.target_path,
            target_lang=result.target_lang,
            item_type=result.item_type,
            item_id=result.item_id,
            title=result.title,
            series_key=metadata.get("series_key"),
            series_title=metadata.get("series_title"),
        )
        _complete_repair_status(
            metadata,
            "deferred",
            reason="repair deferred",
            details={"attempts": result.attempts},
        )
    else:
        _complete_repair_status(
            metadata,
            "failed",
            reason=f"repair {result.action}",
            details={"attempts": result.attempts},
        )
    _scan_child_finished(
        metadata.get("maintenance_scan_job_id"),
        (
            "repaired"
            if result.action == "repaired"
            else (
                result.action
                if result.action in ("quarantined", "deleted")
                else "failed"
            )
        ),
    )


def _queue_repair(repair_key: tuple, job_kwargs: dict, report, label: str, target_lang: str) -> str:
    with _pending_repairs_lock:
        if repair_key in _repair_keys:
            print(f"[REPAIR] Duplicate repair suppressed for {label} '{target_lang}'")
            return "repair-duplicate"
        cue_count = len(getattr(report, "repairable_cue_indexes", []) or [])
        repair_timing = _timing_estimate(
            "repair", job_kwargs.get("source_lang"), target_lang
        )
        initial_details = {
            "totalRepairableCues": cue_count,
            "completedCues": 0,
            "successfulCues": 0,
            "unresolvedCues": 0,
            "rejectedAttempts": 0,
            "progress": 0,
            "secondsPerCue": round(repair_timing["secondsPerCue"], 4),
            "timingSampleCount": repair_timing["sampleCount"],
            "timingScope": repair_timing["scope"],
            "estimatedSeconds": round(
                cue_count
                * repair_timing["secondsPerCue"]
                * REPAIR_TIMEOUT_MULTIPLIER,
                1,
            ),
            "etaSeconds": round(
                cue_count
                * repair_timing["secondsPerCue"]
                * REPAIR_TIMEOUT_MULTIPLIER,
                1,
            ),
            "lane": "repair",
            "attempts": 0,
            "maxAttempts": CLEANUP_MAX_REPAIR_ATTEMPTS,
            "repairStage": "queued",
        }
        status_ref = _status_create_repair_ref(
            job_kwargs, label, target_lang, initial_details
        )
        if not _repair_capacity.acquire(blocking=False):
            print(f"[REPAIR] Queue full; deferred {label} '{target_lang}' to the next scan")
            _status_ref_complete(
                status_ref, "deferred", reason="repair queue full"
            )
            return "repair-deferred"
        durable_job_id = None
        state = _get_validation_state()
        if hasattr(state, "enqueue_repair_job"):
            try:
                durable_key = hashlib.sha256(
                    repr((
                        job_kwargs.get("item_type"), job_kwargs.get("item_id"),
                        target_lang, job_kwargs.get("expected_source_hash"),
                        job_kwargs.get("expected_target_hash"),
                        tuple(getattr(report, "repairable_cue_indexes", []) or []),
                    )).encode("utf-8")
                ).hexdigest()
                durable_job_id = state.enqueue_repair_job(
                    dedupe_key=durable_key,
                    item_type=job_kwargs.get("item_type"),
                    item_id=job_kwargs.get("item_id"),
                    target_language=target_lang,
                    source_path=job_kwargs.get("source_path"),
                    target_path=job_kwargs.get("target_path"),
                    source_hash=job_kwargs.get("expected_source_hash"),
                    target_hash=job_kwargs.get("expected_target_hash"),
                    cue_indexes=getattr(report, "repairable_cue_indexes", []) or [],
                    payload={
                        "origin": job_kwargs.get("origin"),
                        "sourceLanguage": job_kwargs.get("source_lang"),
                        "title": job_kwargs.get("title"),
                        "seriesKey": job_kwargs.get("series_key"),
                        "seriesTitle": job_kwargs.get("series_title"),
                        "trialOwner": job_kwargs.get("trial_owner"),
                        "trialJobId": job_kwargs.get("trial_job_id"),
                        "trialPlanId": job_kwargs.get("trial_plan_id"),
                        "trialGeneration": job_kwargs.get("trial_generation"),
                    },
                )
            except StateStoreError as exc:
                _repair_capacity.release()
                _status_ref_complete(
                    status_ref, "deferred", reason="repair persistence unavailable"
                )
                print(f"{YELLOW}[REPAIR] Could not persist repair before submit: {exc}{RESET}")
                return "repair-deferred"
        metadata = {
            "key": repair_key,
            "report": report,
            "target_path": job_kwargs.get("target_path"),
            "item_type": job_kwargs.get("item_type"),
            "item_id": job_kwargs.get("item_id"),
            "target_lang": job_kwargs.get("target_lang"),
            "source_lang": job_kwargs.get("source_lang"),
            "source_path": job_kwargs.get("source_path"),
            "series_key": job_kwargs.get("series_key"),
            "series_title": job_kwargs.get("series_title"),
            "trial_owner": job_kwargs.get("trial_owner"),
            "trial_job_id": job_kwargs.get("trial_job_id"),
            "trial_plan_id": job_kwargs.get("trial_plan_id"),
            "trial_generation": job_kwargs.get("trial_generation"),
            "queued_monotonic": time.monotonic(),
            "status_ref": status_ref,
            "maintenance_scan_job_id": job_kwargs.get("maintenance_scan_job_id"),
            "status_lock": threading.Lock(),
            "status_published": False,
            "durable_job_id": durable_job_id,
            "publication_lock": threading.Lock(),
            "publication_started": False,
        }
        _repair_keys.add(repair_key)
        capacity_token = _shared_capacity.reserve_repair()
        try:
            future = _get_repair_executor().submit(
                _run_repair_with_capacity,
                capacity_token,
                job_kwargs,
                metadata,
            )
        except Exception:
            _repair_keys.discard(repair_key)
            _shared_capacity.release(capacity_token)
            _repair_capacity.release()
            _status_ref_complete(
                status_ref, "failed", reason="repair worker submission failed"
            )
            raise
        _pending_repairs[future] = metadata
        _scan_child_queued(metadata.get("maintenance_scan_job_id"))
    future.add_done_callback(
        lambda completed, repair_metadata=metadata: _publish_repair_status(
            completed, repair_metadata
        )
    )
    for index in report.repairable_cue_indexes:
        print(f"[REPAIR] Queued {label} '{target_lang}' cue position {index + 1}")
    return "repair-queued"


def _drain_pending_repairs(stats: dict) -> list[RepairJobResult]:
    stats.setdefault("completed", 0)
    stats.setdefault("failed", 0)
    stats.setdefault("translations", [])
    stats.setdefault("episode_activity", False)
    stats.setdefault("movie_activity", False)
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
        if result.action == "repaired":
            elapsed = max(
                0.001,
                time.monotonic() - float(metadata.get("queued_monotonic", time.monotonic())),
            )
            try:
                _get_validation_state().record_timing_sample(
                    kind="repair",
                    source_language=metadata.get("source_lang"),
                    target_language=result.target_lang,
                    cue_count=max(
                        1,
                        len(
                            getattr(
                                metadata.get("report"),
                                "repairable_cue_indexes",
                                [],
                            )
                            or []
                        ),
                    ),
                    elapsed_seconds=elapsed,
                    outcome="accepted",
                    attempts=max(1, result.attempts),
                )
            except StateStoreError as exc:
                print(f"{YELLOW}[TIMING] Could not persist repair sample: {exc}{RESET}")
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
        _repair_shutdown_event.set()
        print(
            f"[REPAIR] Draining active repair worker(s) for up to "
            f"{REPAIR_SHUTDOWN_GRACE_SECONDS}s"
        )
        with _pending_repairs_lock:
            pending_metadata = list(_pending_repairs.items())
            futures = [future for future, _metadata in pending_metadata]
        for future, metadata in pending_metadata:
            if future.running() or future.done():
                continue
            # Future.cancel() invokes callbacks synchronously. Persist and
            # classify first so the callback cannot misreport cancellation as
            # a worker failure.
            metadata["shutdown_classification"] = "cancelled_before_start"
            durable_job_id = metadata.get("durable_job_id")
            if durable_job_id is not None:
                try:
                    _get_validation_state().transition_repair_job(
                        durable_job_id,
                        "persisted_for_restart",
                        shutdown_classification="cancelled_before_start",
                        expected_states=("queued",),
                    )
                except StateStoreError:
                    pass
            future.cancel()
        deadline = time.monotonic() + max(0, REPAIR_SHUTDOWN_GRACE_SECONDS)
        _done, pending = wait(
            futures,
            timeout=max(0.0, deadline - time.monotonic()),
        )
        for future, metadata in pending_metadata:
            if future not in pending:
                continue
            if future.done():
                continue
            with metadata["publication_lock"]:
                publication_started = bool(metadata.get("publication_started"))
            classification = (
                "publishing_interrupted"
                if publication_started
                else "interrupted" if future.running()
                else "cancelled_before_start"
            )
            metadata["shutdown_classification"] = classification
            durable_job_id = metadata.get("durable_job_id")
            if durable_job_id is not None:
                try:
                    _get_validation_state().transition_repair_job(
                        durable_job_id,
                        "active" if publication_started else "persisted_for_restart",
                        shutdown_classification=classification,
                        expected_states=("active",) if publication_started else ("queued", "active"),
                    )
                except StateStoreError:
                    pass
            if not publication_started:
                future.cancel()
        # Running repairs observe the cancellation event; their HTTP call is
        # isolated in a daemon request thread. Never introduce an unbounded
        # executor join after the configured drain deadline.
        executor.shutdown(wait=False, cancel_futures=True)


def _regeneration_delay_cycles(attempts: int) -> int:
    return min(
        REGENERATION_MAX_DELAY_CYCLES,
        max(
            1,
            round(
                REGENERATION_INITIAL_DELAY_CYCLES
                * (REGENERATION_BACKOFF_MULTIPLIER ** max(0, int(attempts)))
            ),
        ),
    )


def _schedule_validation_retry(
    *,
    report,
    action: str,
    source_path: str,
    source_lang: str,
    target_path: str,
    target_lang: str,
    item_type: str | None,
    item_id: int | None,
    title: str,
    series_key: str | None,
    series_title: str | None,
) -> dict | None:
    if item_type not in ("episodes", "movies") or item_id is None:
        return None
    if report is None or not hasattr(report, "valid"):
        return None
    from .subtitles.core import classify_validation_failure

    failure_class = classify_validation_failure(report)
    source_hash = _file_hash_or_none(source_path)
    if source_hash is None:
        failure_class = "source_problem"
        source_hash = f"unavailable:{item_type}:{item_id}"
    target_hash = _file_hash_or_none(target_path)
    archived_attempt = None
    try:
        state_store = _get_validation_state()
        existing_plan = (
            state_store.active_retry_plan(item_type, item_id, target_lang)
            if hasattr(state_store, "active_retry_plan") else None
        )
    except StateStoreError:
        return None
    if target_hash is None and item_type in ("episodes", "movies"):
        try:
            archived = state_store.quarantine_attempts(
                item_type, item_id, target_lang
            )
            if archived:
                archived_attempt = archived[0]
                target_hash = archived_attempt["targetHash"]
        except StateStoreError:
            pass
    if failure_class == "cue_repairable" and action == "repair-deferred":
        state = "repair_retry_queued"
        eligible_cycle = _completed_cycle
    elif failure_class == "source_problem":
        state = "source_blocked"
        eligible_cycle = _completed_cycle
    elif action in ("quarantined", "deleted"):
        attempts = int((existing_plan or {}).get("attemptCount", 0))
        if REGENERATION_MAX_ATTEMPTS > 0 and attempts >= REGENERATION_MAX_ATTEMPTS:
            state = "retry_exhausted"
            eligible_cycle = _completed_cycle
        else:
            state = "regeneration_waiting"
            delay = _regeneration_delay_cycles(attempts)
            eligible_cycle = _completed_cycle + max(1, delay)
        failure_class = "whole_file"
    else:
        return None
    try:
        if not hasattr(state_store, "schedule_retry_plan"):
            return None
        plan, repeated = state_store.schedule_retry_plan(
            item_type=item_type,
            item_id=item_id,
            target_language=target_lang,
            source_hash=source_hash,
            source_path=source_path,
            source_language=source_lang,
            target_path=target_path,
            series_key=series_key,
            series_title=series_title,
            media_title=title,
            source_cue_count=_count_srt_cues(source_path),
            failure_class=failure_class,
            rules=(issue.rule for issue in getattr(report, "issues", [])),
            state=state,
            failed_output_hash=target_hash,
            artifact_path=(
                archived_attempt.get("artifactPath")
                if archived_attempt else None
            ),
            report_path=(
                archived_attempt.get("reportPath")
                if archived_attempt else None
            ),
            eligible_completed_cycle=eligible_cycle,
            reason=getattr(report, "summary", lambda: action)(),
        )
        if getattr(report, "manual_review", False):
            plan = state_store.reschedule_retry_no_progress(
                plan["id"],
                completed_cycle=_completed_cycle,
                deferral_class="manual_review",
                reason="no materially new recovery strategy remains",
                delay_cycles=1,
            ) or plan
        print(
            f"[RETRY] {'Observed unchanged' if repeated else 'Scheduled'} "
            f"{title} '{target_lang}': state={plan['state']} "
            f"eligible_cycle={plan['eligibleCompletedCycle']} "
            f"attempt={plan['attemptCount']}/"
            f"{REGENERATION_MAX_ATTEMPTS or 'unlimited'}"
        )
        _refresh_status_diagnostics()
        return plan
    except StateStoreError as exc:
        print(f"{YELLOW}[RETRY] Could not persist retry plan: {exc}{RESET}")
        return None


def _resolve_retry_success(
    item_type: str | None,
    item_id: int | None,
    target_lang: str,
    *,
    outcome: str = "accepted_after_retry",
) -> None:
    if item_type not in ("episodes", "movies") or item_id is None:
        return
    try:
        state = _get_validation_state()
        if not hasattr(state, "resolve_retry_plans"):
            return
        resolved = state.resolve_retry_plans(
            item_type, item_id, target_lang, outcome=outcome
        )
        if resolved:
            print(
                f"[RETRY] Accepted retry for {item_type}:{item_id} "
                f"'{target_lang}'; resolved {resolved} plan(s)"
            )
            _refresh_status_diagnostics()
    except StateStoreError as exc:
        print(f"{YELLOW}[RETRY] Could not resolve retry plan: {exc}{RESET}")


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
    series_key: str | None = None,
    series_title: str | None = None,
    maintenance_scan_job_id: str | None = None,
    trial_owner: str | None = None,
    trial_job_id: int | None = None,
    trial_plan_id: int | None = None,
    trial_generation: int | None = None,
) -> tuple[str, object]:
    if target_lang not in CLEANUP_LANGUAGES:
        from .subtitles.core import validate_srt_structure

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
            item_type=item_type,
            item_id=item_id,
            dry_run=dry_run,
        )
        if action in ("quarantined", "deleted") and item_id is not None:
            _clear_submission(item_id, target_lang, item_type)
            _clear_submission_for_path(target_path, target_lang)
            print(
                f"[CLEANUP] Cleared submission cooldown for {label} "
                f"'{target_lang}'; retry suppressed for the remainder of "
                "this cycle"
            )
        _schedule_validation_retry(
            report=report,
            action=action,
            source_path=source_path,
            source_lang=source_lang,
            target_path=target_path,
            target_lang=target_lang,
            item_type=item_type,
            item_id=item_id,
            title=label,
            series_key=series_key,
            series_title=series_title,
        )
        return action, report

    from .subtitles.core import (
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
    historical_event = (
        _get_validation_state().quarantine_event(
            quarantine_identity, target_hash=expected_target_hash
        )
        if quarantine_identity is not None else None
    )
    repeat_invalid_hash = bool(
        historical_event
        and expected_target_hash
        and historical_event.get("targetHash") == expected_target_hash
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
            "series_key": series_key,
            "series_title": series_title,
            "maintenance_scan_job_id": maintenance_scan_job_id,
            "trial_owner": trial_owner,
            "trial_job_id": trial_job_id,
            "trial_plan_id": trial_plan_id,
            "trial_generation": trial_generation,
        }
        if defer_repair:
            repair_key = (
                os.path.normcase(os.path.abspath(target_path)), source_hash, expected_target_hash,
                target_lang, tuple(report.repairable_cue_indexes),
            )
            queued_action = _queue_repair(
                repair_key, job_kwargs, report, label, target_lang
            )
            if queued_action == "repair-deferred":
                _schedule_validation_retry(
                    report=report,
                    action=queued_action,
                    source_path=source_path,
                    source_lang=source_lang,
                    target_path=target_path,
                    target_lang=target_lang,
                    item_type=item_type,
                    item_id=item_id,
                    title=label,
                    series_key=series_key,
                    series_title=series_title,
                )
            return queued_action, report
        synchronous_kwargs = dict(job_kwargs)
        synchronous_kwargs.pop("maintenance_scan_job_id", None)
        for coordination_key in (
            "trial_owner", "trial_job_id", "trial_plan_id", "trial_generation"
        ):
            synchronous_kwargs.pop(coordination_key, None)
        result = _perform_repair(**synchronous_kwargs)
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
        item_type=item_type,
        item_id=item_id,
        dry_run=dry_run,
    )
    if action in ("quarantined", "deleted") and item_id is not None:
        _clear_submission(item_id, target_lang, item_type)
        _clear_submission_for_path(target_path, target_lang)
        print(
            f"[CLEANUP] Cleared submission cooldown for {label} '{target_lang}'; "
            "retry suppressed for the remainder of this cycle"
        )
    _schedule_validation_retry(
        report=report,
        action=action,
        source_path=source_path,
        source_lang=source_lang,
        target_path=target_path,
        target_lang=target_lang,
        item_type=item_type,
        item_id=item_id,
        title=label,
        series_key=series_key,
        series_title=series_title,
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


def _record_invalid_circuit_outcome(
    series_key: str,
    series_title: str,
    action: str,
    report,
    *,
    trial_owner: str | None = None,
    trial_job_id: int | None = None,
    trial_plan_id: int | None = None,
    trial_generation: int | None = None,
) -> None:
    if action not in ("quarantined", "deleted"):
        return
    rules = ",".join(issue.rule for issue in getattr(report, "issues", []))
    try:
        _get_validation_state().record_circuit_outcome(
            series_key=series_key,
            series_title=series_title,
            success=False,
            reason=f"invalid subtitle {action}: {rules}",
            threshold=CIRCUIT_FAILURE_THRESHOLD,
            open_cycles=CIRCUIT_OPEN_CYCLES,
            config_fingerprint=_CIRCUIT_CONFIG_FINGERPRINT,
            trial_owner=trial_owner,
            trial_job_id=trial_job_id,
            trial_plan_id=trial_plan_id,
            lease_generation=trial_generation,
        )
        _refresh_status_diagnostics()
    except StateStoreError as exc:
        print(f"{YELLOW}[CIRCUIT] Could not record invalid output: {exc}{RESET}")


def _record_valid_circuit_outcome(
    series_key: str, series_title: str
) -> None:
    try:
        _get_validation_state().record_circuit_outcome(
            series_key=series_key,
            series_title=series_title,
            success=True,
            reason=None,
            threshold=CIRCUIT_FAILURE_THRESHOLD,
            open_cycles=CIRCUIT_OPEN_CYCLES,
            config_fingerprint=_CIRCUIT_CONFIG_FINGERPRINT,
        )
        _refresh_status_diagnostics()
    except StateStoreError as exc:
        print(f"{YELLOW}[CIRCUIT] Could not close validated trial: {exc}{RESET}")


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
    from .subtitles.core import validate_srt_structure

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


def process_item(
    item: dict,
    item_type: str,
    id_field: str,
    stats: dict,
    stats_lock: threading.Lock,
    retry_plan: dict | None = None,
    retry_submission_callback=None,
) -> None:
    if shutdown_requested:
        return

    item_id = item.get(id_field)
    if item_id is None:
        return
    title = _item_title(item, item_type)
    identity = resolve_media_identity(item, item_type, item_id)
    series_title = identity["title"]
    series_key = identity["key"]
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
    _status_set_episode_identity(item_type, item_id, video_path)
    identity = resolve_media_identity(item, item_type, item_id, video_path)
    series_title = identity["title"]
    series_key = identity["key"]
    if item_type == "episodes" and series_key.startswith("sonarr:"):
        fallback_item = dict(item)
        fallback_item.pop("sonarrSeriesId", None)
        fallback_item.pop("sonarr_series_id", None)
        fallback_identity = resolve_media_identity(
            fallback_item, item_type, item_id, video_path
        )
        if fallback_identity["key"] != series_key:
            try:
                _get_validation_state().register_series_alias(
                    fallback_identity["key"], series_key, series_title
                )
            except StateStoreError as exc:
                print(
                    f"{YELLOW}[IDENTITY] Could not persist series alias "
                    f"for {series_title}: {exc}{RESET}"
                )
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
    if retry_plan is not None:
        retry_target = str(retry_plan.get("targetLanguage") or "").lower()
        if retry_target in missing and retry_target not in target_langs:
            # Retry plans must validate stale targets instead of trusting Bazarr's
            # presence flag and leaving the plan at the head of the queue.
            target_langs.append(retry_target)
    source_langs = [l for l in LANGUAGES if l in available_by_lang]
    if retry_plan is not None:
        source_langs = [language for language in source_langs if language != retry_target]
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
                item_type, item_id, target_lang, "missing_source", reason="no source subtitle"
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
                "missing_source",
                reason="no complete source subtitle",
            )
        return
    _status_set_episode_identity(item_type, item_id, source_path)
    if rejected_sources:
        with stats_lock:
            stats["cleanup_alternative_sources"] = stats.get("cleanup_alternative_sources", 0) + 1
        print(f"[SOURCE] {title}: selected fallback '{source_lang}' after rejecting {rejected_sources} source(s)")
    if item_type == "episodes":
        _se = _re.search(r"[Ss](\d{1,2})[Ee](\d{1,2})", os.path.basename(source_path))
        if _se:
            title = f"{title} S{int(_se.group(1)):02d}E{int(_se.group(2)):02d}"
    print(f"[INFO] {title}: source={source_lang}, targets={target_langs}")

    if retry_plan is not None:
        current_source_hash = _file_hash_or_none(source_path)
        if current_source_hash and current_source_hash != retry_plan.get("sourceHash"):
            retry_target = str(retry_plan["targetLanguage"]).lower()
            replacement_target = _derive_target_path(
                source_path, source_lang, retry_target
            )
            _get_validation_state().schedule_retry_plan(
                item_type=item_type,
                item_id=item_id,
                target_language=retry_target,
                source_hash=current_source_hash,
                source_path=source_path,
                source_language=source_lang,
                target_path=replacement_target,
                series_key=series_key,
                series_title=series_title,
                media_title=title,
                source_cue_count=_count_srt_cues(source_path),
                failure_class=retry_plan.get("failureClass") or "whole_file",
                rules=retry_plan.get("rules") or [],
                state="regeneration_waiting",
                eligible_completed_cycle=(
                    _completed_cycle + REGENERATION_INITIAL_DELAY_CYCLES
                ),
                reason="source changed; retry plan superseded",
            )
            _get_validation_state().reset_circuit(
                series_key, "source subtitle fingerprint changed"
            )
            _status_transition(
                item_type,
                item_id,
                retry_target,
                "waiting_retry",
                reason="source changed; retry plan superseded",
            )
            return

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
        if retry_plan is None:
            try:
                scheduled = _get_validation_state().active_retry_plan(
                    item_type, item_id, target_lang
                )
            except StateStoreError as exc:
                print(f"{YELLOW}[RETRY] Could not check retry plan: {exc}{RESET}")
                scheduled = None
            if isinstance(scheduled, dict):
                current_source_hash = _file_hash_or_none(source_path)
                if (
                    current_source_hash
                    and current_source_hash != scheduled.get("sourceHash")
                ):
                    try:
                        scheduled, _ = _get_validation_state().schedule_retry_plan(
                            item_type=item_type,
                            item_id=item_id,
                            target_language=target_lang,
                            source_hash=current_source_hash,
                            source_path=source_path,
                            source_language=source_lang,
                            target_path=target_path,
                            series_key=series_key,
                            series_title=series_title,
                            media_title=title,
                            source_cue_count=_count_srt_cues(source_path),
                            failure_class=scheduled.get("failureClass") or "whole_file",
                            rules=scheduled.get("rules") or [],
                            state="regeneration_waiting",
                            eligible_completed_cycle=(
                                _completed_cycle
                                + REGENERATION_INITIAL_DELAY_CYCLES
                            ),
                            reason="source changed; retry plan reset",
                        )
                        _get_validation_state().reset_circuit(
                            series_key, "source subtitle fingerprint changed"
                        )
                        print(
                            f"[RETRY] Source changed for {title} '{target_lang}'; "
                            "superseded the old plan and reset attempts"
                        )
                    except StateStoreError as exc:
                        print(
                            f"{YELLOW}[RETRY] Could not reset changed-source "
                            f"plan: {exc}{RESET}"
                        )
                with stats_lock:
                    stats["deferred"] = stats.get("deferred", 0) + 1
                print(
                    f"[RETRY] Deferred normal queue for {title} '{target_lang}': "
                    f"state={scheduled['state']} "
                    f"eligible_cycle={scheduled['eligibleCompletedCycle']}"
                )
                _status_transition(
                    item_type,
                    item_id,
                    target_lang,
                    "waiting_retry",
                    reason=f"scheduled retry {scheduled['state']}",
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
                series_key=series_key,
                series_title=series_title,
            )
            if validation_action in ("valid", "valid-warning", "formatted", "repaired"):
                _record_valid_circuit_outcome(series_key, series_title)
                if retry_plan is not None:
                    _resolve_retry_success(item_type, item_id, target_lang)
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
                _record_invalid_circuit_outcome(
                    series_key, series_title, validation_action, validation_report
                )
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
            suppression = _cycle_quarantine_suppression(video_path, target_lang)
            if suppression is not None:
                with stats_lock:
                    stats["cycle_suppressions"] = (
                        stats.get("cycle_suppressions", 0) + 1
                    )
                    stats["deferred"] = stats.get("deferred", 0) + 1
                print(
                    f"[SKIP] {title} '{target_lang}': "
                    f"{suppression.get('action', 'cleanup')} already occurred "
                    "during this cycle"
                )
                _status_transition(
                    item_type,
                    item_id,
                    target_lang,
                    "deferred",
                    reason="same-cycle quarantine suppression",
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
            with stats_lock:
                stats["deferred"] = stats.get("deferred", 0) + 1
                stats["cooldown_deferrals"] = (
                    stats.get("cooldown_deferrals", 0) + 1
                )
            continue

        try:
            circuit = _get_validation_state().circuit_permission(
                series_key=series_key,
                series_title=series_title,
                config_fingerprint=_CIRCUIT_CONFIG_FINGERPRINT,
                claim=False,
            )
        except StateStoreError as exc:
            print(f"{YELLOW}[CIRCUIT] State unavailable for {title}: {exc}{RESET}")
            circuit = {"allowed": True, "state": "unknown", "failures": 0}
        if not circuit["allowed"]:
            eligible_cycle = circuit.get("eligibleAfterCycle")
            print(
                f"{YELLOW}[CIRCUIT] Deferred {title}: {circuit['state']} after "
                f"{circuit.get('failures', 0)} failures; eligible_after_cycle={eligible_cycle}{RESET}"
            )
            _status_transition(
                item_type,
                item_id,
                target_lang,
                "series_protected",
                reason=f"series circuit {circuit['state']}",
            )
            with stats_lock:
                stats["deferred"] = stats.get("deferred", 0) + 1
                stats["circuit_deferrals"] = (
                    stats.get("circuit_deferrals", 0) + 1
                )
            continue

        src_lines = _count_dialogue_lines(source_path)
        if src_lines is None or src_lines == 0:
            reason = "source unreadable" if src_lines is None else "source has no dialogue"
            print(f"{YELLOW}[SKIP] {title} '{target_lang}': {reason}{RESET}")
            with stats_lock:
                stats["deferred"] = stats.get("deferred", 0) + 1
            _status_transition(
                item_type, item_id, target_lang, "missing_source", reason=reason
            )
            continue
        try:
            timing = _estimate_timeout(source_path, source_lang, target_lang)
        except TypeError:
            # Preserve compatibility with integrations that patched the original
            # one-argument timeout hook.
            timeout_override = int(_estimate_timeout(source_path))
            cue_count = _count_srt_cues(source_path) or src_lines
            timing = {
                "cueCount": cue_count,
                "secondsPerCue": (
                    timeout_override / cue_count if cue_count else 0.0
                ),
                "sampleCount": 0,
                "scope": "override",
                "estimatedSeconds": timeout_override,
                "timeoutSeconds": timeout_override,
                "lane": (
                    "long" if timeout_override > LONG_JOB_THRESHOLD else "short"
                ),
            }
        shared_token = _shared_capacity.acquire_translation()
        if shared_token is None:
            _status_transition(
                item_type, item_id, target_lang, "deferred", reason="shared capacity unavailable"
            )
            continue
        lane = _file_lane_gate.acquire(
            timing["lane"] == "long",
            timing["estimatedSeconds"],
        )
        if lane is None:
            _shared_capacity.release(shared_token)
            _status_transition(
                item_type, item_id, target_lang, "deferred", reason="file lane unavailable"
            )
            continue
        if lane == "short (borrowed)":
            print(
                f"[LANE] {title} '{target_lang}' borrowed the idle long slot "
                f"(estimate={timing['estimatedSeconds']:.0f}s)"
            )
        elif lane == "long (borrowed)":
            print(
                f"[LANE] {title} '{target_lang}' borrowed the idle short slot "
                f"(estimate={timing['estimatedSeconds']:.0f}s)"
            )

        try:
            queued_circuit = _get_validation_state().circuit_permission(
                series_key=series_key,
                series_title=series_title,
                config_fingerprint=_CIRCUIT_CONFIG_FINGERPRINT,
                claim=False,
            )
        except StateStoreError as exc:
            print(f"{YELLOW}[CIRCUIT] State unavailable for {title}: {exc}{RESET}")
            queued_circuit = {"allowed": True, "state": "unknown", "failures": 0}
        if not queued_circuit["allowed"]:
            _file_lane_gate.release(lane)
            _shared_capacity.release(shared_token)
            eligible_cycle = queued_circuit.get("eligibleAfterCycle")
            print(
                f"{YELLOW}[CIRCUIT] Released {lane} slot for {title}: protection "
                f"opened while queued; state={queued_circuit['state']} "
                f"failures={queued_circuit.get('failures', 0)} "
                f"eligible_after_cycle={eligible_cycle}{RESET}"
            )
            with stats_lock:
                stats["deferred"] = stats.get("deferred", 0) + 1
                stats["circuit_deferrals"] = (
                    stats.get("circuit_deferrals", 0) + 1
                )
            _status_transition(
                item_type,
                item_id,
                target_lang,
                "series_protected",
                reason=f"series circuit {queued_circuit['state']}",
            )
            continue

        capacity_token = _translation_capacity.acquire(media_id, lingarr_media_type)
        if capacity_token is None:
            _file_lane_gate.release(lane)
            _shared_capacity.release(shared_token)
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
            _file_lane_gate.release(lane)
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
                series_key=series_key,
                series_title=series_title,
            )
            if validation_action in ("valid", "valid-warning", "formatted", "repaired"):
                _record_valid_circuit_outcome(series_key, series_title)
                if retry_plan is not None:
                    _resolve_retry_success(item_type, item_id, target_lang)
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
                _record_invalid_circuit_outcome(
                    series_key, series_title, validation_action, validation_report
                )
                with stats_lock:
                    stats["failed"] += 1
                    _record_cleanup_stats(stats, validation_action, validation_report)
                    if validation_action in ("quarantined", "deleted"):
                        _mark_activity(stats, item_type)
            _status_finish_validation(item_type, item_id, target_lang, validation_action)
            _shared_capacity.release(shared_token)
            continue

        src_lines = _count_dialogue_lines(source_path)
        if src_lines is None:
            _translation_capacity.release(capacity_token)
            _file_lane_gate.release(lane)
            _shared_capacity.release(shared_token)
            print(f"{YELLOW}[SKIP] {title} '{target_lang}': source not readable — deferring{RESET}")
            with stats_lock:
                stats.setdefault("deferred", 0)
                stats["deferred"] += 1
            _status_transition(
                item_type, item_id, target_lang, "missing_source", reason="source unreadable"
            )
            continue
        if src_lines == 0:
            _translation_capacity.release(capacity_token)
            _file_lane_gate.release(lane)
            _shared_capacity.release(shared_token)
            print(f"{YELLOW}[SKIP] {title} '{target_lang}': source has no dialogue lines{RESET}")
            with stats_lock:
                stats.setdefault("deferred", 0)
                stats["deferred"] += 1
            _status_transition(
                item_type, item_id, target_lang, "missing_source", reason="source has no dialogue"
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
            _file_lane_gate.release(lane)
            _shared_capacity.release(shared_token)
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
        if retry_plan is not None:
            try:
                retry_bound = _get_validation_state().bind_retry_submission(
                    retry_plan["id"],
                    retry_plan.get("claimOwner") or "",
                    attempt_id,
                )
            except StateStoreError:
                retry_bound = False
            if not retry_bound:
                _mark_submission_failed(attempt_id)
                _translation_capacity.release(capacity_token)
                _file_lane_gate.release(lane)
                _shared_capacity.release(shared_token)
                _get_validation_state().reschedule_retry_no_progress(
                    retry_plan["id"],
                    completed_cycle=_completed_cycle,
                    deferral_class="claim_binding_failed",
                    reason="retry submission reservation could not be bound",
                )
                continue
        status: str | None = None
        translation_started = time.monotonic()
        job_id: int | None = None
        trial_owner = f"attempt:{attempt_id}"
        trial_claimed = False
        trial_generation: int | None = None
        try:
            try:
                circuit = _get_validation_state().circuit_permission(
                    series_key=series_key,
                    series_title=series_title,
                    config_fingerprint=_CIRCUIT_CONFIG_FINGERPRINT,
                    claim=retry_plan is not None,
                    trial_owner=trial_owner,
                )
            except StateStoreError as exc:
                print(f"{YELLOW}[CIRCUIT] State unavailable for {title}: {exc}{RESET}")
                circuit = {"allowed": True, "state": "unknown", "failures": 0}
            if circuit.get("state") == "eligible" and retry_plan is None:
                circuit = {**circuit, "allowed": False}
            if not circuit["allowed"]:
                _mark_submission_failed(attempt_id)
                eligible_cycle = circuit.get("eligibleAfterCycle")
                print(
                    f"{YELLOW}[CIRCUIT] Deferred {title} before submission: "
                    f"state={circuit['state']} failures={circuit.get('failures', 0)} "
                    f"eligible_after_cycle={eligible_cycle}; released_slot={lane}{RESET}"
                )
                with stats_lock:
                    stats["deferred"] = stats.get("deferred", 0) + 1
                    stats["circuit_deferrals"] = (
                        stats.get("circuit_deferrals", 0) + 1
                    )
                _status_transition(
                    item_type,
                    item_id,
                    target_lang,
                    "series_protected",
                    reason=f"series circuit {circuit['state']}",
                )
                _shared_capacity.release(shared_token)
                continue
            trial_claimed = circuit.get("state") == "half_open"
            trial_generation = circuit.get("leaseGeneration")
            job_id = lingarr_submit_file(
                media_id,
                source_path,
                source_lang,
                target_lang,
                lingarr_media_type,
            )
            if job_id is None:
                _mark_submission_failed(attempt_id)
                if trial_claimed:
                    try:
                        _get_validation_state().release_circuit_trial(
                            series_key=series_key,
                            trial_owner=trial_owner,
                            reason="Lingarr submission did not create a job",
                        )
                    except StateStoreError as exc:
                        print(
                            f"{YELLOW}[CIRCUIT] Could not release unsubmitted "
                            f"half-open trial: {exc}{RESET}"
                        )
                with stats_lock:
                    stats["failed"] += 1
                _status_transition(
                    item_type,
                    item_id,
                    target_lang,
                    "failed",
                    reason="Lingarr submission failed",
                )
                _shared_capacity.release(shared_token)
                continue

            if retry_plan is not None:
                if retry_submission_callback is not None:
                    retry_submission_callback(retry_plan)
                try:
                    _get_validation_state().record_retry_admission(
                        retry_plan["id"], _completed_cycle, "submitted"
                    )
                except StateStoreError:
                    pass

            if trial_claimed:
                try:
                    trial_bound = _get_validation_state().bind_circuit_trial_job(
                        series_key,
                        trial_owner,
                        job_id,
                        trial_plan_id=retry_plan["id"] if retry_plan is not None else None,
                        lease_generation=trial_generation,
                    )
                except StateStoreError as exc:
                    trial_bound = False
                    print(
                        f"{YELLOW}[CIRCUIT] Could not bind half-open trial "
                        f"to Lingarr job {job_id}: {exc}{RESET}"
                    )
                if not trial_bound:
                    lingarr_cancel_job(job_id)
                    _mark_submission_failed(attempt_id)
                    try:
                        _get_validation_state().release_circuit_trial(
                            series_key,
                            trial_owner,
                            "trial job binding failed before monitoring",
                        )
                    except StateStoreError:
                        pass
                    job_id = None
                    with stats_lock:
                        stats["degraded"] = True
                    _status_transition(
                        item_type,
                        item_id,
                        target_lang,
                        "deferred",
                        reason="circuit trial binding failed",
                    )
                    _shared_capacity.release(shared_token)
                    continue
            try:
                _mark_submission_submitted(attempt_id, job_id)
            except StateStoreError as exc:
                print(
                    f"{YELLOW}[STATE] Lingarr accepted {title} '{target_lang}' "
                    f"but its job state could not be persisted; continuing to "
                    f"monitor job {job_id}: {exc}{RESET}"
                )
                with stats_lock:
                    stats["degraded"] = True
            _status_transition(
                item_type,
                item_id,
                target_lang,
                "translating",
                reason="waiting for Lingarr output",
                details={
                    "cueCount": timing["cueCount"],
                    "secondsPerCue": round(timing["secondsPerCue"], 4),
                    "timingSampleCount": timing["sampleCount"],
                    "timingScope": timing["scope"],
                    "estimatedSeconds": timing["estimatedSeconds"],
                    "timeoutSeconds": timing["timeoutSeconds"],
                    "etaSeconds": timing["estimatedSeconds"],
                    "lane": lane,
                    "attempts": 1,
                    "jobId": job_id,
                    "circuit": circuit,
                },
            )
            with stats_lock:
                stats["submitted"] += 1
                _mark_activity(stats, item_type)

            deadline = time.time() + timing["timeoutSeconds"]
            progress_callback = lambda progress: _status_transition(
                    item_type,
                    item_id,
                    target_lang,
                    "translating",
                    details={
                        "progress": progress,
                        "etaSeconds": max(
                            0,
                            round(
                                timing["estimatedSeconds"]
                                * (1.0 - min(100, max(0, progress)) / 100.0),
                                1,
                            ),
                        ),
                    },
                )
            try:
                status = lingarr_poll_job(
                    job_id,
                    deadline,
                    title,
                    progress_callback=progress_callback,
                )
            except TypeError:
                status = lingarr_poll_job(job_id, deadline, title)
        finally:
            if trial_claimed and job_id is None:
                try:
                    _get_validation_state().release_circuit_trial(
                        series_key=series_key,
                        trial_owner=trial_owner,
                        reason="submission path exited before a Lingarr job was created",
                    )
                except StateStoreError as exc:
                    print(
                        f"{YELLOW}[CIRCUIT] Could not release abandoned "
                        f"half-open claim: {exc}{RESET}"
                    )
            _translation_capacity.release(capacity_token)
            _file_lane_gate.release(lane)
        translation_elapsed = time.monotonic() - translation_started
        terminal_job = (
            lingarr_get_job(job_id)
            if status != "Completed" and job_id is not None
            else None
        )
        failure_details = _safe_failure_details(
            job_id,
            terminal_job=terminal_job,
            elapsed_seconds=translation_elapsed,
        )

        if status != "Completed":
            safe_to_recover = status is not None
            if status is None and job_id is not None:
                safe_to_recover = lingarr_cancel_job(job_id)
            recovery = (
                _recover_failed_lingarr_job(
                    job_id,
                    source_path,
                    target_path,
                    source_lang,
                    target_lang,
                    title,
                )
                if job_id is not None and safe_to_recover and not shutdown_requested
                else {"recovered": False, "reason": "job unavailable"}
            )
            if recovery.get("recovered"):
                status = "Completed"
                translation_elapsed += float(recovery.get("repairElapsedSeconds", 0))

        if status != "Completed":
            failure_reason = (
                "Lingarr timeout" if status is None else f"Lingarr job {status.lower()}"
            )
            try:
                _mark_submission_failed(
                    attempt_id,
                    failure_category=failure_details.get("category"),
                    failure_details=failure_details,
                )
            except StateStoreError as exc:
                print(f"{YELLOW}[FAIL] Could not persist Lingarr failure details: {exc}{RESET}")
            print(
                f"[FAILURE] Lingarr job {job_id}: "
                f"category={failure_details.get('category', 'unknown')} "
                f"status={failure_details.get('status') or status or 'timeout'} "
                f"provider={failure_details.get('provider', 'unknown')} "
                f"model={failure_details.get('model', 'unknown')} "
                f"message={failure_details.get('errorMessage') or 'not supplied'}"
            )
            try:
                _get_validation_state().record_circuit_outcome(
                    series_key=series_key,
                    series_title=series_title,
                    success=False,
                    reason=failure_reason,
                    threshold=CIRCUIT_FAILURE_THRESHOLD,
                    open_cycles=CIRCUIT_OPEN_CYCLES,
                    config_fingerprint=_CIRCUIT_CONFIG_FINGERPRINT,
                    trial_owner=trial_owner if trial_claimed else None,
                    trial_job_id=job_id if trial_claimed else None,
                    trial_plan_id=(retry_plan["id"] if trial_claimed and retry_plan else None),
                    lease_generation=trial_generation if trial_claimed else None,
                )
            except StateStoreError as exc:
                print(f"{YELLOW}[CIRCUIT] Could not record failure: {exc}{RESET}")
            _refresh_status_diagnostics()
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
                    item_type,
                    item_id,
                    target_lang,
                    "timed_out",
                    reason="Lingarr timeout",
                    details={"failureDetails": failure_details},
                )
            else:
                _status_transition(
                    item_type,
                    item_id,
                    target_lang,
                    "failed",
                    reason=f"Lingarr job {status.lower()}",
                    details={"failureDetails": failure_details},
                )
            _shared_capacity.release(shared_token)
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
            _shared_capacity.release(shared_token)
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
            _shared_capacity.release(shared_token)
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
            _shared_capacity.release(shared_token)
            continue

        if retry_plan is not None:
            try:
                retry_plan = _get_validation_state().update_retry_plan(
                    retry_plan["id"],
                    state="retry_in_progress",
                    completed_cycle=_completed_cycle,
                    increment_attempt=True,
                    reason="fresh Lingarr output received",
                )
            except StateStoreError as exc:
                print(f"{YELLOW}[RETRY] Could not record retry attempt: {exc}{RESET}")
        _status_transition(item_type, item_id, target_lang, "validating")
        validation_action, validation_report = _validate_translated_file(
            source_path, actual_target_path, source_lang, target_lang, item_id, title=title,
            defer_repair=True, item_type=item_type, media_duration=media_duration,
            origin="lingarr",
            provenance_source_hash=source_hash,
            series_key=series_key,
            series_title=series_title,
            trial_owner=trial_owner if trial_claimed else None,
            trial_job_id=job_id if trial_claimed else None,
            trial_plan_id=(retry_plan["id"] if trial_claimed and retry_plan else None),
            trial_generation=trial_generation if trial_claimed else None,
        )
        if validation_action in ("valid", "valid-warning", "formatted", "repaired"):
            _record_successful_source_readiness(
                source_path,
                source_lang,
                actual_target_path,
                target_lang,
                media_duration,
            )
            try:
                _get_validation_state().record_timing_sample(
                    kind="file",
                    source_language=source_lang,
                    target_language=target_lang,
                    cue_count=timing["cueCount"],
                    elapsed_seconds=translation_elapsed,
                    outcome="accepted",
                    lingarr_job_id=job_id,
                )
                _get_validation_state().record_circuit_outcome(
                    series_key=series_key,
                    series_title=series_title,
                    success=True,
                    reason=None,
                    threshold=CIRCUIT_FAILURE_THRESHOLD,
                    open_cycles=CIRCUIT_OPEN_CYCLES,
                    config_fingerprint=_CIRCUIT_CONFIG_FINGERPRINT,
                    trial_owner=trial_owner if trial_claimed else None,
                    trial_job_id=job_id if trial_claimed else None,
                    trial_plan_id=(retry_plan["id"] if trial_claimed and retry_plan else None),
                    lease_generation=trial_generation if trial_claimed else None,
                )
            except StateStoreError as exc:
                print(f"{YELLOW}[TIMING] Could not persist successful sample: {exc}{RESET}")
            _refresh_status_diagnostics()
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
            _record_invalid_circuit_outcome(
                series_key,
                series_title,
                validation_action,
                validation_report,
                trial_owner=trial_owner if trial_claimed else None,
                trial_job_id=job_id if trial_claimed else None,
                trial_plan_id=(retry_plan["id"] if trial_claimed and retry_plan else None),
                trial_generation=trial_generation if trial_claimed else None,
            )
            with stats_lock:
                stats["failed"] += 1
                stats.setdefault("cleaned", 0)
                stats["cleaned"] += 1
                _record_cleanup_stats(stats, validation_action, validation_report)
        if (
            retry_plan is not None
            and validation_action in ("valid", "valid-warning", "formatted", "repaired")
        ):
            _resolve_retry_success(
                item_type,
                item_id,
                target_lang,
                outcome="accepted_after_regeneration",
            )
        _status_finish_validation(item_type, item_id, target_lang, validation_action)
        _shared_capacity.release(shared_token)

# ---------------------------------------------------------------------------
# Existing-library cleanup
# ---------------------------------------------------------------------------

def _scan_undersized_sidecars(stats: dict) -> bool:
    """Validate regular subtitle density for every language using sibling media duration."""
    if not CLEANUP_UNDERSIZED_ENABLED:
        return False
    from .subtitles.core import file_sha256, validate_srt_structure

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
                tokens = _sidecar_tokens(video, subtitle)
                language = next(
                    (
                        token for token in tokens
                        if len(token) in (2, 3) and token.isalpha()
                    ),
                    "unknown",
                )
                _status_record_maintenance_outcome(
                    "undersized_detection",
                    "undersized",
                    _maintenance_file_identity(subtitle, language),
                )
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
            if action in ("quarantined", "deleted", "action-failed"):
                _status_record_maintenance_outcome(
                    "quarantine" if action == "quarantined" else (
                        "deletion" if action == "deleted" else "validation"
                    ),
                    action if action in ("quarantined", "deleted") else "failed",
                    _maintenance_file_identity(subtitle, language),
                    reason=(
                        "validation action failed"
                        if action == "action-failed"
                        else None
                    ),
                )
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
    from .subtitles.core import (
        file_sha256,
        target_language_for_code,
        validate_srt_structure,
        validate_subtitle_without_source,
    )

    language = classification.language
    evidence = {"path": str(classification.path), "language": language, "valid": False}
    if language is None or any(
        token in _NON_FULL_SUBTITLE_TOKENS for token in classification.tokens
    ):
        evidence["reason"] = "special-purpose track"
        return False, evidence
    target_language = target_language_for_code(language)
    if target_language is None:
        evidence["reason"] = "unsupported validation language"
        return False, evidence
    try:
        target_hash = file_sha256(classification.path)
    except OSError as exc:
        evidence["reason"] = f"hash unavailable: {exc}"
        return False, evidence
    evidence["hash"] = target_hash
    completeness = _evaluate_completeness(classification.path, duration)
    structure = validate_srt_structure(classification.path)
    _add_completeness_issue(structure, completeness)
    evidence["completeness"] = (
        completeness.to_dict() if completeness is not None else None
    )
    if not structure.valid:
        evidence["reason"] = (
            "undersized"
            if any(issue.rule == "undersized_subtitle" for issue in structure.issues)
            else "structure_invalid"
        )
        evidence["validation"] = structure.to_dict()
        return False, evidence
    video = _find_sidecar_video(classification.path)
    trusted = (
        _get_validation_state().source_readiness(
            media_identity=_media_identity_for_video(video),
            source_language=language,
            source_hash=target_hash,
            media_duration_seconds=duration,
        )
        if video is not None else None
    )
    if trusted is not None:
        evidence.update({
            "valid": True,
            "cached": True,
            "reason": "successful_source_hash",
            "readinessId": trusted["id"],
        })
        return True, evidence
    if detector is None:
        evidence["reason"] = "language_detector_unavailable"
        return False, evidence
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
    _add_completeness_issue(report, completeness)
    evidence["validation"] = report.to_dict()
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
    from .subtitles.core import file_sha256, quarantine_subtitle, write_validation_report

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
            destination = quarantine_subtitle(
                subtitle,
                CLEANUP_ROOTS,
                CLEANUP_QUARANTINE_DIR,
                access_coordinator=_artifact_access,
            )
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
    status_job_id: str | None = None,
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
                reasons = sorted({
                    str(candidate.get("reason") or "language_validation_failed")
                    for candidate in evidence if not candidate.get("valid")
                })
                readiness[language] = {
                    "ready": language_ready,
                    "reason": (
                        "successful_source_hash"
                        if any(candidate.get("reason") == "successful_source_hash" for candidate in evidence)
                        else (",".join(reasons) or "validated")
                    ),
                    "candidates": evidence,
                }
                if not language_ready:
                    stats["prune_invalid_languages"] += 1
                    ready = False
            if not ready:
                stats["prune_deferred"] += 1
                if videos is None and _video_has_pending_repair(video):
                    _queue_video_for_pruning(video, item_type)
                missing = ",".join(code for code, value in readiness.items() if not value["ready"])
                reason_codes = "; ".join(
                    f"{code}={value.get('reason') or 'language_validation_failed'}"
                    for code, value in readiness.items() if not value["ready"]
                )
                print(
                    f"{YELLOW}[PRUNE] Deferred {video.name}: managed language(s) "
                    f"not ready: {missing} ({reason_codes}){RESET}"
                )
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
                    _status_record_maintenance_outcome(
                        "sidecar_pruning",
                        "pruned",
                        _maintenance_file_identity(entry.path, entry.language),
                    )
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

    owns_status_job = status_job_id is None
    prune_job_id = status_job_id
    if owns_status_job:
        prune_job_id = _status_create_maintenance(
            "sidecar_pruning",
            {"title": "Subtitle sidecar pruning"},
            state="pruning",
        )
    else:
        _status_update_maintenance(prune_job_id, "pruning")
    try:
        if already_locked:
            result = run()
        else:
            with _cleanup_scan_lock:
                result = run()
    except Exception:
        if owns_status_job:
            _status_complete_maintenance(
                prune_job_id,
                "failed",
                reason="validation action failed",
            )
        raise
    prune_details = {
        "filesDiscovered": result[0]["prune_candidates"],
        "filesChecked": result[0]["prune_videos_checked"],
        "failures": result[0]["prune_failures"],
        "quarantines": result[0]["prune_quarantined"],
    }
    if owns_status_job:
        _status_complete_maintenance(prune_job_id, "accepted", details=prune_details)
    return result

def run_existing_cleanup_scan(
    maintenance_scan_job_id: str | None = None,
) -> dict:
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
    if maintenance_scan_job_id:
        with _maintenance_scan_contexts_lock:
            context = _maintenance_scan_contexts.get(maintenance_scan_job_id)
            if context is not None:
                context["stats"] = stats
    if not CLEANUP_SCAN_EXISTING:
        return stats

    from .subtitles.core import (
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
            prune_stats, prune_episodes, prune_movies = run_extra_sidecar_prune(
                already_locked=True, status_job_id=maintenance_scan_job_id
            )
            prune_stats["prune_bazarr_rescan_batches"] = int(prune_episodes or prune_movies)
            stats.update(prune_stats)
            changed = changed or prune_episodes or prune_movies
            if changed and not shutdown_requested:
                _tracked_bazarr_sync(True, True, SYNC_TIMEOUT)
            return stats
        candidates = discover_target_subtitles(CLEANUP_ROOTS, CLEANUP_LANGUAGES)
        if maintenance_scan_job_id:
            with _maintenance_scan_contexts_lock:
                context = _maintenance_scan_contexts.get(maintenance_scan_job_id)
                if context is not None:
                    context["files_discovered"] = len(candidates)
                    context["last_publish"] = time.monotonic()
                    details = _scan_progress_details(context)
            if context is not None:
                _status_update_maintenance(
                    maintenance_scan_job_id, "scanning", details=details
                )
        print(
            f"[SCAN] Existing subtitle cleanup found {len(candidates)} target file(s) "
            f"under {', '.join(str(root) for root in CLEANUP_ROOTS)}"
        )

        for candidate in candidates:
            if shutdown_requested:
                break
            if maintenance_scan_job_id:
                with _maintenance_scan_contexts_lock:
                    context = _maintenance_scan_contexts.get(
                        maintenance_scan_job_id
                    )
                    if context is not None:
                        context["files_checked"] += 1
            target_language = target_language_for_code(candidate.target_lang)
            if target_language is None:
                print(f"{YELLOW}[SCAN] Unsupported target language for {candidate.path}{RESET}")
                _publish_scan_progress(maintenance_scan_job_id)
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
                _publish_scan_progress(maintenance_scan_job_id)
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
                _publish_scan_progress(maintenance_scan_job_id)
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
                    maintenance_scan_job_id=maintenance_scan_job_id,
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

            operation_outcomes = {
                "valid": ("validation", "validated"),
                "valid-warning": ("validation", "validated"),
                "formatted": ("format_repair", "formatted"),
                "quarantined": ("quarantine", "quarantined"),
                "deleted": ("deletion", "deleted"),
                "action-failed": ("validation", "failed"),
            }
            if action in operation_outcomes:
                operation, outcome = operation_outcomes[action]
                _status_record_maintenance_outcome(
                    operation,
                    outcome,
                    _maintenance_file_identity(
                        candidate.path, candidate.target_lang
                    ),
                    reason="validation action failed" if outcome == "failed" else None,
                )

            _publish_scan_progress(maintenance_scan_job_id)

        _publish_scan_progress(maintenance_scan_job_id, force=True)
        prune_stats, prune_episodes, prune_movies = run_extra_sidecar_prune(
            already_locked=True, status_job_id=maintenance_scan_job_id
        )
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
            _tracked_bazarr_sync(True, True, SYNC_TIMEOUT)
        return stats


def _run_existing_cleanup_scan_safely() -> dict | None:
    scan_job_id = _status_create_maintenance(
        "existing_library_scan",
        {"title": "Existing subtitle library"},
        state="scanning",
        details={
            "filesDiscovered": 0,
            "filesChecked": 0,
            "filesRemaining": 0,
            "progress": 0,
        },
    )
    if scan_job_id:
        with _maintenance_scan_contexts_lock:
            _maintenance_scan_contexts[scan_job_id] = {
                "started": time.monotonic(),
                "stats": {},
                "files_discovered": 0,
                "files_checked": 0,
                "pending": 0,
                "repairs_queued": 0,
                "repairs_completed": 0,
                "enumeration_done": False,
                "last_publish": 0.0,
            }
    try:
        stats = run_existing_cleanup_scan(scan_job_id)
        if _pending_repairs:
            _status_update_maintenance(scan_job_id, "waiting_repair_completion")
            _drain_pending_repairs(stats)
        _scan_enumeration_finished(scan_job_id, stats)
        return (
            None
            if stats.get("async_repair_failures")
            or stats.get("cleanup_repair_failures")
            else stats
        )
    except Exception as e:
        print(f"{RED}[ERROR] Existing subtitle cleanup scan failed: {e}{RESET}")
        if scan_job_id:
            with _maintenance_scan_contexts_lock:
                _maintenance_scan_contexts.pop(scan_job_id, None)
            _status_complete_maintenance(
                scan_job_id, "failed", reason="existing library scan failed"
            )
        if DEBUG:
            import traceback
            traceback.print_exc()
        return None


def run_retention_housekeeping() -> dict:
    from .subtitles.core import purge_old_files

    current_log = [_app_log_sink.current_path] if _app_log_sink.current_path is not None else []
    protected = _get_validation_state().protected_artifact_paths()
    quarantine_removed = purge_old_files(
        CLEANUP_QUARANTINE_DIR,
        QUARANTINE_ARTIFACT_RETENTION_DAYS,
        exclude=protected,
        access_coordinator=_artifact_access,
    )
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


def _run_end_cycle_repair_retries(stats: dict) -> None:
    if not END_OF_CYCLE_REPAIR_RETRY_ENABLED or shutdown_requested:
        return
    try:
        plans = [
            plan for plan in _get_validation_state().retry_plans(include_terminal=False)
            if plan["state"] == "repair_retry_queued"
            and not plan["endCycleRepairAttempted"]
            and plan["eligibleCompletedCycle"] <= _completed_cycle
        ]
    except StateStoreError as exc:
        print(f"{YELLOW}[RETRY] Could not load repair retries: {exc}{RESET}")
        stats["degraded"] = True
        return
    for plan in plans:
        if shutdown_requested:
            break
        source_path = plan.get("sourcePath")
        target_path = plan.get("targetPath")
        try:
            _get_validation_state().update_retry_plan(
                plan["id"],
                state="retry_in_progress",
                completed_cycle=_completed_cycle,
                end_cycle_repair_attempted=True,
                reason="end-of-cycle repair retry",
            )
            if not source_path or not target_path or not os.path.exists(target_path):
                raise OSError("repair source or target is no longer available")
            action, report = _validate_translated_file(
                source_path,
                target_path,
                plan.get("sourceLanguage") or "",
                plan["targetLanguage"],
                plan["itemId"],
                title=plan.get("mediaTitle") or "",
                defer_repair=False,
                item_type=plan["itemType"],
                origin="lingarr",
                provenance_source_hash=plan["sourceHash"],
                series_key=plan.get("seriesKey"),
                series_title=plan.get("seriesTitle"),
            )
            if action in ("valid", "valid-warning", "formatted", "repaired"):
                _resolve_retry_success(
                    plan["itemType"], plan["itemId"], plan["targetLanguage"]
                )
                stats["retry_repairs_accepted"] = (
                    stats.get("retry_repairs_accepted", 0) + 1
                )
            elif action == "repair-deferred":
                _get_validation_state().update_retry_plan(
                    plan["id"],
                    state="retry_exhausted",
                    final_outcome="manual_review",
                    reason="end-of-cycle repair remained deferred",
                )
        except (OSError, StateStoreError) as exc:
            print(f"{YELLOW}[RETRY] Repair retry deferred: {exc}{RESET}")
            try:
                _get_validation_state().update_retry_plan(
                    plan["id"],
                    state="retry_exhausted",
                    final_outcome="manual_review",
                    reason=str(exc),
                )
            except StateStoreError:
                stats["degraded"] = True


def _run_regeneration_retry_batch(
    stats: dict,
    submission_budget: int,
    examined_plan_ids: set[int] | None = None,
    series_admissions: dict[str, int] | None = None,
) -> tuple[int, int]:
    if shutdown_requested:
        return 0, 0
    budget = max(1, int(submission_budget))
    examined_plan_ids = examined_plan_ids if examined_plan_ids is not None else set()
    series_admissions = series_admissions if series_admissions is not None else {}
    try:
        for pending in _get_validation_state().retry_plans(include_terminal=False):
            if pending.get("itemType") != "episodes":
                continue
            identity_item = {"seriesTitle": pending.get("seriesTitle")}
            old_key = str(pending.get("seriesKey") or "")
            if old_key.startswith("sonarr:"):
                try:
                    identity_item["sonarrSeriesId"] = int(old_key.split(":", 1)[1])
                except ValueError:
                    pass
            canonical = resolve_media_identity(
                identity_item,
                "episodes",
                pending["itemId"],
                pending.get("sourcePath"),
            )
            if old_key and canonical["key"] != old_key:
                _get_validation_state().register_series_alias(
                    old_key, canonical["key"], canonical["title"]
                )
        due_before = _get_validation_state().due_retry_count(_completed_cycle)
        plans = _get_validation_state().claim_due_retry_plans(
            _completed_cycle,
            limit=budget,
            per_series_limit=RETRY_MAX_PER_SERIES_PER_CYCLE,
            excluded_plan_ids=examined_plan_ids,
            series_admissions=series_admissions,
        )
    except StateStoreError as exc:
        print(f"{YELLOW}[RETRY] Could not claim regeneration retries: {exc}{RESET}")
        stats["degraded"] = True
        return 0, 0
    plans = [plan for plan in plans if int(plan["id"]) not in examined_plan_ids]
    if not plans:
        return 0, 0
    examined_plan_ids.update(int(plan["id"]) for plan in plans)
    stats["regeneration_queued"] = stats.get("regeneration_queued", 0) + len(plans)
    print(
        f"[RETRY] Due={due_before} admitted={len(plans)} "
        f"remaining={max(0, due_before - len(plans))} "
        f"completed_cycle={_completed_cycle}"
    )
    retry_lock = threading.Lock()
    submitted_plan_ids: set[int] = set()

    def note_retry_submission(plan: dict) -> None:
        with retry_lock:
            submitted_plan_ids.add(int(plan["id"]))

    admitted: list[tuple[dict, dict, str, str]] = []
    for plan in plans:
        try:
            _get_validation_state().record_retry_admission(
                plan["id"], _completed_cycle, "examined"
            )
        except StateStoreError:
            pass
        if shutdown_requested:
            try:
                _get_validation_state().update_retry_plan(
                    plan["id"],
                    state="regeneration_waiting",
                    reason="retry admission cancelled during shutdown",
                )
            except StateStoreError:
                stats["degraded"] = True
            continue
        item_type = plan["itemType"]
        id_field = "sonarrEpisodeId" if item_type == "episodes" else "radarrId"
        identity = retry_media_identity(plan)
        item = {
            id_field: plan["itemId"],
            "title": identity.get("episodeTitle") or identity["displayTitle"],
            "seriesTitle": identity["displayTitle"],
            "missing_subtitles": [{"code2": plan["targetLanguage"]}],
        }
        if plan.get("seriesKey", "").startswith("sonarr:"):
            try:
                item["sonarrSeriesId"] = int(plan["seriesKey"].split(":", 1)[1])
            except (TypeError, ValueError):
                pass
        print(
            f"[RETRY] Admitting regeneration plan {plan['id']} "
            f"for {item_type}:{plan['itemId']} '{plan['targetLanguage']}' "
            f"attempt={plan['attemptCount'] + 1}/"
            f"{REGENERATION_MAX_ATTEMPTS or 'unlimited'}"
        )
        _status_admit_retry(plan, identity)
        admitted.append((plan, item, item_type, id_field))

    def run_retry(
        plan: dict, item: dict, item_type: str, id_field: str
    ) -> None:
        try:
            process_item(
                item,
                item_type,
                id_field,
                stats,
                retry_lock,
                retry_plan=plan,
                retry_submission_callback=note_retry_submission,
            )
        finally:
            _shared_capacity.release_current_translation()

    dispatch_workers = max(
        PARALLEL_TRANSLATES * 4, PARALLEL_TRANSLATES + 1
    )
    with ThreadPoolExecutor(
        max_workers=min(len(admitted), dispatch_workers) or 1,
        thread_name_prefix="retry-worker",
    ) as executor:
        futures = {
            executor.submit(run_retry, plan, item, item_type, id_field): plan
            for plan, item, item_type, id_field in admitted
        }
        for future in as_completed(futures):
            plan = futures[future]
            worker_error: Exception | None = None
            try:
                future.result()
            except Exception as exc:
                worker_error = exc
                print(
                    f"{RED}[ERROR] Retry worker failed for plan "
                    f"{plan['id']}: {exc}{RESET}"
                )
                with retry_lock:
                    stats["degraded"] = True
                _status_transition(
                    plan["itemType"],
                    plan["itemId"],
                    plan["targetLanguage"],
                    "deferred",
                    reason="retry worker failed",
                )
            try:
                current = next(
                    (
                        entry for entry in _get_validation_state().retry_plans()
                        if entry["id"] == plan["id"]
                    ),
                    None,
                )
                if current and current["state"] == "regeneration_queued":
                    deferral_class = (
                        "shutdown"
                        if shutdown_requested
                        else "worker_exception"
                        if worker_error is not None
                        else "admission_no_progress"
                    )
                    _get_validation_state().reschedule_retry_no_progress(
                        plan["id"],
                        completed_cycle=_completed_cycle,
                        deferral_class=deferral_class,
                        reason=(
                            "retry worker failed before Lingarr output"
                            if worker_error is not None
                            else "retry admission stopped during shutdown"
                            if shutdown_requested
                            else "retry admission deferred before Lingarr output"
                        ),
                    )
                    _get_validation_state().record_retry_admission(
                        plan["id"],
                        _completed_cycle,
                        "no_progress",
                        deferral_class,
                    )
                elif current and current.get("submissionAttemptId") is None:
                    _get_validation_state().record_retry_admission(
                        plan["id"], _completed_cycle, "reconciled"
                    )
            except StateStoreError as exc:
                print(
                    f"{YELLOW}[RETRY] Could not release deferred plan: "
                    f"{exc}{RESET}"
                )
                stats["degraded"] = True

    for plan in plans:
        if int(plan["id"]) not in submitted_plan_ids:
            continue
        series_bucket = str(
            plan.get("canonicalSeriesKey")
            or plan.get("seriesKey")
            or f"{plan['itemType']}:{plan['itemId']}"
        )
        series_admissions[series_bucket] = series_admissions.get(series_bucket, 0) + 1
    submissions_used = len(submitted_plan_ids)
    return submissions_used, len(plans)


def _run_regeneration_retries(
    stats: dict,
    submission_budget: int | None = None,
    refill_round: int = 0,
    examined_plan_ids: set[int] | None = None,
    series_admissions: dict[str, int] | None = None,
) -> None:
    """Work-conserving retry admission without recursive refill calls."""
    del refill_round  # retained for compatibility with existing callers/tests
    remaining_budget = max(
        1,
        int(
            RETRY_BATCH_SIZE_PER_CYCLE
            if submission_budget is None
            else submission_budget
        ),
    )
    examined = examined_plan_ids if examined_plan_ids is not None else set()
    admissions = series_admissions if series_admissions is not None else {}
    while remaining_budget > 0 and not shutdown_requested:
        examined_before = len(examined)
        submissions_used, plans_examined = _run_regeneration_retry_batch(
            stats,
            remaining_budget,
            examined,
            admissions,
        )
        remaining_budget = max(0, remaining_budget - submissions_used)
        if remaining_budget <= 0 or plans_examined == 0:
            break
        if len(examined) <= examined_before:
            break
        print(
            f"[RETRY] Refilling {remaining_budget} translation slot(s) after "
            "reconciliation/no-progress outcomes"
        )


def run_cycle(cycle_num: int) -> bool:
    print(f"\n{BOLD}{CYAN}===== Cycle #{cycle_num} ====={RESET}")
    _status_set_phase("cycle_work")

    stats: dict = {
        "submitted": 0,
        "completed": 0,
        "timed_out": 0,
        "failed": 0,
        "deferred": 0,
        "api_errors": 0,
        "degraded": False,
        "cycle_suppressions": 0,
        "cooldown_deferrals": 0,
        "circuit_deferrals": 0,
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
        # Extra dispatch threads let short work reach its reserved lane even when
        # several long items appear first; the lane and Lingarr gates still cap
        # actual full-file translations at PARALLEL_TRANSLATES.
        dispatch_workers = max(PARALLEL_TRANSLATES * 4, PARALLEL_TRANSLATES + 1)
        def run_item_with_capacity_cleanup(*args):
            try:
                return process_item(*args)
            finally:
                _shared_capacity.release_current_translation()

        with ThreadPoolExecutor(max_workers=dispatch_workers) as executor:
            futures = {
                executor.submit(
                    run_item_with_capacity_cleanup,
                    item, itype, ifield, stats, stats_lock,
                ):
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
                    stats["degraded"] = True
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
        _status_set_phase("repair_drain")
        repair_results = _drain_pending_repairs(stats)
        _status_set_phase("cycle_work")

    _status_set_phase("retry_recovery")
    _run_end_cycle_repair_retries(stats)
    _run_regeneration_retries(stats)
    if _pending_repairs:
        print("[REPAIR] Draining repairs queued by retry recovery")
        _status_set_phase("repair_drain")
        repair_results.extend(_drain_pending_repairs(stats))
    _status_set_phase("cycle_work")

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
    print(f"  Cycle suppressions    : {stats.get('cycle_suppressions', 0)}")
    print(f"  Cooldown deferrals    : {stats.get('cooldown_deferrals', 0)}")
    print(f"  Circuit deferrals     : {stats.get('circuit_deferrals', 0)}")
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
        _tracked_bazarr_sync(had_episodes, had_movies, SYNC_TIMEOUT)
        repaired_with_ids = [
            result for result in repair_results
            if result.action == "repaired" and result.item_id is not None
        ]
        missing = [result for result in repaired_with_ids if not _bazarr_has_repaired_path(result)]
        if missing and not shutdown_requested:
            retry_episodes = any(result.item_type == "episodes" for result in missing)
            retry_movies = any(result.item_type == "movies" for result in missing)
            print(f"{YELLOW}[WARNING] Bazarr did not register {len(missing)} repaired path(s); retrying scan once{RESET}")
            _tracked_bazarr_sync(retry_episodes, retry_movies, SYNC_TIMEOUT)
            still_missing = [result for result in missing if not _bazarr_has_repaired_path(result)]
            stats["cleanup_bazarr_registration_failures"] = len(still_missing)
            for result in still_missing:
                print(f"{YELLOW}[WARNING] Bazarr still does not list repaired subtitle for {result.title} '{result.target_lang}'{RESET}")

    queue_drained = _drain_lingarr_queue()
    if not queue_drained:
        stats["degraded"] = True
    _reconcile_retry_claims(_get_validation_state())
    _reconcile_circuit_trial_leases(_get_validation_state())
    _status_finish_cycle(stats)
    return bool(
        not shutdown_requested
        and not stats.get("degraded")
        and not stats.get("api_errors")
    )

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _reconcile_retry_claims(state_store: StateStore) -> int:
    reconciled = 0
    for plan in state_store.retry_claims_with_submissions():
        try:
            if plan["state"] == "retry_in_progress":
                state_store.update_retry_plan(
                    plan["id"],
                    state="regeneration_waiting",
                    eligible_completed_cycle=_completed_cycle,
                    reason="retry output awaiting validation after restart",
                )
                reconciled += 1
                continue
            job_id = plan.get("lingarrJobId")
            job = lingarr_get_job(job_id) if job_id is not None else None
            status = str((job or {}).get("status") or "")
            if status and status not in ("Completed", "Failed", "Cancelled", "Interrupted"):
                continue
            if status == "Completed":
                state_store.update_retry_plan(
                    plan["id"],
                    state="regeneration_waiting",
                    eligible_completed_cycle=_completed_cycle,
                    reason="completed retry output awaiting validation",
                )
            elif status in ("Failed", "Cancelled", "Interrupted"):
                attempts = int(plan.get("attemptCount") or 0)
                state_store.update_retry_plan(
                    plan["id"],
                    state="regeneration_waiting",
                    completed_cycle=_completed_cycle,
                    eligible_completed_cycle=(
                        _completed_cycle + _regeneration_delay_cycles(attempts)
                    ),
                    increment_attempt=True,
                    reason=f"recovered terminal retry: {status}",
                )
            else:
                state_store.reschedule_retry_no_progress(
                    plan["id"],
                    completed_cycle=_completed_cycle,
                    deferral_class="submission_unresolved",
                    reason="durable retry submission could not be resolved",
                )
            reconciled += 1
        except Exception as exc:
            print(
                f"{YELLOW}[RETRY] Could not reconcile claim "
                f"{plan.get('id')}: {exc}{RESET}"
            )
    return reconciled


def _reconcile_circuit_trial_leases(state_store: StateStore) -> int:
    reconciled = 0
    for lease in state_store.circuit_trial_leases():
        try:
            job = lingarr_get_job(lease["trialJobId"])
            job_status = str((job or {}).get("status") or "")
            if job_status in ("Failed", "Cancelled", "Interrupted"):
                state_store.record_circuit_outcome(
                    series_key=lease["seriesKey"],
                    series_title=lease["seriesTitle"],
                    success=False,
                    reason=f"recovered terminal trial: {job_status}",
                    threshold=CIRCUIT_FAILURE_THRESHOLD,
                    open_cycles=CIRCUIT_OPEN_CYCLES,
                    config_fingerprint=_CIRCUIT_CONFIG_FINGERPRINT,
                    trial_owner=lease.get("trialOwner"),
                    trial_job_id=lease.get("trialJobId"),
                    trial_plan_id=lease.get("trialPlanId"),
                    lease_generation=lease.get("leaseGeneration"),
                )
                reconciled += 1
            elif job_status == "Completed":
                state_store.mark_circuit_trial_validation_pending(
                    lease["seriesKey"],
                    lease["trialOwner"],
                )
                reconciled += 1
            elif not job:
                claimed_at = float(lease.get("trialClaimedAt") or 0)
                if claimed_at and (
                    time.time() - claimed_at
                    >= TRANSLATION_TIMEOUT_CAP + max(60, POLL_INTERVAL)
                ):
                    state_store.release_circuit_trial(
                        lease["seriesKey"],
                        lease["trialOwner"],
                        "expired bound trial job could not be resolved",
                    )
                    reconciled += 1
        except Exception as exc:
            print(
                f"{YELLOW}[CIRCUIT] Could not reconcile trial "
                f"{lease.get('seriesKey')}: {exc}{RESET}"
            )
    return reconciled


def _run_legacy_quarantine_index(state_store: StateStore) -> dict:
    """Index legacy artifacts conservatively before the first cycle."""
    from autotranslate.maintenance.legacy_index import LegacyQuarantineIndexer
    from .subtitles.core import (
        cue_source_signature,
        file_sha256,
        parse_srt_cues,
        read_text_best_effort,
        source_cue_signatures,
        target_language_for_code,
        validate_cue_pair,
        validate_subtitle_pair,
    )

    detector = _get_cleanup_detector()

    def inspect(artifact: Path, report: dict, identity: dict) -> dict:
        source_path = Path(report["sourcePath"])
        target_language = str(report["targetLanguage"])
        language = target_language_for_code(target_language)
        if detector is None or language is None:
            return {"accepted": False, "reasonCode": "language_mismatch"}
        source_raw = read_text_best_effort(source_path)
        target_raw = read_text_best_effort(artifact)
        if source_raw is None or target_raw is None:
            return {"accepted": False, "reasonCode": "artifact_unavailable"}
        source_cues, source_errors = parse_srt_cues(source_raw)
        target_cues, target_errors = parse_srt_cues(target_raw)
        if source_errors or target_errors or len(source_cues) != len(target_cues):
            return {"accepted": False, "reasonCode": "source_signature_mismatch"}
        validation = validate_subtitle_pair(
            source_path,
            artifact,
            detector,
            language,
            target_lang=target_language,
            **_validation_kwargs(),
        )
        prior = state_store.quarantine_attempts(
            identity["itemType"], identity["itemId"], target_language
        )
        attempt = state_store.record_quarantine_attempt(
            item_type=identity["itemType"],
            item_id=identity["itemId"],
            target_language=target_language,
            source_hash=report["sourceHash"],
            target_hash=file_sha256(artifact),
            attempt_number=max(
                [int(entry.get("attemptNumber") or 0) for entry in prior] or [0]
            ) + 1,
            artifact_path=artifact,
            report_path=Path(f"{artifact}.validation.json"),
            failure_rules=(issue.rule for issue in validation.issues),
            cue_signatures=source_cue_signatures(source_path),
            repair_provenance=[],
            donor_provenance=[],
        )
        valid_pairs = []
        cue_kwargs = {
            key: value
            for key, value in _validation_kwargs().items()
            if key in {
                "max_cue_lines", "max_cue_chars", "max_expansion_ratio",
                "max_expansion_chars", "max_source_similarity",
                "max_cyrillic_ratio", "max_cjk_ratio", "max_latin_ratio",
            }
        }
        for index, (source_cue, target_cue) in enumerate(zip(source_cues, target_cues)):
            if not validate_cue_pair(
                source_cue, target_cue, cue_index=index,
                target_lang=target_language, **cue_kwargs,
            ):
                valid_pairs.append((source_cue, target_cue))
        if not valid_pairs:
            return {"accepted": False, "reasonCode": "current_validation_failed"}
        partial_id = state_store.record_partial_candidate(
            item_type=identity["itemType"],
            item_id=identity["itemId"],
            source_language=identity.get("sourceLanguage"),
            target_language=target_language,
            source_hash=report["sourceHash"],
            target_hash=file_sha256(artifact),
            changed_cues=[source.number for source, _target in valid_pairs],
            unresolved_cues=[
                source.number for source, target in zip(source_cues, target_cues)
                if (source, target) not in valid_pairs
            ],
            provenance=[{"stage": "legacy_index"}],
            artifact_path=artifact,
            quarantine_attempt_id=attempt["id"],
        )
        for source_cue, target_cue in valid_pairs:
            signature = cue_source_signature(source_cue)
            state_store.record_cue_recovery(
                partial_candidate_id=partial_id,
                item_type=identity["itemType"],
                item_id=identity["itemId"],
                source_language=identity.get("sourceLanguage"),
                target_language=target_language,
                source_file_hash=report["sourceHash"],
                source_cue_number=source_cue.number,
                source_cue_hash=signature["sourceHash"],
                source_signature=signature,
                cue_start_ms=signature.get("startMs"),
                target_text=target_cue.text,
                target_hash=hashlib.sha256(target_cue.text.encode("utf-8")).hexdigest(),
                recovery_stage="legacy_index",
                source_attempt_id=attempt["id"],
            )
        return {
            "accepted": True,
            "quarantineAttemptId": attempt["id"],
            "partialCandidateId": partial_id,
        }

    indexer = LegacyQuarantineIndexer(
        state=state_store,
        root=CLEANUP_QUARANTINE_DIR,
        inspect_artifact=inspect,
        shutdown_requested=lambda: shutdown_requested,
    )
    result = indexer.run()
    print(
        f"[QUARANTINE] Legacy index discovered={result['discovered']} "
        f"indexed={result['indexed']} unresolved={result['unresolved']} "
        f"skipped={result['skipped']}"
    )
    return result


def _requeue_persisted_repairs(state_store: StateStore) -> int:
    """Revalidate and requeue durable repairs without relying on a library scan."""
    queued = 0
    if not hasattr(state_store, "repair_jobs_for_restart"):
        return queued
    for job in state_store.repair_jobs_for_restart():
        if shutdown_requested:
            break
        source_path = job.get("sourcePath")
        target_path = job.get("targetPath")
        if (
            not source_path
            or not target_path
            or _file_hash_or_none(source_path) != job.get("sourceHash")
            or _file_hash_or_none(target_path) != job.get("targetHash")
        ):
            state_store.transition_repair_job(
                job["id"], "failed", error_code="repair_inputs_changed",
                expected_states=("persisted_for_restart",),
            )
            continue
        payload = job.get("payload") or {}
        action, _report = _validate_translated_file(
            source_path, target_path,
            payload.get("sourceLanguage") or "en",
            job["targetLanguage"], job.get("itemId"),
            title=payload.get("title") or os.path.basename(target_path),
            defer_repair=True, item_type=job.get("itemType"),
            origin=payload.get("origin") or "recovered_repair",
            provenance_source_hash=job.get("sourceHash"),
            series_key=payload.get("seriesKey"),
            series_title=payload.get("seriesTitle"),
            trial_owner=payload.get("trialOwner"),
            trial_job_id=payload.get("trialJobId"),
            trial_plan_id=payload.get("trialPlanId"),
            trial_generation=payload.get("trialGeneration"),
        )
        if action == "repair-queued":
            queued += 1
        elif action in (
            "valid", "valid-warning", "formatted", "quarantined", "deleted",
            "reported", "dry-run",
        ):
            state_store.transition_repair_job(
                job["id"], "completed", expected_states=("persisted_for_restart",)
            )
        elif action != "repair-deferred":
            state_store.transition_repair_job(
                job["id"], "failed", error_code=str(action),
                expected_states=("persisted_for_restart",),
            )
    return queued


def main() -> int:
    global _status_tracker, _completed_cycle
    state_store = _initialize_state_store()
    backfilled_source_readiness = state_store.backfill_source_readiness()
    _completed_cycle = state_store.completed_cycle()
    recovered_repairs = state_store.recover_repair_jobs()
    reactivated_manual_reviews = state_store.reactivate_changed_manual_reviews(
        _VALIDATION_CONFIG_FINGERPRINT
    )
    recovered_claims = state_store.recover_retry_claims()
    reconciled_retry_claims = _reconcile_retry_claims(state_store)
    recovered_trials = state_store.recover_abandoned_circuit_trials(
        max_age_seconds=0
    )
    reconciled_trials = _reconcile_circuit_trial_leases(state_store)
    backfilled_retry_sizes = 0
    for plan in state_store.retry_plans(include_terminal=False):
        if plan.get("sourceCueCount") is not None or not plan.get("sourcePath"):
            continue
        cue_count = _count_srt_cues(plan["sourcePath"])
        if cue_count and state_store.set_retry_source_cue_count(plan["id"], cue_count):
            backfilled_retry_sizes += 1
    print(
        f"[CYCLE] Restored completed-cycle sequence {_completed_cycle}; "
        f"released {recovered_claims} orphaned retry claim(s); "
        f"persisted {recovered_repairs} repair job(s) for restart; "
        f"reactivated {reactivated_manual_reviews} changed manual review(s); "
        f"reconciled {reconciled_retry_claims} submitted retry claim(s); "
        f"released {recovered_trials} unbound circuit trial(s); "
        f"reconciled {reconciled_trials} bound circuit trial(s); "
        f"backfilled {backfilled_retry_sizes} retry size(s); "
        f"trusted {backfilled_source_readiness} proven source hash(es)"
    )
    status_server = None
    if STATUS_ENABLED:
        try:
            _status_tracker = StatusTracker(
                STATUS_SNAPSHOT_FILE,
                STATUS_HISTORY_FILE,
                retention_days=STATUS_HISTORY_RETENTION_DAYS,
                recent_limit=STATUS_RECENT_LIMIT,
            )
            _refresh_status_diagnostics()
            try:
                status_server, _ = start_status_server(
                    _status_tracker, STATUS_BIND, STATUS_PORT, LOG_DIR
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
    print(
        f"  Quarantine retry  : {REGENERATION_MAX_ATTEMPTS} attempts after "
        f"{REGENERATION_INITIAL_DELAY_CYCLES} completed cycle(s), "
        f"batch {RETRY_BATCH_SIZE_PER_CYCLE}"
    )
    if _LEGACY_QUARANTINE_HOLD_DAYS is not None:
        print(
            f"{YELLOW}[WARNING] CLEANUP_QUARANTINE_HOLD_DAYS is deprecated "
            "and no longer controls retry eligibility"
            f"{RESET}"
        )
    print(f"  Sidecar pruning   : {'ON' if CLEANUP_PRUNE_EXTRA_LANGUAGES else 'off'} "
          f"({CLEANUP_PRUNE_ACTION}, unknown={'remove' if CLEANUP_PRUNE_UNKNOWN_SIDECARS else 'retain'})")
    print(f"  Max cue lines     : {CLEANUP_MAX_CUE_LINES}")
    print(f"  Format recovery   : {'ON' if CLEANUP_FORMAT_REPAIR_ENABLED else 'off'}")
    print(f"  Shared capacity   : {PARALLEL_TRANSLATES} (translations + repairs; repairs first)")
    print(f"  Repair queue max  : {CLEANUP_REPAIR_QUEUE_MAX}")
    print(f"  Size validation   : {'ON' if CLEANUP_UNDERSIZED_ENABLED else 'off'} "
          f"({CLEANUP_UNDERSIZED_REQUIRED_SIGNALS}/4 signals, media >= {CLEANUP_MIN_MEDIA_DURATION:.0f}s)")
    print(f"  Size thresholds   : {CLEANUP_MIN_CUES_PER_MINUTE:g} cues/min, "
          f"{CLEANUP_MIN_TEXT_CHARS_PER_MINUTE:g} chars/min, "
          f"{CLEANUP_MIN_BYTES_PER_MINUTE:g} bytes/min, "
          f"{CLEANUP_MIN_TIMELINE_COVERAGE:.0%} timeline")
    print(
        f"  Retention         : state/logs {RETENTION_DAYS} days; quarantine "
        f"{QUARANTINE_ARTIFACT_RETENTION_DAYS} days "
        f"(checked every {RETENTION_CHECK_INTERVAL}s)"
    )
    print(f"  Status dashboard  : {'ON' if STATUS_ENABLED else 'off'}"
          + (f" on {STATUS_BIND}:{STATUS_PORT}" if STATUS_ENABLED else ""))
    print(f"  Status retention  : {STATUS_HISTORY_RETENTION_DAYS} days")
    print(
        f"  Adaptive timeout  : x{TRANSLATION_TIMEOUT_MULTIPLIER:g}, "
        f"cap {TRANSLATION_TIMEOUT_CAP}s, cold "
        f"{TRANSLATION_COLD_SECONDS_PER_CUE:g}s/cue"
    )
    print(
        f"  Long-job threshold: {LONG_JOB_THRESHOLD}s "
        f"({_file_lane_gate.short_capacity} short / "
        f"{_file_lane_gate.long_capacity} long file lanes)"
    )
    print(
        f"  Circuit breaker   : {CIRCUIT_FAILURE_THRESHOLD} failures / "
        f"{CIRCUIT_OPEN_CYCLES} healthy completed cycles"
    )
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

    print("[INFO] Waiting 30s for services to start...")
    _status_set_phase("startup_wait")
    sys.stdout.flush()
    for _ in range(30):
        if shutdown_requested:
            break
        time.sleep(1)

    if not shutdown_requested:
        print("[INFO] Running initial Bazarr subtitle synchronization...")
        _status_set_phase("startup_sync")
        trigger_bazarr_sync(True, True)
        wait_for_bazarr_sync(True, True, SYNC_TIMEOUT)

    if not shutdown_requested:
        startup_repairs = _requeue_persisted_repairs(state_store)
        if startup_repairs:
            print(f"[REPAIR] Requeued {startup_repairs} durable startup repair(s)")
            _status_set_phase("repair_drain")
            _drain_pending_repairs({})

    _status_set_phase("startup_cleanup")
    legacy_run_id = state_store.start_maintenance_run(
        "legacy_quarantine_index",
        due_reason="startup reconciliation",
        completed_cycle=_completed_cycle,
    )
    try:
        legacy_metrics = _run_legacy_quarantine_index(state_store)
        state_store.finish_maintenance_run(
            legacy_run_id, success=True, metrics=legacy_metrics
        )
    except Exception as exc:
        state_store.finish_maintenance_run(
            legacy_run_id,
            success=False,
            failure_code=type(exc).__name__,
        )
        print(f"{YELLOW}[QUARANTINE] Legacy index failed: {exc}{RESET}")
    run_retention_housekeeping()
    last_retention_check = time.monotonic()

    cycle = _completed_cycle + 1
    _cycle_suppressions.begin_cycle(str(cycle))
    last_cleanup_scan = 0.0
    if not shutdown_requested and CLEANUP_SCAN_EXISTING:
        _status_set_phase("startup_cleanup")
        startup_cleanup = _run_existing_cleanup_scan_safely()
        if startup_cleanup is not None:
            last_cleanup_scan = time.monotonic()

    def run_cycle_owned(cycle_number: int) -> bool:
        _cycle_suppressions.begin_cycle(str(cycle_number))
        return run_cycle(cycle_number)

    cycle_runner = CycleRunner(run_cycle_owned)

    def advance_completed_cycle() -> int:
        global _completed_cycle
        _completed_cycle = state_store.advance_completed_cycle()
        print(f"[CYCLE] Persisted completed cycle {_completed_cycle}")
        return _completed_cycle

    def tracked_maintenance(name: str, reason: str, operation):
        run_id = state_store.start_maintenance_run(
            name, due_reason=reason, completed_cycle=_completed_cycle
        )
        try:
            metrics = operation()
            if metrics is None:
                raise RuntimeError(f"{name} failed")
        except Exception as exc:
            state_store.finish_maintenance_run(
                run_id, success=False, failure_code=type(exc).__name__
            )
            raise
        state_store.finish_maintenance_run(
            run_id, success=True, metrics=metrics
        )
        return metrics

    def mark_retention_completed() -> None:
        nonlocal last_retention_check
        last_retention_check = time.monotonic()

    def mark_scan_completed() -> None:
        nonlocal last_cleanup_scan
        last_cleanup_scan = time.monotonic()

    maintenance = MaintenanceCoordinator((
        MaintenanceOperation(
            "retention",
            due=lambda: (
                time.monotonic() - last_retention_check
                >= RETENTION_CHECK_INTERVAL
            ),
            run=lambda: tracked_maintenance(
                "retention", "retention interval elapsed",
                run_retention_housekeeping,
            ),
            mark_completed=mark_retention_completed,
        ),
        MaintenanceOperation(
            "existing_library_scan",
            due=lambda: bool(
                CLEANUP_SCAN_EXISTING
                and last_cleanup_scan > 0
                and time.monotonic() - last_cleanup_scan
                >= CLEANUP_SCAN_INTERVAL
            ),
            run=lambda: tracked_maintenance(
                "existing_library_scan", "cleanup scan interval elapsed",
                _run_existing_cleanup_scan_safely,
            ),
            mark_completed=mark_scan_completed,
        ),
    ))

    def lifecycle_phase(phase: str, **values) -> None:
        if phase == "cooldown":
            values.setdefault("next_cycle_at", time.time() + CHECK_INTERVAL)
        _status_set_phase(phase, **values)

    def sleep_interruptibly(seconds: int) -> bool:
        print(f"[INFO] Next cycle in {seconds}s...")
        for _ in range(max(0, int(seconds))):
            if shutdown_requested:
                return True
            time.sleep(1)
        return shutdown_requested

    controller = LifecycleController(
        run_cycle=lambda number: cycle_runner.run(number).healthy,
        advance_completed_cycle=advance_completed_cycle,
        run_maintenance=maintenance.run_due,
        set_phase=lifecycle_phase,
        refresh_diagnostics=_refresh_status_diagnostics,
        sleep_interruptibly=sleep_interruptibly,
        check_interval=CHECK_INTERVAL,
        shutdown_requested=lambda: shutdown_requested,
    )

    def report_iteration(number, healthy, maintenance_result) -> None:
        if not healthy:
            print(
                f"{YELLOW}[CYCLE] Cycle #{number} was degraded or interrupted; "
                f"completed-cycle counter was not advanced{RESET}"
            )
        if not maintenance_result.healthy:
            print(
                f"{YELLOW}[MAINTENANCE] Post-cycle maintenance failed "
                f"({', '.join(maintenance_result.failed)}); it remains due{RESET}"
            )

    controller.run(cycle, on_iteration=report_iteration)

    _shutdown_repair_executor()
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
