from __future__ import annotations
from ..composition import runtime as _runtime

def _scan_undersized_sidecars(stats: dict) -> bool:
    """Validate regular subtitle density for every language using sibling media duration."""
    if not _runtime.CLEANUP_UNDERSIZED_ENABLED:
        return False
    from ..subtitles.foundation import file_sha256, validate_srt_structure
    from ..subtitles.sources import is_extracted_sidecar
    from .workers import MaintenanceFileStat, ValidationTask
    changed = False
    seen: set[_runtime.Path] = set()
    candidates: list[tuple[_runtime.Path, _runtime.Path, str]] = []
    for root in _runtime.CLEANUP_ROOTS:
        if not root.exists():
            continue
        for subtitle in root.rglob('*.srt'):
            if _runtime.shutdown_requested:
                return changed
            if not subtitle.is_file() or subtitle in seen:
                continue
            seen.add(subtitle)
            video = _runtime._find_sidecar_video(subtitle)
            if video is None:
                continue
            if is_extracted_sidecar(subtitle, video):
                continue
            exempt_token = _runtime._explicit_non_full_sidecar(video, subtitle)
            if exempt_token is not None:
                stats['undersized_forced_exempt'] += 1
                _runtime.dbg(f'Completeness exempt {subtitle.name}: explicit {exempt_token} track')
                continue
            stats['undersized_checked'] += 1
            tokens = _runtime._sidecar_tokens(video, subtitle)
            language = next((token for token in tokens if len(token) in (2, 3) and token.isalpha()), 'unknown')
            candidates.append((subtitle, video, language))

    tasks = [
        ValidationTask(
            sequence=sequence,
            operation='structure',
            target_path=str(subtitle),
            target_language=language,
            video_path=str(video),
            completeness_kwargs=_runtime._completeness_kwargs(),
            ffprobe_timeout=_runtime.CLEANUP_FFPROBE_TIMEOUT,
        )
        for sequence, (subtitle, video, language) in enumerate(candidates)
    ]
    analyses = {
        _maintenance_path_key(result.target_path): result
        for result in _runtime._maintenance_worker_pool.map_ordered(
            tasks, stop_requested=lambda: _runtime.shutdown_requested,
        )
        if result.target_path
    } if tasks and _runtime._maintenance_worker_pool is not None else {}
    stats['tasks_submitted'] += len(tasks) if _runtime._maintenance_worker_pool is not None else 0
    stats['tasks_completed'] += len(analyses)
    stats['worker_failures'] += sum(bool(result.error) for result in analyses.values())

    for subtitle, video, language in candidates:
        if _runtime.shutdown_requested:
            return changed
        prepared = analyses.get(_maintenance_path_key(subtitle))
        current_stat = MaintenanceFileStat.capture(subtitle)
        reusable = bool(
            prepared is not None and prepared.error is None
            and prepared.target_stat == current_stat
        )
        if reusable:
            report = prepared.report
            completeness = prepared.completeness
            subtitle_hash = prepared.target_hash
            if completeness is None:
                duration = _runtime._probe_media_duration(video)
                if duration is None:
                    stats['undersized_duration_unavailable'] += 1
                    continue
                completeness = _runtime._evaluate_completeness(subtitle, duration)
                _runtime._add_completeness_issue(report, completeness)
        else:
            duration = _runtime._probe_media_duration(video)
            if duration is None:
                stats['undersized_duration_unavailable'] += 1
                continue
            report = validate_srt_structure(subtitle)
            completeness = _runtime._evaluate_completeness(subtitle, duration)
            _runtime._add_completeness_issue(report, completeness)
            try:
                subtitle_hash = file_sha256(subtitle)
            except OSError:
                subtitle_hash = None
        structural_invalid = any(
            issue.rule != 'undersized_subtitle' for issue in report.issues
        )
        if structural_invalid:
            _runtime.dbg(f'Completeness deferred {subtitle.name}: structural validation must handle {report.summary()}')
            continue
        if completeness is not None and completeness.undersized:
            stats['undersized_detected'] += 1
            _runtime._status_record_maintenance_outcome('undersized_detection', 'undersized', _runtime._maintenance_file_identity(subtitle, language))
            print(f"{_runtime.YELLOW}[SIZE] Undersized {subtitle.name}: {completeness.cue_count} cues, {completeness.subtitle_bytes} bytes, {completeness.media_duration_seconds / 60:.1f} min, failed={','.join(completeness.failed_signals)}{_runtime.RESET}")
        if report.valid:
            continue
        origin = _runtime._get_validation_state().matching_origin(subtitle, subtitle_hash) if subtitle_hash is not None else None
        action = _runtime._apply_cleanup_action(subtitle, None, language, report, expected_target_hash=subtitle_hash, completeness=completeness, origin=origin, lingarr_outcome='not attempted: whole-file completeness failure', dry_run=_runtime.CLEANUP_SCAN_DRY_RUN)
        if action == 'quarantined':
            if completeness is not None and completeness.undersized:
                stats['undersized_quarantined'] += 1
            else:
                stats['quarantined_files'] += 1
            changed = True
        elif action == 'deleted':
            stats['deleted_files'] += 1
            changed = True
        elif action == 'reported':
            stats['reported_files'] += 1
        elif action == 'dry-run':
            stats['dry_run_files'] += 1
        elif action == 'action-failed':
            stats['action_failures'] += 1
        if action in ('quarantined', 'deleted', 'action-failed'):
            _runtime._status_record_maintenance_outcome('quarantine' if action == 'quarantined' else 'deletion' if action == 'deleted' else 'validation', action if action in ('quarantined', 'deleted') else 'failed', _runtime._maintenance_file_identity(subtitle, language), reason='validation action failed' if action == 'action-failed' else None)
    return changed

def _video_sidecars(video: _runtime.Path) -> list[_runtime.Path]:
    """Return SRTs belonging to exactly this video stem, excluding overlapping names."""
    try:
        return sorted((path for path in video.parent.iterdir() if path.is_file() and path.suffix.casefold() == '.srt' and (_runtime._find_sidecar_video(path) == video)), key=lambda path: path.name.casefold())
    except OSError:
        return []

def _queue_video_for_pruning(video_path: str | _runtime.Path, item_type: str | None=None) -> None:
    key = _runtime.os.path.normcase(_runtime.os.path.abspath(str(video_path)))
    with _runtime._pending_prune_lock:
        _runtime._pending_prune_videos[key] = item_type

def _take_pending_prune_videos() -> list[tuple[_runtime.Path, str | None]]:
    with _runtime._pending_prune_lock:
        pending = [(_runtime.Path(path), item_type) for path, item_type in _runtime._pending_prune_videos.items()]
        _runtime._pending_prune_videos.clear()
    return pending

