# Bazarr AutoTranslate - Docker Containerization Complete ✅

## What Was Created

A complete Docker solution for your Bazarr subtitle automation, located in the `docker/` folder.

## Key Files

### Configuration
- **`.env.example`** - Template for your settings (copy to `.env` and edit)
- **`docker-compose.yml`** - Container orchestration
- **`Dockerfile`** - Container build instructions

### Application
- **`Bazarr_AutoTranslate.py`** - Modified to run continuously (no 50-min timeout)
- **`clean_et_subs.py`** - Original cleanup script (unchanged)
- **`entrypoint.sh`** - Container startup script
- **`requirements.txt`** - Python dependencies

### Documentation
- **`README.md`** - Complete Docker usage guide
- **`MIGRATION.md`** - Detailed explanation of changes

## Major Changes from Cron Version

### 1. Continuous Operation
- **Before**: Runs once per hour via cron
- **After**: Runs continuously, checks API every 5 minutes (configurable)

### 2. No Timeouts
- **Before**: 50-minute hard limit
- **After**: Processes until done, then checks again - runs forever

### 3. Smart Checking
- Checks Bazarr API for missing subtitles
- If found: processes them all
- If none: waits CHECK_INTERVAL seconds
- Repeats infinitely

### 4. Environment-Based Config
All settings via `.env` file:
```env
BAZARR_HOSTNAME=localhost:6767
BAZARR_APIKEY=your_key
FIRST_LANG=et
SECOND_LANG=sv
CHECK_INTERVAL=300
CLEANUP_TIME=04:00
MEDIA_PATH=/path/to/media
```

### 5. Daily Cleanup
Still runs once per day at 04:00 (configurable via `CLEANUP_TIME`)

## Quick Start

```bash
cd docker

# Setup configuration
cp .env.example .env
nano .env  # Edit with your settings

# Build and start
docker-compose up -d

# View logs
docker-compose logs -f

# Stop
docker-compose down
```

## Environment Variables You Need to Set

**Required:**
- `BAZARR_HOSTNAME` - Your Bazarr server (e.g., `192.168.1.100:6767`)
- `BAZARR_APIKEY` - Your Bazarr API key
- `MEDIA_PATH` - Path to your media files

**Optional (have defaults):**
- `FIRST_LANG` - Primary language (default: `et`)
- `SECOND_LANG` - Secondary language (default: `sv`)
- `CHECK_INTERVAL` - Seconds between checks (default: `300`)
- `CLEANUP_TIME` - Daily cleanup time (default: `04:00`)
- `MAX_PARALLEL_TRANSLATIONS` - Parallel jobs (default: `2`)

## How It Works

### Main Translation Loop
```
1. Check Bazarr API for missing subtitles
2. If found:
   - Download English subs if needed
   - Translate to FIRST_LANG
   - Translate to SECOND_LANG (if set)
3. Wait CHECK_INTERVAL seconds
4. Go to step 1
```

### Daily Cleanup
```
1. At CLEANUP_TIME (default 04:00):
   - Scan all .et.srt files
   - Detect actual language
   - Remove files that aren't really Estonian
   - Remove files with HTTP errors
```

## Advantages Over Cron

1. ✅ No missed content (continuous vs hourly)
2. ✅ No timeouts (processes everything)
3. ✅ Faster response (5 min vs 1 hour)
4. ✅ Easy configuration (env vars vs code editing)
5. ✅ Better logging (Docker logs)
6. ✅ Auto-restart on failure
7. ✅ Isolated environment
8. ✅ Portable across systems

## Testing Checklist

- [ ] Copy `.env.example` to `.env`
- [ ] Set `BAZARR_HOSTNAME`
- [ ] Set `BAZARR_APIKEY`
- [ ] Set `MEDIA_PATH`
- [ ] Run `docker-compose up -d`
- [ ] Check logs: `docker-compose logs -f`
- [ ] Verify it connects to Bazarr
- [ ] Wait for it to find and process a subtitle
- [ ] Check cleanup runs at configured time

## Monitoring

```bash
# Live logs
docker-compose logs -f

# Just auto-translate
docker-compose logs -f bazarr-autotranslate

# Cleanup logs
docker exec bazarr-autotranslate cat /var/log/bazarr-autotranslate/cleanup.log

# Cron logs
docker exec bazarr-autotranslate cat /var/log/bazarr-autotranslate/cron.log
```

## Common Configurations

### Fast Processing (Powerful Server)
```env
MAX_PARALLEL_TRANSLATIONS=5
CHECK_INTERVAL=60
TRANSLATE_DELAY=0.1
```

### Gentle Processing (Shared Server)
```env
MAX_PARALLEL_TRANSLATIONS=1
CHECK_INTERVAL=600
TRANSLATE_DELAY=1.0
```

### Different Cleanup Time
```env
CLEANUP_TIME=02:30
```

## Next Steps

1. **Test it** - Deploy and monitor for a few days
2. **Tune settings** - Adjust CHECK_INTERVAL based on your needs
3. **Remove old cron** - Once stable, disable the hourly cron job
4. **Enjoy** - Let it run continuously in the background

## Support

All documentation is in the `docker/` folder:
- `README.md` - Complete usage guide
- `MIGRATION.md` - Detailed change explanation
- `.env.example` - All available settings

## Files Changed

**New files created:**
- All files in `docker/` folder are new

**Original files:**
- Untouched and still in parent directory
- Can still be used with cron if needed
- Docker version is independent

Your original setup is preserved - the Docker version is a complete separate implementation!
