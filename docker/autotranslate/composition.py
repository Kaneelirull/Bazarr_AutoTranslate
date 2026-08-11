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
from concurrent.futures import CancelledError, Future, wait
from dataclasses import dataclass
from pathlib import Path
import requests
from .status.tracker import StatusTracker, build_cycle_jobs, episode_identity_from_path
from .status.server import start_status_server
from .media_identity import resolve_media_identity, retry_media_identity
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
GREEN = YELLOW = RED = CYAN = BOLD = RESET = ''
_logging_resource = None
_configured = False


class RuntimeContext:
    """Application-owned runtime namespace assembled by the production root.

    Immutable dependencies and validated configuration fall back to this module;
    mutable lifecycle state and package-owned workflow exports live on the context
    instance instead of being registered on another Python module.
    """

    def __getattr__(self, name):
        try:
            return globals()[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


runtime = RuntimeContext()


def configure(config, logging_resource=None) -> None:
    """Install validated configuration before feature workflows are loaded."""
    global _configured, _logging_resource, _config
    if _configured:
        return
    for name, value in vars(config).items():
        globals()[name.upper()] = value
    globals().update({
        'LANGUAGES': list(config.languages),
        'STATE_DIR': str(config.state_dir),
        'STATE_DB_FILE': config.state_dir / 'bazarr-autotranslate.sqlite3',
        'CLEANUP_QUARANTINE_DIR': config.quarantine_dir,
        'CLEANUP_ROOTS': list(config.cleanup_roots),
        'CLEANUP_LANGUAGES': set(config.cleanup_languages),
        'LOG_DIR': config.log_dir,
        'BAZARR_HEADERS': {'Accept': 'application/json', 'X-API-KEY': config.bazarr_api_key},
        'LINGARR_HEADERS': {'Accept': 'application/json', 'Content-Type': 'application/json'},
        'STATUS_SNAPSHOT_FILE': config.state_dir / 'status.json',
        'STATUS_HISTORY_FILE': config.state_dir / 'status_history.jsonl',
    })
    if config.lingarr_api_key:
        LINGARR_HEADERS['X-Api-Key'] = config.lingarr_api_key
    globals()['_CIRCUIT_CONFIG_FINGERPRINT'] = hashlib.sha256(json.dumps({
        'lingarr': config.lingarr_url, 'languages': config.languages,
        'timeoutMultiplier': config.translation_timeout_multiplier,
        'timeoutCap': config.translation_timeout_cap,
        'parallel': config.parallel_translates,
        'openCycles': config.circuit_open_cycles,
    }, sort_keys=True).encode('utf-8')).hexdigest()[:16]
    globals()['_VALIDATION_CONFIG_FINGERPRINT'] = hashlib.sha256(json.dumps({
        'maxCueLines': config.cleanup_max_cue_lines,
        'maxCueChars': config.cleanup_max_cue_chars,
        'maxExpansionRatio': config.cleanup_max_expansion_ratio,
        'maxExpansionChars': config.cleanup_max_expansion_chars,
        'maxSourceSimilarity': config.cleanup_max_source_similarity,
        'maxCyrillicRatio': config.cleanup_max_cyrillic_ratio,
        'maxCjkRatio': config.cleanup_max_cjk_ratio,
        'maxLatinRatio': config.cleanup_max_latin_ratio,
        'minMediaDuration': config.cleanup_min_media_duration,
        'minCuesPerMinute': config.cleanup_min_cues_per_minute,
        'minTextCharsPerMinute': config.cleanup_min_text_chars_per_minute,
        'minBytesPerMinute': config.cleanup_min_bytes_per_minute,
        'minTimelineCoverage': config.cleanup_min_timeline_coverage,
        'undersizedRequiredSignals': config.cleanup_undersized_required_signals,
        'donorEnabled': config.donor_recovery_enabled,
        'donorSimilarity': 0.95, 'donorTimestampToleranceMs': 500,
    }, sort_keys=True).encode('utf-8')).hexdigest()[:16]
    _logging_resource = logging_resource
    _config = config
    _configured = True
_cleanup_detector = None
_cleanup_detector_lock = threading.Lock()
_validation_state = None
_validation_state_lock = threading.Lock()
_cleanup_scan_lock = threading.Lock()
_repair_executor = None
_repair_executor_lock = threading.Lock()
_repair_shutdown_event = threading.Event()
_repair_capacity = None
_repair_futures = None
_pending_repairs = set()
_pending_repairs_lock = threading.Lock()
_repair_keys = set()
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
_manual_review_service = None
