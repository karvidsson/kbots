#!/usr/bin/env python3
"""kbots Interactive Setup Wizard.

Complete setup from clone to running agent — dependencies, overlay,
vault, Discord, agents, systemd, everything.

Usage: uv run python setup.py
"""

import getpass
import json
import os
import re
import secrets as py_secrets
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

# Engine root the deployment runs from. Defaults to the checkout the wizard
# was started in; step_install may repoint it at a dedicated install clone so
# the working repo stays disconnected from the running service.
ENGINE_ROOT = PROJECT_ROOT

try:
    import yaml
except ImportError:
    print("PyYAML not found. Run: uv sync")
    sys.exit(1)

try:
    from src.core.agent_scaffold import (
        AGENT_NAME_RULE,
        agent_name_error,
        scaffold_agent,
        suggest_agent_name,
    )
    from src.core.base import resolve_vault_key_file, write_private_file
    from src.vault.fernet import FernetVault
except ImportError:
    print("kbots modules not found. Run: uv sync")
    sys.exit(1)


# --- Formatting ---

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
RESET = "\033[0m"


TAGLINE = "one process · LLM-agnostic · trains itself"

# Vault passphrases gate every stored credential; with the KDF alone a short
# one is trivially brute-forceable if secrets.enc + .salt ever leak.
MIN_PASSPHRASE_LEN = 12


def banner(title: str = "kbots Setup Wizard"):
    """Boxed banner, built rather than hand-drawn.

    The two hand-drawn boxes had drifted: settings.py rendered 38 characters
    inside a 40-character border, so it printed crooked. Centring in code means
    a retitle cannot misalign it again.
    """
    lines = [title, TAGLINE]
    inner = max(len(line) for line in lines) + 8
    top = "╔" + "═" * inner + "╗"
    bottom = "╚" + "═" * inner + "╝"
    body = "\n".join(f"║{line.center(inner)}║" for line in lines)
    print(f"\n{BOLD}{CYAN}{top}\n{body}\n{bottom}{RESET}\n")


_PROGRESS = {"current": 0, "total": 0}


def header(text: str):
    # While main() drives the step list, replace each step's hardcoded
    # "Step N:" prefix with a live "Step k/total:" — the user sees how far
    # along they are, and the numbering can't drift from the list again.
    m = re.match(r"Step \d+[a-z]?: (.*)", text)
    if m and _PROGRESS["total"]:
        text = f"Step {_PROGRESS['current']}/{_PROGRESS['total']}: {m.group(1)}"
    print(f"\n{BOLD}{CYAN}── {text} ──{RESET}\n")


def ok(text: str):
    print(f"  {GREEN}✓{RESET} {text}")


def warn(text: str):
    print(f"  {YELLOW}!{RESET} {text}")


def err(text: str):
    print(f"  {RED}✗{RESET} {text}")


def info(text: str):
    print(f"  {DIM}{text}{RESET}")


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    result = input(f"  {prompt}{suffix}: ").strip()
    return result or default


def ask_yn(prompt: str, default: bool = True) -> bool:
    yn = "[Y/n]" if default else "[y/N]"
    result = input(f"  {prompt} {yn}: ").strip().lower()
    if not result:
        return default
    return result in ("y", "yes")


def ask_secret(prompt: str) -> str:
    return getpass.getpass(f"  {prompt}: ").strip()


def ask_choice(prompt: str, options: list[str], default: str = "") -> str:
    for i, opt in enumerate(options, 1):
        marker = " (default)" if opt == default else ""
        print(f"    {i}) {opt}{marker}")
    while True:
        result = input(f"  {prompt}: ").strip()
        if not result and default:
            return default
        if result in options:
            return result
        try:
            idx = int(result) - 1
            if 0 <= idx < len(options):
                return options[idx]
        except ValueError:
            pass
        print(f"  Please enter 1-{len(options)} or a valid option.")


def ask_menu(prompt: str, options: list[str], default: int = 1) -> int:
    """Print a numbered menu once and return the chosen 1-based index."""
    for i, opt in enumerate(options, 1):
        marker = " (default)" if i == default else ""
        print(f"    {i}) {opt}{marker}")
    while True:
        result = input(f"  {prompt}: ").strip()
        if not result:
            return default
        try:
            n = int(result)
            if 1 <= n <= len(options):
                return n
        except ValueError:
            pass
        print(f"  Please enter a number 1-{len(options)}.")


def _is_snowflake(s: str) -> bool:
    """A Discord ID is a numeric snowflake, 17-20 digits."""
    return s.isdigit() and 17 <= len(s) <= 20


def ask_id(prompt: str, required: bool = False) -> str:
    """Ask for a single Discord ID, validating the snowflake shape.

    Re-prompts on obviously-wrong input; blank skips unless required.
    """
    while True:
        v = ask(prompt)
        if not v:
            if required:
                err("Required — paste the Discord ID (enable Developer Mode, "
                    "right-click → Copy ID).")
                continue
            return ""
        if _is_snowflake(v):
            return v
        err("That doesn't look like a Discord ID (should be 17-20 digits). "
            "Right-click the server/user/channel → Copy ID.")
        if not required and not ask_yn("Try again?", default=True):
            return ""


def ask_agent_name(prompt: str, default: str = "") -> str:
    """Ask for an agent's internal name, validating it here and now.

    This used to be a bare ask(). The name was not checked until scaffold_agent
    ran, many steps later, and a capitalised name then aborted the whole wizard
    and rolled back — so a single typo cost every answer given since. Validating
    at the point of entry is the difference between a two-second correction and
    starting over.
    """
    while True:
        raw = ask(prompt, default)
        problem = agent_name_error(raw)
        if not problem:
            return raw
        err(problem)
        suggestion = suggest_agent_name(raw)
        if suggestion:
            info(f"Suggested: {suggestion}")
            if ask_yn(f"Use '{suggestion}'?", default=True):
                return suggestion
        info(f"Internal name: {AGENT_NAME_RULE}.")


def ask_ids(label: str) -> list[str]:
    """Collect zero or more Discord IDs, validated and de-duplicated."""
    info(f"Add {label} ID(s) one per line. Press Enter on a blank line when done.")
    ids: list[str] = []
    while True:
        v = input(f"  {label} ID (Enter when done): ").strip()
        if not v:
            break
        if not _is_snowflake(v):
            err("Not a Discord ID (17-20 digits) — skipped.")
            continue
        if v in ids:
            warn(f"{v} already added — skipping duplicate.")
            continue
        ids.append(v)
        ok(f"Added {label} {v}")
    return ids


# --- Discord API ---

def _looks_like_discord_token(s: str) -> bool:
    """Heuristic: does this string look like a Discord bot token?

    Bot tokens are dot-separated base64url segments (id.timestamp.hmac),
    ~59-72 chars. Used so a token pasted at a yes/no prompt isn't mistaken
    for 'no' and silently skipped.
    """
    s = s.strip()
    parts = s.split(".")
    return (
        len(parts) >= 2
        and len(s) >= 50
        and all(c.isalnum() or c in "._-" for c in s)
    )


