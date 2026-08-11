#!/usr/bin/env bash
# full-control.sh — grant/revoke passwordless sudo for the kbots service user.
#
# This is what turns "full control" from user-level into root-level: it lets the
# service account run `sudo` without a password, so a privileged agent's shell
# can do anything as root. MAXIMUM POWER, MAXIMUM RISK — only for a machine you
# fully trust to your agent.
#
#   scripts/full-control.sh grant [user]    # install the sudoers rule
#   scripts/full-control.sh revoke          # remove it
#   scripts/full-control.sh status          # show current state
#
# Default user is the current user (macOS launchd runs as you). On a Linux VPS
# pass the service account, e.g. `scripts/full-control.sh grant kbots`.
set -euo pipefail

SUDOERS_FILE="/etc/sudoers.d/kbots-fullcontrol"
ACTION="${1:-status}"
USER_NAME="${2:-$(id -un)}"

case "$ACTION" in
  grant)
    echo "This grants '$USER_NAME' passwordless sudo (root) via $SUDOERS_FILE."
    echo "The kbots agent will be able to run ANY command as root."
    TMP="$(mktemp)"
    printf '# Installed by kbots scripts/full-control.sh — passwordless sudo for the agent.\n%s ALL=(ALL) NOPASSWD: ALL\n' "$USER_NAME" > "$TMP"
    # Validate before installing — a broken sudoers file can lock you out.
    if ! sudo visudo -c -f "$TMP" >/dev/null; then
        echo "ERROR: generated sudoers rule failed validation; aborting." >&2
        rm -f "$TMP"
        exit 1
    fi
    sudo install -m 0440 -o root -g wheel "$TMP" "$SUDOERS_FILE" 2>/dev/null \
        || sudo install -m 0440 -o root "$TMP" "$SUDOERS_FILE"
    rm -f "$TMP"
    echo "Granted. Full root control enabled for '$USER_NAME'."
    echo "Revoke any time with: scripts/full-control.sh revoke"
    ;;
  revoke)
    if [ -f "$SUDOERS_FILE" ]; then
        sudo rm -f "$SUDOERS_FILE"
        echo "Revoked. Passwordless sudo removed ($SUDOERS_FILE deleted)."
    else
        echo "Nothing to revoke — $SUDOERS_FILE does not exist."
    fi
    ;;
  status)
    if [ -f "$SUDOERS_FILE" ]; then
        echo "ROOT control ENABLED — $SUDOERS_FILE present:"
        sudo cat "$SUDOERS_FILE" 2>/dev/null | grep -v '^#' || true
    else
        echo "Root control not enabled (no $SUDOERS_FILE). Agent has at most user-level control."
    fi
    ;;
  *)
    echo "Usage: $0 {grant|revoke|status} [user]" >&2
    exit 1
    ;;
esac
