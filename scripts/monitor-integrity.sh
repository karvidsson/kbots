#!/bin/bash
# monitor-integrity.sh — File integrity monitoring for critical kbots v2 files.
# Compares SHA256 checksums against baseline. Alerts on changes.
# Runs on HOST every 12 hours via cron.
set -euo pipefail

KBOTS_HOME="${KBOTS_HOME:-$(cd "$(dirname "$0")/.." && pwd)}"
source "$KBOTS_HOME/scripts/lib-alert.sh"
BASELINE="$KBOTS_HOME/.security/integrity-baseline.json"
LOG_PREFIX="[integrity-monitor]"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $LOG_PREFIX $1"; }

# Critical files to monitor
MONITORED_FILES=(
    "$KBOTS_HOME/config/config.yaml"
    "$KBOTS_HOME/config/agents.yaml"
    "$KBOTS_HOME/config/team.json"
    "$KBOTS_HOME/src/mcp_server.py"
    "$KBOTS_HOME/src/core/hitl.py"
    "$KBOTS_HOME/src/auth/oauth2.py"
)

mkdir -p "$(dirname "$BASELINE")"

# Build current checksums
CURRENT=$(jq -n '{}')
for fpath in "${MONITORED_FILES[@]}"; do
    if [ -f "$fpath" ]; then
        hash=$(sha256sum "$fpath" | awk '{print $1}')
        CURRENT=$(echo "$CURRENT" | jq --arg k "$fpath" --arg v "$hash" '. + {($k): $v}')
    fi
done

# Create baseline on first run
if [ ! -f "$BASELINE" ]; then
    echo "$CURRENT" | jq . > "$BASELINE"
    log "Baseline created with $(echo "$CURRENT" | jq 'length') files"
    exit 0
fi

# Compare
CHANGES=""
BASELINE_DATA=$(cat "$BASELINE")

for fpath in "${MONITORED_FILES[@]}"; do
    current_hash=$(echo "$CURRENT" | jq -r --arg k "$fpath" '.[$k] // "missing"')
    baseline_hash=$(echo "$BASELINE_DATA" | jq -r --arg k "$fpath" '.[$k] // "new"')

    if [ "$baseline_hash" = "new" ] && [ "$current_hash" != "missing" ]; then
        CHANGES="${CHANGES}\n- **NEW**: $(basename "$fpath")"
    elif [ "$current_hash" = "missing" ] && [ "$baseline_hash" != "new" ]; then
        CHANGES="${CHANGES}\n- **DELETED**: $(basename "$fpath")"
    elif [ "$current_hash" != "$baseline_hash" ]; then
        CHANGES="${CHANGES}\n- **MODIFIED**: $(basename "$fpath")"
    fi
done

if [ -n "$CHANGES" ]; then
    send_alert "SECURITY | File integrity change detected$(echo -e "$CHANGES")

Run: $KBOTS_HOME/scripts/monitor-integrity.sh --update-baseline"
    log "Changes detected:$(echo -e "$CHANGES")"
else
    log "All files match baseline"
fi

if [ "${1:-}" = "--update-baseline" ]; then
    echo "$CURRENT" | jq . > "$BASELINE"
    log "Baseline updated"
fi
