#!/usr/bin/env bash
# backup.sh — one encrypted archive of the state that exists nowhere else.
#
# What is irreplaceable here is small: the vault, the configuration and the
# databases (memories, sessions, alerts, goals). Media, logs, caches and the
# checkouts are reproducible or disposable, so they are excluded on purpose and
# the archive stays in the tens of megabytes.
#
# Databases are copied with sqlite3 .backup, never with cp: a live WAL database
# copied byte-for-byte can restore as a corrupt file.
#
# The archive is encrypted with a key that is NOT the vault passphrase, so a
# backup sitting on another disk cannot unlock the vault on its own. The plain
# copy exists only inside a 0700 temp directory that is removed on every exit
# path, which is what keeps this compatible with the "never copy the vault"
# rule: one controlled, encrypted, short-lived copy, not an uncontrolled one.
#
#   scripts/backup.sh                 write a new archive, prune old ones
#   KBOTS_BACKUP_DIR=/Volumes/x/kb    where archives land (external drive, etc)
#   KBOTS_BACKUP_KEEP=14              how many to keep
#
# Restore is proved by scripts/restore-drill.sh, not by hoping.
set -euo pipefail

OVERLAY="${KBOTS_OVERLAY:-$HOME/kbots-overlay}"
DEST="${KBOTS_BACKUP_DIR:-$HOME/kbots-backups}"
KEEP="${KBOTS_BACKUP_KEEP:-14}"
KEY_FILE="${KBOTS_BACKUP_KEY_FILE:-$HOME/.config/kbots-backup-key}"

log() { printf '[backup] %s\n' "$*"; }
die() { printf '[backup] ERROR: %s\n' "$*" >&2; exit 1; }

[ -d "$OVERLAY" ] || die "overlay not found at $OVERLAY"
command -v sqlite3 >/dev/null || die "sqlite3 is required"
command -v openssl >/dev/null || die "openssl is required"

if [ ! -f "$KEY_FILE" ]; then
    mkdir -p "$(dirname "$KEY_FILE")"
    ( umask 077; openssl rand -base64 48 > "$KEY_FILE" )
    chmod 600 "$KEY_FILE"
    log "NEW BACKUP KEY written to $KEY_FILE"
    log "Copy it into a password manager NOW. A key that lives only on this"
    log "machine cannot restore this machine."
fi
[ -s "$KEY_FILE" ] || die "backup key file is empty: $KEY_FILE"

mkdir -p "$DEST"
chmod 700 "$DEST"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/kbots-backup.XXXXXX")"
chmod 700 "$WORK"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT INT TERM

# 1. Configuration and the vault, including its salt and KDF parameters. Without
#    the salt the vault cannot be opened even with the right passphrase.
mkdir -p "$WORK/config"
copied=0
for entry in "$OVERLAY"/config/*; do
    [ -f "$entry" ] || continue
    cp -p "$entry" "$WORK/config/"
    copied=$((copied + 1))
done
[ "$copied" -gt 0 ] || die "no configuration files found under $OVERLAY/config"

# 2. Every database, live-safe, keeping its path so a restore is unambiguous.
databases=0
while IFS= read -r db; do
    relative="${db#"$OVERLAY"/}"
    case "$relative" in tmp/*|*/node_modules/*) continue ;; esac
    mkdir -p "$WORK/data/$(dirname "$relative")"
    if sqlite3 "$db" ".backup '$WORK/data/$relative'" 2>/dev/null; then
        databases=$((databases + 1))
    else
        log "WARNING: sqlite3 could not back up $relative; it is NOT in this archive"
    fi
done < <(find "$OVERLAY" -name '*.db' -type f -not -path '*/tmp/*' -not -path '*/node_modules/*')
[ "$databases" -gt 0 ] || die "no databases could be backed up"

# 3. The small hand-written state that git does not track.
for extra in runtime.json session_consent.json feedback_map.json; do
    [ -f "$OVERLAY/$extra" ] && cp -p "$OVERLAY/$extra" "$WORK/"
done

printf 'created=%s\nhost=%s\noverlay=%s\ndatabases=%s\nconfig_files=%s\n' \
    "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$(hostname)" "$OVERLAY" "$databases" "$copied" \
    > "$WORK/MANIFEST"

ARCHIVE="$DEST/kbots-$(date '+%Y%m%d-%H%M%S').tar.gz.enc"
( umask 077
  tar czf - -C "$WORK" . \
    | openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass "file:$KEY_FILE" \
    > "$ARCHIVE" )
chmod 600 "$ARCHIVE"

size="$(du -h "$ARCHIVE" | cut -f1)"
log "wrote $ARCHIVE ($size): $databases databases, $copied config files"

# 4. Prune, newest kept. Deleting only ever touches this directory's archives.
if [ "$KEEP" -gt 0 ]; then
    # shellcheck disable=SC2012
    ls -1t "$DEST"/kbots-*.tar.gz.enc 2>/dev/null | tail -n "+$((KEEP + 1))" | while IFS= read -r old; do
        rm -f "$old"
        log "pruned $(basename "$old")"
    done
fi

if [ "$DEST" = "$HOME/kbots-backups" ]; then
    log "NOTE: archives are on the same disk as the originals. Set KBOTS_BACKUP_DIR"
    log "to an external volume or sync this directory off the machine."
fi