def validate_discord_token(token: str) -> tuple[dict | None, str]:
    """Validate a Discord bot token by calling the API.

    Returns (bot_info, "") on success, (None, reason) on failure — where
    reason is "invalid" for a rejected token and "network" when Discord could
    not be reached. The two must stay distinguishable: an invalid token should
    be re-entered, while a network failure is a legitimate reason to store the
    token unverified (air-gapped installs).

    A User-Agent is required — Discord's API is fronted by Cloudflare, which
    blocks header-less requests with a 403 (error 1010) before the token is
    ever checked. Without it, every valid token would look invalid.
    """
    try:
        req = urllib.request.Request(
            "https://discord.com/api/v10/users/@me",
            headers={
                "Authorization": f"Bot {token.strip()}",
                "User-Agent": "DiscordBot (https://github.com/karvidsson/kbots, 1.0)",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode()), ""
    except urllib.error.HTTPError:
        return None, "invalid"
    except urllib.error.URLError:
        return None, "network"


def ask_discord_token(prompt: str) -> str:
    """Collect a bot token without echoing it.

    Tokens grant full control of the bot account, and a bare input() leaves
    them in terminal scrollback and session recordings — so entry is hidden,
    always, rather than behind an opt-in.
    """
    return ask_secret(f"{prompt} (input hidden, Enter to skip)")


# Discord permission bits for the invite URL — the minimal set kbots needs
# (README → Quickstart Step 1). Administrator is deliberately absent: it
# would hand any leaked token or prompt-injected agent action full control
# of the server. Manage Channels goes to the setup account alone, so server
# auto-setup can create the fleet channels.
_INVITE_PERMS = (
    (1 << 6)     # Add Reactions — HITL approval cards, reply "show more"
    | (1 << 10)  # View Channels
    | (1 << 11)  # Send Messages
    | (1 << 14)  # Embed Links
    | (1 << 15)  # Attach Files
    | (1 << 16)  # Read Message History
    | (1 << 26)  # Change Nickname — identity boot sets the agent's own nick
)
_PERM_MANAGE_CHANNELS = 1 << 4


def invite_url(app_id: str, manage_channels: bool = False) -> str:
    """The OAuth2 install link for a bot application."""
    perms = _INVITE_PERMS | (_PERM_MANAGE_CHANNELS if manage_channels else 0)
    return ("https://discord.com/oauth2/authorize"
            f"?client_id={app_id}&scope=bot%20applications.commands"
            f"&permissions={perms}")


def _application_id(token: str, bot_info: dict | None) -> str:
    """The application (client) id an invite URL needs.

    /oauth2/applications/@me is authoritative; the bot USER id from /users/@me
    equals it for modern applications and covers the offline-validated case.
    """
    try:
        req = urllib.request.Request(
            "https://discord.com/api/v10/oauth2/applications/@me",
            headers={
                "Authorization": f"Bot {token.strip()}",
                "User-Agent": "DiscordBot (https://github.com/karvidsson/kbots, 1.0)",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            app_id = json.loads(resp.read().decode()).get("id", "")
            if app_id:
                return str(app_id)
    except (urllib.error.URLError, urllib.error.HTTPError):
        pass
    return str((bot_info or {}).get("id", "") or "")


def show_invite_link(state: dict, bot_name: str, token: str,
                     bot_info: dict | None, manage_channels: bool = False) -> None:
    """Print a bot's server-install link and remember it for the summary.

    Silent when Discord is unreachable and the token was never validated —
    there is nothing to build the link from, and the air-gapped install that
    hits this path has no use for an OAuth URL anyway.
    """
    app_id = _application_id(token, bot_info)
    if not app_id:
        return
    url = invite_url(app_id, manage_channels)
    info("Install link — open it to add this bot to a server (grants the "
         "minimal permission set kbots needs; no Administrator):")
    print(f"    {CYAN}{url}{RESET}")
    state.setdefault("invite_urls", {})[bot_name] = url


# ==========================================================================
# Steps
# ==========================================================================

def step_dependencies(state: dict):
    header("Step 1: Dependencies")

    # Python version
    v = sys.version_info
    if v.major >= 3 and v.minor >= 12:
        ok(f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        err(f"Python {v.major}.{v.minor} — 3.12+ required")
        sys.exit(1)

    # uv
    if shutil.which("uv"):
        ok("uv")
    else:
        # Ask first, like every other curl|sh in this step (pkgx, Claude CLI) —
        # piping a remote script to a shell is a trust decision, not a detail.
        info("uv not found. It will be installed by piping "
             "https://astral.sh/uv/install.sh to sh.")
        if not ask_yn("Install uv now?", default=True):
            err("uv is required. Install it manually (https://docs.astral.sh/uv/) "
                "and re-run setup.")
            sys.exit(1)
        try:
            subprocess.run(
                ["bash", "-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"],
                check=True,
            )
            ok("uv installed")
        except subprocess.CalledProcessError:
            err("Failed to install uv. Install manually: https://docs.astral.sh/uv/")
            sys.exit(1)

    # jq (optional but useful)
    if shutil.which("jq"):
        ok("jq")
    else:
        warn("jq not found")
        if ask_yn("Install now?"):
            if sys.platform == "darwin":
                if shutil.which("brew"):
                    try:
                        subprocess.run(["brew", "install", "jq"], check=True)
                        ok("jq installed")
                    except subprocess.CalledProcessError:
                        warn("Failed to install jq — install manually: brew install jq")
                else:
                    warn("Homebrew not found — install jq manually: https://jqlang.org/download/")
            else:
                try:
                    subprocess.run(["sudo", "apt-get", "install", "-y", "jq"], check=True)
                    ok("jq installed")
                except subprocess.CalledProcessError:
                    warn("Failed to install jq — install manually: sudo apt install jq")

    # pkgx (optional): zero-install CLI provider — media tools (ffmpeg), tmux,
    # and agents' Bash can then run CLI deps on demand, cached, no sudo.
    if shutil.which("pkgx"):
        ok("pkgx (zero-install CLI provider)")
    else:
        info("Optional: pkgx (4MiB) lets kbots run CLI tools like ffmpeg on")
        info("demand with no install step (https://pkgx.sh).")
        if ask_yn("Install pkgx?", default=False):
            try:
                subprocess.run(["bash", "-c", "curl -fsSL https://pkgx.sh | sh"],
                               check=True)
                ok("pkgx installed" if shutil.which("pkgx")
                   else "pkgx installed (restart your shell to pick up PATH)")
            except subprocess.CalledProcessError:
                warn("pkgx install failed — skip; everything works without it")

    # Claude Code CLI
    if shutil.which("claude"):
        ok("Claude Code CLI")
    else:
        warn("Claude Code CLI not found")
        if ask_yn("Install now?"):
            try:
                subprocess.run(
                    ["bash", "-c", "curl -fsSL https://claude.ai/install.sh | sh"],
                    check=True,
                )
                if shutil.which("claude"):
                    ok("Claude Code CLI installed")
                else:
                    warn("Installed but not on PATH — restart your shell")
            except subprocess.CalledProcessError:
                err("Installation failed")
                info("Install manually: https://docs.anthropic.com/en/docs/claude-code")
        else:
            info("Install later: curl -fsSL https://claude.ai/install.sh | sh")

    # Claude Code authentication — agents can't respond without it
    if shutil.which("claude"):
        while True:
            try:
                result = subprocess.run(
                    ["claude", "auth", "status"],
                    capture_output=True, text=True, timeout=20,
                )
                authed = result.returncode == 0
            except (subprocess.TimeoutExpired, FileNotFoundError):
                authed = False
            if authed:
                ok("Claude Code authenticated")
                break
            warn("Claude Code CLI is not authenticated — agents cannot respond without it.")
            info("In ANOTHER terminal run:  claude auth login   (Pro/Max plan; add --console for API billing)")
            choice = ask("Press Enter to re-check, or 's' to skip", "")
            if choice.lower() == "s":
                warn("Skipped — agents will fail until you run: claude auth login")
                break

    # Sync Python deps
    info("Installing Python dependencies...")
    sync_script = PROJECT_ROOT / "scripts" / "sync.sh"
    if sync_script.exists():
        subprocess.run([str(sync_script)], cwd=str(PROJECT_ROOT), check=False)
    else:
        subprocess.run(["uv", "sync"], cwd=str(PROJECT_ROOT), check=False)
    ok("Dependencies installed")


def step_install(state: dict):
    """Deploy the engine to a dedicated install directory.

    The running service must be disconnected from the working checkout:
    editing/pulling the repo you develop in must never touch a live
    deployment. We git-clone the engine into an install dir and every later
    step (overlay, services, MCP paths) points at that clone. Updating the
    live install is an explicit act: scripts/update.sh in the install dir.
    """
    global ENGINE_ROOT
    header("Step 2: Install Location")
    info("The service runs from a dedicated engine clone, separate from this")
    info("checkout — so editing this repo never touches the live deployment.")
    print()

    default_dir = str(Path.home() / "kbots") if sys.platform == "darwin" else "/opt/kbots"
    install_input = ask("Engine install directory", default_dir)
    install_dir = Path(install_input).expanduser().resolve()

    if install_dir == PROJECT_ROOT:
        warn("Installing in place — this checkout IS the live engine.")
        warn("Edits and pulls here affect the running service directly.")
        ENGINE_ROOT = PROJECT_ROOT
        state["engine_root"] = ENGINE_ROOT
        return

    if (install_dir / ".git").is_dir():
        ok(f"Existing engine clone found: {install_dir}")
    else:
        if install_dir.exists() and any(install_dir.iterdir()):
            err(f"{install_dir} exists and is not an engine clone — choose another directory.")
            sys.exit(1)
        # Create the parent (may need sudo for /opt on Linux)
        parent = install_dir.parent
        if not os.access(parent if parent.exists() else parent.parent, os.W_OK):
            if _check_sudo():
                subprocess.run(["sudo", "-n", "mkdir", "-p", str(parent)], check=True)
                subprocess.run(
                    ["sudo", "-n", "chown", getpass.getuser(), str(parent)], check=True
                )
            else:
                err(f"No write access to {parent} and no passwordless sudo.")
                info(f"Create it manually (sudo mkdir -p {parent} && sudo chown $USER {parent}) and re-run.")
                sys.exit(1)
        info(f"Cloning engine to {install_dir} ...")
        try:
            subprocess.run(
                ["git", "clone", "--local", str(PROJECT_ROOT), str(install_dir)],
                check=True, capture_output=True, text=True,
            )
        except subprocess.CalledProcessError as e:
            err(f"Clone failed: {e.stderr.strip()}")
            sys.exit(1)
        # We created this clone this run — remove it entirely on abort.
        _track_path(state, install_dir)
        ok(f"Engine cloned to {install_dir}")
        info(f"Updates: cd {install_dir} && scripts/update.sh  (pulls from this checkout)")

    # Sync deps inside the install clone so its .venv exists
    info("Installing engine dependencies in the install directory...")
    subprocess.run(["uv", "sync"], cwd=str(install_dir), check=False)

    ENGINE_ROOT = install_dir
    state["engine_root"] = ENGINE_ROOT
    ok(f"Engine root: {ENGINE_ROOT}")


def step_overlay(state: dict):
    header("Step 3: Deployment Overlay")
    info("The overlay holds your config, agent identities, service files,")
    info("and generated files — separate from the engine code.")
    info("This keeps Core clean for updates.")
    print()

    # Default: sibling of the engine install named <base>-overlay
    parent = ENGINE_ROOT.parent
    base = ENGINE_ROOT.name
    # Strip version suffixes (kbots-v2 -> kbots)
    for suffix in ["-v2", "-v3", "-v4"]:
        base = base.replace(suffix, "")
    default_overlay = str(parent / f"{base}-overlay")

    overlay_input = ask("Overlay directory", default_overlay)
    overlay = Path(overlay_input).resolve()

    # If we're creating the overlay fresh, remove the whole tree on abort.
    # If it already existed, leave it (don't nuke a user's existing data).
    if not overlay.exists():
        _track_path(state, overlay)

    # Create directory structure. tools/ and skills/ exist upfront so the
    # hot-reload watcher registers them at boot (create_tool/create_skill).
    for d in ["config", "agents", "systemd", "tools", "skills",
              "tmp/media", "tmp/docs", "tmp/scratch"]:
        (overlay / d).mkdir(parents=True, exist_ok=True)

    # config/ holds the vault; tmp/ holds agent scratch, fetched media and
    # docs. Owner-only, so other local users can't list key names or read
    # whatever agents drop there. chmod rather than mkdir(mode=...) so a
    # pre-existing overlay gets tightened too.
    for private in ("config", "tmp"):
        try:
            (overlay / private).chmod(0o700)
        except OSError as e:
            warn(f"Could not tighten permissions on {overlay / private}: {e}")

    # .gitignore
    gitignore = overlay / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(
            "# Runtime data\n"
            "agents/*/data/\n"
            "**/MEMORY_CONTEXT.md\n"
            "\n"
            "# Agent-generated files\n"
            "tmp/\n"
            "\n"
            "# Python\n"
            "__pycache__/\n"
            "*.pyc\n"
            "\n"
            "# Claude Code session state\n"
            "/.claude/\n"
            "\n"
            "# Secrets\n"
            "config/secrets.enc\n"
            "config/secrets.salt\n"
            "config/secrets.kdf\n"
        )

    # Add kbots env vars to the shell profile so interactive tools
    # (settings.py etc.) work. Pick the profile the user's shell actually
    # reads: zsh (macOS default) → ~/.zshrc, else ~/.bashrc.
    shell = os.environ.get("SHELL", "")
    if "zsh" in shell or (sys.platform == "darwin" and not shell):
        profile = Path.home() / ".zshrc"
    else:
        profile = Path.home() / ".bashrc"
    content = profile.read_text() if profile.exists() else ""
    if "KBOTS_OVERLAY" not in content:
        env_block = f"\n# kbots environment\nexport KBOTS_OVERLAY={overlay}\n"
        if state.get("kbots_modules"):
            env_block += f"export KBOTS_MODULES={state['kbots_modules']}\n"
        _write_profile_block(profile, env_block)
        _track_undo(state, f"~/{profile.name} env exports",
                    lambda p=profile, b=env_block: _strip_profile_block(p, b))
        ok(f"Added kbots env vars to ~/{profile.name}")
    elif _relocate_profile_block(profile):
        ok(f"Moved kbots env vars above the interactive guard in ~/{profile.name} "
           f"(they were invisible to cron and non-interactive ssh)")
    else:
        info(f"KBOTS_OVERLAY already in ~/{profile.name}")

    ok(f"Overlay: {overlay}")
    state["overlay"] = overlay
    # Export into the wizard's own process too, not just the shell profile:
    # later steps (scaffold_agent → agent_session_dirs) read KBOTS_OVERLAY at
    # call time, and the profile export only reaches new shells. Without this,
    # a first run generated agent sandbox rules pointing at /tmp instead of
    # <overlay>/tmp — an install that only healed when setup was re-run from
    # a fresh shell.
    os.environ["KBOTS_OVERLAY"] = str(overlay)


def step_hooks(state: dict):
    header("Step 4: Git Hooks")

    hooks_src = ENGINE_ROOT / "hooks"
    hooks_dst = ENGINE_ROOT / ".git" / "hooks"

    if not hooks_src.exists() or not hooks_dst.exists():
        info("No hooks to install — skipping")
        return

    installed = 0
    for hook in sorted(hooks_src.iterdir()):
        if not hook.is_file():
            continue
        target = hooks_dst / hook.name
        if target.exists():
            if hook.read_text() != target.read_text():
                if ask_yn(f"Hook '{hook.name}' differs from shipped version. Update?"):
                    shutil.copy2(hook, target)
                    target.chmod(target.stat().st_mode | stat.S_IEXEC)
                    ok(f"Updated hook: {hook.name}")
                    installed += 1
                else:
                    info(f"Hook kept: {hook.name}")
            else:
                info(f"Hook up to date: {hook.name}")
        else:
            shutil.copy2(hook, target)
            target.chmod(target.stat().st_mode | stat.S_IEXEC)
            ok(f"Installed hook: {hook.name}")
            installed += 1

    if installed == 0:
        ok("All hooks up to date")


def step_modules(state: dict):
    header("Step 5: Deployment Pattern & Extension Modules")

    info("kbots supports two deployment patterns:")
    print()
    print(f"  {BOLD}[1] 2-layer{RESET} — Core + Overlay  (single deployment, recommended)")
    info("      All tools/skills live in Core. Overlay holds your config, agents, data.")
    print()
    print(f"  {BOLD}[2] 3-layer{RESET} — Core + Extension Modules + Overlay")
    info("      Use when one extension module set feeds multiple overlays")
    info("      (e.g. shared domain tools across staging/prod or client installs).")
    print()

    # Show core tools
    print(f"  {BOLD}Core tools (always included):{RESET}")
    tools_dir = ENGINE_ROOT / "src" / "tools"
    if tools_dir.exists():
        for py in sorted(tools_dir.glob("*.py")):
            if py.name.startswith("__"):
                continue
            print(f"    {GREEN}✓{RESET} {py.stem}")

    # Show core skills
    skills_dir = ENGINE_ROOT / "skills"
    core_skills = []
    if skills_dir.exists():
        for yml in sorted(list(skills_dir.glob("*.yaml")) + list(skills_dir.glob("*.yml"))):
            core_skills.append(yml.stem)
    if core_skills:
        print(f"\n  {BOLD}Core skills (always included):{RESET}")
        for s in core_skills:
            print(f"    {GREEN}✓{RESET} {s}")

    # Scan for Layer 2 modules up front so we can default the pattern intelligently
    modules_root = None
    for candidate in [ENGINE_ROOT.parent / "kbots-modules", ENGINE_ROOT.parent / "modules"]:
        if candidate.is_dir():
            modules_root = candidate
            break

    # Default to 2-layer unless extension modules are present on disk
    default_pattern = "1" if modules_root is None else "2"
    print()
    pattern = ask("Deployment pattern [1=2-layer, 2=3-layer]", default_pattern)
    state["deployment_pattern"] = "2-layer" if pattern.strip() in ("1", "2-layer") else "3-layer"

    if state["deployment_pattern"] == "2-layer":
        print()
        ok("2-layer deployment: Core + Overlay only")
        state["kbots_modules"] = ""
        state["selected_modules"] = []
        return

    if not modules_root:
        print()
        modules_input = ask("Path to extension modules (leave blank to skip)")
        if modules_input:
            p = Path(modules_input).resolve()
            if p.is_dir():
                modules_root = p

    if not modules_root or not modules_root.is_dir():
        print()
        warn("3-layer selected but no extension module directory found — falling back to 2-layer")
        state["deployment_pattern"] = "2-layer"
        state["kbots_modules"] = ""
        state["selected_modules"] = []
        return

    # List available modules
    available = []
    print(f"\n  {BOLD}Available extension modules:{RESET}")
    idx = 0
    for mod_dir in sorted(modules_root.iterdir()):
        if not mod_dir.is_dir():
            continue
        idx += 1

        mod_tools = []
        if (mod_dir / "tools").exists():
            mod_tools = [p.stem for p in (mod_dir / "tools").glob("*.py") if not p.name.startswith("__")]

        mod_skills = []
        skills_p = mod_dir / "skills"
        if skills_p.exists():
            mod_skills = [p.stem for p in sorted(list(skills_p.glob("*.yaml")) + list(skills_p.glob("*.yml")))]

        available.append(mod_dir.name)
        print(f"\n    {BOLD}[{idx}] {mod_dir.name}{RESET}")
        if mod_tools:
            print(f"        Tools:  {' '.join(mod_tools)}")
        if mod_skills:
            print(f"        Skills: {' '.join(mod_skills)}")

    if not available:
        ok("No extension modules found")
        state["kbots_modules"] = ""
        state["selected_modules"] = []
        return

    print()
    info("Enter module numbers (comma-separated), 'all', or leave blank to skip.")
    choice = ask("Modules", "")

    selected = []
    if choice.lower() == "all":
        selected = available[:]
    elif choice:
        for c in choice.split(","):
            c = c.strip()
            try:
                i = int(c) - 1
                if 0 <= i < len(available):
                    selected.append(available[i])
            except ValueError:
                pass

    if selected:
        modules_paths = [str(modules_root / mod) for mod in selected]
        state["kbots_modules"] = ":".join(modules_paths)
        state["kbots_modules_root"] = str(modules_root)
        state["selected_modules"] = selected
        # Same reasoning as KBOTS_OVERLAY in step_overlay: later steps in this
        # process must see it, not just future shells.
        os.environ["KBOTS_MODULES"] = state["kbots_modules"]
        print()
        for mod in selected:
            ok(f"Module: {mod}")
    else:
        state["kbots_modules"] = ""
        state["selected_modules"] = []
        ok("No extension modules selected (core tools only)")


def _offer_kdf_upgrade(vault: FernetVault, passphrase: str) -> None:
    """Offer to rekey a vault still on legacy KDF parameters.

    Without this, a vault created before the 600k-iteration default stayed at
    100k (and possibly the shared legacy salt) through every setup re-run —
    the wizard early-returns on a working key file, so there was no moment the
    upgrade could ever happen.
    """
    if vault.kdf_current():
        return
    info("This vault still uses legacy key-derivation parameters (weaker "
         "against brute force if the files ever leak).")
    if ask_yn("Upgrade it now? (re-encrypts in place, same passphrase)",
              default=True):
        changed = vault.rekey(passphrase)
        for what, detail in changed.items():
            ok(f"Vault {what}: {detail}")


def step_vault(state: dict):
    header("Step 6: Vault Setup")
    info("The vault encrypts your API tokens and secrets at rest.")
    info("You'll set a passphrase that's needed to unlock the vault at startup.")

    overlay: Path = state["overlay"]
    vault_path = overlay / "config" / "secrets.enc"
    key_file = resolve_vault_key_file()
    vault = FernetVault(str(vault_path))

    # The key file lives outside the overlay (~/.config) — track it for cleanup
    # if we're about to create it fresh. The vault itself is inside the overlay.
    if not key_file.exists():
        _track_path(state, key_file)
    if not vault_path.exists():
        _track_path(state, vault_path)
        _track_path(state, vault_path.with_suffix(".salt"))
        # .kdf records the KDF iteration count. An orphan from an aborted run
        # poisons the next vault: it would be read as authoritative for a
        # vault created with different parameters.
        _track_path(state, vault_path.with_suffix(".kdf"))

    if vault_path.exists():
        info("Existing vault found.")
        # Try key file first
        if key_file.exists():
            try:
                passphrase = key_file.read_text().strip()
                vault.unlock(passphrase)
                ok(f"Vault unlocked ({len(vault.list_keys())} secrets)")
                _offer_kdf_upgrade(vault, passphrase)
                state["vault"] = vault
                state["vault_existed"] = True
                return
            except ValueError:
                warn("Key file passphrase didn't work.")

        for attempt in range(3):
            passphrase = ask_secret("Enter vault passphrase")
            try:
                vault.unlock(passphrase)
                ok(f"Vault unlocked ({len(vault.list_keys())} secrets)")
                _offer_kdf_upgrade(vault, passphrase)
                if ask_yn("Save passphrase to key file for auto-unlock?"):
                    write_private_file(key_file, passphrase + "\n")
                    ok(f"Key file saved to {key_file}")
                state["vault"] = vault
                state["vault_existed"] = True
                return
            except ValueError:
                err("Wrong passphrase.")

        err("Failed to unlock vault after 3 attempts.")
        sys.exit(1)

    else:
        while True:
            passphrase = ask_secret(
                "Create a vault passphrase (Enter to generate one)")
            if not passphrase:
                passphrase = py_secrets.token_urlsafe(24)
                print(f"  Generated passphrase: {BOLD}{passphrase}{RESET}")
                info("Shown only once — store it in a password manager. It is the")
                info("only way to unlock the vault if the key file is ever lost.")
                break
            if len(passphrase) < MIN_PASSPHRASE_LEN:
                err(f"Passphrase must be at least {MIN_PASSPHRASE_LEN} characters "
                    f"— or press Enter to generate a strong one.")
                continue
            confirm = ask_secret("Confirm passphrase")
            if passphrase != confirm:
                err("Passphrases don't match.")
                continue
            break

        vault.unlock(passphrase)
        # Persist empty vault to create the file
        vault.set("_setup", "true")
        vault.delete("_setup")
        ok("Vault created")

        # The key file trades encryption-at-rest for unattended start: the
        # service reads the passphrase from it at boot, and so can anyone else
        # who can read the file. That trade is fine for most installs, but it
        # has to be a choice the user saw — SECURITY.md calls any unencrypted
        # secret on disk a vulnerability, and this is the one sanctioned
        # exception.
        print()
        info("The service needs this passphrase at every start. Saving it to a")
        info(f"key file ({key_file}, owner-only 0600) lets the service start")
        info("unattended after a reboot — but anyone who can read that file can")
        info("unlock the vault.")
        if ask_yn("Save passphrase to the key file for unattended start?",
                  default=True):
            write_private_file(key_file, passphrase + "\n")
            ok(f"Key file saved to {key_file}")
        else:
            warn("No key file — the service cannot auto-start after a reboot.")
            warn("Start it in a terminal (it will prompt), or create the key "
                 "file later by re-running setup.")

        state["vault"] = vault
        state["vault_existed"] = False


def step_discord(state: dict):
    header("Step 7: Discord Bot")
    info("kbots connects to Discord via a bot account.")
    info("Create one at: https://discord.com/developers/applications")
    info("Required intents: Message Content, Server Members, Guild Messages")
    info("Tip: README → QUICKSTART Step 1 has a Claude-in-Chrome prompt that")
    info("automates the whole portal setup (except copying the token).")
    print()

    vault: FernetVault = state["vault"]

    # A re-run against an existing vault shouldn't force a re-paste — the
    # token is already stored, and hunting it down again is the most annoying
    # part of a second pass through the wizard.
    token = ""
    if state.get("vault_existed") and vault.get("discord-token"):
        if ask_yn("A Discord bot token is already in the vault. Keep it?",
                  default=True):
            token = vault.get("discord-token")
    if not token:
        token = ask_discord_token("Paste your bot token")
    while True:
        if not token:
            warn("Skipped — add a token later via vault-manage.py, then restart.")
            state["discord_skip"] = True
            return

        info("Validating token...")
        bot_info, reason = validate_discord_token(token)
        if bot_info:
            ok(f"Bot verified: {bot_info.get('username', '?')}#{bot_info.get('discriminator', '0')}")
            break
        if reason == "network":
            # Air-gapped or offline installs can't reach Discord — storing the
            # token unverified is a legitimate choice there. A rejected token
            # is not: keeping it just moves the failure to first boot.
            if ask_yn("Couldn't reach Discord to validate. Store the token "
                      "unverified?", default=False):
                break
        else:
            err("Discord rejected that token — re-copy it from the Developer "
                "Portal (Bot → Reset Token if it was regenerated).")
        token = ask_discord_token("Paste your bot token")

    vault.set("discord-token", token)
    ok("Token stored in vault")

    # Guild ID + your user ID (validated snowflakes)
    info("Tip: enable Discord Developer Mode (Settings → Advanced), then "
         "right-click → Copy ID. Or read the server ID from the URL: "
         "discord.com/channels/<server-id>/<channel-id>.")
    guild_id = ask_id("Server (guild) ID")
    state["guild_id"] = guild_id

    user_id = ask_id("Your Discord user ID (makes you the admin)")
    state["owner_discord_id"] = user_id
    state["discord_skip"] = False

    # Bot account name
    bot_name = ask("Internal name for this bot account", "main")
    state["bot_name"] = bot_name
    state["bot_token_key"] = "discord-token"

    # Manage Channels: the main bot is the setup account — server auto-setup
    # creates the fleet channels through it on guild join.
    print()
    show_invite_link(state, bot_name, token, bot_info, manage_channels=True)


def step_team(state: dict):
    header("Step 8: Team — Owner Profile")
    info("Set up your profile so the system knows who you are.")
    print()

    name = ask("Your name")
    role = ask("Your role", "Founder")
    timezone = ask("Timezone (e.g. Europe/Stockholm, America/New_York, or UTC)", "UTC")
    context = ask("Short context (what you do)")

    discord_id = state.get("owner_discord_id", "")
    if not discord_id:
        discord_id = ask_id("Your Discord user ID")

    state["owner"] = {
        "id": name.lower().replace(" ", "-"),
        "name": name,
        "type": "human",
        "access": "owner",
        "role": role,
        "responsibilities": ["Everything"],
        "context": context,
        "contact": {"discord": discord_id},
        "preferences": {"timezone": timezone},
    }
    state["team_members"] = [state["owner"]]
    ok(f"Owner profile: {name} ({role})")


def step_agent(state: dict):
    header("Step 9: First Agent")
    info("Configure your primary AI agent.")
    print()

    agent_name = ask_agent_name(
        "Agent internal name (used for its folder and config key)", "main")
    display_name = ask("Display name (shown in Discord — capitals fine here)",
                       agent_name.upper())
    description = ask("One-line description", "Primary agent")
    model = ask_choice("LLM model", ["sonnet", "opus"], default="sonnet")

    state["agent"] = {
        "name": agent_name,
        "display_name": display_name,
        "description": description,
        "model": model,
    }

    # Agent personality
    print()
    info("Optionally set a personality for the agent's identity file (AGENTS.md).")
    personality = ask("Personality (e.g., 'concise and direct', 'friendly and detailed')", "concise and direct")
    state["agent"]["personality"] = personality

    # Routing
    print()
    bot_name = state.get("bot_name", "main")
    state["agent"]["routing"] = _ask_routing(bot_name)

    ok(f"Agent: {display_name} ({model})")


def step_full_control(state: dict):
    header("Step 10: Main Agent — Machine Control")
    info("By default the main agent has NO shell access — it can read files and")
    info("use tools, but cannot run commands or edit the system.")
    print()
    info("You can give it full control of this machine:")
    info(f"  {BOLD}none{RESET}  — default: read + tools only, no shell (safest)")
    info(f"  {BOLD}user{RESET}  — full shell + file access as your account (no sudo)")
    info(f"  {BOLD}root{RESET}  — everything, PLUS passwordless sudo (can act as root)")
    print()
    warn("Full control means the agent can run anything you can. Only enable it")
    warn("on a machine you trust to your agent.")
    print()

    level = ask_choice(
        "Control level", ["none", "user", "root"], default="none"
    )
    state["main_full_control"] = level

    if level == "none":
        ok("Main agent: read + tools only (no shell)")
        return

    # user/root → the main agent is scaffolded as a privileged agent
    state["agent"]["full_control"] = True
    ok(f"Main agent will have {BOLD}{level}-level{RESET} control (privileged, full shell)")

    if level == "root":
        print()
        info("Installing passwordless sudo (you'll be asked for your password once)...")
        script = ENGINE_ROOT / "scripts" / "full-control.sh"
        if script.exists():
            rc = subprocess.run(["bash", str(script), "grant"]).returncode
            if rc == 0:
                _track_undo(state, "passwordless sudo rule",
                            lambda s=script: subprocess.run(["bash", str(s), "revoke"]))
                ok("Passwordless sudo granted — the agent can act as root")
                info("Revoke any time: scripts/full-control.sh revoke")
            else:
                err("Could not install sudo rule — agent has user-level control only")
                info(f"Grant later: bash {script} grant")
        else:
            warn(f"Script not found: {script} — grant sudo manually later")


def step_hitl(state: dict):
    header("Step 11: Human-in-the-Loop Approval")
    info("Some tools require human approval before executing.")
    info("Approvals are sent to a Discord channel as reaction prompts.")
    print()

    default_gated = ["send_email", "install_mcp", "create_agent",
                     "create_tool", "promote_tool", "create_trigger", "delete_trigger"]

    # Whether the approval gate starts on. Toggle live later with /admin hitl.
    enabled = True
    if state.get("main_full_control", "none") != "none":
        info("Your main agent has full machine control.")
        info("The approval gate makes it ask you before sensitive/destructive actions.")
        enabled = ask_yn("Keep the human-approval gate ON? (recommended)", default=True)
        if not enabled:
            warn("Approval gate OFF — the agent acts fully autonomously.")
        info("You can flip this any time in Discord: /admin hitl on|off")
        print()

    if state.get("discord_skip"):
        warn("Discord not configured — using defaults for HITL.")
        state["hitl"] = {
            "channel": "",
            "approvers": [],
            "gated_tools": default_gated,
            "enabled": enabled,
        }
        return

    # ask_id, not ask: a typo here would silently point the approval gate at a
    # nonexistent channel (fail-closed, so every gated action would hang).
    channel_id = ask_id("Discord channel ID for approvals (ops/alerts channel)")
    approvers = [state.get("owner_discord_id", "")]
    approvers = [a for a in approvers if a]

    if ask_yn("Add more approvers?", default=False):
        for uid in ask_ids("approver user"):
            if uid not in approvers:
                approvers.append(uid)

    print()
    info("Default gated tools: send_email, install_mcp, create_agent, create_tool, "
         "promote_tool, create_trigger")
    gated = list(default_gated)
    if ask_yn("Add more gated tools?", default=False):
        while True:
            tool = ask("Tool name (Enter when done)")
            if not tool:
                break
            if tool not in gated:
                gated.append(tool)

    state["hitl"] = {
        "channel": channel_id,
        "approvers": approvers,
        "gated_tools": gated,
        "enabled": enabled,
    }
    ok(f"HITL: {'ON' if enabled else 'OFF'}, {len(approvers)} approver(s), "
       f"{len(gated)} gated tool(s)")


def step_compression(state: dict):
    header("Step 12: Context Compression (optional)")
    info("kbots can compress verbose context files (codex docs, skill prompts)")
    info("to reduce input tokens per agent message. Rule-based, no LLM calls.")
    info("Agent identity files (AGENTS.md) are excluded — carefully tuned prompts.")
    print()

    compression = {"enabled": False, "level": "standard"}

    if ask_yn("Enable context compression?", default=False):
        compression["enabled"] = True
        compression["level"] = ask_choice(
            "Compression level",
            ["lite", "standard"],
            default="standard",
        )
        ok(f"Context compression enabled ({compression['level']})")

        print()
        info("Memory recall compression strips filler from memories before")
        info("injecting them into agent context. Same rules, applied at runtime.")
        print()
        if ask_yn("Enable memory recall compression?", default=False):
            compression["memory_recall"] = True
            ok("Memory recall compression enabled")
        else:
            compression["memory_recall"] = False
    else:
        info("Skipped — can be enabled later in config.yaml")

    state["compression"] = compression


# --- Local models (Ollama / LM Studio) ---

def _detect_ram_gb() -> int:
    """Total system RAM in GB (0 if undetectable)."""
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["sysctl", "-n", "hw.memsize"],
                                 capture_output=True, text=True, timeout=5)
            return round(int(out.stdout.strip()) / (1024 ** 3))
        with open("/proc/meminfo") as f:
            kb = int(next(ln for ln in f if ln.startswith("MemTotal")).split()[1])
            return round(kb / (1024 ** 2))
    except Exception:
        return 0


def _probe_local_runtime() -> str | None:
    """Name of the running local-model runtime, if any."""
    for name, url in (("Ollama", "http://localhost:11434/v1/models"),
                      ("LM Studio", "http://localhost:1234/v1/models")):
        try:
            urllib.request.urlopen(url, timeout=2)
            return name
        except Exception:
            continue
    return None


def _local_model_tiers(ram_gb: int) -> tuple[list[tuple[str, str, str]], int]:
    """(tiers, recommended 1-based index) for the detected RAM.

    Each tier: (label, router_model, workhorse_model). Models verified July 2026
    (official Ollama library, all with tools support) — see docs/LOCAL_MODELS.md.
    """
    is_mac = sys.platform == "darwin"
    tiers = [
        ("Tiny — 8GB (VPS/CPU): granite4.1:3b router + qwen3.5:4b workhorse",
         "granite4.1:3b", "qwen3.5:4b"),
        ("Small — 16GB: qwen3.5:2b router + qwen3.5:9b workhorse",
         "qwen3.5:2b", "qwen3.5:9b"),
        ("Big — 32GB+: qwen3.5:4b router + qwen3.6:35b-a3b workhorse (MoE)",
         "qwen3.5:4b", "qwen3.6:35b-a3b"),
    ]
    if ram_gb and ram_gb < 12:
        rec = 1
    elif ram_gb < 28:
        rec = 2
    else:
        rec = 3
    if not ram_gb:
        rec = 2 if is_mac else 1
    return tiers, rec


def step_local_models(state: dict):
    header("Step 12b: Local Models (optional)")
    info("Run simple requests on a LOCAL model (via Ollama or LM Studio) instead")
    info("of Claude — a tiny router model classifies each message and only")
    info("clearly-simple ones stay local (quality-first), saving Claude usage.")
    print()

    ram = _detect_ram_gb()
    runtime = _probe_local_runtime()
    if ram:
        info(f"Detected: {ram}GB RAM"
             + (f", {runtime} running" if runtime else ", no local runtime running"))
    if not runtime:
        info("Requires Ollama (ollama.com) or LM Studio (lmstudio.ai) — both are")
        info("auto-detected at runtime; you can install one later and re-enable.")
    print()

    if not ask_yn("Enable local models + tier routing?", default=bool(runtime)):
        info("Skipped — enable later: defaults.llm.local + defaults.llm.router "
             "in config.yaml (see docs/LOCAL_MODELS.md)")
        state["local_models"] = {"enabled": False}
        return

    tiers, rec = _local_model_tiers(ram)
    labels = [t[0] + ("   ← recommended for this machine" if i + 1 == rec else "")
              for i, t in enumerate(tiers)]
    choice = ask_menu("Model tier", labels, default=rec)
    _, router_model, local_model = tiers[choice - 1]
    state["local_models"] = {"enabled": True, "router_model": router_model,
                             "local_model": local_model}
    ok(f"Local models: router={router_model}, workhorse={local_model}")

    if shutil.which("ollama") and ask_yn("Pull these models with ollama now?", default=True):
        for m in (router_model, local_model):
            info(f"ollama pull {m} (this can take a while)…")
            try:
                subprocess.run(["ollama", "pull", m], timeout=1800)
            except Exception as e:
                warn(f"pull failed for {m}: {e} — run 'ollama pull {m}' manually")


# --- Training-data collection ---

def step_training(state: dict):
    header("Step 12c: Training-Data Collection (optional)")
    info("kbots can record every agent turn — prompt, tool-call trace, response,")
    info("and your 👍/👎 reactions — into a local dataset you can later use to")
    info("fine-tune a small local model on YOUR tools (see docs/TRAINING.md).")
    info("Everything stays on this machine; secrets are redacted. Off by default")
    info("because it stores full conversation content under <data_dir>/training/.")
    print()

    training = {"enabled": False, "include_tool_trace": True}
    if ask_yn("Enable training-data collection?", default=False):
        training["enabled"] = True
        ok("Training-data collection enabled → <data_dir>/training/")
        info("React 👍/👎 on agent replies — that's what labels the dataset.")
    else:
        info("Skipped — enable later: kbots.training_collection in config.yaml")

    state["training_collection"] = training


# Optional dependency groups from pyproject.toml that a deployment can enable.
# sync.sh reads <overlay>/extras and passes each name as `uv sync --extra` on
# every sync, so a choice made here survives updates.
_PY_EXTRAS = [
    ("embeddings", "semantic memory recall (local embeddings) — recommended"),
    ("data", "data-analysis stack (pandas, matplotlib, scipy, scikit-learn)"),
    ("reports", "PDF / report generation (fpdf2, markdown)"),
    ("search", "Tavily web search tool"),
    ("api", "HTTP API + webhook server (FastAPI)"),
    ("design", "PowerPoint generation (python-pptx)"),
    ("graph", "graph memory backend"),
    ("stagehand", "AI-driven browser automation"),
]


def step_pyextras(state: dict):
    header("Step 13: Optional Features")
    overlay: Path = state["overlay"]
    extras_file = overlay / "extras"

    current: list[str] = []
    if extras_file.exists():
        current = extras_file.read_text().replace(",", " ").split()

    info("Optional feature sets (Python extras). The final dependency sync in")
    info("this run installs them, and every later sync keeps them installed.")
    print()
    for i, (name, desc) in enumerate(_PY_EXTRAS, 1):
        mark = f"{GREEN}✓{RESET}" if name in current else " "
        print(f"    [{i}] {mark} {name} — {desc}")
    print()
    info("Enter numbers (comma-separated), 'all', or blank to keep as is.")
    choice = ask("Extras", "")

    if choice:
        if choice.lower() == "all":
            selected = [n for n, _ in _PY_EXTRAS]
        else:
            selected = []
            for c in choice.split(","):
                try:
                    i = int(c.strip()) - 1
                    if 0 <= i < len(_PY_EXTRAS):
                        selected.append(_PY_EXTRAS[i][0])
                except ValueError:
                    pass
        extras_file.write_text("\n".join(selected) + ("\n" if selected else ""))
        ok(f"Extras: {' '.join(selected) or 'none'} (saved to {_display(extras_file, overlay)})")
    else:
        ok(f"Extras unchanged: {' '.join(current) or 'none'}")

    info("Vendor integrations (google, trello, notion, github, …) are separate —")
    info("they live in extras/ in the engine repo and install by copying into")
    info("the overlay. See extras/README.md.")


def step_generate(state: dict):
    header("Step 13: Generating Config Files")

    overlay: Path = state["overlay"]
    agent = state["agent"]
    owner = state["owner"]
    hitl = state["hitl"]
    bot_name = state.get("bot_name", "main")
    guild_id = state.get("guild_id", "YOUR_GUILD_ID")
    owner_discord = owner["contact"]["discord"] or "YOUR_DISCORD_USER_ID"
    agent_dir = overlay / "agents" / agent["name"]

    # --- config.yaml ---
    # Only wire up the Discord account if its token was actually captured —
    # otherwise the service crash-loops at boot on a missing vault secret
    # (preflight is fail-closed). If skipped, ship discord disabled with an
    # example account so it's easy to fill in later.
    discord_ok = not state.get("discord_skip")
    if discord_ok:
        discord_cfg = {
            "enabled": True,
            "guild_id": guild_id if guild_id and guild_id != "YOUR_GUILD_ID" else "",
            "accounts": {
                bot_name: {"token_key": state.get("bot_token_key", "discord-token")},
            },
        }
    else:
        warn("Discord token wasn't set — writing config with Discord DISABLED.")
        info("Add a token later:  uv run python vault-manage.py  (key: discord-token)")
        info("then set connectors.discord.enabled: true and restart.")
        discord_cfg = {
            "enabled": False,
            "guild_id": "",
            "accounts": {"main": {"token_key": "discord-token"}},
        }
    config = {
        "kbots": {
            "name": "kbots", "log_level": "info", "data_dir": "./data",
            # Always written (even when disabled) so the knob is discoverable
            "training_collection": state.get(
                "training_collection", {"enabled": False, "include_tool_trace": True}),
        },
        "connectors": {
            "discord": discord_cfg,
            "http": {"enabled": False, "port": 8080},
        },
        "defaults": {
            "llm": {"provider": "claude_code", "model": agent["model"], "max_tokens": 4096},
            "session": {"max_history": 50, "summarize_after": 30},
            "memory": {"backend": "sqlite", "semantic_search": False, "decay_enabled": False, "max_results": 10},
        },
        "security": {
            "hitl": {
                "connector": "discord",
                "enabled": hitl.get("enabled", True),
                "channel": hitl["channel"],
                "approvers": hitl["approvers"],
                "timeout": 1800,
                "fail_mode": "closed",
                "poll_interval": 3,
                "gated_tools": hitl["gated_tools"],
            },
            "rate_limits": {"mode": "log", "defaults": {"max_per_hour": 100}},
            "compression": state.get("compression", {"enabled": False, "level": "standard"}),
        },
        "admin_users": {"discord": [owner_discord]},
    }

    # Local models + tier routing (from step_local_models). Router config in
    # defaults.llm applies to all agents; per-agent llm.router overrides it.
    lm = state.get("local_models") or {}
    if lm.get("enabled"):
        config["defaults"]["llm"]["local"] = {"model": lm["local_model"], "timeout": 180}
        config["defaults"]["llm"]["router"] = {
            "enabled": True,
            "router_model": lm["router_model"],
            "local_model": lm["local_model"],
            "confidence": 0.75,
        }

    _write_yaml(overlay / "config" / "config.yaml", config, state)

    # --- team.json ---
    team = {"humans": state["team_members"], "agents": []}
    _write_json(overlay / "config" / "team.json", team, state)

    # --- mcp.yaml ---
    mcp_yaml_path = overlay / "config" / "mcp.yaml"
    if not mcp_yaml_path.exists():
        mcp_config = {
            "servers": {
                "kbots-tools": {
                    "transport": "stdio",
                    "command": str(ENGINE_ROOT / ".venv" / "bin" / "python3"),
                    "args": ["-m", "src.mcp_server"],
                    "cwd": str(ENGINE_ROOT),
                }
            }
        }
        mcp_yaml_path.write_text(yaml.dump(mcp_config, default_flow_style=False, sort_keys=False))
        ok(f"Created {_display(mcp_yaml_path)}")

    # --- agents.yaml + agent directory (shared scaffolding) ---
    # Full-control main agent → privileged (full shell); else coordinator.
    main_tier = "privileged" if agent.get("full_control") else "coordinator"
    written = scaffold_agent(
        overlay,
        agent["name"],
        agent["display_name"],
        agent["description"],
        model=agent["model"],
        tier=main_tier,
        personality=agent.get("personality", ""),
        routing=agent["routing"],
        bot_account=bot_name,
        engine_root=ENGINE_ROOT,
        kbots_modules=state.get("kbots_modules", ""),
        exist_ok=True,
    )
    for p in written:
        ok(f"Created {_display(p, overlay)}")
    ok(f"Agent directory: {_display(agent_dir)}")

    # Neutralise Core remote — prevent accidental pushes to the upstream repo
    _neutralise_core_remote()


def step_ops_instance(state: dict):
    header("Step 14: Privileged Ops Instance (optional)")
    info("kbots supports a dual-instance deployment pattern:")
    info("  Main instance  — sandboxed, runs your user-facing agents")
    info("  Ops instance   — unsandboxed, single privileged agent for dev/ops")
    info("")
    info("The ops agent can edit code, restart services, and run deploys.")
    info("Only the system owner should have access to it.")
    info("See ARCHITECTURE.md for full details.")
    print()
    info("Advanced & optional — if you're just getting started, choose No. "
         "You can add this later with scripts/settings.py.")

    if not ask_yn("Set up a privileged ops instance?", default=False):
        info("Skipped — you can set this up later via scripts/settings.py")
        return

    overlay: Path = state["overlay"]
    vault: FernetVault = state["vault"]

    agent_name = ask_agent_name(
        "Ops agent internal name (used for its folder and config key)", "engineer")
    display_name = ask("Display name (shown in Discord — capitals fine here)",
                       agent_name.capitalize() + " Bot")
    description = ask("Description", "Privileged ops agent — unsandboxed, owner-only")
    model = ask_choice("Model", ["sonnet", "opus"], default="opus")

    # Bot account
    print()
    info("The ops agent needs its own Discord bot account.")
    bot_name = ask("Bot account name", agent_name)
    token = ask_discord_token(f"Discord bot token for '{bot_name}'")

    while token:
        info("Validating token...")
        bot_info, reason = validate_discord_token(token)
        if bot_info:
            ok(f"Bot verified: {bot_info.get('username', '?')}")
            break
        if reason == "network":
            if ask_yn("Couldn't reach Discord to validate. Store the token "
                      "unverified?", default=False):
                break
        else:
            err("Discord rejected that token — re-copy it from the Developer "
                "Portal.")
        token = ask_discord_token(f"Discord bot token for '{bot_name}'")

    if token:
        vault_key = f"discord-{bot_name}"
        vault.set(vault_key, token)
        ok(f"Token stored as '{vault_key}'")
        state.setdefault("extra_bots", {})[bot_name] = vault_key
        show_invite_link(state, bot_name, token, bot_info)
    else:
        warn("No token — add it later via vault-manage.py")

    # Routing
    routing = _ask_routing(bot_name, default_mentions=True)

    agent_dir = overlay / "agents" / agent_name

    # Add bot account to main config.yaml
    if token:
        config_path = overlay / "config" / "config.yaml"
        if config_path.exists():
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            dc = cfg.setdefault("connectors", {}).setdefault("discord", {})
            # This bot has a valid token, so Discord can run even if the main
            # token was skipped earlier.
            dc["enabled"] = True
            dc.setdefault("accounts", {})[bot_name] = {"token_key": f"discord-{bot_name}"}
            # If the main account has no token in the vault, drop it so preflight
            # doesn't fail-closed on the missing secret.
            if state.get("discord_skip"):
                dc.get("accounts", {}).pop("main", None)
            config_path.write_text(yaml.dump(cfg, default_flow_style=False, sort_keys=False))
            ok(f"Added bot '{bot_name}' to config.yaml")

    # Scaffold the ops agent's identity from the platform-matching template:
    # Linux gets the separate unsandboxed rescue instance, macOS gets a
    # main-instance ops agent (no systemd sandbox to escape from).
    template_name = "ops-claude-macos.md" if sys.platform == "darwin" else "rescue-claude.md"
    template_path = ENGINE_ROOT / "config" / "templates" / template_name
    if template_path.exists():
        claude_md = template_path.read_text().format(
            display_name=display_name, agent_dir=agent_dir, engine_root=ENGINE_ROOT)
    else:
        claude_md = f"# {display_name}\n\nYou are {display_name} — the ops and dev agent.\n"
        warn(f"Template not found at config/templates/{template_name} — using minimal fallback")

    # The separate "rescue" profile exists for Linux sandbox isolation (the
    # main service runs sandboxed; the ops agent needs its own unsandboxed
    # service). macOS/launchd has no sandbox, so a second service is pointless
    # and would leave the ops bot connected-but-unserved — put the ops agent in
    # the MAIN instance so the single service actually handles it.
    if sys.platform == "darwin":
        ops_agents_file, ops_profile = "agents.yaml", ""
        info("macOS: adding the ops agent to the main instance (no separate "
             "sandboxed service needed).")
    else:
        ops_agents_file, ops_profile = "agents.rescue.yaml", "rescue"

    written = scaffold_agent(
        overlay,
        agent_name,
        display_name,
        description,
        model=model,
        tier="privileged",
        routing=routing,
        bot_account=bot_name,
        engine_root=ENGINE_ROOT,
        kbots_modules=state.get("kbots_modules", ""),
        profile=ops_profile,
        agents_file=ops_agents_file,
        claude_md=claude_md,
        exist_ok=True,
    )
    for p in written:
        ok(f"Created {_display(p, overlay)}")

    print()
    ok(f"Ops instance configured — agent: {display_name}")
    if sys.platform == "darwin":
        info("The ops agent runs inside the main service — nothing more to install.")
    else:
        # The wizard's systemd step installs only the MAIN unit; promising the
        # rescue service "in a later step" left it configured and never served.
        # Say what actually has to happen instead.
        info("The rescue instance needs its own (unsandboxed) service unit,")
        info("which the wizard does not install. After setup finishes:")
        info(f"  sudo cp {ENGINE_ROOT}/config/kbots-rescue.service /etc/systemd/system/")
        info("  sudo systemctl daemon-reload")
        info("  sudo systemctl enable --now kbots-rescue.service")
        info(f"Check the unit's paths first if the engine is not at /opt/kbots "
             f"(yours: {ENGINE_ROOT}).")


def step_extras(state: dict):
    header("Step 15: Additional Setup (optional)")

    info("Further agents are created by your MAIN AGENT after setup:")
    info("ask it to create an agent, approve via HITL, then restart the service.")

    while True:
        print()
        print("  What would you like to add?")
        print("    1) Another team member")
        print("    2) Another Discord bot")
        print("    3) Done — finish setup")
        print()
        choice = ask("Choice", "3")

        if choice in ("3", "done", "d", ""):
            break
        elif choice in ("1", "team"):
            _add_team_member(state)
        elif choice in ("2", "bot"):
            _add_bot(state)


def service_account_home(unit_text: str) -> Path:
    """The home directory of the account the unit runs as.

    `%h` in a SYSTEM unit is the service manager's home, which is root's, not
    the `User=`'s. The template ships `Environment=HOME=%h` with a comment
    saying it is rewritten at install time, and nothing rewrote it: a fresh
    Debian install failed with `failed to create directory /root/.cache/uv:
    Permission denied` and exited 2 before doing anything. Worse than the crash
    is the near miss, since the same %h would send Claude Code to
    /root/.claude/.credentials.json, where an authenticated service account
    reads as unauthenticated.

    Resolved from the unit's own User= rather than from whoever runs setup,
    because setup is routinely run under sudo.
    """
    import pwd

    for line in unit_text.splitlines():
        if line.startswith("User="):
            user = line.split("=", 1)[1].strip()
            if user:
                try:
                    return Path(pwd.getpwnam(user).pw_dir)
                except KeyError:
                    # The account is created by install-systemd.sh, which may
                    # not have run yet. Debian's adduser --system default.
                    return Path("/home") / user
    return Path.home()


def service_writable_dirs(overlay: Path) -> list[Path]:
    """Directories the unit grants ReadWritePaths and something must create.

    /tmp and the home dotfile dirs are excluded: the first always exists, and
    the second belong to an account setup may not be able to write into.
    """
    return [ENGINE_ROOT / "data", overlay / "agents", overlay / "config",
            overlay / "data", overlay / "tmp", overlay / "tools",
            overlay / "skills"]


def render_service_unit(unit_text: str, overlay: Path, env_lines: list[str]) -> str:
    """Fill the template's install-time placeholders.

    ReadWritePaths gains tools/ and skills/: create_tool and create_skill write
    a .py or .yaml into them and tool_scope keeps its sidecar in tools/, so
    without them every agent-authored capability fails on Linux while working
    perfectly on the developer's Mac, which has no sandbox.
    """
    home = service_account_home(unit_text)
    out = []
    for line in unit_text.split("\n"):
        if line.startswith("Environment=HOME="):
            out.append(f"Environment=HOME={home}")
        elif line.startswith("Environment=PATH="):
            out.append(f"Environment=PATH={home}/.local/bin:/usr/local/bin:/usr/bin:/bin")
            out.extend(env_lines)
        elif line.startswith("ReadWritePaths="):
            paths = " ".join(str(d) for d in service_writable_dirs(overlay))
            # ~/.claude.json is a FILE at the root of a read-only home.
            # ProtectHome=read-only makes $HOME read-only inside the namespace
            # and granting the .claude DIRECTORY does not reach a file beside
            # it. Claude Code writes that file to mark a workspace trusted, so
            # without this the trust never persists and every agent gets
            # "unapproved-permission" on every tool: the whole install is up,
            # connected, and unable to do anything.
            #
            # The `-` prefix makes systemd tolerate it being absent. Without it
            # a fresh install where Claude Code has not run yet fails at step
            # NAMESPACE and crash-loops, which is the exact failure the
            # directory creation above exists to prevent, reintroduced through
            # a path setup cannot create on another account's behalf.
            out.append(f"ReadWritePaths={paths} /tmp {home}/.cache {home}/.claude "
                       f"-{home}/.claude.json")
        elif line.startswith("ReadOnlyPaths="):
            out.append(f"ReadOnlyPaths={ENGINE_ROOT} {overlay}")
        else:
            out.append(line)
    return "\n".join(out)


def step_systemd(state: dict):
    header("Step 16: Systemd Services")

    if not shutil.which("systemctl"):
        info("systemd not available — skipping")
        return

    overlay: Path = state["overlay"]
    has_sudo = _check_sudo()

    if not has_sudo:
        warn("sudo not available — service files will be generated but not installed")
        info("You'll need to symlink them to /etc/systemd/system/ manually")
        print()

    # --- Generate timer unit files (required for memory decay, health, integrity) ---
    timers_dir = ENGINE_ROOT / "config" / "timers"
    if timers_dir.exists():
        info("Generating maintenance timer units...")
        for f in sorted(timers_dir.iterdir()):
            if not f.is_file():
                continue

            # Templates carry literal /opt/kbots; rewrite only for non-default installs.
            # Keeping literals means vanilla Core installs with symlinked units
            # (the default from install-systemd.sh) survive `git pull` unscathed.
            content = f.read_text()
            if str(ENGINE_ROOT) != "/opt/kbots":
                content = re.sub(r"/opt/kbots(?=/|$)", str(ENGINE_ROOT), content)

            # Inject KBOTS_OVERLAY into service files (after KBOTS_HOME line)
            if f.name.endswith(".service"):
                lines = content.split("\n")
                new_lines = []
                for line in lines:
                    new_lines.append(line)
                    if line.startswith("Environment=KBOTS_HOME="):
                        new_lines.append(f"Environment=KBOTS_OVERLAY={overlay}")
                content = "\n".join(new_lines)

            out_path = overlay / "systemd" / f.name
            out_path.write_text(content)

        ok(f"Timer units written to {_display(overlay / 'systemd')}")

    # --- Generate main service unit file ---
    install_service = False
    print()
    if ask_yn("Install systemd service? (auto-start on boot)"):
        install_service = True
        uv_path = shutil.which("uv") or f"{Path.home()}/.local/bin/uv"
        template_path = ENGINE_ROOT / "config" / "kbots.service"

        if not template_path.exists():
            err(f"Service template not found: {template_path}")
            return

        service = template_path.read_text()

        # Rewrite /opt/kbots only for non-default installs; vanilla paths pass through.
        if str(ENGINE_ROOT) != "/opt/kbots":
            service = re.sub(r"/opt/kbots(?=/|$)", str(ENGINE_ROOT), service)
        service = service.replace(
            "ExecStart=/usr/local/bin/uv",
            f"ExecStart={uv_path}",
        )

        # Inject environment variables after the PATH line
        env_lines = [f"Environment=KBOTS_OVERLAY={overlay}"]
        if state.get("kbots_modules"):
            env_lines.append(f"Environment=KBOTS_MODULES={state['kbots_modules']}")

        service = render_service_unit(service, overlay, env_lines)

        # Every writable path must exist before the unit starts. systemd builds
        # the mount namespace BEFORE exec, so a ReadWritePaths entry that is
        # missing fails at step NAMESPACE with status=226 and a message naming
        # the path rather than the permission, and the service restart-loops
        # without ever reaching the code that would have created it.
        for d in service_writable_dirs(overlay):
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                warn(f"Could not create {d}: {e} — the service may fail at step NAMESPACE")

        out_path = overlay / "systemd" / "kbots.service"
        out_path.write_text(service)
        ok(f"Service file: {_display(out_path)}")

    # --- Install into systemd (bash script handles symlinks, reload, enable) ---
    install_script = ENGINE_ROOT / "scripts" / "install-systemd.sh"
    if has_sudo and install_script.exists():
        info("Installing units into systemd...")
        cmd = ["sudo", "-n", "bash", str(install_script), str(overlay)]
        if install_service:
            cmd.append("--enable-service")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            if result.stdout.strip():
                for line in result.stdout.strip().split("\n"):
                    info(line.strip())

            def _undo_systemd():
                for unit in ("kbots.service",):
                    subprocess.run(["sudo", "-n", "systemctl", "disable", "--now", unit],
                                   capture_output=True)
                    subprocess.run(["sudo", "-n", "rm", "-f",
                                    f"/etc/systemd/system/{unit}"], capture_output=True)
                subprocess.run(["sudo", "-n", "systemctl", "daemon-reload"],
                               capture_output=True)
            _track_undo(state, "systemd service (stop + remove unit)", _undo_systemd)

            ok("Systemd units installed and timers enabled")
            if install_service:
                info("Starting kbots service...")
                start = subprocess.run(
                    ["sudo", "-n", "systemctl", "restart", "kbots"],
                    capture_output=True, text=True,
                )
                if start.returncode == 0:
                    ok("Service started")
                    _verify_agent_online(state, "linux")
                else:
                    err(f"Failed to start service: {start.stderr.strip()}")
                    info("Start manually: sudo systemctl start kbots")
        else:
            err("Systemd installation failed:")
            if result.stderr.strip():
                for line in result.stderr.strip().split("\n"):
                    err(f"  {line}")
    elif not has_sudo:
        warn("No sudo — unit files generated but not installed")
        info(f"Install manually: sudo bash {install_script} {overlay}")
    else:
        warn(f"Install script not found: {install_script}")


def step_launchd(state: dict):
    """macOS parity for step_systemd: generate + install a launchd unit."""
    header("Step 16: launchd Service (macOS)")

    overlay: Path = state["overlay"]
    template_path = ENGINE_ROOT / "config" / "kbots.launchd.plist"
    if not template_path.exists():
        err(f"launchd template not found: {template_path}")
        return

    home = Path.home()
    uv_path = shutil.which("uv") or "/opt/homebrew/bin/uv"
    path_env = f"{home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

    extra_env = ""
    if state.get("kbots_modules"):
        extra_env = (
            "        <key>KBOTS_MODULES</key>\n"
            f"        <string>{state['kbots_modules']}</string>"
        )

    content = (
        template_path.read_text()
        .replace("__UV__", uv_path)
        .replace("__ENGINE_ROOT__", str(ENGINE_ROOT))
        .replace("__PATH__", path_env)
        .replace("__HOME__", str(home))
        .replace("__OVERLAY__", str(overlay))
    )
    if extra_env:
        content = content.replace("        <!-- __EXTRA_ENV__ -->", extra_env)
    else:
        content = content.replace("\n        <!-- __EXTRA_ENV__ -->", "")

    (overlay / "data").mkdir(parents=True, exist_ok=True)
    out_path = overlay / "systemd" / "com.kbots.agent.plist"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content)
    ok(f"launchd unit: {_display(out_path, overlay)}")

    plist_dst = home / "Library" / "LaunchAgents" / "com.kbots.agent.plist"
    print()
    if not ask_yn("Install + start the service now? (auto-start on login)"):
        warn("Skipped — install manually:")
        info(f"  cp {out_path} {plist_dst}")
        info(f"  launchctl bootstrap gui/$(id -u) {plist_dst}")
        return

    plist_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(out_path, plist_dst)
    uid = os.getuid()
    # Re-install cleanly if a previous version is loaded
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}/com.kbots.agent"],
        capture_output=True,
    )

    def _undo_launchd(dst=plist_dst, u=uid):
        subprocess.run(["launchctl", "bootout", f"gui/{u}/com.kbots.agent"],
                       capture_output=True)
        dst.unlink(missing_ok=True)
    _track_undo(state, "launchd service (stop + remove plist)", _undo_launchd)

    result = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(plist_dst)],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        ok("Service installed and started")
        info("Stop:    launchctl bootout gui/$(id -u)/com.kbots.agent")
        info(f"Start:   launchctl bootstrap gui/$(id -u) {plist_dst}")
        info("Restart: launchctl kickstart -k gui/$(id -u)/com.kbots.agent")
        info(f"Logs:    tail -f {overlay}/data/launchd.stderr.log")
        _verify_agent_online(state, "darwin")
    else:
        err(f"launchctl bootstrap failed: {result.stderr.strip()}")
        info(f"Install manually: launchctl bootstrap gui/$(id -u) {plist_dst}")


