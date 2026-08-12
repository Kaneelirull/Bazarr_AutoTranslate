from __future__ import annotations
from ..composition import runtime as _runtime

def _drain_lingarr_queue() -> bool:
    drain_deadline = _runtime.time.time() + 2 * _runtime.CHECK_INTERVAL
    while not _runtime.shutdown_requested:
        try:
            active = len(_runtime.lingarr_get_active_translations())
        except _runtime.ServiceRequestError as exc:
            print(f'{_runtime.YELLOW}[WARNING] Lingarr queue state is unverifiable; cycle remains degraded: {exc}{_runtime.RESET}')
            return False
        if active == 0:
            return True
        if _runtime.time.time() >= drain_deadline:
            print(f'{_runtime.YELLOW}[WARNING] Lingarr still has {active} active job(s) after {2 * _runtime.CHECK_INTERVAL}s — continuing anyway{_runtime.RESET}')
            return False
        print(f'[INFO] Lingarr has {active} active job(s) — waiting before next cycle...')
        for _ in range(_runtime.POLL_INTERVAL):
            if _runtime.shutdown_requested:
                return False
            _runtime.time.sleep(1)
    return False

def _run_end_cycle_repair_retries(stats: dict) -> None:
    if not _runtime.END_OF_CYCLE_REPAIR_RETRY_ENABLED or _runtime.shutdown_requested:
        return
    try:
        plans = [plan for plan in _runtime._get_validation_state().retry_plans(include_terminal=False) if plan['state'] == 'repair_retry_queued' and (not plan['endCycleRepairAttempted']) and (plan['eligibleCompletedCycle'] <= _runtime._completed_cycle)]
    except _runtime.StateStoreError as exc:
        print(f'{_runtime.YELLOW}[RETRY] Could not load repair retries: {exc}{_runtime.RESET}')
        stats['degraded'] = True
        return
    for plan in plans:
        if _runtime.shutdown_requested:
            break
        source_path = plan.get('sourcePath')
        target_path = plan.get('targetPath')
        trial = _runtime._get_validation_state().circuit_trial_for_retry_plan(plan['id'])
        retry_resolved = False
        try:
            _runtime._get_validation_state().update_retry_plan(plan['id'], state='retry_in_progress', completed_cycle=_runtime._completed_cycle, end_cycle_repair_attempted=True, reason='end-of-cycle repair retry')
            if not source_path or not target_path or (not _runtime.os.path.exists(target_path)):
                raise OSError('repair source or target is no longer available')
            action, report = _runtime._validate_translated_file(source_path, target_path, plan.get('sourceLanguage') or '', plan['targetLanguage'], plan['itemId'], title=plan.get('mediaTitle') or '', defer_repair=False, item_type=plan['itemType'], origin='lingarr', provenance_source_hash=plan['sourceHash'], series_key=plan.get('seriesKey'), series_title=plan.get('seriesTitle'), retry_plan_id=plan['id'])
            if action in ('valid', 'valid-warning', 'formatted', 'repaired'):
                retry_resolved = _runtime._resolve_retry_success(
                    plan['id'], plan['sourceHash'],
                    lease_generation=trial['leaseGeneration'] if trial is not None else None,
                )
                if retry_resolved and trial is not None:
                    _runtime._get_validation_state().settle_circuit_trial_for_retry(
                        plan['id'], lease_generation=trial['leaseGeneration'],
                        outcome='success', open_cycles=_runtime.CIRCUIT_OPEN_CYCLES,
                    )
                elif trial is not None:
                    _runtime._get_validation_state().settle_circuit_trial_for_retry(
                        plan['id'], lease_generation=trial['leaseGeneration'],
                        outcome='deferred', open_cycles=_runtime.CIRCUIT_OPEN_CYCLES,
                        reason='retry acceptance was superseded by a source change',
                    )
                if retry_resolved:
                    stats['retry_repairs_accepted'] = stats.get('retry_repairs_accepted', 0) + 1
            elif action == 'repair-deferred':
                manual_review = bool(getattr(report, 'manual_review', False))
                _runtime._get_validation_state().reschedule_retry_no_progress(plan['id'], completed_cycle=_runtime._completed_cycle, deferral_class='manual_review' if manual_review else 'repair_deferred', reason='end-of-cycle repair remained deferred', delay_cycles=1, lease_generation=trial['leaseGeneration'] if trial is not None else None)
                if trial is not None:
                    _runtime._get_validation_state().settle_circuit_trial_for_retry(
                        plan['id'], lease_generation=trial['leaseGeneration'],
                        outcome='deferred', open_cycles=_runtime.CIRCUIT_OPEN_CYCLES,
                        reason='end-of-cycle repair remained deferred',
                    )
            else:
                manual_review = bool(getattr(report, 'manual_review', False))
                _runtime._get_validation_state().reschedule_retry_no_progress(
                    plan['id'], completed_cycle=_runtime._completed_cycle,
                    deferral_class='manual_review' if manual_review else 'validation_failed',
                    reason=f'end-of-cycle repair finished with {action}', delay_cycles=1,
                    lease_generation=trial['leaseGeneration'] if trial is not None else None,
                )
                if trial is not None:
                    _runtime._get_validation_state().settle_circuit_trial_for_retry(
                        plan['id'], lease_generation=trial['leaseGeneration'],
                        outcome='failure' if action in ('quarantined', 'deleted') else 'deferred',
                        open_cycles=_runtime.CIRCUIT_OPEN_CYCLES,
                        reason=f'end-of-cycle repair finished with {action}',
                    )
        except (OSError, _runtime.StateStoreError) as exc:
            print(f'{_runtime.YELLOW}[RETRY] Repair retry deferred: {exc}{_runtime.RESET}')
            if retry_resolved:
                stats['degraded'] = True
                continue
            try:
                _runtime._get_validation_state().reschedule_retry_no_progress(plan['id'], completed_cycle=_runtime._completed_cycle, deferral_class='repair_deferred', reason=str(exc), delay_cycles=1, lease_generation=trial['leaseGeneration'] if trial is not None else None)
                if trial is not None:
                    _runtime._get_validation_state().settle_circuit_trial_for_retry(
                        plan['id'], lease_generation=trial['leaseGeneration'],
                        outcome='deferred', open_cycles=_runtime.CIRCUIT_OPEN_CYCLES,
                        reason=str(exc),
                    )
            except _runtime.StateStoreError:
                stats['degraded'] = True
        finally:
            _runtime._refresh_status_diagnostics()

