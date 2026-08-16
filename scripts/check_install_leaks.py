#!/usr/bin/env python3
"""Find text that identifies one installation of kbots.

Core is published; overlays are private. Every deployment's roster, account
IDs, addresses and paths belong in the overlay, and CONTRIBUTING.md says so:
"No secrets, names, or numeric IDs in code, tests, or docs — use synthetic
fixtures".

This module is that rule made executable. It exists as a script rather than
only as a test because the rule has now failed on three different surfaces,
and each one needs a different caller:

  * the working tree      — checked by tests/test_no_install_leaks.py
  * commit messages       — checked by the same tests, and by .githooks/commit-msg
                            before the commit is even written
  * PR titles and bodies  — checked by .github/workflows/ci.yml, which is the
                            only place that can see them

The tree was the surface everyone thought of, and it is the one that leaked
least. Real account IDs and agent names reached commit messages and pull
request descriptions instead — text that no test read, and that survives a
tidy-up of the working tree because rewriting it means rewriting history.

Two layers, deliberately different in character:

  * Shape checks run everywhere, including a fresh public clone, and take no
    configuration. They match things that are identifying by construction — a
    17-19 digit snowflake, a real email address, a home directory, a tailnet
    hostname. These carry no judgement, so they are absolute.
  * The roster check only runs where an overlay exists, and compares text
    against that deployment's actual agent names. It cannot live in Core as a
    word list — writing the forbidden names here would be the very leak it
    guards against.

Usage:
    check_install_leaks.py --tree                 # tracked files
    check_install_leaks.py --commits main..HEAD   # commit messages in a range
    check_install_leaks.py --text FILE            # any text (a PR body)

Exits 1 and prints one finding per line if anything is found.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Lockfiles carry upstream hashes that trip the digit rule and say nothing about
# this install. Everything else, including docs, is in scope.
SKIP_FILES = {"uv.lock", "poetry.lock", "package-lock.json"}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".pdf"}

# Synthetic snowflakes CONTRIBUTING.md prescribes: 1000000000000000001-style.
FAKE_SNOWFLAKE_PREFIX = "1000000000000000"

SNOWFLAKE = re.compile(r"\b\d{17,19}\b")
# Lowercase TLD only. Real addresses are written lowercase, and requiring it
# keeps '\n@Data.Bot' in a mention fixture from reading as one.
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}\b")
HOME_PATH = re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+")
TAILNET = re.compile(r"\b[A-Za-z0-9-]+\.ts\.net\b")

# Addresses that identify nobody. 'noreply' is matched on either side of the @:
# commit trailers carry both noreply@github.com and the
# 1234+user@users.noreply.github.com form, and both are deliberately public.
EMAIL_OK = re.compile(r"\bno-?reply\b|@example\.(?:com|org|net)", re.IGNORECASE)

# Home directories that name nobody: documentation placeholders, and the
# service accounts a Linux install of this project creates for itself.
HOME_OK = {"you", "youruser", "your-user", "username", "user", "me", "runner",
           "kbots", "kagents", "docs"}


def _snowflake_is_real(s: str) -> bool:
    return not s.startswith(FAKE_SNOWFLAKE_PREFIX)


def _email_is_real(s: str) -> bool:
    return not EMAIL_OK.search(s)


def _home_names_someone(s: str) -> bool:
    # Trailing punctuation belongs to the sentence, not the path: '/Users/you.'
    # at the end of a line of prose is the placeholder, not a person. Stripping
    # it is safe — a real leak keeps its name once the full stop is gone.
    return s.rsplit("/", 1)[-1].lower().rstrip(".,;:!?") not in HOME_OK


# (label, pattern, is-a-leak predicate, what to do instead)
SHAPE_CHECKS = (
    ("Discord/snowflake ID", SNOWFLAKE, _snowflake_is_real,
     f"use {FAKE_SNOWFLAKE_PREFIX}001-style synthetic IDs"),
    ("email address", EMAIL, _email_is_real,
     "use someone@example.com"),
    ("home-directory path", HOME_PATH, _home_names_someone,
     "use a relative path, an env var, or /Users/you"),
    ("private-network hostname", TAILNET, lambda s: True,
     "drop the hostname — it names one machine"),
)


def shape_hits(text: str, source: str) -> list[str]:
    """Findings from the configuration-free checks. Works on any text."""
    found = []
    for i, line in enumerate(text.splitlines(), 1):
        for label, pattern, is_leak, advice in SHAPE_CHECKS:
            for m in pattern.finditer(line):
                if is_leak(m.group(0)):
                    found.append(f"{source}:{i}: {label} {m.group(0)!r} — {advice}")
    return found


# --- surfaces ---------------------------------------------------------------

def tracked_text_files(repo: Path = REPO) -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=repo,
                         capture_output=True, text=True, check=True).stdout
    files = []
    for rel in out.split("\0"):
        if not rel or rel in SKIP_FILES or Path(rel).suffix.lower() in SKIP_SUFFIXES:
            continue
        path = repo / rel
        try:
            path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError, IsADirectoryError):
            continue          # binary or unreadable — nothing to leak in text form
        files.append(path)
    return files


def commit_records(rev_range: str, repo: Path = REPO) -> list[tuple[str, str, str]]:
    """(short sha, message body, identity lines) for each commit in `rev_range`.

    Body and identity are kept apart because they need different checks. The
    shape checks belong on both: an author or committer address is written by
    whichever machine happened to run `git commit`, so it is the one field a
    careless local git config leaks a hostname through. The roster check belongs
    on the body alone — the author line carries the repository owner's name on
    every commit ever made, and a roster that lists them as a human would turn
    git's own attribution into a permanent failure.
    """
    sep, end = "\x02", "\x01"
    fmt = f"%H{sep}%an <%ae>{sep}%cn <%ce>{sep}%B{end}"
    proc = subprocess.run(["git", "log", f"--format={fmt}", rev_range],
                          cwd=repo, capture_output=True, text=True)
    if proc.returncode != 0:
        return []
    records = []
    for raw in proc.stdout.split(end):
        raw = raw.strip("\n")
        if not raw:
            continue
        sha, author, committer, body = raw.split(sep, 3)
        records.append((sha[:8], body.strip(), "\n".join([author, committer])))
    return records


# --- roster check: only where a deployment exists to compare against ---------

def overlay_roster_names(*, single_words: bool = False) -> set[str]:
    """Agent/human names from the local overlay roster, if there is one.

    With `single_words=False` (the default, used for the working tree) plain
    single words are skipped whatever their case: a roster holding 'Ops' or
    'Notes' would match ordinary prose in the README and half the test suite,
    and a check that cries wolf is one people stop reading — taking the true
    positives with it. What survives the filter — 'Some.Bot', 'some-bot',
    'Some Bot' — is unmistakably one install's name.

    `single_words=True` is for commit messages on a branch, where the text is
    a handful of paragraphs a person or agent is about to write about its own
    work. A single-word agent name is precisely what leaked there, and the cost
    of a false positive is rewording one sentence rather than auditing a repo.
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
    for kind, members in (("agents", data.get("agents", [])),
                          ("humans", data.get("humans", []))):
        for member in members:
            for field in ("id", "name"):
                value = str(member.get(field) or "").strip()
                if len(value) < 4:
                    continue
                if not single_words and re.fullmatch(r"[A-Za-z]+", value):
                    continue
                names.add(value)
                # A human's name leaks one word at a time — "Ada saw the
                # status" names Ada Lovelace without ever matching the full
                # roster value, which is exactly how forty commit messages
                # slipped past this check. Split HUMAN names into their words
                # in single-word mode. Agent names stay whole: a bot called
                # "Data Bot" must not turn every mention of 'data' into a
                # finding.
                if single_words and kind == "humans" and " " in value:
                    for word in value.split():
                        if len(word) >= 4:
                            names.add(word)
    return names


