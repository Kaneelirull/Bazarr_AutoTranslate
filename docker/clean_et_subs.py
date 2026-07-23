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
import threading
import time
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


def _looks_like_proper_noun_list(text: str) -> bool:
    words = re.findall(r"[A-Za-z\u00C0-\u024F]+", TAG_RE.sub(" ", text))
    return bool(words) and all(word.isupper() or word[0].isupper() for word in words)


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
        and not _looks_like_proper_noun_list(source_text)
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
    max_attempts: int = 2,
    context_lines: int = 5,
    attempt_logger: Optional[Callable[[dict], None]] = None,
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
    attempt_count = 0
    attempt_history: list[dict] = []

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

    for cue_index in cue_indexes:
        source_cue = source_cues[cue_index]
        before = [cue.text for cue in source_cues[max(0, cue_index - context_lines):cue_index]]
        after = [cue.text for cue in source_cues[cue_index + 1:cue_index + 1 + context_lines]]
        accepted = False
        last_reason = "translator returned no usable text"

        for attempt in range(max(1, max_attempts)):
            attempt_count += 1
            attempt_before = before if attempt == 0 else []
            attempt_after = after if attempt == 0 else []
            attempt_record = {
                "cueNumber": source_cue.number,
                "attempt": attempt + 1,
                "maxAttempts": max(1, max_attempts),
                "contextBefore": len(attempt_before),
                "contextAfter": len(attempt_after),
                "withoutContext": not attempt_before and not attempt_after,
                "startedAt": datetime.now(timezone.utc).isoformat(),
            }
            started = time.monotonic()
            if attempt_logger is not None:
                attempt_logger({**attempt_record, "event": "sending"})
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
            if translated is None or not translated.strip():
                attempt_record.update({
                    "durationSeconds": round(time.monotonic() - started, 3),
                    "outcome": "empty_response",
                })
                attempt_history.append(attempt_record)
                if attempt_logger is not None:
                    attempt_logger({**attempt_record, "event": "failed"})
                continue

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
                last_reason = ValidationReport(replacement_issues).summary()
                attempt_record.update({
                    "durationSeconds": round(time.monotonic() - started, 3),
                    "outcome": "rejected",
                    "validationRules": sorted({issue.rule for issue in replacement_issues}),
                })
                attempt_history.append(attempt_record)
                if attempt_logger is not None:
                    attempt_logger({**attempt_record, "event": "rejected"})
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

        if not accepted:
            return RepairResult(
                False,
                repaired_numbers,
                initial_report,
                f"cue {source_cue.number} could not be repaired: {last_reason}",
                attempt_count,
                attempt_history,
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
                False, repaired_numbers, final_report, final_report.summary(), attempt_count, attempt_history
            )

        normalize_managed_file(temp_name)
        os.replace(temp_name, target)
        temp_name = None
        return RepairResult(
            True, repaired_numbers, final_report, "repaired and validated", attempt_count, attempt_history
        )
    except OSError as exc:
        return RepairResult(
            False,
            repaired_numbers,
            initial_report,
            f"could not write repaired file: {exc}",
            attempt_count,
            attempt_history,
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


class ValidationStateStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data: dict = {
            "validatorVersion": VALIDATOR_VERSION,
            "files": {},
            "quarantineTombstones": {},
        }
        self.load()

    @staticmethod
    def _key(path: Path | str) -> str:
        return str(Path(path).resolve())

    def load(self) -> None:
        with self._lock:
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return
            except (OSError, ValueError):
                return
            if isinstance(payload, dict) and isinstance(payload.get("files"), dict):
                self._data = payload
                self._data.setdefault("quarantineTombstones", {})

    def is_unchanged_valid(
        self,
        target_path: Path | str,
        source_hash: Optional[str],
        target_hash: str,
    ) -> bool:
        with self._lock:
            entry = self._data.get("files", {}).get(self._key(target_path), {})
            return (
                entry.get("validatorVersion") == VALIDATOR_VERSION
                and entry.get("result") in ("valid", "valid_with_warnings")
                and entry.get("sourceHash") == source_hash
                and entry.get("targetHash") == target_hash
            )

    def matching_origin(self, target_path: Path | str, target_hash: str) -> Optional[str]:
        """Return provenance only while the on-disk content still matches the recorded hash."""
        entry = self.matching_record(target_path, target_hash)
        if entry is None:
            return None
        origin = entry.get("origin")
        return origin if isinstance(origin, str) and origin else None

    def matching_record(
        self, target_path: Path | str, target_hash: str
    ) -> Optional[dict]:
        """Return a copy of the provenance record while target content is unchanged."""
        with self._lock:
            entry = self._data.get("files", {}).get(self._key(target_path), {})
            if entry.get("targetHash") != target_hash:
                return None
            return dict(entry)

    def current_valid_details(self, target_path: Path | str, target_hash: str) -> Optional[dict]:
        """Return cached validation details only for unchanged content and this validator version."""
        with self._lock:
            entry = self._data.get("files", {}).get(self._key(target_path), {})
            if (
                entry.get("validatorVersion") != VALIDATOR_VERSION
                or entry.get("result") not in ("valid", "valid_with_warnings")
                or entry.get("targetHash") != target_hash
            ):
                return None
            details = entry.get("details")
            return dict(details) if isinstance(details, dict) else {}

    def record(
        self,
        target_path: Path | str,
        *,
        source_hash: Optional[str],
        target_hash: Optional[str],
        result: str,
        origin: Optional[str] = None,
        details: Optional[dict] = None,
    ) -> None:
        entry = {
            "validatorVersion": VALIDATOR_VERSION,
            "sourceHash": source_hash,
            "targetHash": target_hash,
            "result": result,
            "validatedAt": datetime.now(timezone.utc).isoformat(),
        }
        if origin:
            entry["origin"] = origin
        if details:
            entry["details"] = details
        with self._lock:
            self._data.setdefault("files", {})[self._key(target_path)] = entry
            self._data["validatorVersion"] = VALIDATOR_VERSION
            self._save_locked()

    def record_quarantine_tombstone(
        self,
        identity: str,
        *,
        target_path: Path | str,
        target_hash: str,
        target_language: str,
        rules: Iterable[str],
        origin: Optional[str],
        hold_days: int,
        now: Optional[datetime] = None,
    ) -> tuple[dict, bool]:
        timestamp = now or datetime.now(timezone.utc)
        identity_key = str(identity)
        key = f"{identity_key}|{target_hash}"
        with self._lock:
            tombstones = self._data.setdefault("quarantineTombstones", {})
            previous = tombstones.get(key, {})
            if not previous:
                legacy = tombstones.get(identity_key, {})
                if legacy.get("targetHash") == target_hash:
                    previous = legacy
                    tombstones.pop(identity_key, None)
            repeated = previous.get("targetHash") == target_hash
            first_seen = (
                previous.get("firstSeen")
                if repeated and isinstance(previous.get("firstSeen"), str)
                else timestamp.isoformat()
            )
            occurrences = int(previous.get("occurrences", 0)) + 1 if repeated else 1
            entry = {
                "identity": identity_key,
                "targetPath": str(target_path),
                "targetHash": target_hash,
                "targetLanguage": target_language,
                "rules": sorted({str(rule) for rule in rules if rule}),
                "origin": origin or "unknown",
                "firstSeen": first_seen,
                "lastSeen": timestamp.isoformat(),
                "holdUntil": datetime.fromtimestamp(
                    timestamp.timestamp() + max(1, hold_days) * 86400,
                    timezone.utc,
                ).isoformat(),
                "occurrences": occurrences,
            }
            tombstones[key] = entry
            self._save_locked()
            return dict(entry), repeated

    def active_quarantine_tombstone(
        self,
        identity: str,
        *,
        target_hash: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Optional[dict]:
        timestamp = now or datetime.now(timezone.utc)
        with self._lock:
            matches: list[tuple[datetime, dict]] = []
            for key, entry in self._data.setdefault(
                "quarantineTombstones", {}
            ).items():
                if not isinstance(entry, dict):
                    continue
                entry_identity = entry.get("identity")
                if entry_identity != str(identity) and key != str(identity):
                    continue
                if target_hash is not None and entry.get("targetHash") != target_hash:
                    continue
                try:
                    hold_until = datetime.fromisoformat(entry["holdUntil"])
                except (KeyError, TypeError, ValueError):
                    continue
                if hold_until > timestamp:
                    matches.append((hold_until, entry))
            if not matches:
                return None
            return dict(max(matches, key=lambda item: item[0])[1])

    def clear_quarantine_tombstone(self, identity: str) -> bool:
        with self._lock:
            tombstones = self._data.setdefault("quarantineTombstones", {})
            removed = [
                key
                for key, entry in tombstones.items()
                if key == str(identity)
                or (
                    isinstance(entry, dict)
                    and entry.get("identity") == str(identity)
                )
            ]
            for key in removed:
                tombstones.pop(key, None)
            if removed:
                self._save_locked()
            return bool(removed)

    def prune_older_than(self, retention_days: int, now: Optional[datetime] = None) -> int:
        cutoff = (now or datetime.now(timezone.utc)).timestamp() - retention_days * 86400
        removed = 0
        with self._lock:
            files = self._data.setdefault("files", {})
            for key, entry in list(files.items()):
                try:
                    validated_at = datetime.fromisoformat(entry["validatedAt"]).timestamp()
                except (KeyError, TypeError, ValueError):
                    continue
                if validated_at < cutoff:
                    del files[key]
                    removed += 1
            tombstones = self._data.setdefault("quarantineTombstones", {})
            for key, entry in list(tombstones.items()):
                try:
                    hold_until = datetime.fromisoformat(entry["holdUntil"]).timestamp()
                except (KeyError, TypeError, ValueError):
                    continue
                if hold_until < (now or datetime.now(timezone.utc)).timestamp():
                    del tombstones[key]
                    removed += 1
            if removed:
                self._save_locked()
        return removed

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temp_path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp_path, self.path)


def quarantine_subtitle(
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
    normalize_managed_file(source)
    shutil.move(str(source), str(destination))
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
