"""Context compressor for kbots agent files.

Rule-based compression that strips unambiguous prose filler while preserving
code blocks, config, paths, URLs, headings, tables, and technical terms.
No LLM calls.  Conservative by design — only removes phrases that never
carry meaning in LLM instruction files.

Target: codex docs and skill prompts.  CLAUDE.md files (agent identity
prompts) should generally be excluded as they are carefully tuned.
"""

import re
from pathlib import Path

# Blocks to preserve verbatim (never compress)
_CODE_FENCE = re.compile(r"(```[\s\S]*?```)", re.MULTILINE)
_INLINE_CODE = re.compile(r"(`[^`\n]+`)")
_YAML_FRONTMATTER = re.compile(r"^(---\n[\s\S]*?\n---)", re.MULTILINE)
_HTML_COMMENT = re.compile(r"(<!--[\s\S]*?-->)")
_TABLE_ROW = re.compile(r"^\|.+\|$", re.MULTILINE)
_HEADING = re.compile(r"^(#{1,6}\s+.*)$", re.MULTILINE)
_URL = re.compile(r"(https?://\S+)")
_FILE_PATH = re.compile(r"(/[\w./-]+(?:\.\w+)?)")

# Filler phrases to strip — only unambiguous padding that never carries
# meaning in LLM instruction files.  Phrases like "for example", "note that",
# and "keep in mind" are intentionally excluded because they signal emphasis
# or illustrative context that models use for interpretation.
_FILLER_PHRASES = [
    r"\bplease\s+note\s+that\s*",
    r"\bit\s+is\s+important\s+to\s*",
    r"\bit'?s\s+worth\s+noting\s+that\s*",
    r"\bit\s+should\s+be\s+noted\s+that\s*",
    r"\byou\s+should\s+always\s*",
    r"\bmake\s+sure\s+to\s*",
    r"\bbe\s+sure\s+to\s*",
    r"\bin\s+order\s+to\s*",
    r"\bthe\s+reason\s+for\s+this\s+is\s*",
    r"\bas\s+mentioned\s+(?:above|earlier|before),?\s*",
    r"\byou\s+will\s+want\s+to\s*",
    r"\byou\s+will\s+need\s+to\s*",
    r"\bwhat\s+this\s+does\s+is\s*",
    r"\bthis\s+is\s+(?:essentially|basically)\s*",
]

# Collapse contractions
_CONTRACTIONS = [
    (re.compile(r"\bdo\s+not\b", re.IGNORECASE), "don't"),
    (re.compile(r"\bcannot\b", re.IGNORECASE), "can't"),
    (re.compile(r"\bwill\s+not\b", re.IGNORECASE), "won't"),
    (re.compile(r"\bshould\s+not\b", re.IGNORECASE), "shouldn't"),
    (re.compile(r"\bdoes\s+not\b", re.IGNORECASE), "doesn't"),
    (re.compile(r"\bis\s+not\b", re.IGNORECASE), "isn't"),
    (re.compile(r"\bare\s+not\b", re.IGNORECASE), "aren't"),
    (re.compile(r"\bwould\s+not\b", re.IGNORECASE), "wouldn't"),
    (re.compile(r"\bcould\s+not\b", re.IGNORECASE), "couldn't"),
]

# Multi-space collapse
_MULTI_SPACE = re.compile(r"  +")

# Compiled filler patterns
_FILLER_COMPILED = [re.compile(p, re.IGNORECASE) for p in _FILLER_PHRASES]

