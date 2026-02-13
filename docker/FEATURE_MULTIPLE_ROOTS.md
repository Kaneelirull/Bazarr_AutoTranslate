# Feature Added: Multiple CLEANUP_ROOT Support ✅

## Summary

Version **1.0.2** now supports scanning multiple directories for subtitle cleanup by specifying multiple paths in the `CLEANUP_ROOT` environment variable.

## What Changed

### Before (Single Directory)
```bash
CLEANUP_ROOT=/media
```
Only one directory could be specified.

### After (Multiple Directories)
```bash
CLEANUP_ROOT=/media/movies:/media/tv:/media/anime
```
Multiple directories can now be specified, separated by colons (`:`).

## How It Works

The entrypoint script now:
1. Reads the `CLEANUP_ROOT` variable
2. Splits it by colon (`:`) separator
3. Passes each directory as a separate `--root` argument to the cleanup script

The cleanup script (`clean_et_subs.py`) already supported multiple root directories - this update just makes it accessible via the Docker environment variable.

## Usage Examples

### Docker Compose

```yaml
version: '3.8'

services:
  bazarr-autotranslate:
    image: kaneelir0ll/bazarr-autotranslate:latest
    environment:
      - BAZARR_HOSTNAME=http://192.168.1.100:6767
      - BAZARR_APIKEY=your_api_key_here
      - FIRST_LANG=et
      
      # Multiple cleanup directories
      - CLEANUP_ROOT=/media/movies:/media/tv:/media/anime
      - CLEANUP_TIME=04:00
    
    volumes:
      - /mnt/storage/movies:/media/movies
      - /mnt/storage/tv:/media/tv
      - /mnt/storage/anime:/media/anime
```

### Docker Run

```bash
docker run -d \
  --name bazarr-autotranslate \
  -e BAZARR_HOSTNAME=http://192.168.1.100:6767 \
  -e BAZARR_APIKEY=your_api_key \
  -e FIRST_LANG=et \
  -e CLEANUP_ROOT=/media/movies:/media/tv:/media/anime \
  -v /mnt/movies:/media/movies \
  -v /mnt/tv:/media/tv \
  -v /mnt/anime:/media/anime \
  kaneelir0ll/bazarr-autotranslate:latest
```

## Important Rules

### ✅ Valid Formats

```bash
# Single directory (backwards compatible)
CLEANUP_ROOT=/media

# Multiple directories
CLEANUP_ROOT=/media/movies:/media/tv

# Many directories
CLEANUP_ROOT=/path1:/path2:/path3:/path4
```

### ❌ Invalid Formats

```bash
# Don't use commas
CLEANUP_ROOT=/media/movies,/media/tv

# Don't use spaces around colons
CLEANUP_ROOT=/media/movies : /media/tv

# Don't use semicolons
CLEANUP_ROOT=/media/movies;/media/tv
```

## Files Modified

1. ✅ `docker/entrypoint.sh` - Added colon-separated path parsing
2. ✅ `docker/.env.example` - Added comment about multiple paths
3. ✅ Created `MULTIPLE_CLEANUP_ROOT.md` - Full documentation

## Backwards Compatibility

✅ **Fully backwards compatible**

Single directory configurations still work exactly as before:
```bash
CLEANUP_ROOT=/media
```

## Docker Hub

New version pushed:
- `kaneelir0ll/bazarr-autotranslate:latest` (updated)
- `kaneelir0ll/bazarr-autotranslate:1.0.2` (new tag)

## Testing

Test your configuration:

```bash
# Run cleanup immediately
docker exec bazarr-autotranslate /app/run_cleanup.sh

# Check what directories are being scanned
docker logs bazarr-autotranslate | grep "Cleanup Root"

# Verify paths exist in container
docker exec bazarr-autotranslate ls -la /media/movies
docker exec bazarr-autotranslate ls -la /media/tv
```

## Use Cases

### Separate Media Types
```bash
CLEANUP_ROOT=/media/movies:/media/tv:/media/anime
```

### Multiple Storage Locations
```bash
CLEANUP_ROOT=/mnt/ssd:/mnt/hdd:/mnt/nas
```

### Different Language Collections
```bash
CLEANUP_ROOT=/media/estonian:/media/mixed:/media/international
```

## Update Instructions

Pull the latest version:
```bash
docker pull kaneelir0ll/bazarr-autotranslate:latest
```

Update your environment variable:
```yaml
environment:
  - CLEANUP_ROOT=/media/movies:/media/tv  # Add your paths
```

Restart container:
```bash
docker-compose down
docker-compose up -d
```

## Changelog

### Version 1.0.2 (2025-02-13)
- **Added**: Multiple CLEANUP_ROOT support with colon separator
- **Added**: Comprehensive documentation in MULTIPLE_CLEANUP_ROOT.md
- **Improved**: More flexible cleanup directory configuration

### Version 1.0.1 (2025-02-13)
- **Fixed**: Double HTTP protocol issue
- **Added**: HTTPS support

### Version 1.0.0 (2025-02-13)
- Initial Docker Hub release

---

**Feature Complete!** You can now scan multiple directories for subtitle cleanup. 🎉
