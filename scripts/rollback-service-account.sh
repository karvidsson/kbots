#!/usr/bin/env bash
# Rollback: revert kbots from kbots service account back to root.
# Run as root.
set -euo pipefail

KBOTS_DIR="${KBOTS_HOME:-$(cd "$(dirname "$0")/.." && pwd)}"

echo "Rolling back to root... (KBOTS_DIR=$KBOTS_DIR)"

# Stop system service
systemctl stop kbots.service 2>/dev/null || true
systemctl disable kbots.service 2>/dev/null || true
pkill -f "python -m src.main" 2>/dev/null || true
sleep 2

# Restore ownership
chown -R root:root "$KBOTS_DIR"

# Restore permissions
chmod 644 "$KBOTS_DIR"/config/team.json
chmod 644 "$KBOTS_DIR"/config/config.yaml
chmod 644 "$KBOTS_DIR"/data/*.db 2>/dev/null || true
chmod 755 "$KBOTS_DIR"/data

# Re-enable user-level service
systemctl --user enable kbots.service 2>/dev/null || true
systemctl --user start kbots.service 2>/dev/null || true

echo "Rolled back. kbots running as root via user-level service."