def _video_has_pending_repair(video: _runtime.Path) -> bool:
    with _runtime._pending_repairs_lock:
        target_paths = [metadata.get('target_path') for metadata in _runtime._pending_repairs.values()]
    return any((target_path and _runtime._find_sidecar_video(target_path) == video for target_path in target_paths))

def _prune_stats() -> dict:
    return {'prune_videos_checked': 0, 'prune_ready': 0, 'prune_deferred': 0, 'prune_missing_languages': 0, 'prune_invalid_languages': 0, 'prune_duration_unavailable': 0, 'prune_retained_unknown': 0, 'prune_candidates': 0, 'prune_quarantined': 0, 'prune_deleted': 0, 'prune_reported': 0, 'prune_failures': 0, 'prune_bazarr_rescan_batches': 0}


def _merge_prune_stats(stats: dict, prune_stats: dict) -> None:
    worker_keys = {'maintenance_workers', 'tasks_submitted', 'tasks_completed', 'worker_failures'}
    for key in worker_keys - {'maintenance_workers'}:
        stats[key] = stats.get(key, 0) + prune_stats.get(key, 0)
    stats.update({
        key: value for key, value in prune_stats.items() if key not in worker_keys
    })


def _maintenance_path_key(path: str | _runtime.Path) -> str:
    return _runtime.os.path.normcase(_runtime.os.path.abspath(str(path)))


def _maintenance_preflight_source(candidate, video):
    """Resolve a read-only source candidate before worker submission."""
    from ..subtitles.library import find_preferred_source
    receipt = None
    if video is not None:
        from ..subtitles.sources import discover_extracted_sources, extracted_receipt_path
        receipt = extracted_receipt_path(video)
        extracted, _error = discover_extracted_sources(
            video, _runtime._LANGUAGE_ALIASES, _runtime.LANGUAGES
        )
        preferred_variants = (candidate.variant, '') if candidate.variant else ('',)
        for variant in preferred_variants:
            source = next((
                item for item in extracted
                if item.canonical_language != candidate.target_lang
                and item.variant == variant
            ), None)
            if source is not None:
                return source.path, source.canonical_language, receipt
    source_path, source_language = find_preferred_source(candidate)
    return source_path, source_language, receipt


def _maintenance_preflight(candidates: list) -> tuple[dict[str, object], set[str], int]:
    """Skip metadata-stable files and analyze changed files in worker processes."""
    from .workers import (
        MaintenanceFileStat, ValidationTask, cache_entry_matches,
    )
    state = _runtime._get_validation_state()
    cached = state.maintenance_cache_entries(candidate.path for candidate in candidates)
    tasks = []
    cache_hits: set[str] = set()
    for sequence, candidate in enumerate(candidates):
        target_stat = MaintenanceFileStat.capture(candidate.path)
        video = _runtime._find_sidecar_video(candidate.path)
        source_path, _source_language, receipt = _maintenance_preflight_source(
            candidate, video
        )
        source_stat = MaintenanceFileStat.capture(source_path)
        video_stat = MaintenanceFileStat.capture(video)
        receipt_stat = MaintenanceFileStat.capture(receipt)
        identity = state.approval_identity_for_target(candidate.path)
        snapshot = state.name_approval_snapshot(identity)
        decisions = state.cue_decision_snapshot(identity)
        dependencies = {
            'approvalRevision': snapshot['revision'], 'approvalScope': snapshot['scope'],
            'decisionRevision': decisions['revision'],
            'source': source_stat.to_dict() if source_stat else None,
            'video': video_stat.to_dict() if video_stat else None,
            'receipt': receipt_stat.to_dict() if receipt_stat else None,
        }
        key = _maintenance_path_key(candidate.path)
        entry = cached.get(key)
        if cache_entry_matches(
            entry,
            target_stat=target_stat,
            dependency_fingerprint=dependencies,
            validator_version=state.validator_version,
            config_fingerprint=_runtime._MAINTENANCE_CONFIG_FINGERPRINT,
        ):
            cache_hits.add(key)
            continue
        same_inputs = bool(
            entry and target_stat
            and entry.get('targetSize') == target_stat.size
            and entry.get('targetModifiedNs') == target_stat.modified_ns
            and entry.get('dependencyFingerprint') == dependencies
            and entry.get('validatorVersion') == state.validator_version
        )
        tasks.append(ValidationTask(
            sequence=sequence,
            target_path=str(candidate.path),
            target_language=candidate.target_lang,
            source_path=str(source_path) if source_path is not None else None,
            source_aligned=bool(
                same_inputs and (entry.get('details') or {}).get('sourceAligned')
            ),
            video_path=str(video) if video is not None else None,
            receipt_path=str(receipt) if receipt is not None else None,
            validation_kwargs=_runtime._validation_kwargs(identity),
            approval_revision=snapshot['revision'],
            approval_scope=snapshot['scope'],
            completeness_kwargs=_runtime._completeness_kwargs(),
            undersized_enabled=_runtime.CLEANUP_UNDERSIZED_ENABLED,
            ffprobe_timeout=_runtime.CLEANUP_FFPROBE_TIMEOUT,
        ))
    results = {
        _maintenance_path_key(result.target_path): result
        for result in _runtime._maintenance_worker_pool.map_ordered(
            tasks,
            stop_requested=lambda: _runtime.shutdown_requested,
        )
        if result.target_path
    } if tasks and _runtime._maintenance_worker_pool is not None else {}
    failures = sum(bool(getattr(result, 'error', None)) for result in results.values())
    return results, cache_hits, failures


