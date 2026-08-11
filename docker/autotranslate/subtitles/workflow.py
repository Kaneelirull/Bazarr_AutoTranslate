from __future__ import annotations
from ..composition import runtime as _runtime

@_runtime.dataclass(frozen=True)
class SidecarClassification:
    path: _runtime.Path
    kind: str
    language: str | None
    tokens: tuple[str, ...]

def _sub_priority(path: str, lang_code2: str) -> int:
    stem = _runtime.os.path.basename(path).lower().removesuffix('.srt')
    for code in sorted(_runtime._LANGUAGE_ALIASES.get(lang_code2, {lang_code2}), key=len, reverse=True):
        idx = stem.rfind(f'.{code}')
        if idx == -1:
            continue
        suffix = stem[idx + len(code) + 1:]
        if suffix == '':
            return 0
        if suffix in ('hi', 'sdh'):
            return 1
        if suffix.isdigit():
            return 1 + int(suffix)
        return 10
    return 99

def _target_suffix(path: str | _runtime.Path, target_lang: str) -> tuple[str, str] | None:
    name = _runtime.Path(path).name
    aliases = sorted(_runtime._LANGUAGE_ALIASES.get(target_lang, {target_lang}), key=len, reverse=True)
    for alias in aliases:
        match = _runtime._re.search(f'\\.{_runtime._re.escape(alias)}(?P<variant>\\.(?:hi|sdh|\\d+))?\\.srt$', name, _runtime._re.IGNORECASE)
        if match:
            return (name[:match.start()], (match.group('variant') or '').lower())
    return None

def _target_identity_from_sidecar(target_path: str | _runtime.Path, target_lang: str) -> str | None:
    suffix = _runtime._target_suffix(target_path, target_lang)
    if suffix is None:
        return None
    base_name, _ = suffix
    return _runtime.os.path.normcase(_runtime.os.path.abspath(_runtime.os.path.join(_runtime.os.path.dirname(str(target_path)), base_name)))

def _submission_identity(metadata: dict, target_lang: str) -> str | None:
    video_path = metadata.get('videoPath')
    if isinstance(video_path, str) and video_path:
        return _runtime.os.path.normcase(_runtime.os.path.abspath(_runtime.os.path.splitext(video_path)[0]))
    for field in ('actualTargetPath', 'expectedTargetPath', 'targetPath'):
        value = metadata.get(field)
        if isinstance(value, str) and value:
            identity = _runtime._target_identity_from_sidecar(value, target_lang)
            if identity is not None:
                return identity
    return None

def _find_target_sidecars(video_path: str, target_lang: str) -> list[str]:
    video = _runtime.Path(video_path)
    matches: list[str] = []
    try:
        entries = video.parent.iterdir()
    except OSError:
        return matches
    for candidate in entries:
        if not candidate.is_file() or candidate.suffix.casefold() != '.srt':
            continue
        suffix = _runtime._target_suffix(candidate, target_lang)
        if suffix is None or suffix[0].casefold() != video.stem.casefold():
            continue
        matches.append(str(candidate))
    return sorted(matches, key=lambda path: (_runtime._sub_priority(path, target_lang), path.casefold()))

def _find_existing_target(video_path: str, target_lang: str) -> str | None:
    return next(iter(_runtime._find_target_sidecars(video_path, target_lang)), None)

def _snapshot_target_sidecars(video_path: str, target_lang: str) -> dict[str, str | None]:
    return {_runtime.os.path.normcase(_runtime.os.path.abspath(path)): _runtime._file_hash_or_none(path) for path in _runtime._find_target_sidecars(video_path, target_lang)}

def _discover_completed_target(video_path: str, target_lang: str, expected_target_path: str, before: dict[str, str | None]) -> str | None:
    expected = _runtime.os.path.normcase(_runtime.os.path.abspath(expected_target_path))
    changed: list[str] = []
    for path in _runtime._find_target_sidecars(video_path, target_lang):
        normalized = _runtime.os.path.normcase(_runtime.os.path.abspath(path))
        current_hash = _runtime._file_hash_or_none(path)
        if normalized not in before or before[normalized] != current_hash:
            changed.append(path)
    if changed:
        selected = next((path for path in changed if _runtime.os.path.normcase(_runtime.os.path.abspath(path)) == expected), changed[0])
        print(f'[TRANSLATE] Discovered Lingarr output {_runtime.os.path.basename(selected)} (expected {_runtime.os.path.basename(expected_target_path)})')
        return selected
    if _runtime.os.path.exists(expected_target_path):
        return expected_target_path
    return None

def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('1', 'true', 'yes')

def _sidecar_tokens(video_path: str | _runtime.Path, subtitle_path: str | _runtime.Path) -> list[str]:
    video_stem = _runtime.Path(video_path).stem
    subtitle_stem = _runtime.Path(subtitle_path).stem
    if subtitle_stem.casefold() == video_stem.casefold():
        return []
    prefix = f'{video_stem}.'
    if not subtitle_stem.casefold().startswith(prefix.casefold()):
        return []
    return [token.casefold() for token in subtitle_stem[len(prefix):].split('.') if token]

def _explicit_non_full_sidecar(video_path: str | _runtime.Path, subtitle_path: str | _runtime.Path) -> str | None:
    return next((token for token in _runtime._sidecar_tokens(video_path, subtitle_path) if token in _runtime._NON_FULL_SUBTITLE_TOKENS), None)

def _classify_sidecar(video_path: str | _runtime.Path, subtitle_path: str | _runtime.Path) -> _runtime.SidecarClassification:
    path = _runtime.Path(subtitle_path)
    tokens = tuple(_runtime._sidecar_tokens(video_path, path))
    language = next((_runtime._ALIAS_TO_LANGUAGE[token] for token in tokens if token in _runtime._ALIAS_TO_LANGUAGE), None)
    managed = {code.casefold() for code in _runtime.LANGUAGES}
    if language in managed:
        kind = 'managed'
    elif language is not None:
        kind = 'nonmanaged'
    elif any((token in _runtime._NON_FULL_SUBTITLE_TOKENS for token in tokens)):
        kind = 'special'
    else:
        kind = 'unknown'
    return _runtime.SidecarClassification(path, kind, language, tokens)

def _find_sidecar_video(subtitle_path: str | _runtime.Path) -> _runtime.Path | None:
    subtitle = _runtime.Path(subtitle_path)
    subtitle_stem = subtitle.stem.casefold()
    try:
        candidates = [path for path in subtitle.parent.iterdir() if path.is_file() and path.suffix.casefold() in _runtime._VIDEO_EXTENSIONS and (subtitle_stem == path.stem.casefold() or subtitle_stem.startswith(f'{path.stem.casefold()}.'))]
    except OSError:
        return None
    return max(candidates, key=lambda path: len(path.stem), default=None)

def _quarantine_identity(target_lang: str, *, video_path: str | _runtime.Path | None=None, target_path: str | _runtime.Path | None=None) -> str | None:
    if video_path is not None:
        base = _runtime.os.path.normcase(_runtime.os.path.abspath(_runtime.os.path.splitext(str(video_path))[0]))
    elif target_path is not None:
        base = _runtime._target_identity_from_sidecar(target_path, target_lang)
    else:
        return None
    return f'{base}|{target_lang.casefold()}' if base is not None else None

def _cycle_quarantine_suppression(video_path: str | _runtime.Path, target_lang: str) -> dict | None:
    identity = _runtime._quarantine_identity(target_lang, video_path=video_path)
    return _runtime._cycle_suppressions.get(identity)

def _resolve_quarantine_history(target_lang: str, *, video_path: str | _runtime.Path | None=None, target_path: str | _runtime.Path | None=None, target_hash: str | None=None) -> bool:
    identity = _runtime._quarantine_identity(target_lang, video_path=video_path, target_path=target_path)
    if identity is None:
        return False
    return _runtime._get_validation_state().resolve_quarantine_events(
        identity, target_hash=target_hash
    )

def _probe_media_duration(video_path: str | _runtime.Path) -> float | None:
    video = _runtime.Path(video_path)
    try:
        stat = video.stat()
    except (OSError, _runtime.StateStoreError) as e:
        _runtime.dbg(f'Could not stat media for duration {video}: {e}')
        return None
    key = (_runtime.os.path.normcase(_runtime.os.path.abspath(str(video))), stat.st_size, stat.st_mtime_ns)
    with _runtime._duration_cache_lock:
        if key in _runtime._duration_cache:
            return _runtime._duration_cache[key]
    try:
        completed = _runtime.subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', str(video)], capture_output=True, text=True, timeout=_runtime.CLEANUP_FFPROBE_TIMEOUT, check=False)
        duration = float(completed.stdout.strip()) if completed.returncode == 0 else 0.0
        if duration <= 0:
            error = completed.stderr.strip().splitlines()[-1:] or ['invalid duration']
            print(f'{_runtime.YELLOW}[SIZE] ffprobe unavailable for {video.name}: {error[0]}{_runtime.RESET}')
            result = None
        else:
            result = duration
    except (OSError, _runtime.subprocess.TimeoutExpired, ValueError) as e:
        print(f'{_runtime.YELLOW}[SIZE] ffprobe unavailable for {video.name}: {e}{_runtime.RESET}')
        result = None
    if result is not None:
        with _runtime._duration_cache_lock:
            _runtime._duration_cache[key] = result
    return result

def _completeness_kwargs() -> dict:
    return {'min_media_duration': _runtime.CLEANUP_MIN_MEDIA_DURATION, 'min_cues_per_minute': _runtime.CLEANUP_MIN_CUES_PER_MINUTE, 'min_text_chars_per_minute': _runtime.CLEANUP_MIN_TEXT_CHARS_PER_MINUTE, 'min_bytes_per_minute': _runtime.CLEANUP_MIN_BYTES_PER_MINUTE, 'min_timeline_coverage': _runtime.CLEANUP_MIN_TIMELINE_COVERAGE, 'required_signals': _runtime.CLEANUP_UNDERSIZED_REQUIRED_SIGNALS}

def _evaluate_completeness(subtitle_path: str | _runtime.Path, media_duration: float | None):
    if not _runtime.CLEANUP_UNDERSIZED_ENABLED or media_duration is None:
        return None
    from .foundation import evaluate_subtitle_completeness
    return evaluate_subtitle_completeness(subtitle_path, media_duration, **_runtime._completeness_kwargs())

def _add_completeness_issue(report, completeness) -> None:
    if completeness is None:
        return
    from .foundation import completeness_issue
    issue = completeness_issue(completeness)
    if issue is not None:
        report.issues.append(issue)

def _count_dialogue_lines(path: str) -> int | None:
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
        count = 0
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.isdigit() or _runtime._TIMESTAMP_RE.match(stripped):
                continue
            count += 1
        return count
    except OSError:
        return None

def _count_srt_cues(path: str) -> int | None:
    try:
        from .foundation import parse_srt_cues, read_text_best_effort
        raw = read_text_best_effort(_runtime.Path(path))
        if raw is None:
            return None
        cues, _errors = parse_srt_cues(raw)
        return len(cues)
    except (OSError, ValueError):
        return None

def _timing_estimate(kind: str, source_lang: str | None, target_lang: str) -> dict:
    try:
        return _runtime._get_validation_state().timing_estimate(kind=kind, source_language=source_lang, target_language=target_lang, cold_seconds_per_cue=_runtime.TRANSLATION_COLD_SECONDS_PER_CUE, alpha=_runtime.TRANSLATION_TIMING_ALPHA)
    except _runtime.StateStoreError as exc:
        print(f'{_runtime.YELLOW}[TIMING] Using cold estimate; state unavailable: {exc}{_runtime.RESET}')
        return {'secondsPerCue': _runtime.TRANSLATION_COLD_SECONDS_PER_CUE, 'sampleCount': 0, 'scope': 'cold_start'}