# Compression tag
COMPRESS_TAG_RE = re.compile(
    r"<!--\s*compressed:.*?-->\n?"
)


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English."""
    return len(text) // 4


def _is_protected_line(line: str) -> bool:
    """Check if a line should be preserved verbatim."""
    stripped = line.strip()
    if not stripped:
        return True  # blank lines preserved
    if stripped.startswith("#"):
        return True  # headings
    if stripped.startswith("|") and stripped.endswith("|"):
        return True  # table rows
    if stripped.startswith("```"):
        return True  # code fence boundary
    if stripped.startswith("---"):
        return True  # frontmatter / hr
    if stripped.startswith(">"):
        return False  # blockquotes can be compressed
    if stripped.startswith("- ") or stripped.startswith("* ") or re.match(r"^\d+\.\s", stripped):
        return False  # list items — compress the text part
    return False


def compress_text(text: str, level: str = "standard") -> str:
    """Compress prose in a string while preserving code/config/structure.

    Args:
        text: Input text (markdown expected).
        level: "lite" (articles + filler), "standard" (full compression).

    Returns:
        Compressed text.
    """
    # Extract and placeholder protected blocks
    placeholders = {}
    counter = 0

    def _placeholder(match):
        nonlocal counter
        key = f"\x00PROTECT_{counter}\x00"
        placeholders[key] = match.group(0)
        counter += 1
        return key

    # Order matters: frontmatter first, then code fences, then inline
    result = _YAML_FRONTMATTER.sub(_placeholder, text)
    result = _CODE_FENCE.sub(_placeholder, result)
    result = _HTML_COMMENT.sub(_placeholder, result)
    result = _INLINE_CODE.sub(_placeholder, result)
    result = _URL.sub(_placeholder, result)

    # Process line by line
    lines = result.split("\n")
    compressed = []
    in_table = False

    for line in lines:
        stripped = line.strip()

        # Table detection
        if stripped.startswith("|") and stripped.endswith("|"):
            in_table = True
            compressed.append(line)
            continue
        elif in_table and not stripped.startswith("|"):
            in_table = False

        # Protected lines pass through
        if _is_protected_line(line):
            compressed.append(line)
            continue

        # Compress this line
        compressed_line = _compress_line(line, level)
        compressed.append(compressed_line)

    result = "\n".join(compressed)

    # Restore protected blocks
    for key, original in placeholders.items():
        result = result.replace(key, original)

    # Clean up multi-spaces
    result = _MULTI_SPACE.sub(" ", result)

    # Clean up lines that became just whitespace
    result = re.sub(r"\n[ \t]+\n", "\n\n", result)

    # Collapse 3+ consecutive blank lines to 2
    result = re.sub(r"\n{3,}", "\n\n", result)

    return result


def _compress_line(line: str, level: str) -> str:
    """Apply compression rules to a single prose line."""
    # Strip filler phrases (both levels)
    for pattern in _FILLER_COMPILED:
        line = pattern.sub("", line)

    if level == "standard":
        # Apply contractions
        for pattern, replacement in _CONTRACTIONS:
            line = pattern.sub(replacement, line)

    # Clean up artifacts from removals
    line = re.sub(r"\s*,\s*,", ",", line)       # double commas
    line = re.sub(r"\.\s*,", ".", line)          # period then comma
    line = re.sub(r",\s*\.", ".", line)          # comma then period
    line = re.sub(r"\.\s*\.", ".", line)          # double periods
    line = re.sub(r"\s+([.,;:!?])", r"\1", line) # space before punctuation
    line = re.sub(r"([.!?])\s+([a-z])",          # capitalize after sentence end
                  lambda m: m.group(1) + " " + m.group(2).upper(), line)
    line = _MULTI_SPACE.sub(" ", line)            # collapse multi-spaces

    # Capitalize first non-space character if line starts with lowercase
    line = re.sub(r"^(\s*[-*]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), line)
    line = re.sub(r"^(\s*)([a-z])", lambda m: m.group(1) + m.group(2).upper(), line)

    # Strip leading/trailing space but preserve list indent
    indent_match = re.match(r"^(\s*[-*]\s+|\s*\d+\.\s+)", line)
    if indent_match:
        # Preserve list marker indent, strip rest
        line = indent_match.group(0) + line[indent_match.end():].strip()
    else:
        line = line.strip()

    return line


def compress_file(
    file_path: str | Path,
    level: str = "standard",
    dry_run: bool = False,
) -> dict:
    """Compress a file in-place, preserving the original.

    Args:
        file_path: Path to the file to compress.
        level: "lite" or "standard".
        dry_run: If True, return stats without writing.

    Returns:
        Dict with: original_tokens, compressed_tokens, ratio, original_path, skipped.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    text = path.read_text(encoding="utf-8")

    # Strip existing compression tag if present
    clean_text = COMPRESS_TAG_RE.sub("", text).strip()

    # Check if there's an original file and whether it's changed
    original_path = path.with_suffix(".original" + path.suffix)
    if original_path.exists():
        original_text = original_path.read_text(encoding="utf-8")
        if original_text.strip() == clean_text:
            # Source hasn't changed since last compression — re-compress from original
            clean_text = original_text
        else:
            # The compressed file was manually edited — treat current as new source
            pass

    original_tokens = _estimate_tokens(clean_text)
    compressed = compress_text(clean_text, level=level)
    compressed_tokens = _estimate_tokens(compressed)

    if original_tokens == 0:
        ratio = 1.0
    else:
        ratio = compressed_tokens / original_tokens

    result = {
        "original_tokens": original_tokens,
        "compressed_tokens": compressed_tokens,
        "ratio": ratio,
        "saved_pct": round((1 - ratio) * 100, 1),
        "original_path": str(original_path),
        "skipped": False,
    }

    if dry_run:
        return result

    # Save original (only if we don't already have one with this content)
    if not original_path.exists():
        original_path.write_text(clean_text, encoding="utf-8")
    elif original_path.read_text(encoding="utf-8").strip() != clean_text.strip():
        # Source changed — update the original backup
        original_path.write_text(clean_text, encoding="utf-8")

    # Write compressed version with tag
    from datetime import datetime, timezone
    tag = (
        f"<!-- compressed: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}"
        f" | original: {original_path.name}"
        f" | ratio: {ratio:.2f}"
        f" | level: {level} -->\n"
    )
    path.write_text(tag + compressed + "\n", encoding="utf-8")

    return result


def decompress_file(file_path: str | Path) -> bool:
    """Restore a file from its .original backup.

    Returns True if restored, False if no original found.
    """
    path = Path(file_path)
    original_path = path.with_suffix(".original" + path.suffix)

    if not original_path.exists():
        return False

    original_text = original_path.read_text(encoding="utf-8")
    path.write_text(original_text, encoding="utf-8")
    original_path.unlink()
    return True


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Compress a context file")
    parser.add_argument("file", help="Path to the file to compress")
    parser.add_argument("--level", default="standard", choices=["lite", "standard"])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        r = compress_file(args.file, level=args.level, dry_run=args.dry_run)
        print(f"{r['original_tokens']} -> {r['compressed_tokens']} tokens ({r['saved_pct']}% saved)")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
