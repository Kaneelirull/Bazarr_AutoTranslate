from __future__ import annotations

import hashlib
import re
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence
from .cues import CueReviewMixin


class ManualReviewNotFound(LookupError):
    pass


class ManualReviewConflict(RuntimeError):
    pass


class ManualReviewDisabled(PermissionError):
    pass


class ManualReviewUnavailable(RuntimeError):
    pass


class ManualReviewRepository(Protocol):
    def manual_review_plan(self, plan_id: int) -> dict | None: ...
    def manual_review_page(self, query: dict) -> dict: ...
    def manual_review_actions(self, plan_id: int) -> list[dict]: ...
    def manual_review_action_count(self, plan_id: int) -> int: ...
    def manual_review_actions_for_plans(self, plan_ids: list[int], limit: int = 100) -> tuple[dict[int, list[dict]], dict[int, int]]: ...
    def manual_review_scan_states(self, plan_ids: list[int]) -> dict[int, dict]: ...
    def queue_manual_retry(self, plan_id: int, expected_updated_at: float, completed_cycle: int) -> dict: ...
    def dismiss_manual_review(self, plan_id: int, expected_updated_at: float) -> dict: ...
    def record_manual_recheck(self, plan_id: int, expected_updated_at: float, *, valid: bool, reason_code: str, details: dict | None = None) -> dict: ...
    def resolve_manual_recheck(self, plan_id: int, expected_updated_at: float, *, reason_code: str, details: dict, validation_record: dict, quarantine_identity: str | None, quarantine_hash: str | None) -> dict: ...
    def record_manual_scan_outcome(self, plan_id: int, *, lease_owner: str, outcome: str, reason_code: str | None = None) -> int: ...
    def claim_manual_scans(self, limit: int = 10) -> list[dict]: ...
    def recovery_summary(self, item_type: str, item_id: int, target_language: str) -> dict: ...
    def recovery_summaries(self, identities: list[tuple[str, int, str]]) -> dict: ...


@dataclass(frozen=True)
class RecheckResult:
    valid: bool
    result: str
    reason_code: str
    report: Any = None
    details: dict[str, Any] = field(default_factory=dict)


_EPISODE_RE = re.compile(r"(?i)\bS\d{1,3}E\d{1,4}\b")
_WINDOWS_PATH_RE = re.compile(r"(?i)\b[A-Z]:[\\/][^\r\n,;]+")
_UNIX_PATH_RE = re.compile(r"(?<!\w)/(?:media|config)/[^\s,;]+")
_SECRET_RE = re.compile(r"(?i)(api[-_ ]?key|authorization)(\s*[:=]\s*)\S+")
_SAFE_CODE_RE = re.compile(r"[a-z][a-z0-9_:-]{0,79}")
_SAFE_ACTIONS = {"recheck", "queue_retry", "dismiss", "bazarr_scan", "approve_name", "revoke_name"}
_SAFE_OUTCOMES = {"invalid", "resolved", "queued", "dismissed", "pending", "failed", "dispatched"}
_SAFE_DETAIL_KEYS = {
    "validationResult", "validationMode", "issueRules", "observationCount",
    "sourceAvailable", "targetAvailable", "artifactAvailable", "mediaAvailable",
    "scanPending", "completeness",
}
_SAFE_COMPLETENESS_KEYS = {
    "evaluated", "undersized", "reason", "mediaDurationSeconds", "subtitleBytes",
    "cueCount", "dialogueChars", "cuesPerMinute", "textCharsPerMinute",
    "bytesPerMinute", "timelineCoverage", "failedSignals", "thresholds",
}
_SAFE_THRESHOLD_KEYS = {
    "minMediaDurationSeconds", "minCuesPerMinute", "minTextCharsPerMinute",
    "minBytesPerMinute", "minTimelineCoverage", "requiredSignals",
}


