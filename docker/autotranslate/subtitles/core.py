#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import signal
import tempfile
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Iterable, Optional, Tuple

from lingua import Language, LanguageDetectorBuilder

MANAGED_FILE_UID = 568
MANAGED_FILE_GID = 568
MANAGED_FILE_MODE = 0o664


def normalize_managed_file(path: Path | str) -> None:
    """Apply the ownership contract for subtitle artifacts created by the service."""
    managed_path = Path(path)
    if os.name != "posix":
        return
    os.chown(managed_path, MANAGED_FILE_UID, MANAGED_FILE_GID)
    os.chmod(managed_path, MANAGED_FILE_MODE)


# Global flag for graceful shutdown (used by CLI only)
shutdown_requested = False

# code2 -> lingua Language for per-target validation
TARGET_LANGUAGE_MAP: dict[str, Language] = {
    "et": Language.ESTONIAN,
    "en": Language.ENGLISH,
    "sv": Language.SWEDISH,
    "fi": Language.FINNISH,
    "de": Language.GERMAN,
    "fr": Language.FRENCH,
    "es": Language.SPANISH,
    "ru": Language.RUSSIAN,
    "pl": Language.POLISH,
    "lv": Language.LATVIAN,
    "lt": Language.LITHUANIAN,
    "uk": Language.UKRAINIAN,
}

TARGET_CODE_ALIASES: dict[str, set[str]] = {
    "en": {"en", "eng"}, "et": {"et", "est"}, "sv": {"sv", "swe"},
    "de": {"de", "deu", "ger"}, "fr": {"fr", "fra", "fre"},
    "es": {"es", "spa"}, "nl": {"nl", "nld", "dut"},
    "no": {"no", "nor", "nob"}, "fi": {"fi", "fin"},
    "da": {"da", "dan"}, "pl": {"pl", "pol"}, "pt": {"pt", "por"},
    "ru": {"ru", "rus"}, "lv": {"lv", "lav"}, "lt": {"lt", "lit"},
    "uk": {"uk", "ukr"}, "tr": {"tr", "tur"}, "it": {"it", "ita"},
    "cs": {"cs", "ces", "cze"}, "sk": {"sk", "slk", "slo"},
    "hu": {"hu", "hun"}, "ro": {"ro", "ron", "rum"},
    "el": {"el", "ell", "gre"}, "ar": {"ar", "ara"},
    "he": {"he", "heb"}, "ja": {"ja", "jpn"}, "ko": {"ko", "kor"},
    "zh": {"zh", "zho", "chi"},
}

DETECTOR_LANGUAGES = [
    Language.ESTONIAN,
    Language.ENGLISH,
    Language.RUSSIAN,
    Language.FINNISH,
    Language.SWEDISH,
    Language.LATVIAN,
    Language.LITHUANIAN,
    Language.GERMAN,
    Language.FRENCH,
    Language.SPANISH,
    Language.POLISH,
    Language.UKRAINIAN,
]


def signal_handler(signum, frame):
    """Handle termination signals gracefully."""
    global shutdown_requested
    shutdown_requested = True
    print(f"[WARNING] Received signal {signum}. Initiating graceful shutdown...", file=sys.stderr)
    sys.stderr.flush()


# Register signal handlers only when run as CLI (not when imported)
def _register_signal_handlers() -> None:
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

# Basic SRT cleaning:
# - remove indices, timestamps, and blank lines
# - remove common HTML tags and formatting
SRT_TIMESTAMP_RE = re.compile(
    r"^\s*\d{2}:\d{2}:\d{2}[,\.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,\.]\d{3}.*$"
)
SRT_INDEX_RE = re.compile(r"^\s*\d+\s*$")
TAG_RE = re.compile(r"<[^>]+>")
BRACKET_RE = re.compile(r"[\[\]\(\)\{\}]")

# Script profiles per target language (code2)
SCRIPT_PROFILE: dict[str, str] = {
    "et": "latin", "sv": "latin", "en": "latin", "de": "latin", "fr": "latin",
    "es": "latin", "pl": "latin", "lv": "latin", "lt": "latin", "fi": "latin",
    "ru": "cyrillic", "uk": "cyrillic",
}

CYRILLIC_RE = re.compile(r"[\u0400-\u04FF\u0500-\u052F]")
CJK_RE = re.compile(r"[\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF]")
ARABIC_RE = re.compile(r"[\u0600-\u06FF]")
HEBREW_RE = re.compile(r"[\u0590-\u05FF]")
GREEK_RE = re.compile(r"[\u0370-\u03FF]")
LATIN_RE = re.compile(r"[A-Za-z\u00C0-\u024F]")

# HTTP / AI / API garbage patterns — single match triggers rejection
GARBAGE_PATTERNS: list[tuple[str, str]] = [
    (r"500\s+Server\s+Error", "HTTP 500"),
    (r"500\s+Internal\s+Server\s+Error", "HTTP 500"),
    (r"Error\s*500", "HTTP 500"),
    (r"503\s+Service\s+Unavailable", "HTTP 503"),
    (r"400\s+Bad\s+Request", "HTTP 400"),
    (r"429\s+Too\s+Many\s+Requests", "HTTP 429"),
    (r"as an ai\b", "AI refusal"),
    (r"i cannot translate", "AI refusal"),
    (r"i'm sorry", "AI refusal"),
    (r"i am unable to", "AI refusal"),
    (r'\{"error"', "JSON error"),
    (r'"errorMessage"', "JSON error"),
    (r'"stackTrace"', "JSON error"),
    (r"<!DOCTYPE", "HTML error"),
    (r"<html\b", "HTML error"),
    (r"rate limit exceeded", "API error"),
    (r"context length", "API error"),
    (r"lorem ipsum", "placeholder"),
    (r"\[TRANSLATION\]", "placeholder"),
    (r"TODO:\s*translate", "placeholder"),
    (r"\[/?(?:TARGET|CONTEXT|SOURCE|BEFORE|AFTER)\]", "prompt marker"),
    (r">{3,}|<{3,}", "prompt marker"),
]

PUNCT_RE = re.compile(r'[^\w\s]')

REPAIRABLE_CUE_RULES = {
    "prompt_marker",
    "garbage",
    "empty_target",
    "cue_too_long",
    "abnormal_expansion",
    "copied_source",
    "unexpected_script",
    "excessive_lines",
}

SOURCE_FAILURE_RULES = {
    "source_unreadable",
    "source_structure",
    "undersized_source",
}


def classify_validation_failure(report: "ValidationReport") -> str:
    """Return the scheduler-safe retry class for a validation report."""
    if report.valid:
        return "valid"
    rules = {issue.rule for issue in report.issues}
    if rules and rules <= REPAIRABLE_CUE_RULES and report.repairable_cue_indexes:
        return "cue_repairable"
    if rules & SOURCE_FAILURE_RULES:
        return "source_problem"
    return "whole_file"

