#!/usr/bin/env python3
"""Manage the kbots vault — add, view, delete secrets."""
from pathlib import Path

from src.core.base import resolve_config_file, resolve_vault_key_file
from src.vault.fernet import FernetVault, _normalise_key, describe_value_faults

KEY_FILE = resolve_vault_key_file()

# The canonical name for every secret Core tools read. Keep this in step with
# the vault.get() call sites: a key missing from here is a key an operator has
# to guess, and guessing is what this list exists to prevent. Deployments can
# still add their own via "Custom key" — per-bot tokens like
# "discord-token-atlas" are looked up dynamically and belong nowhere in here.
ALL_KEYS = [
    # Discord (one per bot account)
    "discord-token",
    # AI / LLM
    "secrets/gemini-api-key",
    "secrets/groq-api-key",
    # Google Workspace (OAuth2)
    "secrets/google-api-credentials.json",
    # Web search
    "secrets/tavily-api-key",
    "secrets/serpapi-key",
    # Integrations
    "secrets/cloudflare-api-token",
    "secrets/notion-api-key",
    "secrets/trello-credentials.json",
    # Messaging
    "secrets/slack-bot-token",
    "secrets/twilio-account-sid",
    "secrets/twilio-auth-token",
    "secrets/twilio-from-number",
    # GitHub
    "github-token",
]


def canonical_for(key: str) -> str | None:
    """The canonical spelling of `key`, if it is a known secret spelt oddly.

    Catches the mistake at the point it is made: someone types
    "cloudflare-api-token", it saves fine, and days later every Cloudflare tool
    says "not configured" while the value sits in the vault under a
    near-identical name.

    Note that a returned canonical name does NOT mean the typed one is dead.
    Some — CLOUDFLARE_API_TOKEN, GITHUB_TOKEN — are read as explicit fallbacks
    and work fine. They are still worth converging on one spelling, because two
    live spellings is how you end up updating the copy nothing reads. Callers
    must phrase this as "not canonical", never as "nothing reads this".

    Deliberately narrow. It only fires when the typed name normalises to
    exactly one entry in ALL_KEYS and differs from it, so genuinely custom keys
    — per-bot tokens, deployment-specific secrets, anything looked up
    dynamically — are never second-guessed.
    """
    if key in ALL_KEYS:
        return None
    matches = {k for k in ALL_KEYS if _normalise_key(k) == _normalise_key(key)}
    return matches.pop() if len(matches) == 1 else None


def get_vault():
    # Resolve the SAME vault the service reads (overlay → modules → core), not a
    # CWD-relative path — otherwise secrets added from the wrong dir silently go
    # to a stray secrets.enc the service never loads.
    v = FernetVault(str(resolve_config_file("secrets.enc")))
    v.unlock(KEY_FILE.read_text().strip())
    return v


def cmd_list(v):
    keys = sorted(v.list_keys())
    print(f"\n  Vault: {len(keys)} secrets\n")
    for k in keys:
        print(f"    {k}")
    print()


def cmd_add(v):
    # Show every secret already in the vault (pick one to update), then the
    # common keys not yet set (suggestions to add). Previously only the fixed
    # ALL_KEYS template was listed, so existing custom secrets were invisible.
    existing = sorted(v.list_keys())
    suggested = [k for k in ALL_KEYS if k not in existing]
    options = existing + suggested

    print("\n  Keys (* = already set — pick a number to update, or add a suggested/custom key):")
    for i, key in enumerate(options, 1):
        mark = "  *" if key in existing else ""
        print(f"    {i:>2}) {key}{mark}")
    custom_num = len(options) + 1
    print(f"    {custom_num:>2}) Custom key")

    choice = input(f"\n  Pick [1-{custom_num}] or type key name: ").strip()

    try:
        idx = int(choice)
        if idx == custom_num:
            key = input("  Key name: ").strip()
        elif 1 <= idx <= len(options):
            key = options[idx - 1]
        else:
            print("  Invalid number.")
            return
    except ValueError:
        key = choice  # typed a key name directly

    if not key:
        print("  No key provided, aborting.")
        return

    typed = key
    canonical = canonical_for(key)
    if canonical:
        # Deliberately never claims "nothing reads this". Several of these names
        # ARE read, as explicit fallbacks — cloudflare.py reads
        # CLOUDFLARE_API_TOKEN, github.py reads GITHUB_TOKEN. Saying otherwise
        # is both false and, worse, tells someone their working setup is broken.
        print(f"\n  ⚠️  '{key}' is not the name Core looks up first — that is '{canonical}'.")
        if canonical in v.list_keys():
            # The dangerous case: both spellings hold a credential, and the one
            # you are about to edit is not the one tools prefer.
            print(f"      '{canonical}' is ALSO in the vault, and tools read it first.")
            print(f"      Updating only '{key}' would leave the value actually in use")
            print("      unchanged — the classic 'I fixed it and it is still broken'.")
        else:
            print("      Saving under the name you typed still works, but the canonical")
            print("      name is the one every tool agrees on.")
        if input(f"      Use '{canonical}' instead? [Y/n]: ").strip().lower() not in ("n", "no"):
            key = canonical

    # Exact, not v.get(): lookups fall back to a normalised match, so v.get()
    # would report a differently-spelt key as "already exists" and then write a
    # second one beside it.
    if key in v.list_keys():
        confirm = input(f"  '{key}' already exists. Overwrite? [y/N]: ").strip().lower()
        if confirm != "y":
            print("  Skipped.")
            return

    value = input(f"  Paste value for '{key}': ").strip()
    if not value:
        print("  Empty value, aborting.")
        return

    v.set(key, value)
    print(f"  Saved '{key}'. Total secrets: {len(v.list_keys())}")

    # Renaming leaves the old spelling behind holding a stale credential, which
    # is worth clearing: two keys that differ only in spelling are exactly what
    # makes a lookup ambiguous later.
    if key != typed and typed in v.list_keys():
        if input(f"  Remove the old '{typed}'? [Y/n]: ").strip().lower() not in ("n", "no"):
            v.delete(typed)
            print(f"  Removed '{typed}'. Total secrets: {len(v.list_keys())}")


