from __future__ import annotations
from .composition import runtime as _runtime

def _reconcile_retry_claims(state_store: _runtime.StateStore) -> int:
    reconciled = 0
    for plan in state_store.retry_claims_with_submissions():
        try:
            if plan['state'] == 'retry_in_progress':
                state_store.update_retry_plan(plan['id'], state='regeneration_waiting', eligible_completed_cycle=_runtime._completed_cycle, reason='retry output awaiting validation after restart')
                reconciled += 1
                continue
            job_id = plan.get('lingarrJobId')
            job = _runtime.lingarr_get_job(job_id) if job_id is not None else None
            status = str((job or {}).get('status') or '')
            if status and status not in ('Completed', 'Failed', 'Cancelled', 'Interrupted'):
                continue
            if status == 'Completed':
                state_store.update_retry_plan(plan['id'], state='regeneration_waiting', eligible_completed_cycle=_runtime._completed_cycle, reason='completed retry output awaiting validation')
            elif status in ('Failed', 'Cancelled', 'Interrupted'):
                attempts = int(plan.get('attemptCount') or 0)
                state_store.update_retry_plan(plan['id'], state='regeneration_waiting', completed_cycle=_runtime._completed_cycle, eligible_completed_cycle=_runtime._completed_cycle + _runtime._regeneration_delay_cycles(attempts), increment_attempt=True, reason=f'recovered terminal retry: {status}')
            else:
                state_store.reschedule_retry_no_progress(plan['id'], completed_cycle=_runtime._completed_cycle, deferral_class='submission_unresolved', reason='durable retry submission could not be resolved')
            reconciled += 1
        except Exception as exc:
            print(f"{_runtime.YELLOW}[RETRY] Could not reconcile claim {plan.get('id')}: {exc}{_runtime.RESET}")
    return reconciled

