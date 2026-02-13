# Code Change Summary

## Before (Broken)

```python
def get_api_url(endpoint):
    """Construct the full API URL for a given endpoint."""
    return f"http://{BAZARR_HOSTNAME}/api/{endpoint}"
```

**Problem**: Always adds `http://` even if already present in `BAZARR_HOSTNAME`

**Example Issue**:
- Input: `BAZARR_HOSTNAME=http://192.168.0.100:30046`
- Output: `http://http://192.168.0.100:30046/api/wanted/episodes` ❌

---

## After (Fixed)

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

**Solution**: Check for existing protocol before adding one

**Test Cases**:

1. With HTTP:
   - Input: `BAZARR_HOSTNAME=http://192.168.0.100:30046`
   - Output: `http://192.168.0.100:30046/api/wanted/episodes` ✅

2. With HTTPS:
   - Input: `BAZARR_HOSTNAME=https://192.168.0.100:30046`
   - Output: `https://192.168.0.100:30046/api/wanted/episodes` ✅

3. Without Protocol (backwards compatible):
   - Input: `BAZARR_HOSTNAME=192.168.0.100:30046`
   - Output: `http://192.168.0.100:30046/api/wanted/episodes` ✅

4. Without Protocol (localhost):
   - Input: `BAZARR_HOSTNAME=localhost:6767`
   - Output: `http://localhost:6767/api/wanted/episodes` ✅

---

## Files Modified

1. `docker/Bazarr_AutoTranslate.py` - Line 57-59 → Line 57-63
2. `Bazarr_AutoTranslate.py` - Line 70-72 → Line 70-76

Both files now include the protocol detection logic.
