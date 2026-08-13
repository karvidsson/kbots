#!/usr/bin/env bash
# Migrate agent identity files: CLAUDE.md -> AGENTS.md + Claude Code stub.
#
# AGENTS.md is the canonical identity file (read natively by most agent CLIs);
# CLAUDE.md becomes a thin stub importing it, so Claude Code keeps working and
# per-CLI notes have a home. Idempotent — safe to re-run.
#
# Usage: scripts/migrate-agents-md.sh <dir> [<dir>...]
#   <dir> is an agents parent directory (each subdirectory an agent folder)
#   or a single agent folder.
set -euo pipefail

STUB='@AGENTS.md

<!-- Claude Code entry point. This agent'"'"'s identity and instructions live in
     AGENTS.md (imported above) — edit THAT file, not this one. Only Claude
     Code-specific notes belong below this line. -->
'

migrate_dir() {
    local dir="$1"
    local claude="$dir/CLAUDE.md" agents="$dir/AGENTS.md"
    [ -f "$claude" ] || return 0
    if head -1 "$claude" | grep -q '^@AGENTS.md'; then
        return 0  # already a stub
    fi
    if [ -f "$agents" ]; then
        echo "SKIP  $dir — non-stub CLAUDE.md AND AGENTS.md both exist; resolve manually"
        return 0
    fi
    mv "$claude" "$agents"
    printf '%s' "$STUB" > "$claude"
    echo "OK    $dir — CLAUDE.md -> AGENTS.md (+ stub)"
}

[ $# -ge 1 ] || { echo "usage: $0 <agents-dir> [<agents-dir>...]" >&2; exit 1; }

for target in "$@"; do
    migrate_dir "${target%/}"
    for sub in "$target"/*/; do
        [ -d "$sub" ] || continue
        migrate_dir "${sub%/}"
    done
done
