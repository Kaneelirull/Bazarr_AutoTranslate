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
echo "  - Prune extra SRT languages: ${CLEANUP_PRUNE_EXTRA_LANGUAGES:-true} (${CLEANUP_PRUNE_ACTION:-quarantine})"
echo "  - Source-less line-only action: ${CLEANUP_SOURCELESS_LINE_ONLY_ACTION:-warn}"
echo "  - Quarantine translation hold: ${CLEANUP_QUARANTINE_HOLD_DAYS:-30} days"
echo "  - Status dashboard: ${STATUS_ENABLED:-true} on ${STATUS_BIND:-0.0.0.0}:${STATUS_PORT:-8765}"

exec python3 -u /app/Bazarr_AutoTranslate.py