def _estimate_timeout(source_path: str, source_lang: str, target_lang: str) -> dict:
    cue_count = _runtime._count_srt_cues(source_path)
    if cue_count is None:
        cue_count = _runtime._count_dialogue_lines(source_path) or 0
    learned = _runtime._timing_estimate('file', source_lang, target_lang)
    base = cue_count * learned['secondsPerCue']
    timeout = min(max(_runtime.POLL_TIMEOUT, int(base * _runtime.TRANSLATION_TIMEOUT_MULTIPLIER)), _runtime.TRANSLATION_TIMEOUT_CAP)
    estimate = {**learned, 'cueCount': cue_count, 'estimatedSeconds': round(base, 3), 'timeoutSeconds': timeout, 'lane': 'long' if base > _runtime.LONG_JOB_THRESHOLD else 'short'}
    print(f"[TIMING] Source has {cue_count} cues; {learned['secondsPerCue']:.3f}s/cue ({learned['scope']}, {learned['sampleCount']} samples) - estimate ~{int(base)}s, timeout {timeout}s, lane {estimate['lane']}")
    return estimate

def _derive_target_path(source_path: str, source_lang: str, target_lang: str) -> str | None:
    path = _runtime.Path(source_path)
    stem_tokens = path.stem.split('.')
    aliases = {alias.casefold() for alias in _runtime._LANGUAGE_ALIASES.get(source_lang, {source_lang})}
    language_index = next((index for index in range(len(stem_tokens) - 1, -1, -1) if stem_tokens[index].casefold() in aliases), None)
    if language_index is None:
        return None
    stem_tokens[language_index] = target_lang
    return str(path.with_name('.'.join(stem_tokens) + path.suffix))

def _validation_kwargs() -> dict:
    return {'min_chars': _runtime.CLEANUP_MIN_CHARS, 'min_confidence': _runtime.CLEANUP_MIN_CONFIDENCE, 'max_unique_ratio': _runtime.CLEANUP_MAX_UNIQUE_RATIO, 'max_cyrillic_ratio': _runtime.CLEANUP_MAX_CYRILLIC_RATIO, 'max_cjk_ratio': _runtime.CLEANUP_MAX_CJK_RATIO, 'max_latin_ratio': _runtime.CLEANUP_MAX_LATIN_RATIO, 'min_letters_for_script': _runtime.CLEANUP_MIN_LETTERS_FOR_SCRIPT, 'max_cue_lines': _runtime.CLEANUP_MAX_CUE_LINES, 'max_cue_chars': _runtime.CLEANUP_MAX_CUE_CHARS, 'max_expansion_ratio': _runtime.CLEANUP_MAX_EXPANSION_RATIO, 'max_expansion_chars': _runtime.CLEANUP_MAX_EXPANSION_CHARS, 'max_source_similarity': _runtime.CLEANUP_MAX_SOURCE_SIMILARITY}

def _source_less_line_only_warning(report) -> bool:
    return _runtime.CLEANUP_SOURCELESS_LINE_ONLY_ACTION == 'warn' and bool(report.issues) and all((issue.rule == 'excessive_lines' for issue in report.issues))

def _file_hash_or_none(path: str | _runtime.Path | None) -> str | None:
    if path is None:
        return None
    try:
        from .foundation import file_sha256
        return file_sha256(path)
    except OSError as e:
        _runtime.dbg(f'Could not hash {path}: {e}')
        return None

def _media_identity_for_video(video_path: str | _runtime.Path) -> str:
    video = _runtime.Path(video_path)
    return _runtime.os.path.normcase(_runtime.os.path.abspath(str(video.with_suffix(''))))

def _record_successful_source_readiness(source_path: str | _runtime.Path, source_language: str, target_path: str | _runtime.Path, target_language: str, media_duration: float | None=None) -> bool:
    video = _runtime._find_sidecar_video(source_path)
    source_hash = _runtime._file_hash_or_none(source_path)
    if video is None or source_hash is None:
        return False
    if media_duration is None:
        media_duration = _runtime._probe_media_duration(video)
    target_hash = _runtime._file_hash_or_none(target_path)
    artifact = _runtime._get_validation_state().latest_artifact(target_path, target_hash) if target_hash is not None else None
    try:
        _runtime._get_validation_state().record_source_readiness(media_identity=_runtime._media_identity_for_video(video), video_path=video, source_path=source_path, source_language=source_language, source_hash=source_hash, media_duration_seconds=media_duration, target_artifact_id=int(artifact['id']) if artifact else None, target_language=target_language)
        return True
    except (OSError, _runtime.StateStoreError) as exc:
        print(f'{_runtime.YELLOW}[SOURCE] Could not persist successful-use evidence: {exc}{_runtime.RESET}')
        return False

def _publish_validation_observations(report, target_language: str | None, **extra) -> None:
    observations = list(getattr(report, 'observations', []) or [])
    if not observations:
        return
    grouped: dict[str, list[str]] = {}
    for observation in observations:
        grouped.setdefault(observation.classification, []).append(
            str(observation.cue_number) if observation.cue_number is not None else 'unknown'
        )
    decision_summary = '; '.join(
        f"{classification} cue(s) {', '.join(cues)}"
        for classification, cues in sorted(grouped.items())
    )
    public_title = extra.get('title') or (
        'Movie' if extra.get('itemType') == 'movies' else 'Episode'
        if extra.get('itemType') == 'episodes' else 'Media item'
    )
    print(
        f"[VALIDATION] Retained {public_title} '{target_language or 'unknown'}'; "
        f"copied-source repair skipped for {decision_summary}"
    )
    if _runtime._status_tracker is not None:
        for observation in observations:
            try:
                _runtime._status_tracker.record_validation_observation(
                    title=public_title,
                    episode_code=extra.get('episodeCode'),
                    episode_title=extra.get('episodeTitle'),
                    item_type=extra.get('itemType'),
                    item_id=extra.get('itemId'),
                    target_language=target_language,
                    cue_number=observation.cue_number,
                    classification=observation.classification,
                    reason=observation.reason,
                    evidence=observation.evidence,
                )
            except (OSError, TypeError, ValueError) as exc:
                print(f'{_runtime.YELLOW}[STATUS] Could not publish validation observation: {exc}{_runtime.RESET}')


def _record_validation_result(target_path: str | _runtime.Path, source_hash: str | None, target_hash: str | None, result: str, report, origin: str | None=None, resolve_quarantine: bool=True, **extra) -> bool:
    try:
        from .foundation import VALIDATOR_VERSION
        observations = list(getattr(report, 'observations', []) or [])
        if result == 'valid' and observations:
            result = 'valid_with_warnings'
        details = {'validation': report.to_dict(), **extra}
        if details.get('completeness') is not None:
            details.setdefault('filenameClassification', 'regular')
        target_language = extra.get('targetLanguage')
        if target_language is None:
            target_language = next((language for language in _runtime.LANGUAGES if _runtime._target_suffix(target_path, language) is not None), None)
        target_suffix = _runtime._target_suffix(target_path, target_language) if target_language is not None else None
        trusted_source_hash = source_hash if origin == 'lingarr' or extra.get('trustedSource') else None
        _runtime._get_validation_state().record(target_path, source_hash=trusted_source_hash, target_hash=target_hash, result=result, origin=origin, details=details, source_path=extra.get('sourcePath'), source_language=extra.get('sourceLanguage'), target_language=target_language, target_identity=extra.get('targetIdentity') or (_runtime._target_identity_from_sidecar(target_path, target_language) if target_language is not None else None), target_variant=extra.get('targetVariant') if extra.get('targetVariant') is not None else target_suffix[1] if target_suffix is not None else None, operation=extra.get('operation', 'validation'), parent_artifact_id=extra.get('parentArtifactId'), attempt_id=extra.get('attemptId'), validation_mode=extra.get('validationMode') or ('source-aware' if origin == 'lingarr' and trusted_source_hash else 'target-only'), validator_version=VALIDATOR_VERSION, item_type=extra.get('itemType'), item_id=extra.get('itemId'))
        if observations and result in ('valid', 'valid_with_warnings'):
            _publish_validation_observations(report, target_language, **extra)
        if resolve_quarantine and result in ('valid', 'valid_with_warnings'):
            for language in _runtime.LANGUAGES:
                if _runtime._target_suffix(target_path, language) is not None:
                    _runtime._resolve_quarantine_history(language, target_path=target_path)
                    break
        return True
    except (OSError, _runtime.StateStoreError) as e:
        print(f'{_runtime.YELLOW}[WARNING] Could not persist validation state: {e}{_runtime.RESET}')
        return False

def _record_pending_lingarr_output(source_path: str, target_path: str, source_lang: str, target_lang: str, item_type: str, item_id: int) -> bool:
    source_hash = _runtime._file_hash_or_none(source_path)
    target_hash = _runtime._file_hash_or_none(target_path)
    if target_hash is None:
        return False
    try:
        identity = _runtime._target_identity_from_sidecar(target_path, target_lang)
        suffix = _runtime._target_suffix(target_path, target_lang)
        submission = _runtime._get_validation_state().find_submission(identity, target_lang) if identity is not None else None
        _runtime._get_validation_state().record(target_path, source_hash=source_hash, target_hash=target_hash, result='pending_validation', origin='lingarr', details={'sourcePath': source_path, 'sourceLanguage': source_lang, 'targetLanguage': target_lang, 'itemType': item_type, 'itemId': item_id}, source_path=source_path, source_language=source_lang, target_language=target_lang, target_identity=identity, target_variant=suffix[1] if suffix is not None else '', operation='translation', attempt_id=submission.get('attemptId') if submission is not None else None, validation_mode='source-aware')
        return True
    except (OSError, _runtime.StateStoreError) as exc:
        print(f'{_runtime.YELLOW}[WARNING] Could not persist pending Lingarr provenance: {exc}{_runtime.RESET}')
        return False

def _find_submission_for_target(target_path: str | _runtime.Path, target_lang: str) -> dict | None:
    identity = _runtime._target_identity_from_sidecar(target_path, target_lang)
    if identity is None:
        return None
    return _runtime._get_validation_state().find_submission(identity, target_lang)

def _submission_matches_source(metadata: dict | None, source_path: str, source_language: str | None=None, target_path: str | _runtime.Path | None=None, target_language: str | None=None) -> bool:
    if metadata is None:
        return False
    recorded_source = metadata.get('sourcePath')
    if not isinstance(recorded_source, str) or not recorded_source:
        return False
    recorded_language = metadata.get('sourceLanguage')
    if recorded_language and source_language and (str(recorded_language).casefold() != source_language.casefold()):
        return False
    recorded_hash = metadata.get('sourceHash')
    if not recorded_hash:
        return False
    current_hash = _runtime._file_hash_or_none(source_path)
    if recorded_hash != current_hash:
        return False
    same_path = _runtime.os.path.normcase(_runtime.os.path.abspath(recorded_source)) == _runtime.os.path.normcase(_runtime.os.path.abspath(source_path))
    return same_path or (target_path is not None and target_language is not None and _runtime._is_variant_aware_adjacent_source(source_path, source_language, target_path, target_language))

def _is_variant_aware_adjacent_source(source_path: str | _runtime.Path, source_language: str | None, target_path: str | _runtime.Path, target_language: str) -> bool:
    if not source_language:
        return False
    source = _runtime.Path(source_path)
    target = _runtime.Path(target_path)
    if _runtime.os.path.normcase(_runtime.os.path.abspath(source.parent)) != _runtime.os.path.normcase(_runtime.os.path.abspath(target.parent)):
        return False
    suffix = _runtime._target_suffix(target, target_language)
    if suffix is None:
        return False
    base_name, target_variant = suffix
    aliases = _runtime._LANGUAGE_ALIASES.get(source_language, {source_language})
    acceptable = {f'{base_name}.{alias}{variant}.srt'.casefold() for alias in aliases for variant in ({target_variant, ''} if target_variant else {''})}
    return source.name.casefold() in acceptable