def _verify_agent_online(state: dict, platform: str, timeout: int = 60):
    """Poll until the service is active and the Discord connector reports ready."""
    import time

    if state.get("discord_skip"):
        warn("Discord not configured — skipping online verification.")
        info("Add a token via vault-manage.py, restart, and mention the bot to test.")
        return

    overlay: Path = state["overlay"]
    agent = state.get("agent", {})
    display_name = agent.get("display_name", "your agent")
    info(f"Waiting for the main agent to come online (up to {timeout}s)...")

    def _logs() -> str:
        if platform == "darwin":
            log_path = overlay / "data" / "launchd.stderr.log"
            try:
                return log_path.read_text()[-20000:]
            except OSError:
                return ""
        result = subprocess.run(
            ["sudo", "-n", "journalctl", "-u", "kbots", "-n", "200", "--no-pager"],
            capture_output=True, text=True,
        )
        return result.stdout if result.returncode == 0 else ""

    def _active() -> bool:
        if platform == "darwin":
            r = subprocess.run(
                ["launchctl", "print", f"gui/{os.getuid()}/com.kbots.agent"],
                capture_output=True,
            )
            return r.returncode == 0
        r = subprocess.run(
            ["systemctl", "is-active", "--quiet", "kbots"], capture_output=True
        )
        return r.returncode == 0

    start_time = time.monotonic()
    while time.monotonic() - start_time < timeout:
        if _active() and "ready as" in _logs():
            print()
            ok(f"{BOLD}MAIN AGENT ONLINE{RESET} — mention @{display_name} in Discord to talk to it")
            return
        time.sleep(2)

    warn("Timed out waiting for the agent to report ready.")
    if platform == "darwin":
        info(f"Check logs: tail -50 {overlay}/data/launchd.stderr.log")
    else:
        info("Check logs: journalctl -u kbots -n 50")
    info("Likely causes: invalid Discord token, missing Message Content intent,")
    info("or Claude Code not authenticated (claude auth login).")


