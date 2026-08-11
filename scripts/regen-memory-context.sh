#!/usr/bin/env bash
# Regenerate MEMORY_CONTEXT.md for kbots v2 agent workspace.
# Gives agents a snapshot of memory stats for session persistence.
# Run every 30 minutes via cron.
set -uo pipefail

KBOTS_HOME="${KBOTS_HOME:-$(cd "$(dirname "$0")/.." && pwd)}"
KBOTS_OVERLAY="${KBOTS_OVERLAY:-}"

# Resolve memory DB: overlay data dir takes priority over core
if [ -n "$KBOTS_OVERLAY" ] && [ -f "$KBOTS_OVERLAY/data/memory.db" ]; then
    DB_PATH="$KBOTS_OVERLAY/data/memory.db"
elif [ -f "${KBOTS_DATA_DIR:-$KBOTS_HOME/data}/memory.db" ]; then
    DB_PATH="${KBOTS_DATA_DIR:-$KBOTS_HOME/data}/memory.db"
else
    echo "Memory DB not found"
    exit 0
fi

# Collect ALL agent dirs with a CLAUDE.md (overlay + core)
AGENT_DIRS=()
if [ -n "$KBOTS_OVERLAY" ]; then
    for d in "$KBOTS_OVERLAY"/agents/*/CLAUDE.md; do
        [ -f "$d" ] && AGENT_DIRS+=("$(dirname "$d")")
    done
fi
for d in "$KBOTS_HOME"/agents/*/CLAUDE.md; do
    [ -f "$d" ] && AGENT_DIRS+=("$(dirname "$d")")
done

if [ ${#AGENT_DIRS[@]} -eq 0 ]; then
    echo "No agent dirs found"
    exit 0
fi

# Query stats directly from SQLite
total=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM memories WHERE scope NOT LIKE 'archived%';" 2>/dev/null || echo "0")
semantic=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM memories WHERE type='semantic' AND scope NOT LIKE 'archived%';" 2>/dev/null || echo "0")
episodic=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM memories WHERE type='episodic' AND scope NOT LIKE 'archived%';" 2>/dev/null || echo "0")
procedural=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM memories WHERE type='procedural' AND scope NOT LIKE 'archived%';" 2>/dev/null || echo "0")
pinned=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM memories WHERE pinned=1;" 2>/dev/null || echo "0")
embedded=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM memories WHERE embedding IS NOT NULL AND scope NOT LIKE 'archived%';" 2>/dev/null || echo "0")
archived=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM memories WHERE scope LIKE 'archived%';" 2>/dev/null || echo "0")
inserts_24h=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM memories WHERE created_at > datetime('now', '-1 day');" 2>/dev/null || echo "0")
at_risk=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM memories WHERE pinned=0 AND confidence < 0.15 AND scope NOT LIKE 'archived%';" 2>/dev/null || echo "0")
db_size=$(du -m "$DB_PATH" 2>/dev/null | awk '{print $1}' || echo "0")

now_utc=$(date -u '+%Y-%m-%d %H:%M:%S UTC')

CONTENT=$(cat << EOF
# Memory Context — Auto-Generated

**Generated:** $now_utc
**Updated every 30 minutes**
**Backend:** SQLite (inline, no Docker)

## Memory Stats

| Metric | Value |
|--------|-------|
| Active memories | $total |
| Semantic | $semantic |
| Episodic | $episodic |
| Procedural | $procedural |
| Pinned (immune) | $pinned |
| Embedded | $embedded |
| Archived | $archived |
| At risk (<0.15) | $at_risk |
| Inserts (24h) | $inserts_24h |
| DB size | ${db_size}MB |
EOF
)

for agent_dir in "${AGENT_DIRS[@]}"; do
    echo "$CONTENT" > "$agent_dir/MEMORY_CONTEXT.md"
done

echo "MEMORY_CONTEXT.md written to ${#AGENT_DIRS[@]} agent(s) at $now_utc"