def _run_regeneration_retry_batch(stats: dict, submission_budget: int, examined_plan_ids: set[int] | None=None, series_admissions: dict[str, int] | None=None) -> tuple[int, int]:
    if _runtime.shutdown_requested:
        return (0, 0)
    budget = max(1, int(submission_budget))
    examined_plan_ids = examined_plan_ids if examined_plan_ids is not None else set()
    series_admissions = series_admissions if series_admissions is not None else {}
    try:
        for pending in _runtime._get_validation_state().retry_plans(include_terminal=False):
            if pending.get('itemType') != 'episodes':
                continue
            identity_item = {'seriesTitle': pending.get('seriesTitle')}
            old_key = str(pending.get('seriesKey') or '')
            if old_key.startswith('sonarr:'):
                try:
                    identity_item['sonarrSeriesId'] = int(old_key.split(':', 1)[1])
                except ValueError:
                    pass
            canonical = _runtime.resolve_media_identity(identity_item, 'episodes', pending['itemId'], pending.get('sourcePath'))
            if old_key and canonical['key'] != old_key:
                _runtime._get_validation_state().register_series_alias(old_key, canonical['key'], canonical['title'])
        due_before = _runtime._get_validation_state().due_retry_count(_runtime._completed_cycle)
        plans = _runtime._get_validation_state().claim_due_retry_plans(_runtime._completed_cycle, limit=budget, per_series_limit=_runtime.RETRY_MAX_PER_SERIES_PER_CYCLE, excluded_plan_ids=examined_plan_ids, series_admissions=series_admissions)
    except _runtime.StateStoreError as exc:
        print(f'{_runtime.YELLOW}[RETRY] Could not claim regeneration retries: {exc}{_runtime.RESET}')
        stats['degraded'] = True
        return (0, 0)
    plans = [plan for plan in plans if int(plan['id']) not in examined_plan_ids]
    if not plans:
        return (0, 0)
    examined_plan_ids.update((int(plan['id']) for plan in plans))
    stats['regeneration_queued'] = stats.get('regeneration_queued', 0) + len(plans)
    print(f'[RETRY] Due={due_before} admitted={len(plans)} remaining={max(0, due_before - len(plans))} completed_cycle={_runtime._completed_cycle}')
    retry_lock = _runtime.threading.Lock()
    submitted_plan_ids: set[int] = set()

    def note_retry_submission(plan: dict) -> None:
        with retry_lock:
            submitted_plan_ids.add(int(plan['id']))
    admitted: list[tuple[dict, dict, str, str]] = []
    for plan in plans:
        try:
            _runtime._get_validation_state().record_retry_admission(plan['id'], _runtime._completed_cycle, 'examined')
        except _runtime.StateStoreError:
            pass
        if _runtime.shutdown_requested:
            try:
                _runtime._get_validation_state().update_retry_plan(plan['id'], state='regeneration_waiting', reason='retry admission cancelled during shutdown')
            except _runtime.StateStoreError:
                stats['degraded'] = True
            continue
        item_type = plan['itemType']
        id_field = 'sonarrEpisodeId' if item_type == 'episodes' else 'radarrId'
        identity = _runtime.retry_media_identity(plan)
        item = {id_field: plan['itemId'], 'title': identity.get('episodeTitle') or identity['displayTitle'], 'seriesTitle': identity['displayTitle'], 'missing_subtitles': [{'code2': plan['targetLanguage']}]}
        if plan.get('seriesKey', '').startswith('sonarr:'):
            try:
                item['sonarrSeriesId'] = int(plan['seriesKey'].split(':', 1)[1])
            except (TypeError, ValueError):
                pass
        print(f"[RETRY] Admitting regeneration plan {plan['id']} for {item_type}:{plan['itemId']} '{plan['targetLanguage']}' attempt={plan['attemptCount'] + 1}/{_runtime.REGENERATION_MAX_ATTEMPTS or 'unlimited'}")
        _runtime._status_admit_retry(plan, identity)
        admitted.append((plan, item, item_type, id_field))

    def run_retry(plan: dict, item: dict, item_type: str, id_field: str) -> None:
        try:
            _runtime.process_item(item, item_type, id_field, stats, retry_lock, retry_plan=plan, retry_submission_callback=note_retry_submission)
        finally:
            _runtime._shared_capacity.release_current_translation()
    dispatch_workers = max(_runtime.PARALLEL_TRANSLATES * 4, _runtime.PARALLEL_TRANSLATES + 1)
    executor = _runtime._DaemonExecutor(max_workers=min(len(admitted), dispatch_workers) or 1, thread_name_prefix='retry-worker')
    try:
        futures = {executor.submit(run_retry, plan, item, item_type, id_field): plan for plan, item, item_type, id_field in admitted}
        for future in _runtime.completed_futures(futures, stop_requested=lambda: _runtime.shutdown_requested or _runtime._shutdown_controller.is_requested()):
            plan = futures[future]
            worker_error: Exception | None = None
            try:
                future.result()
            except Exception as exc:
                worker_error = exc
                print(f"{_runtime.RED}[ERROR] Retry worker failed for plan {plan['id']}: {exc}{_runtime.RESET}")
                with retry_lock:
                    stats['degraded'] = True
                _runtime._status_transition(plan['itemType'], plan['itemId'], plan['targetLanguage'], 'deferred', reason='retry worker failed')
            try:
                current = next((entry for entry in _runtime._get_validation_state().retry_plans() if entry['id'] == plan['id']), None)
                if current and current['state'] == 'regeneration_queued':
                    deferral_class = 'shutdown' if _runtime.shutdown_requested else 'worker_exception' if worker_error is not None else 'admission_no_progress'
                    _runtime._get_validation_state().reschedule_retry_no_progress(plan['id'], completed_cycle=_runtime._completed_cycle, deferral_class=deferral_class, reason='retry worker failed before Lingarr output' if worker_error is not None else 'retry admission stopped during shutdown' if _runtime.shutdown_requested else 'retry admission deferred before Lingarr output')
                    _runtime._get_validation_state().record_retry_admission(plan['id'], _runtime._completed_cycle, 'no_progress', deferral_class)
                elif current and current.get('submissionAttemptId') is None:
                    _runtime._get_validation_state().record_retry_admission(plan['id'], _runtime._completed_cycle, 'reconciled')
            except _runtime.StateStoreError as exc:
                print(f'{_runtime.YELLOW}[RETRY] Could not release deferred plan: {exc}{_runtime.RESET}')
                stats['degraded'] = True
    finally:
        stopping = _runtime.shutdown_requested or _runtime._shutdown_controller.is_requested()
        executor.shutdown(wait=stopping, cancel_futures=True, timeout=_runtime._shutdown_controller.remaining() if stopping else None)
    for plan in plans:
        if int(plan['id']) not in submitted_plan_ids:
            continue
        series_bucket = str(plan.get('canonicalSeriesKey') or plan.get('seriesKey') or f"{plan['itemType']}:{plan['itemId']}")
        series_admissions[series_bucket] = series_admissions.get(series_bucket, 0) + 1
    submissions_used = len(submitted_plan_ids)
    return (submissions_used, len(plans))

