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

from .copied_source import (
    AMBIGUOUS,
    COPIED_PROSE,
    LIKELY_INVARIANT,
    REVIEW_REQUIRED,
    assess_copied_source,
)
from .names import approved_name

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
    "ambiguous_copied_source",
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

VALIDATOR_VERSION = "source-aware-v6-scoped-name-review"


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


@dataclass(frozen=True)
class ValidationObservation:
    classification: str
    reason: str
    evidence: dict
    cue_index: Optional[int] = None
    cue_number: Optional[int] = None


@dataclass
class ValidationReport:
    issues: list[ValidationIssue] = field(default_factory=list)
    observations: list[ValidationObservation] = field(default_factory=list)

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
            "observations": [
                {
                    "classification": observation.classification,
                    "reason": observation.reason,
                    "evidence": observation.evidence,
                    "cueIndex": observation.cue_index,
                    "cueNumber": observation.cue_number,
                }
                for observation in self.observations
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


def _language_code(language: Language | None) -> str | None:
    if language is None:
        return None
    return next(
        (code for code, candidate in TARGET_LANGUAGE_MAP.items() if candidate == language),
        language.name.casefold(),
    )


def _language_confidence(detector, text: str, language: Language) -> float | None:
    if not text.strip():
        return None
    try:
        return float(detector.compute_language_confidence(text, language))
    except Exception:
        return None


def find_garbage_match(text: str) -> Optional[str]:
    """Return a label for the first garbage pattern matched, or None."""
    for pattern, label in GARBAGE_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return label
    return None


def file_sha256(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _validate_cue_pair_assessed(
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
    cue_language: str | None = None,
    cue_language_confidence: float | None = None,
    whole_target_confidence: float | None = None,
    context_confidence: float | None = None,
    approved_name_pairs=(),
) -> tuple[list[ValidationIssue], ValidationObservation | None]:
    issues: list[ValidationIssue] = []
    observation: ValidationObservation | None = None
    source_text = source.text
    target_text = target.text

    def add(rule: str, detail: str) -> None:
        issues.append(ValidationIssue(rule, detail, cue_index, target.number))

    if not target_text:
        add("empty_target", "translation is empty")
        return issues, observation

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
    if source_normalised and target_normalised:
        similarity = SequenceMatcher(None, source_normalised, target_normalised).ratio()
        repair_eligible = (
            len(source_normalised) >= 20
            and len(target_normalised) >= 20
            and similarity >= max_source_similarity
        )
        assessment = assess_copied_source(
            source_text,
            target_text,
            source_normalized=source_normalised,
            target_normalized=target_normalised,
            similarity=similarity,
            repair_eligible=repair_eligible,
            cue_language=cue_language,
            cue_language_confidence=cue_language_confidence,
            whole_target_confidence=whole_target_confidence,
            context_confidence=context_confidence,
        )
        if approved_name(source_text, target_text, approved_name_pairs):
            observation = ValidationObservation(LIKELY_INVARIANT, "Exact name approved by operator.", {"operatorApproved": True}, cue_index, target.number)
        elif assessment is not None and assessment.outcome == REVIEW_REQUIRED:
            add("ambiguous_copied_source", "possible unchanged name needs review")
        elif assessment is not None and assessment.outcome == COPIED_PROSE and repair_eligible:
            add("copied_source", f"translation matches source ({similarity:.0%} similar)")
        elif assessment is not None and assessment.outcome in (LIKELY_INVARIANT, AMBIGUOUS):
            observation = ValidationObservation(
                assessment.outcome,
                assessment.reason,
                assessment.evidence,
                cue_index,
                target.number,
            )

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

    return issues, observation


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
    approved_name_pairs=(),
) -> list[ValidationIssue]:
    """Validate one cue while preserving the historical issues-only interface."""
    issues, _observation = _validate_cue_pair_assessed(
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
        approved_name_pairs=approved_name_pairs,
    )
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
    approved_name_pairs=(),
    approval_revision=0,
    approval_scope=None,
) -> ValidationReport:
    """Validate a translated SRT against its source and return cue-level findings."""
    report = ValidationReport()
    report.approval_revision = approval_revision
    report.approval_scope = approval_scope
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

    whole_target_text = " ".join(cue.text for cue in target_cues if cue.text)
    whole_target_confidence = _language_confidence(
        detector, whole_target_text, target_language
    )
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
        context_text = " ".join(
            target_cues[index].text
            for index in range(max(0, cue_index - 2), min(len(target_cues), cue_index + 3))
            if index != cue_index and target_cues[index].text
        )
        context_confidence = _language_confidence(
            detector, context_text, target_language
        )
        cue_language, cue_language_confidence = detect_language(detector, target.text)
        cue_issues, observation = _validate_cue_pair_assessed(
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
            cue_language=_language_code(cue_language),
            cue_language_confidence=cue_language_confidence,
            whole_target_confidence=whole_target_confidence,
            context_confidence=context_confidence,
            approved_name_pairs=approved_name_pairs,
        )
        report.issues.extend(cue_issues)
        if observation is not None:
            report.observations.append(observation)

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
