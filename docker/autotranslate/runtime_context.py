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
from concurrent.futures import CancelledError, Future, wait
from dataclasses import dataclass
from pathlib import Path
import requests
from .status.tracker import StatusTracker, build_cycle_jobs, episode_identity_from_path
from .status.server import start_status_server
from media_identity import resolve_media_identity, retry_media_identity
from .persistence.state_store import StateStore, StateStoreError
from .scheduling.locks import ArtifactAccessCoordinator
from .services.bazarr import BazarrClient, ServiceRequestError
from .services.http import JsonRequester
from .services.lingarr import LingarrActiveTranslation, LingarrClient, LingarrProvider, LingarrSourceLanguage, ProviderResponseError
from .models import RepairJobResult
from .scheduling.suppressions import CycleSuppressionRegistry
from .scheduling.retries import RetryQueueProcessor
from .scheduling.repairs import RepairCoordinator
from .scheduling.executor import DaemonExecutor as _DaemonExecutor, DaemonRepairExecutor as _DaemonRepairExecutor, completed_futures
from .scheduling.capacity import FileLaneGate as _FileLaneGate, SharedCapacityCoordinator as _SharedCapacityCoordinator, TranslationCapacityGate as _TranslationCapacityGate
from .status.logging import DailyLogHandler as _DailyLogHandler, DailyLogSink as _DailyLogSink, QueuedLogStream as _QueuedLogStream, TeeStream as _TeeStream, UtcLogFormatter as _UtcLogFormatter
from .cycle import CycleRunner
from .lifecycle import LifecycleController, ShutdownController
from .maintenance.coordinator import MaintenanceCoordinator, MaintenanceOperation
from .maintenance.library import ExistingLibraryMaintenance
from .maintenance.retention import run_retention
from .status.facade import StatusFacade
sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', buffering=1)
sys.stderr = os.fdopen(sys.stderr.fileno(), 'w', buffering=1)
_tty = sys.stdout.isatty()
GREEN = '\x1b[92m' if _tty else ''
YELLOW = '\x1b[93m' if _tty else ''
RED = '\x1b[91m' if _tty else ''
CYAN = '\x1b[96m' if _tty else ''
BOLD = '\x1b[1m' if _tty else ''
RESET = '\x1b[0m' if _tty else ''

def _require(var: str) -> str:
    val = os.getenv(var, '').strip()
    if not val:
        print(f'{RED}[ERROR] {var} environment variable is required{RESET}')
        sys.exit(1)
    return val

def _normalize_url(raw: str) -> str:
    raw = raw.strip().rstrip('/')
    if not raw.startswith(('http://', 'https://')):
        raw = f'http://{raw}'
    return raw
_raw_languages = os.getenv('LANGUAGES', 'en,et,sv')
LANGUAGES = [l.strip().lower() for l in _raw_languages.split(',') if l.strip()]
BAZARR_URL = _normalize_url(_require('BAZARR_URL'))
BAZARR_API_KEY = _require('BAZARR_API_KEY')
LINGARR_URL = _normalize_url(_require('LINGARR_URL'))
LINGARR_API_KEY = os.getenv('LINGARR_API_KEY', '').strip()
PARALLEL_TRANSLATES = max(1, int(os.getenv('PARALLEL_TRANSLATES', '1')))
CHECK_INTERVAL = max(10, int(os.getenv('CHECK_INTERVAL', '1200')))
CONNECT_TIMEOUT = max(5, int(os.getenv('CONNECT_TIMEOUT', '10')))
POLL_INTERVAL = max(5, int(os.getenv('POLL_INTERVAL', '20')))
POLL_TIMEOUT = max(30, int(os.getenv('POLL_TIMEOUT', '900')))
TRANSLATION_TIMEOUT_MULTIPLIER = max(1.0, float(os.getenv('TRANSLATION_TIMEOUT_MULTIPLIER', '1.25')))
TRANSLATION_TIMEOUT_CAP = max(POLL_TIMEOUT, int(os.getenv('TRANSLATION_TIMEOUT_CAP', '10800')))
TRANSLATION_COLD_SECONDS_PER_CUE = max(0.01, float(os.getenv('TRANSLATION_COLD_SECONDS_PER_CUE', '1.8')))
TRANSLATION_TIMING_ALPHA = min(1.0, max(0.01, float(os.getenv('TRANSLATION_TIMING_ALPHA', '0.20'))))
LONG_JOB_THRESHOLD = max(60, int(os.getenv('LONG_JOB_THRESHOLD', '1800')))
REPAIR_TIMEOUT_MULTIPLIER = max(1.0, float(os.getenv('REPAIR_TIMEOUT_MULTIPLIER', '2.0')))
CIRCUIT_FAILURE_THRESHOLD = max(1, int(os.getenv('CIRCUIT_FAILURE_THRESHOLD', '3')))
CIRCUIT_OPEN_CYCLES = max(1, int(os.getenv('CIRCUIT_OPEN_CYCLES', '3')))
if os.getenv('CIRCUIT_OPEN_SECONDS'):
    print('[WARNING] CIRCUIT_OPEN_SECONDS is deprecated and has no scheduling effect; use CIRCUIT_OPEN_CYCLES')
