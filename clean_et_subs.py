#!/usr/bin/env python3
import argparse
import os
import re
import sys
import signal
from pathlib import Path
from typing import Iterable, Optional, Tuple

from lingua import Language, LanguageDetectorBuilder

# Force unbuffered output for CRON compatibility
sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', buffering=1)
sys.stderr = os.fdopen(sys.stderr.fileno(), 'w', buffering=1)

# Global flag for graceful shutdown
shutdown_requested = False


def signal_handler(signum, frame):
    """Handle termination signals gracefully."""
    global shutdown_requested
    shutdown_requested = True
    print(f"[WARNING] Received signal {signum}. Initiating graceful shutdown...", file=sys.stderr)
    sys.stderr.flush()


# Register signal handlers for graceful shutdown
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

# HTTP error patterns to detect in subtitle files
HTTP_ERROR_PATTERNS = [
    r"503\s+Service\s+Unavailable",
    r"400\s+Bad\s+Request",
    r"500\s+Internal\s+Server\s+Error",
    r"429\s+Too\s+Many\s+Requests",
]
HTTP_ERROR_RE = re.compile("|".join(HTTP_ERROR_PATTERNS), re.IGNORECASE)

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
    # Returns (language, confidence)
    # Lingua provides confidence values.
    try:
        lang = detector.detect_language_of(text)
        if lang is None:
            return None, 0.0
        conf = detector.compute_language_confidence(text, lang)
        return lang, float(conf)
    except Exception:
        return None, 0.0

def count_http_errors(text: str) -> int:
    """Count occurrences of HTTP error messages in text."""
    return len(HTTP_ERROR_RE.findall(text))

def delete_or_quarantine(path: Path, quarantine_dir: Optional[Path], do_delete: bool) -> None:
    if shutdown_requested:
        raise InterruptedError("Shutdown requested")
    
    if quarantine_dir is not None:
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        target = quarantine_dir / path.name
        # Avoid overwriting
        i = 1
        while target.exists():
            target = quarantine_dir / f"{path.stem}.{i}{path.suffix}"
            i += 1
        path.rename(target)
        return

    if do_delete:
        path.unlink()

def main() -> int:
    ap = argparse.ArgumentParser(description="Delete or quarantine .et.srt files that are not actually Estonian.")
    ap.add_argument(
        "--root",
        action="append",
        required=True,
        help="Root folder to scan (repeatable). Example: --root /media/tv --root /media/movies",
    )
    ap.add_argument(
        "--suffix",
        default=".et.srt",
        help="File suffix to match. Default: .et.srt",
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
        "--verbose",
        action="store_true",
        help="Print extra details for each file.",
    )

    args = ap.parse_args()

    roots = [Path(r).expanduser().resolve() for r in args.root]
    quarantine_dir = Path(args.quarantine).expanduser().resolve() if args.quarantine else None

    # Focus detector on likely subtitle languages to reduce confusion and improve accuracy.
    languages = [
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
    detector = LanguageDetectorBuilder.from_languages(*languages).build()

    total = 0
    candidates = 0
    not_estonian = 0
    skipped_short = 0
    unknown = 0
    actions = 0
    http_errors = 0

    for path in iter_srt_files(roots, args.suffix):
        if shutdown_requested:
            print("[WARNING] Shutdown requested. Stopping processing.", file=sys.stderr)
            sys.stderr.flush()
            break
        
        total += 1
        raw = read_text_best_effort(path)
        if raw is None:
            unknown += 1
            if args.verbose:
                print(f"UNKNOWN (read failed): {path}")
            continue

        # Check for HTTP errors in raw text
        error_count = count_http_errors(raw)
        if error_count >= 2:
            http_errors += 1
            action_label = "DRYRUN"
            if args.delete or quarantine_dir is not None:
                action_label = "DELETE" if quarantine_dir is None else "QUARANTINE"
            
            print(f"{action_label} (HTTP errors: {error_count}): {path}")
            
            if args.delete or quarantine_dir is not None:
                try:
                    delete_or_quarantine(path, quarantine_dir, do_delete=args.delete and quarantine_dir is None)
                    actions += 1
                except InterruptedError:
                    print("[WARNING] Processing interrupted by shutdown signal.", file=sys.stderr)
                    sys.stderr.flush()
                    break
                except Exception as e:
                    print(f"ERROR: could not apply action to {path}: {e}", file=sys.stderr)
                    sys.stderr.flush()
            continue

        cleaned = clean_srt_text(raw)
        if len(cleaned) < args.min_chars:
            skipped_short += 1
            if args.verbose:
                print(f"SKIP (too short {len(cleaned)} chars): {path}")
            continue

        candidates += 1
        lang, conf = detect_language(detector, cleaned)

        if lang is None:
            unknown += 1
            if args.verbose:
                print(f"UNKNOWN (no language): {path}")
            continue

        is_et = (lang == Language.ESTONIAN) and (conf >= args.min_confidence)

        if is_et:
            if args.verbose:
                print(f"OK (ET {conf:.2f}): {path}")
            continue

        not_estonian += 1
        action_label = "DRYRUN"
        if args.delete or quarantine_dir is not None:
            action_label = "DELETE" if quarantine_dir is None else "QUARANTINE"

        print(f"{action_label} (detected {lang.name} {conf:.2f}): {path}")

        if args.delete or quarantine_dir is not None:
            try:
                delete_or_quarantine(path, quarantine_dir, do_delete=args.delete and quarantine_dir is None)
                actions += 1
            except InterruptedError:
                print("[WARNING] Processing interrupted by shutdown signal.", file=sys.stderr)
                sys.stderr.flush()
                break
            except Exception as e:
                print(f"ERROR: could not apply action to {path}: {e}", file=sys.stderr)
                sys.stderr.flush()

    print("")
    print("Summary")
    print(f"  matched files: {total}")
    print(f"  analysed (>= min chars): {candidates}")
    print(f"  skipped short: {skipped_short}")
    print(f"  unknown/unreadable: {unknown}")
    print(f"  HTTP errors (>= 2): {http_errors}")
    print(f"  not Estonian: {not_estonian}")
    print(f"  actions taken: {actions} (dry run if 0 and no --delete/--quarantine)")
    sys.stdout.flush()

    if shutdown_requested:
        return 130  # Standard exit code for SIGINT
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