def step_browser(state: dict):
    header("Step 17: Browser Tool (optional)")
    info("The browse_url tool uses a headless Chromium browser via Playwright")
    info("to fetch JavaScript-rendered pages. The Python package is already")
    info("installed; Chromium itself is a separate ~170MB download.")
    print()

    if not ask_yn("Install Chromium for the browse_url tool now?", default=True):
        warn("Skipped. browse_url will return an error until you run:")
        info("    uv run playwright install chromium")
        return

    cmd = ["uv", "run", "playwright", "install", "chromium"]
    try:
        print()
        info("Running: " + " ".join(cmd))
        result = subprocess.run(cmd, check=False)
        if result.returncode == 0:
            ok("Chromium installed — browse_url tool is ready.")
        else:
            err("Chromium install failed (exit " + str(result.returncode) + ").")
            info("Run manually later: uv run playwright install chromium")
    except FileNotFoundError:
        err("'uv' not found on PATH.")
        info("Run manually later: uv run playwright install chromium")


def step_final_sync(state: dict):
    """Re-run sync now that KBOTS_MODULES and KBOTS_OVERLAY are configured."""
    kbots_modules = state.get("kbots_modules", "")
    overlay = state.get("overlay")
    if not overlay and not kbots_modules:
        return  # nothing new to sync

    header("Final Dependency Sync")
    info("Re-syncing with module and overlay configuration...")

    env = {**os.environ}
    if kbots_modules:
        env["KBOTS_MODULES"] = kbots_modules
    if overlay:
        env["KBOTS_OVERLAY"] = str(overlay)

    sync_script = ENGINE_ROOT / "scripts" / "sync.sh"
    if sync_script.exists():
        subprocess.run([str(sync_script)], cwd=str(ENGINE_ROOT), env=env, check=False)
    ok("Dependencies synced with module configuration")


