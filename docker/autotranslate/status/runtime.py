from __future__ import annotations
from ..composition import runtime as _runtime

def _register_runtime_resources(*, state_store: _runtime.StateStore | None=None, status_server=None) -> None:
    with _runtime._runtime_resources_lock:
        if state_store is not None:
            _runtime._active_state_store = state_store
        if status_server is not None:
            _runtime._active_status_server = status_server

def close_runtime_resources() -> None:
    """Close production resources once, in reverse construction order."""
    _runtime._shutdown_repair_executor()
    maintenance_pool = _runtime._maintenance_worker_pool
    _runtime._maintenance_worker_pool = None
    if maintenance_pool is not None:
        terminated = maintenance_pool.shutdown(
            wait_for_workers=True,
            grace_seconds=_runtime._shutdown_controller.remaining(),
        )
        if terminated:
            print(f'{_runtime.YELLOW}[MAINTENANCE] Terminated {terminated} worker process(es) after the shutdown deadline{_runtime.RESET}')
    with _runtime._runtime_resources_lock:
        status_server = _runtime._active_status_server
        state_store = _runtime._active_state_store
        _runtime._active_status_server = None
        _runtime._active_state_store = None
    if status_server is not None:
        status_server.shutdown()
        status_server.server_close()
    if state_store is not None:
        state_store.close()

class TranslationCapacityGate(_runtime._TranslationCapacityGate):

    def __init__(self, limit: int):
        super().__init__(limit, active_translations=lambda: _runtime.lingarr_get_active_translations(), shutdown_requested=lambda: _runtime.shutdown_requested, poll_interval=_runtime.POLL_INTERVAL, emit=print, service_errors=(_runtime.ServiceRequestError,))

class SharedCapacityCoordinator(_runtime._SharedCapacityCoordinator):

    def __init__(self, limit: int):
        super().__init__(limit, shutdown_requested=lambda: _runtime.shutdown_requested)

class FileLaneGate(_runtime._FileLaneGate):

    def __init__(self, workers: int):
        super().__init__(workers, shutdown_requested=lambda: _runtime.shutdown_requested)

def dbg(msg: str) -> None:
    if _runtime.DEBUG:
        print(f'[DEBUG] {msg}')

def _status_transition(item_type: str | None, item_id: int | None, target_lang: str, state: str, *, repaired: bool=False, reason: str | None=None, details: dict | None=None) -> bool:
    if _runtime._status_tracker is None:
        return False
    try:
        kwargs = {'repaired': repaired, 'reason': reason}
        if details is not None:
            kwargs['details'] = details
        return _runtime._status_tracker.transition_for(item_type, item_id, target_lang, state, **kwargs)
    except OSError as exc:
        print(f'{_runtime.YELLOW}[STATUS] Could not persist job update: {exc}{_runtime.RESET}')
        return False

def _status_identity(job_kwargs: dict, label: str, target_lang: str) -> dict:
    identity = _runtime.retry_media_identity({'itemType': job_kwargs.get('item_type'), 'itemId': job_kwargs.get('item_id'), 'targetLanguage': target_lang, 'mediaTitle': label, 'sourcePath': job_kwargs.get('source_path'), 'seriesTitle': job_kwargs.get('series_title')})
    return {'title': identity['displayTitle'], 'episodeCode': identity.get('episodeCode'), 'episodeTitle': identity.get('episodeTitle'), 'itemType': job_kwargs.get('item_type'), 'itemId': job_kwargs.get('item_id'), 'targetLanguage': target_lang, 'sourceLanguage': job_kwargs.get('source_lang')}

def _status_create_repair_ref(job_kwargs: dict, label: str, target_lang: str, details: dict) -> dict | None:
    if _runtime._status_tracker is None:
        return None
    try:
        cycle_key = _runtime._status_tracker.active_cycle_job_key(job_kwargs.get('item_type'), job_kwargs.get('item_id'), target_lang)
        if cycle_key:
            _runtime._status_tracker.transition(cycle_key, 'repair_queued', details=details)
            return {'kind': 'cycle', 'id': cycle_key}
        job_id = _runtime._status_tracker.create_maintenance_job('cue_repair', _runtime._status_identity(job_kwargs, label, target_lang), state='repair_queued', details=details)
        return {'kind': 'maintenance', 'id': job_id}
    except OSError as exc:
        print(f'{_runtime.YELLOW}[STATUS] Could not persist repair job: {exc}{_runtime.RESET}')
        return None