def _maintenance_cache_update(
    *, target_path, source_path, video_path, receipt_path, action, report,
    source_aligned: bool, prepared,
) -> dict | None:
    from .workers import MaintenanceFileStat, STABLE_CACHE_ACTIONS
    if action not in STABLE_CACHE_ACTIONS:
        return None
    target_stat = MaintenanceFileStat.capture(target_path)
    if target_stat is None:
        return None
    state = _runtime._get_validation_state()
    snapshot = state.name_approval_snapshot(state.approval_identity_for_target(target_path))
    decisions = state.cue_decision_snapshot(state.approval_identity_for_target(target_path))
    if source_aligned and (getattr(report, 'approval_revision', 0) != snapshot['revision'] or getattr(report, 'approval_scope', snapshot['scope']) != snapshot['scope']):
        return None
    dependencies = {
        'approvalRevision': snapshot['revision'], 'approvalScope': snapshot['scope'],
        'decisionRevision': decisions['revision'],
        'source': (stat.to_dict() if (stat := MaintenanceFileStat.capture(source_path)) else None),
        'video': (stat.to_dict() if (stat := MaintenanceFileStat.capture(video_path)) else None),
        'receipt': (stat.to_dict() if (stat := MaintenanceFileStat.capture(receipt_path)) else None),
    }
    target_hash = getattr(prepared, 'target_hash', None)
    if getattr(prepared, 'target_stat', None) != target_stat:
        target_hash = _runtime._file_hash_or_none(target_path)
    return {
        'targetPath': str(target_path),
        'targetSize': target_stat.size,
        'targetModifiedNs': target_stat.modified_ns,
        'dependencyFingerprint': dependencies,
        'validatorVersion': _runtime._get_validation_state().validator_version,
        'configFingerprint': _runtime._MAINTENANCE_CONFIG_FINGERPRINT,
        'validationResult': (
            'valid' if action in ('valid', 'formatted', 'repaired')
            else 'valid_with_warnings' if action == 'valid-warning'
            else 'reported'
        ),
        'actionResult': action,
        'targetHash': target_hash,
        'details': {
            'sourceAligned': bool(source_aligned),
            'validation': report.to_dict() if report is not None else {},
        },
    }

def _candidate_videos() -> list[_runtime.Path]:
    videos: set[_runtime.Path] = set()
    for root in _runtime.CLEANUP_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob('*'):
            if path.is_file() and path.suffix.casefold() in _runtime._VIDEO_EXTENSIONS:
                videos.add(path)
    return sorted(videos, key=lambda path: str(path).casefold())

def _managed_sidecar_is_valid(classification: _runtime.SidecarClassification, duration: float, detector, prepared=None) -> tuple[bool, dict]:
    from ..subtitles.foundation import file_sha256, target_language_for_code, validate_srt_structure
    from ..subtitles.library import validate_subtitle_without_source
    language = classification.language
    evidence = {'path': str(classification.path), 'language': language, 'valid': False}
    if language is None or any((token in _runtime._NON_FULL_SUBTITLE_TOKENS for token in classification.tokens)):
        evidence['reason'] = 'special_track'
        return (False, evidence)
    target_language = target_language_for_code(language)
    if target_language is None:
        evidence['reason'] = 'unsupported_language'
        return (False, evidence)
    try:
        from .workers import MaintenanceFileStat
        current_stat = MaintenanceFileStat.capture(classification.path)
        reusable = bool(
            prepared is not None and prepared.error is None
            and prepared.target_stat == current_stat
            and prepared.validation_mode == 'target-only'
        )
        target_hash = (
            prepared.target_hash if reusable and prepared.target_hash
            else file_sha256(classification.path)
        )
    except OSError as exc:
        evidence['reason'] = 'hash_unavailable'
        return (False, evidence)
    evidence['hash'] = target_hash
    completeness = (
        prepared.completeness if reusable and prepared.completeness is not None
        else _runtime._evaluate_completeness(classification.path, duration)
    )
    structure = prepared.report if reusable else validate_srt_structure(classification.path)
    if not reusable:
        _runtime._add_completeness_issue(structure, completeness)
    evidence['completeness'] = completeness.to_dict() if completeness is not None else None
    if not structure.valid:
        evidence['reason'] = 'undersized' if any((issue.rule == 'undersized_subtitle' for issue in structure.issues)) else 'structure_invalid'
        evidence['validation'] = structure.to_dict()
        return (False, evidence)
    video = _runtime._find_sidecar_video(classification.path)
    trusted = _runtime._get_validation_state().source_readiness(media_identity=_runtime._media_identity_for_video(video), source_language=language, source_hash=target_hash, media_duration_seconds=duration) if video is not None else None
    cached = _runtime._get_validation_state().current_valid_details(classification.path, target_hash)
    cached_completeness = cached.get('completeness') if cached is not None else None
    cached_duration = cached_completeness.get('mediaDurationSeconds') if isinstance(cached_completeness, dict) else None
    if isinstance(cached_duration, (int, float)) and abs(float(cached_duration) - duration) <= 0.5 and (not cached_completeness.get('undersized', False)):
        evidence.update({'valid': True, 'cached': True, 'reason': 'independent_language_validation'})
        return (True, evidence)
    if detector is None:
        evidence['reason'] = 'language_detector_unavailable'
        return (False, evidence)
    report = (
        prepared.report if reusable
        else validate_subtitle_without_source(classification.path, detector, target_language, target_lang=language, **_runtime._validation_kwargs())
    )
    if not reusable:
        _runtime._add_completeness_issue(report, completeness)
    evidence['validation'] = report.to_dict()
    if completeness is None:
        evidence['reason'] = 'completeness_unavailable'
        return (False, evidence)
    if report.valid:
        evidence.update({'valid': True, 'reason': 'independent_language_validation'})
        _runtime._record_validation_result(classification.path, None, target_hash, 'valid', report, completeness=evidence['completeness'], validationScope='prune-target-only')
    elif trusted is not None and report.issues and all((issue.rule == 'target_file_invalid' and issue.detail.startswith('detected ') for issue in report.issues)):
        evidence.update({'valid': True, 'cached': True, 'reason': 'successful_source_hash', 'readinessId': trusted['id'], 'languageOverride': True})
        return (True, evidence)
    else:
        evidence['reason'] = 'language_validation_failed' if any((issue.rule == 'target_file_invalid' for issue in report.issues)) else 'validation_failed'
        wrong_language_issue = next((
            issue for issue in report.issues
            if issue.rule == 'target_file_invalid'
            and issue.detail.startswith('detected ')
        ), None)
        if wrong_language_issue is not None:
            evidence['detail'] = f'expected {language}; {wrong_language_issue.detail}'
    return (report.valid, evidence)

