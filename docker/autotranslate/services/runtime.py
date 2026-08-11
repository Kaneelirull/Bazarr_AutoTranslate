from __future__ import annotations
from ..composition import runtime as _runtime

def _handle_signal(signum, frame):
    _runtime.shutdown_requested = True
    _runtime._shutdown_controller.request()
    _runtime._repair_shutdown_event.set()
    print(f'\n{_runtime.YELLOW}[WARNING] Signal {signum} received — finishing current jobs then stopping.{_runtime.RESET}')
    _runtime.sys.stdout.flush()

def _check_cooldown(item_id: int, target_lang: str, item_type: str='legacy') -> int | None:
    return _runtime._get_validation_state().check_cooldown(item_type, item_id, target_lang)

def _record_submission(item_id: int, target_lang: str, target_path: str | None=None, *, expected_target_path: str | None=None, actual_target_path: str | None=None, video_path: str | None=None, source_path: str | None=None, source_hash: str | None=None, source_language: str | None=None, item_type: str | None=None, target_variant: str | None=None, lingarr_job_id: int | None=None, status: str='submitted') -> int:
    identity = _runtime._target_identity_from_sidecar(target_path, target_lang) if target_path else None
    return _runtime._get_validation_state().record_submission(item_type or 'legacy', item_id, target_lang, cooldown_seconds=_runtime.RESUBMIT_COOLDOWN, target_identity=identity, target_path=target_path, expected_target_path=expected_target_path or target_path, actual_target_path=actual_target_path, video_path=video_path, source_path=source_path, source_hash=source_hash, source_language=source_language, target_variant=target_variant, lingarr_job_id=lingarr_job_id, status=status)

def _mark_submission_submitted(attempt_id: int, job_id: int) -> None:
    _runtime._get_validation_state().mark_submission_submitted(attempt_id, job_id)

def _mark_submission_failed(attempt_id: int, *, failure_category: str | None=None, failure_details: dict | None=None) -> None:
    _runtime._get_validation_state().mark_submission_failed(attempt_id, failure_category=failure_category, failure_details=failure_details)

def _update_submission_actual_path(item_id: int, target_lang: str, actual_target_path: str, target_variant: str, item_type: str='legacy') -> None:
    _runtime._get_validation_state().update_submission_actual_path(item_type, item_id, target_lang, actual_target_path, target_variant)

def _clear_submission(item_id: int, target_lang: str, item_type: str | None=None) -> None:
    """Remove cooldown entry so a cleaned (deleted) file can be re-translated next cycle."""
    removed = _runtime._get_validation_state().clear_submission(item_type, item_id, target_lang)
    if removed:
        _runtime.dbg(f'_clear_submission({item_id}, {target_lang!r}): cleared')

def _clear_submission_for_path(target_path: str | _runtime.Path, target_lang: str) -> int:
    identity = _runtime._target_identity_from_sidecar(target_path, target_lang)
    removed = _runtime._get_validation_state().clear_submissions_for_identity(identity, target_path, target_lang)
    if removed:
        _runtime.dbg(f"Cleared {removed} cooldown entr{('y' if removed == 1 else 'ies')} for {target_path}")
    return removed

def bazarr_url(endpoint: str) -> str:
    return f'{_runtime.BAZARR_URL}/api/{endpoint}'

def lingarr_url(endpoint: str) -> str:
    return f'{_runtime.LINGARR_URL}/api/{endpoint}'

def _request_json(method: str, url: str, *, service: str, operation: str, **kwargs):
    return _runtime.JsonRequester(transport=_runtime.requests, sleep=lambda seconds: _runtime.time.sleep(seconds), emit=print).request(method, url, service=service, operation=operation, **kwargs)