def roster_hits(text: str, source: str, names: set[str]) -> list[str]:
    """Findings for roster names appearing as names rather than as substrings.

    The boundaries are deliberate, and each one was chosen against a real miss:

      * `-` and `_` are allowed on either side, so a name embedded in an
        identifier still counts. `discord-token-data-bot` is how a roster name
        reaches a commit message in practice — as part of a vault key, not as a
        word — and a boundary that requires whitespace never sees it.
      * A following `.` counts only when it joins the name to more of a name:
        `Data.Bot` must not match a roster entry of 'Data', but 'Data Bot' at
        the end of a sentence must still match despite the full stop. Requiring
        a hard word boundary there is why a planted name went unreported.
      * A following letter or digit never counts, so 'Ledger' does not match
        'ledgers'.
    """
    found = []
    for name in sorted(names):
        pattern = re.compile(
            rf"(?<![A-Za-z0-9.]){re.escape(name)}(?!\.\w)(?![A-Za-z0-9])",
            re.IGNORECASE)
        for i, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                found.append(f"{source}:{i}: names this installation's '{name}'")
    return found


# --- CLI --------------------------------------------------------------------

def _report(findings: list[str]) -> int:
    if not findings:
        print("no installation-identifying text found")
        return 0
    print("Installation-identifying text found:", file=sys.stderr)
    for line in findings:
        print(f"  {line}", file=sys.stderr)
    print("\nCore is published; the overlay is not. Use synthetic fixtures "
          "(Data Bot, Atlas, someone@example.com).", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tree", action="store_true", help="scan tracked files")
    parser.add_argument("--commits", metavar="RANGE",
                        help="scan commit messages in a revision range, e.g. main..HEAD")
    parser.add_argument("--text", metavar="FILE", action="append", default=[],
                        help="scan a text file; '-' reads stdin (use for a PR body)")
    args = parser.parse_args(argv)

    if not (args.tree or args.commits or args.text):
        parser.error("nothing to scan: pass --tree, --commits or --text")

    findings: list[str] = []

    if args.tree:
        for path in tracked_text_files():
            findings += shape_hits(path.read_text(encoding="utf-8"),
                                   str(path.relative_to(REPO)))

    if args.commits:
        names = overlay_roster_names(single_words=True)
        for sha, body, identity in commit_records(args.commits):
            findings += shape_hits(f"{body}\n{identity}", sha)
            findings += roster_hits(body, sha, names)

    for spec in args.text:
        text = sys.stdin.read() if spec == "-" else Path(spec).read_text(encoding="utf-8")
        source = "<stdin>" if spec == "-" else spec
        findings += shape_hits(text, source)
        findings += roster_hits(text, source, overlay_roster_names(single_words=True))

    return _report(findings)


if __name__ == "__main__":
    sys.exit(main())