RESUBMIT_COOLDOWN = max(60, int(os.getenv('RESUBMIT_COOLDOWN', '3600')))
SYNC_TIMEOUT = max(30, int(os.getenv('SYNC_TIMEOUT', '600')))
SYNC_POLL_INTERVAL = max(5, int(os.getenv('SYNC_POLL_INTERVAL', '15')))
SYNC_START_TIMEOUT = max(5, int(os.getenv('SYNC_START_TIMEOUT', '30')))
CLEANUP_MIN_CONFIDENCE = float(os.getenv('CLEANUP_MIN_CONFIDENCE', '0.70'))
CLEANUP_MIN_CHARS = int(os.getenv('CLEANUP_MIN_CHARS', '200'))
CLEANUP_MAX_UNIQUE_RATIO = float(os.getenv('CLEANUP_MAX_UNIQUE_RATIO', '0.15'))
CLEANUP_MAX_CYRILLIC_RATIO = float(os.getenv('CLEANUP_MAX_CYRILLIC_RATIO', '0.05'))
CLEANUP_MAX_CJK_RATIO = float(os.getenv('CLEANUP_MAX_CJK_RATIO', '0.05'))
CLEANUP_MAX_LATIN_RATIO = float(os.getenv('CLEANUP_MAX_LATIN_RATIO', '0.80'))
CLEANUP_MIN_LETTERS_FOR_SCRIPT = int(os.getenv('CLEANUP_MIN_LETTERS_FOR_SCRIPT', '20'))
CLEANUP_MAX_CUE_LINES = max(1, int(os.getenv('CLEANUP_MAX_CUE_LINES', '4')))
CLEANUP_MAX_CUE_CHARS = max(50, int(os.getenv('CLEANUP_MAX_CUE_CHARS', '500')))
CLEANUP_MAX_EXPANSION_RATIO = max(1.0, float(os.getenv('CLEANUP_MAX_EXPANSION_RATIO', '4.0')))
CLEANUP_MAX_EXPANSION_CHARS = max(50, int(os.getenv('CLEANUP_MAX_EXPANSION_CHARS', '300')))
CLEANUP_MAX_SOURCE_SIMILARITY = min(1.0, max(0.5, float(os.getenv('CLEANUP_MAX_SOURCE_SIMILARITY', '0.92'))))
CLEANUP_REPAIR_ENABLED = os.getenv('CLEANUP_REPAIR_ENABLED', 'true').lower() in ('1', 'true', 'yes')
CLEANUP_MAX_REPAIR_ATTEMPTS = max(1, int(os.getenv('CLEANUP_MAX_REPAIR_ATTEMPTS', '5')))
CLEANUP_REPAIR_CONTEXT_LINES = max(0, int(os.getenv('CLEANUP_REPAIR_CONTEXT_LINES', '5')))
CLEANUP_FORMAT_REPAIR_ENABLED = os.getenv('CLEANUP_FORMAT_REPAIR_ENABLED', 'true').lower() in ('1', 'true', 'yes')
CLEANUP_REPAIR_QUEUE_MAX = max(1, int(os.getenv('CLEANUP_REPAIR_QUEUE_MAX', '100')))
REPAIR_SHUTDOWN_GRACE_SECONDS = max(1, int(os.getenv('REPAIR_SHUTDOWN_GRACE_SECONDS', '30')))
CLEANUP_UNDERSIZED_ENABLED = os.getenv('CLEANUP_UNDERSIZED_ENABLED', 'true').lower() in ('1', 'true', 'yes')
CLEANUP_MIN_MEDIA_DURATION = max(0.0, float(os.getenv('CLEANUP_MIN_MEDIA_DURATION', '900')))
CLEANUP_MIN_CUES_PER_MINUTE = max(0.0, float(os.getenv('CLEANUP_MIN_CUES_PER_MINUTE', '1.5')))
CLEANUP_MIN_TEXT_CHARS_PER_MINUTE = max(0.0, float(os.getenv('CLEANUP_MIN_TEXT_CHARS_PER_MINUTE', '40')))
CLEANUP_MIN_BYTES_PER_MINUTE = max(0.0, float(os.getenv('CLEANUP_MIN_BYTES_PER_MINUTE', '100')))
CLEANUP_MIN_TIMELINE_COVERAGE = min(1.0, max(0.0, float(os.getenv('CLEANUP_MIN_TIMELINE_COVERAGE', '0.60'))))
CLEANUP_UNDERSIZED_REQUIRED_SIGNALS = min(4, max(1, int(os.getenv('CLEANUP_UNDERSIZED_REQUIRED_SIGNALS', '3'))))
CLEANUP_FFPROBE_TIMEOUT = max(1, int(os.getenv('CLEANUP_FFPROBE_TIMEOUT', '15')))
CLEANUP_SCAN_EXISTING = os.getenv('CLEANUP_SCAN_EXISTING', 'true').lower() in ('1', 'true', 'yes')
CLEANUP_SCAN_INTERVAL = max(300, int(os.getenv('CLEANUP_SCAN_INTERVAL', '21600')))
CLEANUP_SCAN_DRY_RUN = os.getenv('CLEANUP_SCAN_DRY_RUN', 'false').lower() in ('1', 'true', 'yes')
CLEANUP_PRUNE_EXTRA_LANGUAGES = os.getenv('CLEANUP_PRUNE_EXTRA_LANGUAGES', 'true').lower() in ('1', 'true', 'yes')
CLEANUP_PRUNE_ACTION = os.getenv('CLEANUP_PRUNE_ACTION', 'quarantine').strip().lower()
CLEANUP_PRUNE_SPECIAL_SIDECARS = os.getenv('CLEANUP_PRUNE_SPECIAL_SIDECARS', 'true').lower() in ('1', 'true', 'yes')
CLEANUP_PRUNE_UNKNOWN_SIDECARS = os.getenv('CLEANUP_PRUNE_UNKNOWN_SIDECARS', 'false').lower() in ('1', 'true', 'yes')
CLEANUP_SOURCELESS_LINE_ONLY_ACTION = os.getenv('CLEANUP_SOURCELESS_LINE_ONLY_ACTION', 'warn').strip().lower()
_legacy_hold_days_raw = os.getenv('CLEANUP_QUARANTINE_HOLD_DAYS', '').strip()
_LEGACY_QUARANTINE_HOLD_DAYS = _legacy_hold_days_raw or None
CLEANUP_ROOT_RAW = os.getenv('CLEANUP_ROOT', '/media').strip() or '/media'
CLEANUP_ROOTS = [Path(value.strip()) for value in CLEANUP_ROOT_RAW.split(os.pathsep) if value.strip()]
CLEANUP_ACTION = os.getenv('CLEANUP_ACTION', 'quarantine').strip().lower()
_raw_cleanup_langs = os.getenv('CLEANUP_LANGUAGES', 'et')
CLEANUP_LANGUAGES = {l.strip() for l in _raw_cleanup_langs.split(',') if l.strip()}
STATE_DIR = os.getenv('STATE_DIR', '/config').strip() or '/config'
SUBMIT_CACHE_FILE = os.path.join(STATE_DIR, 'submitted_cache.json')
CLEANUP_QUARANTINE_DIR = Path(os.getenv('CLEANUP_QUARANTINE_DIR', f'{STATE_DIR}/quarantine'))
VALIDATION_STATE_FILE = Path(STATE_DIR) / 'validation_state.json'
STATE_DB_FILE = Path(STATE_DIR) / 'bazarr-autotranslate.sqlite3'
LOG_DIR = Path(os.getenv('LOG_DIR', '/var/log/bazarr-autotranslate'))
RETENTION_DAYS = max(1, int(os.getenv('RETENTION_DAYS', '30')))
QUARANTINE_ARTIFACT_RETENTION_DAYS = max(1, int(os.getenv('QUARANTINE_ARTIFACT_RETENTION_DAYS', str(RETENTION_DAYS))))
REGENERATION_INITIAL_DELAY_CYCLES = max(1, int(os.getenv('REGENERATION_INITIAL_DELAY_CYCLES', '2')))
REGENERATION_MAX_ATTEMPTS = max(0, int(os.getenv('REGENERATION_MAX_ATTEMPTS', '0')))
REGENERATION_MAX_DELAY_CYCLES = max(REGENERATION_INITIAL_DELAY_CYCLES, int(os.getenv('REGENERATION_MAX_DELAY_CYCLES', '16')))
REGENERATION_BACKOFF_MULTIPLIER = max(1.0, float(os.getenv('REGENERATION_BACKOFF_MULTIPLIER', '2.0')))
DONOR_RECOVERY_ENABLED = os.getenv('DONOR_RECOVERY_ENABLED', 'true').lower() in ('1', 'true', 'yes')
RETRY_BATCH_SIZE_PER_CYCLE = max(1, int(os.getenv('RETRY_BATCH_SIZE_PER_CYCLE', '5')))
RETRY_MAX_PER_SERIES_PER_CYCLE = max(1, int(os.getenv('RETRY_MAX_PER_SERIES_PER_CYCLE', '1')))
END_OF_CYCLE_REPAIR_RETRY_ENABLED = os.getenv('END_OF_CYCLE_REPAIR_RETRY_ENABLED', 'true').lower() in ('1', 'true', 'yes')
RETENTION_CHECK_INTERVAL = max(300, int(os.getenv('RETENTION_CHECK_INTERVAL', '3600')))
STATUS_ENABLED = os.getenv('STATUS_ENABLED', 'true').lower() in ('1', 'true', 'yes')
STATUS_BIND = os.getenv('STATUS_BIND', '0.0.0.0').strip() or '0.0.0.0'
STATUS_PORT = int(os.getenv('STATUS_PORT', '8765'))
STATUS_HISTORY_RETENTION_DAYS = max(7, int(os.getenv('STATUS_HISTORY_RETENTION_DAYS', '30')))
STATUS_RECENT_LIMIT = max(1, int(os.getenv('STATUS_RECENT_LIMIT', '20')))
STATUS_SNAPSHOT_FILE = Path(STATE_DIR) / 'status.json'
STATUS_HISTORY_FILE = Path(STATE_DIR) / 'status_history.jsonl'
DEBUG = os.getenv('DEBUG', '').lower() in ('1', 'true', 'yes')
_CIRCUIT_CONFIG_FINGERPRINT = hashlib.sha256(json.dumps({'lingarr': LINGARR_URL, 'languages': LANGUAGES, 'timeoutMultiplier': TRANSLATION_TIMEOUT_MULTIPLIER, 'timeoutCap': TRANSLATION_TIMEOUT_CAP, 'parallel': PARALLEL_TRANSLATES, 'openCycles': CIRCUIT_OPEN_CYCLES}, sort_keys=True).encode('utf-8')).hexdigest()[:16]
_VALIDATION_CONFIG_FINGERPRINT = hashlib.sha256(json.dumps({'maxCueLines': CLEANUP_MAX_CUE_LINES, 'maxCueChars': CLEANUP_MAX_CUE_CHARS, 'maxExpansionRatio': CLEANUP_MAX_EXPANSION_RATIO, 'maxExpansionChars': CLEANUP_MAX_EXPANSION_CHARS, 'maxSourceSimilarity': CLEANUP_MAX_SOURCE_SIMILARITY, 'maxCyrillicRatio': CLEANUP_MAX_CYRILLIC_RATIO, 'maxCjkRatio': CLEANUP_MAX_CJK_RATIO, 'maxLatinRatio': CLEANUP_MAX_LATIN_RATIO, 'minMediaDuration': CLEANUP_MIN_MEDIA_DURATION, 'minCuesPerMinute': CLEANUP_MIN_CUES_PER_MINUTE, 'minTextCharsPerMinute': CLEANUP_MIN_TEXT_CHARS_PER_MINUTE, 'minBytesPerMinute': CLEANUP_MIN_BYTES_PER_MINUTE, 'minTimelineCoverage': CLEANUP_MIN_TIMELINE_COVERAGE, 'undersizedRequiredSignals': CLEANUP_UNDERSIZED_REQUIRED_SIGNALS, 'donorEnabled': DONOR_RECOVERY_ENABLED, 'donorSimilarity': 0.95, 'donorTimestampToleranceMs': 500}, sort_keys=True).encode('utf-8')).hexdigest()[:16]
if not LANGUAGES:
    print(f'{RED}[ERROR] LANGUAGES must contain at least one language code{RESET}')
    sys.exit(1)
