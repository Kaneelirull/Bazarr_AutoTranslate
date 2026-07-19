#!/bin/bash
set -e

echo "[INFO] Bazarr AutoTranslate Docker Container Starting..."
echo "[INFO] Configuration:"
echo "  - Bazarr URL: ${BAZARR_URL}"
echo "  - Lingarr URL: ${LINGARR_URL}"
echo "  - Languages: ${LANGUAGES:-en,et,sv}"
echo "  - Parallel workers: ${PARALLEL_TRANSLATES:-1}"
echo "  - Check Interval: ${CHECK_INTERVAL:-1200}s"
echo "  - Cleanup languages: ${CLEANUP_LANGUAGES:-et}"

exec python3 -u /app/Bazarr_AutoTranslate.py