def _run_regeneration_retries(stats: dict, submission_budget: int | None=None, refill_round: int=0, examined_plan_ids: set[int] | None=None, series_admissions: dict[str, int] | None=None) -> None:
    """Work-conserving retry admission without recursive refill calls."""
    del refill_round
    _runtime.RetryQueueProcessor(batch_size=_runtime.RETRY_BATCH_SIZE_PER_CYCLE, run_batch=_runtime._run_regeneration_retry_batch, shutdown_requested=lambda: _runtime.shutdown_requested, emit=print).process(stats, submission_budget=submission_budget, examined_plan_ids=examined_plan_ids, series_admissions=series_admissions)

def run_cycle(cycle_num: int) -> bool:
    print(f'\n{_runtime.BOLD}{_runtime.CYAN}===== Cycle #{cycle_num} ====={_runtime.RESET}')
    _runtime._status_set_phase('cycle_work')
    stats: dict = {'submitted': 0, 'completed': 0, 'timed_out': 0, 'failed': 0, 'deferred': 0, 'api_errors': 0, 'degraded': False, 'cycle_suppressions': 0, 'cooldown_deferrals': 0, 'circuit_deferrals': 0, 'variant_outputs_discovered': 0, 'recovered_pending_outputs': 0, 'translations': [], 'episode_activity': False, 'movie_activity': False}
    stats_lock = _runtime.threading.Lock()
    _runtime.lingarr_build_media_cache()
    try:
        active_before = len(_runtime.lingarr_get_active_translations())
        print(f'[INFO] Lingarr active queue at cycle start: {active_before}')
    except _runtime.ServiceRequestError as exc:
        stats['api_errors'] += 1
        stats['degraded'] = True
        print(f'{_runtime.YELLOW}[WARNING] Cycle starts degraded: {exc}{_runtime.RESET}')
    work: list[tuple] = []
    queue_errors: list[str] = []
    for item_type, id_field in (('episodes', 'sonarrEpisodeId'), ('movies', 'radarrId')):
        try:
            wanted = _runtime.fetch_wanted(item_type)
        except _runtime.ServiceRequestError as exc:
            stats['api_errors'] += 1
            stats['degraded'] = True
            queue_errors.append(item_type)
            print(f'{_runtime.YELLOW}[WARNING] Deferring {item_type} queue: {exc}{_runtime.RESET}')
            continue
        work.extend(((item, item_type, id_field) for item in wanted))
    if _runtime._status_tracker is not None:
        cycle_id = f'{int(_runtime.time.time())}-{cycle_num}'
        jobs = _runtime.build_cycle_jobs(work, _runtime.LANGUAGES, cycle_id, _runtime._item_title)
        _runtime._status_start_cycle(cycle_id, cycle_num, jobs)
    if not work and queue_errors:
        print(f"{_runtime.YELLOW}[WARNING] No processable work; unavailable queue(s): {', '.join(queue_errors)}{_runtime.RESET}")
    elif not work:
        print('[INFO] No wanted items found.')
    else:
        print(f'[INFO] Processing {len(work)} item(s) with {_runtime.PARALLEL_TRANSLATES} worker(s)...')
        dispatch_workers = max(_runtime.PARALLEL_TRANSLATES * 4, _runtime.PARALLEL_TRANSLATES + 1)

        def run_item_with_capacity_cleanup(*args):
            try:
                return _runtime.process_item(*args)
            finally:
                _runtime._shared_capacity.release_current_translation()
        executor = _runtime._DaemonExecutor(max_workers=dispatch_workers, thread_name_prefix='translation-worker')
        try:
            futures = {executor.submit(run_item_with_capacity_cleanup, item, itype, ifield, stats, stats_lock): (item, itype, ifield) for item, itype, ifield in work}
            for future in _runtime.completed_futures(futures, stop_requested=lambda: _runtime.shutdown_requested or _runtime._shutdown_controller.is_requested()):
                try:
                    future.result()
                except Exception as e:
                    print(f'{_runtime.RED}[ERROR] Worker exception: {e}{_runtime.RESET}')
                    stats['degraded'] = True
                    item, item_type, id_field = futures[future]
                    item_id = item.get(id_field)
                    missing = {str(entry.get('code2', '')).strip().lower() for entry in item.get('missing_subtitles', []) if isinstance(entry, dict)}
                    for language in _runtime.LANGUAGES:
                        if language in missing:
                            _runtime._status_transition(item_type, item_id, language, 'failed', reason='translation worker exception')
        finally:
            stopping = _runtime.shutdown_requested or _runtime._shutdown_controller.is_requested()
            executor.shutdown(wait=stopping, cancel_futures=True, timeout=_runtime._shutdown_controller.remaining() if stopping else None)
    repair_results: list[_runtime.RepairJobResult] = []
    pending_count = len(_runtime._pending_repairs)
    if pending_count:
        print(f'[REPAIR] Waiting for {pending_count} queued repair job(s) before Bazarr sync')
        _runtime._status_set_phase('repair_drain')
        repair_results = _runtime._drain_pending_repairs(stats)
        _runtime._status_set_phase('cycle_work')
    _runtime._status_set_phase('retry_recovery')
    _runtime._run_end_cycle_repair_retries(stats)
    _runtime._run_regeneration_retries(stats)
    if _runtime._pending_repairs:
        print('[REPAIR] Draining repairs queued by retry recovery')
        _runtime._status_set_phase('repair_drain')
        repair_results.extend(_runtime._drain_pending_repairs(stats))
    _runtime._status_set_phase('cycle_work')
    pending_prune = _runtime._take_pending_prune_videos()
    if pending_prune:
        print(f'[PRUNE] Checking {len(pending_prune)} translated/repaired video(s) before Bazarr sync')
        prune_stats, prune_episodes, prune_movies = _runtime.run_extra_sidecar_prune(pending_prune)
        prune_stats['prune_bazarr_rescan_batches'] = int(prune_episodes or prune_movies)
        stats.update(prune_stats)
        stats['episode_activity'] = stats['episode_activity'] or prune_episodes
        stats['movie_activity'] = stats['movie_activity'] or prune_movies
    active_after: int | None = None
    active_after_error: _runtime.ServiceRequestError | None = None
    try:
        active_after = len(_runtime.lingarr_get_active_translations())
    except _runtime.ServiceRequestError as exc:
        stats['degraded'] = True
        stats['api_errors'] += 1
        active_after_error = exc
    print(f'\n{_runtime.BOLD}===== Cycle #{cycle_num} Summary ====={_runtime.RESET}')
    print(f"  Submitted  : {stats['submitted']}")
    print(f"  Completed  : {stats['completed']}")
    print(f"  Timed out  : {stats['timed_out']}")
    print(f"  Failed     : {stats['failed']}")
    print(f"  Deferred   : {stats.get('deferred', 0)}")
    print(f"  Cycle state: {('degraded' if stats.get('degraded') or stats.get('api_errors') else 'healthy')}")
    if stats.get('api_errors'):
        print(f"  API errors : {stats['api_errors']}")
    print(f"  Variant outputs found : {stats.get('variant_outputs_discovered', 0)}")
    print(f"  Pending outputs found : {stats.get('recovered_pending_outputs', 0)}")
    print(f"  Cycle suppressions    : {stats.get('cycle_suppressions', 0)}")
    print(f"  Cooldown deferrals    : {stats.get('cooldown_deferrals', 0)}")
    print(f"  Circuit deferrals     : {stats.get('circuit_deferrals', 0)}")
    if stats.get('cleaned'):
        print(f"  Cleaned    : {stats['cleaned']}")
    if stats.get('cleanup_checked'):
        print(f"  Cleanup checked       : {stats['cleanup_checked']}")
        print(f"  Excessive-line cues   : {stats.get('cleanup_excessive_lines', 0)}")
        print(f"  Other cleanup issues  : {stats.get('cleanup_other_issues', 0)}")
        print(f"  Format-only repairs   : {stats.get('cleanup_formatted', 0)}")
        print(f"  AI repairs queued     : {stats.get('cleanup_repair_queued', 0)}")
        print(f"  AI repair attempts    : {stats.get('cleanup_repair_attempts', 0)}")
        print(f"  No-context attempts   : {stats.get('cleanup_second_attempts', 0)}")
        print(f"  AI repairs deferred   : {stats.get('cleanup_repair_deferred', 0)}")
        print(f"  Repaired translations : {stats.get('cleanup_repaired', 0)}")
        print(f"  Quarantined files     : {stats.get('cleanup_quarantined', 0)}")
        print(f"  Undersized sources    : {stats.get('cleanup_undersized_sources', 0)}")
        print(f"  Undersized targets    : {stats.get('cleanup_undersized_targets', 0)}")
        print(f"  Forced sources skipped: {stats.get('cleanup_forced_sources_skipped', 0)}")
        print(f"  Alternative sources  : {stats.get('cleanup_alternative_sources', 0)}")
        print(f"  Source-less warnings : {stats.get('cleanup_source_less_warnings', 0)}")
        print(f"  Repeat quarantines   : {stats.get('cleanup_repeat_quarantines', 0)}")
        print(f"  AI repairs suppressed: {stats.get('cleanup_ai_repairs_suppressed', 0)}")
    if stats.get('prune_videos_checked'):
        print(f"  Prune videos checked : {stats['prune_videos_checked']}")
        print(f"  Prune ready/deferred : {stats.get('prune_ready', 0)}/{stats.get('prune_deferred', 0)}")
        print(f"  Prune candidates     : {stats.get('prune_candidates', 0)}")
        print(f"  Prune quarantined    : {stats.get('prune_quarantined', 0)}")
        print(f"  Prune rescan batches : {stats.get('prune_bazarr_rescan_batches', 0)}")
    if stats['translations']:
        print('  Completed translations:')
        for t in stats['translations']:
            print(f'    {_runtime.GREEN}- {t}{_runtime.RESET}')
    if active_after is not None:
        print(f'  Lingarr active queue now: {active_after}')
    elif active_after_error is not None:
        print(f'{_runtime.YELLOW}  Lingarr active queue unavailable: {active_after_error}{_runtime.RESET}')
    _runtime.sys.stdout.flush()
    had_activity = stats['submitted'] > 0 or stats['completed'] > 0 or stats['episode_activity'] or stats['movie_activity']
    if had_activity and (not _runtime.shutdown_requested):
        _runtime._status_set_phase('synchronization')
        had_episodes = stats['episode_activity']
        had_movies = stats['movie_activity']
        _runtime._tracked_bazarr_sync(had_episodes, had_movies, _runtime.SYNC_TIMEOUT)
        repaired_with_ids = [result for result in repair_results if result.action == 'repaired' and result.item_id is not None]
        missing = [result for result in repaired_with_ids if not _runtime._bazarr_has_repaired_path(result)]
        if missing and (not _runtime.shutdown_requested):
            retry_episodes = any((result.item_type == 'episodes' for result in missing))
            retry_movies = any((result.item_type == 'movies' for result in missing))
            print(f'{_runtime.YELLOW}[WARNING] Bazarr did not register {len(missing)} repaired path(s); retrying scan once{_runtime.RESET}')
            _runtime._tracked_bazarr_sync(retry_episodes, retry_movies, _runtime.SYNC_TIMEOUT)
            still_missing = [result for result in missing if not _runtime._bazarr_has_repaired_path(result)]
            stats['cleanup_bazarr_registration_failures'] = len(still_missing)
            for result in still_missing:
                print(f"{_runtime.YELLOW}[WARNING] Bazarr still does not list repaired subtitle for {result.title} '{result.target_lang}'{_runtime.RESET}")
    queue_drained = _runtime._drain_lingarr_queue()
    if not queue_drained:
        stats['degraded'] = True
    _runtime._reconcile_retry_claims(_runtime._get_validation_state())
    _runtime._reconcile_circuit_trial_leases(_runtime._get_validation_state())
    _runtime._status_finish_cycle(stats)
    return bool(not _runtime.shutdown_requested and (not stats.get('degraded')) and (not stats.get('api_errors')))
EXPORTS = {
    name: globals()[name] for name in (
        '_drain_lingarr_queue', '_run_end_cycle_repair_retries',
        '_run_regeneration_retry_batch', '_run_regeneration_retries',
        'run_cycle',
    )
}
