# Multiple CLEANUP_ROOT Support

## Feature Overview

The cleanup script now supports scanning multiple directories for subtitle files by specifying multiple paths in the `CLEANUP_ROOT` environment variable.

## How to Use

### Single Directory (Original Behavior)

```bash
CLEANUP_ROOT=/media
```

This will scan `/media` and all subdirectories.

### Multiple Directories (New Feature)

Separate multiple paths with a colon (`:`):

```bash
CLEANUP_ROOT=/media/movies:/media/tv:/media/anime
```

This will scan all three directories and their subdirectories.

## Docker Compose Example

```yaml
version: '3.8'

services:
  bazarr-autotranslate:
    image: kaneelir0ll/bazarr-autotranslate:latest
    container_name: bazarr-autotranslate
    restart: unless-stopped
    environment:
      - BAZARR_HOSTNAME=192.168.1.100:6767
      - BAZARR_APIKEY=your_api_key_here
      - FIRST_LANG=et
      - SECOND_LANG=sv
      
      # Cleanup multiple directories
      - CLEANUP_ROOT=/media/movies:/media/tv:/media/anime
      - CLEANUP_TIME=04:00
    
    volumes:
      - /path/to/movies:/media/movies
      - /path/to/tv:/media/tv
      - /path/to/anime:/media/anime
```

## Docker Run Example

```bash
docker run -d \
  --name bazarr-autotranslate \
  --restart unless-stopped \
  -e BAZARR_HOSTNAME=192.168.1.100:6767 \
  -e BAZARR_APIKEY=your_api_key_here \
  -e FIRST_LANG=et \
  -e CLEANUP_ROOT=/media/movies:/media/tv:/media/anime \
  -v /mnt/storage/movies:/media/movies \
  -v /mnt/storage/tv:/media/tv \
  -v /mnt/storage/anime:/media/anime \
  kaneelir0ll/bazarr-autotranslate:latest
```

## Use Cases

### 1. Separate Movie and TV Directories

```bash
CLEANUP_ROOT=/media/movies:/media/tv
```

```yaml
volumes:
  - /mnt/plex/movies:/media/movies
  - /mnt/plex/tv:/media/tv
```

### 2. Multiple Storage Locations

```bash
CLEANUP_ROOT=/mnt/storage1:/mnt/storage2:/mnt/nas
```

```yaml
volumes:
  - /mnt/ssd/media:/mnt/storage1
  - /mnt/hdd/media:/mnt/storage2
  - /mnt/network/media:/mnt/nas
```

### 3. Different Content Types

```bash
CLEANUP_ROOT=/media/movies:/media/tv:/media/anime:/media/documentaries
```

### 4. Mixed Language Content

If you have separate directories for different language content but want to clean Estonian subtitles across all:

```bash
CLEANUP_ROOT=/media/estonian:/media/international:/media/mixed
```

## Important Notes

### Path Separator

- **Use colon (`:`)** to separate paths: `/path1:/path2:/path3`
- **Do NOT use spaces** around the colons: ❌ `/path1 : /path2`
- **Do NOT use commas**: ❌ `/path1,/path2`

### Valid Examples

✅ `CLEANUP_ROOT=/media/movies:/media/tv`
✅ `CLEANUP_ROOT=/mnt/storage1:/mnt/storage2:/mnt/storage3`
✅ `CLEANUP_ROOT=/media`

### Invalid Examples

❌ `CLEANUP_ROOT=/media/movies, /media/tv` (comma instead of colon)
❌ `CLEANUP_ROOT=/media/movies : /media/tv` (spaces around colon)
❌ `CLEANUP_ROOT=/media/movies;/media/tv` (semicolon instead of colon)

### Mount Points Must Exist

All paths specified in `CLEANUP_ROOT` must be properly mounted as volumes:

```yaml
environment:
  - CLEANUP_ROOT=/media/movies:/media/tv  # Specify paths here

volumes:
  - /host/movies:/media/movies  # Must mount here
  - /host/tv:/media/tv          # Must mount here
```

## Testing

To test if your paths are configured correctly:

### Manual Test Run

