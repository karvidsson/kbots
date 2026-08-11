#!/usr/bin/env bash
# Migrate kbots from root to dedicated kbots service account.
# Run as root AFTER completing: sudo -u kbots claude auth login
#
# This script:
#   1. Stops the user-level (root) kbots service
#   2. Disables it so it doesn't restart
#   3. Transfers ownership of KBOTS_HOME to kbots:kbots
#   4. Locks down sensitive files (600)
#   5. Installs the system-level service (runs as User=kbots)
#   6. Starts kbots under the new service account
#
# Rollback: run scripts/rollback-service-account.sh

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

KBOTS_DIR="${KBOTS_HOME:-$(cd "$(dirname "$0")/.." && pwd)}"

echo -e "${YELLOW}=== kbots Service Account Migration ===${NC}"
echo ""

# Pre-flight checks
echo "Running pre-flight checks..."

if [ "$(id -u)" -ne 0 ]; then
    echo -e "${RED}ERROR: Must run as root${NC}"
    exit 1
fi

if ! id kbots &>/dev/null; then
    echo -e "${RED}ERROR: kbots user does not exist${NC}"
    exit 1
fi

KBOTS_USER_HOME=$(eval echo ~kbots)

if [ ! -f "$KBOTS_USER_HOME/.config/kbots-vault-key" ]; then
    echo -e "${RED}ERROR: Vault key not found at $KBOTS_USER_HOME/.config/kbots-vault-key${NC}"
    exit 1
fi

if [ ! -f "$KBOTS_USER_HOME/.claude/.credentials.json" ]; then
    echo -e "${RED}ERROR: Claude credentials not found for kbots user.${NC}"
    echo "Run: sudo -u kbots claude auth login"
    exit 1
fi

if [ ! -f /etc/sudoers.d/kbots ]; then
    echo -e "${RED}ERROR: Sudoers rule not found at /etc/sudoers.d/kbots${NC}"
    exit 1
fi

if ! command -v /usr/local/bin/uv &>/dev/null; then
    echo -e "${RED}ERROR: uv not found at /usr/local/bin/uv${NC}"
    exit 1
fi

echo -e "${GREEN}All pre-flight checks passed.${NC}"
echo ""

# Step 1: Stop user-level service
echo "Step 1: Stopping user-level kbots service..."
systemctl --user stop kbots.service 2>/dev/null || true
systemctl --user disable kbots.service 2>/dev/null || true
# Also kill any stray process
pkill -f "python -m src.main" 2>/dev/null || true
sleep 2
echo -e "${GREEN}  User-level service stopped.${NC}"

# Step 2: Transfer ownership
echo "Step 2: Transferring ownership to kbots:kbots..."
chown -R kbots:kbots "$KBOTS_DIR"
echo -e "${GREEN}  Ownership transferred.${NC}"

# Step 3: Lock down sensitive files
echo "Step 3: Setting file permissions..."
chmod 600 "$KBOTS_DIR"/config/secrets.enc
chmod 600 "$KBOTS_DIR"/config/team.json
chmod 600 "$KBOTS_DIR"/config/config.yaml
chmod 600 "$KBOTS_DIR"/data/*.db 2>/dev/null || true
chmod 700 "$KBOTS_DIR"/data
echo -e "${GREEN}  Permissions set.${NC}"

# Step 4: Install system-level service
echo "Step 4: Installing system-level service..."
cp "$KBOTS_DIR/config/kbots.service" /etc/systemd/system/kbots.service
systemctl daemon-reload
systemctl enable kbots.service
echo -e "${GREEN}  System service installed and enabled.${NC}"

# Step 5: Start kbots as kbots user
echo "Step 5: Starting kbots as kbots user..."
systemctl start kbots.service
sleep 3

if systemctl is-active --quiet kbots.service; then
    echo -e "${GREEN}  kbots is running as kbots user.${NC}"
    echo ""
    echo -e "${GREEN}=== Migration complete ===${NC}"
    echo ""
    systemctl status kbots.service --no-pager
else
    echo -e "${RED}  kbots failed to start. Check logs:${NC}"
    echo "  journalctl -u kbots -n 30 --no-pager"
    exit 1
fi
