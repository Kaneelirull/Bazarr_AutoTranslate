from __future__ import annotations

from ..composition import runtime as _runtime
from . import ManualReviewService, RecheckResult


def _manual_review_validate(plan: dict, source_path, target_path, media_path) -> RecheckResult:
    from ..subtitles.foundation import (
        ValidationIssue,
        ValidationReport,
        completeness_issue,
        target_language_for_code,
        validate_subtitle_file,
        validate_subtitle_pair,
    )

    detector = _runtime._get_cleanup_detector()
    target_language = target_language_for_code(plan.get("targetLanguage") or "")
    if detector is None or target_language is None:
        return RecheckResult(False, "invalid", "validator_unavailable")
    if source_path is not None:
        report = validate_subtitle_pair(
            source_path, target_path, detector, target_language,
            target_lang=plan["targetLanguage"], **_runtime._validation_kwargs(plan),
        )
        mode = "source-aware"
    else:
        report = ValidationReport()
        valid, reason = validate_subtitle_file(
            target_path, detector, target_language,
            target_lang=plan["targetLanguage"],
            **{
                key: value for key, value in _runtime._validation_kwargs().items()
                if key in {
                    "min_chars", "min_confidence", "max_unique_ratio",
                    "max_cyrillic_ratio", "max_cjk_ratio", "max_latin_ratio",
                    "min_letters_for_script",
                }
            },
        )
        if not valid:
            report.issues.append(ValidationIssue("target_language", reason))
        mode = "target-only"
    completeness = None
    if media_path is not None:
        completeness = _runtime._evaluate_completeness(
            target_path, _runtime._probe_media_duration(media_path)
        )
        issue = completeness_issue(completeness) if completeness is not None else None
        if issue is not None:
            report.issues.append(issue)
    result = "valid_with_warnings" if report.valid and report.observations else (
        "valid" if report.valid else "invalid"
    )
    rules = [issue.rule for issue in report.issues]
    reason_code = "validation_passed" if report.valid else (rules[0] if rules else "validation_failed")
    return RecheckResult(
        report.valid,
        result,
        reason_code,
        report,
        {
            "validationMode": mode,
            "issueRules": rules,
            "observationCount": len(report.observations),
            "completeness": completeness.to_dict() if completeness is not None else None,
        },
    )


def _prepare_manual_validation(plan, result, source_path, target_path) -> dict:
    from ..subtitles.foundation import VALIDATOR_VERSION

    identity = _runtime.retry_media_identity(plan)
    target_language = plan.get("targetLanguage")
    target_suffix = _runtime._target_suffix(target_path, target_language)
    extra = {
        "approvalRevision": getattr(result.report, "approval_revision", 0),
        "canonicalSeriesKey": plan.get("canonicalSeriesKey"),
        "sourcePath": str(source_path) if source_path is not None else None,
        "sourceLanguage": plan.get("sourceLanguage"),
        "targetLanguage": target_language,
        "itemType": plan.get("itemType"),
        "itemId": plan.get("itemId"),
        "title": identity.get("displayTitle"),
        "episodeCode": identity.get("episodeCode"),
        "operation": "manual_recheck",
        "validationMode": "source-aware" if source_path is not None else "target-only",
        "trustedSource": source_path is not None,
        "completeness": result.details.get("completeness"),
    }
    return {
        "target_path": target_path,
        "source_hash": _runtime._file_hash_or_none(source_path),
        "target_hash": _runtime._file_hash_or_none(target_path),
        "result": result.result,
        "origin": "manual_recheck",
        "details": {"validation": result.report.to_dict(), **extra},
        "source_path": str(source_path) if source_path is not None else None,
        "source_language": plan.get("sourceLanguage"),
        "target_language": target_language,
        "target_identity": _runtime._target_identity_from_sidecar(
            target_path, target_language
        ),
        "target_variant": target_suffix[1] if target_suffix is not None else None,
        "operation": "manual_recheck",
        "validation_mode": extra["validationMode"],
        "validator_version": VALIDATOR_VERSION,
        "item_type": plan.get("itemType"),
        "item_id": plan.get("itemId"),
    }