def step_summary(state: dict):
    header("Setup Complete")

    overlay: Path = state["overlay"]
    agent = state["agent"]
    vault: FernetVault = state["vault"]

    print(f"  {BOLD}Core:{RESET}       {ENGINE_ROOT}")
    print(f"  {BOLD}Overlay:{RESET}    {overlay}")
    print(f"  {BOLD}Vault:{RESET}      {len(vault.list_keys())} secret(s)")
    print(f"  {BOLD}Team:{RESET}       {len(state['team_members'])} member(s)")
    print(f"  {BOLD}Agent:{RESET}      {agent['display_name']} ({agent['model']})")

    if state.get("selected_modules"):
        print(f"  {BOLD}Modules:{RESET}    {', '.join(state['selected_modules'])}")

    if state.get("discord_skip"):
        print(f"  {BOLD}Discord:{RESET}    {YELLOW}not configured{RESET} — add token via vault-manage.py")
    else:
        print(f"  {BOLD}Discord:{RESET}    connected (guild: {state.get('guild_id', '?')})")

    if state.get("invite_urls"):
        print(f"\n  {BOLD}Bot install links{RESET} (add a bot to a server any time):")
        for name, url in state["invite_urls"].items():
            print(f"    {name}: {CYAN}{url}{RESET}")

    if sys.platform == "darwin":
        restart_cmd = "launchctl kickstart -k gui/$(id -u)/com.kbots.agent"
        logs_cmd = f"tail -f {overlay}/data/launchd.stderr.log"
        timers_note = (f"  {DIM}Maintenance timers are Linux-only; "
                       f"run scripts/memory-decay.sh manually if needed.{RESET}\n")
    else:
        restart_cmd = "sudo systemctl restart kbots"
        logs_cmd = "journalctl -u kbots -f"
        timers_note = ""

    agent_dir = overlay / "agents" / agent["name"]
    print(f"""
  {BOLD}Generated files:{RESET}
    {overlay}/config/config.yaml
    {overlay}/config/agents.yaml
    {overlay}/config/team.json
    {agent_dir}/AGENTS.md  (+ CLAUDE.md stub)
    {agent_dir}/.mcp.json

  {BOLD}Next steps:{RESET}
    1. Review and customise {agent_dir}/AGENTS.md
    2. Talk to your agent:  mention @{agent['display_name']} in Discord
    3. Logs:                {CYAN}{logs_cmd}{RESET}
    4. Manage secrets:      {CYAN}uv run python vault-manage.py{RESET}
    5. Settings:            {CYAN}uv run python scripts/settings.py{RESET}

  {BOLD}Adding more agents:{RESET}
    Ask your main agent to create them (it has a create_agent tool).
    You approve via HITL in Discord, then restart: {CYAN}{restart_cmd}{RESET}

  {BOLD}Updating the install:{RESET}
    {CYAN}cd {ENGINE_ROOT} && scripts/update.sh{RESET}
    Pulls, syncs deps, hot-reloads tools/skills — restarts only when core code changed.

{timers_note}  {DIM}Config files are in the overlay — the engine stays clean for updates.{RESET}
""")