def _record_quarantine_event(target_path: str | _runtime.Path, target_lang: str, target_hash: str | None, report, origin: str | None) -> tuple[dict | None, bool]:
    if target_hash is None:
        return (None, False)
    identity = _runtime._quarantine_identity(target_lang, target_path=target_path)
    if identity is None:
        return (None, False)
    entry, repeated = _runtime._get_validation_state().record_quarantine_event(identity, target_path=target_path, target_hash=target_hash, target_language=target_lang, rules=(issue.rule for issue in report.issues), origin=origin)
    if repeated:
        print(f"[CLEANUP] Repeat offender hash for {_runtime.os.path.basename(str(target_path))}; historical occurrence {entry['occurrences']}")
    return (entry, repeated)

def _apply_cleanup_action(target_path: str | _runtime.Path, source_path: str | _runtime.Path | None, target_lang: str, report, *, repair_attempts: int=0, lingarr_outcome: str='not attempted', attempt_history: list[dict] | None=None, format_fixes: list[str] | None=None, format_recovered_cues: list[int] | None=None, completeness=None, origin: str | None=None, item_type: str | None=None, item_id: int | None=None, donor_history: list[dict] | None=None, candidate_raw: str | None=None, partial_candidate_id: int | None=None, dry_run: bool=False) -> str:
    from .library import quarantine_destination, quarantine_subtitle, write_validation_report
    target = _runtime.Path(target_path)
    source_hash = _runtime._file_hash_or_none(source_path)
    candidate_temp: _runtime.Path | None = None
    if candidate_raw is not None:
        candidate_temp = _runtime._write_recovery_candidate(target, candidate_raw)
    target_hash = _runtime._file_hash_or_none(candidate_temp or target)
    audit = {'sourcePath': str(source_path) if source_path is not None else None, 'targetPath': str(target), 'sourceHash': source_hash, 'targetHash': target_hash, 'targetLanguage': target_lang, 'repairAttempts': repair_attempts, 'repairAttemptHistory': attempt_history or [], 'formatFixes': format_fixes or [], 'formatRecoveredCues': format_recovered_cues or [], 'lingarrOutcome': lingarr_outcome, 'origin': origin or 'unknown', 'filenameClassification': 'regular' if completeness is not None else None, 'completeness': completeness.to_dict() if completeness is not None else None, 'validation': report.to_dict(), 'recordedAt': _runtime.time.strftime('%Y-%m-%dT%H:%M:%SZ', _runtime.time.gmtime())}
    if dry_run or _runtime.CLEANUP_ACTION == 'report':
        print(f"[CLEANUP] {('DRYRUN' if dry_run else 'REPORT')}: would remove {target}")
        _runtime._record_validation_result(target, source_hash, target_hash, 'dry-run-invalid' if dry_run else 'reported-invalid', report, origin=origin, repairAttempts=repair_attempts, repairAttemptHistory=attempt_history or [], formatFixes=format_fixes or [], formatRecoveredCues=format_recovered_cues or [], lingarrOutcome=lingarr_outcome, completeness=completeness.to_dict() if completeness is not None else None)
        return 'dry-run' if dry_run else 'reported'
    if _runtime.CLEANUP_ACTION == 'quarantine':
        try:
            if target_hash is None:
                raise _runtime.StateStoreError('target hash unavailable before quarantine')
            destination = quarantine_destination(target, _runtime.CLEANUP_ROOTS, _runtime.CLEANUP_QUARANTINE_DIR)
            input_destination = None
            pending_metadata = {'rules': [issue.rule for issue in report.issues], 'holdIdentity': _runtime._quarantine_identity(target_lang, target_path=target), 'phase': 'intent', 'audit': audit}
            if candidate_temp is not None:
                input_destination = destination.with_name(f'{destination.stem}.input{destination.suffix}')
                counter = 1
                while input_destination.exists():
                    input_destination = destination.with_name(f'{destination.stem}.input.{counter}{destination.suffix}')
                    counter += 1
                pending_metadata.update({'candidatePath': str(candidate_temp), 'candidateHash': target_hash, 'inputDestination': str(input_destination)})
            attempt_payload = None
            if item_type in ('episodes', 'movies') and item_id is not None and (source_hash is not None):
                from .foundation import source_cue_signatures
                state_store = _runtime._get_validation_state()
                active_plan = state_store.active_retry_plan(item_type, item_id, target_lang)
                attempt_payload = {'item_type': item_type, 'item_id': item_id, 'target_language': target_lang, 'source_hash': source_hash, 'target_hash': target_hash, 'attempt_number': int(active_plan['attemptCount']) + 1 if active_plan else 1, 'artifact_path': str(destination), 'report_path': f'{destination}.validation.json', 'failure_rules': [issue.rule for issue in report.issues], 'cue_signatures': source_cue_signatures(source_path), 'repair_provenance': attempt_history or [], 'donor_provenance': donor_history or []}
                pending_metadata['quarantineAttempt'] = attempt_payload
                pending_metadata['partialCandidateId'] = partial_candidate_id
            artifact = _runtime._get_validation_state().latest_artifact(target, target_hash)
            if artifact is None:
                artifact_id = _runtime._get_validation_state().record_artifact_version(target, target_hash=target_hash, source_path=source_path, source_hash=source_hash if origin == 'lingarr' else None, source_language=None, target_language=target_lang, origin=origin or 'external', operation='quarantine', target_identity=_runtime._target_identity_from_sidecar(target, target_lang), disposition='quarantine_pending', pending_destination=destination, pending_metadata=pending_metadata)
            else:
                artifact_id = int(artifact['id'])
                _runtime._get_validation_state().set_artifact_disposition(artifact_id, 'quarantine_pending', pending_destination=destination, pending_metadata=pending_metadata)
            if candidate_temp is not None:
                candidate_to_move = candidate_temp
                candidate_temp = None
                quarantine_subtitle(target, _runtime.CLEANUP_ROOTS, _runtime.CLEANUP_QUARANTINE_DIR, destination=input_destination, access_coordinator=_runtime._artifact_access)
                pending_metadata['phase'] = 'input_archived'
                _runtime._get_validation_state().set_artifact_disposition(artifact_id, 'quarantine_pending', pending_destination=destination, pending_metadata=pending_metadata)
                destination = quarantine_subtitle(candidate_to_move, _runtime.CLEANUP_ROOTS, _runtime.CLEANUP_QUARANTINE_DIR, destination=destination, access_coordinator=_runtime._artifact_access)
                audit['supersededInputArtifact'] = input_destination.name
                audit['partialCandidate'] = True
            else:
                destination = quarantine_subtitle(target, _runtime.CLEANUP_ROOTS, _runtime.CLEANUP_QUARANTINE_DIR, destination=destination, access_coordinator=_runtime._artifact_access)
            if _runtime._file_hash_or_none(destination) != target_hash:
                raise _runtime.StateStoreError('quarantine destination hash does not match persisted intent')
            pending_metadata['phase'] = 'candidate_archived'
            pending_metadata['audit'] = audit
            _runtime._get_validation_state().set_artifact_disposition(artifact_id, 'quarantine_pending', pending_destination=destination, pending_metadata=pending_metadata)
            hold_identity = _runtime._quarantine_identity(target_lang, target_path=target)
            if hold_identity is not None:
                event, repeated, pending_metadata = _runtime._get_validation_state().record_pending_quarantine_hold(artifact_id, identity=hold_identity, target_path=target, target_hash=target_hash, target_language=target_lang, rules=(issue.rule for issue in report.issues), origin=origin)
            else:
                event, repeated = (None, False)
            suppression = _runtime._cycle_suppressions.suppress(_runtime._quarantine_identity(target_lang, target_path=target), action='quarantined')
            setattr(report, 'repeat_offender', repeated)
            if event is not None:
                audit['quarantineEvent'] = event
                audit['repeatOffender'] = repeated
                if repeated:
                    print(f"[CLEANUP] Repeat offender hash for {_runtime.os.path.basename(str(target))}; historical occurrence {event['occurrences']}")
            if suppression is not None:
                audit['cycleSuppression'] = suppression
            report_path = write_validation_report(destination, audit)
            pending_metadata['phase'] = 'report_written'
            pending_metadata['audit'] = audit
            if attempt_payload is not None:
                attempt_payload['report_path'] = str(report_path)
                pending_metadata['quarantineAttempt'] = attempt_payload
            _runtime._get_validation_state().set_artifact_disposition(artifact_id, 'quarantine_pending', pending_destination=destination, pending_metadata=pending_metadata)
            _runtime._get_validation_state().finalize_quarantine_operation(artifact_id, attempt=attempt_payload, partial_candidate_id=partial_candidate_id)
            _runtime._record_validation_result(target, source_hash, target_hash, 'quarantined', report, origin=origin, quarantinePath=str(destination), repairAttempts=repair_attempts, repairAttemptHistory=attempt_history or [], formatFixes=format_fixes or [], formatRecoveredCues=format_recovered_cues or [], lingarrOutcome=lingarr_outcome, completeness=completeness.to_dict() if completeness is not None else None)
            print(f'[CLEANUP] Quarantined {target} -> {destination}')
            return 'quarantined'
        except (OSError, _runtime.StateStoreError) as e:
            print(f'{_runtime.RED}[ERROR] Could not quarantine {target}: {e}{_runtime.RESET}')
            return 'action-failed'
        finally:
            if candidate_temp is not None:
                try:
                    candidate_temp.unlink()
                except OSError:
                    pass
    try:
        if target_hash is None:
            raise _runtime.StateStoreError('target hash unavailable before deletion')
        artifact = _runtime._get_validation_state().latest_artifact(target, target_hash)
        if artifact is None:
            artifact_id = _runtime._get_validation_state().record_artifact_version(target, target_hash=target_hash, source_path=source_path, source_hash=source_hash if origin == 'lingarr' else None, source_language=None, target_language=target_lang, origin=origin or 'external', operation='delete', target_identity=_runtime._target_identity_from_sidecar(target, target_lang), disposition='deletion_pending', pending_metadata={'rules': [issue.rule for issue in report.issues], 'holdIdentity': _runtime._quarantine_identity(target_lang, target_path=target)})
        else:
            artifact_id = int(artifact['id'])
            _runtime._get_validation_state().set_artifact_disposition(artifact_id, 'deletion_pending', pending_metadata={'rules': [issue.rule for issue in report.issues], 'holdIdentity': _runtime._quarantine_identity(target_lang, target_path=target)})
        target.unlink()
        _runtime._get_validation_state().set_artifact_disposition(artifact_id, 'deleted')
        _runtime._record_quarantine_event(target, target_lang, target_hash, report, origin)
        _runtime._cycle_suppressions.suppress(_runtime._quarantine_identity(target_lang, target_path=target), action='deleted')
        _runtime._record_validation_result(target, source_hash, target_hash, 'deleted', report, origin=origin, repairAttempts=repair_attempts, repairAttemptHistory=attempt_history or [], formatFixes=format_fixes or [], formatRecoveredCues=format_recovered_cues or [], lingarrOutcome=lingarr_outcome, completeness=completeness.to_dict() if completeness is not None else None)
        print(f'[CLEANUP] Deleted {target}')
        return 'deleted'
    except (OSError, _runtime.StateStoreError) as e:
        print(f'{_runtime.RED}[ERROR] Could not delete {target}: {e}{_runtime.RESET}')
        return 'action-failed'

def _target_repair_lock(target_path: str | _runtime.Path):
    return _runtime._artifact_access.hold(target_path)

def _write_recovery_candidate(target_path: str | _runtime.Path, raw: str, *, same_directory: bool=True) -> _runtime.Path:
    target = _runtime.Path(target_path)
    with _runtime.tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='', prefix=f'.{target.name}.recovery.', suffix='.srt', dir=target.parent if same_directory else None, delete=False) as handle:
        handle.write(raw)
        return _runtime.Path(handle.name)

def _normalize_managed_output(path: str | _runtime.Path, label: str) -> bool:
    from .foundation import normalize_managed_file
    try:
        normalize_managed_file(path)
        return True
    except OSError as exc:
        print(f'{_runtime.RED}[ERROR] Could not set managed ownership for {label}: {exc}{_runtime.RESET}')
        return False

