#!/usr/bin/env bash
# watchdog.sh — automatic recovery for the kbots main service.
#
# Runs periodically as its OWN service (launchd StartInterval / systemd timer),
# independent of the engine, so it stays up when the engine is down. It watches
# the engine's heartbeat file:
#
#   - heartbeat fresh  -> engine healthy; record HEAD as the last known-good commit
#   - heartbeat stale  -> engine crashed / hung / booted-broken: roll the install
#                         back to the last known-good commit and restart
#
# This is the lifeboat: a bad deploy that crash-loops the service is reverted
# automatically, no human and no second bot required. It complements
# self-deploy.sh (which rolls back within its own run) by covering failures that
# happen outside a deploy, or if a deploy's own rollback didn't take.
#
# Designed to live OUTSIDE the volatile checkout (installed into the overlay) so
# a broken checkout can't disable its own recovery.
set -uo pipefail

INSTALL="${KBOTS_HOME:-$HOME/kbots}"
OVERLAY="${KBOTS_OVERLAY:-$HOME/kbots-overlay}"
LABEL="${KBOTS_LAUNCHD_LABEL:-com.kbots.agent}"
SERVICE="${KBOTS_SERVICE_NAME:-kbots}"
STALE="${KBOTS_HEARTBEAT_STALE:-180}"   # seconds without a heartbeat = unhealthy

WLOG="$OVERLAY/data/watchdog.log"
GOOD="$OVERLAY/data/last-good-commit"

# Resolve the engine's data_dir (default ./data, relative to the install cwd).
DATA_DIR="$(grep -E '^[[:space:]]*data_dir:' "$OVERLAY/config/config.yaml" 2>/dev/null \
    | head -1 | sed -E 's/.*data_dir:[[:space:]]*//; s/["'\'' ]//g')"
[ -z "$DATA_DIR" ] && DATA_DIR="./data"
case "$DATA_DIR" in
    /*) : ;;
    *)  DATA_DIR="$INSTALL/${DATA_DIR#./}" ;;
esac
HB="$DATA_DIR/heartbeat"

log() { echo "$(/bin/date '+%Y-%m-%d %H:%M:%S') $*" >> "$WLOG" 2>/dev/null; }

restart_service() {
    if [ "$(uname)" = "Darwin" ]; then
        launchctl kickstart -k "gui/$(id -u)/$LABEL"
    else
        sudo -n systemctl restart "$SERVICE" 2>/dev/null || systemctl restart "$SERVICE"
    fi
}

now=$(/bin/date +%s)
head="$(git -C "$INSTALL" rev-parse HEAD 2>/dev/null || echo "")"
good="$(cat "$GOOD" 2>/dev/null || echo "")"
hb=0; [ -f "$HB" ] && hb="$(cat "$HB" 2>/dev/null || echo 0)"
age=$(( now - hb ))

if [ "$hb" -gt 0 ] && [ "$age" -le "$STALE" ]; then
    # Healthy — promote the running commit as the rollback target.
    if [ -n "$head" ] && [ "$head" != "$good" ]; then
        echo "$head" > "$GOOD"
        log "healthy at ${head:0:8} — recorded as known-good"
    fi
    exit 0
fi

# Unhealthy (stale or never-seen heartbeat).
if [ -z "$good" ]; then
    log "engine unhealthy (heartbeat age ${age}s) but no known-good recorded yet — not rolling back"
    exit 0
fi

if [ -n "$head" ] && [ "$good" != "$head" ]; then
    log "engine unhealthy (heartbeat age ${age}s) — ROLLING BACK ${head:0:8} -> ${good:0:8}"
    git -C "$INSTALL" reset --hard "$good" >> "$WLOG" 2>&1
    ( cd "$INSTALL" && { scripts/sync.sh >> "$WLOG" 2>&1 || uv sync >> "$WLOG" 2>&1; } )
else
    log "engine unhealthy (heartbeat age ${age}s), already at known-good ${good:0:8} — restarting"
fi
restart_service >> "$WLOG" 2>&1
log "restart issued"
