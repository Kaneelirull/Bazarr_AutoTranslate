# Bazarr AutoTranslate

[![Docker Hub](https://img.shields.io/docker/pulls/kaneelir0ll/bazarr-autotranslate.svg)](https://hub.docker.com/r/kaneelir0ll/bazarr-autotranslate)
[![Docker Image Size](https://img.shields.io/docker/image-size/kaneelir0ll/bazarr-autotranslate/latest)](https://hub.docker.com/r/kaneelir0ll/bazarr-autotranslate)

Automated subtitle translation service that continuously monitors your Bazarr instance, translates missing subtitles, and cleans up incorrectly detected subtitle files.

## Features

- 🔄 **Continuous Monitoring**: Automatically checks Bazarr API for missing subtitles every 5 minutes (configurable)
- ⚡ **No Timeouts**: Processes all subtitles until complete, then checks again
- 🧹 **Daily Cleanup**: Validates and removes incorrectly detected subtitle files
- 🌍 **Multi-Language**: Supports translation from English to any target languages
- ⚙️ **Fully Configurable**: All settings via environment variables
- 🛡️ **Production Ready**: Graceful shutdown handling and automatic restart on failure

## Quick Start

### Using Docker Run

```bash
docker run -d \
  --name bazarr-autotranslate \
  --restart unless-stopped \
  -e BAZARR_HOSTNAME=192.168.1.100:6767 \
  -e BAZARR_APIKEY=your_api_key_here \
  -e FIRST_LANG=et \
  -e SECOND_LANG=sv \
  -v /path/to/your/media:/media \
  kaneelir0ll/bazarr-autotranslate:latest
```

### Using Docker Compose

Create a `docker-compose.yml` file:

```yaml
version: '3.8'

services:
  bazarr-autotranslate:
    image: kaneelir0ll/bazarr-autotranslate:latest
    container_name: bazarr-autotranslate
    restart: unless-stopped
    environment:
      # Required
      - BAZARR_HOSTNAME=192.168.1.100:6767
      - BAZARR_APIKEY=your_api_key_here
      
      # Optional (with defaults)
      - FIRST_LANG=et
      - SECOND_LANG=sv
      - CHECK_INTERVAL=300
      - CLEANUP_TIME=04:00
    
    volumes:
      - /path/to/your/media:/media
```

Then run:

```bash
docker-compose up -d
```

## Environment Variables

### Required Settings

| Variable | Description | Example |
|----------|-------------|---------|
| `BAZARR_HOSTNAME` | Bazarr server hostname and port | `localhost:6767` or `192.168.1.100:6767` |
| `BAZARR_APIKEY` | Your Bazarr API key (found in Bazarr Settings → General) | `abc123def456...` |

### Translation Settings (Optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `FIRST_LANG` | `et` | Primary target language code |
| `SECOND_LANG` | `sv` | Secondary target language code (leave empty to disable) |
| `MAX_PARALLEL_TRANSLATIONS` | `2` | Number of parallel translation jobs |
| `TRANSLATE_DELAY` | `0.3` | Delay between translation API calls (seconds) |
| `CHECK_INTERVAL` | `300` | Seconds to wait between checking for new missing subtitles |

### API Settings (Optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `API_TIMEOUT` | `2400` | API request timeout in seconds |
| `CONNECT_TIMEOUT` | `10` | Connection timeout in seconds |

### Cleanup Settings (Optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `CLEANUP_TIME` | `04:00` | Daily cleanup time in HH:MM format |
| `CLEANUP_ROOT` | `/media` | Root directory to scan for subtitles |
| `CLEANUP_MIN_CONFIDENCE` | `0.70` | Minimum language detection confidence |
| `CLEANUP_MIN_CHARS` | `200` | Minimum subtitle text length for detection |

## Volume Mounts

Mount your media directory to `/media` in the container:

```bash
-v /path/to/your/movies:/media/movies
-v /path/to/your/tv:/media/tv
```

Or mount a parent directory:

```bash
-v /mnt/storage:/media
```

The cleanup script will scan all subdirectories under the mounted path.

## How It Works

### Auto-Translate Loop

1. Checks Bazarr API for movies/episodes with missing subtitles
2. Downloads English subtitles if needed
3. Translates to `FIRST_LANG`
4. Translates to `SECOND_LANG` (if configured)
5. Waits `CHECK_INTERVAL` seconds
6. Repeats continuously

### Daily Cleanup

- Runs once daily at `CLEANUP_TIME` (default: 04:00)
- Scans subtitle files with configured language extension (e.g., `.et.srt`)
- Detects actual language using ML
- Removes files that aren't actually in the target language
- Also removes files containing HTTP error messages

## Usage Examples

### Basic Setup

Minimal configuration with Estonian as target language:

```bash
docker run -d \
  --name bazarr-autotranslate \
  -e BAZARR_HOSTNAME=localhost:6767 \
  -e BAZARR_APIKEY=your_key \
  -e FIRST_LANG=et \
  -v /mnt/media:/media \
  kaneelir0ll/bazarr-autotranslate:latest
```

### Multiple Languages

Translate to both Estonian and Swedish:

```bash
docker run -d \
  --name bazarr-autotranslate \
  -e BAZARR_HOSTNAME=localhost:6767 \
  -e BAZARR_APIKEY=your_key \
  -e FIRST_LANG=et \
  -e SECOND_LANG=sv \
  -v /mnt/media:/media \
  kaneelir0ll/bazarr-autotranslate:latest
```

### Fast Processing

For powerful servers with more resources:

```bash
docker run -d \
  --name bazarr-autotranslate \
  -e BAZARR_HOSTNAME=localhost:6767 \
  -e BAZARR_APIKEY=your_key \
  -e FIRST_LANG=et \
  -e MAX_PARALLEL_TRANSLATIONS=5 \
  -e CHECK_INTERVAL=60 \
  -v /mnt/media:/media \
  kaneelir0ll/bazarr-autotranslate:latest
```

### With Bazarr in Same Docker Network

If Bazarr is running in Docker on the same network:

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
    image: kaneelir0ll/bazarr-autotranslate:latest
    container_name: bazarr-autotranslate
    environment:
      - BAZARR_HOSTNAME=bazarr:6767  # Use container name
      - BAZARR_APIKEY=${BAZARR_APIKEY}
      - FIRST_LANG=et
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

## Monitoring

### View Live Logs

```bash
# All logs
docker logs -f bazarr-autotranslate

# Last 100 lines
docker logs --tail 100 bazarr-autotranslate
```

### Check Cleanup Logs

```bash
docker exec bazarr-autotranslate cat /var/log/bazarr-autotranslate/cleanup.log
```

### Check Cron Logs

```bash
docker exec bazarr-autotranslate cat /var/log/bazarr-autotranslate/cron.log
```

## Troubleshooting

### Container won't start

```bash
# Check logs for errors
docker logs bazarr-autotranslate

# Verify environment variables
docker inspect bazarr-autotranslate | grep -A 20 Env
```

### Can't connect to Bazarr

- Verify `BAZARR_HOSTNAME` is correct
- If Bazarr is on the same Docker network, use the container name
- Check if API key is valid in Bazarr settings
- Ensure Bazarr is accessible from the container:
  ```bash
  docker exec bazarr-autotranslate curl http://your-bazarr:6767/api
  ```

### Translations not working

- Check Bazarr has translation providers configured
- Verify API timeout settings are sufficient
- Check container logs for specific errors
- Ensure target languages are supported by Bazarr

### Cleanup not running

- Check cron logs: `docker exec bazarr-autotranslate cat /var/log/bazarr-autotranslate/cron.log`
- Verify `CLEANUP_TIME` is in HH:MM format
- Ensure media path is correctly mounted and accessible

## Manual Operations

### Run Cleanup Immediately

```bash
docker exec bazarr-autotranslate /app/run_cleanup.sh
```

### Restart Container

```bash
docker restart bazarr-autotranslate
```

### Update to Latest Version

```bash
docker pull kaneelir0ll/bazarr-autotranslate:latest
docker stop bazarr-autotranslate
docker rm bazarr-autotranslate
# Then run your docker run or docker-compose up command again
```

## Performance Tuning

### For Powerful Servers

```yaml
environment:
  - MAX_PARALLEL_TRANSLATIONS=5
  - TRANSLATE_DELAY=0.1
  - CHECK_INTERVAL=60
```

### For Limited Resources

```yaml
environment:
  - MAX_PARALLEL_TRANSLATIONS=1
  - TRANSLATE_DELAY=1.0
  - CHECK_INTERVAL=600
```

### Adjust Cleanup Timing

```yaml
environment:
  - CLEANUP_TIME=02:30  # Run at 2:30 AM instead of 4:00 AM
```

## Supported Languages

The container supports any language code that Bazarr supports. Common codes:

- `en` - English
- `et` - Estonian
- `sv` - Swedish
- `fi` - Finnish
- `no` - Norwegian
- `da` - Danish
- `de` - German
- `fr` - French
- `es` - Spanish
- `pt` - Portuguese
- `ru` - Russian

## Architecture

- **Base Image**: Python 3.11 slim
- **Size**: ~400MB (includes ML models for language detection)
- **Platform**: linux/amd64

## Source Code

GitHub: [Kaneelirull/Bazarr_AutoTranslate](https://github.com/Kaneelirull/Bazarr_AutoTranslate)

## License

MIT License - See repository for details

## Support

For issues or questions:

1. Check container logs: `docker logs bazarr-autotranslate`
2. Verify all environment variables are set correctly
3. Test Bazarr API connectivity manually
4. Open an issue on [GitHub](https://github.com/Kaneelirull/Bazarr_AutoTranslate/issues)

---

**Made with ❤️ for automated subtitle management**