def _replace_managed_file(candidate: str | _runtime.Path, target: str | _runtime.Path) -> None:
    from .foundation import normalize_managed_file
    candidate_path = _runtime.Path(candidate)
    try:
        normalize_managed_file(candidate_path)
        _runtime.os.replace(candidate_path, target)
    except OSError:
        try:
            candidate_path.unlink()
        except OSError:
            pass
        raise

def _replace_managed_file_if_current(candidate: str | _runtime.Path, target: str | _runtime.Path, *, source_path: str | _runtime.Path | None, expected_source_hash: str | None, expected_target_hash: str | None, source_language: str | None, target_language: str, origin: str | None, operation: str, parent_artifact_id: int | None) -> bool:
    if expected_source_hash is not None and _runtime._file_hash_or_none(source_path) != expected_source_hash or (expected_target_hash is not None and _runtime._file_hash_or_none(target) != expected_target_hash):
        try:
            _runtime.Path(candidate).unlink()
        except OSError:
            pass
        return False
    candidate_hash = _runtime._file_hash_or_none(candidate)
    if candidate_hash is None:
        raise OSError(f'could not hash replacement candidate {candidate}')
    suffix = _runtime._target_suffix(target, target_language)
    pending_artifact_id = _runtime._get_validation_state().record_artifact_version(target, target_hash=candidate_hash, source_path=source_path, source_hash=expected_source_hash, source_language=source_language, target_language=target_language, origin=origin or 'external', operation=operation, parent_artifact_id=parent_artifact_id, target_identity=_runtime._target_identity_from_sidecar(target, target_language), target_variant=suffix[1] if suffix is not None else '', disposition='replacement_pending', pending_destination=target)
    _runtime._replace_managed_file(candidate, target)
    _runtime._get_validation_state().set_artifact_disposition(pending_artifact_id, 'active')
    return True

def _perform_repair(source_path: str, target_path: str, source_lang: str, target_lang: str, item_id: int | None, title: str, item_type: str | None, initial_report, expected_target_hash: str | None, expected_source_hash: str | None=None, recovery_raw: str | None=None, format_fixes: list[str] | None=None, format_recovered_cues: list[int] | None=None, completeness=None, origin: str | None='lingarr', series_key: str | None=None, series_title: str | None=None, status_ref: dict | None=None, cancellation_requested=None, publication_guard=None, retry_plan_id: int | None=None, trial_owner: str | None=None, trial_job_id: int | None=None, trial_plan_id: int | None=None, trial_generation: int | None=None) -> _runtime.RepairJobResult:
    from .foundation import target_language_for_code
    from .repair import repair_subtitle_file
    label = title or _runtime.os.path.basename(target_path)
    publication_admitted = False

    def admit_publication() -> bool:
        nonlocal publication_admitted
        if publication_admitted:
            return True
        if cancellation_requested is not None and cancellation_requested():
            return False
        if publication_guard is not None and (not publication_guard()):
            return False
        publication_admitted = True
        return True
    detector = _runtime._get_cleanup_detector()
    target_language = target_language_for_code(target_lang)
    if detector is None or target_language is None:
        return _runtime.RepairJobResult('repair-deferred', initial_report, label, target_lang, item_type, item_id, target_path=str(target_path))
    with _runtime._target_repair_lock(target_path):
        if expected_target_hash is not None and _runtime._file_hash_or_none(target_path) != expected_target_hash:
            print(f"[REPAIR] Deferred {label} '{target_lang}': target changed while queued")
            return _runtime.RepairJobResult('repair-deferred', initial_report, label, target_lang, item_type, item_id, target_path=str(target_path))
        if expected_source_hash is not None and _runtime._file_hash_or_none(source_path) != expected_source_hash:
            print(f"[REPAIR] Deferred {label} '{target_lang}': source changed while queued")
            return _runtime.RepairJobResult('repair-deferred', initial_report, label, target_lang, item_type, item_id, target_path=str(target_path))
        if recovery_raw is None:
            from .foundation import read_text_best_effort
            recovery_raw = read_text_best_effort(_runtime.Path(target_path))
            if recovery_raw is None:
                return _runtime.RepairJobResult('repair-deferred', initial_report, label, target_lang, item_type, item_id, target_path=str(target_path))
        recovery_temp = _runtime._write_recovery_candidate(target_path, recovery_raw)
        working_path = recovery_temp
        attempt_state: dict = {}
        progress_started = _runtime.time.monotonic()
        last_progress_signature: tuple | None = None
        last_progress_at = 0.0

        def attempt_logger(event: dict) -> None:
            attempt_state.clear()
            attempt_state.update(event)
            if event.get('event') == 'rejected' and event.get('outputFingerprint') and event.get('sourceCueHash') and (item_type in ('episodes', 'movies')) and (item_id is not None) and expected_source_hash:
                try:
                    _runtime._get_validation_state().record_failure_fingerprint(item_type=item_type, item_id=item_id, target_language=target_lang, source_file_hash=expected_source_hash, source_cue_hash=event['sourceCueHash'], strategy_key=event.get('strategy') or 'unknown', provider='lingarr', config_fingerprint=_runtime._VALIDATION_CONFIG_FINGERPRINT, output_fingerprint=event['outputFingerprint'], failure_class=','.join(event.get('validationRules') or ['validation']))
                except _runtime.StateStoreError as exc:
                    print(f'{_runtime.YELLOW}[REPAIR] Could not persist failure fingerprint: {exc}{_runtime.RESET}')
            if event['event'] == 'donor_accepted':
                print(f"[DONOR] Cue {event.get('cueNumber')} recovered from quarantine attempt {event.get('sourceAttempt')}")
                return
            cue = event.get('cueNumber')
            attempt = event.get('attempt')
            maximum = event.get('maxAttempts')
            duration = event.get('durationSeconds', 0)
            http_status = event.get('httpStatus')
            http_label = f' HTTP {http_status}' if http_status is not None else ''
            worker = _runtime.threading.current_thread().name
            if event['event'] == 'sending':
                context = 'without context' if event.get('withoutContext') else f"with context before={event.get('contextBefore', 0)} after={event.get('contextAfter', 0)}"
                print(f"[REPAIR] {worker} sending {label} '{target_lang}' cue {cue} attempt {attempt}/{maximum} {context}")
            elif event['event'] == 'accepted':
                print(f'[REPAIR] Cue {cue} attempt {attempt} accepted{http_label} after {duration:.1f}s')
            elif event['event'] == 'rejected':
                rules = ','.join(event.get('validationRules', [])) or 'validation'
                print(f'[REPAIR] Cue {cue} attempt {attempt} rejected{http_label} after {duration:.1f}s: {rules}')
            else:
                print(f"[REPAIR] Cue {cue} attempt {attempt} failed{http_label} after {duration:.1f}s: {event.get('outcome')}")

        def progress_callback(event: dict) -> None:
            nonlocal last_progress_signature, last_progress_at
            stage = event.get('stage')
            state = 'repair_validating' if stage == 'repair_validating' else 'repairing'
            completed = int(event.get('completedCues') or 0)
            total = int(event.get('totalRepairableCues') or 0)
            elapsed = max(0.001, _runtime.time.monotonic() - progress_started)
            eta = round(elapsed / completed * max(0, total - completed), 1) if completed else None
            details = {key: value for key, value in event.items() if key in {'totalRepairableCues', 'completedCues', 'currentCueNumber', 'currentCuePosition', 'currentCueOrdinal', 'currentAttempt', 'maxAttempts', 'contextEnabled', 'lastHttpStatus', 'lastRequestDurationSeconds', 'rejectedAttempts', 'successfulCues', 'unresolvedCues', 'progress'}}
            details.update({'repairStage': stage, 'attempts': event.get('currentAttempt')})
            if eta is not None:
                details['etaSeconds'] = eta
                details['estimatedSeconds'] = round(elapsed + eta, 1)
            signature = (state, stage, event.get('currentCueOrdinal'), event.get('currentAttempt'), completed, event.get('rejectedAttempts'), event.get('unresolvedCues'))
            now = _runtime.time.monotonic()
            if signature == last_progress_signature and now - last_progress_at < 0.5:
                return
            last_progress_signature = signature
            last_progress_at = now
            _runtime._status_ref_transition(status_ref, state, details=details)

        def translator(line: str, before: list[str], after: list[str]):
            if cancellation_requested is not None and cancellation_requested():
                return (None, {'cancelled': True})
            outcome_meta: dict = {}
            translated = _runtime.lingarr_translate_line(line, source_lang, target_lang, before, after, repair_label=label, cue_number=attempt_state.get('cueNumber'), attempt=attempt_state.get('attempt'), outcome_meta=outcome_meta, strict=attempt_state.get('strategy') == 'strict_isolated', cancellation_requested=cancellation_requested)
            return (translated, outcome_meta)

        def donor_event_logger(event: dict) -> None:
            if item_type not in ('episodes', 'movies') or item_id is None:
                return
            try:
                _runtime._get_validation_state().record_donor_event(item_type=item_type, item_id=item_id, target_language=target_lang, cue_number=event.get('cueNumber'), donor_attempt_id=event.get('donorAttemptId'), reason_code=event.get('reasonCode') or 'current_validation_failed')
            except _runtime.StateStoreError as exc:
                print(f'{_runtime.YELLOW}[DONOR] Could not persist donor diagnostic: {exc}{_runtime.RESET}')
        cue_list = ', '.join((str(i + 1) for i in initial_report.repairable_cue_indexes))
        print(f"[REPAIR] Retrying {label} '{target_lang}' cue position(s): {cue_list}")
        try:
            donor_attempts = []
            cue_recoveries = []
            exhausted_strategies = {}
            if _runtime.DONOR_RECOVERY_ENABLED and item_type in ('episodes', 'movies') and (item_id is not None):
                donor_attempts = _runtime._get_validation_state().quarantine_attempts(item_type, item_id, target_lang)
                cue_recoveries = _runtime._get_validation_state().cue_recoveries(item_type, item_id, target_lang, source_file_hash=expected_source_hash)
                if expected_source_hash and hasattr(_runtime._get_validation_state(), 'exhausted_recovery_strategies'):
                    exhausted_strategies = _runtime._get_validation_state().exhausted_recovery_strategies(item_type=item_type, item_id=item_id, target_language=target_lang, source_file_hash=expected_source_hash, provider='lingarr', config_fingerprint=_runtime._VALIDATION_CONFIG_FINGERPRINT)
            repair = repair_subtitle_file(_runtime.Path(source_path), working_path, detector, target_language, translator, target_lang=target_lang, max_attempts=_runtime.CLEANUP_MAX_REPAIR_ATTEMPTS, context_lines=_runtime.CLEANUP_REPAIR_CONTEXT_LINES, attempt_logger=attempt_logger, progress_callback=progress_callback, donor_attempts=donor_attempts, cue_recoveries=cue_recoveries, donor_event_logger=donor_event_logger, artifact_access=_runtime._artifact_access, exhausted_strategies=exhausted_strategies, cancellation_requested=lambda: cancellation_requested is not None and cancellation_requested() if cancellation_requested is not None else None, **_runtime._validation_kwargs())
            second_attempts = sum((entry.get('attempt', 0) > 1 and entry.get('withoutContext') for entry in repair.attempt_history))
            if repair.interrupted or (cancellation_requested is not None and cancellation_requested()):
                print(f"[REPAIR] Persisted {label} '{target_lang}' for restart after shutdown interruption")
                return _runtime.RepairJobResult('repair-deferred', repair.report, label, target_lang, item_type, item_id, repair.attempts, second_attempts, str(target_path))
            if repair.success:
                if expected_source_hash is not None and _runtime._file_hash_or_none(source_path) != expected_source_hash:
                    print(f"[REPAIR] Deferred {label} '{target_lang}': source changed during repair")
                    return _runtime.RepairJobResult('repair-deferred', repair.report, label, target_lang, item_type, item_id, repair.attempts, second_attempts, str(target_path))
                if expected_target_hash is not None and _runtime._file_hash_or_none(target_path) != expected_target_hash:
                    print(f"[REPAIR] Deferred {label} '{target_lang}': target changed during repair")
                    return _runtime.RepairJobResult('repair-deferred', repair.report, label, target_lang, item_type, item_id, repair.attempts, second_attempts, str(target_path))
                parent = _runtime._get_validation_state().latest_artifact(target_path, expected_target_hash)
                candidate_hash = _runtime._file_hash_or_none(recovery_temp)
                if candidate_hash is None:
                    return _runtime.RepairJobResult('repair-deferred', repair.report, label, target_lang, item_type, item_id, repair.attempts, second_attempts, str(target_path))
                if not admit_publication():
                    return _runtime.RepairJobResult('repair-deferred', repair.report, label, target_lang, item_type, item_id, repair.attempts, second_attempts, str(target_path))
                suffix = _runtime._target_suffix(target_path, target_lang)
                try:
                    pending_artifact_id = _runtime._get_validation_state().record_artifact_version(target_path, target_hash=candidate_hash, source_path=source_path, source_hash=expected_source_hash, source_language=source_lang, target_language=target_lang, origin=origin or 'external', operation='cue_repair', parent_artifact_id=parent.get('id') if parent else None, target_identity=_runtime._target_identity_from_sidecar(target_path, target_lang), target_variant=suffix[1] if suffix is not None else '', disposition='replacement_pending', pending_destination=target_path)
                except _runtime.StateStoreError as exc:
                    print(f"{_runtime.YELLOW}[REPAIR] Deferred {label} '{target_lang}': could not persist replacement intent ({exc}){_runtime.RESET}")
                    return _runtime.RepairJobResult('repair-deferred', repair.report, label, target_lang, item_type, item_id, repair.attempts, second_attempts, str(target_path))
                _runtime._replace_managed_file(recovery_temp, target_path)
                recovery_temp = None
                try:
                    _runtime._get_validation_state().set_artifact_disposition(pending_artifact_id, 'active')
                except _runtime.StateStoreError as exc:
                    print(f'{_runtime.YELLOW}[REPAIR] Replacement completed but state finalization was deferred: {exc}{_runtime.RESET}')
                    return _runtime.RepairJobResult('repair-deferred', repair.report, label, target_lang, item_type, item_id, repair.attempts, second_attempts, str(target_path))
                repaired = ', '.join((str(number) for number in repair.repaired_cues))
                print(f"{_runtime.GREEN}[REPAIR] Repaired and validated {label} '{target_lang}' cue(s): {repaired}{_runtime.RESET}")
                if not _runtime._record_validation_result(target_path, _runtime._file_hash_or_none(source_path), _runtime._file_hash_or_none(target_path), 'valid', repair.report, origin=origin, repairedCues=repair.repaired_cues, repairAttempts=repair.attempts, repairAttemptHistory=repair.attempt_history, donorRecovery=repair.donor_history, formatFixes=format_fixes or [], formatRecoveredCues=format_recovered_cues or [], lingarrOutcome='repaired', completeness=completeness.to_dict() if completeness is not None else None, sourcePath=source_path, sourceLanguage=source_lang, targetLanguage=target_lang, title=label, itemType=item_type, itemId=item_id, episodeCode=_runtime.episode_identity_from_path(target_path), operation='cue_repair', parentArtifactId=parent.get('id') if parent else None):
                    return _runtime.RepairJobResult('repair-deferred', repair.report, label, target_lang, item_type, item_id, repair.attempts, second_attempts, str(target_path))
                return _runtime.RepairJobResult('repaired', repair.report, label, target_lang, item_type, item_id, repair.attempts, second_attempts, str(target_path), repair.donor_history[0].get('sourceAttempt') if repair.donor_history else None)
            print(f"{_runtime.YELLOW}[REPAIR] Could not repair {label} '{target_lang}': {repair.reason}{_runtime.RESET}")
            if repair.manual_review:
                setattr(repair.report, 'manual_review', True)
            partial_id = None
            if repair.partial_raw and repair.repaired_cues and (item_type in ('episodes', 'movies')) and (item_id is not None) and expected_source_hash:
                try:
                    from .foundation import cue_source_signature, parse_srt_cues, read_text_best_effort
                    source_raw = read_text_best_effort(_runtime.Path(source_path)) or ''
                    source_cues, source_errors = parse_srt_cues(source_raw)
                    partial_cues, partial_errors = parse_srt_cues(repair.partial_raw)
                    partial_hash = _runtime.hashlib.sha256(repair.partial_raw.encode('utf-8')).hexdigest()
                    if not source_errors and (not partial_errors):
                        partial_id = _runtime._get_validation_state().record_partial_candidate(item_type=item_type, item_id=item_id, source_language=source_lang, target_language=target_lang, source_hash=expected_source_hash, target_hash=partial_hash, changed_cues=repair.repaired_cues, unresolved_cues=repair.unresolved_cues, provenance=repair.attempt_history + repair.donor_history, artifact_path=None)
                        changed = set(repair.repaired_cues)
                        by_number = {cue.number: cue for cue in partial_cues}
                        for source_cue in source_cues:
                            target_cue = by_number.get(source_cue.number)
                            if source_cue.number not in changed or target_cue is None:
                                continue
                            signature = cue_source_signature(source_cue)
                            target_text = target_cue.text
                            _runtime._get_validation_state().record_cue_recovery(partial_candidate_id=partial_id, item_type=item_type, item_id=item_id, source_language=source_lang, target_language=target_lang, source_file_hash=expected_source_hash, source_cue_number=source_cue.number, source_cue_hash=signature['sourceHash'], source_signature=signature, cue_start_ms=signature.get('startMs'), target_text=target_text, target_hash=_runtime.hashlib.sha256(target_text.encode('utf-8')).hexdigest(), recovery_stage='cue_repair')
                except (OSError, _runtime.StateStoreError) as exc:
                    print(f'{_runtime.YELLOW}[REPAIR] Could not persist partial progress: {exc}{_runtime.RESET}')
            if not admit_publication():
                return _runtime.RepairJobResult('repair-deferred', repair.report, label, target_lang, item_type, item_id, repair.attempts, second_attempts, str(target_path))
            action = _runtime._apply_cleanup_action(target_path, source_path, target_lang, repair.report, repair_attempts=repair.attempts, lingarr_outcome=repair.reason, attempt_history=repair.attempt_history, format_fixes=format_fixes, format_recovered_cues=format_recovered_cues, completeness=completeness, origin=origin, item_type=item_type, item_id=item_id, donor_history=repair.donor_history, candidate_raw=repair.partial_raw if _runtime.CLEANUP_ACTION == 'quarantine' else None, partial_candidate_id=partial_id)
            if action in ('quarantined', 'deleted') and item_id is not None:
                _runtime._clear_submission(item_id, target_lang, item_type)
                _runtime._clear_submission_for_path(target_path, target_lang)
                print(f"[CLEANUP] Cleared cooldown for retry: {label} '{target_lang}'")
            return _runtime.RepairJobResult(action, repair.report, label, target_lang, item_type, item_id, repair.attempts, second_attempts, str(target_path))
        finally:
            if recovery_temp is not None:
                try:
                    recovery_temp.unlink()
                except OSError:
                    pass

