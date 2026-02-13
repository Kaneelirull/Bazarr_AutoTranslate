# Docker Migration Summary

## What Changed

### Original Setup (Cron-based)
- Two separate scripts run by cron jobs
- `Bazarr_AutoTranslate.py`: Every hour with 50-minute timeout
- `clean_et_subs.py`: Once daily at 04:00
- Manual configuration in script files
- Risk of timeouts if processing takes too long

### New Setup (Docker-based)
- **Continuous monitoring**: No more hourly cron - runs continuously
- **No timeouts**: Processes all items, checks API again, repeats forever
- **Environment-based config**: All settings via `.env` file
- **Built-in scheduler**: Daily cleanup handled by container's cron
- **Easy deployment**: Single `docker-compose up -d` command

## Key Improvements

### 1. Continuous Operation
**Old**: Runs once per hour, might miss new content
**New**: Checks API every 5 minutes (configurable), processes immediately

### 2. No Timeout Limits
**Old**: 50-minute hard limit could interrupt processing
**New**: Runs until all subtitles are processed, then waits and checks again

### 3. Configuration Management
**Old**: Edit Python files directly
**New**: All settings in `.env` file - never touch code

### 4. Reliability
**Old**: If process crashes, waits until next cron run
**New**: Docker restart policy ensures continuous operation

### 5. Deployment
**Old**: Install Python, dependencies, setup cron manually
**New**: `docker-compose up -d` - that's it

## Configuration Mapping

### Bazarr_AutoTranslate.py
| Old (hardcoded) | New (environment variable) |
|-----------------|----------------------------|
| `BAZARR_HOSTNAME = "Localhost:1337"` | `BAZARR_HOSTNAME=localhost:1337` |
| `BAZARR_APIKEY = "BAZARR_APIKEY"` | `BAZARR_APIKEY=your_key` |
| `FIRST_LANG = "et"` | `FIRST_LANG=et` |
| `SECOND_LANG = "sv"` | `SECOND_LANG=sv` |
| `MAX_PARALLEL_TRANSLATIONS = 2` | `MAX_PARALLEL_TRANSLATIONS=2` |
| `MAX_RUNTIME = 3000` | *Removed - no timeout* |
| Cron every 1h | `CHECK_INTERVAL=300` (5 min) |

### clean_et_subs.py
| Old (command-line) | New (environment variable) |
|--------------------|----------------------------|
| `--root /media/tv` | `CLEANUP_ROOT=/media` |
| `--min-confidence 0.70` | `CLEANUP_MIN_CONFIDENCE=0.70` |
| `--min-chars 200` | `CLEANUP_MIN_CHARS=200` |
| Cron at 04:00 | `CLEANUP_TIME=04:00` |

## File Structure

```
docker/
├── Dockerfile              # Container build instructions
├── docker-compose.yml      # Service definition
├── .env.example           # Configuration template
├── requirements.txt       # Python dependencies
├── entrypoint.sh          # Container startup script
├── Bazarr_AutoTranslate.py # Modified main script
├── clean_et_subs.py       # Original cleanup script
├── README.md              # Docker documentation
└── build.sh               # Build helper script
```

## Migration Steps

### 1. Setup
```bash
cd docker
cp .env.example .env
nano .env  # Edit with your settings
```

### 2. Deploy
```bash
docker-compose up -d
```

### 3. Monitor
```bash
docker-compose logs -f
```

### 4. Stop Old Cron Jobs
```bash
crontab -e
# Comment out or remove the old Bazarr entries
```

## Benefits Over Cron

1. **No missed runs**: Continuous operation catches new content immediately
2. **Better logging**: All output in Docker logs, easy to view and monitor
3. **Easier management**: Start/stop/restart with simple docker commands
4. **Isolation**: Dependencies contained, won't conflict with system
5. **Portability**: Works on any system with Docker
6. **Updates**: Pull new version, rebuild, done
7. **Configuration**: Change .env, restart container - no code editing

## Rollback Plan

If you need to go back to cron:

1. Stop Docker container: `docker-compose down`
2. Re-enable cron jobs
3. Original scripts are still in parent directory

## Next Steps

1. Test in your environment
2. Monitor logs for a few days
3. Adjust `CHECK_INTERVAL` based on your needs
4. Once stable, remove old cron setup completely

## Notes

- The Docker version checks for new subtitles every 5 minutes by default (vs 1 hour with cron)
- You can adjust this with `CHECK_INTERVAL` environment variable
- Processing happens immediately when missing subs are found
- No artificial timeouts - it processes everything, then checks again
