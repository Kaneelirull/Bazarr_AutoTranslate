from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .foundation import (
    SubtitleCue,
    file_sha256,
    parse_srt_cues,
    read_text_best_effort,
    render_srt_cues,
)


RECEIPT_SCHEMA_VERSION = 1
DEDUPLICATION_ALGORITHM = "adjacent-exact-v1"
EXTRACTED_MARKER = ".extracted."
_TIMESTAMP_RE = re.compile(
    r"^\s*(?P<start>\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*"
    r"(?P<end>\d{2}:\d{2}:\d{2}[,.]\d{3})(?P<tail>.*)$"
)


@dataclass(frozen=True)
class ExtractedSource:
    path: Path
    receipt_path: Path
    canonical_language: str
    language: str
    variant: str
    track_id: int | None
    default: bool
    hearing_impaired: bool
    track_index: int
    current_hash: str


@dataclass(frozen=True)
class DeduplicationResult:
    raw: str
    changed: bool
    input_cues: int
    output_cues: int
    duplicate_groups: int
    removed_cues: int
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreparedSource:
    path: Path
    source_hash: str | None
    changed: bool
    duplicate_groups: int
    removed_cues: int
    error: str | None = None


def extracted_receipt_path(video_path: str | Path) -> Path:
    video = Path(video_path)
    return video.with_name(f"{video.stem}.extracted.json")


def is_extracted_sidecar(path: str | Path, video_path: str | Path | None = None) -> bool:
    candidate = Path(path)
    folded = candidate.name.casefold()
    if EXTRACTED_MARKER not in folded or candidate.suffix.casefold() != ".srt":
        return False
    if video_path is None:
        return True
    video = Path(video_path)
    return folded.startswith(f"{video.stem}.extracted.".casefold())


def _alias_map(language_aliases: Mapping[str, Iterable[str]]) -> dict[str, str]:
    return {
        str(alias).casefold(): str(language).casefold()
        for language, aliases in language_aliases.items()
        for alias in aliases
    }


def _variant_priority(variant: str) -> int:
    if not variant:
        return 0
    token = variant.removeprefix(".").casefold()
    if token in ("hi", "sdh"):
        return 1
    if token.isdigit():
        return 1 + int(token)
    return 99