def _get_repair_executor() -> _runtime._DaemonRepairExecutor:
    with _runtime._repair_executor_lock:
        if _runtime._repair_executor is None:
            if _runtime.shutdown_requested:
                raise RuntimeError('repair admission stopped during shutdown')
            _runtime._repair_shutdown_event.clear()
            _runtime._repair_executor = _runtime._DaemonRepairExecutor(max_workers=_runtime.PARALLEL_TRANSLATES, thread_name_prefix='repair-worker')
        return _runtime._repair_executor

def _run_repair_with_capacity(capacity_token: int, job_kwargs: dict, metadata: dict) -> _runtime.RepairJobResult:
    """Run one repair after its priority reservation obtains shared capacity."""
    status_ref = metadata.get('status_ref')
    durable_job_id = metadata.get('durable_job_id')
    _runtime._status_ref_transition(status_ref, 'repair_waiting_capacity', details={'repairStage': 'waiting_capacity'})
    if not _runtime._shared_capacity.start_repair(capacity_token):
        _runtime._shared_capacity.release(capacity_token)
        if durable_job_id is not None:
            try:
                _runtime._get_validation_state().transition_repair_job(durable_job_id, 'persisted_for_restart', shutdown_classification='cancelled_before_start', expected_states=('queued', 'persisted_for_restart'))
            except _runtime.StateStoreError:
                pass
        return _runtime.RepairJobResult('repair-deferred', job_kwargs.get('initial_report'), job_kwargs.get('title') or '', job_kwargs.get('target_lang') or '', job_kwargs.get('item_type'), job_kwargs.get('item_id'), target_path=str(job_kwargs.get('target_path') or ''))
    try:
        if durable_job_id is not None:
            try:
                _runtime._get_validation_state().transition_repair_job(durable_job_id, 'active', lease_owner=f'worker:{_runtime.threading.current_thread().name}', lease_expires_at=_runtime.time.time() + _runtime.REPAIR_SHUTDOWN_GRACE_SECONDS, expected_states=('queued', 'persisted_for_restart'))
            except _runtime.StateStoreError as exc:
                print(f'{_runtime.YELLOW}[REPAIR] Durable lease update failed: {exc}{_runtime.RESET}')
        _runtime._status_ref_transition(status_ref, 'repairing', details={'repairStage': 'starting'})
        repair_kwargs = dict(job_kwargs)
        repair_kwargs.pop('maintenance_scan_job_id', None)
        repair_kwargs['cancellation_requested'] = _runtime._repair_shutdown_event.is_set

        def begin_publication() -> bool:
            with metadata['publication_lock']:
                if _runtime._repair_shutdown_event.is_set():
                    return False
                metadata['publication_started'] = True
                return True
        return _runtime._perform_repair(**repair_kwargs, status_ref=status_ref, publication_guard=begin_publication)
    finally:
        _runtime._shared_capacity.release(capacity_token)

