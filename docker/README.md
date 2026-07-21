# Bazarr AutoTranslate

Continuously monitors Bazarr for missing subtitles and translates them through Lingarr's direct API. It also validates new and existing target-language subtitles, repairs isolated bad cues, and quarantines files that cannot be repaired safely.

## How it works

1. Synchronizes Bazarr's subtitle inventory.
2. Scans every regular sidecar SRT for media-duration completeness, then scans configured target languages, at startup and every `CLEANUP_SCAN_INTERVAL`.
3. Uses `ffprobe` plus cue, text, byte, and timeline density to quarantine high-confidence forced/truncated fragments that are mislabeled as full subtitles.
4. Rejects incomplete or explicitly forced sources and falls back through `LANGUAGES` before submitting a translation.
5. Uses source cue anchors to repair safe SRT formatting damage before validation.
6. Validates translated cues against the source, including structure, language, writing system, prompt leakage, character expansion, and physical line count.
7. Sends only remaining invalid cues through a dedicated Lingarr line-repair worker. The first attempt uses bounded context; the second uses no context.
8. Quarantines translations that remain invalid and triggers Bazarr subtitle rescans after repair or quarantine.

Translation timeout is calculated dynamically from the source subtitle's dialogue line count.

## Requirements

- Bazarr with readable subtitle paths under the shared media mount
- Lingarr running and reachable through its direct API
- Docker Compose

## Setup

```bash
cp .env.example .env
# Edit .env with your values
docker compose up -d
```

## Core environment variables

| Variable | Default | Description |
|---|---|---|
| `BAZARR_URL` | required | Bazarr URL, such as `http://192.168.1.100:6767` |
| `BAZARR_API_KEY` | required | Bazarr API key |
| `MEDIA_PATH` | required | Host media path mounted at `/media` |
| `LINGARR_URL` | `http://lingarr:8080` | Lingarr URL |
| `LINGARR_API_KEY` | empty | Optional Lingarr API key |
| `LANGUAGES` | `en,et,sv` | Managed languages in source-priority order |
| `PARALLEL_TRANSLATES` | `1` | Concurrent translation workers |
| `CHECK_INTERVAL` | `1200` | Seconds between wanted-subtitle cycles |
| `POLL_TIMEOUT` | `600` | Minimum per-file translation timeout |
| `RESUBMIT_COOLDOWN` | `3600` | Minimum delay before resubmitting an item/language pair |

## Subtitle validation, repair, and cleanup

Existing-library cleanup runs after startup synchronization and then on its own interval. New translations are validated immediately. Quarantine is the default action; permanent deletion must be selected explicitly.