def _reconcile_circuit_trial_leases(state_store: _runtime.StateStore) -> int:
    reconciled = 0
    for lease in state_store.circuit_trial_leases():
        try:
            plan_id = lease.get('trialPlanId')
            plan = state_store.retry_plan(plan_id) if plan_id is not None else None
            if plan_id is not None and plan is None:
                state_store.settle_circuit_trial_for_retry(
                    plan_id, lease_generation=lease['leaseGeneration'],
                    outcome='deferred', open_cycles=_runtime.CIRCUIT_OPEN_CYCLES,
                    reason='orphaned circuit trial retry plan',
                )
                reconciled += 1
                continue
            plan_state = str((plan or {}).get('state') or '')
            if plan_state.startswith('accepted_after_'):
                state_store.settle_circuit_trial_for_retry(
                    plan_id, lease_generation=lease['leaseGeneration'],
                    outcome='success', open_cycles=_runtime.CIRCUIT_OPEN_CYCLES,
                )
                reconciled += 1
                continue
            if plan_state in ('retry_exhausted', 'source_blocked', 'manual_dismissed'):
                state_store.settle_circuit_trial_for_retry(
                    plan_id, lease_generation=lease['leaseGeneration'],
                    outcome='failure', open_cycles=_runtime.CIRCUIT_OPEN_CYCLES,
                    reason=f'terminal retry plan: {plan_state}',
                )
                reconciled += 1
                continue
            if plan_state == 'superseded':
                state_store.settle_circuit_trial_for_retry(
                    plan_id, lease_generation=lease['leaseGeneration'],
                    outcome='deferred', open_cycles=_runtime.CIRCUIT_OPEN_CYCLES,
                    reason='retry plan superseded',
                )
                reconciled += 1
                continue
            job = _runtime.lingarr_get_job(lease['trialJobId'])
            job_status = str((job or {}).get('status') or '')
            if job_status in ('Failed', 'Cancelled', 'Interrupted'):
                if plan_id is not None:
                    state_store.settle_circuit_trial_for_retry(
                        plan_id, lease_generation=lease['leaseGeneration'],
                        outcome='failure', open_cycles=_runtime.CIRCUIT_OPEN_CYCLES,
                        reason=f'recovered terminal trial: {job_status}',
                    )
                else:
                    state_store.record_circuit_outcome(series_key=lease['seriesKey'], series_title=lease['seriesTitle'], success=False, reason=f'recovered terminal trial: {job_status}', threshold=_runtime.CIRCUIT_FAILURE_THRESHOLD, open_cycles=_runtime.CIRCUIT_OPEN_CYCLES, config_fingerprint=_runtime._CIRCUIT_CONFIG_FINGERPRINT, trial_owner=lease.get('trialOwner'), trial_job_id=lease.get('trialJobId'), trial_plan_id=lease.get('trialPlanId'), lease_generation=lease.get('leaseGeneration'))
                reconciled += 1
            elif job_status == 'Completed':
                # A completed Lingarr job is not circuit success by itself.  Once
                # restart reconciliation has put the retry back into its waiting
                # state, however, retaining the old half-open lease prevents that
                # same plan from ever claiming the next validation attempt.
                if plan_id is not None and plan_state == 'regeneration_waiting' and (not state_store.has_durable_repair_for_retry(plan_id)):
                    state_store.settle_circuit_trial_for_retry(
                        plan_id, lease_generation=lease['leaseGeneration'],
                        outcome='deferred', open_cycles=_runtime.CIRCUIT_OPEN_CYCLES,
                        reason='completed retry output awaits a fresh validation trial',
                    )
                else:
                    state_store.mark_circuit_trial_validation_pending(lease['seriesKey'], lease['trialOwner'])
                reconciled += 1
            elif not job:
                claimed_at = float(lease.get('trialClaimedAt') or 0)
                if claimed_at and _runtime.time.time() - claimed_at >= _runtime.TRANSLATION_TIMEOUT_CAP + max(60, _runtime.POLL_INTERVAL):
                    if plan_id is not None:
                        state_store.settle_circuit_trial_for_retry(
                            plan_id, lease_generation=lease['leaseGeneration'],
                            outcome='deferred', open_cycles=_runtime.CIRCUIT_OPEN_CYCLES,
                            reason='expired bound trial job could not be resolved',
                        )
                    else:
                        state_store.release_circuit_trial(lease['seriesKey'], lease['trialOwner'], 'expired bound trial job could not be resolved')
                    reconciled += 1
        except Exception as exc:
            print(f"{_runtime.YELLOW}[CIRCUIT] Could not reconcile trial {lease.get('seriesKey')}: {exc}{_runtime.RESET}")
    return reconciled

