from __future__ import annotations
from .composition import runtime as _runtime

def _item_title(item: dict, item_type: str) -> str:
    if item_type == 'episodes':
        return item.get('seriesTitle', item.get('series_title', 'Unknown'))
    return item.get('title', 'Unknown')

def _mark_activity(stats: dict, item_type: str) -> None:
    if item_type == 'episodes':
        stats['episode_activity'] = True
    else:
        stats['movie_activity'] = True

def _record_invalid_circuit_outcome(series_key: str, series_title: str, action: str, report, *, trial_owner: str | None=None, trial_job_id: int | None=None, trial_plan_id: int | None=None, trial_generation: int | None=None) -> None:
    if action not in ('quarantined', 'deleted'):
        return
    rules = ','.join((issue.rule for issue in getattr(report, 'issues', [])))
    try:
        _runtime._get_validation_state().record_circuit_outcome(series_key=series_key, series_title=series_title, success=False, reason=f'invalid subtitle {action}: {rules}', threshold=_runtime.CIRCUIT_FAILURE_THRESHOLD, open_cycles=_runtime.CIRCUIT_OPEN_CYCLES, config_fingerprint=_runtime._CIRCUIT_CONFIG_FINGERPRINT, trial_owner=trial_owner, trial_job_id=trial_job_id, trial_plan_id=trial_plan_id, lease_generation=trial_generation)
        _runtime._refresh_status_diagnostics()
    except _runtime.StateStoreError as exc:
        print(f'{_runtime.YELLOW}[CIRCUIT] Could not record invalid output: {exc}{_runtime.RESET}')

def _record_valid_circuit_outcome(series_key: str, series_title: str) -> None:
    try:
        _runtime._get_validation_state().record_circuit_outcome(series_key=series_key, series_title=series_title, success=True, reason=None, threshold=_runtime.CIRCUIT_FAILURE_THRESHOLD, open_cycles=_runtime.CIRCUIT_OPEN_CYCLES, config_fingerprint=_runtime._CIRCUIT_CONFIG_FINGERPRINT)
        _runtime._refresh_status_diagnostics()
    except _runtime.StateStoreError as exc:
        print(f'{_runtime.YELLOW}[CIRCUIT] Could not close validated trial: {exc}{_runtime.RESET}')

def _resolve_existing_retry_success(
    retry_plan: dict | None,
    series_key: str,
    series_title: str,
) -> bool:
    """Accept validated on-disk output and settle any persisted retry trial."""
    if retry_plan is None:
        _runtime._record_valid_circuit_outcome(series_key, series_title)
        return True
    state = _runtime._get_validation_state()
    trial = state.circuit_trial_for_retry_plan(retry_plan['id'])
    generation = trial.get('leaseGeneration') if trial is not None else None
    resolved = _runtime._resolve_retry_success(
        retry_plan['id'], retry_plan.get('sourceHash'),
        lease_generation=generation,
    )
    if trial is None:
        if resolved:
            _runtime._record_valid_circuit_outcome(series_key, series_title)
        return resolved
    try:
        state.settle_circuit_trial_for_retry(
            retry_plan['id'], lease_generation=generation,
            outcome='success' if resolved else 'deferred',
            open_cycles=_runtime.CIRCUIT_OPEN_CYCLES,
            reason=None if resolved else 'retry acceptance was stale or superseded',
        )
        _runtime._refresh_status_diagnostics()
    except _runtime.StateStoreError as exc:
        print(f'{_runtime.YELLOW}[CIRCUIT] Could not settle validated on-disk retry trial: {exc}{_runtime.RESET}')
    return resolved

def _defer_bound_retry_trial(retry_plan: dict | None, trial_claimed: bool, trial_generation: int | None, reason: str) -> None:
    if retry_plan is None or not trial_claimed or trial_generation is None:
        return
    try:
        state = _runtime._get_validation_state()
        rescheduled = state.reschedule_retry_no_progress(
            retry_plan['id'], completed_cycle=_runtime._completed_cycle,
            deferral_class='output_deferred', reason=reason, delay_cycles=1,
            lease_generation=trial_generation,
        )
        if rescheduled is not None:
            state.settle_circuit_trial_for_retry(
                retry_plan['id'], lease_generation=trial_generation,
                outcome='deferred', open_cycles=_runtime.CIRCUIT_OPEN_CYCLES,
                reason=reason,
            )
            _runtime._refresh_status_diagnostics()
    except _runtime.StateStoreError as exc:
        print(f'{_runtime.YELLOW}[CIRCUIT] Could not release deferred output trial: {exc}{_runtime.RESET}')

def _bazarr_has_repaired_path(result: _runtime.RepairJobResult) -> bool:
    if result.item_id is None or result.item_type not in ('episodes', 'movies'):
        return True
    try:
        _, subtitles = _runtime.fetch_subtitles(result.item_type, result.item_id)
    except _runtime.ServiceRequestError as exc:
        print(f'{_runtime.YELLOW}[WARNING] Could not verify repaired path: {exc}{_runtime.RESET}')
        return False
    expected = _runtime.os.path.normcase(_runtime.os.path.normpath(result.target_path))
    return any((_runtime.os.path.normcase(_runtime.os.path.normpath(str(subtitle.get('path', '')))) == expected for subtitle in subtitles))

def _record_cleanup_stats(stats: dict, action: str, report) -> None:
    if report is None:
        return
    stats['cleanup_checked'] = stats.get('cleanup_checked', 0) + 1
    excessive = sum((issue.rule == 'excessive_lines' for issue in report.issues))
    undersized = sum((issue.rule == 'undersized_subtitle' for issue in report.issues))
    stats['cleanup_excessive_lines'] = stats.get('cleanup_excessive_lines', 0) + excessive
    stats['cleanup_undersized_targets'] = stats.get('cleanup_undersized_targets', 0) + undersized
    other = max(0, len(report.issues) - excessive - undersized)
    stats['cleanup_other_issues'] = stats.get('cleanup_other_issues', 0) + other
    stats['cleanup_repeat_quarantines'] = stats.get('cleanup_repeat_quarantines', 0) + int(bool(getattr(report, 'repeat_offender', False)))
    stats['cleanup_ai_repairs_suppressed'] = stats.get('cleanup_ai_repairs_suppressed', 0) + int(bool(getattr(report, 'ai_repair_suppressed', False)))
    if action == 'valid-warning':
        stats['cleanup_source_less_warnings'] = stats.get('cleanup_source_less_warnings', 0) + 1
    elif action == 'formatted':
        stats['cleanup_formatted'] = stats.get('cleanup_formatted', 0) + 1
    elif action == 'repaired':
        stats['cleanup_repaired'] = stats.get('cleanup_repaired', 0) + 1
    elif action in ('quarantined', 'deleted', 'reported', 'dry-run'):
        stats[f'cleanup_{action}'] = stats.get(f'cleanup_{action}', 0) + 1
    elif action == 'action-failed':
        stats['cleanup_action_failed'] = stats.get('cleanup_action_failed', 0) + 1

