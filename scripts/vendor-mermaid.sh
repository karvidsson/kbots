#!/usr/bin/env bash
# vendor-mermaid.sh — fetch a pinned mermaid.min.js into src/lib/vendor/ so
# render_diagram (src/tools/design.py) renders offline. Safe to re-run; skips
# when the pinned version is already present. Never fails the caller (sync.sh
# runs it best-effort on hosts that may be offline).
#
# Usage: scripts/vendor-mermaid.sh [--force]
set -uo pipefail

MERMAID_VERSION="${MERMAID_VERSION:-11.16.1}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST_DIR="$REPO_ROOT/src/lib/vendor"
DEST="$DEST_DIR/mermaid.min.js"
STAMP="$DEST_DIR/.mermaid-version"
URL="https://cdn.jsdelivr.net/npm/mermaid@${MERMAID_VERSION}/dist/mermaid.min.js"

mkdir -p "$DEST_DIR"
if [ "${1:-}" != "--force" ] && [ -s "$DEST" ] && [ "$(cat "$STAMP" 2>/dev/null)" = "$MERMAID_VERSION" ]; then
    echo "[vendor-mermaid] mermaid $MERMAID_VERSION already vendored"
    exit 0
fi

tmp="$(mktemp)"
if curl -fsSL --max-time 60 "$URL" -o "$tmp" && [ -s "$tmp" ] && head -c 200 "$tmp" | grep -q "mermaid\|function\|var " ; then
    mv "$tmp" "$DEST" && chmod 644 "$DEST"
    echo "$MERMAID_VERSION" > "$STAMP"
    echo "[vendor-mermaid] vendored mermaid $MERMAID_VERSION → $DEST ($(du -h "$DEST" | cut -f1))"
else
    rm -f "$tmp"
    echo "[vendor-mermaid] could not fetch $URL — render_diagram will use the CDN at render time" >&2
    exit 0
fi
