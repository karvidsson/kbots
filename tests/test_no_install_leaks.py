"""Core must not carry any single installation's identity.

Core is published; overlays are private. Every deployment's roster, account
IDs, addresses and paths belong in the overlay, and CONTRIBUTING.md says so:
"No secrets, names, or numeric IDs in code, tests, or docs — use synthetic
fixtures". These tests are that rule made executable, because the rule on its
own did not hold.

Three surfaces, because it has now failed on all three:

  * the working tree — a live Discord account ID reached a test fixture and
    agent names reached code comments;
  * commit messages — agent names and a machine hostname reached the log, and
    no test read them, so tidying the tree left them untouched;
  * pull request titles and bodies — live account IDs reached GitHub, where
    only CI can see them (.github/workflows/ci.yml).

The scanning itself lives in scripts/check_install_leaks.py so that the test
suite, the commit-msg hook and CI all enforce one definition rather than three
that drift.

Enforcement is the test suite rather than only a git hook: hooks are per-clone
and `--no-verify` is in this repo's own push convention, whereas
scripts/self-deploy.sh runs the suite and refuses to ship a red one.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import check_install_leaks as leaks  # noqa: E402


def _hits(pattern, keep) -> list[str]:
    """Every match of `pattern` in tracked files that `keep(match)` accepts."""
    found = []
    for path in leaks.tracked_text_files(REPO):
        rel = path.relative_to(REPO)
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in pattern.finditer(line):
                if keep(m.group(0)):
                    found.append(f"{rel}:{i}: {m.group(0)}")
    return found


# --- the working tree -------------------------------------------------------

def test_no_real_discord_ids():
    """A live account ID is the worst of these: it identifies a real account and
    survives in history long after the working tree is tidied."""
    bad = _hits(leaks.SNOWFLAKE, leaks._snowflake_is_real)
    assert not bad, (
        "Real-looking Discord/snowflake IDs in Core:\n  " + "\n  ".join(bad)
        + f"\nUse {leaks.FAKE_SNOWFLAKE_PREFIX}001-style synthetic IDs instead.")


def test_no_real_email_addresses():
    bad = _hits(leaks.EMAIL, leaks._email_is_real)
    assert not bad, (
        "Real email addresses in Core:\n  " + "\n  ".join(bad)
        + "\nUse someone@example.com.")


def test_no_home_directory_paths():
    """An absolute home path names its owner and pins Core to one machine."""
    bad = _hits(leaks.HOME_PATH, leaks._home_names_someone)
    assert not bad, (
        "Home-directory paths in Core:\n  " + "\n  ".join(bad)
        + "\nUse a relative path, an env var, or /Users/you.")


def test_no_private_network_hostnames():
    bad = _hits(leaks.TAILNET, lambda s: True)
    assert not bad, "Tailnet hostnames in Core:\n  " + "\n  ".join(bad)


# --- commit messages --------------------------------------------------------

def _history_available() -> bool:
    proc = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=REPO,
                          capture_output=True, text=True)
    return proc.returncode == 0


def test_commit_history_carries_no_install_identifiers():
    """The shape checks, applied to every commit message reachable from HEAD.

    This is the check that was missing. Nothing read commit messages, so a real
    account ID or a machine hostname in one was invisible to the gate that was
    supposed to catch exactly that — and unlike a file, it could not be fixed by
    editing the working tree. Asserting over all of history rather than only new
    commits keeps it true after a rewrite instead of merely true going forward.
    """
    if not _history_available():
        pytest.skip("not a git checkout — no history to read")
    records = leaks.commit_records("HEAD", REPO)
    if not records:
        pytest.skip("no commit history available (shallow or empty clone)")

    bad = []
    for sha, body, identity in records:
        bad += leaks.shape_hits(f"{body}\n{identity}", sha)
    assert not bad, (
        "Commit messages carry installation-identifying text:\n  "
        + "\n  ".join(bad)
        + "\nThese cannot be fixed by editing files — the message itself must be "
          "rewritten, so keep them out in the first place.")


def test_branch_commit_messages_do_not_name_this_installations_agents():
    """Roster names — including single words — in the commits this branch adds.

    Scoped to `main..HEAD` on purpose. Over all of history a single-word roster
    name would match ordinary prose in old messages that can no longer be edited
    without another rewrite, and a check nobody can make green is a check people
    learn to skip. Over a branch, the cost of a false positive is rewording one
    sentence before it is merged.
    """
    if not _history_available():
        pytest.skip("not a git checkout — no history to read")
    names = leaks.overlay_roster_names(single_words=True)
    if not names:
        pytest.skip("no overlay roster present — nothing local to compare against")

    records = leaks.commit_records("main..HEAD", REPO)
    if not records:
        pytest.skip("no commits ahead of main — nothing new to check")

    bad = []
    for sha, body, _identity in records:
        bad += leaks.roster_hits(body, sha, names)
    assert not bad, (
        "New commit messages name agents from this installation:\n  "
        + "\n  ".join(bad)
        + "\nDescribe the role ('the ops agent') or use a fixture name (Atlas).")


# --- the working tree, against the local roster -----------------------------

def test_core_does_not_name_this_installations_agents():
    """Compare Core against the local overlay roster, never a list written here.

    Skipped without an overlay — a public clone has no roster to leak, and this
    test must not be the place someone's agent names finally get written down.
    """
    roster = leaks.overlay_roster_names()
    if not roster:
        pytest.skip("no overlay roster present — nothing local to compare against")

    # The licence names its copyright holder; that is what a licence is for.
    files = [p for p in leaks.tracked_text_files(REPO) if p.name != "LICENSE"]
    bad = []
    for path in files:
        bad += leaks.roster_hits(path.read_text(encoding="utf-8"),
                                 str(path.relative_to(REPO)), roster)
    assert not bad, (
        "Core names agents from this installation:\n  " + "\n  ".join(bad)
        + "\nInvent a fixture name (Data Bot, Atlas) — Core is published, the "
          "overlay is not.")


# --- human names leak one word at a time ------------------------------------

def test_human_names_split_into_words_in_single_word_mode(tmp_path, monkeypatch):
    """Forty commit messages named the owner by bare first name — never the
    full roster value — and passed. Human names must match word by word in
    single-word mode; agent names must stay whole ('Data Bot' must not turn
    every 'data' into a finding)."""
    import json
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "team.json").write_text(json.dumps({
        "humans": [{"id": "u1", "name": "Ada Lovelace"}],
        "agents": [{"id": "data-bot", "name": "Data Bot"}],
    }))
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))

    names = leaks.overlay_roster_names(single_words=True)
    assert "Ada Lovelace" in names
    assert "Lovelace" in names       # human name word, len >= 4
    assert "Ada" not in names        # short words stay out
    assert "Data" not in names       # agent names are never split

    assert leaks.roster_hits("Lovelace saw the status page", "msg", names)
    assert not leaks.roster_hits("the data pipeline is fine", "msg", names)

    # tree mode is unchanged: multi-word values only, no word splitting
    tree_names = leaks.overlay_roster_names()
    assert "Lovelace" not in tree_names


def test_env_injected_names_work_without_an_overlay(monkeypatch):
    """CI has no overlay — names arrive via KBOTS_LEAK_NAMES (a repo secret).
    Multi-word entries follow roster semantics: whole in tree mode, plus
    word-by-word in single-word mode."""
    monkeypatch.delenv("KBOTS_OVERLAY", raising=False)
    monkeypatch.setenv("KBOTS_LEAK_NAMES", "Ada Lovelace\nzeta-bot\nxy\n")

    single = leaks.overlay_roster_names(single_words=True)
    assert {"Ada Lovelace", "Lovelace", "zeta-bot"} <= single
    assert "xy" not in single            # < 4 chars ignored

    tree = leaks.overlay_roster_names()
    assert "Ada Lovelace" in tree and "zeta-bot" in tree
    assert "Lovelace" not in tree        # no word-split in tree mode

    assert leaks.roster_hits("ping zeta-bot about it", "msg", single)