def _source_is_usable(source_path: str, source_lang: str, media_duration: float | None, title: str, item_type: str, stats: dict, stats_lock: _runtime.threading.Lock, *, origin: str='bazarr') -> bool:
    from .subtitles.foundation import validate_srt_structure
    report = validate_srt_structure(source_path)
    completeness = _runtime._evaluate_completeness(source_path, media_duration)
    _runtime._add_completeness_issue(report, completeness)
    if report.valid:
        return True
    print(f"{_runtime.YELLOW}[SOURCE] Rejected {title} '{source_lang}': {report.summary()}{_runtime.RESET}")
    action = _runtime._apply_cleanup_action(source_path, None, source_lang, report, completeness=completeness, origin=origin, lingarr_outcome='not attempted: source is not suitable for full translation')
    with stats_lock:
        stats['cleanup_undersized_sources'] = stats.get('cleanup_undersized_sources', 0) + int(completeness is not None and completeness.undersized)
        _runtime._record_cleanup_stats(stats, action, report)
        if action in ('quarantined', 'deleted'):
            _runtime._mark_activity(stats, item_type)
    return False

def process_item(item: dict, item_type: str, id_field: str, stats: dict, stats_lock: _runtime.threading.Lock, retry_plan: dict | None=None, retry_submission_callback=None) -> None:
    if _runtime.shutdown_requested:
        return
    item_id = item.get(id_field)
    if item_id is None:
        return
    title = _runtime._item_title(item, item_type)
    identity = _runtime.resolve_media_identity(item, item_type, item_id)
    series_title = identity['title']
    series_key = identity['key']
    lingarr_media_type = 'Episode' if item_type == 'episodes' else 'Movie'
    missing_raw = {str(s.get('code2')).strip().lower() for s in item.get('missing_subtitles', []) if s.get('code2')}
    missing = {l for l in _runtime.LANGUAGES if l in missing_raw}
    if not missing:
        return
    try:
        video_path, subs = _runtime.fetch_subtitles(item_type, item_id)
    except _runtime.ServiceRequestError as exc:
        print(f'{_runtime.YELLOW}[DEFER] {title}: {exc}{_runtime.RESET}')
        with stats_lock:
            stats['api_errors'] = stats.get('api_errors', 0) + 1
            stats['deferred'] = stats.get('deferred', 0) + len(missing)
        for target_lang in missing:
            _runtime._status_transition(item_type, item_id, target_lang, 'deferred', reason='Bazarr subtitle lookup unavailable')
        return
    _runtime._status_set_episode_identity(item_type, item_id, video_path)
    identity = _runtime.resolve_media_identity(item, item_type, item_id, video_path)
    series_title = identity['title']
    series_key = identity['key']
    if item_type == 'episodes' and series_key.startswith('sonarr:'):
        fallback_item = dict(item)
        fallback_item.pop('sonarrSeriesId', None)
        fallback_item.pop('sonarr_series_id', None)
        fallback_identity = _runtime.resolve_media_identity(fallback_item, item_type, item_id, video_path)
        if fallback_identity['key'] != series_key:
            try:
                _runtime._get_validation_state().register_series_alias(fallback_identity['key'], series_key, series_title)
            except _runtime.StateStoreError as exc:
                print(f'{_runtime.YELLOW}[IDENTITY] Could not persist series alias for {series_title}: {exc}{_runtime.RESET}')
    if video_path:
        _runtime._queue_video_for_pruning(video_path, item_type)
    available_by_lang: dict[str, list[str]] = {}
    for s in subs:
        code, path = (s.get('code2'), s.get('path', ''))
        if not code or not path:
            continue
        code = str(code).strip().lower()
        if video_path:
            from .subtitles.sources import is_extracted_sidecar
            if is_extracted_sidecar(path, video_path):
                continue
        if _runtime._truthy(s.get('forced')) or (video_path and _runtime._explicit_non_full_sidecar(video_path, path) is not None):
            with stats_lock:
                stats['cleanup_forced_sources_skipped'] = stats.get('cleanup_forced_sources_skipped', 0) + 1
            continue
        available_by_lang.setdefault(code, []).append(path)
    for code, paths in available_by_lang.items():
        paths.sort(key=lambda path: _runtime._sub_priority(path, code))
    retry_target = str(retry_plan.get('targetLanguage') or '').lower() if retry_plan is not None else ''
    target_langs = [l for l in _runtime.LANGUAGES if l in missing and l not in available_by_lang]
    if retry_plan is not None:
        if retry_target in missing and retry_target not in target_langs:
            target_langs.append(retry_target)
    media_duration = _runtime._probe_media_duration(video_path) if video_path and _runtime.CLEANUP_UNDERSIZED_ENABLED else None
    source_lang = ''
    source_path = ''
    source_origin = 'bazarr'
    source_variant = ''
    rejected_sources = 0
    if video_path:
        from .subtitles.foundation import normalize_managed_file
        from .subtitles.sources import discover_extracted_sources, prepare_extracted_source
        extracted, receipt_error = discover_extracted_sources(video_path, _runtime._LANGUAGE_ALIASES, _runtime.LANGUAGES)
        if receipt_error:
            print(f'{_runtime.YELLOW}[SOURCE] Ignored extracted-subtitle receipt for {title}: {receipt_error}{_runtime.RESET}')
        for candidate in extracted:
            if retry_plan is not None and candidate.canonical_language == retry_target:
                continue
            prepared = prepare_extracted_source(candidate, artifact_access=_runtime._artifact_access, normalize=normalize_managed_file)
            if prepared.error:
                rejected_sources += 1
                print(f'{_runtime.YELLOW}[SOURCE] Rejected extracted source for {title}: {prepared.error}{_runtime.RESET}')
                continue
            if prepared.changed:
                with stats_lock:
                    stats['source_duplicate_groups'] = stats.get('source_duplicate_groups', 0) + prepared.duplicate_groups
                    stats['source_duplicate_cues_removed'] = stats.get('source_duplicate_cues_removed', 0) + prepared.removed_cues
                print(f'[SOURCE] Deduplicated {candidate.path.name}: groups={prepared.duplicate_groups} removed_cues={prepared.removed_cues}')
            if _runtime._source_is_usable(str(candidate.path), candidate.canonical_language, media_duration, title, item_type, stats, stats_lock, origin='embedded'):
                source_lang = candidate.canonical_language
                source_path = str(candidate.path)
                source_origin = 'embedded'
                source_variant = candidate.variant
                with stats_lock:
                    stats['embedded_sources_selected'] = stats.get('embedded_sources_selected', 0) + 1
                break
            rejected_sources += 1
    source_langs = [l for l in _runtime.LANGUAGES if l in available_by_lang and (retry_plan is None or l != retry_target)]
    if not source_path:
        for candidate_lang in source_langs:
            for candidate_path in available_by_lang[candidate_lang]:
                if _runtime._source_is_usable(candidate_path, candidate_lang, media_duration, title, item_type, stats, stats_lock):
                    source_lang = candidate_lang
                    source_path = candidate_path
                    break
                rejected_sources += 1
            if source_path:
                break
    if source_origin == 'embedded' and source_lang in target_langs:
        target_langs.remove(source_lang)
        _runtime._status_transition(item_type, item_id, source_lang, 'deferred', reason='embedded subtitle already on disk')
    for already_available in missing - set(target_langs) - ({source_lang} if source_origin == 'embedded' else set()):
        _runtime._status_transition(item_type, item_id, already_available, 'deferred', reason='subtitle already reported on disk')
    if not source_path:
        print(f'[SKIP] {title}: no source subtitle available from {_runtime.LANGUAGES}')
        for target_lang in target_langs:
            _runtime._status_transition(item_type, item_id, target_lang, 'missing_source', reason='no complete source subtitle')
        return
    if not target_langs:
        return
    _runtime._status_set_episode_identity(item_type, item_id, source_path)
    if rejected_sources:
        with stats_lock:
            stats['cleanup_alternative_sources'] = stats.get('cleanup_alternative_sources', 0) + 1
        print(f"[SOURCE] {title}: selected fallback '{source_lang}' after rejecting {rejected_sources} source(s)")
    if item_type == 'episodes':
        _se = _runtime._re.search('[Ss](\\d{1,2})[Ee](\\d{1,2})', _runtime.os.path.basename(source_path))
        if _se:
            title = f'{title} S{int(_se.group(1)):02d}E{int(_se.group(2)):02d}'
    print(f'[INFO] {title}: source={source_lang} ({source_origin}), targets={target_langs}')
    if retry_plan is not None:
        current_source_hash = _runtime._file_hash_or_none(source_path)
        if current_source_hash and current_source_hash != retry_plan.get('sourceHash'):
            retry_target = str(retry_plan['targetLanguage']).lower()
            if source_origin == 'embedded' and video_path:
                from .subtitles.sources import canonical_target_path
                replacement_target = str(canonical_target_path(video_path, retry_target, source_variant))
            else:
                replacement_target = _runtime._derive_target_path(source_path, source_lang, retry_target)
            _runtime._get_validation_state().schedule_retry_plan(item_type=item_type, item_id=item_id, target_language=retry_target, source_hash=current_source_hash, source_path=source_path, source_language=source_lang, target_path=replacement_target, series_key=series_key, series_title=series_title, media_title=title, source_cue_count=_runtime._count_srt_cues(source_path), failure_class=retry_plan.get('failureClass') or 'whole_file', rules=retry_plan.get('rules') or [], state='regeneration_waiting', eligible_completed_cycle=_runtime._completed_cycle + _runtime.REGENERATION_INITIAL_DELAY_CYCLES, reason='source changed; retry plan superseded')
            _runtime._get_validation_state().reset_circuit(series_key, 'source subtitle fingerprint changed')
            _runtime._status_transition(item_type, item_id, retry_target, 'waiting_retry', reason='source changed; retry plan superseded')
            return
    media_id = _runtime.lingarr_resolve_media_id(item_type, item_id)
    if media_id is None:
        print(f'{_runtime.YELLOW}[SKIP] {title}: not found in Lingarr media cache (id={item_id}){_runtime.RESET}')
        for target_lang in target_langs:
            _runtime._status_transition(item_type, item_id, target_lang, 'deferred', reason='media missing from Lingarr cache')
        return
    for target_lang in target_langs:
        if _runtime.shutdown_requested:
            break
        provider_target_path = _runtime._derive_target_path(source_path, source_lang, target_lang)
        target_path = provider_target_path
        if source_origin == 'embedded' and video_path:
            from .subtitles.sources import canonical_target_path
            target_path = str(canonical_target_path(video_path, target_lang, source_variant))
        if not target_path and video_path:
            target_path = _runtime.os.path.splitext(video_path)[0] + f'.{target_lang}.srt'
        if not target_path:
            print(f"{_runtime.YELLOW}[SKIP] {title} '{target_lang}': could not derive target path{_runtime.RESET}")
            _runtime._status_transition(item_type, item_id, target_lang, 'deferred', reason='target path unavailable')
            continue
        if retry_plan is None:
            try:
                scheduled = _runtime._get_validation_state().active_retry_plan(item_type, item_id, target_lang)
            except _runtime.StateStoreError as exc:
                print(f'{_runtime.YELLOW}[RETRY] Could not check retry plan: {exc}{_runtime.RESET}')
                scheduled = None
            if isinstance(scheduled, dict):
                current_source_hash = _runtime._file_hash_or_none(source_path)
                if current_source_hash and current_source_hash != scheduled.get('sourceHash'):
                    try:
                        scheduled, _ = _runtime._get_validation_state().schedule_retry_plan(item_type=item_type, item_id=item_id, target_language=target_lang, source_hash=current_source_hash, source_path=source_path, source_language=source_lang, target_path=target_path, series_key=series_key, series_title=series_title, media_title=title, source_cue_count=_runtime._count_srt_cues(source_path), failure_class=scheduled.get('failureClass') or 'whole_file', rules=scheduled.get('rules') or [], state='regeneration_waiting', eligible_completed_cycle=_runtime._completed_cycle + _runtime.REGENERATION_INITIAL_DELAY_CYCLES, reason='source changed; retry plan reset')
                        _runtime._get_validation_state().reset_circuit(series_key, 'source subtitle fingerprint changed')
                        print(f"[RETRY] Source changed for {title} '{target_lang}'; superseded the old plan and reset attempts")
                    except _runtime.StateStoreError as exc:
                        print(f'{_runtime.YELLOW}[RETRY] Could not reset changed-source plan: {exc}{_runtime.RESET}')
                with stats_lock:
                    stats['deferred'] = stats.get('deferred', 0) + 1
                print(f"[RETRY] Deferred normal queue for {title} '{target_lang}': state={scheduled['state']} eligible_cycle={scheduled['eligibleCompletedCycle']}")
                _runtime._status_transition(item_type, item_id, target_lang, 'waiting_retry', reason=f"scheduled retry {scheduled['state']}")
                continue
        target_suffix = _runtime._target_suffix(target_path, target_lang)
        target_variant = target_suffix[1] if target_suffix is not None else ''
        print(f"[TRANSLATE] Expected target for {title} '{target_lang}': {_runtime.os.path.basename(target_path)}")
        existing = _runtime._find_existing_target(video_path, target_lang) if video_path else target_path if _runtime.os.path.exists(target_path) else None
        if existing:
            print(f"[DISK] {title} '{target_lang}': {_runtime.os.path.basename(existing)} already on disk")
            submission = _runtime._find_submission_for_target(existing, target_lang)
            recovered_origin = 'lingarr' if _runtime._submission_matches_source(submission, source_path, source_lang, existing, target_lang) else None
            if recovered_origin:
                with stats_lock:
                    stats['recovered_pending_outputs'] = stats.get('recovered_pending_outputs', 0) + 1
                print(f'[TRANSLATE] Recovered pending Lingarr output {_runtime.os.path.basename(existing)}')
                if not _runtime._normalize_managed_output(existing, title):
                    with stats_lock:
                        stats['deferred'] = stats.get('deferred', 0) + 1
                    _runtime._status_transition(item_type, item_id, target_lang, 'deferred', reason='managed file ownership failed')
                    continue
            _runtime._status_transition(item_type, item_id, target_lang, 'validating')
            validation_action, validation_report = _runtime._validate_translated_file(source_path, existing, source_lang, target_lang, item_id, title=title, defer_repair=True, item_type=item_type, media_duration=media_duration, origin=recovered_origin, provenance_source_hash=submission.get('sourceHash') if recovered_origin else None, series_key=series_key, series_title=series_title)
            if validation_action in ('valid', 'valid-warning', 'formatted', 'repaired'):
                _runtime._resolve_existing_retry_success(
                    retry_plan, series_key, series_title,
                )
                with stats_lock:
                    stats['completed'] += 1
                    stats['translations'].append(f'{title}: {source_lang} -> {target_lang} (on disk)')
                    _runtime._record_cleanup_stats(stats, validation_action, validation_report)
                    _runtime._mark_activity(stats, item_type)
            elif validation_action.startswith('repair-'):
                with stats_lock:
                    stats['cleanup_repair_queued'] = stats.get('cleanup_repair_queued', 0) + (validation_action == 'repair-queued')
                    stats['cleanup_repair_deferred'] = stats.get('cleanup_repair_deferred', 0) + (validation_action == 'repair-deferred')
            else:
                _runtime._record_invalid_circuit_outcome(series_key, series_title, validation_action, validation_report)
                with stats_lock:
                    stats['failed'] += 1
                    stats.setdefault('cleaned', 0)
                    stats['cleaned'] += 1
                    _runtime._record_cleanup_stats(stats, validation_action, validation_report)
                    if validation_action in ('quarantined', 'deleted'):
                        _runtime._mark_activity(stats, item_type)
            _runtime._status_finish_validation(item_type, item_id, target_lang, validation_action)
            continue
        if video_path:
            suppression = _runtime._cycle_quarantine_suppression(video_path, target_lang)
            if suppression is not None:
                with stats_lock:
                    stats['cycle_suppressions'] = stats.get('cycle_suppressions', 0) + 1
                    stats['deferred'] = stats.get('deferred', 0) + 1
                print(f"[SKIP] {title} '{target_lang}': {suppression.get('action', 'cleanup')} already occurred during this cycle")
                _runtime._status_transition(item_type, item_id, target_lang, 'deferred', reason='same-cycle quarantine suppression')
                continue
        try:
            age = _runtime._check_cooldown(item_id, target_lang, item_type)
        except _runtime.StateStoreError as exc:
            print(f'{_runtime.YELLOW}[DEFER] State unavailable for cooldown check: {exc}{_runtime.RESET}')
            with stats_lock:
                stats['deferred'] = stats.get('deferred', 0) + 1
            _runtime._status_transition(item_type, item_id, target_lang, 'deferred', reason='persistent state unavailable')
            continue
        if age is not None:
            cooldown_remaining = _runtime.RESUBMIT_COOLDOWN - age
            print(f"[SKIP] {title} '{target_lang}': submitted {age}s ago, cooldown {cooldown_remaining}s remaining")
            _runtime._status_transition(item_type, item_id, target_lang, 'deferred', reason='resubmit cooldown')
            with stats_lock:
                stats['deferred'] = stats.get('deferred', 0) + 1
                stats['cooldown_deferrals'] = stats.get('cooldown_deferrals', 0) + 1
            continue
        try:
            circuit = _runtime._get_validation_state().circuit_permission(series_key=series_key, series_title=series_title, config_fingerprint=_runtime._CIRCUIT_CONFIG_FINGERPRINT, claim=False)
        except _runtime.StateStoreError as exc:
            print(f'{_runtime.YELLOW}[CIRCUIT] State unavailable for {title}: {exc}{_runtime.RESET}')
            circuit = {'allowed': True, 'state': 'unknown', 'failures': 0}
        if not circuit['allowed']:
            eligible_cycle = circuit.get('eligibleAfterCycle')
            print(f"{_runtime.YELLOW}[CIRCUIT] Deferred {title}: {circuit['state']} after {circuit.get('failures', 0)} failures; eligible_after_cycle={eligible_cycle}{_runtime.RESET}")
            _runtime._status_transition(item_type, item_id, target_lang, 'series_protected', reason=f"series circuit {circuit['state']}")
            with stats_lock:
                stats['deferred'] = stats.get('deferred', 0) + 1
                stats['circuit_deferrals'] = stats.get('circuit_deferrals', 0) + 1
            continue
        src_lines = _runtime._count_dialogue_lines(source_path)
        if src_lines is None or src_lines == 0:
            reason = 'source unreadable' if src_lines is None else 'source has no dialogue'
            print(f"{_runtime.YELLOW}[SKIP] {title} '{target_lang}': {reason}{_runtime.RESET}")
            with stats_lock:
                stats['deferred'] = stats.get('deferred', 0) + 1
            _runtime._status_transition(item_type, item_id, target_lang, 'missing_source', reason=reason)
            continue
        try:
            timing = _runtime._estimate_timeout(source_path, source_lang, target_lang)
        except TypeError:
            timeout_override = int(_runtime._estimate_timeout(source_path))
            cue_count = _runtime._count_srt_cues(source_path) or src_lines
            timing = {'cueCount': cue_count, 'secondsPerCue': timeout_override / cue_count if cue_count else 0.0, 'sampleCount': 0, 'scope': 'override', 'estimatedSeconds': timeout_override, 'timeoutSeconds': timeout_override, 'lane': 'long' if timeout_override > _runtime.LONG_JOB_THRESHOLD else 'short'}
        shared_token = _runtime._shared_capacity.acquire_translation()
        if shared_token is None:
            _runtime._status_transition(item_type, item_id, target_lang, 'deferred', reason='shared capacity unavailable')
            continue
        lane = _runtime._file_lane_gate.acquire(timing['lane'] == 'long', timing['estimatedSeconds'])
        if lane is None:
            _runtime._shared_capacity.release(shared_token)
            _runtime._status_transition(item_type, item_id, target_lang, 'deferred', reason='file lane unavailable')
            continue
        if lane == 'short (borrowed)':
            print(f"[LANE] {title} '{target_lang}' borrowed the idle long slot (estimate={timing['estimatedSeconds']:.0f}s)")
        elif lane == 'long (borrowed)':
            print(f"[LANE] {title} '{target_lang}' borrowed the idle short slot (estimate={timing['estimatedSeconds']:.0f}s)")
        try:
            queued_circuit = _runtime._get_validation_state().circuit_permission(series_key=series_key, series_title=series_title, config_fingerprint=_runtime._CIRCUIT_CONFIG_FINGERPRINT, claim=False)
        except _runtime.StateStoreError as exc:
            print(f'{_runtime.YELLOW}[CIRCUIT] State unavailable for {title}: {exc}{_runtime.RESET}')
            queued_circuit = {'allowed': True, 'state': 'unknown', 'failures': 0}
        if not queued_circuit['allowed']:
            _runtime._file_lane_gate.release(lane)
            _runtime._shared_capacity.release(shared_token)
            eligible_cycle = queued_circuit.get('eligibleAfterCycle')
            print(f"{_runtime.YELLOW}[CIRCUIT] Released {lane} slot for {title}: protection opened while queued; state={queued_circuit['state']} failures={queued_circuit.get('failures', 0)} eligible_after_cycle={eligible_cycle}{_runtime.RESET}")
            with stats_lock:
                stats['deferred'] = stats.get('deferred', 0) + 1
                stats['circuit_deferrals'] = stats.get('circuit_deferrals', 0) + 1
            _runtime._status_transition(item_type, item_id, target_lang, 'series_protected', reason=f"series circuit {queued_circuit['state']}")
            continue
        capacity_token = _runtime._translation_capacity.acquire(media_id, lingarr_media_type)
        if capacity_token is None:
            _runtime._file_lane_gate.release(lane)
            _runtime._shared_capacity.release(shared_token)
            with stats_lock:
                stats['api_errors'] = stats.get('api_errors', 0) + 1
                stats['deferred'] = stats.get('deferred', 0) + 1
            _runtime._status_transition(item_type, item_id, target_lang, 'deferred', reason='Lingarr capacity unavailable')
            continue
        appeared = _runtime._find_existing_target(video_path, target_lang) if video_path else target_path if _runtime.os.path.exists(target_path) else None
        if appeared:
            _runtime._translation_capacity.release(capacity_token)
            _runtime._file_lane_gate.release(lane)
            capacity_token = None
            appeared_submission = _runtime._find_submission_for_target(appeared, target_lang)
            appeared_origin = 'lingarr' if _runtime._submission_matches_source(appeared_submission, source_path, source_lang, appeared, target_lang) else None
            if appeared_origin and (not _runtime._normalize_managed_output(appeared, title)):
                with stats_lock:
                    stats['deferred'] = stats.get('deferred', 0) + 1
                _runtime._status_transition(item_type, item_id, target_lang, 'deferred', reason='managed file ownership failed')
                continue
            print(f"[DISK] {title} '{target_lang}': appeared during queue wait")
            _runtime._status_transition(item_type, item_id, target_lang, 'validating')
            validation_action, validation_report = _runtime._validate_translated_file(source_path, appeared, source_lang, target_lang, item_id, title=title, defer_repair=True, item_type=item_type, media_duration=media_duration, origin=appeared_origin, provenance_source_hash=appeared_submission.get('sourceHash') if appeared_origin else None, series_key=series_key, series_title=series_title)
            if validation_action in ('valid', 'valid-warning', 'formatted', 'repaired'):
                _runtime._resolve_existing_retry_success(
                    retry_plan, series_key, series_title,
                )
                with stats_lock:
                    stats['completed'] += 1
                    stats['translations'].append(f'{title}: {source_lang} -> {target_lang} (on disk)')
                    _runtime._record_cleanup_stats(stats, validation_action, validation_report)
                    _runtime._mark_activity(stats, item_type)
            elif validation_action.startswith('repair-'):
                with stats_lock:
                    stats['cleanup_repair_queued'] = stats.get('cleanup_repair_queued', 0) + (validation_action == 'repair-queued')
                    stats['cleanup_repair_deferred'] = stats.get('cleanup_repair_deferred', 0) + (validation_action == 'repair-deferred')
            else:
                _runtime._record_invalid_circuit_outcome(series_key, series_title, validation_action, validation_report)
                with stats_lock:
                    stats['failed'] += 1
                    _runtime._record_cleanup_stats(stats, validation_action, validation_report)
                    if validation_action in ('quarantined', 'deleted'):
                        _runtime._mark_activity(stats, item_type)
            _runtime._status_finish_validation(item_type, item_id, target_lang, validation_action)
            _runtime._shared_capacity.release(shared_token)
            continue
        src_lines = _runtime._count_dialogue_lines(source_path)
        if src_lines is None:
            _runtime._translation_capacity.release(capacity_token)
            _runtime._file_lane_gate.release(lane)
            _runtime._shared_capacity.release(shared_token)
            print(f"{_runtime.YELLOW}[SKIP] {title} '{target_lang}': source not readable — deferring{_runtime.RESET}")
            with stats_lock:
                stats.setdefault('deferred', 0)
                stats['deferred'] += 1
            _runtime._status_transition(item_type, item_id, target_lang, 'missing_source', reason='source unreadable')
            continue
        if src_lines == 0:
            _runtime._translation_capacity.release(capacity_token)
            _runtime._file_lane_gate.release(lane)
            _runtime._shared_capacity.release(shared_token)
            print(f"{_runtime.YELLOW}[SKIP] {title} '{target_lang}': source has no dialogue lines{_runtime.RESET}")
            with stats_lock:
                stats.setdefault('deferred', 0)
                stats['deferred'] += 1
            _runtime._status_transition(item_type, item_id, target_lang, 'missing_source', reason='source has no dialogue')
            continue
        extra_target_paths = (
            [provider_target_path]
            if provider_target_path and _runtime.os.path.normcase(_runtime.os.path.abspath(provider_target_path)) != _runtime.os.path.normcase(_runtime.os.path.abspath(target_path))
            else []
        )
        target_snapshot = _runtime._snapshot_target_sidecars(video_path, target_lang, extra_target_paths) if video_path else {}
        source_hash = _runtime._file_hash_or_none(source_path)
        print(f'[TRANSLATE] {title}: {source_lang} -> {target_lang} ({src_lines} lines)')
        try:
            attempt_id = _runtime._record_submission(item_id, target_lang, target_path, expected_target_path=target_path, video_path=video_path or None, source_path=source_path, source_hash=source_hash, source_language=source_lang, item_type=item_type, target_variant=target_variant, status='reserved')
        except (_runtime.StateStoreError, OSError) as exc:
            _runtime._translation_capacity.release(capacity_token)
            _runtime._file_lane_gate.release(lane)
            _runtime._shared_capacity.release(shared_token)
            print(f"{_runtime.YELLOW}[DEFER] Could not reserve durable translation state for {title} '{target_lang}': {exc}{_runtime.RESET}")
            with stats_lock:
                stats['deferred'] = stats.get('deferred', 0) + 1
            _runtime._status_transition(item_type, item_id, target_lang, 'deferred', reason='could not persist translation reservation')
            continue
        if retry_plan is not None:
            try:
                retry_bound = _runtime._get_validation_state().bind_retry_submission(retry_plan['id'], retry_plan.get('claimOwner') or '', attempt_id)
            except _runtime.StateStoreError:
                retry_bound = False
            if not retry_bound:
                _runtime._mark_submission_failed(attempt_id)
                _runtime._translation_capacity.release(capacity_token)
                _runtime._file_lane_gate.release(lane)
                _runtime._shared_capacity.release(shared_token)
                _runtime._get_validation_state().reschedule_retry_no_progress(retry_plan['id'], completed_cycle=_runtime._completed_cycle, deferral_class='claim_binding_failed', reason='retry submission reservation could not be bound')
                continue
        status: str | None = None
        translation_started = _runtime.time.monotonic()
        job_id: int | None = None
        trial_owner = f'attempt:{attempt_id}'
        trial_claimed = False
        trial_generation: int | None = None
        try:
            try:
                circuit = _runtime._get_validation_state().circuit_permission(series_key=series_key, series_title=series_title, config_fingerprint=_runtime._CIRCUIT_CONFIG_FINGERPRINT, claim=retry_plan is not None, trial_owner=trial_owner)
            except _runtime.StateStoreError as exc:
                print(f'{_runtime.YELLOW}[CIRCUIT] State unavailable for {title}: {exc}{_runtime.RESET}')
                circuit = {'allowed': True, 'state': 'unknown', 'failures': 0}
            if circuit.get('state') == 'eligible' and retry_plan is None:
                circuit = {**circuit, 'allowed': False}
            if not circuit['allowed']:
                _runtime._mark_submission_failed(attempt_id)
                eligible_cycle = circuit.get('eligibleAfterCycle')
                print(f"{_runtime.YELLOW}[CIRCUIT] Deferred {title} before submission: state={circuit['state']} failures={circuit.get('failures', 0)} eligible_after_cycle={eligible_cycle}; released_slot={lane}{_runtime.RESET}")
                with stats_lock:
                    stats['deferred'] = stats.get('deferred', 0) + 1
                    stats['circuit_deferrals'] = stats.get('circuit_deferrals', 0) + 1
                _runtime._status_transition(item_type, item_id, target_lang, 'series_protected', reason=f"series circuit {circuit['state']}")
                _runtime._shared_capacity.release(shared_token)
                continue
            trial_claimed = circuit.get('state') == 'half_open'
            trial_generation = circuit.get('leaseGeneration')
            job_id = _runtime.lingarr_submit_file(media_id, source_path, source_lang, target_lang, lingarr_media_type)
            if job_id is None:
                _runtime._mark_submission_failed(attempt_id)
                if trial_claimed:
                    try:
                        _runtime._get_validation_state().release_circuit_trial(series_key=series_key, trial_owner=trial_owner, reason='Lingarr submission did not create a job')
                    except _runtime.StateStoreError as exc:
                        print(f'{_runtime.YELLOW}[CIRCUIT] Could not release unsubmitted half-open trial: {exc}{_runtime.RESET}')
                with stats_lock:
                    stats['failed'] += 1
                _runtime._status_transition(item_type, item_id, target_lang, 'failed', reason='Lingarr submission failed')
                _runtime._shared_capacity.release(shared_token)
                continue
            if retry_plan is not None:
                if retry_submission_callback is not None:
                    retry_submission_callback(retry_plan)
                try:
                    _runtime._get_validation_state().record_retry_admission(retry_plan['id'], _runtime._completed_cycle, 'submitted')
                except _runtime.StateStoreError:
                    pass
            if trial_claimed:
                try:
                    trial_bound = _runtime._get_validation_state().bind_circuit_trial_job(series_key, trial_owner, job_id, trial_plan_id=retry_plan['id'] if retry_plan is not None else None, lease_generation=trial_generation)
                except _runtime.StateStoreError as exc:
                    trial_bound = False
                    print(f'{_runtime.YELLOW}[CIRCUIT] Could not bind half-open trial to Lingarr job {job_id}: {exc}{_runtime.RESET}')
                if not trial_bound:
                    _runtime.lingarr_cancel_job(job_id)
                    _runtime._mark_submission_failed(attempt_id)
                    try:
                        _runtime._get_validation_state().release_circuit_trial(series_key, trial_owner, 'trial job binding failed before monitoring')
                    except _runtime.StateStoreError:
                        pass
                    job_id = None
                    with stats_lock:
                        stats['degraded'] = True
                    _runtime._status_transition(item_type, item_id, target_lang, 'deferred', reason='circuit trial binding failed')
                    _runtime._shared_capacity.release(shared_token)
                    continue
            try:
                _runtime._mark_submission_submitted(attempt_id, job_id)
            except _runtime.StateStoreError as exc:
                print(f"{_runtime.YELLOW}[STATE] Lingarr accepted {title} '{target_lang}' but its job state could not be persisted; continuing to monitor job {job_id}: {exc}{_runtime.RESET}")
                with stats_lock:
                    stats['degraded'] = True
            _runtime._status_transition(item_type, item_id, target_lang, 'translating', reason='waiting for Lingarr output', details={'cueCount': timing['cueCount'], 'secondsPerCue': round(timing['secondsPerCue'], 4), 'timingSampleCount': timing['sampleCount'], 'timingScope': timing['scope'], 'estimatedSeconds': timing['estimatedSeconds'], 'timeoutSeconds': timing['timeoutSeconds'], 'etaSeconds': timing['estimatedSeconds'], 'lane': lane, 'attempts': 1, 'jobId': job_id, 'circuit': circuit})
            with stats_lock:
                stats['submitted'] += 1
                _runtime._mark_activity(stats, item_type)
            deadline = _runtime.time.time() + timing['timeoutSeconds']
            progress_callback = lambda progress: _runtime._status_transition(item_type, item_id, target_lang, 'translating', details={'progress': progress, 'etaSeconds': max(0, round(timing['estimatedSeconds'] * (1.0 - min(100, max(0, progress)) / 100.0), 1))})
            try:
                status = _runtime.lingarr_poll_job(job_id, deadline, title, progress_callback=progress_callback)
            except TypeError:
                status = _runtime.lingarr_poll_job(job_id, deadline, title)
        finally:
            if trial_claimed and job_id is None:
                try:
                    _runtime._get_validation_state().release_circuit_trial(series_key=series_key, trial_owner=trial_owner, reason='submission path exited before a Lingarr job was created')
                except _runtime.StateStoreError as exc:
                    print(f'{_runtime.YELLOW}[CIRCUIT] Could not release abandoned half-open claim: {exc}{_runtime.RESET}')
            _runtime._translation_capacity.release(capacity_token)
            _runtime._file_lane_gate.release(lane)
        translation_elapsed = _runtime.time.monotonic() - translation_started
        terminal_job = _runtime.lingarr_get_job(job_id) if status != 'Completed' and job_id is not None else None
        failure_details = _runtime._safe_failure_details(job_id, terminal_job=terminal_job, elapsed_seconds=translation_elapsed)
        if status != 'Completed':
            safe_to_recover = status is not None
            if status is None and job_id is not None:
                safe_to_recover = _runtime.lingarr_cancel_job(job_id)
            recovery = _runtime._recover_failed_lingarr_job(job_id, source_path, target_path, source_lang, target_lang, title) if job_id is not None and safe_to_recover and (not _runtime.shutdown_requested) else {'recovered': False, 'reason': 'job unavailable'}
            if recovery.get('recovered'):
                status = 'Completed'
                translation_elapsed += float(recovery.get('repairElapsedSeconds', 0))
        if status != 'Completed':
            failure_reason = 'Lingarr timeout' if status is None else f'Lingarr job {status.lower()}'
            try:
                _runtime._mark_submission_failed(attempt_id, failure_category=failure_details.get('category'), failure_details=failure_details)
            except _runtime.StateStoreError as exc:
                print(f'{_runtime.YELLOW}[FAIL] Could not persist Lingarr failure details: {exc}{_runtime.RESET}')
            print(f"[FAILURE] Lingarr job {job_id}: category={failure_details.get('category', 'unknown')} status={failure_details.get('status') or status or 'timeout'} provider={failure_details.get('provider', 'unknown')} model={failure_details.get('model', 'unknown')} message={failure_details.get('errorMessage') or 'not supplied'}")
            try:
                _runtime._get_validation_state().record_circuit_outcome(series_key=series_key, series_title=series_title, success=False, reason=failure_reason, threshold=_runtime.CIRCUIT_FAILURE_THRESHOLD, open_cycles=_runtime.CIRCUIT_OPEN_CYCLES, config_fingerprint=_runtime._CIRCUIT_CONFIG_FINGERPRINT, trial_owner=trial_owner if trial_claimed else None, trial_job_id=job_id if trial_claimed else None, trial_plan_id=retry_plan['id'] if trial_claimed and retry_plan else None, lease_generation=trial_generation if trial_claimed else None)
            except _runtime.StateStoreError as exc:
                print(f'{_runtime.YELLOW}[CIRCUIT] Could not record failure: {exc}{_runtime.RESET}')
            _runtime._refresh_status_diagnostics()
            with stats_lock:
                if status is None:
                    stats['timed_out'] += 1
                else:
                    stats['failed'] += 1
            if status is None and _runtime.shutdown_requested:
                _runtime._status_transition(item_type, item_id, target_lang, 'deferred', reason='service shutdown')
            elif status is None:
                _runtime._status_transition(item_type, item_id, target_lang, 'timed_out', reason='Lingarr timeout', details={'failureDetails': failure_details})
            else:
                _runtime._status_transition(item_type, item_id, target_lang, 'failed', reason=f'Lingarr job {status.lower()}', details={'failureDetails': failure_details})
            _runtime._shared_capacity.release(shared_token)
            continue
        actual_target_path = _runtime._discover_completed_target(video_path, target_lang, target_path, target_snapshot, extra_target_paths) if video_path else target_path if _runtime.os.path.exists(target_path) else None
        if actual_target_path is None:
            print(f"{_runtime.YELLOW}[WARNING] {title} '{target_lang}': Lingarr completed but no new target-language sidecar was found (expected {target_path}){_runtime.RESET}")
            with stats_lock:
                stats['timed_out'] += 1
            _runtime._status_transition(item_type, item_id, target_lang, 'timed_out', reason='completed output missing')
            _defer_bound_retry_trial(retry_plan, trial_claimed, trial_generation, 'completed output missing')
            _runtime._shared_capacity.release(shared_token)
            continue
        if source_origin == 'embedded' and _runtime.os.path.normcase(_runtime.os.path.abspath(actual_target_path)) != _runtime.os.path.normcase(_runtime.os.path.abspath(target_path)):
            actual_target_path = _runtime._publish_canonical_target(actual_target_path, target_path, title)
            if actual_target_path is None:
                with stats_lock:
                    stats['deferred'] = stats.get('deferred', 0) + 1
                _runtime._status_transition(item_type, item_id, target_lang, 'deferred', reason='canonical target appeared concurrently')
                _defer_bound_retry_trial(retry_plan, trial_claimed, trial_generation, 'canonical target appeared concurrently')
                _runtime._shared_capacity.release(shared_token)
                continue
        if not _runtime._normalize_managed_output(actual_target_path, title):
            with stats_lock:
                stats['deferred'] = stats.get('deferred', 0) + 1
            _runtime._status_transition(item_type, item_id, target_lang, 'deferred', reason='managed file ownership failed')
            _defer_bound_retry_trial(retry_plan, trial_claimed, trial_generation, 'managed file ownership failed')
            _runtime._shared_capacity.release(shared_token)
            continue
        actual_suffix = _runtime._target_suffix(actual_target_path, target_lang)
        actual_variant = actual_suffix[1] if actual_suffix is not None else ''
        _runtime._update_submission_actual_path(item_id, target_lang, actual_target_path, actual_variant, item_type)
        if _runtime.os.path.normcase(_runtime.os.path.abspath(actual_target_path)) != _runtime.os.path.normcase(_runtime.os.path.abspath(target_path)):
            with stats_lock:
                stats['variant_outputs_discovered'] = stats.get('variant_outputs_discovered', 0) + 1
        if not _runtime._record_pending_lingarr_output(source_path, actual_target_path, source_lang, target_lang, item_type, item_id):
            with stats_lock:
                stats['deferred'] = stats.get('deferred', 0) + 1
            _runtime._status_transition(item_type, item_id, target_lang, 'deferred', reason='completed output provenance persistence failed')
            _defer_bound_retry_trial(retry_plan, trial_claimed, trial_generation, 'completed output provenance persistence failed')
            _runtime._shared_capacity.release(shared_token)
            continue
        if retry_plan is not None:
            try:
                retry_plan = _runtime._get_validation_state().update_retry_plan(retry_plan['id'], state='retry_in_progress', completed_cycle=_runtime._completed_cycle, increment_attempt=True, reason='fresh Lingarr output received')
            except _runtime.StateStoreError as exc:
                print(f'{_runtime.YELLOW}[RETRY] Could not record retry attempt: {exc}{_runtime.RESET}')
        _runtime._status_transition(item_type, item_id, target_lang, 'validating')
        validation_action, validation_report = _runtime._validate_translated_file(source_path, actual_target_path, source_lang, target_lang, item_id, title=title, defer_repair=True, item_type=item_type, media_duration=media_duration, origin='lingarr', provenance_source_hash=source_hash, series_key=series_key, series_title=series_title, retry_plan_id=retry_plan['id'] if retry_plan else None, trial_owner=trial_owner if trial_claimed else None, trial_job_id=job_id if trial_claimed else None, trial_plan_id=retry_plan['id'] if trial_claimed and retry_plan else None, trial_generation=trial_generation if trial_claimed else None)
        if validation_action in ('valid', 'valid-warning', 'formatted', 'repaired'):
            _runtime._record_successful_source_readiness(source_path, source_lang, actual_target_path, target_lang, media_duration)
            try:
                _runtime._get_validation_state().record_timing_sample(kind='file', source_language=source_lang, target_language=target_lang, cue_count=timing['cueCount'], elapsed_seconds=translation_elapsed, outcome='accepted', lingarr_job_id=job_id)
                _runtime._get_validation_state().record_circuit_outcome(series_key=series_key, series_title=series_title, success=True, reason=None, threshold=_runtime.CIRCUIT_FAILURE_THRESHOLD, open_cycles=_runtime.CIRCUIT_OPEN_CYCLES, config_fingerprint=_runtime._CIRCUIT_CONFIG_FINGERPRINT, trial_owner=trial_owner if trial_claimed else None, trial_job_id=job_id if trial_claimed else None, trial_plan_id=retry_plan['id'] if trial_claimed and retry_plan else None, lease_generation=trial_generation if trial_claimed else None)
            except _runtime.StateStoreError as exc:
                print(f'{_runtime.YELLOW}[TIMING] Could not persist successful sample: {exc}{_runtime.RESET}')
            _runtime._refresh_status_diagnostics()
            print(f"{_runtime.GREEN}[OK] {title} '{target_lang}' translated to {_runtime.os.path.basename(actual_target_path)}{_runtime.RESET}")
            with stats_lock:
                stats['completed'] += 1
                stats['translations'].append(f'{title}: {source_lang} -> {target_lang}')
                _runtime._record_cleanup_stats(stats, validation_action, validation_report)
                _runtime._mark_activity(stats, item_type)
        elif validation_action.startswith('repair-'):
            with stats_lock:
                stats['cleanup_repair_queued'] = stats.get('cleanup_repair_queued', 0) + (validation_action == 'repair-queued')
                stats['cleanup_repair_deferred'] = stats.get('cleanup_repair_deferred', 0) + (validation_action == 'repair-deferred')
        else:
            _runtime._record_invalid_circuit_outcome(series_key, series_title, validation_action, validation_report, trial_owner=trial_owner if trial_claimed else None, trial_job_id=job_id if trial_claimed else None, trial_plan_id=retry_plan['id'] if trial_claimed and retry_plan else None, trial_generation=trial_generation if trial_claimed else None)
            with stats_lock:
                stats['failed'] += 1
                stats.setdefault('cleaned', 0)
                stats['cleaned'] += 1
                _runtime._record_cleanup_stats(stats, validation_action, validation_report)
        if retry_plan is not None and validation_action in ('valid', 'valid-warning', 'formatted', 'repaired'):
            _runtime._resolve_retry_success(retry_plan['id'], retry_plan.get('sourceHash'), outcome='accepted_after_regeneration')
        _runtime._status_finish_validation(item_type, item_id, target_lang, validation_action)
        _runtime._shared_capacity.release(shared_token)
EXPORTS = {
    name: globals()[name] for name in (
        '_item_title', '_mark_activity', '_record_invalid_circuit_outcome',
        '_record_valid_circuit_outcome', '_resolve_existing_retry_success',
        '_defer_bound_retry_trial',
        '_bazarr_has_repaired_path',
        '_record_cleanup_stats', '_source_is_usable', 'process_item',
    )
}
