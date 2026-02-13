# Bug Fix: Double HTTP Protocol Issue ✅

## Problem

When setting `BAZARR_HOSTNAME` with a protocol prefix like:
```bash
BAZARR_HOSTNAME=http://192.168.0.100:30046
```

The URL was being constructed incorrectly as:
```
http://http://192.168.0.100:30046/api/...
```

## Root Cause

The `get_api_url()` function was hardcoding `http://` before the hostname without checking if the protocol was already present:

```python
def get_api_url(endpoint):
    """Construct the full API URL for a given endpoint."""
    return f"http://{BAZARR_HOSTNAME}/api/{endpoint}"
```

## Solution

Updated the `get_api_url()` function to check if the hostname already includes a protocol:

```python
def get_api_url(endpoint):
    """Construct the full API URL for a given endpoint."""
    # Check if hostname already has http:// or https://
    if BAZARR_HOSTNAME.startswith(('http://', 'https://')):
        base_url = BAZARR_HOSTNAME
    else:
        base_url = f"http://{BAZARR_HOSTNAME}"
    return f"{base_url}/api/{endpoint}"
```

## Files Updated

1. ✅ `docker/Bazarr_AutoTranslate.py` (Docker version)
2. ✅ `Bazarr_AutoTranslate.py` (Standalone version)

## Docker Images Updated

New version **1.0.1** has been built and pushed to Docker Hub:

- `kaneelir0ll/bazarr-autotranslate:latest` (updated)
- `kaneelir0ll/bazarr-autotranslate:1.0.1` (new tag)

## Now Supports All Three Formats

The fix now correctly handles all hostname formats:

### 1. With HTTP Protocol
```bash
BAZARR_HOSTNAME=http://192.168.0.100:30046
```
Result: `http://192.168.0.100:30046/api/...` ✅

### 2. With HTTPS Protocol
```bash
BAZARR_HOSTNAME=https://192.168.0.100:30046
```
Result: `https://192.168.0.100:30046/api/...` ✅

### 3. Without Protocol (backwards compatible)
```bash
BAZARR_HOSTNAME=192.168.0.100:30046
```
Result: `http://192.168.0.100:30046/api/...` ✅

## How to Update

### If Using Docker

Pull the latest version:
```bash
docker pull kaneelir0ll/bazarr-autotranslate:latest
```

Or use the specific version:
```bash
docker pull kaneelir0ll/bazarr-autotranslate:1.0.1
```

Then restart your container:
```bash
docker stop bazarr-autotranslate
docker rm bazarr-autotranslate
# Run your docker-compose up or docker run command again
```

### If Using Standalone Script

The script files have been updated in the repository. Simply use the updated files.

## Testing

You can now use any of these configurations and they will all work correctly:

```yaml
# docker-compose.yml
services:
  bazarr-autotranslate:
    image: kaneelir0ll/bazarr-autotranslate:latest
    environment:
      # All of these work now:
      - BAZARR_HOSTNAME=http://192.168.0.100:30046
      # - BAZARR_HOSTNAME=https://192.168.0.100:30046
      # - BAZARR_HOSTNAME=192.168.0.100:30046
      # - BAZARR_HOSTNAME=localhost:6767
```

## Changelog

### Version 1.0.1 (2025-02-13)
- **Fixed**: Double HTTP protocol issue when BAZARR_HOSTNAME includes protocol
- **Added**: Support for HTTPS protocol
- **Improved**: Better URL construction with protocol detection

### Version 1.0.0 (2025-02-13)
- Initial Docker Hub release

---

**Issue Resolved!** You can now use the full URL with protocol in your configuration. 🎉