if CLEANUP_ACTION not in ('quarantine', 'delete', 'report'):
    print(f'{RED}[ERROR] CLEANUP_ACTION must be quarantine, delete, or report{RESET}')
    sys.exit(1)
if CLEANUP_PRUNE_ACTION not in ('quarantine', 'delete', 'report'):
    print(f'{RED}[ERROR] CLEANUP_PRUNE_ACTION must be quarantine, delete, or report{RESET}')
    sys.exit(1)
if CLEANUP_SOURCELESS_LINE_ONLY_ACTION not in ('warn', 'quarantine'):
    print(f'{RED}[ERROR] CLEANUP_SOURCELESS_LINE_ONLY_ACTION must be warn or quarantine{RESET}')
    sys.exit(1)
if not 1 <= STATUS_PORT <= 65535:
    print(f'{RED}[ERROR] STATUS_PORT must be between 1 and 65535{RESET}')
    sys.exit(1)
_app_log_sink = _DailyLogSink(LOG_DIR)
_log_queue: queue.Queue = queue.Queue()
_app_logger = logging.getLogger('bazarr_autotranslate')
_app_logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)
_app_logger.propagate = False
_queue_handler = logging.handlers.QueueHandler(_log_queue)
_app_logger.addHandler(_queue_handler)
_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(logging.Formatter('%(message)s'))
_daily_handler = _DailyLogHandler(_app_log_sink)
_daily_handler.setFormatter(_UtcLogFormatter('%(asctime)s.%(msecs)03dZ %(message)s', datefmt='%Y-%m-%dT%H:%M:%S'))
_log_listener = logging.handlers.QueueListener(_log_queue, _console_handler, _daily_handler, respect_handler_level=True)
_log_listener.start()
atexit.register(_app_log_sink.close)
atexit.register(_log_listener.stop)
sys.stdout = _QueuedLogStream(_app_logger, logging.INFO, _console_handler.stream)
sys.stderr = _QueuedLogStream(_app_logger, logging.ERROR, _console_handler.stream)
BAZARR_HEADERS: dict = {'Accept': 'application/json', 'X-API-KEY': BAZARR_API_KEY}
LINGARR_HEADERS: dict = {'Accept': 'application/json', 'Content-Type': 'application/json'}
if LINGARR_API_KEY:
    LINGARR_HEADERS['X-Api-Key'] = LINGARR_API_KEY
