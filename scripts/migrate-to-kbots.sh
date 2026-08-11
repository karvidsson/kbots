#!/usr/bin/env bash
#
# migrate-to-kbots.sh — one-time, idempotent migration of a pre-rename
# "k-agents" install to the "kbots" naming.
#
#   cd <install-dir> && scripts/migrate-to-kbots.sh
#
# Renames, in place, everything the kagents→kbots rename touches on a live
# machine: install/overlay/modules dirs, systemd units + timers or launchd
# plists, the Linux service user + sudoers, data/kagents.db, the vault key
# file, shell-profile env blocks, and generated agent .mcp.json/settings.json.
# Every step is guarded ("only if the old thing exists"), so re-running is a
# no-op. A tar backup of data + config is taken first.
#
# Invoked automatically by self-deploy.sh when a legacy layout is detected.
set -euo pipefail

log() { echo "[migrate-kbots] $*"; }

# Run privileged commands via sudo -n when not root (matches other scripts).
_sudo() { if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo -n "$@"; fi; }

# KBOTS_MIGRATE_SKIP_SYSTEM=1 restricts the run to the install/overlay trees —
# no launchctl/systemctl/usermod/sudoers changes. Used by tests/sandbox runs.
SKIP_SYSTEM="${KBOTS_MIGRATE_SKIP_SYSTEM:-0}"