def _apply_prune_action(video: _runtime.Path, classification: _runtime.SidecarClassification, readiness: dict, *, dry_run: bool) -> str:
    from ..subtitles.foundation import file_sha256
    from ..subtitles.library import quarantine_subtitle, write_validation_report
    subtitle = classification.path
    try:
        video_stat = video.stat()
        video_path_hash = _runtime.hashlib.sha256(_runtime.os.path.normcase(_runtime.os.path.abspath(str(video))).encode('utf-8')).hexdigest()
        subtitle_hash = file_sha256(subtitle)
    except OSError as exc:
        print(f'{_runtime.RED}[PRUNE] Could not hash {subtitle}: {exc}{_runtime.RESET}')
        return 'failed'

    audit = {'reason': 'unmanaged subtitle sidecar', 'videoPath': str(video), 'videoPathHash': video_path_hash, 'videoSize': video_stat.st_size, 'videoModifiedNs': video_stat.st_mtime_ns, 'targetPath': str(subtitle), 'targetHash': subtitle_hash, 'classification': {'kind': classification.kind, 'language': classification.language, 'tokens': list(classification.tokens)}, 'managedLanguages': _runtime.LANGUAGES, 'managedLanguageReadiness': readiness, 'action': 'dry-run' if dry_run else _runtime.CLEANUP_PRUNE_ACTION, 'recordedAt': _runtime.time.strftime('%Y-%m-%dT%H:%M:%SZ', _runtime.time.gmtime())}
    if dry_run or _runtime.CLEANUP_PRUNE_ACTION == 'report':
        print(f"[PRUNE] {('DRYRUN' if dry_run else 'REPORT')}: would remove {subtitle}")
        return 'dry-run' if dry_run else 'reported'
    if _runtime.CLEANUP_PRUNE_ACTION == 'quarantine':
        try:
            destination = quarantine_subtitle(subtitle, _runtime.CLEANUP_ROOTS, _runtime.CLEANUP_QUARANTINE_DIR, access_coordinator=_runtime._artifact_access)
            audit['quarantinePath'] = str(destination)
            try:
                write_validation_report(destination, audit)
            except OSError as exc:
                print(f'{_runtime.YELLOW}[PRUNE] Quarantined file but could not write report: {exc}{_runtime.RESET}')
            print(f'[PRUNE] Quarantined {subtitle} -> {destination}')
            return 'quarantined'
        except OSError as exc:
            print(f'{_runtime.RED}[PRUNE] Could not quarantine {subtitle}: {exc}{_runtime.RESET}')
            return 'failed'
    try:
        subtitle.unlink()
        print(f'[PRUNE] Deleted {subtitle}')
        return 'deleted'
    except OSError as exc:
        print(f'{_runtime.RED}[PRUNE] Could not delete {subtitle}: {exc}{_runtime.RESET}')
        return 'failed'


def _prune_parallel_analyses(
    requested: list[tuple[_runtime.Path, str | None]],
) -> tuple[dict[str, object], int, int]:
    from .workers import ValidationTask
    tasks = []
    sequence = 0
    for video, _item_type in requested:
        for path in _runtime._video_sidecars(video):
            classification = _runtime._classify_sidecar(video, path)
            if classification.kind != 'managed' or classification.language is None:
                continue
            if _runtime._explicit_non_full_sidecar(video, path) is not None:
                continue
            tasks.append(ValidationTask(
                sequence=sequence,
                target_path=str(path),
                target_language=classification.language,
                video_path=str(video),
                validation_kwargs=_runtime._validation_kwargs(),
                completeness_kwargs=_runtime._completeness_kwargs(),
                undersized_enabled=_runtime.CLEANUP_UNDERSIZED_ENABLED,
                ffprobe_timeout=_runtime.CLEANUP_FFPROBE_TIMEOUT,
            ))
            sequence += 1
    if not tasks or _runtime._maintenance_worker_pool is None:
        return {}, 0, 0
    analyses = {
        _maintenance_path_key(result.target_path): result
        for result in _runtime._maintenance_worker_pool.map_ordered(
            tasks, stop_requested=lambda: _runtime.shutdown_requested,
        )
        if result.target_path
    }
    failures = sum(bool(result.error) for result in analyses.values())
    return analyses, len(tasks), failures