def _requeue_persisted_repairs(state_store: _runtime.StateStore) -> int:
    """Revalidate and requeue durable repairs without relying on a library scan."""
    queued = 0
    if not hasattr(state_store, 'repair_jobs_for_restart'):
        return queued
    for job in state_store.repair_jobs_for_restart():
        if _runtime.shutdown_requested:
            break
        source_path = job.get('sourcePath')
        target_path = job.get('targetPath')
        payload = job.get('payload') or {}
        retry_plan_id = payload.get('retryPlanId')
        trial_generation = payload.get('trialGeneration')
        if not source_path or not target_path or _runtime._file_hash_or_none(source_path) != job.get('sourceHash') or (_runtime._file_hash_or_none(target_path) != job.get('targetHash')):
            state_store.transition_repair_job(job['id'], 'failed', error_code='repair_inputs_changed', expected_states=('persisted_for_restart',))
            if retry_plan_id is not None and trial_generation is not None:
                state_store.reschedule_retry_no_progress(retry_plan_id, completed_cycle=_runtime._completed_cycle, deferral_class='repair_inputs_changed', reason='durable repair inputs changed during restart', delay_cycles=1, lease_generation=trial_generation)
                state_store.settle_circuit_trial_for_retry(retry_plan_id, lease_generation=trial_generation, outcome='deferred', open_cycles=_runtime.CIRCUIT_OPEN_CYCLES, reason='durable repair inputs changed during restart')
            continue
        action, _report = _runtime._validate_translated_file(source_path, target_path, payload.get('sourceLanguage') or 'en', job['targetLanguage'], job.get('itemId'), title=payload.get('title') or _runtime.os.path.basename(target_path), defer_repair=True, item_type=job.get('itemType'), origin=payload.get('origin') or 'recovered_repair', provenance_source_hash=job.get('sourceHash'), series_key=payload.get('seriesKey'), series_title=payload.get('seriesTitle'), retry_plan_id=payload.get('retryPlanId'), trial_owner=payload.get('trialOwner'), trial_job_id=payload.get('trialJobId'), trial_plan_id=payload.get('trialPlanId'), trial_generation=payload.get('trialGeneration'))
        if action == 'repair-queued':
            queued += 1
        elif action in ('valid', 'valid-warning', 'formatted', 'quarantined', 'deleted'):
            state_store.transition_repair_job(job['id'], 'completed', expected_states=('persisted_for_restart',))
            if retry_plan_id is not None and trial_generation is not None:
                if action in ('valid', 'valid-warning', 'formatted'):
                    resolved = state_store.resolve_retry_plan(retry_plan_id, job.get('sourceHash'), outcome='accepted_after_retry', lease_generation=trial_generation)
                    state_store.settle_circuit_trial_for_retry(
                        retry_plan_id, lease_generation=trial_generation,
                        outcome='success' if resolved else 'deferred',
                        open_cycles=_runtime.CIRCUIT_OPEN_CYCLES,
                        reason=None if resolved else 'restarted repair acceptance was superseded by a source change',
                    )
                elif action in ('quarantined', 'deleted'):
                    state_store.settle_circuit_trial_for_retry(retry_plan_id, lease_generation=trial_generation, outcome='failure', open_cycles=_runtime.CIRCUIT_OPEN_CYCLES, reason=f'restarted repair finished with {action}')
        elif action == 'repair-deferred':
            if retry_plan_id is not None and trial_generation is not None:
                state_store.reschedule_retry_no_progress(retry_plan_id, completed_cycle=_runtime._completed_cycle, deferral_class='repair_deferred', reason='restarted repair deferred', delay_cycles=1, lease_generation=trial_generation)
                state_store.settle_circuit_trial_for_retry(retry_plan_id, lease_generation=trial_generation, outcome='deferred', open_cycles=_runtime.CIRCUIT_OPEN_CYCLES, reason='restarted repair deferred')
        else:
            state_store.transition_repair_job(job['id'], 'failed', error_code=str(action), expected_states=('persisted_for_restart',))
            if retry_plan_id is not None and trial_generation is not None:
                state_store.reschedule_retry_no_progress(retry_plan_id, completed_cycle=_runtime._completed_cycle, deferral_class='repair_deferred', reason=f'restarted repair finished with {action}', delay_cycles=1, lease_generation=trial_generation)
                state_store.settle_circuit_trial_for_retry(retry_plan_id, lease_generation=trial_generation, outcome='deferred', open_cycles=_runtime.CIRCUIT_OPEN_CYCLES, reason=f'restarted repair finished with {action}')
    return queued