def _status_ref_transition(status_ref: dict | None, state: str, *, reason: str | None=None, details: dict | None=None) -> bool:
    if _runtime._status_tracker is None or not status_ref:
        return False
    try:
        if status_ref.get('kind') == 'cycle':
            return _runtime._status_tracker.transition(status_ref['id'], state, reason=reason, details=details)
        return _runtime._status_tracker.transition_maintenance(status_ref['id'], state, reason=reason, details=details)
    except OSError as exc:
        print(f'{_runtime.YELLOW}[STATUS] Could not persist repair progress: {exc}{_runtime.RESET}')
        return False

def _status_ref_complete(status_ref: dict | None, outcome: str, *, reason: str | None=None, repaired: bool=False, details: dict | None=None) -> bool:
    if _runtime._status_tracker is None or not status_ref:
        return False
    try:
        if status_ref.get('kind') == 'cycle':
            cycle_outcome = 'accepted' if outcome == 'repaired' else 'quarantined' if outcome in ('quarantined', 'deleted') else outcome
            return _runtime._status_tracker.transition(status_ref['id'], cycle_outcome, repaired=repaired or outcome == 'repaired', reason=reason, details=details)
        return _runtime._status_tracker.complete_maintenance(status_ref['id'], outcome, reason=reason, details=details)
    except OSError as exc:
        print(f'{_runtime.YELLOW}[STATUS] Could not persist repair completion: {exc}{_runtime.RESET}')
        return False

def _complete_repair_status(metadata: dict, outcome: str, *, reason: str | None=None, repaired: bool=False, details: dict | None=None) -> bool:
    status_ref = metadata.get('status_ref')
    if status_ref:
        return _runtime._status_ref_complete(status_ref, outcome, reason=reason, repaired=repaired, details=details)
    cycle_outcome = 'accepted' if outcome == 'repaired' else 'quarantined' if outcome in ('quarantined', 'deleted') else outcome
    return _runtime._status_transition(metadata.get('item_type'), metadata.get('item_id'), metadata.get('target_lang', ''), cycle_outcome, repaired=repaired or outcome == 'repaired', reason=reason)

def _status_set_episode_identity(item_type: str | None, item_id: int | None, path: str | _runtime.Path | None) -> None:
    if _runtime._status_tracker is None or item_type != 'episodes':
        return
    episode_code = _runtime.episode_identity_from_path(path)
    if not episode_code:
        return
    try:
        _runtime._status_tracker.set_episode_identity(item_type, item_id, episode_code)
    except OSError as exc:
        print(f'{_runtime.YELLOW}[STATUS] Could not persist episode identity: {exc}{_runtime.RESET}')

def _status_admit_retry(plan: dict, identity: dict) -> None:
    if _runtime._status_tracker is None:
        return
    try:
        _runtime._status_tracker.admit_retry(plan_id=plan['id'], item_type=plan['itemType'], item_id=plan['itemId'], target_language=plan['targetLanguage'], display_title=identity['displayTitle'], episode_code=identity.get('episodeCode'), episode_title=identity.get('episodeTitle'), attempt=int(plan.get('attemptCount', 0)) + 1)
    except OSError as exc:
        print(f'{_runtime.YELLOW}[STATUS] Could not admit retry job: {exc}{_runtime.RESET}')