def _publish_repair_status(future: _runtime.Future, metadata: dict) -> None:
    """Publish a repair's terminal dashboard state without draining its future."""
    status_lock = metadata.get('status_lock')
    if status_lock is not None:
        with status_lock:
            if metadata.get('status_published'):
                return
            metadata['status_published'] = True
    try:
        result = future.result()
    except _runtime.CancelledError:
        _runtime._complete_repair_status(metadata, 'deferred', reason='repair persisted for restart during shutdown')
        _runtime._scan_child_finished(metadata.get('maintenance_scan_job_id'), 'deferred')
        return
    except Exception:
        durable_job_id = metadata.get('durable_job_id')
        if durable_job_id is not None and (not metadata.get('shutdown_classification')):
            try:
                _runtime._get_validation_state().transition_repair_job(durable_job_id, 'failed', error_code='worker_exception', expected_states=('queued', 'active'))
            except _runtime.StateStoreError:
                pass
        _runtime._complete_repair_status(metadata, 'failed', reason='repair worker failed')
        _runtime._scan_child_finished(metadata.get('maintenance_scan_job_id'), 'failed')
        return
    durable_job_id = metadata.get('durable_job_id')
    if durable_job_id is not None:
        try:
            if result.action == 'repair-deferred':
                _runtime._get_validation_state().transition_repair_job(durable_job_id, 'persisted_for_restart', shutdown_classification='deferred', expected_states=('queued', 'active'))
            else:
                _runtime._get_validation_state().transition_repair_job(durable_job_id, 'completed' if result.action in ('repaired', 'quarantined', 'deleted') else 'failed', error_code=None if result.action in ('repaired', 'quarantined', 'deleted') else result.action, expected_states=('queued', 'active'))
        except _runtime.StateStoreError as exc:
            print(f'{_runtime.YELLOW}[REPAIR] Durable completion update failed: {exc}{_runtime.RESET}')
    if result.action in ('repaired', 'quarantined', 'deleted'):
        series_key = metadata.get('series_key')
        series_title = metadata.get('series_title')
        if series_key and series_title:
            try:
                _runtime._get_validation_state().record_circuit_outcome(series_key=series_key, series_title=series_title, success=result.action == 'repaired', reason=None if result.action == 'repaired' else f'invalid subtitle {result.action}', threshold=_runtime.CIRCUIT_FAILURE_THRESHOLD, open_cycles=_runtime.CIRCUIT_OPEN_CYCLES, config_fingerprint=_runtime._CIRCUIT_CONFIG_FINGERPRINT, trial_owner=metadata.get('trial_owner'), trial_job_id=metadata.get('trial_job_id'), trial_plan_id=metadata.get('trial_plan_id'), lease_generation=metadata.get('trial_generation'))
                _runtime._refresh_status_diagnostics()
            except _runtime.StateStoreError as exc:
                print(f'{_runtime.YELLOW}[CIRCUIT] Could not record repair outcome: {exc}{_runtime.RESET}')
    if result.action == 'repaired':
        source_path = metadata.get('source_path')
        source_language = metadata.get('source_lang')
        if source_path and source_language:
            _runtime._record_successful_source_readiness(source_path, source_language, result.target_path, result.target_lang)
        result.retry_plan_id = metadata.get('retry_plan_id')
        result.expected_source_hash = metadata.get('expected_source_hash')
        _runtime._resolve_retry_success(
            result.retry_plan_id, result.expected_source_hash,
            outcome='accepted_after_donor_recovery' if result.donor_source_attempt is not None else 'accepted_after_retry',
        )
        _runtime._complete_repair_status(metadata, 'repaired', repaired=True, details={'attempts': result.attempts})
    elif result.action in ('quarantined', 'deleted'):
        _runtime._schedule_validation_retry(report=result.report, action=result.action, source_path=metadata.get('source_path') or '', source_lang=metadata.get('source_lang') or '', target_path=result.target_path, target_lang=result.target_lang, item_type=result.item_type, item_id=result.item_id, title=result.title, series_key=metadata.get('series_key'), series_title=metadata.get('series_title'))
        _runtime._complete_repair_status(metadata, result.action, reason=result.action, details={'attempts': result.attempts})
    elif result.action == 'repair-deferred':
        _runtime._schedule_validation_retry(report=result.report, action=result.action, source_path=metadata.get('source_path') or '', source_lang=metadata.get('source_lang') or '', target_path=result.target_path, target_lang=result.target_lang, item_type=result.item_type, item_id=result.item_id, title=result.title, series_key=metadata.get('series_key'), series_title=metadata.get('series_title'))
        _runtime._complete_repair_status(metadata, 'deferred', reason='repair deferred', details={'attempts': result.attempts})
    else:
        _runtime._complete_repair_status(metadata, 'failed', reason=f'repair {result.action}', details={'attempts': result.attempts})
    _runtime._scan_child_finished(metadata.get('maintenance_scan_job_id'), 'repaired' if result.action == 'repaired' else result.action if result.action in ('quarantined', 'deleted') else 'failed')

def _queue_repair(repair_key: tuple, job_kwargs: dict, report, label: str, target_lang: str) -> str:
    with _runtime._pending_repairs_lock:
        if not _runtime._repair_futures.reserve(repair_key):
            print(f"[REPAIR] Duplicate repair suppressed for {label} '{target_lang}'")
            return 'repair-duplicate'
        cue_count = len(getattr(report, 'repairable_cue_indexes', []) or [])
        repair_timing = _runtime._timing_estimate('repair', job_kwargs.get('source_lang'), target_lang)
        initial_details = {'totalRepairableCues': cue_count, 'completedCues': 0, 'successfulCues': 0, 'unresolvedCues': 0, 'rejectedAttempts': 0, 'progress': 0, 'secondsPerCue': round(repair_timing['secondsPerCue'], 4), 'timingSampleCount': repair_timing['sampleCount'], 'timingScope': repair_timing['scope'], 'estimatedSeconds': round(cue_count * repair_timing['secondsPerCue'] * _runtime.REPAIR_TIMEOUT_MULTIPLIER, 1), 'etaSeconds': round(cue_count * repair_timing['secondsPerCue'] * _runtime.REPAIR_TIMEOUT_MULTIPLIER, 1), 'lane': 'repair', 'attempts': 0, 'maxAttempts': _runtime.CLEANUP_MAX_REPAIR_ATTEMPTS, 'repairStage': 'queued'}
        status_ref = _runtime._status_create_repair_ref(job_kwargs, label, target_lang, initial_details)
        if not _runtime._repair_capacity.acquire(blocking=False):
            _runtime._repair_futures.release_reservation(repair_key)
            print(f"[REPAIR] Queue full; deferred {label} '{target_lang}' to the next scan")
            _runtime._status_ref_complete(status_ref, 'deferred', reason='repair queue full')
            return 'repair-deferred'
        durable_job_id = None
        state = _runtime._get_validation_state()
        if hasattr(state, 'enqueue_repair_job'):
            try:
                durable_key = _runtime.hashlib.sha256(repr((job_kwargs.get('item_type'), job_kwargs.get('item_id'), target_lang, job_kwargs.get('expected_source_hash'), job_kwargs.get('expected_target_hash'), tuple(getattr(report, 'repairable_cue_indexes', []) or []))).encode('utf-8')).hexdigest()
                durable_job_id = _runtime._repair_futures.persist(dedupe_key=durable_key, item_type=job_kwargs.get('item_type'), item_id=job_kwargs.get('item_id'), target_language=target_lang, source_path=job_kwargs.get('source_path'), target_path=job_kwargs.get('target_path'), source_hash=job_kwargs.get('expected_source_hash'), target_hash=job_kwargs.get('expected_target_hash'), cue_indexes=getattr(report, 'repairable_cue_indexes', []) or [], payload={'origin': job_kwargs.get('origin'), 'sourceLanguage': job_kwargs.get('source_lang'), 'title': job_kwargs.get('title'), 'seriesKey': job_kwargs.get('series_key'), 'seriesTitle': job_kwargs.get('series_title'), 'retryPlanId': job_kwargs.get('retry_plan_id'), 'trialOwner': job_kwargs.get('trial_owner'), 'trialJobId': job_kwargs.get('trial_job_id'), 'trialPlanId': job_kwargs.get('trial_plan_id'), 'trialGeneration': job_kwargs.get('trial_generation')})
            except _runtime.StateStoreError as exc:
                _runtime._repair_futures.release_reservation(repair_key)
                _runtime._repair_capacity.release()
                _runtime._status_ref_complete(status_ref, 'deferred', reason='repair persistence unavailable')
                print(f'{_runtime.YELLOW}[REPAIR] Could not persist repair before submit: {exc}{_runtime.RESET}')
                return 'repair-deferred'
        metadata = {'key': repair_key, 'report': report, 'target_path': job_kwargs.get('target_path'), 'item_type': job_kwargs.get('item_type'), 'item_id': job_kwargs.get('item_id'), 'target_lang': job_kwargs.get('target_lang'), 'source_lang': job_kwargs.get('source_lang'), 'source_path': job_kwargs.get('source_path'), 'series_key': job_kwargs.get('series_key'), 'series_title': job_kwargs.get('series_title'), 'retry_plan_id': job_kwargs.get('retry_plan_id'), 'expected_source_hash': job_kwargs.get('expected_source_hash'), 'trial_owner': job_kwargs.get('trial_owner'), 'trial_job_id': job_kwargs.get('trial_job_id'), 'trial_plan_id': job_kwargs.get('trial_plan_id'), 'trial_generation': job_kwargs.get('trial_generation'), 'queued_monotonic': _runtime.time.monotonic(), 'status_ref': status_ref, 'maintenance_scan_job_id': job_kwargs.get('maintenance_scan_job_id'), 'status_lock': _runtime.threading.Lock(), 'status_published': False, 'durable_job_id': durable_job_id, 'publication_lock': _runtime.threading.Lock(), 'publication_started': False}
        capacity_token = _runtime._shared_capacity.reserve_repair()
        try:
            future = _runtime._get_repair_executor().submit(_runtime._run_repair_with_capacity, capacity_token, job_kwargs, metadata)
        except Exception:
            _runtime._repair_futures.release_reservation(repair_key)
            _runtime._shared_capacity.release(capacity_token)
            _runtime._repair_capacity.release()
            _runtime._status_ref_complete(status_ref, 'failed', reason='repair worker submission failed')
            raise
        _runtime._repair_futures.register(future, metadata)
        _runtime._scan_child_queued(metadata.get('maintenance_scan_job_id'))
    future.add_done_callback(lambda completed, repair_metadata=metadata: _runtime._publish_repair_status(completed, repair_metadata))
    for index in report.repairable_cue_indexes:
        print(f"[REPAIR] Queued {label} '{target_lang}' cue position {index + 1}")
    return 'repair-queued'

def _completed_repair_futures(futures: list[_runtime.Future]):
    """Yield completions while allowing a signal to interrupt phase drainage."""
    yield from _runtime._repair_futures.completed(futures, stop_requested=lambda: _runtime.shutdown_requested or _runtime._shutdown_controller.is_requested())

def _drain_pending_repairs(stats: dict) -> list[_runtime.RepairJobResult]:
    stats.setdefault('completed', 0)
    stats.setdefault('failed', 0)
    stats.setdefault('translations', [])
    stats.setdefault('episode_activity', False)
    stats.setdefault('movie_activity', False)
    with _runtime._pending_repairs_lock:
        futures = list(_runtime._pending_repairs)
    results: list[_runtime.RepairJobResult] = []
    for future in _runtime._completed_repair_futures(futures):
        metadata = _runtime._repair_futures.take(future)
        _runtime._repair_capacity.release()
        try:
            result = future.result()
        except Exception as exc:
            print(f'{_runtime.RED}[ERROR] Repair worker failed: {exc}{_runtime.RESET}')
            stats['cleanup_repair_failures'] = stats.get('cleanup_repair_failures', 0) + 1
            _runtime._publish_repair_status(future, metadata)
            continue
        results.append(result)
        if result.action == 'repaired':
            elapsed = max(0.001, _runtime.time.monotonic() - float(metadata.get('queued_monotonic', _runtime.time.monotonic())))
            try:
                _runtime._get_validation_state().record_timing_sample(kind='repair', source_language=metadata.get('source_lang'), target_language=result.target_lang, cue_count=max(1, len(getattr(metadata.get('report'), 'repairable_cue_indexes', []) or [])), elapsed_seconds=elapsed, outcome='accepted', attempts=max(1, result.attempts))
            except _runtime.StateStoreError as exc:
                print(f'{_runtime.YELLOW}[TIMING] Could not persist repair sample: {exc}{_runtime.RESET}')
        _runtime._publish_repair_status(future, metadata)
        stats['cleanup_repair_attempts'] = stats.get('cleanup_repair_attempts', 0) + result.attempts
        stats['cleanup_second_attempts'] = stats.get('cleanup_second_attempts', 0) + result.second_attempts
        _runtime._record_cleanup_stats(stats, result.action, result.report)
        if result.action == 'repaired':
            stats['completed'] += 1
            stats['translations'].append(f'{result.title}: repaired {result.target_lang}')
            if result.item_type:
                _runtime._mark_activity(stats, result.item_type)
            else:
                stats['episode_activity'] = True
                stats['movie_activity'] = True
        elif result.action in ('quarantined', 'deleted'):
            stats['failed'] += 1
            stats['cleaned'] = stats.get('cleaned', 0) + 1
            if result.item_type:
                _runtime._mark_activity(stats, result.item_type)
            else:
                stats['episode_activity'] = True
                stats['movie_activity'] = True
        elif result.action == 'repair-deferred':
            stats['cleanup_repair_deferred'] = stats.get('cleanup_repair_deferred', 0) + 1
    return results

