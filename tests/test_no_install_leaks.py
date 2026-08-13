"""Core must not carry any single installation's identity.

Core is published; overlays are private. Every deployment's roster, account
IDs, addresses and paths belong in the overlay, and CONTRIBUTING.md says so:
"No secrets, names, or numeric IDs in code, tests, or docs — use synthetic
fixtures". This file is that rule made executable, because the rule on its own
did not hold: a live Discord account ID reached a test fixture, and agent names
reached code comments, and nothing objected until a human noticed.

Two layers, deliberately different in character:

  * Shape checks run everywhere, including a fresh public clone, and take no
    configuration. They match things that are identifying by construction — a
    17-19 digit snowflake, a real email address, a home directory, a tailnet
    hostname. These carry no judgement and no false positives worth the name,
    so they are absolute.
  * The roster check only runs where an overlay exists, and compares Core
    against that deployment's actual agent names. It cannot live in Core as a
    word list — writing the forbidden names here would be the very leak it
    guards against.

Enforcement is the test suite rather than a git hook: hooks are per-clone and
`--no-verify` is in this repo's own push convention, whereas scripts/self-deploy.sh
runs the suite and refuses to ship a red one.
"""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Lockfiles carry upstream hashes that trip the digit rule and say nothing about
# this install. Everything else, including docs, is in scope.
_SKIP_FILES = {"uv.lock", "poetry.lock", "package-lock.json"}
_SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".pdf"}

# Synthetic snowflakes CONTRIBUTING.md prescribes: 1000000000000000001-style.
_FAKE_SNOWFLAKE_PREFIX = "1000000000000000"

_SNOWFLAKE = re.compile(r"\b\d{17,19}\b")
# Lowercase TLD only. Real addresses are written lowercase, and requiring it
# keeps '\n@Data.Bot' in a mention fixture from reading as one.
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}\b")
_HOME_PATH = re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+")
_TAILNET = re.compile(r"\b[A-Za-z0-9-]+\.ts\.net\b")

# Addresses that identify nobody. github.com's no-reply form appears in commit
# trailers and is deliberately public.
_EMAIL_OK = re.compile(r"@(?:example\.(?:com|org|net)|.*\bnoreply\b|users\.noreply\.github\.com)",
                       re.IGNORECASE)

# Home directories that name nobody: documentation placeholders, and the
# service accounts a Linux install of this project creates for itself.
_HOME_OK = {"you", "youruser", "your-user", "username", "user", "me", "runner",
            "kbots", "kagents", "docs"}


def _tracked_text_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout
    files = []
    for rel in out.split("\0"):
        if not rel or rel in _SKIP_FILES or Path(rel).suffix.lower() in _SKIP_SUFFIXES:
            continue
        p = REPO / rel
        try:
            p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError, IsADirectoryError):
            continue          # binary or unreadable — nothing to leak in text form
        files.append(p)
    return files


def _hits(pattern: re.Pattern, keep) -> list[str]:
    """Every match of `pattern` in tracked files that `keep(match)` accepts."""
    found = []
    for path in _tracked_text_files():
        rel = path.relative_to(REPO)
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in pattern.finditer(line):
                if keep(m.group(0)):
                    found.append(f"{rel}:{i}: {m.group(0)}")
    return found


def test_no_real_discord_ids():
    """A live account ID is the worst of these: it identifies a real account and
    survives in history long after the working tree is tidied."""
    bad = _hits(_SNOWFLAKE, lambda s: not s.startswith(_FAKE_SNOWFLAKE_PREFIX))
    assert not bad, (
        "Real-looking Discord/snowflake IDs in Core:\n  " + "\n  ".join(bad)
        + f"\nUse {_FAKE_SNOWFLAKE_PREFIX}001-style synthetic IDs instead.")


def test_no_real_email_addresses():
    bad = _hits(_EMAIL, lambda s: not _EMAIL_OK.search(s))
    assert not bad, (
        "Real email addresses in Core:\n  " + "\n  ".join(bad)
        + "\nUse someone@example.com.")


def test_no_home_directory_paths():
    """An absolute home path names its owner and pins Core to one machine."""
    bad = _hits(_HOME_PATH, lambda s: s.rsplit("/", 1)[-1].lower() not in _HOME_OK)
    assert not bad, (
        "Home-directory paths in Core:\n  " + "\n  ".join(bad)
        + "\nUse a relative path, an env var, or /Users/you.")


def test_no_private_network_hostnames():
    bad = _hits(_TAILNET, lambda s: True)
    assert not bad, "Tailnet hostnames in Core:\n  " + "\n  ".join(bad)


# --- roster check: only where a deployment exists to compare against ---------

def _overlay_roster_names() -> set[str]:
    """Distinctive agent/human names from the local overlay roster, if any.

    Plain single words are skipped, whatever their case: a roster holding
    'Ops' or 'Notes' would match ordinary prose in the README and half
    the test suite, and a check that cries wolf is one people stop reading —
    taking the true positives with it. What survives the filter — 'Some.Bot',
    'some-bot', 'Some Bot' — is unmistakably one install's name.

    The gap this leaves is real and worth stating: a single-word agent name
    ('atlas') will not be caught here. The shape checks above are the absolute
    guarantee; this one is a second net for the names most likely to be copied
    into a comment while explaining a bug, which is exactly how it happened.
    """
    overlay = os.environ.get("KBOTS_OVERLAY", "")
    if not overlay:
        return set()
    roster = Path(overlay) / "config" / "team.json"
    if not roster.exists():
        return set()
    try:
        data = json.loads(roster.read_text())
    except (json.JSONDecodeError, OSError):
        return set()

    names = set()
    for member in list(data.get("agents", [])) + list(data.get("humans", [])):
        for field in ("id", "name"):
            v = str(member.get(field) or "").strip()
            if len(v) >= 4 and not re.fullmatch(r"[A-Za-z]+", v):
                names.add(v)
    return names


def test_core_does_not_name_this_installations_agents():
    """Compare Core against the local overlay roster, never a list written here.

    Skipped without an overlay — a public clone has no roster to leak, and this
    test must not be the place someone's agent names finally get written down.
    """
    roster = _overlay_roster_names()
    if not roster:
        pytest.skip("no overlay roster present — nothing local to compare against")

    # The licence names its copyright holder; that is what a licence is for.
    files = [p for p in _tracked_text_files() if p.name != "LICENSE"]
    bad = []
    for name in sorted(roster):
        pattern = re.compile(rf"(?<![\w.-]){re.escape(name)}(?![\w.-])", re.IGNORECASE)
        for path in files:
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    bad.append(f"{path.relative_to(REPO)}:{i}: {name}")
    assert not bad, (
        "Core names agents from this installation:\n  " + "\n  ".join(bad)
        + "\nInvent a fixture name (Data Bot, Atlas) — Core is published, the "
          "overlay is not.")