def _refresh_status_diagnostics() -> None:
    if _runtime._status_facade is None:
        return
    target = next(iter(_runtime.CLEANUP_LANGUAGES), _runtime.LANGUAGES[-1] if _runtime.LANGUAGES else 'et')
    try:
        file_timing = _runtime._get_validation_state().timing_estimate(kind='file', source_language=None, target_language=target, cold_seconds_per_cue=_runtime.TRANSLATION_COLD_SECONDS_PER_CUE, alpha=_runtime.TRANSLATION_TIMING_ALPHA)
        repair_timing = _runtime._get_validation_state().timing_estimate(kind='repair', source_language=None, target_language=target, cold_seconds_per_cue=_runtime.TRANSLATION_COLD_SECONDS_PER_CUE, alpha=_runtime.TRANSLATION_TIMING_ALPHA)
        retry_plans = _runtime._get_validation_state().retry_plans()
        for plan in retry_plans:
            plan.update(_runtime.retry_media_identity(plan))
            attempts = _runtime._get_validation_state().quarantine_attempts(plan['itemType'], plan['itemId'], plan['targetLanguage'])
            plan['archivedAttemptCount'] = len(attempts)
            donor_sources = [donor.get('sourceAttempt') for attempt in attempts for donor in attempt.get('donorProvenance', []) if donor.get('sourceAttempt') is not None]
            plan['latestDonorAttempt'] = donor_sources[0] if donor_sources else None
            plan.update(_runtime._get_validation_state().recovery_summary(plan['itemType'], plan['itemId'], plan['targetLanguage']))
            plan['manualReview'] = plan.get('lastDeferralClass') == 'manual_review'
        _runtime._status_facade.set_diagnostics(timing={'file': file_timing, 'repair': repair_timing}, circuits=_runtime._get_validation_state().circuit_breakers(), retries=retry_plans, completed_cycle=_runtime._get_validation_state().completed_cycle(), retry_max_attempts=_runtime.REGENERATION_MAX_ATTEMPTS, recovery=_runtime._get_validation_state().diagnostic_aggregates())
    except (OSError, _runtime.StateStoreError) as exc:
        print(f'{_runtime.YELLOW}[STATUS] Could not refresh diagnostics: {exc}{_runtime.RESET}')

def _status_set_phase(phase: str, *, next_cycle_at: float | None=None) -> None:
    if _runtime._status_facade is None:
        return
    try:
        _runtime._status_facade.set_phase(phase, next_cycle_at=next_cycle_at)
    except OSError as exc:
        print(f'{_runtime.YELLOW}[STATUS] Could not persist service phase: {exc}{_runtime.RESET}')

def _status_start_cycle(cycle_id: str, cycle_number: int, jobs: list[dict]) -> None:
    if _runtime._status_tracker is None:
        return
    try:
        _runtime._status_tracker.start_cycle(cycle_id, cycle_number, jobs)
    except OSError as exc:
        print(f'{_runtime.YELLOW}[STATUS] Could not persist cycle start: {exc}{_runtime.RESET}')

def _status_finish_cycle(metrics: dict | None=None) -> None:
    if _runtime._status_tracker is None:
        return
    try:
        _runtime._status_tracker.finish_cycle(metrics)
    except OSError as exc:
        print(f'{_runtime.YELLOW}[STATUS] Could not persist cycle completion: {exc}{_runtime.RESET}')

def _status_record_maintenance(metrics: dict) -> None:
    if _runtime._status_tracker is None:
        return
    try:
        _runtime._status_tracker.record_maintenance(metrics)
    except OSError as exc:
        print(f'{_runtime.YELLOW}[STATUS] Could not persist maintenance status: {exc}{_runtime.RESET}')

def _status_create_maintenance(operation: str, identity: dict | None=None, *, state: str='queued', details: dict | None=None) -> str | None:
    if _runtime._status_tracker is None:
        return None
    try:
        return _runtime._status_tracker.create_maintenance_job(operation, identity, state=state, details=details)
    except OSError as exc:
        print(f'{_runtime.YELLOW}[STATUS] Could not create maintenance job: {exc}{_runtime.RESET}')
        return None

def _status_update_maintenance(job_id: str | None, state: str, *, reason: str | None=None, details: dict | None=None) -> bool:
    if _runtime._status_tracker is None or not job_id:
        return False
    try:
        return _runtime._status_tracker.transition_maintenance(job_id, state, reason=reason, details=details)
    except OSError as exc:
        print(f'{_runtime.YELLOW}[STATUS] Could not update maintenance job: {exc}{_runtime.RESET}')
        return False

def _status_complete_maintenance(job_id: str | None, outcome: str, *, reason: str | None=None, details: dict | None=None) -> bool:
    if _runtime._status_tracker is None or not job_id:
        return False
    try:
        return _runtime._status_tracker.complete_maintenance(job_id, outcome, reason=reason, details=details)
    except OSError as exc:
        print(f'{_runtime.YELLOW}[STATUS] Could not complete maintenance job: {exc}{_runtime.RESET}')
        return False

def _status_record_maintenance_outcome(operation: str, outcome: str, identity: dict | None=None, *, reason: str | None=None) -> None:
    if _runtime._status_tracker is None:
        return
    try:
        _runtime._status_tracker.record_maintenance_outcome(operation, outcome, identity, reason=reason)
    except OSError as exc:
        print(f'{_runtime.YELLOW}[STATUS] Could not record maintenance outcome: {exc}{_runtime.RESET}')

