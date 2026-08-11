from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class LifecyclePhase(str, Enum):
    STARTUP_WAIT = "startup_wait"
    STARTUP_SYNC = "startup_sync"
    STARTUP_CLEANUP = "startup_cleanup"
    CYCLE_WORK = "cycle_work"
    RETRY_RECOVERY = "retry_recovery"
    REPAIR_DRAIN = "repair_drain"
    POST_CYCLE_MAINTENANCE = "post_cycle_maintenance"
    COOLDOWN = "cooldown"
    SHUTDOWN = "shutdown"


class ValidationLevel(str, Enum):
    CUE_CANDIDATE_VALID = "cue_candidate_valid"
    PARTIAL_FILE_IMPROVED = "partial_file_improved"
    COMPLETE_FILE_VALID = "complete_file_valid"
    PUBLISHED = "published"


class RecoveryStage(str, Enum):
    FORMAT = "format"
    CONTEXTUAL = "contextual"
    CONTEXT_FREE = "context_free"
    DONOR = "donor"
    STRICT_ISOLATED = "strict_isolated"
    ALTERNATE_PROVIDER = "alternate_provider"
    PARTIAL_ARCHIVE = "partial_archive"
    FULL_REGENERATION = "full_regeneration"
    MANUAL_REVIEW = "manual_review"


class RetryAdmissionClass(str, Enum):
    EXAMINED = "examined"
    RECONCILED = "reconciled"
    TRANSLATION_REQUIRED = "translation_required"
    SUBMITTED = "submitted"
    NO_PROGRESS = "no_progress"


class RepairJobState(str, Enum):
    QUEUED = "queued"
    ACTIVE = "active"
    CANCELLED_BEFORE_START = "cancelled_before_start"
    INTERRUPTED = "interrupted"
    PERSISTED_FOR_RESTART = "persisted_for_restart"
    COMPLETED = "completed"
    FAILED = "failed"


class RepairOutcomeKind(str, Enum):
    PUBLISHED = "published"
    PARTIAL_ARCHIVED = "partial_archived"
    DEFERRED = "deferred"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ProviderFailureKind(str, Enum):
    TRANSPORT = "transport"
    HTTP_RETRYABLE = "http_retryable"
    HTTP_PERMANENT = "http_permanent"
    MALFORMED_RESPONSE = "malformed_response"
    INVALID_TRANSLATION = "invalid_translation"
    CANCELLED = "cancelled"


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    ELIGIBLE = "eligible"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class WorkItem:
    item_type: str
    item_id: int
    target_language: str
    title: str = ""


@dataclass(frozen=True)
class CycleResult:
    cycle_number: int
    healthy: bool
    degraded: bool = False
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MaintenanceResult:
    healthy: bool
    attempted: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)


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
    retry_plan_id: int | None = None
    expected_source_hash: str | None = None


@dataclass(frozen=True)
class RetryOutcome:
    plan_id: int
    classification: RetryAdmissionClass
    reason: str | None = None


@dataclass(frozen=True)
class RepairOutcome:
    kind: RepairOutcomeKind
    changed_cues: tuple[int, ...] = ()
    unresolved_cues: tuple[int, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class CueRecovery:
    cue_number: int
    target_hash: str
    stage: RecoveryStage
    source_attempt_id: int | None = None


@dataclass(frozen=True)
class CircuitDecision:
    allowed: bool
    state: CircuitState
    trial_owner: str | None = None
    reason: str | None = None