def _bazarr_client() -> _runtime.BazarrClient:
    return _runtime.BazarrClient(_runtime.BAZARR_URL, _runtime.BAZARR_API_KEY, request_json=lambda *args, **kwargs: _runtime._request_json(*args, **kwargs), get=lambda *args, **kwargs: _runtime.requests.get(*args, **kwargs), post=lambda *args, **kwargs: _runtime.requests.post(*args, **kwargs), patch=lambda *args, **kwargs: _runtime.requests.patch(*args, **kwargs), connect_timeout=_runtime.CONNECT_TIMEOUT, sync_start_timeout=_runtime.SYNC_START_TIMEOUT, sync_poll_interval=_runtime.SYNC_POLL_INTERVAL, time_value=lambda: _runtime.time.time(), sleep=lambda seconds: _runtime.time.sleep(seconds), shutdown_requested=lambda: _runtime.shutdown_requested, emit=print)

def fetch_wanted(item_type: str) -> list:
    result = _runtime._bazarr_client().fetch_wanted(item_type)
    _runtime.dbg(f'fetch_wanted({item_type}): {len(result)} item(s)')
    return result

def fetch_subtitles(item_type: str, item_id: int) -> tuple[str, list]:
    result = _runtime._bazarr_client().fetch_subtitles(item_type, item_id)
    _runtime.dbg(f'fetch_subtitles({item_type}, {item_id}): video_path={result[0]!r}')
    return result

def trigger_bazarr_sync(had_episodes: bool, had_movies: bool) -> None:
    _runtime._bazarr_client().trigger_sync(had_episodes, had_movies)

def _job_matches_scan(job: dict, had_episodes: bool, had_movies: bool) -> bool:
    return _runtime.BazarrClient._job_matches(job, had_episodes, had_movies)

def wait_for_bazarr_sync(had_episodes: bool, had_movies: bool, timeout: int) -> bool:
    return _runtime._bazarr_client().wait_for_sync(had_episodes, had_movies, timeout)

def _tracked_bazarr_sync(had_episodes: bool, had_movies: bool, timeout: int) -> bool:
    scope = 'Series and movies' if had_episodes and had_movies else 'Series' if had_episodes else 'Movies'
    job_id = _runtime._status_create_maintenance('bazarr_sync', {'title': scope}, state='synchronizing')
    try:
        _runtime.trigger_bazarr_sync(had_episodes, had_movies)
        success = _runtime.wait_for_bazarr_sync(had_episodes, had_movies, timeout)
    except Exception:
        _runtime._status_complete_maintenance(
            job_id, 'failed', reason='Bazarr synchronization failed'
        )
        raise
    _runtime._status_complete_maintenance(job_id, 'accepted' if success else 'failed', reason=None if success else 'Bazarr synchronization did not complete')
    return success

def _lingarr_client() -> _runtime.LingarrClient:
    return _runtime.LingarrClient(_runtime.LINGARR_URL, _runtime.LINGARR_HEADERS, request_json=lambda *args, **kwargs: _runtime._request_json(*args, **kwargs), get=lambda *args, **kwargs: _runtime.requests.get(*args, **kwargs), post=lambda *args, **kwargs: _runtime.requests.post(*args, **kwargs), connect_timeout=_runtime.CONNECT_TIMEOUT, shutdown_requested=lambda: _runtime.shutdown_requested, emit=print)

def lingarr_get_languages() -> list[_runtime.LingarrSourceLanguage]:
    return _runtime._lingarr_client().languages()

def lingarr_build_media_cache() -> None:
    episode_cache, movie_cache = _runtime._lingarr_client().media_cache()
    with _runtime._media_cache_lock:
        _runtime._episode_cache = episode_cache
        _runtime._movie_cache = movie_cache
    print(f'[INFO] Lingarr media cache: {len(movie_cache)} movie(s), {len(episode_cache)} episode(s)')
    return

def lingarr_resolve_media_id(item_type: str, item_id: int) -> int | None:
    with _runtime._media_cache_lock:
        if item_type == 'episodes':
            return _runtime._episode_cache.get(item_id)
        return _runtime._movie_cache.get(item_id)

def lingarr_get_active_translations() -> list[_runtime.LingarrActiveTranslation]:
    return _runtime._lingarr_client().active_translations()