def main(config=None) -> int:
    if config is not None and config != _runtime._config:
        mismatched = sorted(
            name for name, value in vars(config).items()
            if value != getattr(_runtime._config, name)
        )
        raise RuntimeError(
            "runtime configuration differs from the composed Application "
            f"(mismatched: {', '.join(mismatched)})"
        )
    state_store = _runtime._initialize_state_store()
    _runtime._register_runtime_resources(state_store=state_store)
    backfilled_source_readiness = state_store.backfill_source_readiness()
    _runtime._completed_cycle = state_store.completed_cycle()
    recovered_end_cycle_repairs = state_store.recover_stale_end_cycle_repair_attempts()
    recovered_repairs = state_store.recover_repair_jobs()
    reactivated_manual_reviews = state_store.reactivate_changed_manual_reviews(_runtime._VALIDATION_CONFIG_FINGERPRINT)
    recovered_claims = state_store.recover_retry_claims()
    reconciled_retry_claims = _runtime._reconcile_retry_claims(state_store)
    recovered_trials = state_store.recover_abandoned_circuit_trials(max_age_seconds=0)
    reconciled_trials = _runtime._reconcile_circuit_trial_leases(state_store)
    _runtime._manual_review_service = _runtime.build_manual_review_service(state_store)
    pending_manual_scans = _runtime._manual_review_service.dispatch_pending_scans(limit=10)
    backfilled_retry_sizes = 0
    for plan in state_store.retry_plans(include_terminal=False):
        if plan.get('sourceCueCount') is not None or not plan.get('sourcePath'):
            continue
        cue_count = _runtime._count_srt_cues(plan['sourcePath'])
        if cue_count and state_store.set_retry_source_cue_count(plan['id'], cue_count):
            backfilled_retry_sizes += 1
    print(f'[CYCLE] Restored completed-cycle sequence {_runtime._completed_cycle}; released {recovered_claims} orphaned retry claim(s); re-enabled {recovered_end_cycle_repairs} stale end-cycle repair(s); persisted {recovered_repairs} repair job(s) for restart; reactivated {reactivated_manual_reviews} changed manual review(s); reconciled {reconciled_retry_claims} submitted retry claim(s); released {recovered_trials} unbound circuit trial(s); reconciled {reconciled_trials} bound circuit trial(s); backfilled {backfilled_retry_sizes} retry size(s); trusted {backfilled_source_readiness} proven source hash(es); dispatched {pending_manual_scans["dispatched"]}/{pending_manual_scans["examined"]} pending manual scan(s)')
    status_server = None
    if _runtime.STATUS_ENABLED:
        try:
            _runtime._status_tracker = _runtime.StatusTracker(_runtime.STATUS_SNAPSHOT_FILE, _runtime.STATUS_HISTORY_FILE, retention_days=_runtime.STATUS_HISTORY_RETENTION_DAYS, recent_limit=_runtime.STATUS_RECENT_LIMIT)
            _runtime._status_facade = _runtime.StatusFacade(_runtime._status_tracker)
            _runtime._refresh_status_diagnostics()
            try:
                status_server, _ = _runtime.start_status_server(
                    _runtime._status_tracker, _runtime.STATUS_BIND,
                    _runtime.STATUS_PORT, _runtime.LOG_DIR,
                    manual_review_service=_runtime._manual_review_service,
                    display_timezone=_runtime.DISPLAY_TIMEZONE,
                )
                _runtime._register_runtime_resources(status_server=status_server)
                print(f'[STATUS] Dashboard listening on http://{_runtime.STATUS_BIND}:{_runtime.STATUS_PORT}')
            except OSError as exc:
                print(f'{_runtime.YELLOW}[STATUS] Dashboard port unavailable ({_runtime.STATUS_BIND}:{_runtime.STATUS_PORT}): {exc}; translations will continue{_runtime.RESET}')
        except OSError as exc:
            _runtime._status_tracker = None
            _runtime._status_facade = None
            print(f'{_runtime.YELLOW}[STATUS] Could not initialize persistent status state: {exc}; translations will continue{_runtime.RESET}')
    print(f'\n{_runtime.BOLD}Bazarr AutoTranslate starting{_runtime.RESET}')
    print(f'  Bazarr URL        : {_runtime.BAZARR_URL}')
    print(f'  Lingarr URL       : {_runtime.LINGARR_URL}')
    print(f"  Languages         : {', '.join(_runtime.LANGUAGES)}")
    print(f"  Cleanup languages : {', '.join(sorted(_runtime.CLEANUP_LANGUAGES)) or '(none)'}")
    print(f"  Existing scan     : {('ON' if _runtime.CLEANUP_SCAN_EXISTING else 'off')} every {_runtime.CLEANUP_SCAN_INTERVAL}s")
    print(f"  Cleanup roots     : {', '.join((str(root) for root in _runtime.CLEANUP_ROOTS))}")
    print(f"  Cleanup action    : {_runtime.CLEANUP_ACTION}{(' (scan dry-run)' if _runtime.CLEANUP_SCAN_DRY_RUN else '')}")
    print(f'  Source-less lines : {_runtime.CLEANUP_SOURCELESS_LINE_ONLY_ACTION}')
    print(f'  Quarantine retry  : {_runtime.REGENERATION_MAX_ATTEMPTS} attempts after {_runtime.REGENERATION_INITIAL_DELAY_CYCLES} completed cycle(s), batch {_runtime.RETRY_BATCH_SIZE_PER_CYCLE}')
    print(f"  Sidecar pruning   : {('ON' if _runtime.CLEANUP_PRUNE_EXTRA_LANGUAGES else 'off')} ({_runtime.CLEANUP_PRUNE_ACTION}, unknown={('remove' if _runtime.CLEANUP_PRUNE_UNKNOWN_SIDECARS else 'retain')})")
    print(f'  Max cue lines     : {_runtime.CLEANUP_MAX_CUE_LINES}')
    print(f"  Format recovery   : {('ON' if _runtime.CLEANUP_FORMAT_REPAIR_ENABLED else 'off')}")
    print(f'  Shared capacity   : {_runtime.PARALLEL_TRANSLATES} (translations + repairs; repairs first)')
    print(f'  Repair queue max  : {_runtime.CLEANUP_REPAIR_QUEUE_MAX}')
    print(f"  Size validation   : {('ON' if _runtime.CLEANUP_UNDERSIZED_ENABLED else 'off')} ({_runtime.CLEANUP_UNDERSIZED_REQUIRED_SIGNALS}/4 signals, media >= {_runtime.CLEANUP_MIN_MEDIA_DURATION:.0f}s)")
    print(f'  Size thresholds   : {_runtime.CLEANUP_MIN_CUES_PER_MINUTE:g} cues/min, {_runtime.CLEANUP_MIN_TEXT_CHARS_PER_MINUTE:g} chars/min, {_runtime.CLEANUP_MIN_BYTES_PER_MINUTE:g} bytes/min, {_runtime.CLEANUP_MIN_TIMELINE_COVERAGE:.0%} timeline')
    print(f'  Retention         : state/logs {_runtime.RETENTION_DAYS} days; quarantine {_runtime.QUARANTINE_ARTIFACT_RETENTION_DAYS} days (checked every {_runtime.RETENTION_CHECK_INTERVAL}s)')
    print(f"  Status dashboard  : {('ON' if _runtime.STATUS_ENABLED else 'off')}" + (f' on {_runtime.STATUS_BIND}:{_runtime.STATUS_PORT}' if _runtime.STATUS_ENABLED else ''))
    print(f'  Status retention  : {_runtime.STATUS_HISTORY_RETENTION_DAYS} days')
    print(f"  Manual actions    : {('enabled' if _runtime.STATUS_MANUAL_ACTIONS_ENABLED else 'read-only')}")
    print(f'  Adaptive timeout  : x{_runtime.TRANSLATION_TIMEOUT_MULTIPLIER:g}, cap {_runtime.TRANSLATION_TIMEOUT_CAP}s, cold {_runtime.TRANSLATION_COLD_SECONDS_PER_CUE:g}s/cue')
    print(f'  Long-job threshold: {_runtime.LONG_JOB_THRESHOLD}s ({_runtime._file_lane_gate.short_capacity} short / {_runtime._file_lane_gate.long_capacity} long file lanes)')
    print(f'  Circuit breaker   : {_runtime.CIRCUIT_FAILURE_THRESHOLD} failures / {_runtime.CIRCUIT_OPEN_CYCLES} healthy completed cycles')
    print(f'  Check interval    : {_runtime.CHECK_INTERVAL}s (after Bazarr sync)')
    print(f'  Poll interval     : {_runtime.POLL_INTERVAL}s  (floor {_runtime.POLL_TIMEOUT}s per translation)')
    print(f'  Sync timeout      : {_runtime.SYNC_TIMEOUT}s')
    print(f'  Sync start timeout: {_runtime.SYNC_START_TIMEOUT}s')
    print(f'  Resubmit cooldown : {_runtime.RESUBMIT_COOLDOWN}s')
    print(f"  Debug mode        : {('ON' if _runtime.DEBUG else 'off')}")
    _runtime.sys.stdout.flush()
    startup_job_id = _runtime._status_create_maintenance(
        'startup', {'title': 'Service startup'}, state='startup_wait'
    )

    def startup_phase(phase: str) -> None:
        _runtime._status_set_phase(phase)
        _runtime._status_update_maintenance(startup_job_id, phase)

    def stop_during_startup() -> int:
        _runtime._status_complete_maintenance(
            startup_job_id, 'deferred',
            reason='shutdown requested during startup',
        )
        print('[INFO] Bazarr AutoTranslate stopped during startup.')
        return 0

    try:
        startup_phase('startup_wait')
        langs = _runtime.lingarr_get_languages()
        if langs:
            mappings = []
            for language in langs:
                targets = ', '.join(language.targets) if language.targets else 'none'
                mappings.append(f'{language.name} ({language.code} -> {targets})')
            print(f"[INFO] Lingarr supports languages: {'; '.join(mappings)}")
        print('[INFO] Waiting 30s for services to start...')
        _runtime.sys.stdout.flush()
        for _ in range(30):
            if _runtime.shutdown_requested:
                break
            _runtime.time.sleep(1)
        if _runtime.shutdown_requested:
            return stop_during_startup()

        print('[INFO] Running initial Bazarr subtitle synchronization...')
        startup_phase('startup_sync')
        _runtime._tracked_bazarr_sync(True, True, _runtime.SYNC_TIMEOUT)
        if _runtime.shutdown_requested:
            return stop_during_startup()

        startup_repairs = _runtime._requeue_persisted_repairs(state_store)
        if startup_repairs:
            print(f'[REPAIR] Requeued {startup_repairs} durable startup repair(s)')
            startup_phase('repair_drain')
            _runtime._drain_pending_repairs({})
        if _runtime.shutdown_requested:
            return stop_during_startup()

        startup_phase('startup_cleanup')
        _runtime._run_retention_housekeeping_tracked()
        last_retention_check = _runtime.time.monotonic()
        cycle = _runtime._completed_cycle + 1
        _runtime._cycle_suppressions.begin_cycle(str(cycle))
        last_cleanup_scan = 0.0
        if _runtime.CLEANUP_SCAN_EXISTING:
            startup_cleanup = _runtime._run_existing_cleanup_scan_safely()
            if startup_cleanup is not None:
                last_cleanup_scan = _runtime.time.monotonic()
        if _runtime.shutdown_requested:
            return stop_during_startup()
    except Exception:
        _runtime._status_complete_maintenance(
            startup_job_id, 'failed', reason='startup failed'
        )
        raise
    _runtime._status_complete_maintenance(startup_job_id, 'accepted')

    def run_cycle_owned(cycle_number: int) -> bool:
        _runtime._cycle_suppressions.begin_cycle(str(cycle_number))
        return _runtime.run_cycle(cycle_number)
    cycle_runner = _runtime.CycleRunner(run_cycle_owned)

    def advance_completed_cycle() -> int:
        _runtime._completed_cycle = state_store.advance_completed_cycle()
        print(f'[CYCLE] Persisted completed cycle {_runtime._completed_cycle}')
        return _runtime._completed_cycle

    def tracked_maintenance(name: str, reason: str, operation):
        run_id = state_store.start_maintenance_run(name, due_reason=reason, completed_cycle=_runtime._completed_cycle)
        try:
            metrics = operation()
            if metrics is None:
                raise RuntimeError(f'{name} failed')
        except Exception as exc:
            state_store.finish_maintenance_run(run_id, success=False, failure_code=type(exc).__name__)
            raise
        state_store.finish_maintenance_run(run_id, success=True, metrics=metrics)
        return metrics

    def mark_retention_completed() -> None:
        nonlocal last_retention_check
        last_retention_check = _runtime.time.monotonic()

    def mark_scan_completed() -> None:
        nonlocal last_cleanup_scan
        last_cleanup_scan = _runtime.time.monotonic()
    existing_library = _runtime.ExistingLibraryMaintenance(_runtime._run_existing_cleanup_scan_safely)
    maintenance = _runtime.MaintenanceCoordinator((_runtime.MaintenanceOperation('retention', due=lambda: _runtime.time.monotonic() - last_retention_check >= _runtime.RETENTION_CHECK_INTERVAL, run=lambda: tracked_maintenance('retention', 'retention interval elapsed', lambda: _runtime.run_retention(_runtime._run_retention_housekeeping_tracked)), mark_completed=mark_retention_completed), _runtime.MaintenanceOperation('existing_library_scan', due=lambda: bool(_runtime.CLEANUP_SCAN_EXISTING and last_cleanup_scan > 0 and (_runtime.time.monotonic() - last_cleanup_scan >= _runtime.CLEANUP_SCAN_INTERVAL)), run=lambda: tracked_maintenance('existing_library_scan', 'cleanup scan interval elapsed', existing_library.run), mark_completed=mark_scan_completed)), stop_requested=lambda: _runtime.shutdown_requested or _runtime._shutdown_controller.is_requested())

    def lifecycle_phase(phase: str, **values) -> None:
        if phase == 'cooldown':
            values.setdefault('next_cycle_at', _runtime.time.time() + _runtime.CHECK_INTERVAL)
        _runtime._status_set_phase(phase, **values)

    def sleep_interruptibly(seconds: int) -> bool:
        print(f'[INFO] Next cycle in {seconds}s...')
        for _ in range(max(0, int(seconds))):
            if _runtime.shutdown_requested:
                return True
            _runtime.time.sleep(1)
        return _runtime.shutdown_requested
    controller = _runtime.LifecycleController(run_cycle=lambda number: cycle_runner.run(number).healthy, advance_completed_cycle=advance_completed_cycle, run_maintenance=maintenance.run_due, set_phase=lifecycle_phase, refresh_diagnostics=_runtime._refresh_status_diagnostics, sleep_interruptibly=sleep_interruptibly, check_interval=_runtime.CHECK_INTERVAL, shutdown_requested=lambda: _runtime.shutdown_requested)

    def report_iteration(number, healthy, maintenance_result) -> None:
        if not healthy:
            print(f'{_runtime.YELLOW}[CYCLE] Cycle #{number} was degraded or interrupted; completed-cycle counter was not advanced{_runtime.RESET}')
        if not maintenance_result.healthy:
            print(f"{_runtime.YELLOW}[MAINTENANCE] Post-cycle maintenance failed ({', '.join(maintenance_result.failed)}); it remains due{_runtime.RESET}")
    controller.run(cycle, on_iteration=report_iteration)
    print('[INFO] Bazarr AutoTranslate stopped cleanly.')
    return 0
EXPORTS = {
    name: globals()[name] for name in (
        '_reconcile_retry_claims', '_reconcile_circuit_trial_leases',
        '_requeue_persisted_repairs', 'main',
    )
}