VALIDATOR_VERSION = "source-aware-v4-completeness-provenance"


@dataclass
class SubtitleCue:
    number: int
    timestamp: str
    lines: list[str]

    @property
    def text(self) -> str:
        return " ".join(line.strip() for line in self.lines if line.strip()).strip()


@dataclass(frozen=True)
class ValidationIssue:
    rule: str
    detail: str
    cue_index: Optional[int] = None
    cue_number: Optional[int] = None


@dataclass
class ValidationReport:
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.issues

    @property
    def repairable_cue_indexes(self) -> list[int]:
        if any(issue.cue_index is None or issue.rule not in REPAIRABLE_CUE_RULES for issue in self.issues):
            return []
        return sorted({issue.cue_index for issue in self.issues if issue.cue_index is not None})

    def summary(self, limit: int = 5) -> str:
        if self.valid:
            return "OK"
        labels = []
        for issue in self.issues[:limit]:
            prefix = f"cue {issue.cue_number}: " if issue.cue_number is not None else ""
            labels.append(f"{prefix}{issue.detail}")
        remaining = len(self.issues) - len(labels)
        if remaining:
            labels.append(f"and {remaining} more issue(s)")
        return "; ".join(labels)

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "summary": self.summary(),
            "issues": [
                {
                    "rule": issue.rule,
                    "detail": issue.detail,
                    "cueIndex": issue.cue_index,
                    "cueNumber": issue.cue_number,
                }
                for issue in self.issues
            ],
        }


@dataclass
class RepairResult:
    success: bool
    repaired_cues: list[int]
    report: ValidationReport
    reason: str
    attempts: int = 0
    attempt_history: list[dict] = field(default_factory=list)
    donor_history: list[dict] = field(default_factory=list)
    partial_raw: Optional[str] = None
    unresolved_cues: list[int] = field(default_factory=list)
    manual_review: bool = False
    interrupted: bool = False


@dataclass
class FormatRecoveryResult:
    safe: bool
    changed: bool
    raw: Optional[str]
    fixes: list[str] = field(default_factory=list)
    recovered_cues: list[int] = field(default_factory=list)
    reason: str = ""


@dataclass(frozen=True)
class DiscoveredSubtitle:
    path: Path
    target_lang: str
    variant: str
    language_token: str = ""


@dataclass(frozen=True)
class CompletenessResult:
    evaluated: bool
    undersized: bool
    reason: str
    media_duration_seconds: float
    subtitle_bytes: int = 0
    cue_count: int = 0
    dialogue_chars: int = 0
    cues_per_minute: float = 0.0
    text_chars_per_minute: float = 0.0
    bytes_per_minute: float = 0.0
    timeline_coverage: float = 0.0
    failed_signals: tuple[str, ...] = ()
    thresholds: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "evaluated": self.evaluated,
            "undersized": self.undersized,
            "reason": self.reason,
            "mediaDurationSeconds": round(self.media_duration_seconds, 3),
            "subtitleBytes": self.subtitle_bytes,
            "cueCount": self.cue_count,
            "dialogueChars": self.dialogue_chars,
            "cuesPerMinute": round(self.cues_per_minute, 3),
            "textCharsPerMinute": round(self.text_chars_per_minute, 3),
            "bytesPerMinute": round(self.bytes_per_minute, 3),
            "timelineCoverage": round(self.timeline_coverage, 4),
            "failedSignals": list(self.failed_signals),
            "thresholds": dict(self.thresholds),
        }


def build_detector():
    """Build a reusable lingua language detector."""
    return LanguageDetectorBuilder.from_languages(*DETECTOR_LANGUAGES).build()


def target_language_for_code(code2: str) -> Optional[Language]:
    return TARGET_LANGUAGE_MAP.get(code2.lower())


def script_profile_for_code(code2: str) -> str:
    return SCRIPT_PROFILE.get(code2.lower(), "latin")


def iter_srt_files(roots: Iterable[Path], suffix: str) -> Iterable[Path]:
    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob(f"*{suffix}"):
            if p.is_file():
                yield p


def read_text_best_effort(path: Path) -> Optional[str]:
    # Try utf-8 first, then fall back to latin-1 (common for subtitle files)
    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return path.read_text(encoding=enc, errors="strict")
        except UnicodeDecodeError:
            continue
        except Exception:
            return None
    # last resort: decode with replacement
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def clean_srt_text(raw: str) -> str:
    lines = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        if SRT_INDEX_RE.match(line):
            continue
        if SRT_TIMESTAMP_RE.match(line):
            continue
        line = TAG_RE.sub(" ", line)
        line = BRACKET_RE.sub(" ", line)
        line = line.replace("\\N", " ")
        lines.append(line.strip())
    text = " ".join(lines)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def detect_language(detector, text: str) -> Tuple[Optional[Language], float]:
    try:
        lang = detector.detect_language_of(text)
        if lang is None:
            return None, 0.0
        conf = detector.compute_language_confidence(text, lang)
        return lang, float(conf)
    except Exception:
        return None, 0.0


def find_garbage_match(text: str) -> Optional[str]:
    """Return a label for the first garbage pattern matched, or None."""
    for pattern, label in GARBAGE_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return label
    return None


def _script_letter_counts(text: str) -> dict[str, int]:
    cyrillic = len(CYRILLIC_RE.findall(text))
    cjk = len(CJK_RE.findall(text))
    arabic = len(ARABIC_RE.findall(text))
    hebrew = len(HEBREW_RE.findall(text))
    greek = len(GREEK_RE.findall(text))
    latin = len(LATIN_RE.findall(text))
    total = cyrillic + cjk + arabic + hebrew + greek + latin
    return {
        "cyrillic": cyrillic,
        "cjk": cjk,
        "arabic": arabic,
        "hebrew": hebrew,
        "greek": greek,
        "latin": latin,
        "total": total,
        "non_latin": cyrillic + cjk + arabic + hebrew + greek,
    }


