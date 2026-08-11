#!/usr/bin/env bash
# Batch compress agent context files to reduce input tokens.
# Compresses CLAUDE.md, codex docs, and skill prompts.
# Idempotent — skips files unchanged since last compression.
#
# Usage: scripts/compress-context.sh [--dry-run] [--level lite|standard]
#
# Reads compression config from config.yaml. Respects per-agent overrides.
# Originals preserved as .original.md alongside compressed versions.

set -uo pipefail

KBOTS_DIR="${KBOTS_HOME:-$(cd "$(dirname "$0")/.." && pwd)}"
OVERLAY="${KBOTS_OVERLAY:-}"
LEVEL="standard"
DRY_RUN=""

# Parse args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN="--dry-run"; shift ;;
        --level) LEVEL="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

# Check if compression is enabled in config
CONFIG="${OVERLAY:+$OVERLAY/config/config.yaml}"
CONFIG="${CONFIG:-$KBOTS_DIR/config/config.yaml}"

if [ -f "$CONFIG" ]; then
    # Simple YAML check — look for compression.enabled: false
    if grep -qE '^\s*enabled:\s*false' <(grep -A2 '^compression:' "$CONFIG" 2>/dev/null); then
        echo "[compress] Compression disabled in config. Exiting."
        exit 0
    fi
    # Read level from config if not overridden by CLI arg
    config_level=$(grep -A2 '^compression:' "$CONFIG" 2>/dev/null | grep -oP 'level:\s*\K\w+' || true)
    if [ -n "$config_level" ] && [ "$LEVEL" = "standard" ]; then
        LEVEL="$config_level"
    fi
fi

COMPRESSOR="$KBOTS_DIR/src/lib/compressor.py"

if [ ! -f "$COMPRESSOR" ]; then
    echo "[compress] ERROR: Compressor not found at $COMPRESSOR"
    exit 1
fi

compress_file() {
    local file="$1"
    local level="$2"

    if [ ! -f "$file" ]; then
        return
    fi

    # Skip if file has compression tag and original hasn't changed
    if grep -q "<!-- compressed:" "$file" 2>/dev/null; then
        local original="${file%.*}.original.${file##*.}"
        if [ -f "$original" ]; then
            # Check if original is newer than compressed
            if [ ! "$original" -nt "$file" ]; then
                echo "[compress] Skip: $(basename "$file") (unchanged)"
                return
            fi
        fi
    fi

    local result
    local dry_run_flag="${DRY_RUN:+--dry-run}"
    result=$(cd "$KBOTS_DIR" && python3 -m src.lib.compressor "$file" --level "$level" $dry_run_flag 2>&1)

    if [ $? -eq 0 ]; then
        echo "[compress] ${DRY_RUN:+[DRY RUN] }$(basename "$file"): $result"
    else
        echo "[compress] ERROR: $(basename "$file"): $result"
    fi
}

total=0

# NOTE: CLAUDE.md files are excluded by default — they are carefully tuned
# agent identity prompts where every word matters.  Use the MCP tool
# (compress_context) to compress individual files manually if needed.

# --- Codex docs ---
if [ -n "$OVERLAY" ] && [ -d "$OVERLAY/codex" ]; then
    while IFS= read -r -d '' f; do
        compress_file "$f" "$LEVEL"
        total=$((total + 1))
    done < <(find "$OVERLAY/codex" -name "*.md" -not -name "*.original.md" -print0)
fi

if [ -d "$KBOTS_DIR/codex" ]; then
    while IFS= read -r -d '' f; do
        compress_file "$f" "$LEVEL"
        total=$((total + 1))
    done < <(find "$KBOTS_DIR/codex" -name "*.md" -not -name "*.original.md" -print0)
fi

echo "[compress] Done. Processed $total files."
