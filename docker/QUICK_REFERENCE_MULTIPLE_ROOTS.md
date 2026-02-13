# Multiple CLEANUP_ROOT - Quick Reference

## Syntax

```bash
CLEANUP_ROOT=/path1:/path2:/path3
```

Separate multiple paths with colon (`:`)

## Examples

### Single Directory
```bash
CLEANUP_ROOT=/media
```

### Two Directories
```bash
CLEANUP_ROOT=/media/movies:/media/tv
```

### Three+ Directories
```bash
CLEANUP_ROOT=/media/movies:/media/tv:/media/anime
```

## Docker Compose Example

```yaml
services:
  bazarr-autotranslate:
    image: kaneelir0ll/bazarr-autotranslate:latest
    environment:
      - CLEANUP_ROOT=/media/movies:/media/tv
    volumes:
      - /host/movies:/media/movies
      - /host/tv:/media/tv
```

## Rules

✅ Use colon: `/path1:/path2`
❌ Don't use comma: `/path1,/path2`
❌ Don't use spaces: `/path1 : /path2`
❌ Don't use semicolon: `/path1;/path2`

## Testing

```bash
# Test run
docker exec bazarr-autotranslate /app/run_cleanup.sh

# Check logs
docker logs bazarr-autotranslate | grep "Cleanup Root"
```

## Full Documentation

See `MULTIPLE_CLEANUP_ROOT.md` for complete details.
