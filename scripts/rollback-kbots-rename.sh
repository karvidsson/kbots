#!/usr/bin/env bash
#
# rollback-kbots-rename.sh — best-effort reverse of migrate-to-kbots.sh.
#
#   cd <install-dir> && scripts/rollback-kbots-rename.sh
#
# Restores the legacy k-agents naming: dirs, units/plists, Linux service user,
# db and vault-key filenames, shell profiles, and generated agent files.
# Data is never deleted; the migration backup tarball is left untouched.
set -euo pipefail

log() { echo "[rollback-kbots] $*"; }
_sudo() { if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo -n "$@"; fi; }

main() {
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    INSTALL="$(cd "$SCRIPT_DIR/.." && pwd)"
    DARWIN=false; [ "$(uname)" = "Darwin" ] && DARWIN=true
    if $DARWIN; then
        OWNER=$(stat -f %Su "$INSTALL"); OWNER_UID=$(id -u "$OWNER")
        OWNER_HOME=$(eval echo "~$OWNER")
    else
        OWNER_HOME="$HOME"
    fi

    # Stop new-name services
    if $DARWIN; then
        _sudo launchctl bootout "gui/$OWNER_UID/com.kbots.agent" 2>/dev/null || true
        _sudo launchctl bootout "gui/$OWNER_UID/com.kbots.watchdog" 2>/dev/null || true
    else
        _sudo systemctl disable --now kbots.service 2>/dev/null || true
        for t in $(_sudo systemctl list-unit-files 'kbots-*.timer' --no-legend 2>/dev/null | awk '{print $1}'); do
            _sudo systemctl disable --now "$t" 2>/dev/null || true
        done
    fi

    # Linux service user back
    if ! $DARWIN && id kbots >/dev/null 2>&1 && ! id kagents >/dev/null 2>&1; then
        _sudo usermod -l kagents kbots
        _sudo groupmod -n kagents kbots
        [ "$(eval echo ~kagents)" = "/home/kbots" ] && _sudo usermod -d /home/kagents -m kagents
        log "service user kbots → kagents"
    fi
    for sf in kbots-fullcontrol kbots-watchdog; do
        if [ -f "/etc/sudoers.d/$sf" ]; then
            old_sf=$(echo "$sf" | sed -e 's/kbots-fullcontrol/kagents-fullcontrol/' -e 's/kbots-watchdog/k-agents-watchdog/')
            _sudo sh -c "sed -e 's/kbots/kagents/g' /etc/sudoers.d/$sf > /etc/sudoers.d/$old_sf && chmod 0440 /etc/sudoers.d/$old_sf && visudo -cf /etc/sudoers.d/$old_sf >/dev/null && rm -f /etc/sudoers.d/$sf"
        fi
    done

    # Dirs back
    NEW_INSTALL="$INSTALL"
    if [ "$(basename "$INSTALL")" = "kbots" ]; then
        OLD_INSTALL="$(dirname "$INSTALL")/k-agents"
        mv "$INSTALL" "$OLD_INSTALL"; INSTALL="$OLD_INSTALL"
        log "install dir back to $INSTALL"
    fi
    OVERLAY=""
    NEW_OVERLAY="$(dirname "$INSTALL")/kbots-overlay"
    if [ -d "$NEW_OVERLAY" ]; then
        OVERLAY="$(dirname "$INSTALL")/k-agents-overlay"
        mv "$NEW_OVERLAY" "$OVERLAY"
        log "overlay back to $OVERLAY"
    fi
    [ -d "$(dirname "$INSTALL")/kbots-modules" ] && mv "$(dirname "$INSTALL")/kbots-modules" "$(dirname "$INSTALL")/k-agents-modules"

    # Data files back
    for d in "$INSTALL/data" ${OVERLAY:+"$OVERLAY/data"}; do
        [ -f "$d/kbots.db" ] && [ ! -f "$d/kagents.db" ] && mv "$d/kbots.db" "$d/kagents.db"
        rm -f "$d"/kbots*.lock 2>/dev/null || true
    done
    for h in "$OWNER_HOME" $( ! $DARWIN && id kagents >/dev/null 2>&1 && eval echo "~kagents" ); do
        [ -f "$h/.config/kbots-vault-key" ] && [ ! -f "$h/.config/k-agents-vault-key" ] \
            && mv "$h/.config/kbots-vault-key" "$h/.config/k-agents-vault-key"
    done

    # Profiles + generated files back
    for pf in "$OWNER_HOME/.zshrc" "$OWNER_HOME/.zprofile" "$OWNER_HOME/.bashrc" "$OWNER_HOME/.bash_profile" "$OWNER_HOME/.profile"; do
        [ -f "$pf" ] || continue
        sed -i.bak -e 's/# kbots environment/# K-Agents environment/' -e 's/export KBOTS_/export KAGENTS_/g' \
            -e "s|$NEW_INSTALL|$INSTALL|g" "$pf" && rm -f "$pf.bak"
    done
    if [ -n "$OVERLAY" ] && [ -d "$OVERLAY" ]; then
        find "$OVERLAY" -maxdepth 3 \( -name '.mcp.json' -o -name 'settings.json' -o -name 'settings.local.json' \) 2>/dev/null | while read -r f; do
            sed -i.bak -e 's/kbots-tools/k-agents-tools/g' -e 's/KBOTS_/KAGENTS_/g' \
                -e "s|$NEW_INSTALL|$INSTALL|g" -e "s|$NEW_OVERLAY|$OVERLAY|g" "$f" && rm -f "$f.bak"
        done
        for cf in "$OVERLAY"/config/config*.yaml; do
            [ -f "$cf" ] && sed -i.bak 's/^kbots:/kagents:/' "$cf" && rm -f "$cf.bak"
        done
    fi

    # Reinstall legacy-named services from the (new-named) templates
    if $DARWIN; then
        NEW_PLIST="$OWNER_HOME/Library/LaunchAgents/com.kbots.agent.plist"
        OLD_PLIST="$OWNER_HOME/Library/LaunchAgents/com.k-agents.agent.plist"
        if [ -f "$NEW_PLIST" ]; then
            sed -e 's/com\.kbots\./com.k-agents./g' -e 's/KBOTS_/KAGENTS_/g' \
                -e "s|$NEW_INSTALL|$INSTALL|g" ${NEW_OVERLAY:+-e "s|$NEW_OVERLAY|${OVERLAY:-}|g"} \
                "$NEW_PLIST" > "$OLD_PLIST"
            rm -f "$NEW_PLIST"; chown "$OWNER" "$OLD_PLIST" 2>/dev/null || true
        fi
        _sudo launchctl bootstrap "gui/$OWNER_UID" "$OLD_PLIST"
    else
        for unit in /etc/systemd/system/kbots*.service /etc/systemd/system/kbots*.timer; do
            [ -f "$unit" ] || continue
            old_unit="/etc/systemd/system/$(basename "$unit" | sed 's/^kbots/k-agents/')"
            _sudo sh -c "sed -e 's|$NEW_INSTALL|$INSTALL|g' ${OVERLAY:+-e 's|$NEW_OVERLAY|$OVERLAY|g'} \
                -e 's/kbots\.service/k-agents.service/g' -e 's/kbots-/k-agents-/g' \
                -e 's/KBOTS_/KAGENTS_/g' -e 's/^User=kbots/User=kagents/' -e 's/^Group=kbots/Group=kagents/' \
                -e 's/SyslogIdentifier=kbots/SyslogIdentifier=k-agents/' '$unit' > '$old_unit' && rm -f '$unit'"
        done
        _sudo systemctl daemon-reload
        _sudo systemctl enable --now k-agents.service 2>/dev/null || true
    fi

    log "✅ rolled back to legacy k-agents naming. Note: the checked-out code on"
    log "   this install is post-rename; pin to a pre-rename commit if you also"
    log "   need the old code: git checkout <pre-rename-tag>"
}

main "$@"