# ==========================================================================
# Private helpers
# ==========================================================================

def _add_team_member(state: dict):
    overlay: Path = state["overlay"]
    name = ask("Name")
    role = ask("Role")
    discord_id = ask_id("Discord user ID")
    access = ask_choice("Access level", ["owner", "admin", "staff", "viewer"], default="staff")

    member = {
        "id": name.lower().replace(" ", "-"),
        "name": name,
        "type": "human",
        "access": access,
        "role": role,
        "responsibilities": [],
        "context": "",
        "contact": {"discord": discord_id},
        "preferences": {"timezone": "UTC"},
    }
    state["team_members"].append(member)

    # Update team.json
    team = {"humans": state["team_members"], "agents": []}
    team_path = overlay / "config" / "team.json"
    team_path.write_text(json.dumps(team, indent=2) + "\n")
    ok(f"Added {name} ({role}, {access})")


def _add_bot(state: dict):
    overlay: Path = state["overlay"]
    vault: FernetVault = state["vault"]
    bot_name = ask("Bot account name (internal)")

    # Same validation loop as the main bot — a token stored unchecked here
    # would only surface as a connect failure at first boot.
    token = ask_discord_token("Bot token")
    while token:
        info("Validating token...")
        bot_info, reason = validate_discord_token(token)
        if bot_info:
            ok(f"Bot verified: {bot_info.get('username', '?')}")
            break
        if reason == "network":
            if ask_yn("Couldn't reach Discord to validate. Store the token "
                      "unverified?", default=False):
                break
        else:
            err("Discord rejected that token — re-copy it from the Developer "
                "Portal.")
        token = ask_discord_token("Bot token")
    if not token:
        warn("Skipped — no token entered.")
        return

    vault_key = f"discord-{bot_name}"
    vault.set(vault_key, token)
    ok(f"Token stored as '{vault_key}'")
    show_invite_link(state, bot_name, token, bot_info)

    # Add to config.yaml. setdefault, not raw indexing: a config written by an
    # earlier version (or hand-edited) may lack the intermediate keys, and a
    # KeyError here used to abort the wizard and roll back the whole run.
    config_path = overlay / "config" / "config.yaml"
    with open(config_path) as f:
        cfg = yaml.safe_load(f) or {}

    discord_cfg = cfg.setdefault("connectors", {}).setdefault("discord", {})
    discord_cfg.setdefault("accounts", {})[bot_name] = {"token_key": vault_key}
    config_path.write_text(yaml.dump(cfg, default_flow_style=False, sort_keys=False))
    ok(f"Bot '{bot_name}' added to config")
    info("Note: the bot serves nothing until an agent routes to it "
         "(agents.yaml → routing.discord.account).")