def lingarr_submit_file(media_id: int, subtitle_path: str, source_lang: str, target_lang: str, media_type: str) -> int | None:
    body = {'mediaId': media_id, 'subtitlePath': subtitle_path, 'sourceLanguage': source_lang, 'targetLanguage': target_lang, 'mediaType': media_type, 'subtitleFormat': 'srt'}
    return _runtime._lingarr_client().submit_file(body)

def lingarr_translate_line(subtitle_line: str, source_lang: str, target_lang: str, context_before: list[str], context_after: list[str], *, repair_label: str='', cue_number: int | None=None, attempt: int | None=None, outcome_meta: dict | None=None, strict: bool=False, cancellation_requested=None) -> str | None:

    def record_provider_event(classification: str, *, retryable: bool, http_status: int | None=None, payload=None) -> None:
        try:
            state = _runtime._get_validation_state()
            if not hasattr(state, 'record_provider_event'):
                return
            shape = None
            if payload is not None:
                from autotranslate.services.lingarr import response_shape
                shape = response_shape(payload)
            state.record_provider_event(provider='lingarr', operation='translate_line', classification=classification, retryable=retryable, http_status=http_status, response_shape=shape)
        except (OSError, _runtime.StateStoreError):
            pass

    def provider_event(event: dict) -> None:
        record_provider_event(event.get('classification', 'transport'), retryable=bool(event.get('retryable')), http_status=event.get('status'), payload=event.get('payload'))
        if outcome_meta is not None:
            outcome_meta.update({'httpStatus': event.get('status'), 'httpDurationSeconds': round(float(event.get('duration', 0)), 3), 'cancelled': event.get('classification') == 'cancelled'})
    provider = _runtime.LingarrProvider(base_url=_runtime.LINGARR_URL, headers=_runtime.LINGARR_HEADERS, post=lambda *args, **kwargs: _runtime.requests.post(*args, **kwargs), timeout=max(_runtime.CONNECT_TIMEOUT, 120), max_attempts=1, on_event=provider_event)
    try:
        return provider.translate_cue(subtitle_line, source_lang, target_lang, context_before, context_after, strict=strict, cancellation_requested=cancellation_requested)
    except _runtime.ProviderResponseError as exc:
        _runtime.dbg(f"lingarr_translate_line failed for {repair_label or 'line repair'}: {exc}")
        return None

def lingarr_get_job(job_id: int) -> dict | None:
    return _runtime._lingarr_client().get_job(job_id)

def lingarr_cancel_job(job_id: int) -> bool:
    return _runtime._lingarr_client().cancel_job(job_id)

def _classify_lingarr_failure(status: str | None, text: str) -> str:
    folded = f"{status or ''} {text}".casefold()
    if 'cancel' in folded or 'interrupt' in folded:
        return 'cancelled'
    if any((token in folded for token in ('context length', 'context window', 'token limit', 'too many tokens'))):
        return 'context_limit'
    if any((token in folded for token in ('parse', 'invalid json', 'deserialize', 'format'))):
        return 'parser'
    if any((token in folded for token in ('disk', 'storage', 'permission denied', 'no space', 'read-only'))):
        return 'storage'
    if any((token in folded for token in ('model', 'provider', 'rate limit', 'content filter'))):
        return 'model'
    if any((token in folded for token in ('timeout', 'http', 'network', 'connection', 'service unavailable'))):
        return 'service'
    return 'unknown'

def _sanitize_failure_message(value: object, limit: int=500) -> str:
    message = str(value or '').replace('\r', ' ').replace('\n', ' ').strip()
    for secret in (_runtime.BAZARR_API_KEY, _runtime.LINGARR_API_KEY):
        if secret:
            message = message.replace(secret, '[redacted]')
    message = _runtime._re.sub('(?i)(?:[a-z]:\\\\|/)(?:[^\\\\/\\s]+[\\\\/])+[^\\\\/\\s]+', '[path]', message)
    if any((marker in message.casefold() for marker in ('[source]', '[target]', 'subtitle text', 'translated text', 'prompt:'))):
        return '[redacted content event]'
    return message[:limit]

