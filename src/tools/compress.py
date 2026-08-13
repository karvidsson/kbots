"""Context compression tool for kbots.

Compresses prose in context files (identity files, codex, skills) to reduce
input tokens per agent message. Preserves code, config, structure.
"""

from pathlib import Path

from src.core.base import ToolContext
from src.core.tools import tool
from src.lib.compressor import compress_file, decompress_file


@tool(
    name="compress_context",
    description=(
        "Compress a context file to reduce tokens. "
        "Preserves code, config, paths, headings, tables — compresses only prose. "
        "Original saved as .original.md alongside."
    ),
)
async def compress_context(
    ctx: ToolContext,
    file_path: str,
    level: str = "standard",
) -> str:
    """Compress a context file in-place.

    Args:
        file_path: Absolute path to the file to compress.
        level: "lite" (articles + filler only, ~20-25% reduction),
               "standard" (full compression, ~35-45% reduction),
               "report" (dry run — show savings without writing).
    """
    if level not in ("lite", "standard", "report"):
        return "Error: level must be 'lite', 'standard', or 'report'"

    path = Path(file_path)
    if not path.exists():
        return f"Error: file not found: {file_path}"

    if path.suffix not in (".md", ".yaml", ".yml", ".txt"):
        return f"Error: unsupported file type: {path.suffix} (expected .md, .yaml, .yml, .txt)"

    dry_run = level == "report"
    actual_level = "standard" if dry_run else level

    try:
        result = compress_file(path, level=actual_level, dry_run=dry_run)
    except Exception as e:
        return f"Error: {e}"

    if result["skipped"]:
        return f"Skipped: {path.name} — unchanged since last compression"

    action = "Would save" if dry_run else "Saved"
    return (
        f"{'[DRY RUN] ' if dry_run else ''}"
        f"Compressed {path.name}: "
        f"{result['original_tokens']} → {result['compressed_tokens']} tokens "
        f"({result['saved_pct']}% reduction). "
        f"{action}: {result['original_path']}"
    )


@tool(
    name="decompress_context",
    description="Restore a compressed file from its .original backup.",
)
async def decompress_context(ctx: ToolContext, file_path: str) -> str:
    """Restore a file from its .original backup."""
    path = Path(file_path)
    if not path.exists():
        return f"Error: file not found: {file_path}"

    if decompress_file(path):
        return f"Restored {path.name} from original backup."
    else:
        return f"No original backup found for {path.name}"
