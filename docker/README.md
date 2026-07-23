# Bazarr AutoTranslate

Continuously monitors Bazarr for missing subtitles and translates them through Lingarr's direct API. It also validates new and existing target-language subtitles, repairs isolated bad cues, and quarantines files that cannot be repaired safely.

The container includes a read-only status dashboard at `http://<docker-host>:8765`. It is intentionally manual-refresh and intended for a trusted LAN.

## How it works

1. Synchronizes Bazarr's subtitle inventory.
2. Scans every regular sidecar SRT for media-duration completeness, then scans configured target languages, at startup and every `CLEANUP_SCAN_INTERVAL`.
3. Uses `ffprobe` plus cue, text, byte, and timeline density to quarantine high-confidence forced/truncated fragments that are mislabeled as full subtitles.
4. Rejects incomplete or explicitly forced sources and falls back through `LANGUAGES` before submitting a translation.
5. Uses source cue anchors to repair safe SRT formatting damage before validation.
6. Validates translated cues against the source, including structure, language, writing system, prompt leakage, character expansion, and physical line count.
7. Sends only remaining invalid cues through a dedicated Lingarr line-repair worker. The first attempt uses bounded context; the second uses no context.
8. Once every managed language is valid, quarantines recognized extra-language and unmanaged special-purpose SRT sidecars.
9. Normalizes managed subtitle artifacts to UID/GID `568:568` with mode `0664`.
10. Quarantines translations that remain invalid and triggers Bazarr subtitle rescans after repair, quarantine, or pruning.

Translation timeout is calculated dynamically from the source subtitle's dialogue line count.

## Requirements

- Bazarr with readable subtitle paths under the shared media mount
- Lingarr running and reachable through its direct API, with the same media
  library mounted at the same `/media` path
- A host filesystem that permits the application container to set subtitle
  ownership to `568:568`
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
| `MEDIA_PATH` | required | Host media path mounted at `/media` in both containers |
| `LINGARR_URL` | `http://lingarr:8080` | Lingarr URL |
| `LINGARR_API_KEY` | empty | Optional Lingarr API key |
| `LANGUAGES` | `en,et,sv` | Managed languages in source-priority order |
| `PARALLEL_TRANSLATES` | `1` | Verified global active-translation limit |
| `CHECK_INTERVAL` | `1200` | Seconds between wanted-subtitle cycles |
| `POLL_TIMEOUT` | `900` | Minimum per-file translation timeout |
| `RESUBMIT_COOLDOWN` | `3600` | Minimum delay before resubmitting an item/language pair |

## Subtitle validation, repair, and cleanup

Existing-library cleanup runs after startup synchronization and then on its own interval. New translations are validated immediately. Quarantine is the default action; permanent deletion must be selected explicitly.

| Variable | Default | Description |
|---|---|---|
| `CLEANUP_LANGUAGES` | `et` | Comma-separated target languages to validate |
| `CLEANUP_SCAN_EXISTING` | `true` | Scan existing files independently of Bazarr's wanted list |
| `CLEANUP_SCAN_INTERVAL` | `21600` | Seconds between existing-library scans (6 hours) |
| `CLEANUP_SCAN_DRY_RUN` | `false` | Report existing-file actions without repair, move, or deletion |
| `CLEANUP_PRUNE_EXTRA_LANGUAGES` | `true` | Prune recognized unmanaged SRT sidecars after all managed languages are ready |
| `CLEANUP_PRUNE_ACTION` | `quarantine` | Prune action: `quarantine`, `delete`, or `report` |
| `CLEANUP_PRUNE_SPECIAL_SIDECARS` | `true` | Remove unmanaged forced, foreign, signs, and commentary sidecars |
| `CLEANUP_PRUNE_UNKNOWN_SIDECARS` | `false` | Also remove language-less, numeric-only, or unclassifiable sidecars |
| `CLEANUP_SOURCELESS_LINE_ONLY_ACTION` | `warn` | Retain source-less subtitles whose only issue is excessive physical cue lines; use `quarantine` for the previous behavior |
| `CLEANUP_QUARANTINE_HOLD_DAYS` | `30` | Defer retranslating the same media/language after an invalid subtitle is quarantined |
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

## Translation status dashboard

The status page shows one queue entry per missing target language. Its initial count is fixed when the Bazarr wanted queue is read. A Lingarr submission is shown as `translating`; it becomes `accepted` only after the resulting subtitle passes local validation. `Done` includes accepted, failed, timed-out, deferred, and quarantined jobs, while `Remaining` contains queued, translating, validating, and repairing work.

The page includes the current or most recently completed cycle, active jobs, the next ten jobs, the latest twenty outcomes, and exact rolling 1-hour, 6-hour, 12-hour, 24-hour, and 7-day totals. Existing-library repairs, quarantines, undersized detections, and sidecar pruning are reported separately as maintenance activity.

| Variable | Default | Description |
|---|---|---|
| `STATUS_ENABLED` | `true` | Start the read-only status server |
| `STATUS_BIND` | `0.0.0.0` | Container interface used by the status server |
| `STATUS_PORT` | `8765` | Container and published host port |
| `STATUS_HISTORY_RETENTION_DAYS` | `30` | Status-event retention; minimum 7 days |
| `STATUS_RECENT_LIMIT` | `20` | Recent terminal jobs displayed and returned |