def _maintenance_file_identity(path: str | _runtime.Path, target_language: str | None=None) -> dict:
    identity = _runtime.retry_media_identity({'itemType': 'media', 'mediaTitle': _runtime.Path(path).name, 'targetLanguage': target_language})
    return {'title': identity['displayTitle'], 'episodeCode': identity.get('episodeCode'), 'episodeTitle': identity.get('episodeTitle'), 'targetLanguage': target_language}

def _maintenance_metrics(stats: dict) -> dict:
    return {'formatted': stats.get('formatted_files', 0), 'repaired': stats.get('repaired_files', 0) + stats.get('async_repairs_completed', 0), 'quarantined': stats.get('quarantined_files', 0) + stats.get('undersized_quarantined', 0) + stats.get('prune_quarantined', 0), 'deleted': stats.get('deleted_files', 0) + stats.get('prune_deleted', 0), 'undersized': stats.get('undersized_detected', 0), 'pruned': stats.get('prune_quarantined', 0) + stats.get('prune_deleted', 0), 'source_less_warnings': stats.get('source_less_warnings', 0), 'repeat_quarantines': stats.get('repeat_quarantines', 0), 'cycle_suppressions': stats.get('cycle_suppressions', 0), 'variant_outputs': stats.get('variant_outputs_discovered', 0) + stats.get('recovered_pending_outputs', 0), 'cache_hits': stats.get('cache_hits', 0), 'worker_failures': stats.get('worker_failures', 0), 'failures': stats.get('repair_failures', 0) + stats.get('action_failures', 0) + stats.get('prune_failures', 0) + stats.get('async_repair_failures', 0) + stats.get('worker_failures', 0)}

def _scan_progress_details(context: dict) -> dict:
    stats = context.get('stats', {})
    discovered = int(context.get('files_discovered', 0))
    checked = int(context.get('files_checked', 0))
    elapsed = max(0.001, _runtime.time.monotonic() - context['started'])
    remaining = max(0, discovered - checked)
    eta = round(elapsed / checked * remaining, 1) if checked else None
    details = {'filesDiscovered': discovered, 'filesChecked': checked, 'filesRemaining': remaining, 'unchangedFilesSkipped': stats.get('skipped_unchanged', 0), 'maintenanceWorkers': stats.get('maintenance_workers', 1), 'cacheHits': stats.get('cache_hits', 0), 'tasksSubmitted': stats.get('tasks_submitted', 0), 'tasksCompleted': stats.get('tasks_completed', 0), 'workerFailures': stats.get('worker_failures', 0), 'validationsPerformed': stats.get('files_checked', 0), 'formatRepairs': stats.get('formatted_files', 0), 'cueRepairsQueued': context.get('repairs_queued', 0), 'cueRepairsCompleted': context.get('repairs_completed', 0), 'quarantines': stats.get('quarantined_files', 0) + stats.get('undersized_quarantined', 0) + stats.get('prune_quarantined', 0), 'failures': _runtime._maintenance_metrics(stats)['failures'], 'progress': round(checked * 100 / max(1, discovered), 1), 'estimatedSeconds': round(elapsed + eta, 1) if eta is not None else None, 'etaSeconds': eta}
    return details

def _publish_scan_progress(scan_job_id: str | None, *, force: bool=False) -> None:
    if not scan_job_id:
        return
    details = None
    with _runtime._maintenance_scan_contexts_lock:
        context = _runtime._maintenance_scan_contexts.get(scan_job_id)
        if context is None:
            return
        now = _runtime.time.monotonic()
        should_publish = bool(force or context['files_checked'] == 1 or now - context.get('last_publish', 0) >= 0.5 or (context['files_checked'] >= context['files_discovered']))
        if should_publish:
            context['last_publish'] = now
            details = _runtime._scan_progress_details(context)
    if details is not None:
        _runtime._status_update_maintenance(scan_job_id, 'scanning', details=details)

def _scan_child_queued(scan_job_id: str | None) -> None:
    if not scan_job_id:
        return
    with _runtime._maintenance_scan_contexts_lock:
        context = _runtime._maintenance_scan_contexts.get(scan_job_id)
        if context is None:
            return
        context['pending'] += 1
        context['repairs_queued'] += 1
        details = _runtime._scan_progress_details(context)
    _runtime._status_update_maintenance(scan_job_id, 'scanning', details=details)