def check_script_profile(
    entries: list[str],
    profile: str,
    *,
    max_cyrillic_ratio: float = 0.05,
    max_cjk_ratio: float = 0.05,
    max_latin_ratio: float = 0.80,
    min_letters_for_script: int = 20,
) -> Tuple[bool, str]:
    """
    Validate dialogue text against expected script profile.
    Returns (ok, reason). ok=True means script is acceptable.
    """
    if not entries:
        return True, "no entries"

    for entry in entries:
        counts = _script_letter_counts(entry)
        if counts["total"] < 10:
            continue
        if profile == "latin" and counts["latin"] == 0 and counts["non_latin"] > 0:
            return False, "entry is 100% non-Latin script"
        if profile == "cyrillic" and counts["cyrillic"] == 0 and counts["latin"] > 0:
            return False, "entry is 100% Latin script"

    combined = " ".join(entries)
    counts = _script_letter_counts(combined)
    if counts["total"] < min_letters_for_script:
        return True, f"too few letters for script check ({counts['total']})"

    if profile == "latin":
        cyr_ratio = counts["cyrillic"] / counts["total"]
        cjk_ratio = counts["cjk"] / counts["total"]
        if cyr_ratio > max_cyrillic_ratio:
            return False, f"unexpected Cyrillic ({cyr_ratio:.1%})"
        if cjk_ratio > max_cjk_ratio:
            return False, f"unexpected CJK ({cjk_ratio:.1%})"
        return True, "script OK"

    if profile == "cyrillic":
        latin_ratio = counts["latin"] / counts["total"]
        if latin_ratio > max_latin_ratio:
            return False, f"unexpected Latin ({latin_ratio:.1%})"
        return True, "script OK"

    return True, "script OK"


def parse_srt_cues(raw: str) -> tuple[list[SubtitleCue], list[str]]:
    """Parse standard SRT blocks while retaining cue identity and line structure."""
    cues: list[SubtitleCue] = []
    errors: list[str] = []
    blocks = re.split(r"\r?\n\s*\r?\n", raw.strip()) if raw.strip() else []

    for block_index, block in enumerate(blocks, start=1):
        lines = block.splitlines()
        if len(lines) < 2:
            errors.append(f"block {block_index} has fewer than two lines")
            continue
        number_text = lines[0].strip().lstrip("\ufeff")
        if not number_text.isdigit():
            errors.append(f"block {block_index} has invalid cue number {number_text!r}")
            continue
        timestamp = lines[1].strip()
        if not SRT_TIMESTAMP_RE.match(timestamp):
            errors.append(f"cue {number_text} has invalid timestamp {timestamp!r}")
            continue
        cues.append(SubtitleCue(int(number_text), timestamp, lines[2:]))

    return cues, errors


def validate_srt_structure(path: Path | str) -> ValidationReport:
    """Return structural SRT findings without requiring a language detector."""
    report = ValidationReport()
    raw = read_text_best_effort(Path(path))
    if raw is None:
        report.issues.append(ValidationIssue("target_unreadable", "subtitle is unreadable"))
        return report
    cues, errors = parse_srt_cues(raw)
    for error in errors:
        report.issues.append(ValidationIssue("target_structure", error))
    if not errors and not cues:
        report.issues.append(ValidationIssue("target_structure", "subtitle contains no cues"))
    return report


