#!/usr/bin/env python3
"""kbots Uninstall Wizard — remove a deployment (and optionally export it first).

Reverses what setup.py created: stops/removes the service, revokes passwordless
sudo, strips the shell-profile env exports, and deletes the overlay, the engine
clone, and the vault key file — with confirmation and a clear summary first.

Export your installation before removing it (to move to another machine):
    uv run python uninstall.py            # offers export, then removes
    uv run python uninstall.py --export-only

Usage: uv run python uninstall.py
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.core.base import resolve_vault_key_file  # noqa: E402

BOLD, DIM, GREEN, YELLOW, RED, CYAN, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[36m", "\033[0m")


def header(t): print(f"\n{BOLD}{CYAN}── {t} ──{RESET}\n")
def ok(m): print(f"  {GREEN}✓{RESET} {m}")
def warn(m): print(f"  {YELLOW}!{RESET} {m}")
def err(m): print(f"  {RED}✗{RESET} {m}")
def info(m): print(f"  {DIM}{m}{RESET}")


def ask(prompt, default=""):
    s = f" [{default}]" if default else ""
    return input(f"  {prompt}{s}: ").strip() or default


def ask_yn(prompt, default=False):
    yn = "[Y/n]" if default else "[y/N]"
    r = input(f"  {prompt} {yn}: ").strip().lower()
    return default if not r else r in ("y", "yes")


def detect(state: dict):
    header("Detecting installation")

    # Overlay: env var, or ask
    overlay = os.environ.get("KBOTS_OVERLAY", "")
    if overlay and Path(overlay).is_dir():
        info(f"KBOTS_OVERLAY = {overlay}")
    else:
        overlay = ask("Overlay directory to remove (blank to skip)")
    state["overlay"] = Path(overlay).resolve() if overlay else None

    # Engine root: from an agent's .mcp.json cwd; else the default install dir
    # (covers partial installs whose overlay has no agents yet).
    engine = None
    if state["overlay"]:
        for mcp in state["overlay"].glob("agents/*/.mcp.json"):
            try:
                import json
                cwd = json.loads(mcp.read_text()).get(
                    "mcpServers", {}).get("kbots-tools", {}).get("cwd")
                if cwd:
                    engine = Path(cwd)
                    break
            except Exception:
                pass
    if engine is None:
        default = (Path.home() / "kbots" if sys.platform == "darwin"
                   else Path("/opt/kbots"))
        if (default / ".git").is_dir() and default.resolve() != PROJECT_ROOT:
            engine = default
    state["engine"] = engine

    # Service (platform-specific)
    state["darwin"] = sys.platform == "darwin"
    if state["darwin"]:
        plist = Path.home() / "Library" / "LaunchAgents" / "com.kbots.agent.plist"
        state["launchd_plist"] = plist if plist.exists() else None
    else:
        unit = Path("/etc/systemd/system/kbots.service")
        state["systemd_unit"] = unit if unit.exists() else None

    # Sudoers, profile, vault key
    sudoers = Path("/etc/sudoers.d/kbots-fullcontrol")
    state["sudoers"] = sudoers if sudoers.exists() else None
    state["key_file"] = resolve_vault_key_file() if resolve_vault_key_file().exists() else None
    profiles = []
    for pf in (Path.home() / ".zshrc", Path.home() / ".bashrc"):
        if pf.exists() and "KBOTS_OVERLAY" in pf.read_text():
            profiles.append(pf)
    state["profiles"] = profiles

    # Summary
    print()
    info("Found:")
    print(f"    Overlay:      {state['overlay'] or '(none)'}")
    print(f"    Engine clone: {state['engine'] or '(unknown / in-place)'}")
    svc = (state.get("launchd_plist") if state["darwin"] else state.get("systemd_unit"))
    print(f"    Service:      {svc or '(not installed)'}")
    print(f"    Sudo rule:    {state['sudoers'] or '(none)'}")
    print(f"    Vault key:    {state['key_file'] or '(none)'}")
    print(f"    Shell env:    {', '.join(str(p.name) for p in profiles) or '(none)'}")


def export_installation(state: dict):
    if not state["overlay"]:
        warn("No overlay to export.")
        return
    header("Export installation (move to another machine)")
    if not ask_yn("Export this installation to a portable bundle first?", default=True):
        return
    out = ask("Output directory for the bundle", str(Path.home()))
    with_key = ask_yn("Include the vault passphrase key? (needed to unlock secrets "
                      "on the new machine, but sensitive)", default=False)
    cmd = [sys.executable, str(PROJECT_ROOT / "scripts" / "migrate.py"), "export",
           "--overlay", str(state["overlay"]), "--out", out]
    if with_key:
        cmd.append("--with-key")
    # Stamp the filename with a timestamp from the shell (scripts can't use time here cheaply)
    import time
    cmd += ["--timestamp", time.strftime("%Y%m%d-%H%M%S")]
    subprocess.run(cmd, check=False)


def _stop_service(state: dict):
    if state["darwin"]:
        label = state["launchd_plist"].stem if state.get("launchd_plist") else "com.kbots.agent"
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
                       capture_output=True)
        if state.get("launchd_plist"):
            state["launchd_plist"].unlink(missing_ok=True)
        ok("Stopped + removed launchd service")
    else:
        unit_name = state["systemd_unit"].stem if state.get("systemd_unit") else "kbots"
        subprocess.run(["sudo", "-n", "systemctl", "disable", "--now", unit_name],
                       capture_output=True)
        subprocess.run(["sudo", "-n", "rm", "-f", str(state["systemd_unit"] or "/etc/systemd/system/kbots.service")],
                       capture_output=True)
        subprocess.run(["sudo", "-n", "systemctl", "daemon-reload"], capture_output=True)
        ok("Stopped + removed systemd service")


def _strip_profiles(state: dict):
    for pf in state["profiles"]:
        lines = pf.read_text().splitlines(keepends=True)
        out, skip = [], False
        for line in lines:
            if line.strip() == "# kbots environment":
                if out and out[-1].strip() == "":
                    out.pop()
                skip = True
                continue
            if skip and line.startswith("export KBOTS_"):
                continue
            skip = False
            out.append(line)
        pf.write_text("".join(out))
        ok(f"Removed env exports from ~/{pf.name}")


def remove(state: dict):
    header("Remove installation")
    warn("This permanently deletes the overlay (config, vault, agents, memory),")
    warn("the engine clone, the vault key, the service, and shell env exports.")
    if state["overlay"]:
        info(f"Make sure you've exported first if you want to keep {state['overlay'].name}.")
    print()
    if not ask_yn("Proceed with uninstall?", default=False):
        info("Aborted — nothing removed.")
        return

    svc = (state.get("launchd_plist") if state["darwin"] else state.get("systemd_unit"))
    if svc:
        _stop_service(state)
    if state["sudoers"]:
        script = PROJECT_ROOT / "scripts" / "full-control.sh"
        if script.exists():
            subprocess.run(["bash", str(script), "revoke"])
        ok("Revoked passwordless sudo")
    if state["profiles"]:
        _strip_profiles(state)
    if state["key_file"]:
        state["key_file"].unlink(missing_ok=True)
        ok(f"Deleted vault key: {state['key_file']}")
    if state["overlay"] and state["overlay"].is_dir():
        shutil.rmtree(state["overlay"], ignore_errors=True)
        ok(f"Deleted overlay: {state['overlay']}")
    if state["engine"] and state["engine"] != PROJECT_ROOT and state["engine"].is_dir():
        if ask_yn(f"Also delete the engine clone at {state['engine']}?", default=True):
            shutil.rmtree(state["engine"], ignore_errors=True)
            ok(f"Deleted engine clone: {state['engine']}")

    print()
    ok("kbots uninstalled.")
    info("This checkout (the engine source) was left in place.")


def main():
    print(f"\n{BOLD}{CYAN}kbots Uninstall Wizard{RESET}")
    export_only = "--export-only" in sys.argv

    state: dict = {}
    detect(state)
    export_installation(state)
    if export_only:
        ok("Export-only mode — nothing removed.")
        return
    remove(state)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n\n  {YELLOW}Interrupted — nothing further removed.{RESET}\n")
        sys.exit(130)
