# Bazarr AutoTranslate

Continuously monitors Bazarr for missing subtitles and translates them using Lingarr (local AI). Only works with subtitles already on disk — it never asks Bazarr to search or download.

## How it works

1. Fetches all wanted episodes and movies from Bazarr
2. For each item, checks which languages from your `LANGUAGES` list are available on disk
3. Uses the first available language as the source and translates to all missing ones
4. Polls Bazarr every 20s to confirm the translated subtitle appeared
5. Logs a per-cycle summary, then waits `CHECK_INTERVAL` before scanning again

Translation timeout is calculated dynamically from the source subtitle's line count (~1.1s per dialogue line), so short episodes don't wait unnecessarily.

## Requirements

- [Bazarr](https://www.bazarr.media/) with Lingarr configured as a translation provider
- [Lingarr](https://github.com/lingarr-translate/lingarr) running and reachable

## Setup

```bash
cp .env.example .env
# Edit .env with your values
docker compose up -d
```

## Environment Variables

### Required

| Variable | Description |
|---|---|
| `BAZARR_URL` | Bazarr URL, e.g. `http://192.168.1.100:6767` |
| `BAZARR_API_KEY` | Bazarr API key (Settings → General) |
| `MEDIA_PATH` | Host path to your media, mounted as `/media` in the container |

### Languages

| Variable | Default | Description |
|---|---|---|
| `LANGUAGES` | `en,et,sv` | Comma-separated language codes. Order matters — first available is used as the translation source. |

### Lingarr

| Variable | Default | Description |
|---|---|---|
| `LINGARR_URL` | `http://lingarr:8080` | Lingarr URL |
| `LINGARR_API_KEY` | *(empty)* | API key if Lingarr requires auth; omit if not needed |

### Timing

| Variable | Default | Description |
|---|---|---|
| `PARALLEL_TRANSLATES` | `1` | Concurrent translation workers |
| `CHECK_INTERVAL` | `1200` | Seconds between full scans (default: 20 min) |
| `POLL_INTERVAL` | `20` | Seconds between checks while waiting for a translation |
| `POLL_TIMEOUT` | `600` | Minimum poll timeout per translation in seconds; extended automatically based on subtitle length |
| `CONNECT_TIMEOUT` | `10` | HTTP connect timeout in seconds |

### Subtitle Cleanup

Runs once daily via cron (inside the container). Scans subtitle files, detects their actual language using ML, and removes ones that don't match.

| Variable | Default | Description |
|---|---|---|
| `CLEANUP_TIME` | `04:00` | Daily cleanup time (HH:MM) |
| `CLEANUP_ROOT` | `/media` | Directory to scan. Multiple paths: `/media/movies:/media/tv` |
| `CLEANUP_MIN_CONFIDENCE` | `0.70` | Minimum language detection confidence to keep a file |
| `CLEANUP_MIN_CHARS` | `200` | Minimum subtitle text length required for detection |

## Logs

```bash
# Follow main process
docker compose logs -f bazarr-autotranslate

# Check cleanup history
docker exec bazarr-autotranslate cat /var/log/bazarr-autotranslate/cleanup.log

# Run cleanup immediately
docker exec bazarr-autotranslate /app/run_cleanup.sh
```

## Example cycle output

```
===== Cycle #1 =====
[INFO] Lingarr active queue at cycle start: 0
[INFO] Processing 2 item(s) with 1 worker(s)...
[INFO] Modern Family - Pilot: source=en, targets=['et', 'sv']
[INFO] Source has 312 dialogue lines — estimated ~343s, timeout set to 600s
[TRANSLATE] Modern Family - Pilot: en -> et
[POLL] [episodes:26443] Waiting for 'et'... 20s elapsed, 580s remaining
[OK] [episodes:26443] 'et' appeared after 340s
[TRANSLATE] Modern Family - Pilot: en -> sv
[OK] [episodes:26443] 'sv' appeared after 295s

===== Cycle #1 Summary =====
  Submitted  : 2
  Completed  : 2
  Timed out  : 0
  Failed     : 0
  Completed translations:
    - Modern Family - Pilot: en -> et
    - Modern Family - Pilot: en -> sv
  Lingarr active queue now: 0
[INFO] Next cycle in 1200s...
```

## Stopping

```bash
docker compose down
```

The container finishes its current translation before stopping.
