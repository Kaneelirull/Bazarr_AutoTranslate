# Bazarr AutoTranslate

Continuously monitors Bazarr for missing subtitles and translates them through Lingarr's direct API. It also validates new and existing target-language subtitles, repairs isolated bad cues, and quarantines files that cannot be repaired safely.

The container includes a read-only status dashboard at `http://<docker-host>:8765`. It refreshes every 3 seconds while work is active and every 20 seconds while idle, retains a manual refresh control, and is intended for a trusted LAN.

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

### Corrected TrueNAS release

Use the immutable image tag `kaneelir0ll/bazarr-autotranslate:1.0.1` on
TrueNAS. This corrected release includes `media_identity.py` in the image and is
smoke-tested for both the `media_identity` and `status_dashboard` imports before
publication. The `latest` tag is updated to the exact same image digest only
after that check passes.

After changing the image tag, pull `1.0.1` and use **Recreate** (or redeploy the
app with **Force Pull Image** enabled) rather than merely restarting the existing
container. This ensures TrueNAS replaces the container created from the broken
image. Keeping `1.0.1` configured pins the corrected image explicitly.

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
| `PARALLEL_TRANSLATES` | `1` | Shared maximum for active full-file translations and cue repairs |
| `CHECK_INTERVAL` | `1200` | Seconds between wanted-subtitle cycles |
| `POLL_TIMEOUT` | `900` | Minimum per-file translation timeout |
| `REPAIR_SHUTDOWN_GRACE_SECONDS` | `30` | Maximum graceful drain for active repair workers during shutdown |
| `RESUBMIT_COOLDOWN` | `3600` | Minimum delay before resubmitting an item/language pair |
| `CIRCUIT_FAILURE_THRESHOLD` | `3` | Consecutive failures before series protection opens |
| `CIRCUIT_OPEN_CYCLES` | `3` | Healthy completed cycles before one half-open trial |

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
| `CLEANUP_MAX_REPAIR_ATTEMPTS` | `5` | Maximum attempts per invalid cue; every repairable cue is tried before file-level failure |
| `CLEANUP_REPAIR_CONTEXT_LINES` | `5` | Context cues on attempt one; later attempts use no context |
| `CLEANUP_FORMAT_REPAIR_ENABLED` | `true` | Repair source-anchored SRT formatting damage without AI |
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
| `RETENTION_DAYS` | `30` | Maximum age for application logs and validation-state audit records |
| `QUARANTINE_ARTIFACT_RETENTION_DAYS` | `30` | Maximum age for quarantined subtitle files and reports; never controls retry eligibility |
| `REGENERATION_INITIAL_DELAY_CYCLES` | `2` | Healthy completed cycles before the first fresh translation retry |
| `REGENERATION_MAX_ATTEMPTS` | `0` | Fresh translation limit; `0` retries indefinitely |
| `REGENERATION_MAX_DELAY_CYCLES` | `16` | Maximum completed-cycle delay between fresh translations |
| `DONOR_RECOVERY_ENABLED` | `true` | Reuse individually revalidated cues from prior quarantine attempts |
| `REGENERATION_BACKOFF_MULTIPLIER` | `2.0` | Completed-cycle backoff multiplier, producing 2/4/8 with defaults |
| `RETRY_BATCH_SIZE_PER_CYCLE` | `5` | Maximum regeneration admissions per completed cycle |
| `RETRY_MAX_PER_SERIES_PER_CYCLE` | `1` | Maximum regeneration admissions from one series per cycle |
| `END_OF_CYCLE_REPAIR_RETRY_ENABLED` | `true` | Allow one low-priority retry for a deferred cue repair |
| `RETENTION_CHECK_INTERVAL` | `3600` | Seconds between retention checks; cleanup also runs at startup |
| `LOG_DIR` | `/var/log/bazarr-autotranslate` | Daily application log directory |

## Translation status dashboard

### Adaptive timing and long files

Accepted translations are sampled in SQLite and used to learn a robust
seconds-per-cue estimate for each language pair. New installations start at
`TRANSLATION_COLD_SECONDS_PER_CUE=1.8`. Per-file deadlines use the learned
estimate, `TRANSLATION_TIMEOUT_MULTIPLIER`, and
`TRANSLATION_TIMEOUT_CAP` (three hours by default).

`PARALLEL_TRANSLATES` is the single shared capacity limit for full-file
translations and cue repairs. Pending repairs have priority over new file
submissions, and a file that needs repair hands its slot directly to that
repair. With two or more workers, one file lane is reserved for files estimated above
`LONG_JOB_THRESHOLD` and the remaining lanes serve short files. With one
worker, short files have priority whenever no repair is pending.

Failed Lingarr jobs are inspected for positioned line results. Valid completed
lines are retained and unresolved cues are retried individually instead of
resubmitting the complete episode. Three consecutive failures open a persisted
per-series circuit. It remains blocked for three healthy completed cycles, then
allows exactly one half-open trial. Interrupted and degraded cycles do not count.

