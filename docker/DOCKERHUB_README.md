# Bazarr AutoTranslate

[![Docker Hub](https://img.shields.io/docker/pulls/kaneelir0ll/bazarr-autotranslate.svg)](https://hub.docker.com/r/kaneelir0ll/bazarr-autotranslate)
[![Docker Image Size](https://img.shields.io/docker/image-size/kaneelir0ll/bazarr-autotranslate/latest)](https://hub.docker.com/r/kaneelir0ll/bazarr-autotranslate)

Bazarr AutoTranslate watches Bazarr's wanted subtitle queues, submits translations
to Lingarr, validates the resulting SRT files, and asks Bazarr to rescan changed
media. It also performs scheduled validation and quarantine of malformed,
contaminated, truncated, or unmanaged subtitle sidecars.

This image is the current Lingarr-based application. It does not use the legacy
`BAZARR_HOSTNAME`, `BAZARR_APIKEY`, `FIRST_LANG`, or
`MAX_PARALLEL_TRANSLATIONS` settings.

## Features

- Bazarr movie and episode wanted-queue monitoring
- Lingarr file translation with a fail-closed concurrency limit
- Source-provenance and target-language validation
- Completeness checks using `ffprobe`, cue density, text density, and timeline coverage
- Safe format recovery and targeted cue repair
- Quarantine retention and repeat-invalid-output protection
- Optional pruning of recognized unmanaged subtitle sidecars
- Read-only translation status dashboard on port `8765`
- Generated subtitle ownership normalized to UID/GID `568:568`, mode `0664`

## Requirements

- A reachable Bazarr instance and API key
- A reachable Lingarr instance
- The same media library visible to Bazarr AutoTranslate and Lingarr at the same
  in-container path, normally `/media`
- A host filesystem that permits the container to set subtitle ownership to
  `568:568`

Path identity matters: if Bazarr reports `/media/movies/example.mkv`, both this
container and Lingarr must see that file as `/media/movies/example.mkv`.

## Docker Compose

The repository includes a complete
[`docker-compose.yml`](https://github.com/Kaneelirull/Bazarr_AutoTranslate/blob/main/docker/docker-compose.yml).
Create a `.env` beside it:

```env
BAZARR_URL=http://192.168.1.100:6767
BAZARR_API_KEY=replace-with-bazarr-api-key
LINGARR_URL=http://lingarr:8080
LINGARR_API_KEY=
MEDIA_PATH=/mnt/tank/media

LANGUAGES=en,et,sv
PARALLEL_TRANSLATES=1
TZ=Europe/Tallinn
```

Then start the stack:

```bash
docker compose up -d
docker compose logs -f bazarr-autotranslate
```

The bundled Compose file gives Lingarr and Bazarr AutoTranslate the same
`MEDIA_PATH:/media` mount and sets Lingarr's `MAX_CONCURRENT_JOBS` to the same
value as `PARALLEL_TRANSLATES`.

## Docker Run

Use this form when Bazarr and Lingarr already run elsewhere:

```bash
docker run -d \
  --name bazarr-autotranslate \
  --restart unless-stopped \
  -e BAZARR_URL=http://192.168.1.100:6767 \
  -e BAZARR_API_KEY=replace-with-bazarr-api-key \
  -e LINGARR_URL=http://192.168.1.100:8080 \
  -e LINGARR_API_KEY= \
  -e LANGUAGES=en,et,sv \
  -e PARALLEL_TRANSLATES=1 \
  -e TZ=Europe/Tallinn \
  -v /mnt/tank/media:/media \
  -v bazarr-autotranslate-data:/config \
  -v /mnt/tank/app-logs/bazarr-autotranslate:/var/log/bazarr-autotranslate \
  -p 8765:8765 \
  kaneelir0ll/bazarr-autotranslate:latest
```

## Core settings

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `BAZARR_URL` | Yes | — | Bazarr base URL, including scheme and port |
| `BAZARR_API_KEY` | Yes | — | Bazarr API key |
| `LINGARR_URL` | Yes | — | Lingarr base URL, including scheme and port |
| `LINGARR_API_KEY` | No | empty | Lingarr API key when authentication is enabled |
| `LANGUAGES` | No | `en,et,sv` | Managed languages in source-priority order |
| `PARALLEL_TRANSLATES` | No | `1` | Maximum verified active translations |
| `CHECK_INTERVAL` | No | `1200` | Seconds between wanted-queue cycles |
| `CONNECT_TIMEOUT` | No | `10` | External API timeout in seconds |
| `POLL_INTERVAL` | No | `20` | Lingarr job polling interval |
| `POLL_TIMEOUT` | No | `900` | Base translation timeout |
| `RESUBMIT_COOLDOWN` | No | `3600` | Duplicate-submission cooldown |

API connection failures are retried with bounded backoff. Bazarr category
failures are reported as degraded cycles instead of empty queues, and Lingarr
capacity failures defer work instead of submitting without a verified slot.

## Cleanup settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `CLEANUP_LANGUAGES` | `et` | Languages receiving full content validation |
| `CLEANUP_SCAN_EXISTING` | `true` | Enable scheduled existing-library scans |
| `CLEANUP_SCAN_INTERVAL` | `21600` | Seconds between existing-library scans |
| `CLEANUP_SCAN_DRY_RUN` | `false` | Report scan actions without changing files |
| `CLEANUP_ROOT` | `/media` | Root path or path-separated roots to scan |
| `CLEANUP_ACTION` | `quarantine` | `quarantine`, `delete`, or `report` |
| `CLEANUP_QUARANTINE_DIR` | `/config/quarantine` | Quarantine destination |
| `CLEANUP_QUARANTINE_HOLD_DAYS` | `30` | Repeat-invalid-output hold duration |
| `CLEANUP_REPAIR_ENABLED` | `true` | Enable targeted Lingarr cue repair |
| `CLEANUP_MAX_REPAIR_ATTEMPTS` | `5` | Attempts per invalid cue; attempt one uses context and attempts 2–5 do not |
| `CLEANUP_REPAIR_CONTEXT_LINES` | `5` | Surrounding source cues supplied on the first attempt |
| `CLEANUP_FORMAT_REPAIR_ENABLED` | `true` | Enable safe local SRT normalization |
| `CLEANUP_UNDERSIZED_ENABLED` | `true` | Enable completeness validation |
| `CLEANUP_PRUNE_EXTRA_LANGUAGES` | `true` | Prune recognized unmanaged languages once managed subtitles are valid |

Every repairable cue is attempted before the file is declared unrepairable.
Attempt one includes source context; attempts two through five run without
context. If any cue still fails, the untouched original subtitle is handled by
`CLEANUP_ACTION` rather than installing a partially repaired candidate.

Additional validation thresholds are documented in the repository's
[`docker/README.md`](https://github.com/Kaneelirull/Bazarr_AutoTranslate/blob/main/docker/README.md)
and `.env.example`.

## Status dashboard

The dashboard is enabled by default:

```text
http://HOST:8765
```

Relevant settings are `STATUS_ENABLED`, `STATUS_BIND`, `STATUS_PORT`,
`STATUS_HISTORY_RETENTION_DAYS`, and `STATUS_RECENT_LIMIT`. The dashboard is
read-only and requires manual refresh.

## Persistent paths

| Container path | Purpose |
| --- | --- |
| `/media` | Shared media and subtitle library |
| `/config` | SQLite provenance state, migration backups, dashboard history, quarantine |
| `/var/log/bazarr-autotranslate` | Daily application logs |

Do not run the application container as an arbitrary non-root user. It must be
able to correct Lingarr-created subtitle ownership to the managed
`568:568/0664` contract.

Correctness-critical state is stored in
`/config/bazarr-autotranslate.sqlite3`. It transactionally records submission
cooldowns, exact source/target hashes, Lingarr outputs, validation results,
repair lineage, and quarantine holds. Existing `submitted_cache.json` and
`validation_state.json` files are imported once and preserved as
`.migrated.bak` backups.

Only one application container may use a given `/config` directory. A second
instance exits instead of risking duplicate translations. If SQLite cannot be
opened, verified, or updated, translation and repair actions fail closed.

## Updating

```bash
docker compose pull
docker compose up -d
```

For locally built deployments:

```bash
git pull
docker compose build --pull
docker compose up -d
```

## Troubleshooting

- **Bazarr queue is degraded:** verify `BAZARR_URL`, the API key, and container
  network access. An outage is logged explicitly and is not treated as no work.
- **Translations are deferred:** verify Lingarr's active endpoint and API key.
  The application fails closed when capacity cannot be checked.
- **Lingarr cannot read a subtitle:** verify both containers mount the same host
  directory at the same `/media` path.
- **Subtitle ownership remains root:** verify the media filesystem allows
  `chown` from the application container and inspect the logged permission error.
- **Bazarr does not detect output:** verify path identity, then trigger a Bazarr
  subtitle scan and inspect the application logs.

Source: [Kaneelirull/Bazarr_AutoTranslate](https://github.com/Kaneelirull/Bazarr_AutoTranslate)