def _finalize_scan_context(scan_job_id: str, context: dict, *, failed: bool, details: dict) -> bool:
    """Persist a scan terminal state before releasing its in-memory ownership."""
    completed = False
    for _attempt in range(3):
        if _runtime._status_complete_maintenance(
            scan_job_id,
            'failed' if failed else 'accepted',
            reason='repair worker failed' if failed else None,
            details=details,
        ):
            completed = True
            break
    if not completed:
        print(f'{_runtime.YELLOW}[STATUS] Maintenance scan completion degraded after 3 attempts; child completion will retry{_runtime.RESET}')
        return False
    with _runtime._maintenance_scan_contexts_lock:
        if _runtime._maintenance_scan_contexts.get(scan_job_id) is context:
            _runtime._maintenance_scan_contexts.pop(scan_job_id, None)
    _runtime._status_record_maintenance(_runtime._maintenance_metrics(context['stats']))
    return True


def _schedule_scan_finalization_retry(scan_job_id: str, context: dict) -> None:
    """Retry transient scan-finalization failures without blocking maintenance."""
    with _runtime._maintenance_scan_contexts_lock:
        if (
            _runtime._maintenance_scan_contexts.get(scan_job_id) is not context
            or context.get('finalization_retry_scheduled')
        ):
            return
        context['finalization_retry_scheduled'] = True

    def retry() -> None:
        with _runtime._maintenance_scan_contexts_lock:
            if _runtime._maintenance_scan_contexts.get(scan_job_id) is not context:
                return
            context['finalization_retry_scheduled'] = False
        _retry_scan_finalization(scan_job_id, context)

    timer = _runtime.threading.Timer(1.0, retry)
    timer.daemon = True
    timer.start()


def _retry_scan_finalization(scan_job_id: str, context: dict) -> bool:
    """Reconcile one finalization-pending scan context."""
    with _runtime._maintenance_scan_contexts_lock:
        if _runtime._maintenance_scan_contexts.get(scan_job_id) is not context:
            return True
        if context.get('finalization_in_progress'):
            return False
        context['finalization_in_progress'] = True
        details = _runtime._scan_progress_details(context)
        failed = bool(
            context['stats'].get('async_repair_failures', 0)
            or context['stats'].get('cleanup_repair_failures', 0)
        )
    try:
        completed = _finalize_scan_context(
            scan_job_id, context, failed=failed, details=details,
        )
    finally:
        with _runtime._maintenance_scan_contexts_lock:
            if _runtime._maintenance_scan_contexts.get(scan_job_id) is context:
                context['finalization_in_progress'] = False
    if not completed:
        _schedule_scan_finalization_retry(scan_job_id, context)
    return completed


def _scan_child_finished(scan_job_id: str | None, outcome: str) -> bool:
    if not scan_job_id:
        return True
    finalize = False
    with _runtime._maintenance_scan_contexts_lock:
        context = _runtime._maintenance_scan_contexts.get(scan_job_id)
        if context is None:
            return True
        if not context.get('finalization_pending'):
            context['pending'] = max(0, context['pending'] - 1)
            context['repairs_completed'] += 1
            if outcome == 'repaired':
                context['stats']['async_repairs_completed'] = context['stats'].get('async_repairs_completed', 0) + 1
            elif outcome not in ('completed', 'quarantined', 'deleted'):
                context['stats']['async_repair_failures'] = context['stats'].get('async_repair_failures', 0) + 1
        finalize = context.get('enumeration_done', False) and context['pending'] == 0
        if finalize:
            context['finalization_pending'] = True
    if finalize:
        return _retry_scan_finalization(scan_job_id, context)
    else:
        details = _runtime._scan_progress_details(context)
        _runtime._status_update_maintenance(scan_job_id, 'waiting_repair_completion', details=details)
        return True

def _scan_enumeration_finished(scan_job_id: str | None, stats: dict) -> None:
    if not scan_job_id:
        _runtime._status_record_maintenance(_runtime._maintenance_metrics(stats))
        return
    with _runtime._maintenance_scan_contexts_lock:
        context = _runtime._maintenance_scan_contexts.get(scan_job_id)
        if context is None:
            return
        context['stats'].update(stats)
        context['enumeration_done'] = True
        finalize = context['pending'] == 0
        if finalize:
            context['finalization_pending'] = True
    if finalize:
        _retry_scan_finalization(scan_job_id, context)
    else:
        details = _runtime._scan_progress_details(context)
        _runtime._status_update_maintenance(scan_job_id, 'waiting_repair_completion', details=details)