The dashboard shows learned cue speed, estimates, ETA, lanes, and circuit
state. The **Logs** link opens a searchable, sanitized, read-only view of
managed AutoTranslate logs. Lingarr's internal provider command timeout is not
configurable through its documented API and is not changed here.

The status page shows one queue entry per missing target language. Its initial count is fixed when the Bazarr wanted queue is read. A Lingarr submission is shown as `translating`; it becomes `accepted` only after the resulting subtitle passes local validation. `Done` includes accepted, failed, timed-out, deferred, and quarantined jobs, while `Remaining` contains queued, translating, validating, and repair work. Repair work uses `repair queued`, `waiting for capacity`, `repairing`, and `validating repaired file` stages.

The page includes the current or most recently completed cycle, active jobs, the next ten jobs, the latest twenty outcomes, and exact rolling 1-hour, 6-hour, 12-hour, 24-hour, and 7-day totals. Active now combines wanted-cycle and maintenance rows visually, but their API collections and counters remain separate. Existing-library scans, cue repairs, Bazarr synchronization, and noticeable pruning batches are live maintenance work. Format repair, validation actions, undersized detection, quarantine, deletion, and individual pruning are shown through scan progress, recent maintenance outcomes, and rolling totals; retention housekeeping remains aggregate-only.

Cue repair progress reports finalized cues, the current cue, attempt/max attempts, rejected attempts, elapsed time, percentage, and ETA. `HTTP 200` means Lingarr returned a transport-level response; the cue is not complete until the candidate passes local validation or the cue exhausts all attempts. No source or target dialogue or context is published.

| Variable | Default | Description |
|---|---|---|
| `STATUS_ENABLED` | `true` | Start the read-only status server |
| `STATUS_BIND` | `0.0.0.0` | Container interface used by the status server |
| `STATUS_PORT` | `8765` | Container and published host port |
| `STATUS_HISTORY_RETENTION_DAYS` | `30` | Status-event retention; minimum 7 days |
| `STATUS_RECENT_LIMIT` | `20` | Recent terminal jobs displayed and returned |

Endpoints:

- `/` — responsive dashboard shell with live and manual refresh controls
- `/api/status` — the same snapshot as JSON
- `/healthz` — status-server health and current worker phase

The stable JSON sections are `generatedAt`, `service`, `currentCycle`, `activeJobs`, `upNext`, `recentOutcomes`, `history`, and `maintenance`. Existing fields remain backward compatible. `maintenance` additionally contains `activeJobs` and `recentOutcomes`; jobs expose a stable `statusJobId`, work kind, operation, lifecycle state, safe media identity, timing, and whitelisted progress fields. No filesystem paths, subtitle text, context, prompts, credentials, or provider response bodies are returned.

Current state is atomically written to `/config/status.json`; terminal history is appended to `/config/status_history.jsonl`. Both survive container recreation through the existing `/config` volume. Interrupted wanted jobs are finalized as deferred; interrupted maintenance jobs become a single `interrupted` maintenance outcome. Status history is compacted during normal retention housekeeping.

The dashboard has no authentication. Keep port `8765` restricted to a trusted LAN or protect it with your firewall/reverse proxy. Set `STATUS_ENABLED=false` to disable the listener. A port-binding or status-file failure is non-fatal and does not stop translations.

Completeness scanning covers regular subtitles in every language, including HI, SDH, numbered, and language-less sidecars. Files explicitly labelled `forced`, `foreign`, `signs`, or `commentary` are exempt. A file is undersized only when at least three configured density/coverage signals fail; an unavailable duration is a safe skip.

Sidecar pruning runs after queued cue repairs and during each existing-library scan. It groups files by the exact sibling video stem and proceeds only when every language in `LANGUAGES` has a validated full subtitle and the media duration is usable. Every managed-language variant is preserved, including HI, SDH, numbered, and forced tracks; forced-only tracks do not satisfy readiness. Recognized non-managed languages and unmanaged forced/foreign/signs/commentary tracks are pruned. Language-less, numeric-only, and unclassifiable files are retained unless `CLEANUP_PRUNE_UNKNOWN_SIDECARS=true`.

The default prune action moves candidates out of the media directory immediately into `/config/quarantine`, so Bazarr no longer sees them. They remain recoverable until retention housekeeping permanently purges the subtitle and its audit report after 30 days by default. Use `CLEANUP_SCAN_DRY_RUN=true` to preview candidates without moving files or triggering Bazarr.

Target variants `.et.srt`, `.et.hi.srt`, `.et.sdh.srt`, and arbitrary numbered forms such as `.et.12.srt` receive language/content validation. Lingarr output names are discovered from the files that actually changed: for example, an `.en.hi.srt` source expects and accepts `.et.hi.srt` rather than incorrectly waiting for `.et.srt`. Existing target variants prefer a matching source variant, then fall back to a plain source.