def run_extra_sidecar_prune(videos: list[tuple[_runtime.Path, str | None]] | None=None, *, already_locked: bool=False, status_job_id: str | None=None) -> tuple[dict, bool, bool]:
    """Prune recognized unmanaged sidecars after all managed languages are ready."""
    stats = _runtime._prune_stats()
    if not _runtime.CLEANUP_PRUNE_EXTRA_LANGUAGES:
        return (stats, False, False)

    def run() -> tuple[dict, bool, bool]:
        detector = _runtime._get_cleanup_detector()
        requested = videos if videos is not None else [(video, None) for video in _runtime._candidate_videos()]
        analyses, tasks_submitted, worker_failures = _prune_parallel_analyses(requested)
        stats['maintenance_workers'] = _runtime.MAINTENANCE_WORKERS
        stats['tasks_submitted'] = tasks_submitted
        stats['tasks_completed'] = len(analyses)
        stats['worker_failures'] = worker_failures
        changed_episodes = False
        changed_movies = False
        for video, item_type in requested:
            if _runtime.shutdown_requested or not video.exists():
                continue
            sidecars = _runtime._video_sidecars(video)
            if not sidecars:
                continue
            stats['prune_videos_checked'] += 1
            duration = _runtime._probe_media_duration(video)
            if duration is None:
                stats['prune_duration_unavailable'] += 1
                stats['prune_deferred'] += 1
                print(f'{_runtime.YELLOW}[PRUNE] Deferred {video.name}: media duration unavailable{_runtime.RESET}')
                continue
            classified = [_runtime._classify_sidecar(video, path) for path in sidecars]
            readiness: dict[str, dict] = {}
            ready = True
            for language in [code.casefold() for code in _runtime.LANGUAGES]:
                candidates = [entry for entry in classified if entry.kind == 'managed' and entry.language == language]
                full_candidates = [entry for entry in candidates if _runtime._explicit_non_full_sidecar(video, entry.path) is None]
                if not full_candidates:
                    readiness[language] = {'ready': False, 'reason': 'missing_full_sidecar'}
                    stats['prune_missing_languages'] += 1
                    ready = False
                    continue
                evidence = []
                language_ready = False
                for entry in full_candidates:
                    prepared = analyses.get(_maintenance_path_key(entry.path))
                    validator = _runtime._managed_sidecar_is_valid
                    if prepared is not None and validator is _managed_sidecar_is_valid:
                        valid, candidate_evidence = validator(
                            entry, duration, detector, prepared,
                        )
                    else:
                        valid, candidate_evidence = validator(entry, duration, detector)
                    evidence.append(candidate_evidence)
                    language_ready = language_ready or valid
                reasons = sorted({str(candidate.get('reason') or 'language_validation_failed') for candidate in evidence if not candidate.get('valid')})
                readiness[language] = {'ready': language_ready, 'reason': 'successful_source_hash' if any((candidate.get('reason') == 'successful_source_hash' for candidate in evidence)) else ','.join(reasons) or 'validated', 'candidates': evidence}
                if not language_ready:
                    stats['prune_invalid_languages'] += 1
                    ready = False
            if not ready:
                stats['prune_deferred'] += 1
                if videos is None and _runtime._video_has_pending_repair(video):
                    _runtime._queue_video_for_pruning(video, item_type)
                missing = ','.join((code for code, value in readiness.items() if not value['ready']))
                reason_parts = []
                for code, value in readiness.items():
                    if value['ready']:
                        continue
                    reason = value.get('reason') or 'language_validation_failed'
                    detail = next((
                        candidate.get('detail')
                        for candidate in value.get('candidates', [])
                        if candidate.get('detail')
                    ), None)
                    reason_parts.append(
                        f"{code}={reason}" + (f" ({detail})" if detail else '')
                    )
                reason_codes = '; '.join(reason_parts)
                print(f'{_runtime.YELLOW}[PRUNE] Deferred {video.name}: managed language(s) not ready: {missing} ({reason_codes}){_runtime.RESET}')
                continue
            stats['prune_ready'] += 1
            for entry in classified:
                candidate = entry.kind == 'nonmanaged' or (entry.kind == 'special' and _runtime.CLEANUP_PRUNE_SPECIAL_SIDECARS) or (entry.kind == 'unknown' and _runtime.CLEANUP_PRUNE_UNKNOWN_SIDECARS)
                if entry.kind == 'unknown' and (not _runtime.CLEANUP_PRUNE_UNKNOWN_SIDECARS):
                    stats['prune_retained_unknown'] += 1
                if not candidate:
                    continue
                stats['prune_candidates'] += 1
                action = _runtime._apply_prune_action(video, entry, readiness, dry_run=_runtime.CLEANUP_SCAN_DRY_RUN)
                if action == 'quarantined':
                    stats['prune_quarantined'] += 1
                elif action == 'deleted':
                    stats['prune_deleted'] += 1
                elif action == 'reported':
                    stats['prune_reported'] += 1
                elif action == 'failed':
                    stats['prune_failures'] += 1
                if action in ('quarantined', 'deleted'):
                    _runtime._clear_submission_for_path(entry.path, entry.language or 'unknown')
                    _runtime._status_record_maintenance_outcome('sidecar_pruning', 'pruned', _runtime._maintenance_file_identity(entry.path, entry.language))
                    if item_type == 'episodes':
                        changed_episodes = True
                    elif item_type == 'movies':
                        changed_movies = True
                    else:
                        changed_episodes = changed_movies = True
        print(f"[PRUNE] Summary: videos={stats['prune_videos_checked']} ready={stats['prune_ready']} deferred={stats['prune_deferred']} candidates={stats['prune_candidates']} quarantined={stats['prune_quarantined']} deleted={stats['prune_deleted']} missing={stats['prune_missing_languages']} invalid={stats['prune_invalid_languages']} no-duration={stats['prune_duration_unavailable']} retained-unknown={stats['prune_retained_unknown']} failures={stats['prune_failures']}")
        return (stats, changed_episodes, changed_movies)
    owns_status_job = status_job_id is None
    prune_job_id = status_job_id
    if owns_status_job:
        prune_job_id = _runtime._status_create_maintenance('sidecar_pruning', {'title': 'Subtitle sidecar pruning'}, state='pruning')
    else:
        _runtime._status_update_maintenance(prune_job_id, 'pruning')
    try:
        if already_locked:
            result = run()
        else:
            with _runtime._cleanup_scan_lock:
                result = run()
    except Exception:
        if owns_status_job:
            _runtime._status_complete_maintenance(prune_job_id, 'failed', reason='validation action failed')
        raise
    prune_details = {'filesDiscovered': result[0]['prune_candidates'], 'filesChecked': result[0]['prune_videos_checked'], 'maintenanceWorkers': result[0].get('maintenance_workers', 1), 'tasksSubmitted': result[0].get('tasks_submitted', 0), 'tasksCompleted': result[0].get('tasks_completed', 0), 'workerFailures': result[0].get('worker_failures', 0), 'failures': result[0]['prune_failures'] + result[0].get('worker_failures', 0), 'quarantines': result[0]['prune_quarantined']}
    if owns_status_job:
        _runtime._status_complete_maintenance(prune_job_id, 'accepted', details=prune_details)
    return result