def _safe_failure_details(job_id: int | None, *, terminal_job: dict | None=None, elapsed_seconds: float | None=None) -> dict:
    if job_id is None:
        return {}
    job = terminal_job if isinstance(terminal_job, dict) else _runtime.lingarr_get_job(job_id) or {}
    messages = [_runtime._sanitize_failure_message(event.get('message')) for event in job.get('events', []) if isinstance(event, dict) and event.get('message')]
    error_message = _runtime._sanitize_failure_message(job.get('errorMessage'), 1000) or None
    safe_scalars = {}
    blocked = ('source', 'target', 'subtitle', 'prompt', 'text', 'path', 'key', 'token')
    for key, value in job.items():
        if len(safe_scalars) >= 12 or any((part in str(key).casefold() for part in blocked)) or key in {'events', 'errorMessage', 'provider', 'model', 'status', 'progress'} or (not isinstance(value, (str, int, float, bool, type(None)))):
            continue
        safe_scalars[str(key)[:80]] = _runtime._sanitize_failure_message(value, 300) if isinstance(value, str) else value
    combined = ' '.join(filter(None, [error_message, *messages]))
    return {'jobId': job_id, 'status': job.get('status'), 'progress': job.get('progress'), 'category': _runtime._classify_lingarr_failure(job.get('status'), combined), 'errorMessage': error_message, 'events': messages[-10:], 'provider': job.get('provider') or 'unknown', 'model': job.get('model') or 'unknown', 'elapsedSeconds': round(elapsed_seconds, 3) if elapsed_seconds is not None else None, 'safePayload': safe_scalars}

def lingarr_poll_job(job_id: int, deadline: float, label: str, progress_callback=None) -> str | None:
    last_progress = -1
    while not _runtime.shutdown_requested:
        job = _runtime.lingarr_get_job(job_id)
        if job:
            status = job.get('status', '')
            progress = job.get('progress', 0)
            if progress != last_progress:
                last_progress = progress
                _runtime.dbg(f'{label} job {job_id}: status={status} progress={progress}')
                if progress_callback is not None:
                    progress_callback(progress)
            if status == 'Completed':
                return 'Completed'
            if status in ('Failed', 'Cancelled', 'Interrupted'):
                err = job.get('errorMessage', '')
                print(f'{_runtime.RED}[FAIL] {label} Lingarr job {job_id}: {status}' + (f' — {err}' if err else '') + _runtime.RESET)
                return status
        if _runtime.time.time() >= deadline:
            print(f'{_runtime.YELLOW}[TIMEOUT] {label} Lingarr job {job_id} not completed in time{_runtime.RESET}')
            return None
        for _ in range(_runtime.POLL_INTERVAL):
            if _runtime.shutdown_requested:
                return None
            _runtime.time.sleep(1)
    return None

