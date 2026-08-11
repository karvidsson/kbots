"""CLI-dependency resolution with zero-install pkgx fallback.

pkgx-inspired (https://pkgx.sh): instead of failing when an external binary
(ffmpeg, tmux, …) isn't installed, tools resolve it at call time —

  1. on PATH → run it directly (normal case, zero overhead)
  2. pkgx installed → run via `pkgx -q <tool>` (fetched + cached on first use,
     no sudo, nothing installed system-wide; later calls are near-instant)
  3. neither → a clean, actionable install hint instead of raw exit-127 noise

pkgx is strictly OPTIONAL — a 4MiB standalone binary the setup wizard offers.
Everything works without it; with it, CLI deps become run-on-first-use.
"""

import logging
import shutil
from functools import lru_cache

logger = logging.getLogger(__name__)


@lru_cache(maxsize=64)
def _which(binary: str) -> str | None:
    return shutil.which(binary)


def resolve_cli(binary: str) -> list[str] | None:
    """argv prefix for an external CLI, or None when unavailable.

    [binary] when on PATH; ["pkgx", "-q", binary] when pkgx can provide it
    zero-install; None when neither — pair with cli_hint(binary).
    """
    if _which(binary):
        return [binary]
    if _which("pkgx"):
        logger.info(f"clidep: '{binary}' not on PATH — running via pkgx (zero-install)")
        return ["pkgx", "-q", binary]
    return None


def cli_hint(binary: str) -> str:
    """Actionable message for a missing CLI dependency."""
    return (f"{binary} is not installed. Install it (macOS: brew install {binary}, "
            f"Linux: apt install {binary}) — or install pkgx once "
            f"(https://pkgx.sh, 4MiB: `curl -fsSL https://pkgx.sh | sh`) and "
            f"kbots runs tools like {binary} on demand with no install step.")


def reset_cache() -> None:
    """Clear the which-cache (tests; or after installing a binary mid-process)."""
    _which.cache_clear()