def run_existing_cleanup_scan(maintenance_scan_job_id: str | None=None) -> dict:
    stats = {'files_checked': 0, 'skipped_unchanged': 0, 'maintenance_workers': _runtime.MAINTENANCE_WORKERS, 'cache_hits': 0, 'tasks_submitted': 0, 'tasks_completed': 0, 'worker_failures': 0, 'excessive_line_cues': 0, 'other_invalid_cues': 0, 'formatted_files': 0, 'repaired_files': 0, 'repair_failures': 0, 'repair_queued': 0, 'repair_deferred': 0, 'quarantined_files': 0, 'deleted_files': 0, 'reported_files': 0, 'dry_run_files': 0, 'without_source': 0, 'source_less_warnings': 0, 'recovered_pending_outputs': 0, 'repeat_quarantines': 0, 'ai_repairs_suppressed': 0, 'action_failures': 0, 'undersized_checked': 0, 'undersized_forced_exempt': 0, 'undersized_duration_unavailable': 0, 'undersized_detected': 0, 'undersized_quarantined': 0, **_runtime._prune_stats()}
    if maintenance_scan_job_id:
        with _runtime._maintenance_scan_contexts_lock:
            context = _runtime._maintenance_scan_contexts.get(maintenance_scan_job_id)
            if context is not None:
                context['stats'] = stats
    if not _runtime.CLEANUP_SCAN_EXISTING:
        return stats
    from ..subtitles.foundation import file_sha256, target_language_for_code
    from ..subtitles.library import discover_target_subtitles, find_preferred_source, validate_subtitle_without_source
    with _runtime._cleanup_scan_lock:
        managed_validation_languages = {
            str(code).strip().casefold()
            for code in (*_runtime.LANGUAGES, *_runtime.CLEANUP_LANGUAGES)
            if str(code).strip()
        }
        detector = _runtime._get_cleanup_detector()
        state = _runtime._get_validation_state()
        changed = _runtime._scan_undersized_sidecars(stats)
        if detector is None or not managed_validation_languages:
            prune_stats, prune_episodes, prune_movies = _runtime.run_extra_sidecar_prune(already_locked=True, status_job_id=maintenance_scan_job_id)
            prune_stats['prune_bazarr_rescan_batches'] = int(prune_episodes or prune_movies)
            _merge_prune_stats(stats, prune_stats)
            changed = changed or prune_episodes or prune_movies
            if changed and (not _runtime.shutdown_requested):
                _runtime._tracked_bazarr_sync(True, True, _runtime.SYNC_TIMEOUT)
            return stats
        candidates = discover_target_subtitles(
            _runtime.CLEANUP_ROOTS, managed_validation_languages
        )
        analyses, cache_hits, worker_failures = _maintenance_preflight(candidates)
        stats['cache_hits'] = len(cache_hits)
        stats['tasks_submitted'] += len(candidates) - len(cache_hits)
        stats['tasks_completed'] += len(analyses)
        stats['worker_failures'] += worker_failures
        cache_updates: list[dict] = []
        state.delete_maintenance_cache_entries(
            candidate.path for candidate in candidates
            if _maintenance_path_key(candidate.path) not in cache_hits
        )
        if maintenance_scan_job_id:
            with _runtime._maintenance_scan_contexts_lock:
                context = _runtime._maintenance_scan_contexts.get(maintenance_scan_job_id)
                if context is not None:
                    context['files_discovered'] = len(candidates)
                    context['last_publish'] = _runtime.time.monotonic()
                    details = _runtime._scan_progress_details(context)
            if context is not None:
                _runtime._status_update_maintenance(maintenance_scan_job_id, 'scanning', details=details)
        print(f"[SCAN] Existing subtitle cleanup found {len(candidates)} target file(s) under {', '.join((str(root) for root in _runtime.CLEANUP_ROOTS))}")
        for candidate in candidates:
            if _runtime.shutdown_requested:
                break
            if maintenance_scan_job_id:
                with _runtime._maintenance_scan_contexts_lock:
                    context = _runtime._maintenance_scan_contexts.get(maintenance_scan_job_id)
                    if context is not None:
                        context['files_checked'] += 1
            candidate_key = _maintenance_path_key(candidate.path)
            if candidate_key in cache_hits:
                stats['skipped_unchanged'] += 1
                _runtime._publish_scan_progress(maintenance_scan_job_id)
                continue
            prepared_analysis = analyses.get(candidate_key)
            target_language = target_language_for_code(candidate.target_lang)
            if target_language is None:
                print(f'{_runtime.YELLOW}[SCAN] Unsupported target language for {candidate.path}{_runtime.RESET}')
                _runtime._publish_scan_progress(maintenance_scan_job_id)
                continue
            source_path = None
            source_lang = None
            candidate_video = _runtime._find_sidecar_video(candidate.path)
            receipt_path = None
            if candidate_video is not None:
                from ..subtitles.foundation import normalize_managed_file
                from ..subtitles.sources import discover_extracted_sources, extracted_receipt_path, prepare_extracted_source
                receipt_path = extracted_receipt_path(candidate_video)
                extracted, receipt_error = discover_extracted_sources(candidate_video, _runtime._LANGUAGE_ALIASES, _runtime.LANGUAGES)
                if receipt_error:
                    _runtime.dbg(f'Ignored extracted-subtitle receipt for {candidate.path.name}: {receipt_error}')
                preferred_variants = (candidate.variant, '') if candidate.variant else ('',)
                for variant in preferred_variants:
                    for extracted_candidate in extracted:
                        if extracted_candidate.canonical_language == candidate.target_lang or extracted_candidate.variant != variant:
                            continue
                        prepared = prepare_extracted_source(extracted_candidate, artifact_access=_runtime._artifact_access, normalize=normalize_managed_file)
                        if prepared.error:
                            print(f'{_runtime.YELLOW}[SCAN] Rejected extracted source for {candidate.path.name}: {prepared.error}{_runtime.RESET}')
                            continue
                        source_path = extracted_candidate.path
                        source_lang = extracted_candidate.canonical_language
                        if prepared.changed:
                            stats['source_duplicate_groups'] = stats.get('source_duplicate_groups', 0) + prepared.duplicate_groups
                            stats['source_duplicate_cues_removed'] = stats.get('source_duplicate_cues_removed', 0) + prepared.removed_cues
                            print(f'[SCAN] Deduplicated {source_path.name}: groups={prepared.duplicate_groups} removed_cues={prepared.removed_cues}')
                        break
                    if source_path is not None:
                        break
            if source_path is None:
                source_path, source_lang = find_preferred_source(candidate)
            if source_path is not None and candidate.variant:
                print(f'[SCAN] Paired {candidate.path.name} with variant-aware source {source_path.name}')
            try:
                from .workers import MaintenanceFileStat
                current_target_stat = MaintenanceFileStat.capture(candidate.path)
                target_hash = (
                    prepared_analysis.target_hash
                    if prepared_analysis is not None
                    and prepared_analysis.target_stat == current_target_stat
                    and prepared_analysis.target_hash is not None
                    else file_sha256(candidate.path)
                )
            except OSError as e:
                print(f'{_runtime.YELLOW}[SCAN] Could not hash {candidate.path}: {e}{_runtime.RESET}')
                _runtime._publish_scan_progress(maintenance_scan_job_id)
                continue
            validation_origin = None
            validation_source_hash = None
            validation_item_type = None
            validation_item_id = None
            submission = _runtime._find_submission_for_target(candidate.path, candidate.target_lang)
            if submission is not None:
                pending_source = submission.get('sourcePath')
                pending_language = submission.get('sourceLanguage')
                if isinstance(pending_source, str) and pending_source and _runtime.os.path.exists(pending_source) and isinstance(pending_language, str) and pending_language:
                    try:
                        pending_hash = file_sha256(pending_source)
                    except OSError as e:
                        print(f'{_runtime.YELLOW}[SCAN] Could not hash pending source {pending_source}: {e}{_runtime.RESET}')
                        pending_hash = None
                    candidate_source_matches = source_path is not None and source_lang is not None and _runtime._submission_matches_source(submission, str(source_path), source_lang, candidate.path, candidate.target_lang)
                    if pending_hash is not None and submission.get('sourceHash') == pending_hash and (_runtime._submission_matches_source(submission, pending_source, pending_language, candidate.path, candidate.target_lang) if source_path is None else candidate_source_matches):
                        if source_path is None:
                            source_path = _runtime.Path(pending_source)
                            source_lang = pending_language
                        validation_origin = 'lingarr'
                        validation_source_hash = pending_hash
                        validation_item_type = submission.get('itemType')
                        validation_item_id = submission.get('itemId')
                        stats['recovered_pending_outputs'] += 1
                        print(f'[SCAN] Recovered pending Lingarr output {candidate.path.name} with source {source_path.name}')
            try:
                current_source_stat = MaintenanceFileStat.capture(source_path)
                source_hash = (
                    prepared_analysis.source_hash
                    if source_path is not None
                    and prepared_analysis is not None
                    and prepared_analysis.source_stat == current_source_stat
                    and prepared_analysis.source_hash is not None
                    else file_sha256(source_path) if source_path is not None else None
                )
            except OSError as e:
                print(f'{_runtime.YELLOW}[SCAN] Could not hash {source_path}: {e}{_runtime.RESET}')
                source_path = None
                source_lang = None
                source_hash = None
            if validation_item_id is None and source_hash is not None and hasattr(state, 'retry_plans'):
                normalized_target = _runtime.os.path.normcase(_runtime.os.path.abspath(candidate.path))
                plan = next((entry for entry in state.retry_plans(include_terminal=False) if entry.get('targetLanguage') == candidate.target_lang and entry.get('sourceHash') == source_hash and _runtime.os.path.normcase(_runtime.os.path.abspath(entry.get('targetPath') or '')) == normalized_target), None)
                if plan is not None:
                    validation_item_type = plan['itemType']
                    validation_item_id = plan['itemId']
            if state.is_unchanged_valid(candidate.path, source_hash, target_hash):
                stats['skipped_unchanged'] += 1
                cache_entry = _maintenance_cache_update(
                    target_path=candidate.path,
                    source_path=source_path,
                    video_path=candidate_video,
                    receipt_path=receipt_path,
                    action='valid',
                    report=None,
                    source_aligned=False,
                    prepared=prepared_analysis,
                )
                if cache_entry is not None:
                    cache_updates.append(cache_entry)
                _runtime._publish_scan_progress(maintenance_scan_job_id)
                continue
            stats['files_checked'] += 1
            if source_path is not None and source_lang is not None:
                action, report = _runtime._validate_translated_file(str(source_path), str(candidate.path), source_lang, candidate.target_lang, validation_item_id, title=candidate.path.name, dry_run=_runtime.CLEANUP_SCAN_DRY_RUN, defer_repair=not _runtime.CLEANUP_SCAN_DRY_RUN, item_type=validation_item_type, media_duration=_runtime._probe_media_duration(candidate_video) if candidate_video is not None else None, origin=validation_origin, provenance_source_hash=validation_source_hash, maintenance_scan_job_id=maintenance_scan_job_id, prepared_analysis=prepared_analysis)
            else:
                stats['without_source'] += 1
                reusable = bool(
                    prepared_analysis is not None
                    and prepared_analysis.error is None
                    and prepared_analysis.validation_mode == 'target-only'
                    and prepared_analysis.target_stat == MaintenanceFileStat.capture(candidate.path)
                )
                report = (
                    prepared_analysis.report if reusable
                    else validate_subtitle_without_source(candidate.path, detector, target_language, target_lang=candidate.target_lang, **_runtime._validation_kwargs())
                )
                completeness = (
                    prepared_analysis.completeness if reusable
                    else None
                )
                if completeness is None:
                    completeness = _runtime._evaluate_completeness(
                        candidate.path,
                        _runtime._probe_media_duration(candidate_video)
                        if candidate_video is not None else None,
                    )
                    _runtime._add_completeness_issue(report, completeness)
                if report.valid:
                    print(f'[SCAN] OK {candidate.path.name} (target-only validation passed)')
                    _runtime._record_validation_result(candidate.path, None, target_hash, 'valid', report, sourceAvailable=False)
                    action = 'valid'
                elif _runtime._source_less_line_only_warning(report):
                    print(f'{_runtime.YELLOW}[SCAN] Retained {candidate.path.name} with source-less line-count warning: {report.summary()}{_runtime.RESET}')
                    _runtime._record_validation_result(candidate.path, None, target_hash, 'valid_with_warnings', report, sourceAvailable=False, warningRules=['excessive_lines'])
                    stats['source_less_warnings'] += 1
                    action = 'valid-warning'
                else:
                    print(f'{_runtime.YELLOW}[SCAN] Invalid target without source {candidate.path.name}: {report.summary()}{_runtime.RESET}')
                    wrong_language = _runtime._confident_wrong_language_evidence(
                        candidate.path, detector, target_language,
                        candidate.target_lang, completeness,
                    ) if candidate.target_lang not in _runtime.CLEANUP_LANGUAGES else None
                    if candidate.target_lang not in _runtime.CLEANUP_LANGUAGES and wrong_language is None:
                        print(f'{_runtime.YELLOW}[SCAN] Retained {candidate.path.name}: outside AI cleanup scope without a confident whole-file language mismatch{_runtime.RESET}')
                        _runtime._record_validation_result(
                            candidate.path, None, target_hash, 'reported', report,
                            sourceAvailable=False,
                            completeness=completeness.to_dict() if completeness is not None else None,
                        )
                        action = 'reported'
                    else:
                        if wrong_language is not None:
                            setattr(report, 'wrong_language_evidence', wrong_language)
                        action = _runtime._apply_cleanup_action(
                            candidate.path, None, candidate.target_lang, report,
                            expected_target_hash=target_hash,
                            lingarr_outcome='not attempted: no source subtitle',
                            completeness=completeness,
                            dry_run=_runtime.CLEANUP_SCAN_DRY_RUN,
                        )
            if report is not None:
                excessive = sum((issue.rule == 'excessive_lines' for issue in report.issues))
                stats['excessive_line_cues'] += excessive
                stats['other_invalid_cues'] += len(report.issues) - excessive
                stats['repeat_quarantines'] += int(bool(getattr(report, 'repeat_offender', False)))
                stats['ai_repairs_suppressed'] += int(bool(getattr(report, 'ai_repair_suppressed', False)))
                if action not in ('valid', 'valid-warning', 'formatted', 'repaired', 'repair-queued', 'repair-duplicate', 'repair-deferred') and source_path is not None and _runtime.CLEANUP_REPAIR_ENABLED and report.repairable_cue_indexes and (not _runtime.CLEANUP_SCAN_DRY_RUN):
                    stats['repair_failures'] += 1
            if action == 'formatted':
                stats['formatted_files'] += 1
                changed = True
            elif action == 'repaired':
                stats['repaired_files'] += 1
                changed = True
            elif action == 'repair-queued':
                stats['repair_queued'] += 1
            elif action == 'repair-deferred':
                stats['repair_deferred'] += 1
            elif action == 'quarantined':
                stats['quarantined_files'] += 1
                _runtime._clear_submission_for_path(candidate.path, candidate.target_lang)
                changed = True
            elif action == 'deleted':
                stats['deleted_files'] += 1
                _runtime._clear_submission_for_path(candidate.path, candidate.target_lang)
                changed = True
            elif action == 'reported':
                stats['reported_files'] += 1
            elif action == 'dry-run':
                stats['dry_run_files'] += 1
            elif action == 'action-failed':
                stats['action_failures'] += 1
            wrong_language = getattr(report, 'wrong_language_evidence', None)
            cache_entry = _maintenance_cache_update(
                target_path=candidate.path,
                source_path=source_path,
                video_path=candidate_video,
                receipt_path=receipt_path,
                action=action,
                report=report,
                source_aligned=bool(getattr(report, 'source_aligned', False)),
                prepared=prepared_analysis,
            )
            if cache_entry is not None:
                cache_updates.append(cache_entry)
            operation_outcomes = {'valid': ('validation', 'validated'), 'valid-warning': ('validation', 'validated'), 'formatted': ('format_repair', 'formatted'), 'quarantined': ('language_validation' if wrong_language is not None else 'quarantine', 'quarantined'), 'deleted': ('language_validation' if wrong_language is not None else 'deletion', 'deleted'), 'action-failed': ('validation', 'failed')}
            if action in operation_outcomes:
                operation, outcome = operation_outcomes[action]
                reason = (
                    f"expected {wrong_language['expectedLanguage']}; detected {wrong_language['detectedLanguage']} {wrong_language['detectedConfidence']:.2f}"
                    if wrong_language is not None
                    else 'validation action failed' if outcome == 'failed' else None
                )
                _runtime._status_record_maintenance_outcome(operation, outcome, _runtime._maintenance_file_identity(candidate.path, candidate.target_lang), reason=reason)
            _runtime._publish_scan_progress(maintenance_scan_job_id)
        state.upsert_maintenance_cache_entries(cache_updates)
        _runtime._publish_scan_progress(maintenance_scan_job_id, force=True)
        prune_stats, prune_episodes, prune_movies = _runtime.run_extra_sidecar_prune(already_locked=True, status_job_id=maintenance_scan_job_id)
        prune_stats['prune_bazarr_rescan_batches'] = int(prune_episodes or prune_movies)
        _merge_prune_stats(stats, prune_stats)
        changed = changed or prune_episodes or prune_movies
        print('[SCAN] Existing subtitle cleanup summary:')
        print(f"  Checked             : {stats['files_checked']}")
        print(f"  Skipped unchanged   : {stats['skipped_unchanged']}")
        print(f"  Cache hits          : {stats['cache_hits']}")
        print(f"  Worker tasks/failures: {stats['tasks_completed']}/{stats['worker_failures']}")
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
        if _runtime.CLEANUP_SCAN_DRY_RUN:
            print(f"  Dry-run files       : {stats['dry_run_files']}")
        if changed and (not _runtime.shutdown_requested):
            _runtime._tracked_bazarr_sync(True, True, _runtime.SYNC_TIMEOUT)
        return stats

