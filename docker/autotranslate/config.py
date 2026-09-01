from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class ConfigError(ValueError):
    """Raised when environment configuration is invalid."""


def _required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} environment variable is required")
    return value


def _url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    return normalized if normalized.startswith(("http://", "https://")) else f"http://{normalized}"


def _integer(values: Mapping[str, str], name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(values.get(name, str(default))))
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc


def _boolean(values: Mapping[str, str], name: str, default: bool) -> bool:
    raw = values.get(name, "true" if default else "false").strip().lower()
    if raw in {"1", "true", "yes"}:
        return True
    if raw in {"0", "false", "no"}:
        return False
    raise ConfigError(f"{name} must be a boolean")


def _float(
    values: Mapping[str, str], name: str, default: float,
    minimum: float = 0.0, maximum: float | None = None,
) -> float:
    try:
        value = max(minimum, float(values.get(name, str(default))))
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    return min(maximum, value) if maximum is not None else value


def _choice(
    values: Mapping[str, str], name: str, default: str, allowed: set[str]
) -> str:
    value = values.get(name, default).strip().lower()
    if value not in allowed:
        raise ConfigError(f"{name} must be one of {', '.join(sorted(allowed))}")
    return value


@dataclass(frozen=True)
class Config:
    bazarr_url: str
    bazarr_api_key: str
    lingarr_url: str
    lingarr_api_key: str
    languages: tuple[str, ...]
    parallel_translates: int
    maintenance_workers: int
    check_interval: int
    connect_timeout: int
    poll_interval: int
    poll_timeout: int
    repair_shutdown_grace_seconds: int
    state_dir: Path
    quarantine_dir: Path
    log_dir: Path
    status_manual_actions_enabled: bool
    translation_timeout_multiplier: float
    translation_timeout_cap: int
    translation_cold_seconds_per_cue: float
    translation_timing_alpha: float
    long_job_threshold: int
    repair_timeout_multiplier: float
    circuit_failure_threshold: int
    circuit_open_cycles: int
    resubmit_cooldown: int
    sync_timeout: int
    sync_poll_interval: int
    sync_start_timeout: int
    cleanup_min_confidence: float
    cleanup_min_chars: int
    cleanup_max_unique_ratio: float
    cleanup_max_cyrillic_ratio: float
    cleanup_max_cjk_ratio: float
    cleanup_max_latin_ratio: float
    cleanup_min_letters_for_script: int
    cleanup_max_cue_lines: int
    cleanup_max_cue_chars: int
    cleanup_max_expansion_ratio: float
    cleanup_max_expansion_chars: int
    cleanup_max_source_similarity: float
    cleanup_repair_enabled: bool
    cleanup_max_repair_attempts: int
    cleanup_repair_context_lines: int
    cleanup_format_repair_enabled: bool
    cleanup_repair_queue_max: int
    cleanup_undersized_enabled: bool
    cleanup_min_media_duration: float
    cleanup_min_cues_per_minute: float
    cleanup_min_text_chars_per_minute: float
    cleanup_min_bytes_per_minute: float
    cleanup_min_timeline_coverage: float
    cleanup_undersized_required_signals: int
    cleanup_ffprobe_timeout: int
    cleanup_scan_existing: bool
    cleanup_scan_interval: int
    cleanup_scan_dry_run: bool
    cleanup_prune_extra_languages: bool
    cleanup_prune_action: str
    cleanup_prune_special_sidecars: bool
    cleanup_prune_unknown_sidecars: bool
    cleanup_sourceless_line_only_action: str
    cleanup_roots: tuple[Path, ...]
    cleanup_action: str
    cleanup_languages: frozenset[str]
    retention_days: int
    quarantine_artifact_retention_days: int
    regeneration_initial_delay_cycles: int
    regeneration_max_attempts: int
    regeneration_max_delay_cycles: int
    regeneration_backoff_multiplier: float
    donor_recovery_enabled: bool
    retry_batch_size_per_cycle: int
    retry_max_per_series_per_cycle: int
    end_of_cycle_repair_retry_enabled: bool
    retention_check_interval: int
    status_enabled: bool
    status_bind: str
    status_port: int
    status_history_retention_days: int
    status_recent_limit: int
    display_timezone: str
    debug: bool

    @classmethod
    def from_env(cls, values: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if values is None else values
        languages = tuple(
            language.strip().lower()
            for language in env.get("LANGUAGES", "en,et,sv").split(",")
            if language.strip()
        )
        if not languages:
            raise ConfigError("LANGUAGES must contain at least one language code")
        state_dir = Path(env.get("STATE_DIR", "/config").strip() or "/config")
        poll_timeout = _integer(env, "POLL_TIMEOUT", 900, 30)
        retention_days = _integer(env, "RETENTION_DAYS", 30, 1)
        initial_delay = _integer(env, "REGENERATION_INITIAL_DELAY_CYCLES", 2, 1)
        status_port = _integer(env, "STATUS_PORT", 8765, 1)
        if status_port > 65535:
            raise ConfigError("STATUS_PORT must be between 1 and 65535")
        cleanup_roots = tuple(
            Path(value.strip())
            for value in (env.get("CLEANUP_ROOT", "/media").strip() or "/media").split(os.pathsep)
            if value.strip()
        )
        maintenance_workers = _integer(env, "MAINTENANCE_WORKERS", 4, 1)
        if maintenance_workers > 32:
            raise ConfigError("MAINTENANCE_WORKERS must be between 1 and 32")
        return cls(
            bazarr_url=_url(_required(env, "BAZARR_URL")),
            bazarr_api_key=_required(env, "BAZARR_API_KEY"),
            lingarr_url=_url(_required(env, "LINGARR_URL")),
            lingarr_api_key=env.get("LINGARR_API_KEY", "").strip(),
            languages=languages,
            parallel_translates=_integer(env, "PARALLEL_TRANSLATES", 1, 1),
            maintenance_workers=maintenance_workers,
            check_interval=_integer(env, "CHECK_INTERVAL", 1200, 10),
            connect_timeout=_integer(env, "CONNECT_TIMEOUT", 10, 5),
            poll_interval=_integer(env, "POLL_INTERVAL", 20, 5),
            poll_timeout=poll_timeout,
            repair_shutdown_grace_seconds=_integer(
                env, "REPAIR_SHUTDOWN_GRACE_SECONDS", 30, 1
            ),
            state_dir=state_dir,
            quarantine_dir=Path(
                env.get("CLEANUP_QUARANTINE_DIR", f"{state_dir}/quarantine")
            ),
            log_dir=Path(env.get("LOG_DIR", "/var/log/bazarr-autotranslate")),
            status_manual_actions_enabled=_boolean(
                env, "STATUS_MANUAL_ACTIONS_ENABLED", True
            ),
            translation_timeout_multiplier=_float(env, "TRANSLATION_TIMEOUT_MULTIPLIER", 1.25, 1.0),
            translation_timeout_cap=max(poll_timeout, _integer(env, "TRANSLATION_TIMEOUT_CAP", 10800, 1)),
            translation_cold_seconds_per_cue=_float(env, "TRANSLATION_COLD_SECONDS_PER_CUE", 1.8, 0.01),
            translation_timing_alpha=_float(env, "TRANSLATION_TIMING_ALPHA", 0.20, 0.01, 1.0),
            long_job_threshold=_integer(env, "LONG_JOB_THRESHOLD", 1800, 60),
            repair_timeout_multiplier=_float(env, "REPAIR_TIMEOUT_MULTIPLIER", 2.0, 1.0),
            circuit_failure_threshold=_integer(env, "CIRCUIT_FAILURE_THRESHOLD", 3, 1),
            circuit_open_cycles=_integer(env, "CIRCUIT_OPEN_CYCLES", 3, 1),
            resubmit_cooldown=_integer(env, "RESUBMIT_COOLDOWN", 3600, 60),
            sync_timeout=_integer(env, "SYNC_TIMEOUT", 600, 30),
            sync_poll_interval=_integer(env, "SYNC_POLL_INTERVAL", 15, 5),
            sync_start_timeout=_integer(env, "SYNC_START_TIMEOUT", 30, 5),
            cleanup_min_confidence=_float(env, "CLEANUP_MIN_CONFIDENCE", .70),
            cleanup_min_chars=_integer(env, "CLEANUP_MIN_CHARS", 200, 0),
            cleanup_max_unique_ratio=_float(env, "CLEANUP_MAX_UNIQUE_RATIO", .15),
            cleanup_max_cyrillic_ratio=_float(env, "CLEANUP_MAX_CYRILLIC_RATIO", .05),
            cleanup_max_cjk_ratio=_float(env, "CLEANUP_MAX_CJK_RATIO", .05),
            cleanup_max_latin_ratio=_float(env, "CLEANUP_MAX_LATIN_RATIO", .80),
            cleanup_min_letters_for_script=_integer(env, "CLEANUP_MIN_LETTERS_FOR_SCRIPT", 20, 0),
            cleanup_max_cue_lines=_integer(env, "CLEANUP_MAX_CUE_LINES", 4, 1),
            cleanup_max_cue_chars=_integer(env, "CLEANUP_MAX_CUE_CHARS", 500, 50),
            cleanup_max_expansion_ratio=_float(env, "CLEANUP_MAX_EXPANSION_RATIO", 4.0, 1.0),
            cleanup_max_expansion_chars=_integer(env, "CLEANUP_MAX_EXPANSION_CHARS", 300, 50),
            cleanup_max_source_similarity=_float(env, "CLEANUP_MAX_SOURCE_SIMILARITY", .92, .5, 1.0),
            cleanup_repair_enabled=_boolean(env, "CLEANUP_REPAIR_ENABLED", True),
            cleanup_max_repair_attempts=_integer(env, "CLEANUP_MAX_REPAIR_ATTEMPTS", 5, 1),
            cleanup_repair_context_lines=_integer(env, "CLEANUP_REPAIR_CONTEXT_LINES", 5, 0),
            cleanup_format_repair_enabled=_boolean(env, "CLEANUP_FORMAT_REPAIR_ENABLED", True),
            cleanup_repair_queue_max=_integer(env, "CLEANUP_REPAIR_QUEUE_MAX", 100, 1),
            cleanup_undersized_enabled=_boolean(env, "CLEANUP_UNDERSIZED_ENABLED", True),
            cleanup_min_media_duration=_float(env, "CLEANUP_MIN_MEDIA_DURATION", 900),
            cleanup_min_cues_per_minute=_float(env, "CLEANUP_MIN_CUES_PER_MINUTE", 1.5),
            cleanup_min_text_chars_per_minute=_float(env, "CLEANUP_MIN_TEXT_CHARS_PER_MINUTE", 40),
            cleanup_min_bytes_per_minute=_float(env, "CLEANUP_MIN_BYTES_PER_MINUTE", 100),
            cleanup_min_timeline_coverage=_float(env, "CLEANUP_MIN_TIMELINE_COVERAGE", .60, 0, 1),
            cleanup_undersized_required_signals=min(4, _integer(env, "CLEANUP_UNDERSIZED_REQUIRED_SIGNALS", 3, 1)),
            cleanup_ffprobe_timeout=_integer(env, "CLEANUP_FFPROBE_TIMEOUT", 15, 1),
            cleanup_scan_existing=_boolean(env, "CLEANUP_SCAN_EXISTING", True),
            cleanup_scan_interval=_integer(env, "CLEANUP_SCAN_INTERVAL", 21600, 300),
            cleanup_scan_dry_run=_boolean(env, "CLEANUP_SCAN_DRY_RUN", False),
            cleanup_prune_extra_languages=_boolean(env, "CLEANUP_PRUNE_EXTRA_LANGUAGES", True),
            cleanup_prune_action=_choice(env, "CLEANUP_PRUNE_ACTION", "quarantine", {"quarantine", "delete", "report"}),
            cleanup_prune_special_sidecars=_boolean(env, "CLEANUP_PRUNE_SPECIAL_SIDECARS", True),
            cleanup_prune_unknown_sidecars=_boolean(env, "CLEANUP_PRUNE_UNKNOWN_SIDECARS", False),
            cleanup_sourceless_line_only_action=_choice(env, "CLEANUP_SOURCELESS_LINE_ONLY_ACTION", "warn", {"warn", "quarantine"}),
            cleanup_roots=cleanup_roots,
            cleanup_action=_choice(env, "CLEANUP_ACTION", "quarantine", {"quarantine", "delete", "report"}),
            cleanup_languages=frozenset(value.strip() for value in env.get("CLEANUP_LANGUAGES", "et").split(",") if value.strip()),
            retention_days=retention_days,
            quarantine_artifact_retention_days=_integer(env, "QUARANTINE_ARTIFACT_RETENTION_DAYS", retention_days, 1),
            regeneration_initial_delay_cycles=initial_delay,
            regeneration_max_attempts=_integer(env, "REGENERATION_MAX_ATTEMPTS", 0, 0),
            regeneration_max_delay_cycles=max(initial_delay, _integer(env, "REGENERATION_MAX_DELAY_CYCLES", 16, 1)),
            regeneration_backoff_multiplier=_float(env, "REGENERATION_BACKOFF_MULTIPLIER", 2.0, 1.0),
            donor_recovery_enabled=_boolean(env, "DONOR_RECOVERY_ENABLED", True),
            retry_batch_size_per_cycle=_integer(env, "RETRY_BATCH_SIZE_PER_CYCLE", 5, 1),
            retry_max_per_series_per_cycle=_integer(env, "RETRY_MAX_PER_SERIES_PER_CYCLE", 1, 1),
            end_of_cycle_repair_retry_enabled=_boolean(env, "END_OF_CYCLE_REPAIR_RETRY_ENABLED", True),
            retention_check_interval=_integer(env, "RETENTION_CHECK_INTERVAL", 3600, 300),
            status_enabled=_boolean(env, "STATUS_ENABLED", True),
            status_bind=env.get("STATUS_BIND", "0.0.0.0").strip() or "0.0.0.0",
            status_port=status_port,
            status_history_retention_days=_integer(env, "STATUS_HISTORY_RETENTION_DAYS", 30, 7),
            status_recent_limit=_integer(env, "STATUS_RECENT_LIMIT", 20, 1),
            display_timezone=env.get("TZ", "UTC").strip() or "UTC",
            debug=_boolean(env, "DEBUG", False),
        )