def cmd_delete(v):
    key = input("  Key to delete: ").strip()
    if not key:
        return
    if not v.get(key):
        print(f"  '{key}' not found.")
        return
    confirm = input(f"  Delete '{key}'? [y/N]: ").strip().lower()
    if confirm == "y":
        v.delete(key)
        print(f"  Deleted. Total secrets: {len(v.list_keys())}")


def cmd_check(v):
    key = input("  Key to check: ").strip()
    val = v.get(key)
    if not val:
        print(f"  '{key}' = NOT FOUND")
        return
    exact = "" if key in v.list_keys() else "  (resolved by name normalisation)"
    print(f"  '{key}' = exists ({len(val)} chars){exact}")
    # "Exists" is not the same as "works" — say so here rather than let a tool
    # discover it later as a codec error.
    for fault in describe_value_faults(val):
        print(f"    ⚠️  {fault}")


def cmd_health(v):
    """Report keys that are misspelt, duplicated, or hold an unusable value.

    Exists because these failures are invisible until a tool dies far away from
    the vault: an unreadable name reports "not configured", a duplicate lets you
    update the copy nothing reads, and a value containing non-ASCII kills the
    HTTP request with a codec error that never mentions credentials. Values are
    never printed — only their shape.
    """
    keys = sorted(v.list_keys())
    problems = 0

    groups: dict[str, list[str]] = {}
    for k in keys:
        groups.setdefault(_normalise_key(k), []).append(k)

    dupes = {n: ks for n, ks in groups.items() if len(ks) > 1}
    if dupes:
        print("\n  Duplicate keys — these differ only in spelling:")
        for norm, ks in sorted(dupes.items()):
            problems += 1
            print(f"    {norm}: {', '.join(ks)}")
        print("    A lookup matching one exactly still works, but any lookup that")
        print("    matches none of them exactly is ambiguous and returns nothing.")
        print("    Worse, updating one leaves the others holding a stale credential.")
        print("    Keep one and delete the rest.")

    misnamed = [(k, canonical_for(k)) for k in keys]
    misnamed = [(k, c) for k, c in misnamed if c]
    if misnamed:
        print("\n  Non-canonical names — Core looks up a different name first:")
        for k, c in misnamed:
            problems += 1
            print(f"    {k}  ->  canonical is  {c}")
        print("    Some of these are read as explicit fallbacks and work today.")
        print("    They are worth renaming anyway: the fallback chains are the")
        print("    reason nobody can tell which name is the real one.")

    bad_values = [(k, describe_value_faults(v.get(k) or "")) for k in keys]
    bad_values = [(k, f) for k, f in bad_values if f]
    if bad_values:
        print("\n  Malformed values — present and findable, but unusable:")
        for k, faults in bad_values:
            problems += 1
            for fault in faults:
                print(f"    {k}: {fault}")
        print("    HTTP headers are latin-1, so a non-ASCII character stops the")
        print("    request being built at all; stray whitespace usually surfaces")
        print("    as a 401 that reads like a permissions problem. Re-paste, or")
        print("    mint a fresh credential.")

    if problems:
        print(f"\n  {problems} problem(s) across {len(keys)} secrets.\n")
    else:
        print(f"\n  No problems found across {len(keys)} secrets.\n")