def _run_existing_cleanup_scan_safely() -> dict | None:
    scan_job_id = _runtime._status_create_maintenance('existing_library_scan', {'title': 'Existing subtitle library'}, state='scanning', details={'filesDiscovered': 0, 'filesChecked': 0, 'filesRemaining': 0, 'progress': 0})
    if scan_job_id:
        with _runtime._maintenance_scan_contexts_lock:
            _runtime._maintenance_scan_contexts[scan_job_id] = {'started': _runtime.time.monotonic(), 'stats': {}, 'files_discovered': 0, 'files_checked': 0, 'pending': 0, 'repairs_queued': 0, 'repairs_completed': 0, 'enumeration_done': False, 'last_publish': 0.0}
    try:
        stats = _runtime.run_existing_cleanup_scan(scan_job_id)
        if _runtime._pending_repairs:
            _runtime._status_update_maintenance(scan_job_id, 'waiting_repair_completion')
            _runtime._drain_pending_repairs(stats)
        _runtime._scan_enumeration_finished(scan_job_id, stats)
        return None if stats.get('async_repair_failures') or stats.get('cleanup_repair_failures') else stats
    except Exception as e:
        print(f'{_runtime.RED}[ERROR] Existing subtitle cleanup scan failed: {e}{_runtime.RESET}')
        if scan_job_id:
            with _runtime._maintenance_scan_contexts_lock:
                _runtime._maintenance_scan_contexts.pop(scan_job_id, None)
            _runtime._status_complete_maintenance(scan_job_id, 'failed', reason='existing library scan failed')
        if _runtime.DEBUG:
            import traceback
            traceback.print_exc()
        return None