```bash
# Run cleanup immediately to test
docker exec bazarr-autotranslate /app/run_cleanup.sh
```

### Check Logs

```bash
# Check what directories are being scanned
docker logs bazarr-autotranslate | grep "Cleanup Root"

# Check cleanup logs
docker exec bazarr-autotranslate cat /var/log/bazarr-autotranslate/cleanup.log
```

### Verify Paths Exist

```bash
# List the directories inside the container
docker exec bazarr-autotranslate ls -la /media/movies
docker exec bazarr-autotranslate ls -la /media/tv
```

## Performance Considerations

### More Directories = Longer Scan Time

Scanning multiple large directories will take longer. Consider:

1. **Adjust CLEANUP_TIME** to run during off-peak hours:
   ```bash
   CLEANUP_TIME=03:00  # 3 AM when nobody's watching
   ```

2. **Limit depth** if you have very deep directory structures by organizing content better

3. **Exclude unnecessary paths** - only include directories that actually contain `.et.srt` files

### Resource Usage

The cleanup script processes files sequentially, so memory usage remains constant regardless of directory count. CPU usage is minimal during language detection.

## Troubleshooting

### Issue: "Directory not found" in logs

**Cause**: Path specified in `CLEANUP_ROOT` is not mounted as a volume

**Solution**: Ensure all paths in `CLEANUP_ROOT` are mounted:
```yaml
environment:
  - CLEANUP_ROOT=/media/movies:/media/tv
volumes:
  - /your/movies:/media/movies  # ← Add this
  - /your/tv:/media/tv          # ← Add this
```

### Issue: Only one directory is being scanned

**Cause**: Incorrect separator or formatting

**Solution**: Use colon (`:`) with no spaces:
```bash
# Correct
CLEANUP_ROOT=/media/movies:/media/tv

# Wrong
CLEANUP_ROOT=/media/movies, /media/tv
```

### Issue: Cleanup takes too long

**Cause**: Too many directories or files to scan

**Solutions**:
1. Reduce the number of directories
2. Move cleanup to overnight hours: `CLEANUP_TIME=02:00`
3. Increase `CLEANUP_MIN_CHARS` to skip more files: `CLEANUP_MIN_CHARS=500`

## Migration from Single to Multiple Paths

If you're currently using a single `CLEANUP_ROOT`:

**Before:**
```yaml
environment:
  - CLEANUP_ROOT=/media
volumes:
  - /mnt/storage:/media
```

**After (no change needed - backwards compatible):**
```yaml
environment:
  - CLEANUP_ROOT=/media
volumes:
  - /mnt/storage:/media
```

**After (with multiple specific paths):**
```yaml
environment:
  - CLEANUP_ROOT=/media/movies:/media/tv
volumes:
  - /mnt/storage/movies:/media/movies
  - /mnt/storage/tv:/media/tv
```

## Complete Example Configuration

```yaml
version: '3.8'

services:
  bazarr-autotranslate:
    image: kaneelir0ll/bazarr-autotranslate:1.0.2
    container_name: bazarr-autotranslate
    restart: unless-stopped
    environment:
      # Bazarr Connection
      - BAZARR_HOSTNAME=http://192.168.1.100:6767
      - BAZARR_APIKEY=abc123def456
      
      # Translation Settings
      - FIRST_LANG=et
      - SECOND_LANG=sv
      - CHECK_INTERVAL=300
      
      # Cleanup Settings - Multiple Directories
      - CLEANUP_ROOT=/media/movies:/media/tv:/media/anime
      - CLEANUP_TIME=04:00
      - CLEANUP_MIN_CONFIDENCE=0.70
      - CLEANUP_MIN_CHARS=200
    
    volumes:
      # Mount all directories specified in CLEANUP_ROOT
      - /mnt/nas/movies:/media/movies
      - /mnt/nas/tv:/media/tv
      - /mnt/local/anime:/media/anime
      # Optional: logs
      - ./logs:/var/log/bazarr-autotranslate
```

---

**Version**: 1.0.2
**Feature**: Multiple CLEANUP_ROOT support added
**Backwards Compatible**: Yes - single path still works