def _timestamp_end_seconds(value: str) -> Optional[float]:
    match = re.match(
        r"^\s*\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*"
        r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})",
        value,
    )
    if not match:
        return None
    hours, minutes, seconds, milliseconds = (int(part) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000


def evaluate_subtitle_completeness(
    path: Path | str,
    media_duration_seconds: float,
    *,
    min_media_duration: float = 900,
    min_cues_per_minute: float = 1.5,
    min_text_chars_per_minute: float = 40,
    min_bytes_per_minute: float = 100,
    min_timeline_coverage: float = 0.60,
    required_signals: int = 3,
) -> CompletenessResult:
    """Evaluate whether a regular subtitle is dense enough to represent full dialogue."""
    subtitle = Path(path)
    thresholds = {
        "minMediaDurationSeconds": min_media_duration,
        "minCuesPerMinute": min_cues_per_minute,
        "minTextCharsPerMinute": min_text_chars_per_minute,
        "minBytesPerMinute": min_bytes_per_minute,
        "minTimelineCoverage": min_timeline_coverage,
        "requiredSignals": min(4, max(1, int(required_signals))),
    }
    try:
        duration = float(media_duration_seconds)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        return CompletenessResult(
            False, False, "media duration unavailable", duration, thresholds=thresholds
        )
    if duration < min_media_duration:
        return CompletenessResult(
            False,
            False,
            "media shorter than configured minimum",
            duration,
            thresholds=thresholds,
        )

    raw = read_text_best_effort(subtitle)
    if raw is None:
        return CompletenessResult(
            False, False, "subtitle is unreadable", duration, thresholds=thresholds
        )
    cues, errors = parse_srt_cues(raw)
    if errors or not cues:
        return CompletenessResult(
            False, False, "subtitle structure is invalid", duration, thresholds=thresholds
        )

    minutes = duration / 60
    dialogue_chars = sum(len(TAG_RE.sub("", cue.text).strip()) for cue in cues)
    try:
        subtitle_bytes = subtitle.stat().st_size
    except OSError:
        subtitle_bytes = len(raw.encode("utf-8"))
    last_end = max((_timestamp_end_seconds(cue.timestamp) or 0.0) for cue in cues)
    cues_per_minute = len(cues) / minutes
    text_chars_per_minute = dialogue_chars / minutes
    bytes_per_minute = subtitle_bytes / minutes
    timeline_coverage = min(1.0, max(0.0, last_end / duration))

    failed: list[str] = []
    if cues_per_minute < min_cues_per_minute:
        failed.append("cue_density")
    if text_chars_per_minute < min_text_chars_per_minute:
        failed.append("text_density")
    if bytes_per_minute < min_bytes_per_minute:
        failed.append("byte_density")
    if timeline_coverage < min_timeline_coverage:
        failed.append("timeline_coverage")
    required = thresholds["requiredSignals"]
    undersized = len(failed) >= required
    reason = (
        f"{len(failed)}/{required} completeness signals failed"
        if undersized else f"{len(failed)}/{required} completeness signals failed; accepted"
    )
    return CompletenessResult(
        True,
        undersized,
        reason,
        duration,
        subtitle_bytes,
        len(cues),
        dialogue_chars,
        cues_per_minute,
        text_chars_per_minute,
        bytes_per_minute,
        timeline_coverage,
        tuple(failed),
        thresholds,
    )


def completeness_issue(result: CompletenessResult) -> Optional[ValidationIssue]:
    if not result.evaluated or not result.undersized:
        return None
    signals = ", ".join(result.failed_signals)
    detail = (
        f"subtitle is undersized for {result.media_duration_seconds / 60:.1f} min media: "
        f"{result.cue_count} cues, {result.dialogue_chars} text chars, "
        f"{result.subtitle_bytes} bytes; failed {signals}"
    )
    return ValidationIssue("undersized_subtitle", detail)


def _canonical_timestamp(value: str) -> Optional[str]:
    match = re.match(
        r"^\s*(\d{2}:\d{2}:\d{2})[,\.](\d{3})\s*-->\s*"
        r"(\d{2}:\d{2}:\d{2})[,\.](\d{3})(?:\s+.*)?$",
        value,
    )
    if not match:
        return None
    return f"{match.group(1)},{match.group(2)} --> {match.group(3)},{match.group(4)}"


def recover_srt_structure(source_raw: str, target_raw: str) -> FormatRecoveryResult:
    """Conservatively rebuild target blocks when all source anchors still match in order."""
    source_cues, source_errors = parse_srt_cues(source_raw)
    if source_errors or not source_cues:
        return FormatRecoveryResult(False, False, None, reason="source structure is invalid")

    had_bom = target_raw.startswith("\ufeff")
    had_crlf = "\r\n" in target_raw
    lines = target_raw.lstrip("\ufeff").splitlines()
    anchors: list[tuple[int, int, str]] = []
    timestamp_fixes = 0
    for index in range(len(lines) - 1):
        number_text = lines[index].strip()
        if not number_text.isdigit():
            continue
        timestamp = _canonical_timestamp(lines[index + 1])
        if timestamp is None:
            continue
        if lines[index + 1].strip() != timestamp:
            timestamp_fixes += 1
        anchors.append((index, int(number_text), timestamp))

    if len(anchors) != len(source_cues):
        # A single damaged number or timestamp can still be mapped safely when
        # every surrounding block matches its source position exactly.
        blocks = re.split(r"\r?\n\s*\r?\n", target_raw.lstrip("\ufeff").strip())
        if len(blocks) == len(source_cues):
            rebuilt: list[SubtitleCue] = []
            damaged: list[int] = []
            for position, (block, source_cue) in enumerate(zip(blocks, source_cues)):
                block_lines = block.splitlines()
                source_timestamp = _canonical_timestamp(source_cue.timestamp)
                number_ok = bool(
                    block_lines
                    and block_lines[0].strip().isdigit()
                    and int(block_lines[0].strip()) == source_cue.number
                )
                timestamp_at_one = (
                    _canonical_timestamp(block_lines[1])
                    if len(block_lines) > 1 else None
                )
                timestamp_at_zero = (
                    _canonical_timestamp(block_lines[0])
                    if block_lines else None
                )
                malformed_timestamp_at_one = bool(
                    len(block_lines) > 1
                    and ">" in block_lines[1]
                    and re.search(r"\d{1,2}:\d{2}", block_lines[1])
                )
                if number_ok and timestamp_at_one == source_timestamp:
                    text_lines = block_lines[2:]
                elif timestamp_at_zero == source_timestamp:
                    damaged.append(position)
                    text_lines = block_lines[1:]
                elif number_ok and len(block_lines) > 2 and malformed_timestamp_at_one:
                    damaged.append(position)
                    text_lines = block_lines[2:]
                elif len(block_lines) > 2 and timestamp_at_one == source_timestamp:
                    damaged.append(position)
                    text_lines = block_lines[2:]
                else:
                    return FormatRecoveryResult(
                        False, False, None,
                        reason=f"target block {position + 1} cannot be aligned uniquely",
                    )
                if not any(line.strip() for line in text_lines):
                    return FormatRecoveryResult(
                        False, False, None,
                        reason=f"target block {position + 1} has no translated text",
                    )
                rebuilt.append(
                    SubtitleCue(source_cue.number, source_timestamp, text_lines)
                )
            if len(damaged) == 1:
                newline = "\r\n" if had_crlf else "\n"
                rendered = render_srt_cues(rebuilt, newline=newline)
                if had_bom:
                    rendered = "\ufeff" + rendered
                return FormatRecoveryResult(
                    True,
                    rendered != target_raw,
                    rendered,
                    fixes=["source_aligned_single_anchor"],
                    recovered_cues=[source_cues[damaged[0]].number],
                    reason="repaired one source-aligned structural anchor",
                )
        return FormatRecoveryResult(
            False,
            False,
            None,
            reason=f"anchor count differs ({len(source_cues)} source, {len(anchors)} target)",
        )

    first_anchor = anchors[0][0]
    if any(line.strip() for line in lines[:first_anchor]):
        return FormatRecoveryResult(False, False, None, reason="non-empty content precedes first cue")

    recovered: list[SubtitleCue] = []
    recovered_numbers: list[int] = []
    trailing_space_lines = 0
    repeated_separators = 0
    for position, ((line_index, number, timestamp), source_cue) in enumerate(zip(anchors, source_cues)):
        source_timestamp = _canonical_timestamp(source_cue.timestamp)
        if number != source_cue.number or timestamp != source_timestamp:
            return FormatRecoveryResult(
                False,
                False,
                None,
                reason=(
                    f"target anchor {number} at position {position + 1} does not match "
                    f"source cue {source_cue.number}"
                ),
            )

        next_anchor = anchors[position + 1][0] if position + 1 < len(anchors) else len(lines)
        content = lines[line_index + 2:next_anchor]
        trailing_space_lines += sum(line != line.rstrip() for line in content)
        while content and not content[0].strip():
            content.pop(0)
        trailing_blanks = 0
        while content and not content[-1].strip():
            content.pop()
            trailing_blanks += 1
        if trailing_blanks > 1:
            repeated_separators += trailing_blanks - 1
        if any(not line.strip() for line in content):
            recovered_numbers.append(number)
        cleaned = [line.rstrip() for line in content if line.strip()]
        recovered.append(SubtitleCue(number, source_cue.timestamp.strip(), cleaned))

    newline = "\r\n" if had_crlf else "\n"
    rendered = render_srt_cues(recovered, newline=newline)
    comparable_original = target_raw.lstrip("\ufeff")
    fixes: list[str] = []
    if had_bom:
        fixes.append("removed_bom")
    if timestamp_fixes:
        fixes.append(f"normalized_timestamps:{timestamp_fixes}")
    if trailing_space_lines:
        fixes.append(f"trimmed_trailing_whitespace:{trailing_space_lines}")
    if repeated_separators:
        fixes.append(f"collapsed_repeated_separators:{repeated_separators}")
    if recovered_numbers:
        fixes.append(f"folded_orphan_breaks:{len(recovered_numbers)}")
    if rendered != comparable_original:
        fixes.append("canonicalized_srt_structure")

    return FormatRecoveryResult(
        True,
        rendered != target_raw,
        rendered,
        fixes=fixes,
        recovered_cues=sorted(set(recovered_numbers)),
        reason="source anchors match",
    )


def recover_subtitle_pair(source_path: Path | str, target_path: Path | str) -> FormatRecoveryResult:
    source_raw = read_text_best_effort(Path(source_path))
    target_raw = read_text_best_effort(Path(target_path))
    if source_raw is None or target_raw is None:
        return FormatRecoveryResult(False, False, None, reason="source or target is unreadable")
    return recover_srt_structure(source_raw, target_raw)


def parse_srt_entries(raw: str) -> list[str]:
    cues, _ = parse_srt_cues(raw)
    return [cue.text for cue in cues]


def render_srt_cues(cues: list[SubtitleCue], newline: str = "\n") -> str:
    blocks = []
    for cue in cues:
        blocks.append(newline.join([str(cue.number), cue.timestamp, *cue.lines]))
    return (newline * 2).join(blocks) + newline


def _normalise_for_similarity(text: str) -> str:
    text = TAG_RE.sub(" ", text).casefold()
    text = PUNCT_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _is_invariant_dominated(source: str, target: str) -> bool:
    """Allow copied names/models without exempting ordinary title-cased prose."""
    clean_source = TAG_RE.sub(" ", source).strip()
    words = re.findall(r"[A-Za-z\u00C0-\u024F0-9-]+", clean_source)
    if len(words) < 2:
        return False
    connectors = {"and", "or", "vs", "versus"}
    content = [word for word in words if word.casefold() not in connectors]
    if not content or not all(
        word.isupper()
        or any(char.isdigit() for char in word)
        or (word[0].isupper() and not word[1:].isupper())
        for word in content
    ):
        return False
    has_model = any(any(char.isdigit() for char in word) or word.isupper() for word in content)
    has_connector = bool(
        re.search(r"[&/|]", clean_source)
        or any(word.casefold() in connectors for word in words)
    )
    punctuation_groups = [
        re.findall(r"[A-Za-z\u00C0-\u024F0-9-]+", group)
        for group in re.split(r"[,;:!]+", clean_source)
        if group.strip()
    ]
    has_short_name_groups = (
        len(punctuation_groups) >= 2
        and all(1 <= len(group) <= 2 for group in punctuation_groups)
    )
    if not (has_model or has_connector or has_short_name_groups):
        return False
    source_content = {
        word.casefold() for word in content if len(word) > 1
    }
    target_words = {
        word.casefold()
        for word in re.findall(r"[A-Za-z\u00C0-\u024F0-9-]+", TAG_RE.sub(" ", target))
    }
    return bool(source_content) and len(source_content & target_words) / len(source_content) >= 0.8


def _timestamp_start_ms(timestamp: str) -> int | None:
    match = re.match(r"\s*(\d{2}):(\d{2}):(\d{2})[,\.](\d{3})", timestamp)
    if not match:
        return None
    hours, minutes, seconds, millis = (int(value) for value in match.groups())
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def cue_source_signature(cue: SubtitleCue) -> dict:
    """Build privacy-safe matching metadata; no dialogue is persisted."""
    tokens = re.findall(r"\w+", _normalise_for_similarity(cue.text), flags=re.UNICODE)
    token_hashes = [
        hashlib.sha256(token.encode("utf-8")).hexdigest()[:16] for token in tokens
    ]
    return {
        "cueNumber": cue.number,
        "startMs": _timestamp_start_ms(cue.timestamp),
        "tokenHashes": token_hashes,
        "sourceHash": hashlib.sha256(
            "\x1f".join(token_hashes).encode("ascii")
        ).hexdigest(),
    }


def source_cue_signatures(source_path: Path | str) -> list[dict]:
    raw = read_text_best_effort(Path(source_path))
    if raw is None:
        return []
    cues, errors = parse_srt_cues(raw)
    if errors:
        return []
    return [cue_source_signature(cue) for cue in cues]


def validate_cue_pair(
    source: SubtitleCue,
    target: SubtitleCue,
    *,
    cue_index: int,
    target_lang: str,
    max_cue_lines: int = 4,
    max_cue_chars: int = 500,
    max_expansion_ratio: float = 4.0,
    max_expansion_chars: int = 300,
    max_source_similarity: float = 0.92,
    max_cyrillic_ratio: float = 0.05,
    max_cjk_ratio: float = 0.05,
    max_latin_ratio: float = 0.80,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    source_text = source.text
    target_text = target.text

    def add(rule: str, detail: str) -> None:
        issues.append(ValidationIssue(rule, detail, cue_index, target.number))

    if not target_text:
        add("empty_target", "translation is empty")
        return issues

    source_line_count = len([line for line in source.lines if line.strip()])
    target_line_count = len([line for line in target.lines if line.strip()])
    line_limit = max(source_line_count + 1, max_cue_lines)
    if target_line_count > line_limit:
        add(
            "excessive_lines",
            f"translation has {target_line_count} lines (max {line_limit} for {source_line_count}-line source)",
        )

    garbage = find_garbage_match(target_text)
    if garbage is not None:
        rule = "prompt_marker" if garbage == "prompt marker" else "garbage"
        add(rule, f"garbage pattern ({garbage})")

    if len(target_text) > max_cue_chars:
        add("cue_too_long", f"translation is {len(target_text)} characters (max {max_cue_chars})")

    expansion_limit = max(max_expansion_chars, int(len(source_text) * max_expansion_ratio))
    if source_text and len(target_text) > expansion_limit:
        ratio = len(target_text) / max(1, len(source_text))
        add("abnormal_expansion", f"translation expanded {ratio:.1f}x ({len(source_text)} -> {len(target_text)} chars)")

    source_normalised = _normalise_for_similarity(source_text)
    target_normalised = _normalise_for_similarity(target_text)
    if (
        len(source_normalised) >= 20
        and len(target_normalised) >= 20
        and not _is_invariant_dominated(source_text, target_text)
    ):
        similarity = SequenceMatcher(None, source_normalised, target_normalised).ratio()
        if similarity >= max_source_similarity:
            add("copied_source", f"translation matches source ({similarity:.0%} similar)")

    script_ok, script_reason = check_script_profile(
        [target_text],
        script_profile_for_code(target_lang),
        max_cyrillic_ratio=max_cyrillic_ratio,
        max_cjk_ratio=max_cjk_ratio,
        max_latin_ratio=max_latin_ratio,
        min_letters_for_script=10,
    )
    if not script_ok:
        add("unexpected_script", script_reason)

    return issues


def entry_unique_ratio(entries: list) -> float:
    if not entries:
        return 1.0
    normalised = [PUNCT_RE.sub('', e.lower()).strip() for e in entries]
    return len(set(normalised)) / len(normalised)


def validate_subtitle_file(
    path: Path | str,
    detector,
    target_language: Language,
    *,
    target_lang: str = "",
    min_chars: int = 200,
    min_confidence: float = 0.70,
    max_unique_ratio: float = 0.15,
    max_cyrillic_ratio: float = 0.05,
    max_cjk_ratio: float = 0.05,
    max_latin_ratio: float = 0.80,
    min_letters_for_script: int = 20,
) -> Tuple[bool, str]:
    """
    Validate a single subtitle file against the expected target language.
    Returns (is_valid, reason). Valid means keep the file; invalid means remove it.
    """
    p = Path(path)
    raw = read_text_best_effort(p)
    if raw is None:
        return False, "unreadable"

    garbage = find_garbage_match(raw)
    if garbage is not None:
        return False, f"garbage pattern ({garbage})"

    entries = parse_srt_entries(raw)

    code2 = target_lang.lower() if target_lang else None
    if not code2:
        for k, v in TARGET_LANGUAGE_MAP.items():
            if v == target_language:
                code2 = k
                break
    profile = script_profile_for_code(code2 or "et")
    script_ok, script_reason = check_script_profile(
        entries,
        profile,
        max_cyrillic_ratio=max_cyrillic_ratio,
        max_cjk_ratio=max_cjk_ratio,
        max_latin_ratio=max_latin_ratio,
        min_letters_for_script=min_letters_for_script,
    )
    if not script_ok:
        return False, script_reason

    if len(entries) >= 5:
        ratio = entry_unique_ratio(entries)
        if ratio < max_unique_ratio:
            return False, f"repetitive (unique={ratio:.3f}, {len(entries)} entries)"

    cleaned = clean_srt_text(raw)
    if len(cleaned) < min_chars:
        return True, f"too short ({len(cleaned)} chars)"

    lang, conf = detect_language(detector, cleaned)
    if lang is None:
        return True, "language unknown"

    if lang == target_language and conf >= min_confidence:
        return True, f"OK ({target_language.name} {conf:.2f})"

    return False, f"detected {lang.name} {conf:.2f}"


def validate_subtitle_pair(
    source_path: Path | str,
    target_path: Path | str,
    detector,
    target_language: Language,
    *,
    target_lang: str,
    min_chars: int = 200,
    min_confidence: float = 0.70,
    max_unique_ratio: float = 0.15,
    max_cyrillic_ratio: float = 0.05,
    max_cjk_ratio: float = 0.05,
    max_latin_ratio: float = 0.80,
    min_letters_for_script: int = 20,
    max_cue_lines: int = 4,
    max_cue_chars: int = 500,
    max_expansion_ratio: float = 4.0,
    max_expansion_chars: int = 300,
    max_source_similarity: float = 0.92,
) -> ValidationReport:
    """Validate a translated SRT against its source and return cue-level findings."""
    report = ValidationReport()
    source_raw = read_text_best_effort(Path(source_path))
    target_raw = read_text_best_effort(Path(target_path))
    if source_raw is None:
        report.issues.append(ValidationIssue("source_unreadable", "source subtitle is unreadable"))
        return report
    if target_raw is None:
        report.issues.append(ValidationIssue("target_unreadable", "target subtitle is unreadable"))
        return report

    source_cues, source_errors = parse_srt_cues(source_raw)
    target_cues, target_errors = parse_srt_cues(target_raw)
    for error in source_errors:
        report.issues.append(ValidationIssue("source_structure", error))
    for error in target_errors:
        report.issues.append(ValidationIssue("target_structure", error))
    if source_errors or target_errors:
        return report

    if len(source_cues) != len(target_cues):
        report.issues.append(ValidationIssue(
            "cue_count_mismatch",
            f"cue count differs ({len(source_cues)} source, {len(target_cues)} target)",
        ))
        return report

    for cue_index, (source, target) in enumerate(zip(source_cues, target_cues)):
        if source.number != target.number:
            report.issues.append(ValidationIssue(
                "cue_number_mismatch",
                f"source cue {source.number} aligns with target cue {target.number}",
                cue_index,
                target.number,
            ))
            continue
        if source.timestamp != target.timestamp:
            report.issues.append(ValidationIssue(
                "timestamp_mismatch",
                "timestamp differs from source",
                cue_index,
                target.number,
            ))
            continue
        report.issues.extend(validate_cue_pair(
            source,
            target,
            cue_index=cue_index,
            target_lang=target_lang,
            max_cue_lines=max_cue_lines,
            max_cue_chars=max_cue_chars,
            max_expansion_ratio=max_expansion_ratio,
            max_expansion_chars=max_expansion_chars,
            max_source_similarity=max_source_similarity,
            max_cyrillic_ratio=max_cyrillic_ratio,
            max_cjk_ratio=max_cjk_ratio,
            max_latin_ratio=max_latin_ratio,
        ))

    target_valid, target_reason = validate_subtitle_file(
        target_path,
        detector,
        target_language,
        target_lang=target_lang,
        min_chars=min_chars,
        min_confidence=min_confidence,
        max_unique_ratio=max_unique_ratio,
        max_cyrillic_ratio=max_cyrillic_ratio,
        max_cjk_ratio=max_cjk_ratio,
        max_latin_ratio=max_latin_ratio,
        min_letters_for_script=min_letters_for_script,
    )
    if not target_valid:
        garbage_already_located = target_reason.startswith("garbage pattern") and any(
            issue.rule in ("prompt_marker", "garbage") for issue in report.issues
        )
        script_already_located = any(issue.rule == "unexpected_script" for issue in report.issues)
        if not garbage_already_located and not script_already_located:
            report.issues.append(ValidationIssue("target_file_invalid", target_reason))

    return report


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


def validate_subtitle_without_source(
    target_path: Path | str,
    detector,
    target_language: Language,
    *,
    target_lang: str,
    max_cue_lines: int = 4,
    max_cue_chars: int = 500,
    min_chars: int = 200,
    min_confidence: float = 0.70,
    max_unique_ratio: float = 0.15,
    max_cyrillic_ratio: float = 0.05,
    max_cjk_ratio: float = 0.05,
    max_latin_ratio: float = 0.80,
    min_letters_for_script: int = 20,
    **_unused,
) -> ValidationReport:
    """Run strong target-only checks when no matching source subtitle exists."""
    report = ValidationReport()
    raw = read_text_best_effort(Path(target_path))
    if raw is None:
        report.issues.append(ValidationIssue("target_unreadable", "target subtitle is unreadable"))
        return report

    cues, errors = parse_srt_cues(raw)
    for error in errors:
        report.issues.append(ValidationIssue("target_structure", error))
    if errors:
        return report

    profile = script_profile_for_code(target_lang)
    for cue_index, cue in enumerate(cues):
        line_count = len([line for line in cue.lines if line.strip()])
        if line_count > max_cue_lines:
            report.issues.append(ValidationIssue(
                "excessive_lines",
                f"translation has {line_count} lines (max {max_cue_lines} without source)",
                cue_index,
                cue.number,
            ))
        if len(cue.text) > max_cue_chars:
            report.issues.append(ValidationIssue(
                "cue_too_long",
                f"translation is {len(cue.text)} characters (max {max_cue_chars})",
                cue_index,
                cue.number,
            ))
        garbage = find_garbage_match(cue.text)
        if garbage is not None:
            rule = "prompt_marker" if garbage == "prompt marker" else "garbage"
            report.issues.append(ValidationIssue(
                rule,
                f"garbage pattern ({garbage})",
                cue_index,
                cue.number,
            ))
        script_ok, script_reason = check_script_profile(
            [cue.text],
            profile,
            max_cyrillic_ratio=max_cyrillic_ratio,
            max_cjk_ratio=max_cjk_ratio,
            max_latin_ratio=max_latin_ratio,
            min_letters_for_script=10,
        )
        if not script_ok:
            report.issues.append(ValidationIssue(
                "unexpected_script", script_reason, cue_index, cue.number
            ))

    target_valid, target_reason = validate_subtitle_file(
        target_path,
        detector,
        target_language,
        target_lang=target_lang,
        min_chars=min_chars,
        min_confidence=min_confidence,
        max_unique_ratio=max_unique_ratio,
        max_cyrillic_ratio=max_cyrillic_ratio,
        max_cjk_ratio=max_cjk_ratio,
        max_latin_ratio=max_latin_ratio,
        min_letters_for_script=min_letters_for_script,
    )
    if not target_valid:
        located = (
            target_reason.startswith("garbage pattern")
            and any(issue.rule in ("prompt_marker", "garbage") for issue in report.issues)
        ) or any(issue.rule == "unexpected_script" for issue in report.issues)
        if not located:
            report.issues.append(ValidationIssue("target_file_invalid", target_reason))
    return report


def discover_target_subtitles(
    roots: Iterable[Path],
    target_languages: Iterable[str],
) -> list[DiscoveredSubtitle]:
    canonical_languages = {
        lang.strip().lower() for lang in target_languages if lang.strip()
    }
    alias_to_language = {
        alias: language
        for language in canonical_languages
        for alias in TARGET_CODE_ALIASES.get(language, {language})
    }
    aliases = sorted(alias_to_language, key=len, reverse=True)
    if not aliases:
        return []
    language_pattern = "|".join(re.escape(alias) for alias in aliases)
    pattern = re.compile(
        rf"\.(?P<lang>{language_pattern})(?P<variant>\.(?:hi|sdh|\d+))?\.srt$",
        re.IGNORECASE,
    )
    discovered: list[DiscoveredSubtitle] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.srt"):
            if not path.is_file() or path in seen:
                continue
            match = pattern.search(path.name)
            if match:
                language_token = match.group("lang").lower()
                seen.add(path)
                discovered.append(DiscoveredSubtitle(
                    path=path,
                    target_lang=alias_to_language[language_token],
                    variant=(match.group("variant") or "").lower(),
                    language_token=language_token,
                ))
    return sorted(discovered, key=lambda item: str(item.path).casefold())


def find_preferred_source(
    candidate: DiscoveredSubtitle,
    source_codes: tuple[str, ...] = ("eng", "en"),
) -> tuple[Optional[Path], Optional[str]]:
    language_token = candidate.language_token or candidate.target_lang
    suffix = f".{language_token}{candidate.variant}.srt"
    if not candidate.path.name.lower().endswith(suffix):
        return None, None
    base_name = candidate.path.name[:-len(suffix)]
    files_by_name = {
        path.name.casefold(): path
        for path in candidate.path.parent.iterdir()
        if path.is_file()
    }
    variants = (candidate.variant, "") if candidate.variant else ("",)
    for variant in variants:
        for code in source_codes:
            source = files_by_name.get(f"{base_name}.{code}{variant}.srt".casefold())
            if source is not None:
                return source, "en" if code in ("en", "eng") else code
    return None, None


def file_sha256(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# Backward-compatible import name; validation state is now SQLite-backed.
from autotranslate.persistence.state_store import StateStore as ValidationStateStore


def quarantine_destination(
    path: Path | str,
    roots: Iterable[Path],
    quarantine_root: Path | str,
) -> Path:
    source = Path(path)
    relative: Optional[Path] = None
    resolved_source = source.resolve()
    for root in roots:
        try:
            relative = resolved_source.relative_to(root.resolve())
            break
        except ValueError:
            continue
    if relative is None:
        relative = Path(source.name)

    destination = Path(quarantine_root) / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    base_destination = destination
    counter = 1
    while destination.exists():
        destination = base_destination.with_name(
            f"{base_destination.stem}.{counter}{base_destination.suffix}"
        )
        counter += 1
    return destination


def quarantine_subtitle(
    path: Path | str,
    roots: Iterable[Path],
    quarantine_root: Path | str,
    *,
    destination: Path | str | None = None,
    access_coordinator=None,
) -> Path:
    source = Path(path)
    destination = (
        Path(destination)
        if destination is not None
        else quarantine_destination(source, roots, quarantine_root)
    )
    access = (
        access_coordinator.hold(source, destination)
        if access_coordinator is not None else nullcontext()
    )
    with access:
        destination.parent.mkdir(parents=True, exist_ok=True)
        normalize_managed_file(source)
        shutil.move(str(source), str(destination))
        # Retention begins at quarantine time, not at the source sidecar's age.
        os.utime(destination, None)
    return destination


def write_validation_report(path: Path | str, payload: dict) -> Path:
    report_path = Path(f"{path}.validation.json")
    temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            prefix=f".{report_path.name}.",
            suffix=".tmp",
            dir=report_path.parent,
            delete=False,
        ) as report_file:
            json.dump(payload, report_file, ensure_ascii=False, indent=2)
            temp_path = Path(report_file.name)
        normalize_managed_file(temp_path)
        os.replace(temp_path, report_path)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass
    return report_path


def purge_old_files(
    root: Path | str,
    retention_days: int,
    *,
    now_timestamp: Optional[float] = None,
    exclude: Iterable[Path | str] = (),
    access_coordinator=None,
) -> list[Path]:
    """Delete files older than the retention cutoff and remove empty child directories."""
    directory = Path(root)
    if not directory.exists():
        return []
    cutoff = (now_timestamp if now_timestamp is not None else datetime.now(timezone.utc).timestamp()) - (
        retention_days * 86400
    )
    excluded = {str(Path(path).resolve()) for path in exclude}
    removed: list[Path] = []
    for path in directory.rglob("*"):
        if not (path.is_file() or path.is_symlink()):
            continue
        if str(path.resolve()) in excluded:
            continue
        try:
            access = (
                access_coordinator.hold(path)
                if access_coordinator is not None else nullcontext()
            )
            with access:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed.append(path)
        except FileNotFoundError:
            continue

    child_directories = sorted(
        (path for path in directory.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for child in child_directories:
        try:
            child.rmdir()
        except OSError:
            pass
    return removed


def delete_or_quarantine(path: Path, quarantine_dir: Optional[Path], do_delete: bool) -> None:
    if shutdown_requested:
        raise InterruptedError("Shutdown requested")

    if quarantine_dir is not None:
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        target = quarantine_dir / path.name
        i = 1
        while target.exists():
            target = quarantine_dir / f"{path.stem}.{i}{path.suffix}"
            i += 1
        path.rename(target)
        return

    if do_delete:
        path.unlink()


def _process_file(
    path: Path,
    detector,
    target_language: Language,
    args,
    quarantine_dir: Optional[Path],
    counters: dict,
) -> None:
    is_valid, reason = validate_subtitle_file(
        path,
        detector,
        target_language,
        target_lang=args.target_lang,
        min_chars=args.min_chars,
        min_confidence=args.min_confidence,
        max_unique_ratio=args.max_unique_ratio,
        max_cyrillic_ratio=args.max_cyrillic_ratio,
        max_cjk_ratio=args.max_cjk_ratio,
        max_latin_ratio=args.max_latin_ratio,
        min_letters_for_script=args.min_letters_for_script,
    )

    if is_valid:
        if args.verbose:
            print(f"OK ({reason}): {path}")
        if "too short" in reason:
            counters["skipped_short"] += 1
        elif "unknown" in reason:
            counters["unknown"] += 1
        else:
            counters["candidates"] += 1
        return

    if "garbage pattern" in reason:
        counters["garbage"] += 1
    elif any(x in reason for x in ("Cyrillic", "CJK", "Latin", "non-Latin", "Latin script")):
        counters["script"] += 1
    elif "repetitive" in reason:
        counters["repetitive"] += 1
    else:
        counters["not_target"] += 1

    action_label = "DRYRUN"
    if args.delete or quarantine_dir is not None:
        action_label = "DELETE" if quarantine_dir is None else "QUARANTINE"

    print(f"{action_label} ({reason}): {path}")

    if args.delete or quarantine_dir is not None:
        try:
            delete_or_quarantine(path, quarantine_dir, do_delete=args.delete and quarantine_dir is None)
            counters["actions"] += 1
        except InterruptedError:
            raise
        except Exception as e:
            print(f"ERROR: could not apply action to {path}: {e}", file=sys.stderr)
            sys.stderr.flush()


def main() -> int:
    # Unbuffered output for log visibility
    sys.stdout = os.fdopen(sys.stdout.fileno(), "w", buffering=1)
    sys.stderr = os.fdopen(sys.stderr.fileno(), "w", buffering=1)
    _register_signal_handlers()

    ap = argparse.ArgumentParser(
        description="Delete or quarantine subtitle files that are not in the expected target language."
    )
    ap.add_argument(
        "--root",
        action="append",
        help="Root folder to scan (repeatable). Example: --root /media/tv --root /media/movies",
    )
    ap.add_argument(
        "--file",
        action="append",
        help="Single subtitle file to validate (repeatable). Skips directory scan.",
    )
    ap.add_argument(
        "--target-lang",
        default="et",
        help="Expected target language code2. Default: et",
    )
    ap.add_argument(
        "--suffix",
        default=".et.srt",
        help="File suffix to match when scanning --root. Default: .et.srt",
    )
    ap.add_argument(
        "--min-chars",
        type=int,
        default=200,
        help="Minimum cleaned subtitle text length needed for language detection. Default: 200",
    )
    ap.add_argument(
        "--min-confidence",
        type=float,
        default=0.70,
        help="Minimum confidence to treat detection as reliable. Default: 0.70",
    )
    ap.add_argument(
        "--delete",
        action="store_true",
        help="Actually delete files (or move if --quarantine is set). Without this, dry run only.",
    )
    ap.add_argument(
        "--quarantine",
        default=None,
        help="Move files here instead of deleting (safer). Example: --quarantine /tmp/bad_subs",
    )
    ap.add_argument(
        "--max-unique-ratio",
        type=float,
        default=0.15,
        help="Flag files where unique_words/total_words is below this — catches repetition hallucinations. Default: 0.15",
    )
    ap.add_argument(
        "--max-cyrillic-ratio",
        type=float,
        default=0.05,
        help="Max Cyrillic letter ratio for Latin-target files. Default: 0.05",
    )
    ap.add_argument(
        "--max-cjk-ratio",
        type=float,
        default=0.05,
        help="Max CJK letter ratio for Latin-target files. Default: 0.05",
    )
    ap.add_argument(
        "--max-latin-ratio",
        type=float,
        default=0.80,
        help="Max Latin letter ratio for Cyrillic-target files. Default: 0.80",
    )
    ap.add_argument(
        "--min-letters-for-script",
        type=int,
        default=20,
        help="Minimum letters before whole-file script check applies. Default: 20",
    )
    ap.add_argument(
        "--verbose",
        action="store_true",
        help="Print extra details for each file.",
    )

    args = ap.parse_args()

    if not args.root and not args.file:
        ap.error("at least one of --root or --file is required")

    target_language = target_language_for_code(args.target_lang)
    if target_language is None:
        print(f"[ERROR] Unsupported --target-lang {args.target_lang!r}", file=sys.stderr)
        return 1

    roots = [Path(r).expanduser().resolve() for r in (args.root or [])]
    files = [Path(f).expanduser().resolve() for f in (args.file or [])]
    quarantine_dir = Path(args.quarantine).expanduser().resolve() if args.quarantine else None
    detector = build_detector()

    counters = {
        "total": 0,
        "candidates": 0,
        "not_target": 0,
        "skipped_short": 0,
        "unknown": 0,
        "actions": 0,
        "garbage": 0,
        "script": 0,
        "repetitive": 0,
    }

    paths: Iterable[Path]
    if files:
        paths = files
    else:
        paths = iter_srt_files(roots, args.suffix)

    for path in paths:
        if shutdown_requested:
            print("[WARNING] Shutdown requested. Stopping processing.", file=sys.stderr)
            sys.stderr.flush()
            break

        counters["total"] += 1
        try:
            _process_file(path, detector, target_language, args, quarantine_dir, counters)
        except InterruptedError:
            print("[WARNING] Processing interrupted by shutdown signal.", file=sys.stderr)
            sys.stderr.flush()
            break

    print("")
    print("Summary")
    print(f"  matched files: {counters['total']}")
    print(f"  analysed (>= min chars): {counters['candidates']}")
    print(f"  skipped short: {counters['skipped_short']}")
    print(f"  unknown/unreadable: {counters['unknown']}")
    print(f"  garbage patterns: {counters['garbage']}")
    print(f"  script mismatch: {counters['script']}")
    print(f"  repetitive (hallucination): {counters['repetitive']}")
    print(f"  not {args.target_lang}: {counters['not_target']}")
    print(f"  actions taken: {counters['actions']} (dry run if 0 and no --delete/--quarantine)")
    sys.stdout.flush()

    if shutdown_requested:
        return 130
    return 0


if __name__ == "__main__":
    exit_code = 1
    try:
        exit_code = main()
    except KeyboardInterrupt:
        print("[WARNING] Script interrupted by user.", file=sys.stderr)
        sys.stderr.flush()
        exit_code = 130
    except Exception as e:
        print(f"[ERROR] Unexpected error: {e}", file=sys.stderr)
        sys.stderr.flush()
        exit_code = 1
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        sys.exit(exit_code)