_cleanup_detector = None
_cleanup_detector_lock = threading.Lock()
_validation_state = None
_validation_state_lock = threading.Lock()
_cleanup_scan_lock = threading.Lock()
_repair_executor = None
_repair_executor_lock = threading.Lock()
_repair_shutdown_event = threading.Event()
_repair_capacity = threading.BoundedSemaphore(PARALLEL_TRANSLATES + CLEANUP_REPAIR_QUEUE_MAX)
_repair_futures = RepairCoordinator(state_provider=lambda: _get_validation_state())
_pending_repairs = _repair_futures.pending
_pending_repairs_lock = _repair_futures.lock
_repair_keys = _repair_futures.keys
_artifact_access = ArtifactAccessCoordinator()
_duration_cache: dict[tuple[str, int, int], float | None] = {}
_duration_cache_lock = threading.Lock()
_pending_prune_videos: dict[str, str | None] = {}
_pending_prune_lock = threading.Lock()
_maintenance_scan_contexts: dict[str, dict] = {}
_maintenance_scan_contexts_lock = threading.Lock()
_status_tracker: StatusTracker | None = None
_status_facade: StatusFacade | None = None
_completed_cycle = 0
_runtime_resources_lock = threading.Lock()
_active_state_store: StateStore | None = None
_active_status_server = None