# ==========================================================================
# File writers
# ==========================================================================

def _ask_routing(bot_account: str, default_mentions: bool = True) -> dict:
    """Prompt for agent routing config and return the routing dict."""
    mentions_only = ask_yn("Only respond when @mentioned?", default=default_mentions)

    print()
    info("Where should this agent listen? (most people: 1)")
    scope = ask_menu("Choose", [
        "All channels — responds everywhere it can see (simplest)",
        "Specific channels only (by channel ID)",
        "A whole category (all channels in it)",
        "Specific channels + a category",
    ], default=1)

    channels = ask_ids("channel") if scope in (2, 4) else []
    categories = ask_ids("category") if scope in (3, 4) else []

    # routing.discord.guilds (a per-agent server allowlist) is deliberately NOT
    # asked here. The wizard serves single-server installs, where the answer is
    # always "no restriction" — and defaulting it to the install's guild would
    # be worse: the agent would silently ignore any server the bot is invited
    # to later. The router still honors the key for hand-edited agents.yaml.
    users = []
    if ask_yn("Only respond to specific user(s)? (most people: No — team access "
              "tiers already gate who can talk to it)", default=False):
        users = ask_ids("user")

    routing = {
        "discord": {
            "account": bot_account,
            "channels": channels,
            "mentions": mentions_only,
        }
    }
    if categories:
        routing["discord"]["categories"] = categories
    if users:
        routing["discord"]["users"] = users

    return routing


