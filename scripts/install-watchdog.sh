#!/usr/bin/env bash
# install-watchdog.sh — register the automatic-recovery watchdog as its own
# service, independent of the main kbots service.
#
#   scripts/install-watchdog.sh            # install + start
#   scripts/install-watchdog.sh uninstall  # remove
#
# The watchdog script is copied into the overlay (outside the volatile checkout,
# so a broken checkout can't disable its own recovery) and run every 2 minutes.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL="$(dirname "$SCRIPT_DIR")"
OVERLAY="${KBOTS_OVERLAY:-$HOME/kbots-overlay}"
LABEL="${KBOTS_LAUNCHD_LABEL:-com.kbots.agent}"
WD_LABEL="com.kbots.watchdog"
SERVICE="${KBOTS_SERVICE_NAME:-kbots}"
INTERVAL="${KBOTS_WATCHDOG_INTERVAL:-120}"
DEST="$OVERLAY/watchdog.sh"
PLIST="$HOME/Library/LaunchAgents/$WD_LABEL.plist"

action="${1:-install}"

if [ "$(uname)" = "Darwin" ]; then
    if [ "$action" = "uninstall" ]; then
        launchctl bootout "gui/$(id -u)/$WD_LABEL" 2>/dev/null || true
        rm -f "$PLIST" "$DEST"
        echo "Watchdog uninstalled."
        exit 0
    fi
    mkdir -p "$OVERLAY/data" "$(dirname "$PLIST")"
    cp "$SCRIPT_DIR/watchdog.sh" "$DEST"
    chmod +x "$DEST"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$WD_LABEL</string>
    <key>ProgramArguments</key>
    <array><string>/bin/bash</string><string>$DEST</string></array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>HOME</key><string>$HOME</string>
        <key>KBOTS_HOME</key><string>$INSTALL</string>
        <key>KBOTS_OVERLAY</key><string>$OVERLAY</string>
        <key>KBOTS_LAUNCHD_LABEL</key><string>$LABEL</string>
    </dict>
    <key>StartInterval</key><integer>$INTERVAL</integer>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>$OVERLAY/data/watchdog.out.log</string>
    <key>StandardErrorPath</key><string>$OVERLAY/data/watchdog.err.log</string>
</dict>
</plist>
EOF
    launchctl bootout "gui/$(id -u)/$WD_LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "Watchdog installed ($WD_LABEL) — runs every ${INTERVAL}s."
    echo "  script: $DEST"
    echo "  log:    $OVERLAY/data/watchdog.log"
else
    # Linux: systemd timer + service (user or system depending on how kbots runs)
    UNIT_DIR="/etc/systemd/system"
    if [ "$action" = "uninstall" ]; then
        sudo systemctl disable --now kbots-watchdog.timer 2>/dev/null || true
        sudo systemctl disable --now k-agents-watchdog.timer 2>/dev/null || true  # legacy name
        sudo rm -f "$UNIT_DIR/kbots-watchdog.service" "$UNIT_DIR/kbots-watchdog.timer" \
            "$UNIT_DIR/kbots-watchdog.service" "$UNIT_DIR/kbots-watchdog.timer" \
            /etc/sudoers.d/kbots-watchdog /etc/sudoers.d/kbots-watchdog "$DEST"
        sudo systemctl daemon-reload
        echo "Watchdog uninstalled."
        exit 0
    fi
    mkdir -p "$OVERLAY/data"
    cp "$SCRIPT_DIR/watchdog.sh" "$DEST"; chmod +x "$DEST"
    # Run the watchdog as the SERVICE user (owner of the install), not whoever
    # ran the installer — so its git reset / sync keep files service-user-owned.
    SVC_USER="${KBOTS_USER:-$(stat -c '%U' "$INSTALL" 2>/dev/null || id -un)}"
    chown -R "$SVC_USER" "$OVERLAY/data" 2>/dev/null || sudo chown -R "$SVC_USER" "$OVERLAY/data" 2>/dev/null || true
    # The main service is a systemd SYSTEM unit, so restarting it needs root.
    # Grant the service user JUST that one command, passwordless.
    SYSTEMCTL="$(command -v systemctl || echo /usr/bin/systemctl)"
    echo "$SVC_USER ALL=(root) NOPASSWD: $SYSTEMCTL restart $SERVICE, $SYSTEMCTL restart $SERVICE.service" \
        | sudo tee /etc/sudoers.d/kbots-watchdog >/dev/null
    sudo chmod 0440 /etc/sudoers.d/kbots-watchdog
    sudo visudo -cf /etc/sudoers.d/kbots-watchdog >/dev/null
    sudo tee "$UNIT_DIR/kbots-watchdog.service" >/dev/null <<EOF
[Unit]
Description=kbots watchdog (automatic recovery)
[Service]
Type=oneshot
User=$SVC_USER
Environment=KBOTS_HOME=$INSTALL
Environment=KBOTS_OVERLAY=$OVERLAY
Environment=KBOTS_SERVICE_NAME=$SERVICE
ExecStart=/bin/bash $DEST
EOF
    sudo tee "$UNIT_DIR/kbots-watchdog.timer" >/dev/null <<EOF
[Unit]
Description=Run kbots watchdog every ${INTERVAL}s
[Timer]
OnBootSec=60
OnUnitActiveSec=${INTERVAL}
[Install]
WantedBy=timers.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable --now kbots-watchdog.timer
    echo "Watchdog installed (kbots-watchdog.timer) — runs every ${INTERVAL}s."
fi
