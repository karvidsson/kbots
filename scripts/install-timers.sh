#!/usr/bin/env bash
# Install all kbots systemd timers.
# Run as root: sudo bash scripts/install-timers.sh
#
# Renders timer templates from config/timers/ with correct paths,
# writes them to the overlay's systemd/ dir, and symlinks from
# /etc/systemd/system/. Falls back to direct install if no overlay.
set -euo pipefail

KBOTS_HOME="${KBOTS_HOME:-$(cd "$(dirname "$0")/.." && pwd)}"
TIMER_DIR="$KBOTS_HOME/config/timers"

# Try to pick up KBOTS_OVERLAY from env, or from the main service unit
if [ -z "${KBOTS_OVERLAY:-}" ]; then
    KBOTS_OVERLAY=$(grep -oP 'KBOTS_OVERLAY=\K.*' /etc/systemd/system/kbots.service 2>/dev/null \
        || true)
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: Must run as root (sudo)"
    exit 1
fi

if [ ! -d "$TIMER_DIR" ]; then
    echo "ERROR: Timer directory not found: $TIMER_DIR"
    exit 1
fi

echo "Installing kbots systemd timers..."
echo "  KBOTS_HOME:    $KBOTS_HOME"
echo "  KBOTS_OVERLAY: ${KBOTS_OVERLAY:-<not set>}"

# Determine target directory for rendered files
if [ -n "${KBOTS_OVERLAY:-}" ]; then
    TARGET_DIR="$KBOTS_OVERLAY/systemd"
    mkdir -p "$TARGET_DIR"
    USE_SYMLINKS=true
else
    TARGET_DIR="/etc/systemd/system"
    USE_SYMLINKS=false
fi

# Install service and timer files:
# 1. Substitute /opt/kbots placeholder with actual KBOTS_HOME
# 2. Inject KBOTS_OVERLAY env var if set (after the KBOTS_HOME line)
for src in "$TIMER_DIR"/kbots-*.service "$TIMER_DIR"/kbots-*.timer; do
    [ -f "$src" ] || continue
    name=$(basename "$src")
    content=$(sed "s|/opt/kbots|$KBOTS_HOME|g" "$src")
    if [ -n "${KBOTS_OVERLAY:-}" ] && [[ "$name" == *.service ]]; then
        content=$(echo "$content" | sed "/Environment=KBOTS_HOME=/a Environment=KBOTS_OVERLAY=$KBOTS_OVERLAY")
    fi
    echo "$content" > "$TARGET_DIR/$name"

    # Symlink from /etc/systemd/system/ if using overlay
    if [ "$USE_SYMLINKS" = true ]; then
        ln -sf "$TARGET_DIR/$name" "/etc/systemd/system/$name"
    fi

    echo "  Installed: $name"
done

# Reload systemd
systemctl daemon-reload

# Enable and start all timers
for timer in "$TIMER_DIR"/kbots-*.timer; do
    [ -f "$timer" ] || continue
    name=$(basename "$timer")
    systemctl enable --now "$name"
    echo "  Enabled: $name"
done

echo ""
echo "All timers installed. Verify with:"
echo "  systemctl list-timers | grep kbots"