def _safe_completeness(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key not in _SAFE_COMPLETENESS_KEYS:
            continue
        if key == "failedSignals" and isinstance(item, (list, tuple)):
            result[key] = [
                code for signal in item[:10]
                if _SAFE_CODE_RE.fullmatch(code := str(signal)[:80])
            ]
        elif key == "thresholds" and isinstance(item, dict):
            result[key] = {
                threshold: amount for threshold, amount in item.items()
                if threshold in _SAFE_THRESHOLD_KEYS
                and (amount is None or isinstance(amount, (bool, int, float)))
            }
        elif item is None or isinstance(item, (bool, int, float)):
            result[key] = item
        elif key == "reason" and isinstance(item, str):
            result[key] = item[:120]
    return result


def _hash_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _safe_details(values: dict | None) -> dict:
    result: dict[str, Any] = {}
    for key, value in (values or {}).items():
        if key not in _SAFE_DETAIL_KEYS:
            continue
        if key == "issueRules" and isinstance(value, (list, tuple)):
            result[key] = [
                code for item in value[:20]
                if _SAFE_CODE_RE.fullmatch(code := str(item)[:80])
            ]
        elif key == "completeness":
            completeness = _safe_completeness(value)
            if completeness is not None:
                result[key] = completeness
        elif value is None or isinstance(value, (bool, int, float)):
            result[key] = value
        elif isinstance(value, str):
            result[key] = value[:120]
    return result


def _safe_text(value: object, limit: int = 500) -> str:
    text = _SECRET_RE.sub(r"\1\2<redacted>", str(value or ""))
    text = _WINDOWS_PATH_RE.sub("<managed-path>", text)
    text = _UNIX_PATH_RE.sub("<managed-path>", text)
    return text[:limit]


class ManualReviewService(CueReviewMixin):
    """Operator-facing recovery workflow with no HTTP or runtime-global coupling."""

    def __init__(
        self,
        repository: ManualReviewRepository,
        *,
        managed_roots: Sequence[str | Path],
        quarantine_root: str | Path | None,
        artifact_access: Any = None,
        validate: Callable[[dict, Path | None, Path, Path | None], RecheckResult],
        resolve_media: Callable[[dict, Path], str | Path | None] | None = None,
        prepare_validation: Callable[[dict, RecheckResult, Path | None, Path], dict] | None = None,
        quarantine_identity: Callable[[dict], tuple[str | None, str | None]] | None = None,
        publish_validation: Callable[[dict, RecheckResult], None] | None = None,
        dispatch_scan: Callable[[dict], bool] | None = None,
        completed_cycle: Callable[[], int] = lambda: 0,
        on_change: Callable[[], None] | None = None,
        actions_enabled: bool = True,
        hash_file: Callable[[Path], str | None] = _hash_file,
        emit: Callable[[str], None] = lambda _message: None,
        inspect_cues=None,
    ) -> None:
        self.repository = repository
        roots = [Path(root).resolve(strict=False) for root in managed_roots]
        if quarantine_root is not None:
            roots.append(Path(quarantine_root).resolve(strict=False))
        self.managed_roots = tuple(dict.fromkeys(roots))
        self.artifact_access = artifact_access
        self.validate = validate
        self.resolve_media = resolve_media
        self.prepare_validation = prepare_validation or self._default_validation_record
        self.quarantine_identity = quarantine_identity
        self.publish_validation = publish_validation
        self.dispatch_scan = dispatch_scan
        self.completed_cycle = completed_cycle
        self.on_change = on_change
        self.actions_enabled = bool(actions_enabled)
        self.hash_file = hash_file
        self.emit = emit
        self.inspect_cues = inspect_cues

    def _default_validation_record(
        self, plan: dict, result: RecheckResult,
        source_path: Path | None, target_path: Path,
    ) -> dict:
        report = result.report.to_dict() if hasattr(result.report, "to_dict") else {}
        return {
            "target_path": target_path,
            "source_hash": self.hash_file(source_path) if source_path is not None else None,
            "target_hash": self.hash_file(target_path),
            "result": result.result,
            "origin": "manual_recheck",
            "details": {"validation": report, **result.details},
            "source_path": source_path,
            "source_language": plan.get("sourceLanguage"),
            "target_language": plan.get("targetLanguage"),
            "operation": "manual_recheck",
            "validation_mode": "source-aware" if source_path is not None else "target-only",
            "item_type": plan.get("itemType"),
            "item_id": plan.get("itemId"),
        }

    @staticmethod
    def _status(plan: dict) -> str:
        if plan.get("state") == "manual_dismissed":
            return "dismissed"
        if plan.get("state") in {
            "accepted_after_manual_recheck", "accepted_after_retry",
            "accepted_after_donor_recovery", "superseded",
        }:
            return "resolved"
        if (
            plan.get("state") == "regeneration_waiting"
            and plan.get("lastDeferralClass") == "manual_review"
        ):
            return "needs_attention"
        return "manually_queued"

    def _managed_path(self, raw: object) -> tuple[Path | None, str | None]:
        path, relative, _reason = self._managed_path_status(raw)
        return path, relative

    def _managed_path_status(
        self, raw: object
    ) -> tuple[Path | None, str | None, str]:
        if not raw:
            return None, None, "not_found"
        candidate = Path(str(raw)).resolve(strict=False)
        for root in self.managed_roots:
            try:
                relative = candidate.relative_to(root)
            except ValueError:
                continue
            return (
                candidate,
                relative.as_posix(),
                "available" if candidate.is_file() else "not_found",
            )
        return None, None, "outside_managed_root"

    def _media_path(
        self, plan: dict, target: Path | None
    ) -> tuple[Path | None, str | None, str]:
        if target is None:
            return None, None, "not_found"
        if self.resolve_media is None:
            return None, None, "resolver_unavailable"
        try:
            raw = self.resolve_media(plan, target)
        except (LookupError, OSError, RuntimeError, TypeError, ValueError):
            self.emit("[MANUAL REVIEW] Media resolver unavailable")
            return None, None, "resolver_unavailable"
        if raw is None:
            return None, None, "not_found"
        media, relative = self._managed_path(raw)
        if media is None:
            return None, None, "outside_managed_root"
        return media, relative, "available" if media.is_file() else "not_found"

    def _safe_operator_text(
        self, value: object, fallback: str | None = None, *, limit: int = 500
    ) -> str | None:
        text = _safe_text(value, limit)
        if not text:
            return fallback
        if any(character in text for character in ("/", "\\", "\r", "\n")):
            return fallback
        return text

    def _identity(self, plan: dict) -> dict:
        title = plan.get("seriesTitle") or plan.get("mediaTitle") or (
            "Movie" if plan.get("itemType") == "movies" else "Episode"
        )
        episode = None
        for value in (plan.get("mediaTitle"), plan.get("targetPath"), plan.get("sourcePath")):
            match = _EPISODE_RE.search(str(value or ""))
            if match:
                episode = match.group(0).upper()
                break
        return {
            "title": self._safe_operator_text(title, "Media item", limit=160),
            "episodeCode": episode,
        }

    def _public_action(self, action: dict) -> dict:
        raw_reason = str(action.get("reasonCode") or "")[:80]
        return {
            "action": action.get("action") if action.get("action") in _SAFE_ACTIONS else None,
            "outcome": action.get("outcome") if action.get("outcome") in _SAFE_OUTCOMES else None,
            "reasonCode": raw_reason if re.fullmatch(r"[a-z0-9_:-]+", raw_reason) else None,
            "details": _safe_details(action.get("details")),
            "createdAt": action.get("createdAt"),
        }

    def _public_item(
        self, plan: dict, *, actions: list[dict] | None = None,
        action_count: int | None = None, recovery: dict | None = None,
        scan: dict | None = None,
    ) -> dict:
        source, source_relative, source_reason = self._managed_path_status(plan.get("sourcePath"))
        target, target_relative, target_reason = self._managed_path_status(plan.get("targetPath"))
        artifact, artifact_relative, artifact_reason = self._managed_path_status(plan.get("artifactPath"))
        media, media_relative, media_reason = self._media_path(plan, target)
        status = self._status(plan)
        raw_actions = actions if actions is not None else self.repository.manual_review_actions(int(plan["id"]))
        actions = [self._public_action(action) for action in raw_actions]
        if action_count is None:
            action_count = self.repository.manual_review_action_count(int(plan["id"]))
        if recovery is None:
            try:
                recovery = self.repository.recovery_summary(
                    str(plan.get("itemType")), int(plan.get("itemId")),
                    str(plan.get("targetLanguage")),
                )
            except (AttributeError, LookupError, OSError, RuntimeError, TypeError, ValueError):
                recovery = {}
        latest_recheck = next(
            (action for action in actions if action.get("action") == "recheck"), None
        )
        if scan is None:
            scan = self.repository.manual_review_scan_states([int(plan["id"])]).get(
                int(plan["id"])
            )
        latest_scan = next(
            (action for action in actions if action.get("action") == "bazarr_scan"), None
        )
        scan_state = scan.get("state") if scan is not None else (
            latest_scan.get("outcome") if latest_scan is not None else None
        )
        scan_pending = scan_state in {"pending", "claimed", "failed"}
        identity = self._identity(plan)
        allowed = ["recheck", "queue_retry", "dismiss"] if status == "needs_attention" else []
        return {
            "id": int(plan["id"]),
            "media": identity,
            "itemType": plan.get("itemType"),
            "itemId": plan.get("itemId"),
            "targetLanguage": plan.get("targetLanguage"),
            "sourceLanguage": plan.get("sourceLanguage"),
            "status": status,
            "state": plan.get("state"),
            "attemptCount": int(plan.get("attemptCount") or 0),
            "failureClass": self._safe_operator_text(plan.get("failureClass"), limit=80),
            "failureRules": [str(rule)[:80] for rule in (plan.get("rules") or [])[:20]],
            "lastReason": self._safe_operator_text(
                plan.get("lastReason"), "Additional details are available in service logs.", limit=500
            ),
            "sourceAvailable": bool(source and source.is_file()),
            "targetAvailable": bool(target and target.is_file()),
            "artifactAvailable": bool(artifact and artifact.is_file()),
            "mediaAvailable": bool(media and media.is_file()),
            "sourceAvailabilityReason": source_reason,
            "targetAvailabilityReason": target_reason,
            "artifactAvailabilityReason": artifact_reason,
            "mediaAvailabilityReason": media_reason,
            "sourceRelativePath": source_relative,
            "targetRelativePath": target_relative,
            "artifactRelativePath": artifact_relative,
            "mediaRelativePath": media_relative,
            "recovery": {
                "validRecoveredCueCount": int(recovery.get("validRecoveredCueCount") or 0),
                "unresolvedCueCount": int(recovery.get("unresolvedCueCount") or 0),
                "latestRecoveryStage": recovery.get("latestRecoveryStage"),
            },
            "validationFeedback": ({
                "outcome": latest_recheck.get("outcome"),
                "reasonCode": latest_recheck.get("reasonCode"),
                **latest_recheck.get("details", {}),
            } if latest_recheck is not None else None),
            "scanPending": scan_pending,
            "scanState": scan_state,
            "allowedActions": allowed,
            "actions": actions,
            "actionCount": int(action_count or 0),
            "actionsTruncated": int(action_count or 0) > len(actions),
            "updatedAt": float(plan.get("updatedAt") or 0),
        }

    def list_reviews(self, query: dict[str, Any] | None = None) -> dict:
        values = query or {}
        page_data = self.repository.manual_review_page(values)
        plans = page_data["plans"]
        plan_ids = [int(plan["id"]) for plan in plans]
        actions_by_plan, action_counts = self.repository.manual_review_actions_for_plans(plan_ids)
        scans_by_plan = self.repository.manual_review_scan_states(plan_ids)
        identities = [
            (str(plan.get("itemType")), int(plan.get("itemId")), str(plan.get("targetLanguage")))
            for plan in plans
        ]
        recoveries = self.repository.recovery_summaries(identities)
        items = [
            self._public_item(
                plan,
                actions=actions_by_plan.get(int(plan["id"]), []),
                action_count=action_counts.get(int(plan["id"]), 0),
                recovery=recoveries.get(identity, {}),
                scan=scans_by_plan.get(int(plan["id"])),
            )
            for plan, identity in zip(plans, identities)
        ]
        counts = page_data["counts"]
        return {
            "counts": {
                "needsAttention": counts["needs_attention"],
                "manuallyQueued": counts["manually_queued"],
                "resolved": counts["resolved"],
                "dismissed": counts["dismissed"],
            },
            "items": items,
            "pagination": {
                "page": page_data["page"], "pageSize": page_data["pageSize"],
                "total": page_data["total"],
            },
            "actionsEnabled": self.actions_enabled,
        }

    def _require_enabled(self) -> None:
        if not self.actions_enabled:
            raise ManualReviewDisabled("manual review actions are disabled")

    @staticmethod
    def _translate_repository_error(exc: Exception) -> Exception:
        if type(exc).__name__ == "StateStoreError":
            return ManualReviewUnavailable("manual review persistence unavailable")
        if isinstance(exc, LookupError):
            return ManualReviewNotFound(str(exc))
        if isinstance(exc, RuntimeError):
            return ManualReviewConflict(str(exc))
        return exc

    def perform_action(
        self, plan_id: int, action: str, expected_updated_at: float
    ) -> tuple[int, dict]:
        self._require_enabled()
        if action not in {"recheck", "queue_retry", "dismiss"}:
            raise ValueError("unsupported action")
        if action == "queue_retry":
            try:
                plan = self.repository.queue_manual_retry(
                    plan_id, expected_updated_at, self.completed_cycle()
                )
            except (LookupError, RuntimeError) as exc:
                raise self._translate_repository_error(exc) from exc
            self._notify_change(plan)
            return 200, {"outcome": "queued", "item": self._public_item(plan)}
        if action == "dismiss":
            try:
                plan = self.repository.dismiss_manual_review(plan_id, expected_updated_at)
            except (LookupError, RuntimeError) as exc:
                raise self._translate_repository_error(exc) from exc
            self._notify_change(plan)
            return 200, {"outcome": "dismissed", "item": self._public_item(plan)}
        return self._recheck(plan_id, expected_updated_at)

    def _recheck(self, plan_id: int, expected_updated_at: float) -> tuple[int, dict]:
        plan = self.repository.manual_review_plan(plan_id)
        if plan is None:
            raise ManualReviewNotFound("manual review not found")
        if abs(float(plan.get("updatedAt") or 0) - float(expected_updated_at)) > 0.000001:
            raise ManualReviewConflict("manual review changed")
        if self._status(plan) != "needs_attention":
            raise ManualReviewConflict("manual review is no longer awaiting action")
        target, _relative = self._managed_path(plan.get("targetPath"))
        source, _source_relative = self._managed_path(plan.get("sourcePath"))
        artifact, _artifact_relative = self._managed_path(plan.get("artifactPath"))
        media, _media_relative, _media_reason = self._media_path(plan, target)
        access = (
            self.artifact_access.hold(source, target, artifact, media)
            if self.artifact_access is not None else nullcontext()
        )
        with access, (self.repository.approval_guard() if hasattr(self.repository, "approval_guard") else nullcontext()):
            if target is None or not target.is_file():
                try:
                    current = self.repository.record_manual_recheck(
                        plan_id, expected_updated_at, valid=False,
                        reason_code="target_unavailable",
                        details={"targetAvailable": False},
                    )
                except (LookupError, RuntimeError) as exc:
                    raise self._translate_repository_error(exc) from exc
                self._notify_change(current)
                return 200, {"outcome": "invalid", "item": self._public_item(current)}
            trusted_source = source if (
                source is not None and source.is_file()
                and self.hash_file(source) == plan.get("sourceHash")
            ) else None
            result = self.validate(plan, trusted_source, target, media)
            details = _safe_details({
                **result.details,
                "validationResult": result.result,
                "validationMode": "source-aware" if trusted_source else "target-only",
                "sourceAvailable": bool(source and source.is_file()),
                "targetAvailable": True,
                "artifactAvailable": bool(artifact and artifact.is_file()),
                "mediaAvailable": bool(media and media.is_file()),
            })
            if not result.valid:
                try:
                    current = self.repository.record_manual_recheck(
                        plan_id, expected_updated_at, valid=False,
                        reason_code=result.reason_code, details=details,
                    )
                except (LookupError, RuntimeError) as exc:
                    raise self._translate_repository_error(exc) from exc
                self._notify_change(current)
                return 200, {"outcome": "invalid", "item": self._public_item(current)}
            validation_record = self.prepare_validation(
                plan, result, trusted_source, target
            )
            quarantine_identity, quarantine_hash = (
                self.quarantine_identity(plan)
                if self.quarantine_identity is not None else (None, None)
            )
            try:
                current = self.repository.resolve_manual_recheck(
                    plan_id, expected_updated_at,
                    reason_code=result.reason_code,
                    details=details,
                    validation_record=validation_record,
                    quarantine_identity=quarantine_identity,
                    quarantine_hash=quarantine_hash,
                )
            except (LookupError, RuntimeError) as exc:
                raise self._translate_repository_error(exc) from exc
            if self.publish_validation is not None:
                try:
                    self.publish_validation(plan, result)
                except Exception:
                    self._emit_failure("observation_publish_failed", current)
        scan_pending = not self._dispatch_one(current)
        self._notify_change(current)
        payload = {
            "outcome": "resolved", "scanPending": scan_pending,
            "item": self._public_item(current),
        }
        return (202 if scan_pending else 200), payload

    def _dispatch_one(self, plan: dict) -> bool:
        lease_owner = str(plan.get("scanLeaseOwner") or "")
        if not lease_owner:
            self._emit_failure("scan_lease_missing", plan)
            return False
        if self.dispatch_scan is None:
            try:
                self.repository.record_manual_scan_outcome(
                    int(plan["id"]), lease_owner=lease_owner,
                    outcome="failed", reason_code="scan_unavailable"
                )
            except (LookupError, OSError, RuntimeError, TypeError, ValueError):
                self._emit_failure("scan_outcome_persist_failed", plan)
            return False
        try:
            accepted = bool(self.dispatch_scan(plan))
        except Exception:
            self._emit_failure("scan_dispatch_failed", plan)
            accepted = False
        try:
            self.repository.record_manual_scan_outcome(
                int(plan["id"]), lease_owner=lease_owner,
                outcome="dispatched" if accepted else "failed",
                reason_code=None if accepted else "scan_dispatch_failed",
            )
        except (LookupError, OSError, RuntimeError, TypeError, ValueError):
            self._emit_failure("scan_outcome_persist_failed", plan)
            return False
        return accepted

    def _emit_failure(self, code: str, plan: dict) -> None:
        safe_code = code if _SAFE_CODE_RE.fullmatch(code) else "manual_review_failure"
        try:
            plan_id = int(plan.get("id") or 0)
        except (TypeError, ValueError):
            plan_id = 0
        try:
            self.emit(
                f"[MANUAL REVIEW] event={safe_code} plan_id={plan_id}"
            )
        except Exception:
            return

    def _notify_change(self, plan: dict) -> None:
        if self.on_change is None:
            return
        try:
            self.on_change()
        except Exception:
            self._emit_failure("status_publish_failed", plan)

    def dispatch_pending_scans(
        self, limit: int = 10, *, now: float | None = None
    ) -> dict:
        plans = self.repository.claim_manual_scans(
            limit=min(10, limit), now=now
        )
        dispatched = sum(1 for plan in plans if self._dispatch_one(plan))
        return {"examined": len(plans), "dispatched": dispatched}
