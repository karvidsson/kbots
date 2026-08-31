#!/usr/bin/env bash
# self-deploy.sh — deploy the latest pushed engine to THIS install, safely.
#
#   cd <install-dir> && scripts/self-deploy.sh
#
# The safe path for an agent (or you) to ship a change and update the live
# service without risking a crash-loop. Unlike update.sh, every step is gated:
#
#   1. pull latest from origin
#   2. sync dependencies
#   3. GATE: ruff + full pytest on the new code — abort+rollback if red
#   4. restart the service
#   5. HEALTH CHECK: wait for a clean boot — auto-rollback if it doesn't come up
#
# A failure at any stage reverts the install to the exact commit it was on and
# restarts, so the box is never left on broken code. Deterministic on purpose:
# the safety lives in this script, not in an agent's judgement.
set -uo pipefail  # NOT -e: failures are handled explicitly so we can roll back

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$ENGINE_ROOT"

# Root shells operate some installs. Re-exec as the install owner: launchctl
# must target the owner's gui domain (gui/0 doesn't exist), $HOME-derived
# overlay/log paths must resolve to the owner's, and a root-run test gate
# leaves root-owned caches (.ruff_cache, __pycache__) that break later runs.
OWNER="$(stat -f %Su "$ENGINE_ROOT" 2>/dev/null || stat -c %U "$ENGINE_ROOT" 2>/dev/null)"
if [ "$(id -u)" = "0" ] && [ -n "$OWNER" ] && [ "$OWNER" != "root" ]; then
    echo "[self-deploy] running as root — re-executing as install owner '$OWNER'"
    exec sudo -H -u "$OWNER" "${BASH_SOURCE[0]}" "$@"
fi

SERVICE_NAME="${KBOTS_SERVICE_NAME:-kbots}"
LAUNCHD_LABEL="${KBOTS_LAUNCHD_LABEL:-com.kbots.agent}"
OVERLAY="${KBOTS_OVERLAY:-$HOME/kbots-overlay}"
export KBOTS_OVERLAY="$OVERLAY"   # so sync.sh sees it (installs durable extras)
LOG="${KBOTS_LOG:-$OVERLAY/data/launchd.stderr.log}"
HEALTH_TIMEOUT="${KBOTS_HEALTH_TIMEOUT:-150}"

OLD=$(git rev-parse HEAD)

log() { echo "[self-deploy] $*"; }

restart_service() {
    if [ "$(uname)" = "Darwin" ]; then
        launchctl kickstart -k "gui/$(id -u)/$LAUNCHD_LABEL" 2>/dev/null
    else
        sudo -n systemctl restart "$SERVICE_NAME" 2>/dev/null || systemctl restart "$SERVICE_NAME" 2>/dev/null
    fi
}

rollback() {
    log "ROLLING BACK to $(git rev-parse --short "$OLD")"
    git reset --hard "$OLD" >/dev/null 2>&1
    "$SCRIPT_DIR/sync.sh" >/dev/null 2>&1 || uv sync >/dev/null 2>&1
    restart_service
}

# Wait for a clean boot after a restart. $1 = log line count before restart.
health_ok() {
    local before="$1" deadline=$((SECONDS + HEALTH_TIMEOUT))
    while [ "$SECONDS" -lt "$deadline" ]; do
        if [ "$(uname)" = "Darwin" ] && [ -f "$LOG" ]; then
            local new; new="$(tail -n "+$((before + 1))" "$LOG" 2>/dev/null)"
            # Match the real Python traceback header + genuine fatal log markers,
            # NOT prose. The "Running version … — <commit subject>" line we log
            # can legitimately contain words like "traceback"/"fatal"/"critical"
            # in a commit message, which must not fail the health check.
            if echo "$new" | grep -qE "Traceback \(most recent call last\)|Address already in use|Preflight[^|]*[Ff]ail| CRITICAL "; then
                return 1
            fi
            if echo "$new" | grep -q "running —"; then
                return 0
            fi
        else
            # systemd: active + not crash-looping
            sleep 5
            systemctl is-active --quiet "$SERVICE_NAME" && return 0 || return 1
        fi
        sleep 2
    done
    return 1
}

log "install at $(git rev-parse --short HEAD) — pulling latest"
# Bare pull uses the branch's configured tracking remote (may be 'upstream',
# not 'origin'); matches update.sh and avoids a hardcoded remote name.
if ! git pull --ff-only; then
    log "pull failed / not fast-forward — aborting (resolve manually)"; exit 1
fi
# Fetch tags too — the platform version is derived from the nearest vX.Y.Z tag
# (git describe), so the boot-time version is only correct if tags are present.
git fetch --tags --quiet 2>/dev/null || true
NEW=$(git rev-parse HEAD)