Endpoints:

- `/` — server-rendered dashboard with a manual Refresh button
- `/api/status` — the same snapshot as JSON
- `/healthz` — status-server health and current worker phase

The stable JSON sections are `generatedAt`, `service`, `currentCycle`, `activeJobs`, `upNext`, `recentOutcomes`, `history`, and `maintenance`. No filesystem paths, subtitle text, or credentials are returned.

Current state is atomically written to `/config/status.json`; terminal history is appended to `/config/status_history.jsonl`. Both survive container recreation through the existing `/config` volume. Active jobs found after a restart are finalized as deferred with an interruption reason. Status history is compacted during normal retention housekeeping.

The dashboard has no authentication. Keep port `8765` restricted to a trusted LAN or protect it with your firewall/reverse proxy. Set `STATUS_ENABLED=false` to disable the listener. A port-binding or status-file failure is non-fatal and does not stop translations.

Completeness scanning covers regular subtitles in every language, including HI, SDH, numbered, and language-less sidecars. Files explicitly labelled `forced`, `foreign`, `signs`, or `commentary` are exempt. A file is undersized only when at least three configured density/coverage signals fail; an unavailable duration is a safe skip.

Sidecar pruning runs after queued cue repairs and during each existing-library scan. It groups files by the exact sibling video stem and proceeds only when every language in `LANGUAGES` has a validated full subtitle and the media duration is usable. Every managed-language variant is preserved, including HI, SDH, numbered, and forced tracks; forced-only tracks do not satisfy readiness. Recognized non-managed languages and unmanaged forced/foreign/signs/commentary tracks are pruned. Language-less, numeric-only, and unclassifiable files are retained unless `CLEANUP_PRUNE_UNKNOWN_SIDECARS=true`.

The default prune action moves candidates out of the media directory immediately into `/config/quarantine`, so Bazarr no longer sees them. They remain recoverable until retention housekeeping permanently purges the subtitle and its audit report after 30 days by default. Use `CLEANUP_SCAN_DRY_RUN=true` to preview candidates without moving files or triggering Bazarr.

Target variants `.et.srt`, `.et.hi.srt`, `.et.sdh.srt`, and arbitrary numbered forms such as `.et.12.srt` receive language/content validation. Lingarr output names are discovered from the files that actually changed: for example, an `.en.hi.srt` source expects and accepts `.et.hi.srt` rather than incorrectly waiting for `.et.srt`. Existing target variants prefer a matching source variant, then fall back to a plain source.

Lingarr provenance is persisted before validation, including the source and actual output hashes and paths. If the container restarts between Lingarr completion and local validation, the next scan recovers that relationship and can still use source-aware format or cue repair. Exact source cue alignment, source-anchored formatting recovery, and AI cue repair apply only to outputs positively identified as Lingarr-created. Bazarr/manual subtitles are treated as independently segmented and are not rejected merely for differing English cue counts or timestamps.

A source-less subtitle whose only validation issue is `excessive_lines` is retained as `valid_with_warnings` by default and skipped on later scans while its hash is unchanged. Prompt leakage, malformed structure, wrong language/script, repetition, undersized content, or any other strong rule still makes it eligible for the configured cleanup action. No dialogue lines are joined automatically.

When a subtitle is quarantined or deleted, a media/language tombstone records the invalid hash for `CLEANUP_QUARANTINE_HOLD_DAYS`. If that exact hash reappears, duplicate AI repair is suppressed and the occurrence is recorded; a new Lingarr job for that media/language is deferred until the hold expires. A different replacement hash is validated immediately, and accepting a valid replacement clears the hold. Dry-run scans never create or change holds.

Source-anchored recovery normalizes BOMs, newlines, trailing whitespace, timestamp spacing, repeated separators, and blank lines inside cues. Orphan text is folded into its preceding cue only when every numbered timestamp anchor still matches the source in order. Missing, duplicate, reordered, or mismatched anchors are never guessed.

`CLEANUP_REPAIR_WORKERS=1` provides one additional repair lane: with `PARALLEL_TRANSLATES=1`, one complete subtitle job and one small line repair may run concurrently. Repair logs show queueing, worker, cue number, attempt, context counts, safe HTTP status, duration, validation rejection, and the no-context retry. Subtitle text, context contents, and credentials are never logged.

## Quarantine recovery

Each quarantined subtitle has a companion `.validation.json` report containing its original path, hashes, failed cues, validation rules, repair outcome, provenance, filename classification, and—when applicable—the media duration, completeness metrics, thresholds, and failed signals.

1. Read `targetPath` and the validation issues in the report.
2. Correct the subtitle or adjust settings only for a confirmed false positive.
3. Move the subtitle back to `targetPath` and trigger a Bazarr subtitle scan.

The `/config` volume persists quarantine files, validation state, provenance, cooldown state, and quarantine holds across container recreation. Quarantine files, companion reports, daily application logs, old validation-state records, and expired holds are removed after their configured retention period. Cleanup runs at startup and hourly by default.

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