def _confirm_overwrite(path: Path) -> bool:
    """Ask before clobbering an existing config file.

    Keyed on the file's own existence — this used to be gated on whether the
    VAULT pre-existed, so re-running the wizard after the vault was deleted or
    moved silently overwrote config.yaml and team.json.
    """
    if not path.exists():
        return True
    return ask_yn(f"  {_display(path)} exists. Overwrite?", default=False)


def _write_config(path: Path, text: str):
    """config.yaml and team.json hold guild/admin IDs and real names — written
    owner-only, same as the vault files beside them."""
    path.parent.mkdir(parents=True, exist_ok=True)
    write_private_file(path, text)


def _write_yaml(path: Path, data: dict, state: dict):
    if not _confirm_overwrite(path):
        warn(f"Skipped {_display(path)}")
        return
    _write_config(path, yaml.dump(data, default_flow_style=False, sort_keys=False))
    ok(f"Created {_display(path)}")


def _write_json(path: Path, data: dict, state: dict):
    if not _confirm_overwrite(path):
        warn(f"Skipped {_display(path)}")
        return
    _write_config(path, json.dumps(data, indent=2) + "\n")
    ok(f"Created {_display(path)}")


def _display(path: Path, overlay: Path | None = None) -> str:
    """Show path relative to ENGINE_ROOT or overlay for readability."""
    if overlay:
        try:
            return f"<overlay>/{path.relative_to(overlay)}"
        except ValueError:
            pass
    try:
        return str(path.relative_to(ENGINE_ROOT))
    except ValueError:
        return str(path)


def _neutralise_core_remote():
    """Rename origin -> upstream and block push to prevent accidental pushes to the Core repo."""
    try:
        result = subprocess.run(
            ["git", "-C", str(ENGINE_ROOT), "remote", "get-url", "origin"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return  # No origin remote

        subprocess.run(
            ["git", "-C", str(ENGINE_ROOT), "remote", "rename", "origin", "upstream"],
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(ENGINE_ROOT), "remote", "set-url", "--push", "upstream", "no-push"],
            capture_output=True,
        )
        ok("Core remote: origin → upstream (push disabled)")
        info("Pull updates:  git pull upstream main")
        info("Maintainers:   git remote rename upstream origin")
    except FileNotFoundError:
        pass  # git not installed


# ==========================================================================
# Rollback — undo everything the wizard created if it's aborted
# ==========================================================================

def _track_path(state: dict, path: Path) -> None:
    """Mark a file/dir this run created for deletion on abort."""
    state.setdefault("_created_paths", []).append(Path(path))


def _track_undo(state: dict, label: str, fn) -> None:
    """Register a non-file undo action (service stop, sudo revoke, profile edit)."""
    state.setdefault("_undo", []).append((label, fn))


def _strip_profile_block(profile_path: Path, block: str) -> None:
    if profile_path.exists():
        content = profile_path.read_text()
        if block and block in content:
            profile_path.write_text(content.replace(block, ""))


# Debian/Ubuntu ~/.bashrc returns early for non-interactive shells. Anything
# exported below this guard does not exist in cron, in `ssh host '<cmd>'`, or
# in a script-driven `bash -c` — exactly the shells that run kbots tooling
# unattended. The export has to go ABOVE it.
_PROFILE_GUARDS = (
    re.compile(r"^\s*case\s+\$-\s+in"),                        # case $- in *i*) ;; *) return;; esac
    re.compile(r"^\s*\[\s*-z\s+[\"']?\$PS1[\"']?\s*\]\s*&&\s*return"),   # [ -z "$PS1" ] && return
    re.compile(r"^\s*\[\[\s*\$-\s*!=\s*\*i\*\s*\]\]\s*&&\s*return"),     # [[ $- != *i* ]] && return
)


def _guard_index(lines: list[str]) -> int | None:
    """Index of the interactive-shell guard, or None if the profile has none."""
    for i, line in enumerate(lines):
        if any(g.match(line) for g in _PROFILE_GUARDS):
            return i
    return None


def _write_profile_block(profile_path: Path, block: str) -> None:
    """Add `block` to the profile, above the interactive guard if there is one.

    The block is written verbatim so _strip_profile_block still matches it.
    """
    content = profile_path.read_text() if profile_path.exists() else ""
    lines = content.splitlines(keepends=True)
    idx = _guard_index(lines)
    if idx is None:
        with open(profile_path, "a") as f:
            f.write(block)
        return
    lines.insert(idx, block.lstrip("\n"))
    profile_path.write_text("".join(lines))


def _relocate_profile_block(profile_path: Path) -> bool:
    """Move an already-installed kbots export block above the guard.

    Setup's idempotency check is "is KBOTS_OVERLAY in the file", so without
    this every host installed before the fix keeps its export stranded below
    the guard forever and re-running setup silently leaves it there.
    Returns True if anything moved.
    """
    if not profile_path.exists():
        return False
    lines = profile_path.read_text().splitlines(keepends=True)
    idx = _guard_index(lines)
    if idx is None:
        return False
    is_export = re.compile(r"^\s*export\s+KBOTS_(OVERLAY|MODULES)=").match
    moved = [line for line in lines[idx:] if is_export(line)]
    if not moved:
        return False
    kept = [line for i, line in enumerate(lines)
            if not (i >= idx and (is_export(line)
                                  or line.strip() == "# kbots environment"))]
    new_idx = _guard_index(kept)
    if new_idx is None:
        return False
    kept[new_idx:new_idx] = ["# kbots environment\n", *moved, "\n"]
    profile_path.write_text("".join(kept))
    return True


def _rollback(state: dict) -> None:
    """Delete everything this run created. Best-effort; each step is guarded."""
    undos = state.get("_undo", [])
    paths = state.get("_created_paths", [])
    if not undos and not paths:
        info("Nothing was created yet — nothing to clean up.")
        return

    print()
    warn("Rolling back — removing everything this run created...")
    # Undo actions first (stop services, revoke sudo) before their files vanish.
    for label, fn in reversed(undos):
        try:
            fn()
            ok(f"Reverted: {label}")
        except Exception as e:
            err(f"Could not revert {label}: {e}")
    # Delete paths deepest-first so nested files go before their parents.
    for p in sorted(set(paths), key=lambda x: len(str(x)), reverse=True):
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
                ok(f"Deleted: {p}")
            elif p.exists():
                p.unlink()
                ok(f"Deleted: {p}")
        except Exception as e:
            err(f"Could not delete {p}: {e}")
    print()
    ok("Rollback complete — the system is back to how it was before setup.")


def _check_sudo() -> bool:
    """Check if passwordless sudo is available."""
    try:
        result = subprocess.run(
            ["sudo", "-n", "true"],
            capture_output=True, timeout=2,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# ==========================================================================
# Main
# ==========================================================================

def main():
    os.chdir(PROJECT_ROOT)
    banner()

    state: dict = {}

    steps = [
        step_dependencies,
        step_install,
        step_overlay,
        step_hooks,
        step_modules,
        step_vault,
        step_discord,
        step_team,
        step_agent,
        step_full_control,
        step_hitl,
        step_compression,
        step_local_models,
        step_training,
        step_pyextras,
        step_generate,
        step_ops_instance,
        step_extras,
        step_launchd if sys.platform == "darwin" else step_systemd,
        step_browser,
        step_final_sync,
        step_summary,
    ]

    _PROGRESS["total"] = len(steps)

    try:
        for step in steps:
            _PROGRESS["current"] += 1
            step(state)
    except KeyboardInterrupt:
        print(f"\n\n  {YELLOW}Setup interrupted.{RESET}")
        # An abort late in the run used to cost every answer given so far —
        # rollback was the only option. Keeping is safe: the wizard detects
        # existing files on a re-run (vault unlocks from the key file, configs
        # prompt before overwrite), so continuing is mostly pressing Enter.
        if sys.stdin.isatty() and (state.get("_undo") or state.get("_created_paths")):
            info("Keep what's been set up so far and re-run setup later to "
                 "continue — or roll everything back now.")
            try:
                if ask_yn("Keep progress? (No = roll back)", default=True):
                    ok("Kept. Continue any time with: uv run python setup.py")
                    sys.exit(130)
            except (KeyboardInterrupt, EOFError):
                print()
        _rollback(state)
        sys.exit(130)
    except Exception as e:
        print(f"\n\n  {RED}Setup failed: {e}{RESET}")
        _rollback(state)
        sys.exit(1)

    print(f"  {GREEN}{BOLD}kbots is ready.{RESET}\n")


if __name__ == "__main__":
    main()