def _status_compact_history() -> int:
    if _runtime._status_tracker is None:
        return 0
    try:
        return _runtime._status_tracker.compact_history()
    except OSError as exc:
        print(f'{_runtime.YELLOW}[STATUS] Could not compact status history: {exc}{_runtime.RESET}')
        return 0

def _status_finish_validation(item_type: str, item_id: int, target_lang: str, action: str) -> None:
    if action in ('valid', 'valid-warning', 'formatted', 'repaired'):
        _runtime._status_transition(item_type, item_id, target_lang, 'accepted', repaired=action in ('formatted', 'repaired'))
    elif action in ('repair-queued', 'repair-duplicate'):
        return
    elif action == 'repair-deferred':
        _runtime._status_transition(item_type, item_id, target_lang, 'deferred', reason='repair deferred')
    elif action in ('quarantined', 'deleted'):
        _runtime._status_transition(item_type, item_id, target_lang, 'quarantined', reason=action)
    else:
        _runtime._status_transition(item_type, item_id, target_lang, 'failed', reason=f'validation {action}')

def _get_cleanup_detector():
    if not _runtime.CLEANUP_LANGUAGES and not _runtime.LANGUAGES:
        return None
    with _runtime._cleanup_detector_lock:
        if _runtime._cleanup_detector is None:
            print('[INFO] Loading language detector for per-file cleanup...')
            from ..subtitles.foundation import build_detector
            _runtime._cleanup_detector = build_detector()
        return _runtime._cleanup_detector

def _get_validation_state():
    with _runtime._validation_state_lock:
        if _runtime._validation_state is None:
            from ..subtitles.foundation import VALIDATOR_VERSION
            _runtime._validation_state = _runtime.StateStore(_runtime.STATE_DB_FILE, validator_version=VALIDATOR_VERSION, config_fingerprint=_runtime._VALIDATION_CONFIG_FINGERPRINT)
        return _runtime._validation_state

def _initialize_state_store() -> _runtime.StateStore:
    with _runtime._validation_state_lock:
        if _runtime._validation_state is not None:
            return _runtime._validation_state
        from ..subtitles.foundation import VALIDATOR_VERSION
        store = _runtime.StateStore(_runtime.STATE_DB_FILE, acquire_process_lock=True, validator_version=VALIDATOR_VERSION, config_fingerprint=_runtime._VALIDATION_CONFIG_FINGERPRINT)
        reconciliation = store.reconcile_pending_operations()
        circuit_migration = store.initialize_cycle_circuits(_runtime.CIRCUIT_OPEN_CYCLES)
        print(f"[STATE] Circuit migration: completed_cycle={circuit_migration['completedCycle']}, migrated={circuit_migration['migrated']}, retired_generic={circuit_migration['retiredGeneric']}")
        _runtime._validation_state = store
    print(f'[STATE] SQLite state ready at {_runtime.STATE_DB_FILE}')
    if reconciliation['completed'] or reconciliation['abandoned']:
        print(f"[STATE] Reconciled {reconciliation['completed']} pending operation(s); abandoned {reconciliation['abandoned']}")
    return store
EXPORTS = {
    name: globals()[name] for name in (
        '_register_runtime_resources', 'close_runtime_resources',
        'TranslationCapacityGate', 'SharedCapacityCoordinator', 'FileLaneGate',
        'dbg', '_status_transition', '_status_identity',
        '_status_create_repair_ref', '_status_ref_transition',
        '_status_ref_complete', '_complete_repair_status',
        '_status_set_episode_identity', '_status_admit_retry',
        '_refresh_status_diagnostics', '_status_set_phase',
        '_status_start_cycle', '_status_finish_cycle',
        '_status_record_maintenance', '_status_create_maintenance',
        '_status_update_maintenance', '_status_complete_maintenance',
        '_status_record_maintenance_outcome', '_maintenance_file_identity',
        '_maintenance_metrics', '_scan_progress_details',
        '_publish_scan_progress', '_scan_child_queued', '_scan_child_finished',
        '_schedule_scan_finalization_retry', '_retry_scan_finalization',
        '_scan_enumeration_finished', '_status_compact_history',
        '_status_finish_validation', '_get_cleanup_detector',
        '_get_validation_state', '_initialize_state_store',
    )
}
