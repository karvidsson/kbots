#!/usr/bin/env bash
# update.sh — one-command update for a running kbots install.
#
#   cd <install-dir> && scripts/update.sh [--restart]
#
# Pulls the latest engine (from the clone's origin — typically your working
# checkout or GitHub), syncs dependencies, then either:
#   - hot-reloads live (tools/skills/codex changes only — the engine's file
#     watcher picks them up without a restart), or
#   - restarts the service (core code, dependency, or unit changes).
#
# --restart forces a service restart even when already up to date — the
# sanctioned path for config-only changes that need a process restart
# (same as /admin reboot), instead of a raw launchctl/systemctl call.
set -euo pipefail

FORCE_RESTART=0
[ "${1:-}" = "--restart" ] && FORCE_RESTART=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$ENGINE_ROOT"

SERVICE_NAME="${KBOTS_SERVICE_NAME:-kbots}"
LAUNCHD_LABEL="${KBOTS_LAUNCHD_LABEL:-com.kbots.agent}"

OLD=$(git rev-parse HEAD)
echo "Pulling updates..."
git pull --ff-only
NEW=$(git rev-parse HEAD)

if [ "$OLD" = "$NEW" ] && [ "$FORCE_RESTART" -eq 0 ]; then
    echo "Already up to date ($(git rev-parse --short HEAD))."
    exit 0
fi

echo
echo "Updated $(git rev-parse --short "$OLD")..$(git rev-parse --short "$NEW"):"
git log --oneline "$OLD".."$NEW" | sed 's/^/  /'
echo

# Sync dependencies across all layers (service units run uv with --no-sync,
# so skipping this would crash the service on any new dependency).
# OLD rev lets sync.sh grandfather modules this update relocated to extras/.
export KBOTS_GRANDFATHER_OLD_REV="$OLD"
if [ -x "$SCRIPT_DIR/sync.sh" ]; then
    "$SCRIPT_DIR/sync.sh"
else
    uv sync
fi

# Classify changed paths: tools/skills/codex hot-reload live; anything else
# (src/core, connectors, llm, deps, units) needs a process restart.
NEED_RESTART=$FORCE_RESTART
UNIT_CHANGED=0
while IFS= read -r f; do
    case "$f" in
        src/tools/*|skills/*|codex/*) ;;
        config/*.service|config/timers/*) NEED_RESTART=1; UNIT_CHANGED=1 ;;
        *) NEED_RESTART=1 ;;
    esac
done < <(git diff --name-only "$OLD".."$NEW")

# A unit template is not the unit. The service re-execs the INSTALLED unit
# (<overlay>/systemd/*.service, symlinked from /etc/systemd/system), which
# only the setup wizard ever rendered — so a pulled fix to config/kbots.service
# used to restart the service into the old sandbox and report success.
# Re-render from the new templates, then relink + daemon-reload.
if [ "$UNIT_CHANGED" -eq 1 ] && [ "$(uname)" != "Darwin" ]; then
    OVERLAY="${KBOTS_OVERLAY:-}"
    if [ -z "$OVERLAY" ]; then
        OVERLAY=$(grep -o 'KBOTS_OVERLAY=.*' /etc/systemd/system/kbots.service 2>/dev/null \
                  | head -1 | cut -d= -f2-)
    fi
    UNITS_APPLIED=0
    if [ -n "$OVERLAY" ] && KBOTS_OVERLAY="$OVERLAY" uv run --no-sync python setup.py --rerender-units; then
        if sudo -n bash "$SCRIPT_DIR/install-systemd.sh" "$OVERLAY" 2>/dev/null; then
            UNITS_APPLIED=1
            echo "Unit files re-rendered and reloaded."
        fi
    fi
    if [ "$UNITS_APPLIED" -eq 0 ]; then
        echo
        echo "!! Unit template changed in this update, but it was NOT applied."
        echo "!! Restarting alone re-execs the old installed unit. Apply it from"
        echo "!! a shell outside the service (needs passwordless sudo):"
        echo "!!   KBOTS_OVERLAY=<overlay> uv run python setup.py --rerender-units"
        echo "!!   sudo bash scripts/install-systemd.sh <overlay>   # relinks + daemon-reload"
        echo "!!   sudo systemctl restart $SERVICE_NAME"
        echo
    fi
fi

if [ "$NEED_RESTART" -eq 0 ]; then
    echo "Only tools/skills/codex changed — hot-reloaded live, no restart needed."
    exit 0
fi

echo "Core changes detected — restarting service..."
if [ "$(uname)" = "Darwin" ]; then
    if launchctl kickstart -k "gui/$(id -u)/$LAUNCHD_LABEL" 2>/dev/null; then
        echo "Service restarted ($LAUNCHD_LABEL)."
    else
        echo "Could not restart via launchctl — restart manually:"
        echo "  launchctl kickstart -k gui/\$(id -u)/$LAUNCHD_LABEL"
        exit 1
    fi
else
    if sudo -n systemctl restart "$SERVICE_NAME" 2>/dev/null || systemctl restart "$SERVICE_NAME" 2>/dev/null; then
        echo "Service restarted ($SERVICE_NAME)."
    else
        echo "Could not restart via systemctl — restart manually:"
        echo "  sudo systemctl restart $SERVICE_NAME"
        exit 1
    fi
fi
