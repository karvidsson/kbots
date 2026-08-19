#!/usr/bin/env bash
# sync.sh — Install all dependencies across layers.
#
# Replaces bare `uv sync` in the deploy ritual. Ensures Layer 2 packages
# (declared in requirements.txt files alongside KBOTS_MODULES modules) survive
# Core dependency reconciliation.
#
# Usage:
#   scripts/sync.sh          # from /opt/kbots, or
#   KBOTS_MODULES=... scripts/sync.sh   # with explicit layer 2 paths

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# --- Resolve layer paths from systemd if not in environment ---
# Find the service unit whose WorkingDirectory matches this repo. On a
# multi-install box (e.g. kbots.service + kbots-tutor.service) each instance
# has its own unit pointing at a different core dir.
_resolve_service_unit() {
    for unit in $(systemctl list-units --type=service --state=loaded --no-legend 2>/dev/null \
            | awk '/kbots/{print $1}'); do
        local wd env
        wd=$(systemctl show "$unit" -p WorkingDirectory --value 2>/dev/null || true)
        [ "$wd" != "$REPO_ROOT" ] && continue
        env=$(systemctl show "$unit" -p Environment --value 2>/dev/null || true)
        # Only match the main agent service — must have both layer paths
        echo "$env" | grep -qP 'KBOTS_MODULES=' || continue
        echo "$env" | grep -qP 'KBOTS_OVERLAY=' || continue
        echo "$unit"
        return
    done
}

if [ -z "${KBOTS_MODULES:-}" ] || [ -z "${KBOTS_OVERLAY:-}" ]; then
    _UNIT=$(_resolve_service_unit)
    if [ -n "${_UNIT:-}" ]; then
        _ENV=$(systemctl show "$_UNIT" -p Environment --value 2>/dev/null || true)
        if [ -z "${KBOTS_MODULES:-}" ]; then
            KBOTS_MODULES=$(echo "$_ENV" | tr ' ' '\n' | grep -oP '^KBOTS_MODULES=\K.*' || true)
            [ -n "$KBOTS_MODULES" ] && echo "[sync] KBOTS_MODULES resolved from $_UNIT"
        fi
        if [ -z "${KBOTS_OVERLAY:-}" ]; then
            KBOTS_OVERLAY=$(echo "$_ENV" | tr ' ' '\n' | grep -oP '^KBOTS_OVERLAY=\K.*' || true)
            [ -n "$KBOTS_OVERLAY" ] && echo "[sync] KBOTS_OVERLAY resolved from $_UNIT"
        fi
    fi
fi

# --- macOS / fallback: resolve KBOTS_OVERLAY from the launchd plist ---
# (systemd resolution above is Linux-only; without this the extras file below
# is never found on macOS and optional extras get pruned on every deploy.)
if [ -z "${KBOTS_OVERLAY:-}" ] && [ "$(uname)" = "Darwin" ]; then
    _plist="$HOME/Library/LaunchAgents/${KBOTS_LAUNCHD_LABEL:-com.kbots.agent}.plist"
    if [ -f "$_plist" ]; then
        KBOTS_OVERLAY=$(/usr/libexec/PlistBuddy \
            -c "Print :EnvironmentVariables:KBOTS_OVERLAY" "$_plist" 2>/dev/null || true)
        [ -n "${KBOTS_OVERLAY:-}" ] && echo "[sync] KBOTS_OVERLAY resolved from launchd plist"
    fi
fi

# --- Layer 1: Core (+ optional extras this deployment needs) ---
# Extras survive deploys (a bare `uv sync` would prune them). Configure via
# KBOTS_EXTRAS (comma/space-separated) and/or an $KBOTS_OVERLAY/extras file
# (one per line). e.g. "data reports" to install the analysis/reporting stacks.
_extras="${KBOTS_EXTRAS:-}"
if [ -n "${KBOTS_OVERLAY:-}" ] && [ -f "$KBOTS_OVERLAY/extras" ]; then
    _extras="$_extras $(tr ',\n' '  ' < "$KBOTS_OVERLAY/extras")"
fi
EXTRA_ARGS=()
for _e in $_extras; do
    [ -n "$_e" ] && EXTRA_ARGS+=(--extra "$_e")
