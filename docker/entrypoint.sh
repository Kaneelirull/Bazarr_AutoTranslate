#!/bin/bash
set -e

# Environment variables with defaults
CLEANUP_TIME="${CLEANUP_TIME:-04:00}"
CLEANUP_ROOT="${CLEANUP_ROOT:-/media}"
CLEANUP_MIN_CONFIDENCE="${CLEANUP_MIN_CONFIDENCE:-0.70}"
CLEANUP_MIN_CHARS="${CLEANUP_MIN_CHARS:-200}"

echo "[INFO] Bazarr AutoTranslate Docker Container Starting..."
echo "[INFO] Configuration:"
echo "  - Bazarr Host: ${BAZARR_HOSTNAME}"
echo "  - Primary Language: ${FIRST_LANG}"
echo "  - Secondary Language: ${SECOND_LANG}"
echo "  - Check Interval: ${CHECK_INTERVAL}s"
echo "  - Cleanup Time: ${CLEANUP_TIME}"
echo "  - Cleanup Root: ${CLEANUP_ROOT}"

# Function to run cleanup
run_cleanup() {
    echo "[INFO] Running subtitle cleanup..."
    # Convert colon-separated paths to multiple --root arguments
    IFS=':' read -ra ROOTS <<< "${CLEANUP_ROOT}"
    ROOT_ARGS=()
    for root in "${ROOTS[@]}"; do
        ROOT_ARGS+=(--root "$root")
    done
    
    python3 /app/clean_et_subs.py \
        "${ROOT_ARGS[@]}" \
        --min-confidence "${CLEANUP_MIN_CONFIDENCE}" \
        --min-chars "${CLEANUP_MIN_CHARS}" \
        --delete \
        --verbose 2>&1 | tee -a /var/log/bazarr-autotranslate/cleanup.log
    echo "[INFO] Cleanup completed at $(date)"
}

# Schedule cleanup job
setup_cron() {
    # Parse the time (HH:MM format)
    HOUR=$(echo $CLEANUP_TIME | cut -d: -f1)
    MINUTE=$(echo $CLEANUP_TIME | cut -d: -f2)
    
    # Create cron job
    echo "${MINUTE} ${HOUR} * * * /app/run_cleanup.sh >> /var/log/bazarr-autotranslate/cron.log 2>&1" > /etc/cron.d/cleanup-subs
    chmod 0644 /etc/cron.d/cleanup-subs
    
    # Create the cleanup runner script
    cat > /app/run_cleanup.sh << 'EOF'
#!/bin/bash
export CLEANUP_ROOT="${CLEANUP_ROOT}"
export CLEANUP_MIN_CONFIDENCE="${CLEANUP_MIN_CONFIDENCE}"
export CLEANUP_MIN_CHARS="${CLEANUP_MIN_CHARS}"
export BAZARR_HOSTNAME="${BAZARR_HOSTNAME}"
export BAZARR_APIKEY="${BAZARR_APIKEY}"

# Convert colon-separated paths to multiple --root arguments
IFS=':' read -ra ROOTS <<< "${CLEANUP_ROOT}"
ROOT_ARGS=()
for root in "${ROOTS[@]}"; do
    ROOT_ARGS+=(--root "$root")
done

python3 /app/clean_et_subs.py \
    "${ROOT_ARGS[@]}" \
    --min-confidence "${CLEANUP_MIN_CONFIDENCE}" \
    --min-chars "${CLEANUP_MIN_CHARS}" \
    --delete \
    --verbose
EOF
    chmod +x /app/run_cleanup.sh
    
    # Apply environment variables to crontab
    crontab /etc/cron.d/cleanup-subs
    
    echo "[INFO] Cron job scheduled for ${CLEANUP_TIME} daily"
}

# Setup cron for cleanup
setup_cron

# Start cron in background
cron

# Handle shutdown signals gracefully
shutdown() {
    echo "[INFO] Received shutdown signal, stopping services..."
    if [ -n "$TRANSLATE_PID" ]; then
        kill -TERM "$TRANSLATE_PID" 2>/dev/null || true
        wait "$TRANSLATE_PID" 2>/dev/null || true
    fi
    exit 0
}

trap shutdown SIGTERM SIGINT

# Start the main translation script
echo "[INFO] Starting Bazarr AutoTranslate main process..."
python3 /app/Bazarr_AutoTranslate.py &
TRANSLATE_PID=$!

# Wait for the translation process
wait "$TRANSLATE_PID"
