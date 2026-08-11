#!/usr/bin/env bash
# Memory decay — daily lifecycle management.
# Decays confidence, archives faded memories, purges old archives.
# Run daily at 03:00 UTC via kbots-memory-decay.timer.
#
# Lifecycle:
#   Day 0:  0.70 (new memory)
#   Day 10: 0.59
#   Day 30: 0.38
#   Day 60: 0.05 → archived
#   +90 days archived → permanently deleted
#   Accessed → decay pauses
#   Pinned → immune forever
set -uo pipefail

KBOTS_HOME="${KBOTS_HOME:-$(cd "$(dirname "$0")/.." && pwd)}"
DB_PATH="${KBOTS_DATA_DIR:-$KBOTS_HOME/data}/memory.db"

if [ ! -f "$DB_PATH" ]; then
    echo "Memory DB not found at $DB_PATH"
    exit 0
fi

# Decay: non-pinned memories not accessed in 24h lose 0.0108 confidence/day (~60 days to archive)
decayed=$(sqlite3 "$DB_PATH" "
    UPDATE memories
    SET confidence = MAX(confidence - 0.0108, 0.0),
        updated_at = datetime('now')
    WHERE pinned = 0
      AND scope NOT LIKE 'archived%'
      AND (last_accessed IS NULL OR last_accessed < datetime('now', '-1 day'));
    SELECT changes();
" 2>/dev/null || echo "0")

# Archive: memories below 0.05 confidence
archived=$(sqlite3 "$DB_PATH" "
    UPDATE memories
    SET scope = 'archived:' || scope,
        updated_at = datetime('now')
    WHERE pinned = 0
      AND scope NOT LIKE 'archived%'
      AND confidence < 0.05;
    SELECT changes();
" 2>/dev/null || echo "0")

# Purge: archived memories older than 90 days — permanently deleted
purged=$(sqlite3 "$DB_PATH" "
    DELETE FROM memories
    WHERE scope LIKE 'archived%'
      AND updated_at < datetime('now', '-90 days');
    SELECT changes();
" 2>/dev/null || echo "0")

now_utc=$(date -u '+%Y-%m-%d %H:%M:%S UTC')
echo "Memory decay completed at $now_utc: decayed=$decayed archived=$archived purged=$purged"