def _shutdown_repair_executor() -> None:
    with _runtime._repair_executor_lock:
        executor = _runtime._repair_executor
        _runtime._repair_executor = None
    if executor is not None:
        _runtime._repair_shutdown_event.set()
        print(f'[REPAIR] Draining active repair worker(s) for up to {_runtime.REPAIR_SHUTDOWN_GRACE_SECONDS}s')
        pending_metadata = _runtime._repair_futures.snapshot()
        futures = [future for future, _metadata in pending_metadata]
        for future, metadata in pending_metadata:
            if future.running() or future.done():
                continue
            metadata['shutdown_classification'] = 'cancelled_before_start'
            durable_job_id = metadata.get('durable_job_id')
            if durable_job_id is not None:
                try:
                    _runtime._get_validation_state().transition_repair_job(durable_job_id, 'persisted_for_restart', shutdown_classification='cancelled_before_start', expected_states=('queued',))
                except _runtime.StateStoreError:
                    pass
            future.cancel()
        timeout = _runtime._shutdown_controller.remaining() if _runtime._shutdown_controller.is_requested() else max(0, _runtime.REPAIR_SHUTDOWN_GRACE_SECONDS)
        deadline = _runtime.time.monotonic() + timeout
        _done, pending = _runtime.wait(futures, timeout=max(0.0, deadline - _runtime.time.monotonic()))
        for future, metadata in pending_metadata:
            if future not in pending:
                continue
            if future.done():
                continue
            with metadata['publication_lock']:
                publication_started = bool(metadata.get('publication_started'))
            classification = 'publishing_interrupted' if publication_started else 'interrupted' if future.running() else 'cancelled_before_start'
            metadata['shutdown_classification'] = classification
            durable_job_id = metadata.get('durable_job_id')
            if durable_job_id is not None:
                try:
                    _runtime._get_validation_state().transition_repair_job(durable_job_id, 'active' if publication_started else 'persisted_for_restart', shutdown_classification=classification, expected_states=('active',) if publication_started else ('queued', 'active'))
                except _runtime.StateStoreError:
                    pass
            if not publication_started:
                future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)

def _regeneration_delay_cycles(attempts: int) -> int:
    return min(_runtime.REGENERATION_MAX_DELAY_CYCLES, max(1, round(_runtime.REGENERATION_INITIAL_DELAY_CYCLES * _runtime.REGENERATION_BACKOFF_MULTIPLIER ** max(0, int(attempts)))))

def _schedule_validation_retry(*, report, action: str, source_path: str, source_lang: str, target_path: str, target_lang: str, item_type: str | None, item_id: int | None, title: str, series_key: str | None, series_title: str | None) -> dict | None:
    if item_type not in ('episodes', 'movies') or item_id is None:
        return None
    if report is None or not hasattr(report, 'valid'):
        return None
    from .foundation import classify_validation_failure
    failure_class = classify_validation_failure(report)
    source_hash = _runtime._file_hash_or_none(source_path)
    if source_hash is None:
        failure_class = 'source_problem'
        source_hash = f'unavailable:{item_type}:{item_id}'
    target_hash = _runtime._file_hash_or_none(target_path)
    archived_attempt = None
    try:
        state_store = _runtime._get_validation_state()
        existing_plan = state_store.active_retry_plan(item_type, item_id, target_lang) if hasattr(state_store, 'active_retry_plan') else None
    except _runtime.StateStoreError:
        return None
    if target_hash is None and item_type in ('episodes', 'movies'):
        try:
            archived = state_store.quarantine_attempts(item_type, item_id, target_lang)
            if archived:
                archived_attempt = archived[0]
                target_hash = archived_attempt['targetHash']
        except _runtime.StateStoreError:
            pass
    if failure_class == 'cue_repairable' and action == 'repair-deferred':
        state = 'repair_retry_queued'
        eligible_cycle = _runtime._completed_cycle
    elif failure_class == 'source_problem':
        state = 'source_blocked'
        eligible_cycle = _runtime._completed_cycle
    elif action in ('quarantined', 'deleted'):
        attempts = int((existing_plan or {}).get('attemptCount', 0))
        if _runtime.REGENERATION_MAX_ATTEMPTS > 0 and attempts >= _runtime.REGENERATION_MAX_ATTEMPTS:
            state = 'retry_exhausted'
            eligible_cycle = _runtime._completed_cycle
        else:
            state = 'regeneration_waiting'
            delay = _runtime._regeneration_delay_cycles(attempts)
            eligible_cycle = _runtime._completed_cycle + max(1, delay)
        failure_class = 'whole_file'
    else:
        return None
    try:
        if not hasattr(state_store, 'schedule_retry_plan'):
            return None
        plan, repeated = state_store.schedule_retry_plan(item_type=item_type, item_id=item_id, target_language=target_lang, source_hash=source_hash, source_path=source_path, source_language=source_lang, target_path=target_path, series_key=series_key, series_title=series_title, media_title=title, source_cue_count=_runtime._count_srt_cues(source_path), failure_class=failure_class, rules=(issue.rule for issue in getattr(report, 'issues', [])), state=state, failed_output_hash=target_hash, artifact_path=archived_attempt.get('artifactPath') if archived_attempt else None, report_path=archived_attempt.get('reportPath') if archived_attempt else None, eligible_completed_cycle=eligible_cycle, reason=getattr(report, 'summary', lambda: action)())
        if getattr(report, 'manual_review', False):
            plan = state_store.reschedule_retry_no_progress(plan['id'], completed_cycle=_runtime._completed_cycle, deferral_class='manual_review', reason='no materially new recovery strategy remains', delay_cycles=1) or plan
        print(f"[RETRY] {('Observed unchanged' if repeated else 'Scheduled')} {title} '{target_lang}': state={plan['state']} eligible_cycle={plan['eligibleCompletedCycle']} attempt={plan['attemptCount']}/{_runtime.REGENERATION_MAX_ATTEMPTS or 'unlimited'}")
        _runtime._refresh_status_diagnostics()
        return plan
    except _runtime.StateStoreError as exc:
        print(f'{_runtime.YELLOW}[RETRY] Could not persist retry plan: {exc}{_runtime.RESET}')
        return None

def _resolve_retry_success(plan_id: int | None, expected_source_hash: str | None, *, outcome: str='accepted_after_retry') -> None:
    if plan_id is None or not expected_source_hash:
        return
    try:
        state = _runtime._get_validation_state()
        if not hasattr(state, 'resolve_retry_plan'):
            return
        resolved = state.resolve_retry_plan(plan_id, expected_source_hash, outcome=outcome)
        if resolved:
            print(f"[RETRY] Accepted retry plan {plan_id}")
            _runtime._refresh_status_diagnostics()
    except _runtime.StateStoreError as exc:
        print(f'{_runtime.YELLOW}[RETRY] Could not resolve retry plan: {exc}{_runtime.RESET}')

