from __future__ import annotations

from .foundation import *

def repair_subtitle_file(
    source_path: Path | str,
    target_path: Path | str,
    detector,
    target_language: Language,
    translator: Callable[[str, list[str], list[str]], Optional[str]],
    *,
    target_lang: str,
    max_attempts: int = 5,
    context_lines: int = 5,
    attempt_logger: Optional[Callable[[dict], None]] = None,
    progress_callback: Optional[Callable[[dict], None]] = None,
    donor_attempts: Optional[list[dict]] = None,
    cue_recoveries: Optional[list[dict]] = None,
    donor_similarity: float = 0.95,
    donor_timestamp_tolerance_ms: int = 500,
    donor_event_logger: Optional[Callable[[dict], None]] = None,
    artifact_access=None,
    exhausted_strategies: Optional[dict[str, set[str]]] = None,
    cancellation_requested: Optional[Callable[[], bool]] = None,
    **validation_kwargs,
) -> RepairResult:
    """Repair only invalid aligned cues, then atomically replace the target after full validation."""
    initial_report = validate_subtitle_pair(
        source_path,
        target_path,
        detector,
        target_language,
        target_lang=target_lang,
        **validation_kwargs,
    )
    if initial_report.valid:
        return RepairResult(True, [], initial_report, "already valid", 0, [])

    cue_indexes = initial_report.repairable_cue_indexes
    if not cue_indexes:
        return RepairResult(False, [], initial_report, "validation failure is not safely repairable", 0, [])

    source_raw = read_text_best_effort(Path(source_path))
    target_raw = read_text_best_effort(Path(target_path))
    if source_raw is None or target_raw is None:
        return RepairResult(False, [], initial_report, "source or target became unreadable", 0, [])
    source_cues, source_errors = parse_srt_cues(source_raw)
    target_cues, target_errors = parse_srt_cues(target_raw)
    if source_errors or target_errors:
        return RepairResult(False, [], initial_report, "source or target structure changed", 0, [])

    candidate_cues = [SubtitleCue(cue.number, cue.timestamp, list(cue.lines)) for cue in target_cues]
    repaired_numbers: list[int] = []
    unresolved_cues: list[tuple[int, str]] = []
    attempt_count = 0
    attempt_history: list[dict] = []
    donor_history: list[dict] = []
    completed_cues = 0
    successful_cues = 0
    rejected_attempts = 0
    exhausted_cue_numbers: set[int] = set()

    def interrupted_result() -> RepairResult:
        """Return without rendering or publishing in-memory partial progress."""
        return RepairResult(
            False,
            repaired_numbers,
            initial_report,
            "repair cancelled during shutdown",
            attempt_count,
            attempt_history,
            donor_history,
            None,
            [source_cues[index].number for index in cue_indexes],
            False,
            True,
        )

    def publish_progress(**values) -> None:
        if progress_callback is None:
            return
        payload = {
            "totalRepairableCues": len(cue_indexes),
            "completedCues": completed_cues,
            "successfulCues": successful_cues,
            "unresolvedCues": len(unresolved_cues),
            "rejectedAttempts": rejected_attempts,
            "progress": round(
                completed_cues * 100 / max(1, len(cue_indexes)), 1
            ),
            **values,
        }
        try:
            progress_callback(payload)
        except Exception:
            # Status reporting is best effort and must never abort file repair.
            pass

    publish_progress(stage="repairing")

    cue_validation_keys = {
        "max_cue_lines",
        "max_cue_chars",
        "max_expansion_ratio",
        "max_expansion_chars",
        "max_source_similarity",
        "max_cyrillic_ratio",
        "max_cjk_ratio",
        "max_latin_ratio",
    }
    cue_validation_kwargs = {
        key: value for key, value in validation_kwargs.items() if key in cue_validation_keys
    }

    for cue_ordinal, cue_index in enumerate(cue_indexes, start=1):
        if cancellation_requested is not None and cancellation_requested():
            return interrupted_result()
        source_cue = source_cues[cue_index]
        before = [cue.text for cue in source_cues[max(0, cue_index - context_lines):cue_index]]
        after = [cue.text for cue in source_cues[cue_index + 1:cue_index + 1 + context_lines]]
        accepted = False
        last_reason = "translator returned no usable text"
        failed_fingerprints: set[str] = set()

        provider_attempted = False
        for attempt in range(max(1, max_attempts)):
            if cancellation_requested is not None and cancellation_requested():
                return interrupted_result()
            attempt_before = before if attempt == 0 else []
            attempt_after = after if attempt == 0 else []
            from autotranslate.subtitles.recovery import (
                normalized_output_fingerprint,
                strategy_for_attempt,
            )

            strategy = strategy_for_attempt(
                attempt, bool(attempt_before or attempt_after)
            )
            source_signature = cue_source_signature(source_cue)
            if strategy in (exhausted_strategies or {}).get(
                source_signature["sourceHash"], set()
            ):
                last_reason = f"{strategy} strategy exhausted by equivalent failures"
                continue
            attempt_count += 1
            provider_attempted = True
            attempt_record = {
                "cueNumber": source_cue.number,
                "attempt": attempt + 1,
                "maxAttempts": max(1, max_attempts),
                "contextBefore": len(attempt_before),
                "contextAfter": len(attempt_after),
                "withoutContext": not attempt_before and not attempt_after,
                "strategy": strategy,
                "sourceCueHash": source_signature["sourceHash"],
                "startedAt": datetime.now(timezone.utc).isoformat(),
            }
            started = time.monotonic()
            if attempt_logger is not None:
                attempt_logger({**attempt_record, "event": "sending"})
            publish_progress(
                stage="calling_lingarr",
                currentCueNumber=source_cue.number,
                currentCuePosition=cue_index + 1,
                currentCueOrdinal=cue_ordinal,
                currentAttempt=attempt + 1,
                maxAttempts=max(1, max_attempts),
                contextEnabled=bool(attempt_before or attempt_after),
            )
            try:
                translated_result = translator(source_cue.text, attempt_before, attempt_after)
            except Exception as exc:
                last_reason = f"translator error: {exc}"
                attempt_record.update({
                    "durationSeconds": round(time.monotonic() - started, 3),
                    "outcome": "translator_error",
                    "reason": str(exc),
                })
                attempt_history.append(attempt_record)
                if attempt_logger is not None:
                    attempt_logger({**attempt_record, "event": "failed"})
                publish_progress(
                    stage="calling_lingarr",
                    currentCueNumber=source_cue.number,
                    currentCuePosition=cue_index + 1,
                    currentCueOrdinal=cue_ordinal,
                    currentAttempt=attempt + 1,
                    maxAttempts=max(1, max_attempts),
                    contextEnabled=bool(attempt_before or attempt_after),
                    lastRequestDurationSeconds=attempt_record["durationSeconds"],
                )
                continue
            response_metadata: dict = {}
            if (
                isinstance(translated_result, tuple)
                and len(translated_result) == 2
                and isinstance(translated_result[1], dict)
            ):
                translated, response_metadata = translated_result
                attempt_record.update(response_metadata)
            else:
                translated = translated_result
            if cancellation_requested is not None and cancellation_requested():
                return interrupted_result()
            publish_progress(
                stage="validating_candidate",
                currentCueNumber=source_cue.number,
                currentCuePosition=cue_index + 1,
                currentCueOrdinal=cue_ordinal,
                currentAttempt=attempt + 1,
                maxAttempts=max(1, max_attempts),
                contextEnabled=bool(attempt_before or attempt_after),
                lastHttpStatus=response_metadata.get("httpStatus"),
                lastRequestDurationSeconds=round(time.monotonic() - started, 3),
            )
            if translated is None or not translated.strip():
                attempt_record.update({
                    "durationSeconds": round(time.monotonic() - started, 3),
                    "outcome": "empty_response",
                })
                attempt_history.append(attempt_record)
                if attempt_logger is not None:
                    attempt_logger({**attempt_record, "event": "failed"})
                continue

            output_fingerprint = normalized_output_fingerprint(translated)
            attempt_record["outputFingerprint"] = output_fingerprint

            replacement = SubtitleCue(
                candidate_cues[cue_index].number,
                candidate_cues[cue_index].timestamp,
                [line.strip() for line in translated.strip().splitlines() if line.strip()],
            )
            replacement_issues = validate_cue_pair(
                source_cue,
                replacement,
                cue_index=cue_index,
                target_lang=target_lang,
                **cue_validation_kwargs,
            )
            if replacement_issues:
                rejected_attempts += 1
                last_reason = ValidationReport(replacement_issues).summary()
                attempt_record.update({
                    "durationSeconds": round(time.monotonic() - started, 3),
                    "outcome": "rejected",
                    "validationRules": sorted({issue.rule for issue in replacement_issues}),
                })
                attempt_history.append(attempt_record)
                if attempt_logger is not None:
                    attempt_logger({**attempt_record, "event": "rejected"})
                publish_progress(
                    stage="validating_candidate",
                    currentCueNumber=source_cue.number,
                    currentCuePosition=cue_index + 1,
                    currentCueOrdinal=cue_ordinal,
                    currentAttempt=attempt + 1,
                    maxAttempts=max(1, max_attempts),
                    contextEnabled=bool(attempt_before or attempt_after),
                    lastHttpStatus=response_metadata.get("httpStatus"),
                    lastRequestDurationSeconds=attempt_record["durationSeconds"],
                )
                failed_fingerprints.add(output_fingerprint)
                continue

            candidate_cues[cue_index] = replacement
            repaired_numbers.append(replacement.number)
            attempt_record.update({
                "durationSeconds": round(time.monotonic() - started, 3),
                "outcome": "accepted",
                "validationRules": [],
            })
            attempt_history.append(attempt_record)
            if attempt_logger is not None:
                attempt_logger({**attempt_record, "event": "accepted"})
            accepted = True
            break

        if not accepted and cue_recoveries:
            signature = cue_source_signature(source_cue)
            for recovery in cue_recoveries:
                if recovery.get("sourceCueHash") != signature.get("sourceHash"):
                    continue
                text = recovery.get("targetText")
                if not isinstance(text, str) or not text.strip():
                    continue
                replacement = SubtitleCue(
                    source_cue.number,
                    source_cue.timestamp,
                    [line.strip() for line in text.splitlines() if line.strip()],
                )
                issues = validate_cue_pair(
                    source_cue,
                    replacement,
                    cue_index=cue_index,
                    target_lang=target_lang,
                    **cue_validation_kwargs,
                )
                if issues:
                    if donor_event_logger is not None:
                        donor_event_logger({
                            "cueNumber": source_cue.number,
                            "reasonCode": "current_validation_failed",
                        })
                    continue
                candidate_cues[cue_index] = replacement
                repaired_numbers.append(replacement.number)
                donor_record = {
                    "cueNumber": source_cue.number,
                    "sourceRecoveryId": recovery.get("id"),
                    "sourceAttemptId": recovery.get("sourceAttemptId"),
                }
                donor_history.append(donor_record)
                if donor_event_logger is not None:
                    donor_event_logger({**donor_record, "reasonCode": "selected"})
                accepted = True
                break

        if (
            not accepted
            and not donor_attempts
            and not cue_recoveries
            and donor_event_logger is not None
        ):
            donor_event_logger({
                "cueNumber": source_cue.number,
                "reasonCode": "no_indexed_attempts",
            })
        if not accepted and donor_attempts:
            current_signature = cue_source_signature(source_cue)
            ranked: list[tuple[tuple, SubtitleCue, dict]] = []
            for donor_attempt in donor_attempts:
                artifact_path = donor_attempt.get("artifactPath")
                signatures = donor_attempt.get("cueSignatures") or []
                access = (
                    artifact_access.hold(artifact_path)
                    if artifact_access is not None and artifact_path
                    else nullcontext()
                )
                with access:
                    donor_raw = (
                        read_text_best_effort(Path(artifact_path))
                        if artifact_path else None
                    )
                    try:
                        artifact_hash = (
                            file_sha256(Path(artifact_path)) if artifact_path else None
                        )
                    except OSError:
                        artifact_hash = None
                if (
                    donor_raw is None
                    or not donor_attempt.get("targetHash")
                    or artifact_hash != donor_attempt.get("targetHash")
                ):
                    if donor_event_logger is not None:
                        donor_event_logger({
                            "cueNumber": source_cue.number,
                            "donorAttemptId": donor_attempt.get("id"),
                            "reasonCode": "artifact_unavailable" if donor_raw is None else "hash_mismatch",
                        })
                    continue
                donor_cues, donor_errors = parse_srt_cues(donor_raw)
                if donor_errors or len(donor_cues) != len(signatures):
                    if donor_event_logger is not None:
                        donor_event_logger({
                            "cueNumber": source_cue.number,
                            "donorAttemptId": donor_attempt.get("id"),
                            "reasonCode": "source_signature_mismatch",
                        })
                    continue
                for donor_cue, signature in zip(donor_cues, signatures):
                    current_tokens = current_signature["tokenHashes"]
                    donor_tokens = signature.get("tokenHashes") or []
                    similarity = SequenceMatcher(
                        None, current_tokens, donor_tokens
                    ).ratio()
                    current_ms = current_signature.get("startMs")
                    donor_ms = signature.get("startMs")
                    if current_ms is None or donor_ms is None:
                        continue
                    timestamp_distance = abs(current_ms - int(donor_ms))
                    if (
                        similarity < donor_similarity
                        or timestamp_distance > donor_timestamp_tolerance_ms
                    ):
                        if donor_event_logger is not None:
                            donor_event_logger({
                                "cueNumber": source_cue.number,
                                "donorAttemptId": donor_attempt.get("id"),
                                "reasonCode": (
                                    "source_signature_mismatch"
                                    if similarity < donor_similarity
                                    else "timestamp_mismatch"
                                ),
                            })
                        continue
                    replacement = SubtitleCue(
                        source_cue.number,
                        source_cue.timestamp,
                        list(donor_cue.lines),
                    )
                    issues = validate_cue_pair(
                        source_cue,
                        replacement,
                        cue_index=cue_index,
                        target_lang=target_lang,
                        **cue_validation_kwargs,
                    )
                    if issues:
                        if donor_event_logger is not None:
                            donor_event_logger({
                                "cueNumber": source_cue.number,
                                "donorAttemptId": donor_attempt.get("id"),
                                "reasonCode": "current_validation_failed",
                            })
                        continue
                    exact = current_signature["sourceHash"] == signature.get("sourceHash")
                    detected_language, detected_confidence = detect_language(
                        detector, replacement.text
                    )
                    if (
                        detected_language is not None
                        and detected_language != target_language
                        and detected_confidence >= 0.70
                    ):
                        if donor_event_logger is not None:
                            donor_event_logger({
                                "cueNumber": source_cue.number,
                                "donorAttemptId": donor_attempt.get("id"),
                                "reasonCode": "language_mismatch",
                            })
                        continue
                    language_confidence = (
                        detected_confidence
                        if detected_language in (None, target_language) else 0.0
                    )
                    warnings = int(
                        detected_language is not None
                        and detected_language != target_language
                    )
                    expansion = abs(len(replacement.text) - len(source_cue.text))
                    rank = (
                        0 if exact else 1,
                        -similarity,
                        timestamp_distance,
                        warnings,
                        -language_confidence,
                        expansion,
                        -float(donor_attempt.get("createdAt") or 0),
                        -int(donor_attempt.get("attemptNumber") or 0),
                    )
                    ranked.append((rank, replacement, donor_attempt))
            if ranked:
                _, replacement, donor_attempt = min(ranked, key=lambda entry: entry[0])
                candidate_cues[cue_index] = replacement
                repaired_numbers.append(replacement.number)
                donor_record = {
                    "cueNumber": source_cue.number,
                    "sourceAttempt": donor_attempt.get("attemptNumber"),
                    "sourceAttemptId": donor_attempt.get("id"),
                }
                donor_history.append(donor_record)
                if donor_event_logger is not None:
                    donor_event_logger({
                        **donor_record,
                        "donorAttemptId": donor_attempt.get("id"),
                        "reasonCode": "selected",
                    })
                if attempt_logger is not None:
                    attempt_logger({**donor_record, "event": "donor_accepted"})
                accepted = True

        if not accepted:
            if not provider_attempted:
                exhausted_cue_numbers.add(source_cue.number)
            unresolved_cues.append((source_cue.number, last_reason))
        else:
            successful_cues += 1
        completed_cues += 1
        publish_progress(
            stage="repairing",
            currentCueNumber=source_cue.number,
            currentCuePosition=cue_index + 1,
            currentCueOrdinal=cue_ordinal,
            currentAttempt=min(max(1, max_attempts), attempt + 1),
            maxAttempts=max(1, max_attempts),
            contextEnabled=False,
        )

    if unresolved_cues:
        newline = "\r\n" if "\r\n" in target_raw else "\n"
        partial_raw = render_srt_cues(candidate_cues, newline=newline)
        partial_name: Optional[str] = None
        partial_report = initial_report
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="", suffix=".srt",
                dir=Path(target_path).parent, delete=False,
            ) as partial_file:
                partial_file.write(partial_raw)
                partial_name = partial_file.name
            partial_report = validate_subtitle_pair(
                source_path, partial_name, detector, target_language,
                target_lang=target_lang, **validation_kwargs,
            )
        finally:
            if partial_name is not None:
                try:
                    os.unlink(partial_name)
                except OSError:
                    pass
        unresolved_summary = "; ".join(
            f"cue {cue_number}: {reason}"
            for cue_number, reason in unresolved_cues
        )
        return RepairResult(
            False,
            repaired_numbers,
            partial_report,
            (
                f"{len(unresolved_cues)} cue(s) could not be repaired: "
                f"{unresolved_summary}"
            ),
            attempt_count,
            attempt_history,
            donor_history,
            partial_raw,
            [cue_number for cue_number, _reason in unresolved_cues],
            bool(unresolved_cues) and all(
                cue_number in exhausted_cue_numbers
                for cue_number, _reason in unresolved_cues
            ),
        )

    newline = "\r\n" if "\r\n" in target_raw else "\n"
    target = Path(target_path)
    temp_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
            delete=False,
        ) as temp_file:
            temp_file.write(render_srt_cues(candidate_cues, newline=newline))
            temp_name = temp_file.name

        publish_progress(stage="repair_validating")
        final_report = validate_subtitle_pair(
            source_path,
            temp_name,
            detector,
            target_language,
            target_lang=target_lang,
            **validation_kwargs,
        )
        if not final_report.valid:
            return RepairResult(
                False, repaired_numbers, final_report, final_report.summary(),
                attempt_count, attempt_history, donor_history
            )

        normalize_managed_file(temp_name)
        os.replace(temp_name, target)
        temp_name = None
        return RepairResult(
            True, repaired_numbers, final_report, "repaired and validated",
            attempt_count, attempt_history, donor_history
        )
    except OSError as exc:
        return RepairResult(
            False,
            repaired_numbers,
            initial_report,
            f"could not write repaired file: {exc}",
            attempt_count,
            attempt_history,
            donor_history,
        )
    finally:
        if temp_name is not None:
            try:
                Path(temp_name).unlink()
            except OSError:
                pass


