from __future__ import annotations

from .foundation import *
from .repair import *

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
    from .sources import is_extracted_sidecar
    discovered: list[DiscoveredSubtitle] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.srt"):
            if not path.is_file() or path in seen or is_extracted_sidecar(path):
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