def _recover_failed_lingarr_job(job_id: int, source_path: str, target_path: str, source_lang: str, target_lang: str, label: str) -> dict:
    """Rebuild a failed file job from completed Lingarr lines and repair gaps."""
    from ..subtitles.foundation import SubtitleCue, parse_srt_cues, read_text_best_effort, render_srt_cues
    detail = _runtime.lingarr_get_job(job_id) or {}
    line_rows = detail.get('lines')
    if not isinstance(line_rows, list) or not line_rows:
        return {'recovered': False, 'reason': 'Lingarr returned no positioned lines'}
    raw = read_text_best_effort(_runtime.Path(source_path))
    if raw is None:
        return {'recovered': False, 'reason': 'source unreadable'}
    source_cues, errors = parse_srt_cues(raw)
    if errors or not source_cues:
        return {'recovered': False, 'reason': 'source SRT is not structurally recoverable'}
    positioned = {int(row['position']): row for row in line_rows if isinstance(row, dict) and isinstance(row.get('position'), int)}
    if not positioned:
        return {'recovered': False, 'reason': 'Lingarr line positions missing'}
    offset = 0 if 0 in positioned else 1
    recovered: list[SubtitleCue] = []
    unresolved: list[int] = []
    repair_elapsed = 0.0
    repair_attempts = 0
    repaired_cues = 0
    for index, cue in enumerate(source_cues):
        row = positioned.get(index + offset, {})
        translated = row.get('target') if isinstance(row, dict) else None
        translated = translated.strip() if isinstance(translated, str) else ''
        if not translated:
            before = [entry.text for entry in source_cues[max(0, index - 5):index]]
            after = [entry.text for entry in source_cues[index + 1:index + 6]]
            delays = (5, 15, 45)
            for attempt, delay in enumerate(delays, start=1):
                started = _runtime.time.monotonic()
                translated = _runtime.lingarr_translate_line(cue.text, source_lang, target_lang, before, after, repair_label=label, cue_number=cue.number, attempt=attempt) or ''
                repair_elapsed += _runtime.time.monotonic() - started
                repair_attempts += 1
                if translated.strip():
                    break
                if attempt < len(delays) and (not _runtime.shutdown_requested):
                    _runtime.time.sleep(delay)
            if translated.strip():
                repaired_cues += 1
        if not translated.strip():
            unresolved.append(cue.number)
            continue
        recovered.append(SubtitleCue(cue.number, cue.timestamp, translated.strip().splitlines()))
    if unresolved:
        print(f"{_runtime.YELLOW}[RECOVER] {label} job {job_id}: unresolved cue(s) {','.join(map(str, unresolved[:20]))}{('...' if len(unresolved) > 20 else '')}{_runtime.RESET}")
        return {'recovered': False, 'reason': 'unresolved cues', 'unresolvedCues': unresolved, 'attempts': repair_attempts}
    destination = _runtime.Path(target_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path: _runtime.Path | None = None
    try:
        with _runtime.tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n', prefix=f'.{destination.name}.', suffix='.recovering', dir=destination.parent, delete=False) as handle:
            handle.write(render_srt_cues(recovered))
            handle.flush()
            _runtime.os.fsync(handle.fileno())
            temp_path = _runtime.Path(handle.name)
        _runtime.os.replace(temp_path, destination)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass
    if repair_attempts and repair_elapsed > 0:
        try:
            _runtime._get_validation_state().record_timing_sample(kind='repair', source_language=source_lang, target_language=target_lang, cue_count=max(1, repaired_cues), elapsed_seconds=repair_elapsed, outcome='accepted', lingarr_job_id=job_id, attempts=repair_attempts)
        except _runtime.StateStoreError as exc:
            print(f'{_runtime.YELLOW}[TIMING] Could not persist repair timing: {exc}{_runtime.RESET}')
    print(f'{_runtime.GREEN}[RECOVER] Reconstructed {label} from Lingarr job {job_id}; repaired {repair_attempts} cue attempt(s){_runtime.RESET}')
    return {'recovered': True, 'path': str(destination), 'attempts': repair_attempts, 'repairedCues': repaired_cues, 'repairElapsedSeconds': round(repair_elapsed, 3), 'eventMessages': [str(event.get('message')) for event in detail.get('events', []) if isinstance(event, dict) and event.get('message')][-5:]}
EXPORTS = {
    name: globals()[name] for name in (
        '_handle_signal', '_check_cooldown', '_record_submission',
        '_mark_submission_submitted', '_mark_submission_failed',
        '_update_submission_actual_path', '_clear_submission',
        '_clear_submission_for_path', 'bazarr_url', 'lingarr_url',
        '_request_json', '_bazarr_client', 'fetch_wanted', 'fetch_subtitles',
        'trigger_bazarr_sync', '_job_matches_scan', 'wait_for_bazarr_sync',
        '_tracked_bazarr_sync', '_lingarr_client', 'lingarr_get_languages',
        'lingarr_build_media_cache', 'lingarr_resolve_media_id',
        'lingarr_get_active_translations', 'lingarr_submit_file',
        'lingarr_translate_line', 'lingarr_get_job', 'lingarr_cancel_job',
        '_classify_lingarr_failure', '_sanitize_failure_message',
        '_safe_failure_details', 'lingarr_poll_job',
        '_recover_failed_lingarr_job',
    )
}
