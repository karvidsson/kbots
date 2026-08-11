#!/bin/bash
# health-audit.sh — System health + security audit for kbots deployments
# Timer mode (default): alerts only on issues, heartbeat on success
# Report mode (--report): full PASS/WARN/FAIL report for on-demand checks
set -euo pipefail

KBOTS_HOME="${KBOTS_HOME:-$(cd "$(dirname "$0")/.." && pwd)}"
source "$KBOTS_HOME/scripts/lib-alert.sh"
LOG_PREFIX="[health-audit]"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $LOG_PREFIX $1"; }

REPORT_MODE=false
[ "${1:-}" = "--report" ] && REPORT_MODE=true

# ── Load config ──────────────────────────────────────────────────────────
CONFIG=""
[ -n "${KBOTS_OVERLAY:-}" ] && [ -f "$KBOTS_OVERLAY/config/health-config.yaml" ] && CONFIG="$KBOTS_OVERLAY/config/health-config.yaml"
[ -z "$CONFIG" ] && [ -f "$KBOTS_HOME/config/health-config.yaml" ] && CONFIG="$KBOTS_HOME/config/health-config.yaml"

_yaml_val() {
    "$KBOTS_HOME/.venv/bin/python" -c "
import yaml, sys
c = yaml.safe_load(open('$CONFIG'))
keys = '$1'.split('.')
v = c
for k in keys:
    if isinstance(v, dict):
        v = v.get(k, '$2')
    else:
        v = '$2'
        break
print(v if v is not None else '$2', end='')
" 2>/dev/null || echo -n "$2"
}

_yaml_list() {
    "$KBOTS_HOME/.venv/bin/python" -c "
import yaml
c = yaml.safe_load(open('$CONFIG'))
keys = '$1'.split('.')
v = c
for k in keys:
    if isinstance(v, dict):
        v = v.get(k, [])
    else:
        v = []
        break
if isinstance(v, list):
    for item in v:
        print(item)
" 2>/dev/null
}

# Counters
PASSED=0; WARNED=0; FAILED=0
ISSUES=""
REPORT=""

_ok()   { PASSED=$((PASSED + 1)); REPORT="${REPORT}\n  ✓ $1"; }
_warn() { WARNED=$((WARNED + 1)); REPORT="${REPORT}\n  ⚠ $1"; ISSUES="${ISSUES}\n- **WARN**: $1"; }
_fail() { FAILED=$((FAILED + 1)); REPORT="${REPORT}\n  ✗ $1"; ISSUES="${ISSUES}\n- **FAIL**: $1"; }

if [ -z "$CONFIG" ]; then
    echo "ERROR: No health-config.yaml found"
    exit 1
fi

DEPLOYMENT=$(_yaml_val "deployment" "unknown")
KBOTS_DIR=$(_yaml_val "paths.kbots_home" "$KBOTS_HOME")
OVERLAY=$(_yaml_val "paths.overlay" "${KBOTS_OVERLAY:-}")
VAULT_PATH=$(_yaml_val "paths.vault" "")
DB_PATH=$(_yaml_val "paths.memory_db" "$KBOTS_DIR/data/memory.db")
EMBEDDINGS_DIR=$(_yaml_val "paths.embeddings_model" "$KBOTS_DIR/data/models/bge-small-en-v1.5")
DISK_THRESH=$(_yaml_val "thresholds.disk_percent" "80")
RAM_THRESH=$(_yaml_val "thresholds.ram_percent" "85")
SWAP_THRESH=$(_yaml_val "thresholds.swap_percent" "70")
LOAD_THRESH=$(_yaml_val "thresholds.load_per_cpu" "1.5")

# ── 1. Services ──────────────────────────────────────────────────────────
REPORT="${REPORT}\n\n## Services"
while IFS= read -r svc; do
    [ -z "$svc" ] && continue
    if systemctl is-active "$svc" >/dev/null 2>&1; then
        _ok "$svc running"
    else
        _fail "$svc NOT running"
    fi
    restarts=$(systemctl show "$svc" --property=NRestarts --value 2>/dev/null || echo "0")
    [ "$restarts" -gt 5 ] 2>/dev/null && _warn "$svc restarted $restarts times (lifetime)"
