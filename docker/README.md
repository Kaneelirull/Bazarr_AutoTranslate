# Bazarr AutoTranslate Docker

Docker container that automatically translates subtitles for your Bazarr media library and cleans up incorrectly detected subtitle files.

## Features

- **Continuous Monitoring**: Automatically checks Bazarr API for missing subtitles and translates them
- **No Timeouts**: Runs until all subtitles are processed, then checks again after a configurable interval
- **Daily Cleanup**: Runs subtitle validation once per day to remove incorrectly detected files
- **Fully Configurable**: All settings via environment variables
- **Graceful Shutdown**: Properly handles container stops and restarts

## Quick Start

### 1. Clone and Setup

```bash
cd docker
cp .env.example .env
# Edit .env with your settings
nano .env
```

### 2. Configure Environment Variables

Edit `.env` file with your settings:

```env
# REQUIRED: Your Bazarr connection details
BAZARR_HOSTNAME=192.168.1.100:6767
BAZARR_APIKEY=your_api_key_here

# REQUIRED: Path to your media files
MEDIA_PATH=/mnt/media

# OPTIONAL: Translation settings
FIRST_LANG=et
SECOND_LANG=sv
CHECK_INTERVAL=300
```

### 3. Build and Run

```bash
docker-compose up -d
```

### 4. Check Logs

```bash
# Follow all logs
docker-compose logs -f

# Check just auto-translate logs
docker-compose logs -f bazarr-autotranslate

# Check cleanup logs
docker exec bazarr-autotranslate cat /var/log/bazarr-autotranslate/cleanup.log
```

## Configuration

### Environment Variables

#### Bazarr Connection (Required)
- `BAZARR_HOSTNAME`: Bazarr server hostname and port (e.g., `localhost:6767` or `192.168.1.100:6767`)
- `BAZARR_APIKEY`: Your Bazarr API key (found in Bazarr Settings → General)

#### Media Path (Required)
- `MEDIA_PATH`: Path to your media files on the host system

#### Translation Settings
- `FIRST_LANG`: Primary target language code (default: `et`)
- `SECOND_LANG`: Secondary target language code (default: `sv`)
- `MAX_PARALLEL_TRANSLATIONS`: Number of parallel translation jobs (default: `2`)
- `TRANSLATE_DELAY`: Delay between translation API calls in seconds (default: `0.3`)
- `CHECK_INTERVAL`: Seconds to wait between checking for new missing subtitles (default: `300` = 5 minutes)

#### API Settings
- `API_TIMEOUT`: API request timeout in seconds (default: `2400`)
- `CONNECT_TIMEOUT`: Connection timeout in seconds (default: `10`)

#### Cleanup Settings
- `CLEANUP_TIME`: Daily cleanup time in HH:MM format (default: `04:00`)
- `CLEANUP_ROOT`: Root directory to scan for subtitles (default: `/media`)
- `CLEANUP_MIN_CONFIDENCE`: Minimum language detection confidence (default: `0.70`)
- `CLEANUP_MIN_CHARS`: Minimum subtitle text length for detection (default: `200`)

## How It Works

### Auto-Translate Script
1. Checks Bazarr API for movies/episodes with missing subtitles
2. Downloads English subtitles if needed
3. Translates to configured target languages
4. Processes all items, then waits `CHECK_INTERVAL` seconds
5. Repeats continuously until container is stopped

### Cleanup Script
- Runs once daily at configured time (default: 04:00)
- Scans subtitle files with `.et.srt` extension
- Detects actual language using ML
- Removes or quarantines files that aren't actually Estonian
- Also removes files containing HTTP error messages

## Docker Compose with Bazarr

If you're running Bazarr in Docker on the same host:

```yaml
version: '3.8'

services:
  bazarr:
    image: lscr.io/linuxserver/bazarr:latest
    container_name: bazarr
    networks:
      - media-network
    # ... other bazarr config

  bazarr-autotranslate:
    build: ./docker
    container_name: bazarr-autotranslate
    environment:
      - BAZARR_HOSTNAME=bazarr:6767  # Use container name
      - BAZARR_APIKEY=${BAZARR_APIKEY}
    volumes:
      - /path/to/media:/media
    networks:
      - media-network
    depends_on:
      - bazarr

networks:
  media-network:
    driver: bridge
```

## Troubleshooting

### Container won't start
```bash
# Check logs
docker-compose logs bazarr-autotranslate

# Verify environment variables
docker-compose config
```

### Can't connect to Bazarr
- Verify `BAZARR_HOSTNAME` is correct
- If Bazarr is on the same Docker network, use the container name instead of `localhost`
- Check if API key is valid in Bazarr settings

### Translations not working
- Check Bazarr has translation providers configured
- Verify API timeout settings are sufficient
- Check container logs for specific errors

### Cleanup not running
- Check cron logs: `docker exec bazarr-autotranslate cat /var/log/bazarr-autotranslate/cron.log`
- Verify `CLEANUP_TIME` is in HH:MM format
- Ensure media path is correctly mounted

## Advanced Usage

### Custom Networks

If you want to use a specific Docker network:

```yaml
services:
  bazarr-autotranslate:
    networks:
      - your-network-name

networks:
  your-network-name:
    external: true
```

### Multiple Media Paths

Currently only one root path is supported. If you have separate TV and Movie directories, mount them under a common parent:

```yaml
volumes:
  - /mnt/storage/tv:/media/tv
  - /mnt/storage/movies:/media/movies
```

Then set `CLEANUP_ROOT=/media`

### Manual Cleanup Run

```bash
# Run cleanup immediately (doesn't affect scheduled runs)
docker exec bazarr-autotranslate /app/run_cleanup.sh
```

### Adjust Parallel Processing

For faster processing on powerful servers:
```env
MAX_PARALLEL_TRANSLATIONS=5
CHECK_INTERVAL=60
```

For limited resources:
```env
MAX_PARALLEL_TRANSLATIONS=1
TRANSLATE_DELAY=1.0
CHECK_INTERVAL=600
```

## Stopping the Container

```bash
# Stop gracefully
docker-compose down

# Stop and remove volumes
docker-compose down -v
```

The container will finish processing the current translation before stopping.

## Updates

To update to the latest version:

```bash
cd docker
git pull
docker-compose down
docker-compose build --no-cache
docker-compose up -d
```

## Support

For issues or questions:
1. Check container logs: `docker-compose logs -f`
2. Verify all environment variables are set correctly
3. Test Bazarr API connectivity manually
4. Open an issue on GitHub with relevant logs