done
if [ ${#EXTRA_ARGS[@]} -gt 0 ]; then
    echo "[sync] Layer 1: uv sync (Core) + extras:${_extras}"
    uv sync "${EXTRA_ARGS[@]}"
else
    echo "[sync] Layer 1: uv sync (Core)"
    uv sync
fi

# --- Vendored browser assets (best-effort; offline hosts fall back to CDN) ---
"$REPO_ROOT/scripts/vendor-mermaid.sh" || true

# --- Layer 2: KBOTS_MODULES modules ---
if [ -n "${KBOTS_MODULES:-}" ]; then
    IFS=':' read -ra modules_dirs <<< "$KBOTS_MODULES"
    for dir in "${modules_dirs[@]}"; do
        # KBOTS_MODULES entries point to module dirs (e.g. /opt/kbots-modules/my_module).
        # requirements.txt lives in the module root.
        req="$dir/requirements.txt"
        if [ -f "$req" ]; then
            echo "[sync] Layer 2: installing $req"
            uv pip install -r "$req"
        fi
    done
else
    echo "[sync] Layer 2: KBOTS_MODULES not set, skipping"
fi

# --- Layer 3: Overlay ---
if [ -n "${KBOTS_OVERLAY:-}" ] && [ -f "$KBOTS_OVERLAY/requirements.txt" ]; then
    echo "[sync] Layer 3: installing $KBOTS_OVERLAY/requirements.txt"
    uv pip install -r "$KBOTS_OVERLAY/requirements.txt"
fi

# --- Grandfather relocated extras (Core → extras/) ---
# When an update moves a module out of the always-loaded path (src/tools/ or
# skills/) into extras/, an install that was running it would silently lose it.
# update.sh/self-deploy.sh export the pre-pull rev; anything DELETED from the
# loaded path in that range and present under extras/ gets copied into the
# overlay — unless the overlay already has a file by that name (never
# overwritten, so deployments keep control). Fresh installs never cross the
# relocation range, so extras stay opt-in for them. --no-renames forces the
# moves to appear as deletions with their old path.
if [ -n "${KBOTS_GRANDFATHER_OLD_REV:-}" ] && [ -n "${KBOTS_OVERLAY:-}" ]; then
    while IFS= read -r deleted; do
        base=$(basename "$deleted")
        case "$deleted" in
            src/tools/*.py) sub=tools ;;
            skills/*.yaml)  sub=skills ;;
            *) continue ;;
        esac
        relocated=$(ls extras/*/"$base" 2>/dev/null | head -1 || true)
        [ -n "$relocated" ] || continue
        dest="$KBOTS_OVERLAY/$sub/$base"
        if [ ! -e "$dest" ]; then
            mkdir -p "$KBOTS_OVERLAY/$sub"
            cp "$relocated" "$dest"
            echo "[sync] grandfathered $base → overlay $sub/ (relocated to extras/ upstream)"
        fi
    done < <(git diff --name-only --diff-filter=D --no-renames \
             "$KBOTS_GRANDFATHER_OLD_REV"..HEAD -- src/tools skills 2>/dev/null || true)
fi

# --- Agent temp dirs ---
if [ -n "${KBOTS_OVERLAY:-}" ]; then
    mkdir -p "$KBOTS_OVERLAY/tmp"/{media,docs,scratch}
    echo "[sync] Agent tmp dirs ensured at $KBOTS_OVERLAY/tmp/"
fi

# --- Fix ownership if run as root ---
if [ "$(id -u)" -eq 0 ]; then
    # Chown to the install's actual owner — the kbots service user on Linux,
    # the login user on macOS (no service account there).
    _owner=$(stat -f '%Su:%Sg' "$REPO_ROOT" 2>/dev/null || stat -c '%U:%G' "$REPO_ROOT")
    echo "[sync] Ran as root — fixing ownership to $_owner"
    chown -R "$_owner" "$REPO_ROOT"
    [ -n "${KBOTS_OVERLAY:-}" ] && chown -R "$_owner" "$KBOTS_OVERLAY"
fi

echo "[sync] Done"