# The whole script body lives in main() so bash parses the entire file before
# executing: this file's own path changes when the install dir is renamed.
main() {
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    INSTALL="$(cd "$SCRIPT_DIR/.." && pwd)"
    DARWIN=false; [ "$(uname)" = "Darwin" ] && DARWIN=true

    # Owner of the install = the service identity on macOS (launchd gui domain)
    if $DARWIN; then
        OWNER=$(stat -f %Su "$INSTALL"); OWNER_UID=$(id -u "$OWNER")
        OWNER_HOME=$(eval echo "~$OWNER")
    else
        OWNER_HOME="$HOME"
    fi

    # ── Detect legacy layout ─────────────────────────────────────────────
    LEGACY=false
    [ "$(basename "$INSTALL")" = "k-agents" ] && LEGACY=true
    [ -f /etc/systemd/system/k-agents.service ] && LEGACY=true
    [ -f "$OWNER_HOME/Library/LaunchAgents/com.k-agents.agent.plist" ] && LEGACY=true
    if ! $DARWIN && id kagents >/dev/null 2>&1 && ! id kbots >/dev/null 2>&1; then LEGACY=true; fi
    if ! $LEGACY; then
        log "no legacy k-agents layout found — nothing to do"; exit 0
    fi
    log "legacy layout detected — migrating $INSTALL"

    # Overlay/modules siblings (from env, else conventional siblings)
    OVERLAY="${KBOTS_OVERLAY:-${KAGENTS_OVERLAY:-}}"
    [ -z "$OVERLAY" ] && [ -d "$(dirname "$INSTALL")/k-agents-overlay" ] && OVERLAY="$(dirname "$INSTALL")/k-agents-overlay"
    MODULES_DIR=""
    [ -d "$(dirname "$INSTALL")/k-agents-modules" ] && MODULES_DIR="$(dirname "$INSTALL")/k-agents-modules"

    # ── Backup ───────────────────────────────────────────────────────────
    BACKUP="$(dirname "$INSTALL")/kbots-migration-backup-$(date +%Y%m%d-%H%M%S).tar.gz"
    if [ ! -f "$BACKUP" ]; then
        log "backing up data + config to $BACKUP"
        tar czf "$BACKUP" -C "$(dirname "$INSTALL")" \
            "$(basename "$INSTALL")/data" "$(basename "$INSTALL")/config" \
            $( [ -n "$OVERLAY" ] && [ -d "$OVERLAY" ] && echo "--exclude=$(basename "$OVERLAY")/tmp $(basename "$OVERLAY")" ) \
            2>/dev/null || log "backup partial (some paths missing) — continuing"
    fi

    # ── Stop legacy services ─────────────────────────────────────────────
    if [ "$SKIP_SYSTEM" = "1" ]; then
        log "KBOTS_MIGRATE_SKIP_SYSTEM=1 — skipping service/user/sudoers steps"
    fi
    if [ "$SKIP_SYSTEM" != "1" ]; then
        if $DARWIN; then
            _sudo launchctl bootout "gui/$OWNER_UID/com.k-agents.agent" 2>/dev/null || true
            _sudo launchctl bootout "gui/$OWNER_UID/com.k-agents.watchdog" 2>/dev/null || true
        else
            _sudo systemctl disable --now k-agents.service 2>/dev/null || true
            _sudo systemctl disable --now k-agents-rescue.service 2>/dev/null || true
            for t in $(_sudo systemctl list-unit-files 'k-agents-*.timer' --no-legend 2>/dev/null | awk '{print $1}'); do
                _sudo systemctl disable --now "$t" 2>/dev/null || true
            done
        fi
        log "legacy services stopped"

        # ── Linux: rename service user/group + sudoers ───────────────────
        if ! $DARWIN && id kagents >/dev/null 2>&1 && ! id kbots >/dev/null 2>&1; then
            log "renaming service user kagents → kbots"
            _sudo usermod -l kbots kagents
            _sudo groupmod -n kbots kagents
            if [ "$(eval echo ~kbots)" = "/home/kagents" ]; then
                _sudo usermod -d /home/kbots -m kbots
            fi
        fi
        for sf in kagents-fullcontrol k-agents-watchdog kagents; do
            if [ -f "/etc/sudoers.d/$sf" ]; then
                new_sf=$(echo "$sf" | sed -e 's/kagents/kbots/' -e 's/k-agents/kbots/')
                _sudo sh -c "sed -e 's/kagents/kbots/g' -e 's/k-agents/kbots/g' /etc/sudoers.d/$sf > /etc/sudoers.d/$new_sf && chmod 0440 /etc/sudoers.d/$new_sf && visudo -cf /etc/sudoers.d/$new_sf >/dev/null && rm -f /etc/sudoers.d/$sf" \
                    && log "sudoers: $sf → $new_sf"
            fi
        done
    fi

    # ── Rename directories ───────────────────────────────────────────────
    OLD_INSTALL="$INSTALL"
    if [ "$(basename "$INSTALL")" = "k-agents" ]; then
        NEW_INSTALL="$(dirname "$INSTALL")/kbots"
        mv "$INSTALL" "$NEW_INSTALL"
        INSTALL="$NEW_INSTALL"
        log "install dir: $OLD_INSTALL → $INSTALL"
    fi
    OLD_OVERLAY="$OVERLAY"
    if [ -n "$OVERLAY" ] && [ "$(basename "$OVERLAY")" = "k-agents-overlay" ]; then
        NEW_OVERLAY="$(dirname "$OVERLAY")/kbots-overlay"
        mv "$OVERLAY" "$NEW_OVERLAY"; OVERLAY="$NEW_OVERLAY"
        log "overlay: $OLD_OVERLAY → $OVERLAY"
    fi
    if [ -n "$MODULES_DIR" ]; then
        mv "$MODULES_DIR" "$(dirname "$MODULES_DIR")/kbots-modules"
        log "modules: $MODULES_DIR → $(dirname "$MODULES_DIR")/kbots-modules"
    fi

    # ── Rename data files ────────────────────────────────────────────────
    for d in "$INSTALL/data" ${OVERLAY:+"$OVERLAY/data"}; do
        [ -f "$d/kagents.db" ] && [ ! -f "$d/kbots.db" ] && mv "$d/kagents.db" "$d/kbots.db" && log "db: $d/kagents.db → kbots.db"
        rm -f "$d"/kagents*.lock 2>/dev/null || true
    done
    for h in "$OWNER_HOME" $( ! $DARWIN && id kbots >/dev/null 2>&1 && eval echo "~kbots" ); do
        if [ -f "$h/.config/k-agents-vault-key" ] && [ ! -f "$h/.config/kbots-vault-key" ]; then
            mv "$h/.config/k-agents-vault-key" "$h/.config/kbots-vault-key"
            log "vault key moved: $h/.config/kbots-vault-key"
        fi
    done

    # ── Shell-profile env blocks ─────────────────────────────────────────
    for pf in "$OWNER_HOME/.zshrc" "$OWNER_HOME/.zprofile" "$OWNER_HOME/.bashrc" "$OWNER_HOME/.bash_profile" "$OWNER_HOME/.profile"; do
        [ -f "$pf" ] || continue
        if grep -q 'KAGENTS_\|# K-Agents environment' "$pf" 2>/dev/null; then
            sed -i.kbots-bak -e 's/# K-Agents environment/# kbots environment/' \
                -e 's/export KAGENTS_/export KBOTS_/g' \
                -e "s|$OLD_INSTALL|$INSTALL|g" \
                ${OLD_OVERLAY:+-e "s|$OLD_OVERLAY|$OVERLAY|g"} "$pf"
            log "profile updated: $pf"
        fi
    done

    # ── Rewrite generated agent files + overlay config ───────────────────
    # Gaps found on the first live migration (2026-07-22): agent settings live
    # at agents/<name>/.claude/settings.json (depth 4, missed by -maxdepth 3);
    # agents.yaml carries project_dir paths; mcp.yaml carries the server key;
    # team.json carries tool-prefix allowlists; agents' CLAUDE.md/LESSONS.md
    # carry paths and tool names; systemd/ holds rendered unit copies.
    if [ -n "$OVERLAY" ] && [ -d "$OVERLAY" ]; then
        find "$OVERLAY" -maxdepth 5 \( -name '.mcp.json' -o -name 'settings.json' -o -name 'settings.local.json' \) -not -path "$OVERLAY/tmp/*" 2>/dev/null | while read -r f; do
            sed -i.kbots-bak -e 's/k-agents-tools/kbots-tools/g' -e 's/KAGENTS_/KBOTS_/g' \
                -e "s|$OLD_INSTALL|$INSTALL|g" ${OLD_OVERLAY:+-e "s|$OLD_OVERLAY|$OVERLAY|g"} "$f" && rm -f "$f.kbots-bak"
        done
        for cf in "$OVERLAY"/config/*.yaml "$OVERLAY"/config/*.json; do
            [ -f "$cf" ] || continue
            sed -i.kbots-bak -e 's/^kagents:/kbots:/' -e 's/k-agents-tools/kbots-tools/g' \
                -e 's/mcp__k-agents-tools__/mcp__kbots-tools__/g' -e 's/KAGENTS_/KBOTS_/g' \
                -e "s|$OLD_INSTALL|$INSTALL|g" ${OLD_OVERLAY:+-e "s|$OLD_OVERLAY|$OVERLAY|g"} "$cf" && rm -f "$cf.kbots-bak"
        done
        find "$OVERLAY/agents" -maxdepth 2 \( -name 'CLAUDE.md' -o -name 'LESSONS.md' \) 2>/dev/null | while read -r f; do
            sed -i.kbots-bak -e 's/mcp__k-agents-tools__/mcp__kbots-tools__/g' -e 's/KAGENTS_/KBOTS_/g' \
                -e "s|$OLD_INSTALL|$INSTALL|g" ${OLD_OVERLAY:+-e "s|$OLD_OVERLAY|$OVERLAY|g"} "$f" && rm -f "$f.kbots-bak"
        done
        for u in "$OVERLAY"/systemd/*k-agents*; do
            [ -f "$u" ] || continue
            new_u="$(dirname "$u")/$(basename "$u" | sed -e 's/k-agents/kbots/g')"
            sed -e 's/com\.k-agents\./com.kbots./g' -e 's/k-agents/kbots/g' -e 's/kagents/kbots/g' \
                -e 's/KAGENTS_/KBOTS_/g' -e "s|$OLD_INSTALL|$INSTALL|g" \
                ${OLD_OVERLAY:+-e "s|$OLD_OVERLAY|$OVERLAY|g"} "$u" > "$new_u" && rm -f "$u"
        done
        log "overlay agent files + config rewritten"
    fi

    # ── Reinstall watchdog under the new label (old one was booted out) ──
    if [ "$SKIP_SYSTEM" != "1" ]; then
        WD_WAS_INSTALLED=false
        [ -f "$OWNER_HOME/Library/LaunchAgents/com.k-agents.watchdog.plist" ] && WD_WAS_INSTALLED=true
        [ -f /etc/systemd/system/k-agents-watchdog.timer ] && WD_WAS_INSTALLED=true
        if $WD_WAS_INSTALLED; then
            rm -f "$OWNER_HOME/Library/LaunchAgents/com.k-agents.watchdog.plist"
            if $DARWIN && [ "$(id -u)" -eq 0 ]; then
                sudo -u "$OWNER" "$INSTALL/scripts/install-watchdog.sh" >/dev/null 2>&1 \
                    && log "watchdog reinstalled (com.kbots.watchdog)" \
                    || log "⚠ watchdog reinstall failed — run scripts/install-watchdog.sh manually"
            else
                "$INSTALL/scripts/install-watchdog.sh" >/dev/null 2>&1 \
                    && log "watchdog reinstalled" \
                    || log "⚠ watchdog reinstall failed — run scripts/install-watchdog.sh manually"
            fi
        fi
    fi

    # ── Install services under new names ─────────────────────────────────
    HAVE_SERVICE=true
    if [ "$SKIP_SYSTEM" = "1" ]; then
        HAVE_SERVICE=false
    elif $DARWIN; then
        OLD_PLIST="$OWNER_HOME/Library/LaunchAgents/com.k-agents.agent.plist"
        NEW_PLIST="$OWNER_HOME/Library/LaunchAgents/com.kbots.agent.plist"
        if [ -f "$OLD_PLIST" ]; then
            sed -e 's/com\.k-agents\./com.kbots./g' -e 's/KAGENTS_/KBOTS_/g' \
                -e "s|$OLD_INSTALL|$INSTALL|g" ${OLD_OVERLAY:+-e "s|$OLD_OVERLAY|$OVERLAY|g"} \
                "$OLD_PLIST" > "$NEW_PLIST"
            rm -f "$OLD_PLIST"
            chown "$OWNER" "$NEW_PLIST" 2>/dev/null || true
        fi
        if [ -f "$NEW_PLIST" ]; then
            _sudo launchctl bootstrap "gui/$OWNER_UID" "$NEW_PLIST"
            log "launchd: com.kbots.agent bootstrapped"
        else
            HAVE_SERVICE=false
            log "no launchd plist found — dirs/files migrated, no service to start"
        fi
    else
        for unit in /etc/systemd/system/k-agents*.service /etc/systemd/system/k-agents*.timer; do
            [ -f "$unit" ] || continue
            new_unit="/etc/systemd/system/$(basename "$unit" | sed 's/^k-agents/kbots/')"
            _sudo sh -c "sed -e 's/k-agents/kbots/g' -e 's/kagents/kbots/g' -e 's/KAGENTS_/KBOTS_/g' -e 's|$OLD_INSTALL|$INSTALL|g' ${OLD_OVERLAY:+-e 's|$OLD_OVERLAY|$OVERLAY|g'} '$unit' > '$new_unit' && rm -f '$unit'"
            log "unit: $(basename "$unit") → $(basename "$new_unit")"
        done
        if [ -f /etc/systemd/system/kbots.service ]; then
            _sudo systemctl daemon-reload
            _sudo systemctl enable --now kbots.service
            for t in /etc/systemd/system/kbots-*.timer; do
                [ -f "$t" ] && _sudo systemctl enable --now "$(basename "$t")" 2>/dev/null || true
            done
            log "systemd: kbots.service + timers enabled"
        else
            HAVE_SERVICE=false
            log "no systemd unit found — dirs/files migrated, no service to start"
        fi
    fi

    # ── Health check ─────────────────────────────────────────────────────
    if ! $HAVE_SERVICE; then
        log "✅ migration complete (no service installed) — install: $INSTALL"
        log "   backup kept at: $BACKUP"
        exit 0
    fi
    log "waiting for healthy boot (up to 60s)"
    healthy=false
    for _ in $(seq 1 12); do
        sleep 5
        if $DARWIN; then
            _sudo launchctl print "gui/$OWNER_UID/com.kbots.agent" >/dev/null 2>&1 && { healthy=true; break; }
        else
            systemctl is-active --quiet kbots.service && { healthy=true; break; }
        fi
    done
    if ! $healthy; then
        log "❌ service did not come up under the new name"
        log "   backup: $BACKUP — inspect logs, or run scripts/rollback-kbots-rename.sh"
        exit 1
    fi

    # ── Ownership audit ──────────────────────────────────────────────────
    if ! $DARWIN && id kbots >/dev/null 2>&1; then
        AUDIT_USER=kbots
    else
        AUDIT_USER=$(stat -f %Su "$INSTALL" 2>/dev/null || stat -c %U "$INSTALL")
    fi
    stray=$(find "$INSTALL" ${OVERLAY:+"$OVERLAY"} -not -user "$AUDIT_USER" -not -path '*/.git/*' 2>/dev/null | head -5 || true)
    [ -n "$stray" ] && log "⚠ files not owned by $AUDIT_USER (fix with chown -R): $stray"

    log "✅ migration complete — install: $INSTALL"
    log "   backup kept at: $BACKUP (delete once you're confident)"
    log "   NOTE: re-point your git remote after the GitHub repo rename:"
    log "   git remote set-url origin https://github.com/karvidsson/kbots.git"
}

main "$@"