def _read_receipt(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("receipt root must be an object")
    return value


def _accepted_track_hashes(track: dict) -> set[str]:
    values = {track.get("sha256")}
    preparation = track.get("preparation")
    if isinstance(preparation, dict):
        values.update((preparation.get("inputSha256"), preparation.get("outputSha256")))
    return {str(value) for value in values if isinstance(value, str) and value}


def discover_extracted_sources(
    video_path: str | Path,
    language_aliases: Mapping[str, Iterable[str]],
    language_order: Iterable[str],
) -> tuple[list[ExtractedSource], str | None]:
    video = Path(video_path)
    receipt_path = extracted_receipt_path(video)
    if not receipt_path.is_file():
        return ([], None)
    try:
        receipt = _read_receipt(receipt_path)
        if receipt.get("schemaVersion") != RECEIPT_SCHEMA_VERSION:
            raise ValueError("unsupported receipt schema")
        video_metadata = receipt.get("video")
        if not isinstance(video_metadata, dict):
            raise ValueError("missing video metadata")
        stat = video.stat()
        if str(video_metadata.get("name") or "").casefold() != video.name.casefold():
            raise ValueError("video name does not match")
        if int(video_metadata.get("size", -1)) != stat.st_size:
            raise ValueError("video size does not match")
        if int(video_metadata.get("mtimeNs", -1)) != stat.st_mtime_ns:
            raise ValueError("video modification time does not match")
        tracks = receipt.get("tracks")
        if not isinstance(tracks, list):
            raise ValueError("tracks must be a list")
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return ([], str(exc))

    aliases = _alias_map(language_aliases)
    order = {str(language).casefold(): index for index, language in enumerate(language_order)}
    pattern = re.compile(
        rf"^{re.escape(video.stem)}\.extracted\."
        r"(?P<language>[a-z0-9-]+)(?P<variant>\.(?:hi|sdh|\d+))?\.srt$",
        re.IGNORECASE,
    )
    candidates: list[ExtractedSource] = []
    for index, track in enumerate(tracks):
        if not isinstance(track, dict) or bool(track.get("forced")):
            continue
        relative_name = track.get("path")
        if not isinstance(relative_name, str) or Path(relative_name).name != relative_name:
            continue
        match = pattern.match(relative_name)
        if match is None:
            continue
        path = video.parent / relative_name
        try:
            if not path.is_file():
                continue
            current_hash = file_sha256(path)
        except OSError:
            continue
        if current_hash not in _accepted_track_hashes(track):
            continue
        filename_language = match.group("language").casefold()
        metadata_language = str(track.get("language") or filename_language).casefold()
        filename_canonical = aliases.get(filename_language)
        metadata_canonical = aliases.get(metadata_language)
        if (
            filename_canonical is not None
            and metadata_canonical is not None
            and filename_canonical != metadata_canonical
        ):
            continue
        canonical = filename_canonical or metadata_canonical
        if canonical is None or canonical not in order:
            continue
        variant = (match.group("variant") or "").casefold()
        metadata_variant = str(track.get("variant") or "").casefold()
        if metadata_variant.removeprefix(".") != variant.removeprefix("."):
            continue
        candidates.append(
            ExtractedSource(
                path=path,
                receipt_path=receipt_path,
                canonical_language=canonical,
                language=metadata_language,
                variant=variant,
                track_id=int(track["trackId"]) if isinstance(track.get("trackId"), int) else None,
                default=bool(track.get("default")),
                hearing_impaired=bool(track.get("hearingImpaired")),
                track_index=index,
                current_hash=current_hash,
            )
        )
    candidates.sort(
        key=lambda candidate: (
            order[candidate.canonical_language],
            candidate.hearing_impaired,
            not candidate.default,
            _variant_priority(candidate.variant),
            candidate.track_id if candidate.track_id is not None else 2**31,
            candidate.path.name.casefold(),
        )
    )
    return (candidates, None)


def _timestamp_ms(value: str) -> int:
    hours, minutes, seconds, milliseconds = (
        int(part) for part in re.split(r"[:,.]", value)
    )
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + milliseconds


def _timestamp_parts(cue: SubtitleCue) -> tuple[str, str, str, int, int] | None:
    match = _TIMESTAMP_RE.match(cue.timestamp)
    if match is None:
        return None
    start = match.group("start")
    end = match.group("end")
    return (start, end, match.group("tail"), _timestamp_ms(start), _timestamp_ms(end))


def deduplicate_rolling_cues(raw: str, *, max_gap_ms: int = 100) -> DeduplicationResult:
    cues, errors = parse_srt_cues(raw)
    if errors:
        return DeduplicationResult(raw, False, len(cues), len(cues), 0, 0, tuple(errors))
    merged: list[SubtitleCue] = []
    duplicate_groups = 0
    removed_cues = 0
    index = 0
    while index < len(cues):
        first = cues[index]
        first_parts = _timestamp_parts(first)
        normalized_lines = tuple(line.rstrip() for line in first.lines)
        final_index = index
        final_parts = first_parts
        while first_parts is not None and final_parts is not None and final_index + 1 < len(cues):
            following = cues[final_index + 1]
            following_parts = _timestamp_parts(following)
            if following_parts is None:
                break
            same_payload = tuple(line.rstrip() for line in following.lines) == normalized_lines
            chronological = following_parts[3] >= first_parts[3] and following_parts[4] >= final_parts[4]
            connected = following_parts[3] <= final_parts[4] + max_gap_ms
            if not (same_payload and chronological and connected):
                break
            final_index += 1
            final_parts = following_parts
        if final_index > index and first_parts is not None and final_parts is not None:
            duplicate_groups += 1
            removed_cues += final_index - index
            timestamp = f"{first_parts[0]} --> {final_parts[1]}{final_parts[2]}"
        else:
            timestamp = first.timestamp
        merged.append(SubtitleCue(len(merged) + 1, timestamp, list(normalized_lines)))
        index = final_index + 1
    if not removed_cues:
        return DeduplicationResult(raw, False, len(cues), len(cues), 0, 0)
    newline = "\r\n" if "\r\n" in raw else "\n"
    rendered = render_srt_cues(merged, newline=newline)
    if raw.startswith("\ufeff"):
        rendered = "\ufeff" + rendered
    return DeduplicationResult(
        rendered,
        True,
        len(cues),
        len(merged),
        duplicate_groups,
        removed_cues,
    )


def _atomic_write_text(path: Path, raw: str, normalize: Callable[[Path], None]) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        normalize(temporary)
        os.replace(temporary, path)
    except Exception:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
        raise


def _atomic_write_json(path: Path, value: dict, normalize: Callable[[Path], None]) -> None:
    _atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        normalize,
    )


