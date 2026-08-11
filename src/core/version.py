"""Platform version — the git commit the running process booted from.

The RUNNING version is captured once at boot (write_running_version) so it
reflects what's actually executing — NOT the on-disk checkout, which the
watchdog / self-deploy can `git reset` while the old process keeps running.
Comparing the two tells you whether a deployed update has actually taken effect.
"""

import json
import logging
import re
import subprocess
import time
from pathlib import Path

from src.core.base import PROJECT_ROOT

logger = logging.getLogger(__name__)

_VERSION_FILE = "version.json"
_DESCRIBE_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)-(\d+)-g[0-9a-f]+$")


def _git(*args: str) -> str | None:
    """Run a git command in the install checkout; None on any failure."""
    try:
        out = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), *args],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _pyproject_version() -> str:
    try:
        for line in (PROJECT_ROOT / "pyproject.toml").read_text().splitlines():
            s = line.strip()
            if s.startswith("version") and "=" in s:
                return s.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return "?"


def semver() -> str:
    """Semantic version derived from git tags: the nearest `vMAJOR.MINOR.PATCH`
    tag plus the number of commits since it, so the patch auto-increments per
    commit/deploy. e.g. tag v0.1.0 + 5 commits → 'v0.1.5'; exactly on v0.2.0 →
    'v0.2.0'. Bump minor/major just by tagging (git tag v0.2.0). Falls back to
    the short SHA when no version tag is reachable."""
    desc = _git("describe", "--tags", "--long", "--match", "v[0-9]*")
    if desc:
        m = _DESCRIBE_RE.match(desc)
        if m:
            major, minor, patch, dist = (int(x) for x in m.groups())
            return f"v{major}.{minor}.{patch + dist}"
        return desc  # unexpected shape — surface it rather than hide it
    short = _git("rev-parse", "--short=8", "HEAD") or ""
    return f"g{short}" if short else "unknown"


def current_commit() -> dict:
    """The ON-DISK checkout commit (git HEAD in PROJECT_ROOT)."""
    full = _git("rev-parse", "HEAD") or ""
    return {
        "commit": full,
        "short": full[:8] if full else "unknown",
        "version": semver(),
        "subject": _git("log", "-1", "--format=%s") or "",
        "date": _git("log", "-1", "--format=%cd", "--date=format:%Y-%m-%d %H:%M") or "",
    }


def is_update(prev: dict | None, running: dict | None) -> bool:
    """True iff the process booted on a DIFFERENT commit than the previous boot
    (i.e. a real update took effect). Silent on first boot (no prev)."""
    if not prev or not running:
        return False
    p, r = prev.get("commit"), running.get("commit")
    return bool(p and r and p != r)


def commits_between(old: str, new: str, limit: int = 15) -> str:
    """One-line log of commits in old..new (empty if unavailable / not linear)."""
    out = _git("log", f"{old}..{new}", "--oneline", "--no-decorate")
    if not out:
        return ""
    return "\n".join(out.splitlines()[:limit])


def _default_data_dir() -> Path:
    return PROJECT_ROOT / "data"


def write_running_version(data_dir: Path | None = None) -> dict:
    """Capture the current commit as the RUNNING version and persist it. Call
    once at boot so version.json is frozen to what this process is executing."""
    d = Path(data_dir) if data_dir else _default_data_dir()
    info = current_commit()
    info["pyproject_version"] = _pyproject_version()
    info["booted_at"] = int(time.time())
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / _VERSION_FILE).write_text(json.dumps(info, indent=2))
    except OSError as e:
        logger.warning(f"Could not write {_VERSION_FILE}: {e}")
    return info


def read_running_version(data_dir: Path | None = None) -> dict | None:
    """Read the RUNNING version recorded at boot, or None if not recorded yet."""
    d = Path(data_dir) if data_dir else _default_data_dir()
    path = d / _VERSION_FILE
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