def _validate_translated_file(source_path: str, target_path: str, source_lang: str, target_lang: str, item_id: int | None, title: str='', dry_run: bool=False, *, defer_repair: bool=False, item_type: str | None=None, media_duration: float | None=None, origin: str | None=None, provenance_source_hash: str | None=None, series_key: str | None=None, series_title: str | None=None, maintenance_scan_job_id: str | None=None, retry_plan_id: int | None=None, trial_owner: str | None=None, trial_job_id: int | None=None, trial_plan_id: int | None=None, trial_generation: int | None=None) -> tuple[str, object]:
    validation_identity = {
        'title': title or ('Movie' if item_type == 'movies' else 'Episode' if item_type == 'episodes' else 'Media item'),
        'episodeCode': _runtime.episode_identity_from_path(target_path),
        'itemType': item_type,
        'itemId': item_id,
        'targetLanguage': target_lang,
    }
    if target_lang not in _runtime.CLEANUP_LANGUAGES:
        from .foundation import validate_srt_structure
        report = validate_srt_structure(target_path)
        completeness = _runtime._evaluate_completeness(target_path, media_duration)
        _runtime._add_completeness_issue(report, completeness)
        if report.valid:
            if not _runtime._record_validation_result(target_path, _runtime._file_hash_or_none(source_path), _runtime._file_hash_or_none(target_path), 'valid', report, origin=origin, completeness=completeness.to_dict() if completeness is not None else None, **validation_identity):
                return ('repair-deferred', report)
            return ('valid', report)
        label = title or _runtime.os.path.basename(target_path)
        print(f"{_runtime.YELLOW}[CLEANUP] Invalid translation {label} '{target_lang}': {report.summary()}{_runtime.RESET}")
        action = _runtime._apply_cleanup_action(target_path, source_path, target_lang, report, lingarr_outcome='not attempted: file-level issue is not cue-repairable', completeness=completeness, origin=origin, item_type=item_type, item_id=item_id, dry_run=dry_run)
        if action in ('quarantined', 'deleted') and item_id is not None:
            _runtime._clear_submission(item_id, target_lang, item_type)
            _runtime._clear_submission_for_path(target_path, target_lang)
            print(f"[CLEANUP] Cleared submission cooldown for {label} '{target_lang}'; retry suppressed for the remainder of this cycle")
        _runtime._schedule_validation_retry(report=report, action=action, source_path=source_path, source_lang=source_lang, target_path=target_path, target_lang=target_lang, item_type=item_type, item_id=item_id, title=label, series_key=series_key, series_title=series_title)
        return (action, report)
    from .foundation import recover_subtitle_pair, target_language_for_code, validate_subtitle_pair
    from .library import validate_subtitle_without_source
    target_language = target_language_for_code(target_lang)
    detector = _runtime._get_cleanup_detector()
    if target_language is None or detector is None:
        return ('valid', None)
    source_hash = _runtime._file_hash_or_none(source_path)
    expected_target_hash = _runtime._file_hash_or_none(target_path)
    target_suffix = _runtime._target_suffix(target_path, target_lang)
    target_identity = _runtime._target_identity_from_sidecar(target_path, target_lang)
    target_variant = target_suffix[1] if target_suffix is not None else None
    recorded = _runtime._get_validation_state().matching_record(target_path, expected_target_hash, target_identity=target_identity, target_language=target_lang, target_variant=target_variant) if expected_target_hash is not None else None
    recorded_origin = recorded.get('origin') if recorded is not None else None
    recorded_source_aligned = bool(recorded_origin == 'lingarr' and source_hash is not None and (recorded.get('sourceHash') is not None) and (recorded.get('sourceHash') == source_hash) and (not recorded.get('sourceLanguage') or recorded.get('sourceLanguage') == source_lang) and (not recorded.get('sourcePath') or _runtime.os.path.normcase(_runtime.os.path.abspath(recorded['sourcePath'])) == _runtime.os.path.normcase(_runtime.os.path.abspath(source_path)) or _runtime._is_variant_aware_adjacent_source(source_path, source_lang, target_path, target_lang)))
    explicit_source_aligned = bool(origin == 'lingarr' and provenance_source_hash is not None and (provenance_source_hash == source_hash) and recorded_source_aligned)
    if recorded_origin == 'lingarr' and (not recorded_source_aligned):
        print(f'{_runtime.YELLOW}[CLEANUP] Lingarr provenance source changed for {_runtime.os.path.basename(target_path)}; using conservative target-only validation{_runtime.RESET}')
    if origin == 'lingarr' and (not explicit_source_aligned):
        print(f'{_runtime.YELLOW}[CLEANUP] Unverified Lingarr provenance for {_runtime.os.path.basename(target_path)}; using conservative target-only validation{_runtime.RESET}')
    source_aligned = recorded_source_aligned
    effective_origin = 'lingarr' if source_aligned else origin if origin != 'lingarr' else None
    if source_aligned:
        report = validate_subtitle_pair(_runtime.Path(source_path), _runtime.Path(target_path), detector, target_language, target_lang=target_lang, **_runtime._validation_kwargs())
    else:
        report = validate_subtitle_without_source(_runtime.Path(target_path), detector, target_language, target_lang=target_lang, **_runtime._validation_kwargs())
    completeness = _runtime._evaluate_completeness(target_path, media_duration)
    _runtime._add_completeness_issue(report, completeness)
    if not source_aligned and _runtime._source_less_line_only_warning(report):
        print(f'{_runtime.YELLOW}[CLEANUP] Retained {_runtime.os.path.basename(target_path)} with source-less line-count warning: {report.summary()}{_runtime.RESET}')
        if not _runtime._record_validation_result(target_path, source_hash, expected_target_hash, 'valid_with_warnings', report, origin=effective_origin, warningRules=['excessive_lines'], completeness=completeness.to_dict() if completeness is not None else None, **validation_identity):
            return ('repair-deferred', report)
        return ('valid-warning', report)
    if report.valid:
        if _runtime.CLEANUP_FORMAT_REPAIR_ENABLED and source_aligned:
            recovery = recover_subtitle_pair(source_path, target_path)
            if recovery.safe and recovery.changed and (recovery.raw is not None):
                candidate = _runtime._write_recovery_candidate(target_path, recovery.raw, same_directory=False)
                try:
                    normalized_report = validate_subtitle_pair(_runtime.Path(source_path), candidate, detector, target_language, target_lang=target_lang, **_runtime._validation_kwargs())
                    _runtime._add_completeness_issue(normalized_report, completeness)
                finally:
                    try:
                        candidate.unlink()
                    except OSError:
                        pass
                if not normalized_report.valid:
                    print(f'{_runtime.YELLOW}[FORMAT] Normalized candidate rejected for {_runtime.os.path.basename(target_path)}: {normalized_report.summary()}{_runtime.RESET}')
                    recovery = None
                if recovery is None:
                    print(f'[CLEANUP] OK {_runtime.os.path.basename(target_path)} (original retained)')
                    if not _runtime._record_validation_result(target_path, source_hash, expected_target_hash, 'valid', report, origin=effective_origin, completeness=completeness.to_dict() if completeness is not None else None, **validation_identity):
                        return ('repair-deferred', report)
                    return ('valid', report)
                if dry_run:
                    print(f'[FORMAT] DRYRUN: would normalize {target_path}')
                    return ('dry-run', report)
                try:
                    with _runtime._target_repair_lock(target_path):
                        temp = _runtime._write_recovery_candidate(target_path, recovery.raw)
                        replaced = _runtime._replace_managed_file_if_current(temp, target_path, source_path=source_path, expected_source_hash=source_hash, expected_target_hash=expected_target_hash, source_language=source_lang, target_language=target_lang, origin=effective_origin, operation='format_repair', parent_artifact_id=recorded.get('artifactId') if recorded else None)
                except (OSError, _runtime.StateStoreError) as exc:
                    print(f'{_runtime.RED}[ERROR] Could not normalize {target_path}: {exc}{_runtime.RESET}')
                    return ('action-failed', report)
                if not replaced:
                    print(f'{_runtime.YELLOW}[FORMAT] Deferred {target_path}: source or target changed during normalization{_runtime.RESET}')
                    return ('repair-deferred', report)
                print(f"{_runtime.GREEN}[FORMAT] Normalized {_runtime.os.path.basename(target_path)} without AI: {', '.join(recovery.fixes) or 'canonicalized'}{_runtime.RESET}")
                if not _runtime._record_validation_result(target_path, source_hash, _runtime._file_hash_or_none(target_path), 'valid', normalized_report, origin=effective_origin, formatFixes=recovery.fixes, formatRecoveredCues=recovery.recovered_cues, completeness=completeness.to_dict() if completeness is not None else None, sourcePath=source_path, sourceLanguage=source_lang, operation='format_repair', parentArtifactId=recorded.get('artifactId') if recorded else None, **validation_identity):
                    return ('repair-deferred', normalized_report)
                return ('formatted', normalized_report)
        mode = 'source-aware' if source_aligned else 'independent target'
        print(f'[CLEANUP] OK {_runtime.os.path.basename(target_path)} ({mode} validation passed)')
        if not _runtime._record_validation_result(target_path, source_hash, expected_target_hash, 'valid', report, origin=effective_origin, completeness=completeness.to_dict() if completeness is not None else None, **validation_identity):
            return ('repair-deferred', report)
        return ('valid', report)
    label = title or _runtime.os.path.basename(target_path)
    print(f"{_runtime.YELLOW}[CLEANUP] Invalid translation {label} '{target_lang}': {report.summary()}{_runtime.RESET}")
    quarantine_identity = _runtime._quarantine_identity(target_lang, target_path=target_path)
    historical_event = _runtime._get_validation_state().quarantine_event(quarantine_identity, target_hash=expected_target_hash) if quarantine_identity is not None else None
    repeat_invalid_hash = bool(historical_event and expected_target_hash and (historical_event.get('targetHash') == expected_target_hash))
    if repeat_invalid_hash:
        setattr(report, 'ai_repair_suppressed', True)
        print(f'[CLEANUP] Known invalid hash reappeared for {label}; skipping duplicate AI repair')
    recovery_raw = None
    format_fixes: list[str] = []
    format_recovered_cues: list[int] = []
    if _runtime.CLEANUP_FORMAT_REPAIR_ENABLED and source_aligned:
        recovery = recover_subtitle_pair(source_path, target_path)
        if recovery.safe and recovery.changed and (recovery.raw is not None):
            candidate = _runtime._write_recovery_candidate(target_path, recovery.raw, same_directory=False)
            try:
                recovered_report = validate_subtitle_pair(_runtime.Path(source_path), candidate, detector, target_language, target_lang=target_lang, **_runtime._validation_kwargs())
                _runtime._add_completeness_issue(recovered_report, completeness)
            finally:
                try:
                    candidate.unlink()
                except OSError:
                    pass
            format_fixes = recovery.fixes
            format_recovered_cues = recovery.recovered_cues
            print(f"[FORMAT] Source-anchored recovery prepared for {label} '{target_lang}': {', '.join(format_fixes) or 'canonicalized'}")
            if recovered_report.valid:
                if dry_run:
                    print(f'[FORMAT] DRYRUN: would atomically repair {target_path}')
                    return ('dry-run', report)
                try:
                    with _runtime._target_repair_lock(target_path):
                        temp = _runtime._write_recovery_candidate(target_path, recovery.raw)
                        replaced = _runtime._replace_managed_file_if_current(temp, target_path, source_path=source_path, expected_source_hash=source_hash, expected_target_hash=expected_target_hash, source_language=source_lang, target_language=target_lang, origin=effective_origin, operation='format_repair', parent_artifact_id=recorded.get('artifactId') if recorded else None)
                except (OSError, _runtime.StateStoreError) as exc:
                    print(f'{_runtime.RED}[ERROR] Could not repair {target_path}: {exc}{_runtime.RESET}')
                    return ('action-failed', report)
                if not replaced:
                    print(f'{_runtime.YELLOW}[FORMAT] Deferred {target_path}: source or target changed during format repair{_runtime.RESET}')
                    return ('repair-deferred', report)
                print(f"{_runtime.GREEN}[FORMAT] Repaired and validated {label} '{target_lang}' without AI{_runtime.RESET}")
                if not _runtime._record_validation_result(target_path, source_hash, _runtime._file_hash_or_none(target_path), 'valid', recovered_report, origin=effective_origin, formatFixes=format_fixes, formatRecoveredCues=format_recovered_cues, completeness=completeness.to_dict() if completeness is not None else None, sourcePath=source_path, sourceLanguage=source_lang, operation='format_repair', parentArtifactId=recorded.get('artifactId') if recorded else None, **validation_identity):
                    return ('repair-deferred', recovered_report)
                return ('formatted', recovered_report)
            report = recovered_report
            recovery_raw = recovery.raw
        elif not recovery.safe:
            _runtime.dbg(f'Format recovery unsafe for {label}: {recovery.reason}')
    if source_aligned and _runtime.CLEANUP_REPAIR_ENABLED and report.repairable_cue_indexes and (not dry_run) and (not repeat_invalid_hash):
        job_kwargs = {'source_path': source_path, 'target_path': target_path, 'source_lang': source_lang, 'target_lang': target_lang, 'item_id': item_id, 'title': label, 'item_type': item_type, 'initial_report': report, 'expected_target_hash': expected_target_hash, 'expected_source_hash': source_hash, 'recovery_raw': recovery_raw, 'format_fixes': format_fixes, 'format_recovered_cues': format_recovered_cues, 'completeness': completeness, 'origin': effective_origin, 'series_key': series_key, 'series_title': series_title, 'maintenance_scan_job_id': maintenance_scan_job_id, 'retry_plan_id': retry_plan_id, 'trial_owner': trial_owner, 'trial_job_id': trial_job_id, 'trial_plan_id': trial_plan_id, 'trial_generation': trial_generation}
        if defer_repair:
            repair_key = (_runtime.os.path.normcase(_runtime.os.path.abspath(target_path)), source_hash, expected_target_hash, target_lang, tuple(report.repairable_cue_indexes))
            queued_action = _runtime._queue_repair(repair_key, job_kwargs, report, label, target_lang)
            if queued_action == 'repair-deferred':
                _runtime._schedule_validation_retry(report=report, action=queued_action, source_path=source_path, source_lang=source_lang, target_path=target_path, target_lang=target_lang, item_type=item_type, item_id=item_id, title=label, series_key=series_key, series_title=series_title)
            return (queued_action, report)
        synchronous_kwargs = dict(job_kwargs)
        synchronous_kwargs.pop('maintenance_scan_job_id', None)
        for coordination_key in ('trial_owner', 'trial_job_id', 'trial_plan_id', 'trial_generation'):
            synchronous_kwargs.pop(coordination_key, None)
        result = _runtime._perform_repair(**synchronous_kwargs)
        return (result.action, result.report)
    action = _runtime._apply_cleanup_action(target_path, source_path, target_lang, report, format_fixes=format_fixes, format_recovered_cues=format_recovered_cues, completeness=completeness, origin=effective_origin, item_type=item_type, item_id=item_id, dry_run=dry_run)
    if action in ('quarantined', 'deleted') and item_id is not None:
        _runtime._clear_submission(item_id, target_lang, item_type)
        _runtime._clear_submission_for_path(target_path, target_lang)
        print(f"[CLEANUP] Cleared submission cooldown for {label} '{target_lang}'; retry suppressed for the remainder of this cycle")
    _runtime._schedule_validation_retry(report=report, action=action, source_path=source_path, source_lang=source_lang, target_path=target_path, target_lang=target_lang, item_type=item_type, item_id=item_id, title=label, series_key=series_key, series_title=series_title)
    return (action, report)
EXPORTS = {
    name: globals()[name] for name in (
        'SidecarClassification', '_sub_priority', '_target_suffix',
        '_target_identity_from_sidecar', '_submission_identity',
        '_find_target_sidecars', '_find_existing_target',
        '_snapshot_target_sidecars', '_discover_completed_target', '_truthy',
        '_sidecar_tokens', '_explicit_non_full_sidecar', '_classify_sidecar',
        '_find_sidecar_video', '_quarantine_identity',
        '_cycle_quarantine_suppression', '_resolve_quarantine_history',
        '_probe_media_duration', '_completeness_kwargs',
        '_evaluate_completeness', '_add_completeness_issue',
        '_count_dialogue_lines', '_count_srt_cues', '_timing_estimate',
        '_estimate_timeout', '_derive_target_path', '_validation_kwargs',
        '_source_less_line_only_warning', '_file_hash_or_none',
        '_media_identity_for_video', '_record_successful_source_readiness',
        '_record_validation_result', '_publish_validation_observations',
        '_record_pending_lingarr_output', '_find_submission_for_target',
        '_submission_matches_source', '_is_variant_aware_adjacent_source',
        '_record_quarantine_event', '_apply_cleanup_action',
        '_target_repair_lock', '_write_recovery_candidate',
        '_normalize_managed_output', '_replace_managed_file',
        '_replace_managed_file_if_current', '_perform_repair',
        '_get_repair_executor', '_run_repair_with_capacity',
        '_publish_repair_status', '_queue_repair',
        '_completed_repair_futures', '_drain_pending_repairs',
        '_shutdown_repair_executor', '_regeneration_delay_cycles',
        '_schedule_validation_retry', '_resolve_retry_success',
        '_validate_translated_file',
    )
}