def _manual_quarantine_identity(plan: dict) -> tuple[str | None, str | None]:
    failed_hash = plan.get("failedOutputHash")
    if not failed_hash:
        return None, None
    return (
        _runtime._quarantine_identity(
            plan.get("targetLanguage") or "", target_path=plan.get("targetPath")
        ),
        str(failed_hash),
    )


def _publish_manual_validation(plan: dict, result: RecheckResult) -> None:
    identity = _runtime.retry_media_identity(plan)
    _runtime._publish_validation_observations(
        result.report,
        plan.get("targetLanguage"),
        title=identity.get("displayTitle"),
        episodeCode=identity.get("episodeCode"),
        itemType=plan.get("itemType"),
        itemId=plan.get("itemId"),
    )


def _dispatch_manual_scan(plan: dict) -> bool:
    series_id = None
    canonical = str(plan.get("canonicalSeriesKey") or plan.get("seriesKey") or "")
    if canonical.startswith("sonarr:"):
        try:
            series_id = int(canonical.split(":", 1)[1])
        except ValueError:
            series_id = None
    return _runtime._bazarr_client().trigger_item_scan(
        str(plan.get("itemType")), int(plan.get("itemId")), series_id=series_id
    )


def _resolve_manual_media(_plan: dict, target_path):
    return _runtime._find_sidecar_video(target_path)


def build_manual_review_service(state_store) -> ManualReviewService:
    return ManualReviewService(
        state_store,
        managed_roots=[*_runtime.CLEANUP_ROOTS, state_store.path.parent / 'recovery'],
        quarantine_root=_runtime.CLEANUP_QUARANTINE_DIR,
        artifact_access=_runtime._artifact_access,
        validate=_manual_review_validate,
        resolve_media=_resolve_manual_media,
        prepare_validation=_prepare_manual_validation,
        quarantine_identity=_manual_quarantine_identity,
        publish_validation=_publish_manual_validation,
        dispatch_scan=_dispatch_manual_scan,
        completed_cycle=lambda: _runtime._completed_cycle,
        on_change=_runtime._refresh_status_diagnostics,
        actions_enabled=_runtime.STATUS_MANUAL_ACTIONS_ENABLED,
        emit=print,
        inspect_cues=_inspect_review_cues,
    )


def _inspect_review_cues(plan, source, candidate, pairs):
    from ..subtitles.foundation import (
        cue_text_hash, parse_srt_cues, read_text_best_effort,
        target_language_for_code, validate_subtitle_pair,
    )
    source_cues, errors = parse_srt_cues(read_text_best_effort(source) or '')
    target_cues, target_errors = parse_srt_cues(read_text_best_effort(candidate) or '')
    if errors or target_errors or len(source_cues) != len(target_cues):
        from .service import ManualReviewConflict
        raise ManualReviewConflict('candidate requires structural recovery before cue review')
    options = _runtime._validation_kwargs()
    options['approved_name_pairs'] = pairs
    report = validate_subtitle_pair(source, candidate, _runtime._get_cleanup_detector(),
        target_language_for_code(plan['targetLanguage']), target_lang=plan['targetLanguage'], **options)
    output = []
    for index, (original, translated) in enumerate(zip(source_cues, target_cues)):
        issues = [i for i in report.issues if i.cue_index == index]
        if not issues:
            continue
        source_text, target_text = '\n'.join(original.lines), '\n'.join(translated.lines)
        output.append({'cueNumber': original.number, 'timestamp': original.timestamp,
            'sourceText': source_text, 'targetText': target_text,
            'sourceCueHash': cue_text_hash(original),
            'targetCueHash': cue_text_hash(translated),
            'rules': sorted({i.rule for i in issues}), 'reason': '; '.join(i.detail for i in issues),
            'canApproveCue': all(i.rule in {'copied_source','ambiguous_copied_source','unexpected_script',
                'excessive_lines','cue_too_long','abnormal_expansion','garbage','prompt_marker'} for i in issues),
            'canApproveName': original.number == translated.number and original.timestamp == translated.timestamp
                and any(i.rule == 'ambiguous_copied_source' for i in issues),
            'context': [{'cueNumber': source_cues[j].number, 'sourceText': '\n'.join(source_cues[j].lines),
                         'targetText': '\n'.join(target_cues[j].lines)} for j in (index-1, index+1) if 0 <= j < len(source_cues)]})
    return output


EXPORTS = {"build_manual_review_service": build_manual_review_service}