def cmd_migrate_salt(v):
    salt_path = Path("config/secrets.salt")
    if salt_path.exists():
        print("  Per-instance salt already exists — already migrated.")
        return
    print("\n  This will migrate from the hardcoded vault salt to a random per-instance salt.")
    print("  The vault will be re-encrypted. Your passphrase is needed to re-derive the key.")
    confirm = input("\n  Proceed? [y/N]: ").strip().lower()
    if confirm != "y":
        print("  Aborted.")
        return
    passphrase = KEY_FILE.read_text().strip()
    v.migrate_salt(passphrase)
    print(f"  Salt migrated. New salt saved to {salt_path}")
    print(f"  Vault re-encrypted with new key. Total secrets: {len(v.list_keys())}")


def detect_overlay() -> tuple[str, str] | None:
    """Find the overlay this host actually runs, and say where the answer came from.

    An unset KBOTS_OVERLAY does NOT mean "engine-local install". The variable
    is exported from a shell profile, so any shell opened before setup ran, and
    every non-interactive shell on Debian (the profile export sits below the
    interactive guard), sees it unset on a perfectly normal overlay install.
    Inferring engine-local from that sends secrets to a file the service never
    reads, and answering "y" at the old prompt did exactly that.

    So look at what is installed rather than at the environment.
    """
    import os
    import re
    env = os.environ.get("KBOTS_OVERLAY")
    if env:
        return env, "KBOTS_OVERLAY"

    # The service unit is authoritative: it is what the running engine reads.
    for unit in (Path("/etc/systemd/system/kbots.service"),
                 Path("/etc/systemd/system/k-agents.service"),
                 Path.home() / ".config/systemd/user/kbots.service"):
        try:
            m = re.search(r"^Environment=KBOTS_OVERLAY=(.+)$", unit.read_text(), re.M)
        except OSError:
            continue
        if m:
            return m.group(1).strip(), str(unit)
    plist = Path.home() / "Library/LaunchAgents/com.kbots.agent.plist"
    try:
        m = re.search(r"<key>KBOTS_OVERLAY</key>\s*<string>([^<]+)</string>",
                      plist.read_text())
        if m:
            return m.group(1).strip(), str(plist)
    except OSError:
        pass

    # No unit (a fresh install, or a user-run engine): the shell profile still
    # records the intent even when the current shell never sourced it.
    for profile in (Path.home() / ".bashrc", Path.home() / ".zshrc",
                    Path.home() / ".profile"):
        try:
            m = re.search(r"^\s*export\s+KBOTS_OVERLAY=(.+)$", profile.read_text(), re.M)
        except OSError:
            continue
        if m:
            return m.group(1).strip().strip('"\''), f"~/{profile.name}"
    return None


def main():
    import os
    detected = detect_overlay()
    if detected and not os.environ.get("KBOTS_OVERLAY"):
        overlay, source = detected
        if (Path(overlay) / "config").is_dir():
            os.environ["KBOTS_OVERLAY"] = overlay
            print("\n  KBOTS_OVERLAY was unset in this shell — using the overlay this")
            print(f"  host is installed with, found in {source}:")
            print(f"      {overlay}")
        else:
            detected = None

    vault_path = resolve_config_file("secrets.enc")
    if not os.environ.get("KBOTS_OVERLAY"):
        print("\n  ⚠️  No overlay install detected — falling back to the vault inside")
        print(f"      the engine checkout: {vault_path}")
        print("      On an overlay-based install the service does NOT read this file,")
        print("      so secrets saved here will never reach the bots. Set KBOTS_OVERLAY")
        print("      to your overlay path and re-run unless this is intentional.")
        if input("      Continue anyway? [y/N]: ").strip().lower() != "y":
            return

    v = get_vault()
    print(f"\n  Vault: {vault_path}")

    while True:
        print("\n  kbots Vault Manager")
        print("  ─────────────────────")
        print("  1) List secrets")
        print("  2) Add/update secret")
        print("  3) Check if secret exists")
        print("  4) Delete secret")
        print("  5) Check all keys and values for problems")
        print("  6) Migrate vault salt")
        print("  q) Quit")

        try:
            choice = input("\n  > ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\n  Done.")
            break

        try:
            if choice in ("1", "list"):
                cmd_list(v)
            elif choice in ("2", "add"):
                cmd_add(v)
            elif choice in ("3", "check"):
                cmd_check(v)
            elif choice in ("4", "delete"):
                cmd_delete(v)
            elif choice in ("5", "health", "doctor"):
                cmd_health(v)
            elif choice in ("6", "migrate"):
                cmd_migrate_salt(v)
            elif choice in ("q", "quit", "exit"):
                print("  Done.")
                break
            else:
                print("  Invalid choice.")
        except (KeyboardInterrupt, EOFError):
            # Ctrl-C / Ctrl-D mid-operation → abandon it, back to the menu.
            print("\n  Cancelled — back to menu.")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n  Done.")
