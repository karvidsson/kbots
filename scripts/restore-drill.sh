#!/usr/bin/env bash
# restore-drill.sh — prove the newest backup can actually be restored.
#
# An untested backup is a belief, not a backup. This decrypts an archive into a
# throwaway 0700 directory and checks the three things that decide whether this
# machine could be rebuilt from it:
#
#   1. the vault opens with the vault key file, and holds the secrets it should
#   2. every database passes SQLite's own integrity check and has its tables
#   3. the configuration parses as YAML
#
# It never touches the live install, never prints a secret value, and removes
# the decrypted copy on every exit path.
#
#   scripts/restore-drill.sh                  drill the newest archive
#   scripts/restore-drill.sh <archive.enc>    drill a specific one
set -uo pipefail

DEST="${KBOTS_BACKUP_DIR:-$HOME/kbots-backups}"
KEY_FILE="${KBOTS_BACKUP_KEY_FILE:-$HOME/.config/kbots-backup-key}"
VAULT_KEY_FILE="${KBOTS_VAULT_KEY_FILE:-$HOME/.config/kbots-vault-key}"

pass=0
fail=0
ok() { printf '  PASS  %s\n' "$*"; pass=$((pass + 1)); }
bad() { printf '  FAIL  %s\n' "$*"; fail=$((fail + 1)); }
die() { printf '[drill] ERROR: %s\n' "$*" >&2; exit 2; }

ARCHIVE="${1:-}"
if [ -z "$ARCHIVE" ]; then
    # shellcheck disable=SC2012
    ARCHIVE="$(ls -1t "$DEST"/kbots-*.tar.gz.enc 2>/dev/null | head -1)"
fi
[ -n "$ARCHIVE" ] && [ -f "$ARCHIVE" ] || die "no archive found (looked in $DEST)"
[ -s "$KEY_FILE" ] || die "backup key file missing or empty: $KEY_FILE"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/kbots-drill.XXXXXX")"
chmod 700 "$WORK"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT INT TERM

printf '[drill] %s (%s, written %s)\n' "$(basename "$ARCHIVE")" \
    "$(du -h "$ARCHIVE" | cut -f1)" \
    "$(date -r "$ARCHIVE" '+%Y-%m-%d %H:%M')"

if openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass "file:$KEY_FILE" -in "$ARCHIVE" \
    | tar xzf - -C "$WORK" 2>/dev/null; then
    ok "archive decrypts and unpacks"
else
    bad "archive does not decrypt with $KEY_FILE"
    printf '[drill] %d passed, %d failed\n' "$pass" "$fail"
    exit 1
fi

[ -f "$WORK/MANIFEST" ] && sed 's/^/        /' "$WORK/MANIFEST"

# 1. The vault. Names are counted, values are never read or printed.
if [ -f "$WORK/config/secrets.enc" ]; then
    if [ -s "$VAULT_KEY_FILE" ]; then
        secrets="$(KBOTS_DRILL_VAULT="$WORK/config/secrets.enc" KBOTS_DRILL_KEY="$VAULT_KEY_FILE" \
            python3 - <<'PY' 2>/dev/null
import os, sys
sys.path.insert(0, os.environ.get("KBOTS_ROOT", os.path.expanduser("~/kbots")))
from src.vault.fernet import FernetVault
vault = FernetVault(vault_path=os.environ["KBOTS_DRILL_VAULT"])
vault.unlock(open(os.environ["KBOTS_DRILL_KEY"]).read().strip())
print(len(vault._secrets))
PY
        )"
        if [ -n "$secrets" ] && [ "$secrets" -gt 0 ] 2>/dev/null; then
            ok "vault opens with the vault key file ($secrets secrets)"
        else
            bad "vault did not open with $VAULT_KEY_FILE"
        fi
    else
        bad "vault key file missing: $VAULT_KEY_FILE (the backup alone cannot restore it)"
    fi
else
    bad "archive contains no vault"
fi

# 2. Databases: SQLite's own verdict, plus a table count so an empty file fails.
databases=0
while IFS= read -r db; do
    databases=$((databases + 1))
    name="${db#"$WORK"/data/}"
    verdict="$(sqlite3 "$db" 'PRAGMA integrity_check;' 2>/dev/null | head -1)"
    tables="$(sqlite3 "$db" "SELECT count(*) FROM sqlite_master WHERE type='table';" 2>/dev/null)"
    if [ "$verdict" = "ok" ] && [ -n "$tables" ] && [ "$tables" -gt 0 ] 2>/dev/null; then
        ok "$name ($tables tables)"
    else
        bad "$name (integrity: ${verdict:-unreadable}, tables: ${tables:-0})"
    fi
done < <(find "$WORK/data" -name '*.db' -type f 2>/dev/null)
[ "$databases" -gt 0 ] || bad "archive contains no databases"

# 3. Configuration.
for yaml in "$WORK"/config/*.yaml; do
    [ -f "$yaml" ] || continue
    if python3 -c 'import sys,yaml; yaml.safe_load(open(sys.argv[1]))' "$yaml" 2>/dev/null; then
        ok "$(basename "$yaml") parses"
    else
        bad "$(basename "$yaml") does not parse"
    fi
done

printf '[drill] %d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ] || exit 1