def _preparation_metadata(result: DeduplicationResult, input_hash: str, output_hash: str, state: str) -> dict:
    return {
        "algorithm": DEDUPLICATION_ALGORITHM,
        "state": state,
        "inputSha256": input_hash,
        "outputSha256": output_hash,
        "inputCueCount": result.input_cues,
        "outputCueCount": result.output_cues,
        "duplicateGroups": result.duplicate_groups,
        "removedCues": result.removed_cues,
        "preparedAt": datetime.now(timezone.utc).isoformat(),
    }


def prepare_extracted_source(
    candidate: ExtractedSource,
    *,
    artifact_access,
    normalize: Callable[[Path], None],
) -> PreparedSource:
    with artifact_access.hold(candidate.path, candidate.receipt_path):
        try:
            receipt = _read_receipt(candidate.receipt_path)
            tracks = receipt.get("tracks")
            if not isinstance(tracks, list) or candidate.track_index >= len(tracks):
                raise ValueError("receipt track is no longer present")
            track = tracks[candidate.track_index]
            if not isinstance(track, dict) or track.get("path") != candidate.path.name:
                raise ValueError("receipt track changed")
            current_hash = file_sha256(candidate.path)
            preparation = track.get("preparation")
            if isinstance(preparation, dict) and preparation.get("algorithm") == DEDUPLICATION_ALGORITHM:
                output_hash = preparation.get("outputSha256")
                if current_hash == output_hash:
                    if preparation.get("state") != "complete" or track.get("sha256") != output_hash:
                        completed = copy.deepcopy(receipt)
                        completed_track = completed["tracks"][candidate.track_index]
                        completed_track["sha256"] = output_hash
                        completed_track["size"] = candidate.path.stat().st_size
                        completed_track["preparation"] = {**preparation, "state": "complete"}
                        _atomic_write_json(candidate.receipt_path, completed, normalize)
                    return PreparedSource(
                        candidate.path,
                        current_hash,
                        bool(preparation.get("removedCues")),
                        int(preparation.get("duplicateGroups") or 0),
                        int(preparation.get("removedCues") or 0),
                    )
                if preparation.get("state") != "pending" or current_hash != preparation.get("inputSha256"):
                    raise ValueError("prepared source hash does not match receipt")
            elif current_hash != track.get("sha256"):
                raise ValueError("source hash does not match receipt")

            raw = read_text_best_effort(candidate.path)
            if raw is None:
                raise OSError("source is unreadable")
            result = deduplicate_rolling_cues(raw)
            if result.errors:
                return PreparedSource(
                    candidate.path,
                    current_hash,
                    False,
                    0,
                    0,
                    f"invalid SRT structure: {result.errors[0]}",
                )
            output_hash = (
                hashlib.sha256(result.raw.encode("utf-8")).hexdigest()
                if result.changed else current_hash
            )
            pending = copy.deepcopy(receipt)
            pending["tracks"][candidate.track_index]["preparation"] = _preparation_metadata(
                result, current_hash, output_hash, "pending"
            )
            _atomic_write_json(candidate.receipt_path, pending, normalize)
            if result.changed:
                _atomic_write_text(candidate.path, result.raw, normalize)
            completed = copy.deepcopy(pending)
            completed_track = completed["tracks"][candidate.track_index]
            completed_track["sha256"] = output_hash
            completed_track["size"] = candidate.path.stat().st_size
            completed_track["preparation"]["state"] = "complete"
            _atomic_write_json(candidate.receipt_path, completed, normalize)
            return PreparedSource(
                candidate.path,
                output_hash,
                result.changed,
                result.duplicate_groups,
                result.removed_cues,
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return PreparedSource(candidate.path, None, False, 0, 0, str(exc))


def canonical_target_path(
    video_path: str | Path,
    target_language: str,
    variant: str = "",
) -> Path:
    video = Path(video_path)
    normalized_variant = variant if not variant or variant.startswith(".") else f".{variant}"
    return video.with_name(f"{video.stem}.{target_language}{normalized_variant}.srt")


__all__ = [
    "DEDUPLICATION_ALGORITHM",
    "DeduplicationResult",
    "ExtractedSource",
    "PreparedSource",
    "canonical_target_path",
    "deduplicate_rolling_cues",
    "discover_extracted_sources",
    "extracted_receipt_path",
    "is_extracted_sidecar",
    "prepare_extracted_source",
]