done < <(_yaml_list "services")

# ── 2. Timers ────────────────────────────────────────────────────────────
REPORT="${REPORT}\n\n## Timers"
mapfile -t LIVE_TIMERS < <(systemctl list-units --type=timer --no-legend --plain 'kbots-*' 2>/dev/null | awk '{print $1}' | sed 's/\.timer$//')
if [ ${#LIVE_TIMERS[@]} -gt 0 ]; then
    for timer in "${LIVE_TIMERS[@]}"; do
        [ -z "$timer" ] && continue
        state=$(systemctl show "${timer}.timer" --property=ActiveState --value 2>/dev/null)
        if [ "$state" = "active" ]; then
            _ok "${timer}.timer active"
        else
            _fail "${timer}.timer NOT active (state: $state)"
        fi
    done
else
    _warn "No kbots-* timers found in systemd"
fi
while IFS= read -r cfg_timer; do
    [ -z "$cfg_timer" ] && continue
    found=false
    for lt in "${LIVE_TIMERS[@]}"; do
        [ "$lt" = "$cfg_timer" ] && found=true && break
    done
    [ "$found" = "false" ] && _warn "${cfg_timer}.timer in config but not in systemd — stale?"
done < <(_yaml_list "timers")

# ── 3. Resources ────────────────────────────────────────────────────────
REPORT="${REPORT}\n\n## Resources"

disk_pct=$(df / --output=pcent | tail -1 | tr -d ' %')
if [ "$disk_pct" -gt "$DISK_THRESH" ]; then
    _fail "Disk: ${disk_pct}% (threshold: ${DISK_THRESH}%)"
elif [ "$disk_pct" -gt "$((DISK_THRESH - 10))" ]; then
    _warn "Disk: ${disk_pct}% (approaching ${DISK_THRESH}%)"
else
    _ok "Disk: ${disk_pct}%"
fi

mem_pct=$(free | awk '/Mem:/ {printf "%.0f", ($2-$7)/$2*100}')
if [ "$mem_pct" -gt "$RAM_THRESH" ]; then
    _fail "RAM: ${mem_pct}% (threshold: ${RAM_THRESH}%)"
elif [ "$mem_pct" -gt "$((RAM_THRESH - 10))" ]; then
    _warn "RAM: ${mem_pct}% (approaching ${RAM_THRESH}%)"
else
    _ok "RAM: ${mem_pct}%"
fi

swap_pct=$(free | awk '/Swap:/ {if ($2>0) printf "%.0f", $3/$2*100; else print "0"}')
if [ "$swap_pct" -gt "$SWAP_THRESH" ]; then
    _fail "Swap: ${swap_pct}% (threshold: ${SWAP_THRESH}%)"
else
    _ok "Swap: ${swap_pct}%"
fi

cpus=$(nproc)
load=$(awk '{print $1}' /proc/loadavg)
max_load=$(echo "$cpus * $LOAD_THRESH" | bc)
if [ "$(echo "$load > $max_load" | bc)" = "1" ]; then
    _fail "Load: $load (threshold: $max_load for $cpus CPUs)"
else
    _ok "Load: $load ($cpus CPUs)"
fi

zombies=$(ps aux | awk '$8 ~ /Z/' | wc -l)
if [ "$zombies" -gt 0 ]; then
    _warn "Zombie processes: $zombies"
else
    _ok "No zombie processes"
fi

uptime_str=$(uptime -p)
_ok "Uptime: $uptime_str"

# ── 4. Network ──────────────────────────────────────────────────────────
REPORT="${REPORT}\n\n## Network"

if [ "$(_yaml_val "tailscale" "false")" = "True" ] || [ "$(_yaml_val "tailscale" "false")" = "true" ]; then
    if systemctl is-active tailscaled.service >/dev/null 2>&1; then
        _ok "Tailscale running"
    else
        _fail "Tailscale NOT running"
    fi
fi

if [ "$(_yaml_val "cloudflared" "false")" = "True" ] || [ "$(_yaml_val "cloudflared" "false")" = "true" ]; then
    if systemctl is-active cloudflared.service >/dev/null 2>&1; then
        _ok "Cloudflare tunnel running"
    else
        _fail "Cloudflare tunnel NOT running"
    fi
fi

if getent hosts github.com >/dev/null 2>&1; then
    _ok "DNS resolution working"
else
    _fail "DNS resolution failed (github.com)"
fi

# ── 5. Security ─────────────────────────────────────────────────────────
REPORT="${REPORT}\n\n## Security"

SVC_USER=kbots
bad_owner=$(find "$KBOTS_DIR" -not -user "$SVC_USER" -not -path '*/.git/*' 2>/dev/null | head -5)
if [ -n "$bad_owner" ]; then
    _fail "Files not owned by $SVC_USER: $bad_owner"
else
    _ok "All $KBOTS_DIR files owned by $SVC_USER:$SVC_USER"
fi

while IFS= read -r hook; do
    [ -z "$hook" ] && continue
    hook_path="$KBOTS_DIR/.git/hooks/$hook"
    if [ -x "$hook_path" ]; then
        _ok "Git hook $hook present + executable"
    elif [ -f "$hook_path" ]; then
        _warn "Git hook $hook present but NOT executable"
    else
        _fail "Git hook $hook MISSING"
    fi
done < <(_yaml_list "git_hooks")

failed_units=$(systemctl list-units --state=failed --no-legend --plain | head -5)
if [ -n "$failed_units" ]; then
    _fail "Failed systemd units: $failed_units"
else
    _ok "No failed systemd units"
fi

if curl -s --max-time 1 http://169.254.169.254/ >/dev/null 2>&1; then
    _fail "Cloud metadata endpoint reachable (should be blocked)"
fi

# Port exposure
_resolve_expected_ports() {
    local cfg=""
    [ -n "${KBOTS_OVERLAY:-}" ] && [ -f "$KBOTS_OVERLAY/config/config.yaml" ] && cfg="$KBOTS_OVERLAY/config/config.yaml"
    [ -z "$cfg" ] && [ -f "$KBOTS_HOME/config/config.yaml" ] && cfg="$KBOTS_HOME/config/config.yaml"
    [ -n "$cfg" ] && "$KBOTS_HOME/.venv/bin/python" -c "
import yaml, sys
c = yaml.safe_load(open('$cfg'))
print(c.get('security', {}).get('expected_ports', ''), end='')
" 2>/dev/null || echo -n ""
}
EXPECTED_PUBLIC="${KBOTS_EXPECTED_PORTS:-$(_resolve_expected_ports)}"
EXPECTED_PUBLIC="${EXPECTED_PUBLIC:-22 80 443}"
PUBLIC_PORTS=$(ss -tlnp 2>/dev/null | grep -v '127\.\|172\.1[6-9]\.\|172\.2[0-9]\.\|172\.3[0-1]\.\|::1\|100\.6[4-9]\.\|100\.[7-9][0-9]\.\|100\.1[0-1][0-9]\.\|100\.12[0-7]\.\|fd7a:115c:a1e0' | awk 'NR>1 {print $4}' | grep -oE '[0-9]+$' | sort -nu)
for port in $PUBLIC_PORTS; do
    expected=false
    for ep in $EXPECTED_PUBLIC; do
        [ "$port" = "$ep" ] && expected=true && break
    done
    if [ "$expected" = "false" ]; then
        proc=$(ss -tlnp "sport = :$port" 2>/dev/null | tail -1 | grep -oP 'users:\(\("\K[^"]+' || echo "unknown")
        case "$proc" in tailscaled|tailscale*) continue ;; esac
        _warn "Port $port ($proc) — not in expected list"
    fi
done

# ── 6. Memory System ───────────────────────────────────────────────────
REPORT="${REPORT}\n\n## Memory System"

if [ -f "$DB_PATH" ]; then
    mem_count=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM memories;" 2>/dev/null || echo "ERROR")
    if [ "$mem_count" != "ERROR" ]; then
        _ok "Memory DB readable ($mem_count memories)"
    else
        _fail "Memory DB exists but query failed"
    fi

    integrity=$(sqlite3 "$DB_PATH" "PRAGMA integrity_check;" 2>/dev/null || echo "FAILED")
    if [ "$integrity" = "ok" ]; then
        _ok "Memory DB integrity OK"
    else
        _fail "Memory DB integrity: $integrity"
    fi

    WAL_PATH="${DB_PATH}-wal"
    if [ -f "$WAL_PATH" ]; then
        wal_bytes=$(stat -c%s "$WAL_PATH" 2>/dev/null || echo "0")
        wal_mb=$((wal_bytes / 1048576))
        if [ "$wal_mb" -gt 50 ]; then
            _warn "Memory DB WAL is ${wal_mb}MB — consider checkpoint"
        else
            _ok "Memory DB WAL: ${wal_mb}MB"
        fi
    fi

    fts_count=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM memories_fts;" 2>/dev/null)
    if [ $? -eq 0 ] && [ -n "$fts_count" ]; then
        _ok "FTS5 index healthy ($fts_count entries)"
    else
        _warn "FTS5 index missing or corrupt"
    fi

    mem_stats=$(sqlite3 "$DB_PATH" "SELECT COUNT(*), SUM(CASE WHEN pinned=1 THEN 1 ELSE 0 END), SUM(CASE WHEN embedding IS NOT NULL THEN 1 ELSE 0 END) FROM memories;" 2>/dev/null)
    if [ -n "$mem_stats" ]; then
        total=$(echo "$mem_stats" | cut -d'|' -f1)
        pinned=$(echo "$mem_stats" | cut -d'|' -f2)
        embedded=$(echo "$mem_stats" | cut -d'|' -f3)
        _ok "Memories — total: $total, pinned: ${pinned:-0}, embeddings: ${embedded:-0}"
    fi

    if [ -d "$EMBEDDINGS_DIR" ]; then
        _ok "Embeddings model present"
    else
        _warn "Embeddings model not found — semantic search disabled"
    fi
else
    _fail "Memory DB NOT FOUND at $DB_PATH"
fi

# ── 7. Vault ────────────────────────────────────────────────────────────
REPORT="${REPORT}\n\n## Vault"

if [ -n "$VAULT_PATH" ] && [ -f "$VAULT_PATH" ]; then
    vault_mode=$(stat -c%a "$VAULT_PATH" 2>/dev/null || echo "???")
    if [ "$vault_mode" = "600" ]; then
        _ok "Vault present (mode $vault_mode)"
    else
        _warn "Vault mode $vault_mode (expected 600)"
    fi

    salt_path="${VAULT_PATH%.enc}.salt"
    [ -f "${VAULT_PATH%.*}.salt" ] && salt_path="${VAULT_PATH%.*}.salt"
    if [ -f "$salt_path" ]; then
        _ok "Per-instance salt present"
    else
        _warn "Using legacy hardcoded salt"
    fi

    KEY_FILE="$HOME/.config/kbots-vault-key"
    if [ -f "$KEY_FILE" ]; then
        vault_keys=$(cd "$KBOTS_DIR" && "$KBOTS_DIR/.venv/bin/python" -c "
from src.vault.fernet import FernetVault
v = FernetVault('$VAULT_PATH')
v.unlock(open('$KEY_FILE').read().strip())
keys = v.list_keys()
print('|'.join(sorted(keys)))
" 2>/dev/null || echo "ERROR")
        if [ "$vault_keys" != "ERROR" ] && [ -n "$vault_keys" ]; then
            key_count=$(echo "$vault_keys" | tr '|' '\n' | wc -l)
            _ok "Vault unlocks OK ($key_count keys)"

            while IFS= read -r ek; do
                [ -z "$ek" ] && continue
                if echo "$vault_keys" | tr '|' '\n' | grep -qx "$ek"; then
                    _ok "Vault key $ek present"
                else
                    _fail "Vault key $ek MISSING"
                fi
            done < <(_yaml_list "vault_expected_keys")
        elif [ "$vault_keys" = "ERROR" ]; then
            _fail "Vault unlock FAILED"
        else
            _warn "Vault unlocks but empty"
        fi
    else
        _warn "Vault key file not found — cannot test unlock"
    fi
elif [ -n "$VAULT_PATH" ]; then
    _fail "Vault NOT FOUND at $VAULT_PATH"
fi

# ── 8. MCP Servers ──────────────────────────────────────────────────────
REPORT="${REPORT}\n\n## MCP Servers"

MCP_CFG=""
[ -n "$OVERLAY" ] && [ -f "$OVERLAY/config/mcp.yaml" ] && MCP_CFG="$OVERLAY/config/mcp.yaml"
[ -z "$MCP_CFG" ] && [ -f "$KBOTS_DIR/config/mcp.yaml" ] && MCP_CFG="$KBOTS_DIR/config/mcp.yaml"

if [ -n "$MCP_CFG" ]; then
    mcp_result=$("$KBOTS_HOME/.venv/bin/python" -c "
import yaml, sys
try:
    c = yaml.safe_load(open('$MCP_CFG'))
    servers = c.get('servers', {})
    active = {k: v for k, v in servers.items() if not str(k).startswith('#')}
    print(f'{len(active)} servers')
    for name, srv in active.items():
        t = srv.get('transport', '')
        if t == 'stdio' and srv.get('command'):
            print(f'stdio|{name}|{srv[\"command\"].split()[0]}')
except yaml.YAMLError as e:
    print(f'YAML_ERROR|{e}', file=sys.stderr)
    sys.exit(1)
" 2>/dev/null)
    if [ $? -eq 0 ]; then
        server_count=$(echo "$mcp_result" | head -1)
        _ok "MCP config valid ($server_count)"
        while IFS='|' read -r transport name cmd; do
            [ "$transport" != "stdio" ] && continue
            if command -v "$cmd" >/dev/null 2>&1 || [ -x "$cmd" ]; then
                _ok "MCP $name command $cmd found"
            else
                _warn "MCP $name command $cmd not found"
            fi
        done < <(echo "$mcp_result" | tail -n +2)
    else
        _fail "MCP config YAML error"
    fi
else
    _ok "No MCP config (none configured)"
fi

# ── 9. Tools & Skills ──────────────────────────────────────────────────
REPORT="${REPORT}\n\n## Tools & Skills"

tool_result=$("$KBOTS_HOME/.venv/bin/python" -c "
import ast, os, yaml
from pathlib import Path

kroot = '$KBOTS_DIR'
modules_env = os.environ.get('KBOTS_MODULES', '')
overlay = '$OVERLAY'

tool_dirs = [os.path.join(kroot, 'src', 'tools')]
if modules_env:
    for p in modules_env.split(':'):
        p = p.strip()
        if p and Path(p).is_dir():
            for sub in Path(p).iterdir():
                td = sub / 'tools'
                if td.is_dir():
                    tool_dirs.append(str(td))

total = 0; errors = []
for td in tool_dirs:
    if not Path(td).is_dir(): continue
    for f in Path(td).glob('*.py'):
        if f.name.startswith('_'): continue
        total += 1
        try:
            ast.parse(f.read_text())
        except SyntaxError as e:
            errors.append(f'{f.name}: {e}')

skill_dirs = [os.path.join(kroot, 'skills')]
if overlay and Path(os.path.join(overlay, 'skills')).is_dir():
    skill_dirs.append(os.path.join(overlay, 'skills'))
if modules_env:
    for p in modules_env.split(':'):
        p = p.strip()
        if p and Path(p).is_dir():
            for sub in Path(p).iterdir():
                sd = sub / 'skills'
                if sd.is_dir():
                    skill_dirs.append(str(sd))

total_skills = 0; skill_errors = []
for sd in skill_dirs:
    if not Path(sd).is_dir(): continue
    for yf in list(Path(sd).glob('*.yaml')) + list(Path(sd).glob('*.yml')):
        total_skills += 1
        try:
            d = yaml.safe_load(yf.read_text())
            if not d or not isinstance(d, dict):
                skill_errors.append(f'{yf.name}: empty or invalid')
            elif 'name' not in d:
                skill_errors.append(f'{yf.name}: missing name')
        except yaml.YAMLError as e:
            skill_errors.append(f'{yf.name}: {e}')

print(f'{total} tools across {len(tool_dirs)} dirs')
print(f'{total_skills} skills across {len(skill_dirs)} dirs')
for e in errors:
    print(f'TOOL_ERR|{e}')
for e in skill_errors:
    print(f'SKILL_ERR|{e}')
" 2>/dev/null)

tool_line=$(echo "$tool_result" | head -1)
skill_line=$(echo "$tool_result" | sed -n '2p')
_ok "$tool_line (syntax OK)"
_ok "$skill_line"
while IFS= read -r errline; do
    [ -z "$errline" ] && continue
    case "$errline" in
        TOOL_ERR\|*)  _fail "Tool ${errline#TOOL_ERR|}" ;;
        SKILL_ERR\|*) _warn "Skill ${errline#SKILL_ERR|}" ;;
    esac
done < <(echo "$tool_result" | tail -n +3)

# ── 10. Databases ──────────────────────────────────────────────────────
REPORT="${REPORT}\n\n## Databases"

DATA_DIR="${OVERLAY:+$OVERLAY/data}"
DATA_DIR="${DATA_DIR:-$KBOTS_DIR/data}"
if [ -d "$DATA_DIR" ]; then
    for db in "$DATA_DIR"/*.db; do
        [ ! -f "$db" ] && continue
        [ "$(basename "$db")" = "memory.db" ] && continue
        db_name=$(basename "$db")
        result=$(sqlite3 "$db" "PRAGMA integrity_check;" 2>/dev/null || echo "FAILED")
        if [ "$result" = "ok" ]; then
            size_kb=$(stat -c%s "$db" 2>/dev/null || echo "0")
            size_mb=$((size_kb / 1048576))
            _ok "$db_name integrity OK (${size_mb}MB)"
        else
            _fail "$db_name integrity FAILED"
        fi
    done
fi

# ── 11. Application Health ─────────────────────────────────────────────
REPORT="${REPORT}\n\n## Application Health"

if [ "$(_yaml_val "oauth_check" "false")" = "True" ] || [ "$(_yaml_val "oauth_check" "false")" = "true" ]; then
    OAUTH_PATH=$(_yaml_val "paths.oauth_token" "$KBOTS_DIR/config/google-token.json")
    if [ -f "$OAUTH_PATH" ]; then
        oauth_status=$("$KBOTS_HOME/.venv/bin/python" -c "
import json
from datetime import datetime, timezone
d = json.load(open('$OAUTH_PATH'))
exp = d.get('expiry', '')
if exp:
    e = datetime.fromisoformat(exp.replace('Z', '+00:00'))
    print('valid' if e > datetime.now(timezone.utc) else 'expired')
else:
    print('no_expiry')
" 2>/dev/null || echo "parse_error")
        case "$oauth_status" in
            valid)       _ok "OAuth token valid" ;;
            expired)     _fail "OAuth token EXPIRED" ;;
            no_expiry)   _ok "OAuth token present" ;;
            *)           _warn "OAuth token could not parse" ;;
        esac
    else
        _warn "OAuth token not found"
    fi
fi

# Config YAML validity
if [ -n "$OVERLAY" ]; then
    while IFS= read -r cfg_file; do
        [ -z "$cfg_file" ] && continue
        cfg_path="$OVERLAY/$cfg_file"
        if [ -f "$cfg_path" ]; then
            if "$KBOTS_HOME/.venv/bin/python" -c "import yaml; yaml.safe_load(open('$cfg_path'))" 2>/dev/null; then
                _ok "$cfg_file valid YAML"
            else
                _fail "$cfg_file YAML parse error"
            fi
        fi
    done < <(_yaml_list "config_files")
fi

# Agent temp dirs
while IFS= read -r tmpdir; do
    [ -z "$tmpdir" ] && continue
    d="${OVERLAY:+$OVERLAY/$tmpdir}"
    if [ -z "$d" ]; then continue; fi
    if [ ! -d "$d" ]; then
        _fail "$tmpdir missing"
    elif [ ! -w "$d" ]; then
        _fail "$tmpdir not writable"
    else
        _ok "$tmpdir exists + writable"
    fi
done < <(_yaml_list "agent_tmp")

# ── 12. Logs ────────────────────────────────────────────────────────────
REPORT="${REPORT}\n\n## Logs"

journal_size=$(journalctl --disk-usage 2>/dev/null | grep -oP '[\d.]+[MG]' | head -1 || true)
[ -n "$journal_size" ] && _ok "Journal size: $journal_size"

while IFS= read -r svc; do
    [ -z "$svc" ] && continue
    err_count=$(journalctl -u "$svc" --since '6 hours ago' --no-pager -p err 2>/dev/null | grep -cv '^--\|^$\|^Hint:' || echo 0)
    [ "$err_count" -gt 0 ] 2>/dev/null && _warn "$svc — $err_count error lines in last 6h"
done < <(_yaml_list "services") || true

# ── 13. Git State ───────────────────────────────────────────────────────
REPORT="${REPORT}\n\n## Git State"

MODULES_DIR=$(_yaml_val "paths.modules" "")
for label_path in "Core|$KBOTS_DIR" "Modules|$MODULES_DIR" "Overlay|$OVERLAY"; do
    label="${label_path%%|*}"
    path="${label_path#*|}"
    [ -z "$path" ] || [ ! -d "$path" ] && continue
    dirty=$(git -C "$path" status --porcelain 2>/dev/null | head -5)
    if [ -n "$dirty" ]; then
        _warn "$label ($path) has uncommitted changes"
    else
        _ok "$label clean"
    fi
done

# ── 14. Cleanup ─────────────────────────────────────────────────────────
cleaned=$(find /tmp -maxdepth 1 -name "kbots-*" -type f -user "$SVC_USER" -mmin +120 -delete -print 2>/dev/null | wc -l)
[ "$cleaned" -gt 0 ] && log "Cleaned $cleaned old temp files"

journal_mb=$(journalctl --disk-usage 2>/dev/null | grep -oP '\d+\.\d+M' | head -1 | tr -d 'M' || true)
journal_mb="${journal_mb:-0}"
if [ "$(echo "$journal_mb > 200" | bc 2>/dev/null || echo 0)" = "1" ]; then
    journalctl --vacuum-size=100M >/dev/null 2>&1
    log "Rotated journal (was ${journal_mb}MB)"
fi

# ── Summary ─────────────────────────────────────────────────────────────
TOTAL=$((PASSED + WARNED + FAILED))
if [ "$FAILED" -gt 0 ]; then
    STATUS="UNHEALTHY"
elif [ "$WARNED" -gt 0 ]; then
    STATUS="DEGRADED"
else
    STATUS="HEALTHY"
fi

TIMESTAMP=$(date -u +"%Y-%m-%d %H:%M UTC")

HEADER="System Audit: ${DEPLOYMENT^^}
${STATUS} — ${PASSED} passed, ${WARNED} warnings, ${FAILED} failures
Run at ${TIMESTAMP}"
FULL_REPORT="${HEADER}$(echo -e "$REPORT")"

if [ "$REPORT_MODE" = "true" ]; then
    # Full report — post to Discord ops-alert channel
    send_report "$FULL_REPORT" "health-audit-$(date -u +%Y%m%d-%H%M).txt"
    echo "${STATUS} — ${PASSED} passed, ${WARNED} warnings, ${FAILED} failures. Posted to #ops-alert."
else
    # Timer mode: alert on issues, heartbeat on success
    if [ -n "$ISSUES" ]; then
        send_alert "HEALTH | ${STATUS} (${PASSED}✓ ${WARNED}⚠ ${FAILED}✗):$(echo -e "$ISSUES")"
        log "Issues found:$(echo -e "$ISSUES")"
    else
        send_alert "HEARTBEAT | All checks passed — ${PASSED}✓ (disk=${disk_pct}%, ram=${mem_pct}%, swap=${swap_pct}%, load=${load})"
        log "All checks passed (disk=${disk_pct}%, ram=${mem_pct}%, swap=${swap_pct}%, load=${load})"
    fi
fi