| Variable | Default | Description |
|---|---|---|
| `CLEANUP_LANGUAGES` | `et` | Comma-separated target languages to validate |
| `CLEANUP_SCAN_EXISTING` | `true` | Scan existing files independently of Bazarr's wanted list |
| `CLEANUP_SCAN_INTERVAL` | `21600` | Seconds between existing-library scans (6 hours) |
| `CLEANUP_SCAN_DRY_RUN` | `false` | Report existing-file actions without repair, move, or deletion |
| `CLEANUP_ROOT` | `/media` | Scan root; separate multiple Linux paths with `:` |
| `CLEANUP_ACTION` | `quarantine` | Failure action: `quarantine`, `delete`, or `report` |
| `CLEANUP_QUARANTINE_DIR` | `/config/quarantine` | Persistent quarantine directory |
| `CLEANUP_MIN_CONFIDENCE` | `0.70` | Required whole-file target-language confidence |
| `CLEANUP_MIN_CHARS` | `200` | Minimum text length for whole-file detection |
| `CLEANUP_MAX_CUE_LINES` | `4` | Hard cue line limit; paired checks allow `source lines + 1` when greater |
| `CLEANUP_MAX_CUE_CHARS` | `500` | Maximum flattened characters in one cue |
| `CLEANUP_MAX_EXPANSION_RATIO` | `4.0` | Maximum target/source character expansion ratio |
| `CLEANUP_MAX_EXPANSION_CHARS` | `300` | Absolute allowance before expansion rejection |
| `CLEANUP_REPAIR_ENABLED` | `true` | Retry aligned invalid cues through `/api/Translate/line` |
| `CLEANUP_MAX_REPAIR_ATTEMPTS` | `2` | Maximum attempts per invalid cue |
| `CLEANUP_REPAIR_CONTEXT_LINES` | `5` | Context cues on attempt one; attempt two always uses none |
| `CLEANUP_FORMAT_REPAIR_ENABLED` | `true` | Repair source-anchored SRT formatting damage without AI |
| `CLEANUP_REPAIR_WORKERS` | `1` | Dedicated line-repair workers in addition to `PARALLEL_TRANSLATES` |
| `CLEANUP_REPAIR_QUEUE_MAX` | `100` | Maximum queued cue-repair files; overflow is deferred |
| `CLEANUP_UNDERSIZED_ENABLED` | `true` | Check every regular sidecar SRT against media duration |
| `CLEANUP_MIN_MEDIA_DURATION` | `900` | Minimum media duration in seconds before density checks apply |
| `CLEANUP_MIN_CUES_PER_MINUTE` | `1.5` | Cue-density completeness signal |
| `CLEANUP_MIN_TEXT_CHARS_PER_MINUTE` | `40` | Dialogue-character-density completeness signal |
| `CLEANUP_MIN_BYTES_PER_MINUTE` | `100` | File-byte-density completeness signal |
| `CLEANUP_MIN_TIMELINE_COVERAGE` | `0.60` | Final cue must reach this fraction of media duration |
| `CLEANUP_UNDERSIZED_REQUIRED_SIGNALS` | `3` | Failed completeness signals required for quarantine |
| `CLEANUP_FFPROBE_TIMEOUT` | `15` | Maximum seconds for one duration probe |
| `SYNC_START_TIMEOUT` | `30` | Seconds to wait for a triggered Bazarr scan to appear |
| `RETENTION_DAYS` | `30` | Maximum age for quarantine files, reports, application logs, and validation-state records |
| `RETENTION_CHECK_INTERVAL` | `3600` | Seconds between retention checks; cleanup also runs at startup |
| `LOG_DIR` | `/var/log/bazarr-autotranslate` | Daily application log directory |

Completeness scanning covers regular subtitles in every language, including HI, SDH, numbered, and language-less sidecars. Files explicitly labelled `forced`, `foreign`, `signs`, or `commentary` are exempt. A file is undersized only when at least three configured density/coverage signals fail; an unavailable duration is a safe skip.

Target variants `.et.srt`, `.et.hi.srt`, `.et.sdh.srt`, and numbered forms such as `.et.2.srt` receive language/content validation. Exact source cue alignment, source-anchored formatting recovery, and AI cue repair apply only to unchanged outputs recorded as created by Lingarr. Bazarr/manual subtitles are treated as independently segmented and are not rejected merely for differing English cue counts or timestamps.

Source-anchored recovery normalizes BOMs, newlines, trailing whitespace, timestamp spacing, repeated separators, and blank lines inside cues. Orphan text is folded into its preceding cue only when every numbered timestamp anchor still matches the source in order. Missing, duplicate, reordered, or mismatched anchors are never guessed.

`CLEANUP_REPAIR_WORKERS=1` provides one additional repair lane: with `PARALLEL_TRANSLATES=1`, one complete subtitle job and one small line repair may run concurrently. Repair logs show queueing, worker, cue number, attempt, context counts, safe HTTP status, duration, validation rejection, and the no-context retry. Subtitle text, context contents, and credentials are never logged.

## Quarantine recovery

Each quarantined subtitle has a companion `.validation.json` report containing its original path, hashes, failed cues, validation rules, repair outcome, provenance, filename classification, and—when applicable—the media duration, completeness metrics, thresholds, and failed signals.

1. Read `targetPath` and the validation issues in the report.
2. Correct the subtitle or adjust settings only for a confirmed false positive.
3. Move the subtitle back to `targetPath` and trigger a Bazarr subtitle scan.

The `/config` volume persists quarantine files, validation state, and cooldown state across container recreation. Quarantine files, companion reports, daily application logs, and old validation-state records are removed after `RETENTION_DAYS`. Cleanup runs at startup and hourly by default.

Docker console logs use the `local` logging driver with five 10 MB rotated files. Docker supports size-based rather than age-based console-log rotation; the daily files under `./logs` are the age-controlled 30-day log history.

## Operations

```bash
# Follow logs
docker compose logs -f bazarr-autotranslate

# Inspect quarantine files and reports
docker exec bazarr-autotranslate find /config/quarantine -type f

# Stop services
docker compose down
```

The container handles termination signals and finishes active work before stopping.