# kagents→kbots rename: migrate legacy layouts in place. The migration script
# is idempotent (no-op once migrated), takes its own backup, and finishes with
# service start + health check. Checked BEFORE the up-to-date early exit: the
# deploy that first pulls the rename executes under the pre-rename script (no
# hook), so the migration must fire on a later run even with nothing to pull.
maybe_migrate_kbots() {
    if [ "$(basename "$ENGINE_ROOT")" = "k-agents" ] \
       || [ -f /etc/systemd/system/k-agents.service ] \
       || [ -f "$HOME/Library/LaunchAgents/com.k-agents.agent.plist" ]; then
        log "legacy k-agents layout detected — running rename migration"
        exec "$SCRIPT_DIR/migrate-to-kbots.sh"
    fi
}

if [ "$OLD" = "$NEW" ]; then
    maybe_migrate_kbots
    log "already up to date ($(git rev-parse --short HEAD))"; exit 0
fi
log "updating $(git rev-parse --short "$OLD")..$(git rev-parse --short "$NEW")"

log "syncing dependencies"
# OLD rev lets sync.sh grandfather modules this deploy relocated to extras/.
export KBOTS_GRANDFATHER_OLD_REV="$OLD"
if ! "$SCRIPT_DIR/sync.sh" >/dev/null 2>&1 && ! uv sync >/dev/null 2>&1; then
    log "dependency sync failed"; rollback; exit 1
fi

log "GATE: ruff + pytest on new code"
# The gate runs in a THROWAWAY env (UV_PROJECT_ENVIRONMENT), never the live
# .venv. `uv run` without --no-sync reconciles the env it targets against the
# lockfile + requested extras — aimed at .venv, that silently UNINSTALLED the
# 38 Layer 2/3 extras sync.sh had just put back, and the service then booted
# with --no-sync into the pruned env. Core imports fine, so the health check
# passed; every tool needing matplotlib/lxml/fpdf2/… broke with no signal.
# (--no-sync alone is not an option: the gate needs dev extras the live .venv
# deliberately lacks.)
GATE_ENV="$OVERLAY/tmp/gate-venv"
# `.` not a path list: ruff honours the excludes in pyproject.toml, so a new
# top-level dir (extras/ was the one that slipped through) is linted the day it
# lands instead of whenever someone remembers to extend this line.
if ! UV_PROJECT_ENVIRONMENT="$GATE_ENV" uv run --extra dev ruff check .; then
    log "LINT FAILED — not deploying"; rollback; exit 1
fi
if ! UV_PROJECT_ENVIRONMENT="$GATE_ENV" uv run --extra dev pytest -q; then
    log "TESTS FAILED — not deploying"; rollback; exit 1
fi

# Post-gate migration check (fresh pull that brought the rename lands here)
maybe_migrate_kbots

# Ship unit changes too. Without this the pull updates config/kbots.service and
# nothing else: the live unit is whatever the setup wizard generated, possibly
# years ago, so a sandbox fix lands in the history and on no machine. Runs
# after the gate (the code it renders through was just tested) and before the
# restart (so the restart is what picks it up). A no-op when nothing changed.
if [ -f "$SCRIPT_DIR/refresh_units.py" ]; then
    uv run --no-sync python "$SCRIPT_DIR/refresh_units.py" --reload
    # 10 = a unit changed, which is a success here: the restart below is what
    # picks it up. Only a genuine failure (unit written, manager not reloaded)
    # rolls back, because that leaves disk and process disagreeing.
    rc=$?
    if [ "$rc" -ne 0 ] && [ "$rc" -ne 10 ]; then
        log "unit refresh failed — see above"; rollback; exit 1
    fi
fi

LINES_BEFORE=0
[ -f "$LOG" ] && LINES_BEFORE=$(wc -l < "$LOG")
log "restarting service"
if ! restart_service; then
    log "restart command failed"; rollback; exit 1
fi

log "health check (up to ${HEALTH_TIMEOUT}s)"
if health_ok "$LINES_BEFORE"; then
    # Record the healthy commit so the watchdog rolls back to it if a later
    # failure crash-loops the service.
    echo "$NEW" > "$OVERLAY/data/last-good-commit" 2>/dev/null || true
    log "✅ deployed $(git rev-parse --short "$NEW") — service healthy"
    exit 0
fi

log "❌ service did NOT come up cleanly"
rollback
LINES_BEFORE=0; [ -f "$LOG" ] && LINES_BEFORE=$(wc -l < "$LOG")
if health_ok "$LINES_BEFORE"; then
    log "reverted to $(git rev-parse --short "$OLD") — healthy again"
else
    log "STILL UNHEALTHY after rollback — MANUAL INTERVENTION NEEDED"
fi
exit 1