Lingarr provenance is persisted transactionally in `/config/bazarr-autotranslate.sqlite3` before submission and again when the actual output is discovered. It includes media identity, source and target paths, languages, variants, SHA-256 hashes, Lingarr job IDs, validation results, and parent/child versions created by format or cue repair. If the container restarts between Lingarr completion and local validation, the next scan recovers that relationship and can still use source-aware format or cue repair.

Source-aware validation requires an exact target path/hash record and the current source hash. A moved adjacent source is accepted only when its language and content hash still match. Changed or unproven files fall back to conservative target-only validation. Bazarr/manual subtitles are stored as external observations without an inferred source.

On the first upgraded startup, valid entries from `submitted_cache.json` and `validation_state.json` are imported once. The originals are retained as `.migrated.bak` files. Malformed rows are skipped with a warning. SQLite initialization, integrity, or write failures fail closed: new translations and source-aware repairs are deferred rather than proceeding without durable provenance.

Only one Bazarr AutoTranslate container may use a given `/config` directory. A second instance exits with an explicit lock error, preventing duplicate submissions and conflicting state updates.

A source-less subtitle whose only validation issue is `excessive_lines` is retained as `valid_with_warnings` by default and skipped on later scans while its hash is unchanged. Prompt leakage, malformed structure, wrong language/script, repetition, undersized content, or any other strong rule still makes it eligible for the configured cleanup action. No dialogue lines are joined automatically.

Quarantine retention and retry eligibility are independent. Invalid artifacts and reports remain available for audit for `QUARANTINE_ARTIFACT_RETENTION_DAYS`, including after a later success. Cue-local failures use the bounded repair path and may receive one end-of-cycle retry. Structurally unsafe target output is regenerated from the current source after persistent completed-cycle backoff. Service failures use normal cooldown and circuit protection and do not create subtitle quarantine plans. Retry state survives restart, unchanged failed hashes do not consume attempts, and exhausted plans remain visible for manual review. The legacy `CLEANUP_QUARANTINE_HOLD_DAYS` variable is accepted with a warning but no longer affects eligibility.

The application schema is additive through v13 while retaining SQLite
`user_version=8` for rollback-image compatibility. The migration ledger records
the authoritative application schema. It includes retry admission rotation,
durable repair and cue recovery state, provider and donor events, canonical series aliases,
no-progress deferrals, and owned half-open circuit leases. Existing attempts,
eligibility, circuit history, and quarantine files remain untouched. Retry claims
recovered after a crash move to the next completed cycle without consuming a
translation attempt, preventing the same no-progress batch from starving later
work. Half-open protection is claimed only immediately before submission, bound
to the Lingarr job, and released when no job was created. Before upgrading, back
up `/config/bazarr-autotranslate.sqlite3`; rolling back to an older image requires
stopping the new process first. Additive tables and dual-written circuit fields
remain available for a later re-upgrade; keep the backup as an operational safeguard.

## Code layout and compatibility

Runtime implementations live under `autotranslate/`. The historical
`Bazarr_AutoTranslate.py`, `clean_et_subs.py`, `state_store.py`, and
`status_dashboard.py` files are intentionally thin compatibility entry points.
The Docker command and standalone cleanup CLI are unchanged, and existing Python
imports resolve to the packaged implementation modules so monkey-patching and
documented integrations retain the same behavior.

Source-anchored recovery normalizes BOMs, newlines, trailing whitespace, timestamp spacing, repeated separators, and blank lines inside cues. Orphan text is folded into its preceding cue only when every numbered timestamp anchor still matches the source in order. Missing, duplicate, reordered, or mismatched anchors are never guessed.

Cue repairs share `PARALLEL_TRANSLATES` with full-file work. A waiting repair is admitted before a new translation, and the combined active count never exceeds that limit. Repair logs and status show queueing, capacity waiting, worker execution, cue number, attempt, safe HTTP status, duration, candidate validation, and completed-file validation. Subtitle text, context contents, credentials, prompts, and raw provider payloads are never logged or displayed.

## Quarantine recovery

Each quarantined subtitle has a companion `.validation.json` report containing its original path, hashes, failed cues, validation rules, repair outcome, provenance, filename classification, and—when applicable—the media duration, completeness metrics, thresholds, and failed signals.

1. Read `targetPath` and the validation issues in the report.
2. Correct the subtitle or adjust settings only for a confirmed false positive.
3. Move the subtitle back to `targetPath` and trigger a Bazarr subtitle scan.

The `/config` volume persists the SQLite state database, migration backups, dashboard history, quarantine files, provenance, cooldown state, and quarantine audit history across container recreation. Provenance artifact lineage is retained; expired cooldown-only attempts, old validation and quarantine audit records, quarantine files, reports, and logs follow the configured retention policy. Cleanup runs at startup and hourly by default.

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