def run_retention_housekeeping() -> dict:
    from ..subtitles.library import purge_old_files
    current_log = [_runtime.current_log_path] if _runtime.current_log_path is not None else []
    protected = _runtime._get_validation_state().protected_artifact_paths()
    quarantine_removed = purge_old_files(_runtime.CLEANUP_QUARANTINE_DIR, _runtime.QUARANTINE_ARTIFACT_RETENTION_DAYS, exclude=protected, access_coordinator=_runtime._artifact_access)
    logs_removed = purge_old_files(_runtime.LOG_DIR, _runtime.RETENTION_DAYS, exclude=current_log)
    try:
        state_removed = _runtime._get_validation_state().prune_older_than(_runtime.RETENTION_DAYS)
    except (OSError, _runtime.StateStoreError) as e:
        print(f'{_runtime.YELLOW}[WARNING] Could not prune validation state: {e}{_runtime.RESET}')
        state_removed = 0
    pending_scans = _runtime._manual_review_service.dispatch_pending_scans(limit=10) if _runtime._manual_review_service is not None else {'examined': 0, 'dispatched': 0}
    result = {'quarantine_files': len(quarantine_removed), 'log_files': len(logs_removed), 'state_entries': state_removed, 'status_events': _runtime._status_compact_history(), 'manual_scans': pending_scans['dispatched']}
    print(f"[RETENTION] Removed {result['quarantine_files']} quarantine file(s), {result['log_files']} log file(s), and {result['state_entries']} validation state record(s) plus {result['status_events']} status event(s) beyond their retention window")
    return result

def _run_retention_housekeeping_tracked() -> dict:
    job_id = _runtime._status_create_maintenance(
        'retention', {'title': 'Retention housekeeping'}, state='retaining'
    )
    try:
        result = run_retention_housekeeping()
    except Exception:
        _runtime._status_complete_maintenance(
            job_id, 'failed', reason='retention housekeeping failed'
        )
        raise
    _runtime._status_complete_maintenance(job_id, 'accepted')
    return result

EXPORTS = {
    name: globals()[name] for name in (
        '_scan_undersized_sidecars', '_video_sidecars',
        '_queue_video_for_pruning', '_take_pending_prune_videos',
        '_video_has_pending_repair', '_prune_stats', '_candidate_videos',
        '_managed_sidecar_is_valid', '_apply_prune_action',
        'run_extra_sidecar_prune', 'run_existing_cleanup_scan',
        '_run_existing_cleanup_scan_safely', 'run_retention_housekeeping',
        '_run_retention_housekeeping_tracked',
    )
}
