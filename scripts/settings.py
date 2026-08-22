#!/usr/bin/env python3
"""kbots Settings Manager.

Interactive post-install tool for viewing and modifying kbots configuration.
Reads existing config files, presents a menu, writes changes back.

Usage: uv run python scripts/settings.py
"""

import getpass
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Resolve project root (parent of scripts/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Overlay deployments set KBOTS_OVERLAY — config lives there, not in core
_OVERLAY = os.environ.get("KBOTS_OVERLAY")
CONFIG_DIR = Path(_OVERLAY) / "config" if _OVERLAY else PROJECT_ROOT / "config"

try:
    import yaml
except ImportError:
    print("Run 'uv sync' first to install dependencies.")
    sys.exit(1)

try:
    from src.vault.fernet import FernetVault
except ImportError:
    print("Run 'uv sync' first to install dependencies.")
    sys.exit(1)

# Single source of truth for tier allow-lists. A local copy of this function
# once diverged (dropped Write/Edit/MultiEdit) and agents created through this
# TUI silently couldn't write files — never duplicate it again.
from src.core.agent_scaffold import cc_allow_for_tier  # noqa: E402

# --- Formatting (matches setup.py) ---

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
RESET = "\033[0m"


TAGLINE = "one process · LLM-agnostic · trains itself"


def banner(title: str = "kbots Settings Manager"):
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


def header(text: str):
    print(f"\n{BOLD}{CYAN}── {text} ──{RESET}\n")


def ok(text: str):
    print(f"  {GREEN}✓{RESET} {text}")


def warn(text: str):
    print(f"  {YELLOW}!{RESET} {text}")


def err(text: str):
    print(f"  {RED}✗{RESET} {text}")


def info(text: str):
    print(f"  {DIM}{text}{RESET}")


ALL_PERMISSIONS = [
    "Read(*)",
    "Glob(*)",
    "Grep(*)",
    "Edit(*)",
    "Write(*)",
    "MultiEdit(*)",
    "Bash(*)",
    "Bash(uv run python:*)",
    "WebSearch(*)",
    "WebFetch(*)",
    "mcp__kbots-tools",
]


def _permission_checklist(current: list[str]) -> list[str]:
    """Interactive toggle checklist for permissions. Returns updated list.

    Rules already present but not in ALL_PERMISSIONS (tier-scoped entries like
    "Write(./**)", custom additions) are shown and toggleable too — they must
    survive a visit to this menu, not get silently stripped.
    """
    items = list(dict.fromkeys([*ALL_PERMISSIONS, *current]))
    enabled = set(current)

    while True:
        print()
        for i, perm in enumerate(items, 1):
            check = f"{GREEN}✓{RESET}" if perm in enabled else f"{RED}✗{RESET}"
            print(f"    {i:>2}) {check} {perm}")
        print()

        choice = ask(f"Toggle (1-{len(items)}), d) done", "d").lower()
        if choice == "d":
            break

        try:
            idx = int(choice) - 1
            perm = items[idx]
            if perm in enabled:
                enabled.discard(perm)
            else:
                enabled.add(perm)
        except (ValueError, IndexError):
            err("Invalid choice")

    return [p for p in items if p in enabled]


def show(key: str, value, indent: int = 2):
    pad = " " * indent
    if isinstance(value, list):
        if not value:
            print(f"{pad}{BOLD}{key}:{RESET} {DIM}(empty){RESET}")
        else:
            print(f"{pad}{BOLD}{key}:{RESET}")
            for item in value:
                print(f"{pad}  - {item}")
    elif isinstance(value, dict):
        print(f"{pad}{BOLD}{key}:{RESET}")
        for k, v in value.items():
            show(k, v, indent + 2)
    else:
        print(f"{pad}{BOLD}{key}:{RESET} {value}")


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
        marker = f" {DIM}(current){RESET}" if opt == default else ""
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


def ask_int(prompt: str, default: int) -> int:
    while True:
        result = input(f"  {prompt} [{default}]: ").strip()
        if not result:
            return default
        try:
            return int(result)
        except ValueError:
            print("  Please enter a number.")


# --- Routing Helper ---

def _ask_routing(bot_account: str, default_mentions: bool = True) -> dict:
    """Prompt for agent routing config and return the routing dict."""
    mentions = ask_yn("Only respond when @mentioned?", default=default_mentions)

    print()
    info("How should this agent listen for messages?")
    print(f"    1) All channels {DIM}(wildcard — responds everywhere){RESET}")
    print(f"    2) Specific channels {DIM}(by channel ID){RESET}")
    print(f"    3) By category {DIM}(all channels in a Discord category){RESET}")
    print("    4) Specific channels + category")
    scope = ask_choice("Scope", ["1", "2", "3", "4"], default="1")

    channels = []
    categories = []
    if scope in ("2", "4"):
        info("Enter channel IDs (one per line, empty to stop):")
        while True:
            ch = ask("Channel ID (empty to stop)")
            if not ch:
                break
            channels.append(ch)
    if scope in ("3", "4"):
        info("Enter category IDs (one per line, empty to stop):")
        while True:
            cat = ask("Category ID (empty to stop)")
            if not cat:
                break
            categories.append(cat)

    guilds = []
    if ask_yn("Restrict to specific server/guild?", default=False):
        while True:
            g = ask("Guild ID (empty to stop)")
            if not g:
                break
            guilds.append(g)

    routing = {
        "discord": {
            "account": bot_account,
            "channels": channels,
            "mentions": mentions,
        }
    }
    if categories:
        routing["discord"]["categories"] = categories
    if guilds:
        routing["discord"]["guilds"] = guilds

    return routing


# --- Config I/O ---

def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _display_path(path: Path) -> Path | str:
    try:
        return path.relative_to(PROJECT_ROOT)
    except ValueError:
        return path


def save_yaml(path: Path, data: dict):
    path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
    ok(f"Saved {_display_path(path)}")


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def save_json(path: Path, data: dict):
    path.write_text(json.dumps(data, indent=2) + "\n")
    ok(f"Saved {_display_path(path)}")


def load_config() -> dict:
    return load_yaml(CONFIG_DIR / "config.yaml")


def save_config(cfg: dict):
    save_yaml(CONFIG_DIR / "config.yaml", cfg)


def load_agents() -> dict:
    return load_yaml(CONFIG_DIR / "agents.yaml")


def load_all_agents() -> dict:
    """Load agents from both main and rescue profiles."""
    main = load_yaml(CONFIG_DIR / "agents.yaml")
    rescue_path = CONFIG_DIR / "agents.rescue.yaml"
    if rescue_path.exists():
        rescue = load_yaml(rescue_path)
        for name, agent in rescue.get("agents", {}).items():
            main.setdefault("agents", {})[name] = agent
    return main


def save_agents(agents: dict):
    save_yaml(CONFIG_DIR / "agents.yaml", agents)


def get_vault() -> FernetVault | None:
    """Try to open the vault. Returns None if unavailable."""
    vault_path = CONFIG_DIR / "secrets.enc"
    if not vault_path.exists():
        return None
    vault = FernetVault(str(vault_path))
    key_file = Path.home() / ".config" / "kbots-vault-key"
    if key_file.exists():
        try:
            vault.unlock(key_file.read_text().strip())
            return vault
        except ValueError:
            pass
    for _ in range(3):
        passphrase = ask_secret("Vault passphrase")
        try:
            vault.unlock(passphrase)
            return vault
        except ValueError:
            err("Wrong passphrase.")
    return None


# --- Discord API ---

def validate_discord_token(token: str) -> dict | None:
    try:
        req = urllib.request.Request(
            "https://discord.com/api/v10/users/@me",
            headers={"Authorization": f"Bot {token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError):
        return None


# ==========================================================================
# Section editors
# ==========================================================================

def edit_llm(cfg: dict):
    header("LLM Defaults")
    llm = cfg.setdefault("defaults", {}).setdefault("llm", {})

    show("provider", llm.get("provider", "claude_code"))
    show("model", llm.get("model", "sonnet"))
    show("max_tokens", llm.get("max_tokens", 4096))
    show("mcp_config", llm.get("mcp_config", "(not set)"))
    print()

    if ask_yn("Edit LLM defaults?"):
        llm["provider"] = ask("Provider", llm.get("provider", "claude_code"))
        llm["model"] = ask_choice("Model", ["sonnet", "opus", "haiku"], default=llm.get("model", "sonnet"))
        llm["max_tokens"] = ask_int("Max tokens", llm.get("max_tokens", 4096))

        mcp = ask("MCP config path (empty to clear)", llm.get("mcp_config", ""))
        if mcp:
            llm["mcp_config"] = mcp
        elif "mcp_config" in llm:
            del llm["mcp_config"]

        save_config(cfg)


def edit_connectors(cfg: dict):
    header("Connectors")
    connectors = cfg.setdefault("connectors", {})

    # Show current state
    for name, conn in connectors.items():
        enabled = conn.get("enabled", False)
        status = f"{GREEN}enabled{RESET}" if enabled else f"{DIM}disabled{RESET}"
        print(f"  {BOLD}{name}{RESET}: {status}")
        if name == "discord" and "accounts" in conn:
            for bot_name, bot_cfg in conn["accounts"].items():
                print(f"    bot: {bot_name} (token: {bot_cfg.get('token_key', '?')})")
        if name == "http" and conn.get("port"):
            print(f"    port: {conn['port']}")

    print()
    print("  Options:")
    print("    1) Toggle connector on/off")
    print("    2) Add Discord bot account")
    print("    3) Remove Discord bot account")
    print("    4) Set HTTP port")
    print("    5) Back")
    print()

    while True:
        choice = ask("Choice", "5")
        if choice in ("5", "back", "b", ""):
            break
        elif choice == "1":
            name = ask("Connector name (discord/http)")
            if name in connectors:
                current = connectors[name].get("enabled", False)
                connectors[name]["enabled"] = not current
                state = "enabled" if not current else "disabled"
                ok(f"{name} {state}")
                save_config(cfg)
            else:
                err(f"Unknown connector: {name}")
        elif choice == "2":
            _add_bot_account(cfg)
        elif choice == "3":
            discord = connectors.get("discord", {})
            accounts = discord.get("accounts", {})
            if len(accounts) <= 1:
                err("Can't remove last bot account.")
            else:
                name = ask("Bot account name to remove")
                if name in accounts:
                    del accounts[name]
                    ok(f"Removed bot account '{name}'")
                    save_config(cfg)
                else:
                    err(f"No bot account named '{name}'")
        elif choice == "4":
            connectors.setdefault("http", {"enabled": False})
            connectors["http"]["port"] = ask_int("HTTP port", connectors["http"].get("port", 8080))
            save_config(cfg)


def _add_bot_account(cfg: dict):
    """Add a new Discord bot account."""
    bot_name = ask("Bot account name (internal, e.g. 'luna')")
    if not bot_name:
        return

    vault = get_vault()
    if vault is None:
        err("Vault unavailable — can't store token.")
        return

    token = ask_secret("Bot token")
    if not token:
        warn("Skipped.")
        return

    info("Validating token...")
    bot_info = validate_discord_token(token)
    if bot_info:
        ok(f"Bot verified: {bot_info.get('username', '?')}")
    else:
        if not ask_yn("Validation failed. Store anyway?", default=False):
            return

    vault_key = f"discord-{bot_name}"
    vault.set(vault_key, token)
    ok(f"Token stored as '{vault_key}'")

    discord = cfg.setdefault("connectors", {}).setdefault("discord", {"enabled": True})
    discord.setdefault("accounts", {})[bot_name] = {"token_key": vault_key}
    save_config(cfg)


def edit_session(cfg: dict):
    header("Session Defaults")
    session = cfg.setdefault("defaults", {}).setdefault("session", {})

    show("max_history", session.get("max_history", 50))
    show("summarize_after", session.get("summarize_after", 30))
    print()

    if ask_yn("Edit session settings?"):
        session["max_history"] = ask_int("Max history messages", session.get("max_history", 50))
        session["summarize_after"] = ask_int("Summarize after N messages", session.get("summarize_after", 30))
        save_config(cfg)


def edit_memory(cfg: dict):
    header("Memory")
    memory = cfg.setdefault("defaults", {}).setdefault("memory", {})

    show("backend", memory.get("backend", "sqlite"))
    show("semantic_search", memory.get("semantic_search", False))
    show("decay_enabled", memory.get("decay_enabled", False))
    show("max_results", memory.get("max_results", 10))
    print()

    if ask_yn("Edit memory settings?"):
        memory["backend"] = ask_choice("Backend", ["sqlite"], default=memory.get("backend", "sqlite"))

        current_semantic = memory.get("semantic_search", False)
        memory["semantic_search"] = ask_yn(
            "Enable semantic search (requires embedding model)?", default=current_semantic)

        if memory["semantic_search"] and not current_semantic:
            info("Semantic search requires the BGE embedding model (~50MB).")
            info("It will be downloaded automatically on first use.")

        memory["decay_enabled"] = ask_yn("Enable memory decay?", default=memory.get("decay_enabled", False))
        memory["max_results"] = ask_int("Max results per search", memory.get("max_results", 10))
        save_config(cfg)


def edit_reply(cfg: dict):
    """Reply length: post the point, keep the rest behind a reaction."""
    header("Reply Length")
    reply = cfg.setdefault("defaults", {}).setdefault("reply", {})
    shorten = reply.setdefault("shorten", {})

    show("enabled", shorten.get("enabled", False))
    show("threshold_chars", shorten.get("threshold_chars", 700))
    show("emoji", shorten.get("emoji", "\N{LEFT-POINTING MAGNIFYING GLASS}"))
    show("ttl_hours", shorten.get("ttl_hours", 72))
    print()
    info("Over the threshold, a reply is cut at a section boundary and the head")
    info("is posted with a footer saying how much was held back. The rest arrives")
    info("when you react with the emoji below, or say \"more\". Nothing is")
    info("summarised and nothing is lost: the cut is where a section starts.")
    print()

    if ask_yn("Edit reply settings?"):
        shorten["enabled"] = ask_yn("Shorten long replies?",
                                    default=shorten.get("enabled", False))
        if shorten["enabled"]:
            shorten["threshold_chars"] = ask_int(
                "Shorten replies longer than (chars)",
                shorten.get("threshold_chars", 700))
            shorten["emoji"] = ask(
                "Reaction that expands a shortened reply",
                default=shorten.get("emoji", "\N{LEFT-POINTING MAGNIFYING GLASS}"))
            shorten["ttl_hours"] = ask_int(
                "Keep the held-back text for (hours)", shorten.get("ttl_hours", 72))
        save_config(cfg)


def edit_hitl(cfg: dict):
    header("Security — Human-in-the-Loop")
    hitl = cfg.setdefault("security", {}).setdefault("hitl", {})

    show("enabled", hitl.get("enabled", True))
    show("connector", hitl.get("connector", "discord"))
    show("channel", hitl.get("channel", "(not set)"))
    show("approvers", hitl.get("approvers", []))
    show("timeout", hitl.get("timeout", 1800))
    show("fail_mode", hitl.get("fail_mode", "closed"))
    show("poll_interval", hitl.get("poll_interval", 3))
    show("gated_tools", hitl.get("gated_tools", []))
    print()

    print("  Options:")
    print("    1) Edit channel / timeout / fail mode")
    print("    2) Manage approvers")
    print("    3) Manage gated tools")
    print("    4) Toggle approval gate on/off")
    print("    5) Back")
    print()
    info("Note: at runtime the gate is also toggleable live via /admin hitl in Discord.")

    while True:
        choice = ask("Choice", "5")
        if choice in ("5", "back", "b", ""):
            break
        elif choice == "4":
            current = hitl.get("enabled", True)
            hitl["enabled"] = not current
            save_config(cfg)
            ok(f"Approval gate default set to: {'ON' if hitl['enabled'] else 'OFF'}")
            warn("This is the boot default. A live /admin hitl toggle overrides it "
                 "until changed again.")
        elif choice == "1":
            hitl["channel"] = ask("Approval channel ID", hitl.get("channel", ""))
            hitl["timeout"] = ask_int("Timeout (seconds)", hitl.get("timeout", 1800))
            hitl["fail_mode"] = ask_choice("Fail mode", ["closed", "open"], default=hitl.get("fail_mode", "closed"))
            hitl["poll_interval"] = ask_int("Poll interval (seconds)", hitl.get("poll_interval", 3))
            save_config(cfg)
        elif choice == "2":
            _edit_hitl_approvers(cfg, hitl)
        elif choice == "3":
            _edit_hitl_gated_tools(cfg, hitl)


def _edit_hitl_approvers(cfg: dict, hitl: dict):
    current = hitl.get("approvers", [])
    if current:
        print()
        for i, uid in enumerate(current, 1):
            print(f"    {i}) {uid}")
    else:
        info("No approvers configured.")

    print()
    print("    a) Add approver")
    print("    r) Remove approver")
    print("    b) Back")
    print()

    while True:
        choice = ask("Choice", "b")
        if choice in ("b", "back", ""):
            break
        elif choice == "a":
            uid = ask("Discord user ID")
            if uid and uid not in current:
                current.append(uid)
                hitl["approvers"] = current
                save_config(cfg)
                ok(f"Added approver: {uid}")
            elif uid in current:
                warn("Already in list.")
        elif choice == "r":
            uid = ask("Discord user ID to remove")
            if uid in current:
                current.remove(uid)
                hitl["approvers"] = current
                save_config(cfg)
                ok(f"Removed approver: {uid}")
            else:
                err("Not in list.")


def _edit_hitl_gated_tools(cfg: dict, hitl: dict):
    gated = hitl.get("gated_tools", [])
    if gated:
        print()
        info("Currently gated tools (require human approval):")
        for i, tool in enumerate(gated, 1):
            print(f"    {i}) {tool}")
    else:
        info("No tools are HITL-gated. All tools execute without approval.")

    # Show available tools for reference
    tools_dir = PROJECT_ROOT / "src" / "tools"
    available = []
    if tools_dir.exists():
        available = [p.stem for p in sorted(tools_dir.glob("*.py")) if not p.name.startswith("__")]

    print()
    print("    a) Add tool to gate")
    print("    r) Remove tool from gate")
    if available:
        print("    l) List available tools")
    print("    b) Back")
    print()

    while True:
        choice = ask("Choice", "b")
        if choice in ("b", "back", ""):
            break
        elif choice == "a":
            tool = ask("Tool name (e.g. send_email, install_mcp)")
            if tool and tool not in gated:
                gated.append(tool)
                hitl["gated_tools"] = gated
                save_config(cfg)
                ok(f"Gated: {tool} — now requires approval")
            elif tool in gated:
                warn("Already gated.")
        elif choice == "r":
            if not gated:
                warn("No tools to remove.")
                continue
            for i, t in enumerate(gated, 1):
                print(f"    {i}) {t}")
            target = ask("Tool number or name")
            removed = None
            try:
                idx = int(target) - 1
                if 0 <= idx < len(gated):
                    removed = gated.pop(idx)
            except ValueError:
                if target in gated:
                    gated.remove(target)
                    removed = target
            if removed:
                hitl["gated_tools"] = gated
                save_config(cfg)
                ok(f"Ungated: {removed} — no longer requires approval")
            else:
                err(f"Not found: {target}")
        elif choice == "l" and available:
            print()
            for t in available:
                gated_marker = f" {YELLOW}(gated){RESET}" if t in gated else ""
                print(f"    {t}{gated_marker}")


def edit_rate_limits(cfg: dict):
    header("Rate Limits")
    rl = cfg.setdefault("security", {}).setdefault("rate_limits", {})

    show("mode", rl.get("mode", "log"))
    defaults = rl.get("defaults", {})
    show("max_per_hour", defaults.get("max_per_hour", 100))
    print()

    if ask_yn("Edit rate limits?"):
        rl["mode"] = ask_choice("Mode", ["log", "enforce"], default=rl.get("mode", "log"))
        rl.setdefault("defaults", {})["max_per_hour"] = ask_int(
            "Max requests per hour", defaults.get("max_per_hour", 100)
        )
        save_config(cfg)


def edit_compression(cfg: dict):
    header("Context Compression")
    comp = cfg.setdefault("security", {}).setdefault("compression", {})

    show("enabled", comp.get("enabled", False))
    show("level", comp.get("level", "standard"))
    show("memory_recall", comp.get("memory_recall", False))
    print()

    if ask_yn("Edit compression settings?"):
        comp["enabled"] = ask_yn("Enable context compression?", default=comp.get("enabled", False))
        if comp["enabled"]:
            comp["level"] = ask_choice("Level", ["lite", "standard"], default=comp.get("level", "standard"))
            comp["memory_recall"] = ask_yn(
                "Enable memory recall compression?", default=comp.get("memory_recall", False)
            )
        save_config(cfg)


def edit_admin_users(cfg: dict):
    header("Admin Users")
    admins = cfg.setdefault("admin_users", {})

    for platform, users in admins.items():
        show(platform, users)
    print()

    if ask_yn("Edit admin users?"):
        platform = ask("Platform", "discord")
        current = admins.get(platform, [])
        if current:
            info(f"Current: {', '.join(current)}")
        if ask_yn("Reset list?", default=False):
            current = []
        while ask_yn("Add a user ID?", default=not current):
            uid = ask("User ID")
            if uid and uid not in current:
                current.append(uid)
        admins[platform] = current
        save_config(cfg)


def edit_kbots_identity(cfg: dict):
    header("kbots Identity")
    kag = cfg.setdefault("kbots", {})

    show("name", kag.get("name", "kbots"))
    show("log_level", kag.get("log_level", "info"))
    show("data_dir", kag.get("data_dir", "./data"))
    print()

    if ask_yn("Edit identity settings?"):
        kag["name"] = ask("System name", kag.get("name", "kbots"))
        kag["log_level"] = ask_choice(
            "Log level", ["debug", "info", "warning", "error"], default=kag.get("log_level", "info")
        )
        kag["data_dir"] = ask("Data directory", kag.get("data_dir", "./data"))
        save_config(cfg)


def manage_vault():
    header("Vault Secrets")
    vault = get_vault()
    if vault is None:
        err("Could not open vault.")
        return

    def show_keys() -> list[str]:
        keys = sorted(vault.list_keys())
        if keys:
            info(f"{len(keys)} secret(s) stored:")
            for i, key in enumerate(keys, 1):
                print(f"    {i:>2}) {key}")
        else:
            info("Vault is empty.")
        return keys

    def pick_key(prompt: str, keys: list[str]) -> str | None:
        val = ask(prompt)
        if not val:
            return None
        if val.isdigit():
            idx = int(val) - 1
            if 0 <= idx < len(keys):
                return keys[idx]
            err(f"Number out of range (1-{len(keys)}).")
            return None
        return val

    sorted_keys = show_keys()

    print()
    print("  Options:")
    print("    1) Add / update a secret")
    print("    2) Remove a secret")
    print("    3) Back")
    print()

    while True:
        choice = ask("Choice", "3")
        if choice in ("3", "back", "b", ""):
            break
        elif choice == "1":
            key = pick_key("Secret number or name (e.g. gemini-api-key, cloudflare-api-token)", sorted_keys)
            if not key:
                continue
            if (not key.startswith("secrets/") and not key.startswith("discord-")
                    and not key.startswith("active-") and not key.isupper()):
                key = f"secrets/{key}"
                info(f"Auto-prefixed → '{key}'")
            value = ask_secret(f"Value for '{key}'")
            if value:
                vault.set(key, value)
                ok(f"Stored '{key}'")
                sorted_keys = show_keys()
        elif choice == "2":
            key = pick_key("Secret number or name to remove", sorted_keys)
            if key and key in vault.list_keys():
                vault.delete(key)
                ok(f"Removed '{key}'")
                sorted_keys = show_keys()
            else:
                err(f"Secret '{key}' not found.")


def create_agent():
    header("Create New Agent")
    cfg = load_config()
    agents_cfg = load_agents()

    existing = list(agents_cfg.get("agents", {}).keys())
    if existing:
        info(f"Existing agents: {', '.join(existing)}")
    print()

    agent_name = ask("Agent internal name (lowercase, no spaces)")
    if not agent_name:
        return
    if agent_name in agents_cfg.get("agents", {}):
        err(f"Agent '{agent_name}' already exists.")
        return

    display_name = ask("Display name", agent_name.capitalize())
    description = ask("One-line description", "")
    model = ask_choice("Model", ["sonnet", "opus", "haiku"], default="sonnet")

    # Tier selection drives defaults
    print()
    info("Tier determines default permissions:")
    info("  privileged — full file/shell access (ops/dev agents)")
    info("  coordinator — MCP tools only, no builtins (team leads)")
    info("  assistant — MCP tools only, no builtins (helpers)")
    tier = ask_choice("Tier", ["privileged", "coordinator", "assistant"], default="assistant")

    privileged = tier == "privileged"
    disallow = [] if privileged else ["Edit", "Write", "Bash", "MultiEdit"]

    # Bot account
    discord_accounts = list(
        cfg.get("connectors", {}).get("discord", {}).get("accounts", {}).keys()
    )
    if discord_accounts:
        info(f"Available bot accounts: {', '.join(discord_accounts)}")
        bot_account = ask("Bot account", discord_accounts[0])
    else:
        bot_account = ask("Bot account", "main")

    # Routing
    routing = _ask_routing(bot_account)

    # Personality
    personality = ask("Personality (e.g. 'concise and direct')", "concise and direct")

    # Per-agent LLM override
    agent_llm = {"provider": "claude_code", "model": model}

    # Per-agent compression override
    comp_override = None
    if ask_yn("Set per-agent compression override?", default=False):
        comp_override = {
            "enabled": ask_yn("Enable compression for this agent?", default=False),
            "level": ask_choice("Level", ["lite", "standard"], default="standard"),
        }

    # Build config entry
    agent_entry = {
        "display_name": display_name,
        "description": description,
        "project_dir": f"./agents/{agent_name}",
        "llm": agent_llm,
        "tools": "all",
        "skills": "all",
    }
    if privileged:
        agent_entry["privileged"] = True
    if disallow:
        agent_entry["disallow_builtins"] = disallow
    if comp_override:
        agent_entry["compression"] = comp_override
    agent_entry["routing"] = routing

    # Write agents.yaml
    agents_cfg.setdefault("agents", {})[agent_name] = agent_entry
    save_agents(agents_cfg)

    # Create agent directory
    agent_dir = PROJECT_ROOT / "agents" / agent_name
    agent_dir.mkdir(parents=True, exist_ok=True)

    # CLAUDE.md
    claude_md = f"""# {display_name}

## Identity

You are **{display_name}**. {description}.

## Guidelines

- Be {personality}
- Search memory before asking the user for context you might already have
- Remember important things from conversations by storing them to memory
- When handling tasks, break them down and track progress

## File System

Your project directory is `{agent_dir}/`. Use `$KBOTS_TMP` for generated files (media, docs, scratch).

**NEVER** `git add`, `git commit`, or `git push` in the kbots core engine
directory — it is the framework, not your workspace. Agent configs, custom
files, and deployment data belong in the overlay.

## Memory System

Use your MCP tools for memory — do NOT use curl or HTTP calls.

- `memory_search` — keyword/FTS5 search
- `memory_semantic_search` — embedding-based conceptual search
- `memory_store` — save important information
- `memory_forget` — remove a memory by ID

## Team

Call `team_list` for the roster — who your teammates are and how to reach
them — and `team_get` for one member's detail. Use the tools rather than
looking for a roster file: it lives outside your sandbox, so hunting for it
only earns you a denied read. User context is injected before each message
so you know who you're talking to.
"""
    from src.core.agent_scaffold import write_identity
    identity_written = write_identity(agent_dir, claude_md)
    if identity_written:
        for p in identity_written:
            ok(f"Created {_display_path(agent_dir)}/{p.name}")
    else:
        warn("Identity files already exist — skipping (won't overwrite)")

    # .mcp.json
    mcp_path = agent_dir / ".mcp.json"
    if mcp_path.exists():
        warn(".mcp.json already exists — skipping (won't overwrite)")
    else:
        mcp_json = {
            "mcpServers": {
                "kbots-tools": {
                    "command": str(PROJECT_ROOT / ".venv" / "bin" / "python3"),
                    "args": ["-m", "src.mcp_server"],
                    "cwd": str(PROJECT_ROOT),
                }
            }
        }
        mcp_path.write_text(json.dumps(mcp_json, indent=2) + "\n")
        ok(f"Created {agent_dir.relative_to(PROJECT_ROOT)}/.mcp.json")

    # .claude/settings.json — tier drives default permissions
    claude_dir = agent_dir / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path = claude_dir / "settings.json"
    if settings_path.exists():
        warn(".claude/settings.json already exists — skipping (won't overwrite)")
    else:
        allow_list = cc_allow_for_tier(tier)
        settings = {
            "permissions": {
                "allow": list(allow_list),
                "deny": [],
            }
        }
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
        ok(f"Created {agent_dir.relative_to(PROJECT_ROOT)}/.claude/settings.json")

    # Avatar — the brand template with per-agent eyes + accent (assets/README.md)
    print()
    if ask_yn("Generate an avatar for this agent?", default=True):
        from avatar import ACCENTS as _ACCENTS
        from avatar import EYES as _EYES

        eyes = ask_choice("Eye style", list(_EYES), default="capsule")
        accent = ask_choice("Accent color", list(_ACCENTS), default="red")
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "avatar.py"),
             "--eyes", eyes, "--accent", accent,
             "--out", str(agent_dir / "avatar")],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            ok(f"Avatar written to agents/{agent_name}/avatar.svg (+ .png if Chromium available)")
            png_path = agent_dir / "avatar.png"
            if png_path.exists() and ask_yn(
                f"Set it as bot '{bot_account}'s Discord avatar now?", default=True
            ):
                from avatar import upload_discord_avatar

                token_key = (
                    cfg.get("connectors", {}).get("discord", {}).get("accounts", {})
                    .get(bot_account, {}).get("token_key", f"discord-{bot_account}")
                )
                vault = get_vault()
                token = vault.get(token_key) if vault else None
                if not token and vault:
                    token = vault.get("discord-token")
                if token:
                    success, msg = upload_discord_avatar(png_path, token)
                    if success:
                        ok(msg)
                    else:
                        warn(f"Discord upload failed: {msg}")
                        info("Upload manually: Discord Developer Portal → your app → Bot → icon.")
                else:
                    warn(f"No vault token for '{bot_account}' ({token_key}) — upload manually.")
            else:
                info("Upload avatar.png as the bot's icon: Discord Developer Portal → your app → Bot.")
        else:
            warn(f"Avatar generation failed: {result.stderr.strip() or result.stdout.strip()}")
            info("Run manually: uv run python scripts/avatar.py --list")

    print()
    ok(f"Agent '{display_name}' created — directory: agents/{agent_name}/")
    info("Customise the CLAUDE.md to set the agent's full identity and behaviour.")
    info("Restart kbots to pick up the new agent.")



def view_agents():
    header("Agents Overview")
    agents_cfg = load_all_agents()
    agents = agents_cfg.get("agents", {})

    if not agents:
        info("No agents configured.")
        return

    agent_names = list(agents.keys())
    for name, agent in agents.items():
        priv = f" {YELLOW}[privileged]{RESET}" if agent.get("privileged") else ""
        model = agent.get("llm", {}).get("model", "?")
        routing = agent.get("routing", {}).get("discord", {})
        bot = routing.get("account", "?")
        mentions = "mentions" if routing.get("mentions") else "all messages"
        channels = routing.get("channels", [])
        categories = routing.get("categories", [])
        guilds = routing.get("guilds", [])
        scope_parts = []
        if channels:
            scope_parts.append(f"channels: {','.join(channels)}")
        if categories:
            scope_parts.append(f"categories: {','.join(categories)}")
        if guilds:
            scope_parts.append(f"guilds: {','.join(guilds)}")
        scope_str = f", {', '.join(scope_parts)}" if scope_parts else ""

        print(f"  {BOLD}{name}{RESET}{priv}")
        print(f"    {agent.get('display_name', name)} — {agent.get('description', '')}")
        print(f"    model: {model}, bot: {bot}, {mentions}{scope_str}")

        # Per-agent overrides
        comp = agent.get("compression")
        if comp:
            print(f"    compression: {comp}")
        disallow = agent.get("disallow_builtins", [])
        if disallow:
            print(f"    disallow_builtins: {', '.join(disallow)}")
        print()

    # Agent editing
    print("  Options:")
    print("    1) Change agent model")
    print("    2) Change agent routing")
    print("    3) Back")
    print()

    while True:
        choice = ask("Choice", "4")
        if choice in ("3", "back", "b", ""):
            break

        # Select target agent
        if choice in ("1", "2"):
            if len(agent_names) == 1:
                target = agent_names[0]
                info(f"Only one agent: {target}")
            else:
                for i, name in enumerate(agent_names, 1):
                    current_model = agents[name].get("llm", {}).get("model", "?")
                    print(f"    {i}) {name} (model: {current_model})")
                target_input = ask("Agent number or name")
                target = None
                try:
                    idx = int(target_input) - 1
                    if 0 <= idx < len(agent_names):
                        target = agent_names[idx]
                except ValueError:
                    if target_input in agents:
                        target = target_input
                if not target:
                    err(f"Unknown agent: {target_input}")
                    continue

        if choice == "1":
            current_model = agents[target].get("llm", {}).get("model", "sonnet")
            new_model = ask_choice("Model", ["sonnet", "opus", "haiku"], default=current_model)
            if new_model != current_model:
                agents[target].setdefault("llm", {})["model"] = new_model
                save_agents(agents_cfg)
                ok(f"{target}: model → {new_model}")
            else:
                info("No change.")
        elif choice == "2":
            current_bot = agents[target].get("routing", {}).get("discord", {}).get("account", "main")
            info(f"Reconfiguring routing for {target} (bot: {current_bot})")
            new_routing = _ask_routing(current_bot)
            agents[target]["routing"] = new_routing
            save_agents(agents_cfg)
            ok(f"{target}: routing updated")


def create_ops_instance():
    """Guided setup for the dual-instance (sandboxed + privileged ops) pattern."""
    header("Create Privileged Ops Instance")
    info("This sets up the dual-instance deployment pattern:")
    info("  Main instance  — sandboxed, runs user-facing agents")
    info("  Ops instance   — unsandboxed, single privileged agent for dev/ops")
    info("")
    info("The ops agent can edit code, restart services, and run deploys.")
    info("Only the system owner should have access to it.")
    print()

    # Check if rescue profile already exists
    rescue_yaml = CONFIG_DIR / "agents.rescue.yaml"
    rescue_service = PROJECT_ROOT / "config" / "kbots-rescue.service"

    if rescue_yaml.exists():
        warn(f"agents.rescue.yaml already exists at {rescue_yaml}")
        if not ask_yn("Overwrite and reconfigure?", default=False):
            return
    print()

    # Agent name
    agent_name = ask("Agent internal name", "engineer")
    display_name = ask("Display name", agent_name.capitalize() + " Bot")
    description = ask("Description", "Privileged ops agent — unsandboxed, owner-only")
    model = ask_choice("Model", ["sonnet", "opus", "haiku"], default="opus")

    # Bot account
    cfg = load_config()
    discord_accounts = list(
        cfg.get("connectors", {}).get("discord", {}).get("accounts", {}).keys()
    )

    print()
    info("The ops agent needs its own Discord bot account so it has a")
    info("separate identity from your user-facing agents.")
    print()

    if ask_yn("Create a new Discord bot account for this agent?", default=True):
        bot_name = ask("Bot account name", agent_name)
        vault = get_vault()
        if vault:
            token = ask_secret(f"Discord bot token for '{bot_name}'")
            if token:
                info("Validating token...")
                bot_info = validate_discord_token(token)
                if bot_info:
                    ok(f"Bot verified: {bot_info.get('username', '?')}")
                else:
                    if not ask_yn("Validation failed. Store anyway?", default=False):
                        warn("Skipped token — add it later via vault.")
                        token = None
                if token:
                    vault_key = f"discord-{bot_name}"
                    vault.set(vault_key, token)
                    ok(f"Token stored as '{vault_key}'")
                    dc = cfg.setdefault("connectors", {}).setdefault("discord", {})
                    dc.setdefault("accounts", {})[bot_name] = {"token_key": vault_key}
                    save_config(cfg)
            else:
                warn("No token provided — add it later via vault.")
        else:
            err("Vault unavailable — add the bot token later.")
            bot_name = ask("Bot account name (will configure token later)", agent_name)
    else:
        if discord_accounts:
            info(f"Available accounts: {', '.join(discord_accounts)}")
        bot_name = ask("Existing bot account to use", discord_accounts[0] if discord_accounts else "main")

    # Routing
    print()
    routing = _ask_routing(bot_name, default_mentions=True)

    # Write agents.rescue.yaml
    rescue_agents = {
        "agents": {
            agent_name: {
                "display_name": display_name,
                "description": description,
                "project_dir": f"./agents/{agent_name}",
                "privileged": True,
                "llm": {"provider": "claude_code", "model": model},
                "tools": "all",
                "skills": "all",
                "routing": routing,
            }
        }
    }
    save_yaml(rescue_yaml, rescue_agents)

    # Create agent directory + scaffolding
    agent_dir = PROJECT_ROOT / "agents" / agent_name
    agent_dir.mkdir(parents=True, exist_ok=True)

    template_name = "ops-claude-macos.md" if sys.platform == "darwin" else "rescue-claude.md"
    template_path = PROJECT_ROOT / "config" / "templates" / template_name
    if template_path.exists():
        claude_md = template_path.read_text().format(
            display_name=display_name, agent_dir=agent_dir, engine_root=PROJECT_ROOT)
    else:
        claude_md = f"# {display_name}\n\nYou are {display_name} — the ops and dev agent.\n"
        warn(f"Template not found at config/templates/{template_name} — using minimal fallback")
    from src.core.agent_scaffold import write_identity
    identity_written = write_identity(agent_dir, claude_md)
    if identity_written:
        for p in identity_written:
            ok(f"Created agents/{agent_name}/{p.name}")
    else:
        warn("Identity files already exist — skipping (won't overwrite)")

    mcp_path = agent_dir / ".mcp.json"
    if mcp_path.exists():
        warn(".mcp.json already exists — skipping (won't overwrite)")
    else:
        mcp_json = {
            "mcpServers": {
                "kbots-tools": {
                    "command": str(PROJECT_ROOT / ".venv" / "bin" / "python3"),
                    "args": ["-m", "src.mcp_server"],
                    "cwd": str(PROJECT_ROOT),
                }
            }
        }
        mcp_path.write_text(json.dumps(mcp_json, indent=2) + "\n")
        ok(f"Created agents/{agent_name}/.mcp.json")

    claude_dir = agent_dir / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path = claude_dir / "settings.json"
    if settings_path.exists():
        warn(".claude/settings.json already exists — skipping (won't overwrite)")
    else:
        settings = {"permissions": {"allow": cc_allow_for_tier("privileged"), "deny": []}}
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
        ok(f"Created agents/{agent_name}/.claude/settings.json")

    # Check if service file exists
    print()
    if rescue_service.exists():
        ok("kbots-rescue.service already exists in config/")
    else:
        warn("kbots-rescue.service not found in config/")
        info("The service template ships with kbots core at config/kbots-rescue.service")

    # Install instructions
    print()
    header("Next Steps")
    info("1. Customise the CLAUDE.md:  agents/" + agent_name + "/CLAUDE.md")
    info("2. Install the service unit:")
    info("     sudo cp config/kbots-rescue.service /etc/systemd/system/")
    info("     sudo systemctl daemon-reload")
    info("     sudo systemctl enable --now kbots-rescue.service")
    info("3. The ops agent loads profile 'rescue' — only agents in")
    info("   agents.rescue.yaml are started. Your main agents are unaffected.")
    info("")
    info("See ARCHITECTURE.md → Deployment Patterns for full details.")


def manage_timers():
    header("Systemd Timers")

    if not shutil.which("systemctl"):
        err("systemd not available on this system.")
        return

    # Gather timer units from core and overlay
    core_timers = PROJECT_ROOT / "config" / "timers"
    overlay_systemd = Path(_OVERLAY) / "systemd" if _OVERLAY else None

    timer_names = set()
    if core_timers.exists():
        for f in core_timers.iterdir():
            if f.name.endswith(".timer"):
                timer_names.add(f.name)
    if overlay_systemd and overlay_systemd.exists():
        for f in overlay_systemd.iterdir():
            if f.name.endswith(".timer"):
                timer_names.add(f.name)

    if not timer_names:
        info("No timer units found.")
        return

    # Show status of each timer
    def _show_timers():
        print()
        for name in sorted(timer_names):
            # Check systemd status
            result = subprocess.run(
                ["systemctl", "is-enabled", name],
                capture_output=True, text=True,
            )
            enabled = result.stdout.strip() == "enabled"

            result2 = subprocess.run(
                ["systemctl", "is-active", name],
                capture_output=True, text=True,
            )
            active = result2.stdout.strip()

            # Get schedule from timer file
            schedule = "?"
            for search_dir in ([overlay_systemd, core_timers] if overlay_systemd else [core_timers]):
                timer_file = search_dir / name if search_dir else None
                if timer_file and timer_file.exists():
                    for line in timer_file.read_text().splitlines():
                        if line.startswith("OnCalendar="):
                            schedule = line.split("=", 1)[1]
                            break
                        elif line.startswith("OnBootSec=") or line.startswith("OnUnitActiveSec="):
                            schedule = line.split("=", 1)[1]
                            break
                    break

            if enabled:
                status = f"{GREEN}enabled{RESET}"
            else:
                status = f"{DIM}disabled{RESET}"
            active_str = f" ({active})" if active not in ("", "inactive") else ""

            label = name.replace(".timer", "").replace("kbots-", "")
            print(f"    {BOLD}{label}{RESET}: {status}{active_str}  schedule: {schedule}")

    _show_timers()

    print()
    print("  Options:")
    print("    1) Enable a timer")
    print("    2) Disable a timer")
    print("    3) Change timer schedule")
    print("    4) Back")
    print()

    while True:
        choice = ask("Choice", "4")
        if choice in ("4", "back", "b", ""):
            break
        elif choice in ("1", "2"):
            action = "enable" if choice == "1" else "disable"
            label = ask(f"Timer name to {action} (e.g. memory-decay, health-audit)")
            # Resolve to full unit name
            unit = label if label.endswith(".timer") else f"kbots-{label}.timer"
            if unit not in timer_names:
                err(f"Unknown timer: {unit}")
                continue
            cmd = ["sudo", "-n", "systemctl", action]
            if action == "enable":
                cmd.append("--now")
            cmd.append(unit)
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                ok(f"{unit} {action}d")
            else:
                stderr = result.stderr.strip()
                if "password is required" in stderr or "authentication" in stderr.lower():
                    err(f"sudo required — run: sudo systemctl {action} {unit}")
                else:
                    err(f"Failed: {stderr}")
        elif choice == "3":
            label = ask("Timer name (e.g. memory-decay, health-audit)")
            unit = label if label.endswith(".timer") else f"kbots-{label}.timer"
            if unit not in timer_names:
                err(f"Unknown timer: {unit}")
                continue

            # Find the timer file (prefer overlay)
            timer_file = None
            if overlay_systemd and (overlay_systemd / unit).exists():
                timer_file = overlay_systemd / unit
            elif core_timers and (core_timers / unit).exists():
                timer_file = core_timers / unit

            if not timer_file:
                err("Timer file not found.")
                continue

            content = timer_file.read_text()
            schedule_key = None
            current = "?"
            for line in content.splitlines():
                if line.startswith("OnCalendar="):
                    schedule_key = "OnCalendar"
                    current = line.split("=", 1)[1]
                    break
                elif line.startswith("OnUnitActiveSec="):
                    schedule_key = "OnUnitActiveSec"
                    current = line.split("=", 1)[1]
                    break
                elif line.startswith("OnBootSec="):
                    schedule_key = "OnBootSec"
                    current = line.split("=", 1)[1]
                    break

            if not schedule_key:
                warn("No recognised schedule directive found in this timer.")
                continue

            info(f"Current: {schedule_key}={current}")
            if schedule_key == "OnCalendar":
                info("Examples: *-*-* 03:00:00 (daily 3am), *-*-* *:00:00 (hourly)")
                info("          *-*-* 06,18:00:00 (twice daily)")
            else:
                info(f"This timer uses interval-based scheduling ({schedule_key}).")
                info("Examples: 30min, 1h, 6h, 1d")
            new_schedule = ask(f"New {schedule_key} value", current)
            if new_schedule == current:
                info("No change.")
                continue

            # Write updated timer — if source is core, copy to overlay first
            if overlay_systemd and timer_file.parent == core_timers:
                timer_file = overlay_systemd / unit
                timer_file.parent.mkdir(parents=True, exist_ok=True)
                timer_file.write_text(content)
                info(f"Copied to overlay: {_display_path(timer_file)}")

            lines = timer_file.read_text().splitlines()
            new_lines = []
            for line in lines:
                if line.startswith(f"{schedule_key}="):
                    new_lines.append(f"{schedule_key}={new_schedule}")
                else:
                    new_lines.append(line)
            timer_file.write_text("\n".join(new_lines) + "\n")
            ok(f"Schedule updated: {schedule_key}={new_schedule}")

            # Reload if systemd is managing it
            subprocess.run(
                ["sudo", "-n", "systemctl", "daemon-reload"],
                capture_output=True,
            )
            subprocess.run(
                ["sudo", "-n", "systemctl", "restart", unit],
                capture_output=True,
            )
            ok("Timer reloaded")

        _show_timers()


# ==========================================================================
# Tier Permissions
# ==========================================================================


def _load_team_json() -> dict:
    """Load team.json from config dir."""
    path = CONFIG_DIR / "team.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text()) or {}


def _agent_tier(agent_name: str, agents_cfg: dict, team: dict) -> str:
    """Resolve agent tier from team.json, falling back to agents.yaml heuristics."""
    for entry in team.get("agents", []):
        if entry.get("id") == agent_name:
            return entry.get("agent_tier", "assistant")
    # Fallback: infer from agents.yaml
    agent_cfg = agents_cfg.get("agents", {}).get(agent_name, {})
    if agent_cfg.get("privileged"):
        return "privileged"
    if not agent_cfg.get("disallow_builtins"):
        return "privileged"
    return "assistant"


def manage_tier_permissions():
    """View and edit per-agent Claude Code permissions (reads from agents.yaml + team.json)."""

    agents_cfg = _load_agents_yaml()
    team = _load_team_json()
    agents_dir = _resolve_agents_dir()

    if not agents_cfg.get("agents"):
        err("No agents found in agents.yaml")
        return

    # Show current state per agent
    print(f"\n  {BOLD}Agent Permissions{RESET}")
    print()
    info("Source: team.json (tier) + agents.yaml (cc_allow override)")
    info("Tier defaults: privileged → full access, coordinator → read + restricted shell, assistant → read only")
    print()

    agent_list = []
    for name, cfg in agents_cfg["agents"].items():
        tier = _agent_tier(name, agents_cfg, team)
        cc_allow = cfg.get("cc_allow")
        if cc_allow:
            source = "cc_allow (custom)"
            perms = cc_allow
        else:
            source = f"tier:{tier} (default)"
            perms = cc_allow_for_tier(tier)

        agent_list.append((name, tier, cc_allow, perms, source))
        display = cfg.get("display_name", name)
        print(f"  {BOLD}{display}{RESET} ({name})  —  {source}")
        for p in perms:
            print(f"    {GREEN}✓{RESET} {p}")
        print()

    if not agent_list:
        return

    # Edit an agent's permissions
    print(f"  {BOLD}Edit agent permissions?{RESET}")
    print()
    for i, (name, *_) in enumerate(agent_list, 1):
        print(f"    {i}) {name}")
    print("    s) Skip")
    print()

    choice = ask("Edit which agent", "s").lower()
    if choice == "s":
        return

    try:
        idx = int(choice) - 1
        agent_name, tier, cc_allow, current_perms, _ = agent_list[idx]
    except (ValueError, IndexError):
        err("Invalid choice")
        return

    print()
    info(f"Current tier: {tier}")
    info(f"Current allow: {', '.join(current_perms)}")
    print()
    print(f"    1) Use tier default ({tier}) — derives from team.json")
    print("    2) Change tier")
    print("    3) Set custom cc_allow list in agents.yaml")
    print()

    action = ask("Action", "1")

    if action == "1":
        # Remove any cc_allow override, use tier default
        if "cc_allow" in agents_cfg["agents"][agent_name]:
            del agents_cfg["agents"][agent_name]["cc_allow"]
            _save_agents_yaml(agents_cfg)
            ok(f"Removed cc_allow override — using tier default ({tier})")
        else:
            info("Already using tier default")
        allow_list = cc_allow_for_tier(tier)

    elif action == "2":
        new_tier = ask_choice("New tier", ["privileged", "coordinator", "assistant"], default=tier)
        if new_tier == tier:
            info("Tier unchanged")
            allow_list = cc_allow_for_tier(tier)
        else:
            # Update team.json
            for entry in team.get("agents", []):
                if entry.get("id") == agent_name:
                    entry["agent_tier"] = new_tier
                    break
            team_path = CONFIG_DIR / "team.json"
            team_path.write_text(json.dumps(team, indent=2) + "\n")
            ok(f"Updated team.json: {agent_name} → {new_tier}")

            # Update disallow_builtins in agents.yaml to match
            if new_tier == "privileged":
                agents_cfg["agents"][agent_name].pop("disallow_builtins", None)
                agents_cfg["agents"][agent_name]["privileged"] = True
            else:
                agents_cfg["agents"][agent_name]["disallow_builtins"] = [
                    "Edit", "Write", "Bash", "MultiEdit",
                ]
                agents_cfg["agents"][agent_name].pop("privileged", None)

            # Remove cc_allow override since we're using tier default
            agents_cfg["agents"][agent_name].pop("cc_allow", None)
            _save_agents_yaml(agents_cfg)
            ok(f"Updated agents.yaml for tier {new_tier}")
            allow_list = cc_allow_for_tier(new_tier)

    elif action == "3":
        allow_list = _permission_checklist(current_perms)
        agents_cfg["agents"][agent_name]["cc_allow"] = allow_list
        _save_agents_yaml(agents_cfg)
        ok("Saved custom cc_allow to agents.yaml")

    else:
        err("Invalid action")
        return

    # Regenerate settings.json
    if agents_dir:
        settings_file = agents_dir / agent_name / ".claude" / "settings.json"
        if settings_file.exists():
            try:
                existing = json.loads(settings_file.read_text())
            except (json.JSONDecodeError, FileNotFoundError):
                existing = {}
            existing.setdefault("permissions", {})["allow"] = allow_list
            existing.setdefault("permissions", {})["deny"] = []
            settings_file.write_text(json.dumps(existing, indent=2) + "\n")
            ok(f"Regenerated {agent_name}/.claude/settings.json")
        else:
            warn(f"No settings.json found at {settings_file} — will be created on next agent setup")


def _resolve_agents_dir() -> Path | None:
    """Find agents directory from overlay or project root."""
    overlay = os.environ.get("KBOTS_OVERLAY")
    if overlay:
        return Path(overlay) / "agents"
    return PROJECT_ROOT / "agents"


def _load_agents_yaml() -> dict:
    """Load agents.yaml from config dir."""
    path = CONFIG_DIR / "agents.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def _save_agents_yaml(data: dict):
    """Write agents.yaml back to config dir."""
    path = CONFIG_DIR / "agents.yaml"
    path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))


# ==========================================================================
# Main menu
# ==========================================================================

def manage_full_control():
    """Grant or revoke the main agent's full control of the machine."""
    header("Main Agent — Machine Control")
    agents = load_agents()
    entries = agents.get("agents", {})
    if not entries:
        warn("No agents configured yet.")
        return
    # The main agent is the first non-privileged coordinator, or just the first.
    main_id = next(iter(entries))
    for aid, cfg in entries.items():
        if cfg.get("tier") == "coordinator" or cfg.get("privileged"):
            main_id = aid
            break
    entry = entries[main_id]
    privileged = bool(entry.get("privileged"))

    show("main agent", main_id)
    show("current control", "FULL (privileged shell)" if privileged else "none (read + tools only)")
    sudoers = Path("/etc/sudoers.d/kbots-fullcontrol")
    show("passwordless sudo", "ENABLED (root)" if sudoers.exists() else "not enabled")
    print()

    print("  Options:")
    print("    1) Give full control (privileged shell — no sudo)")
    print("    2) Give full control + passwordless sudo (ROOT)")
    print("    3) Revoke full control (back to read + tools only)")
    print("    4) Revoke passwordless sudo only")
    print("    5) Back")
    print()
    warn("Changes to control level require a service restart to take effect.")

    script = PROJECT_ROOT / "scripts" / "full-control.sh"
    choice = ask("Choice", "5")
    if choice in ("5", "back", "b", ""):
        return
    if choice in ("1", "2"):
        entry["privileged"] = True
        entry.pop("disallow_builtins", None)
        entry["tier"] = "privileged"
        save_agents(agents)
        ok(f"{main_id} is now privileged (full shell). Restart to apply.")
        if choice == "2" and script.exists():
            info("Installing passwordless sudo (password required once)...")
            subprocess.run(["bash", str(script), "grant"])
    elif choice == "3":
        entry["privileged"] = False
        entry["tier"] = "coordinator"
        entry["disallow_builtins"] = ["Edit", "Write", "Bash", "MultiEdit"]
        save_agents(agents)
        if script.exists():
            subprocess.run(["bash", str(script), "revoke"])
        ok(f"{main_id} control revoked (read + tools only). Restart to apply.")
    elif choice == "4" and script.exists():
        subprocess.run(["bash", str(script), "revoke"])


# (key, label, handler, needs_cfg)
MENU_ITEMS = [
    ("1", "LLM Defaults", edit_llm, True),
    ("2", "Connectors", edit_connectors, True),
    ("3", "Session", edit_session, True),
    ("4", "Memory", edit_memory, True),
    ("5", "Security / HITL", edit_hitl, True),
    ("6", "Rate Limits", edit_rate_limits, True),
    ("7", "Compression", edit_compression, True),
    ("17", "Reply Length", edit_reply, True),
    ("8", "Admin Users", edit_admin_users, True),
    ("9", "Identity (name, log level)", edit_kbots_identity, True),
    ("10", "Agents", view_agents, False),
    ("11", "Create Agent", create_agent, False),
    ("12", "Timers (systemd)", manage_timers, False),
    ("13", "Ops Instance", create_ops_instance, False),
    ("14", "Vault Secrets", manage_vault, False),
    ("15", "Tier Permissions", manage_tier_permissions, False),
    ("16", "Main Agent — Machine Control", manage_full_control, False),
    ("q", "Quit", None, False),
]


def main():
    os.chdir(PROJECT_ROOT)
    banner()

    config_path = CONFIG_DIR / "config.yaml"
    if not config_path.exists():
        err(f"No config found at {config_path}")
        info("Run setup.py first: uv run python setup.py")
        sys.exit(1)

    while True:
        print(f"\n  {BOLD}Settings{RESET}")
        print()
        for key, label, _, _ in MENU_ITEMS:
            print(f"    {BOLD}{key:>2}){RESET} {label}")
        print()

        choice = ask("Choice", "q").lower()

        if choice in ("q", "quit", "exit"):
            print()
            ok("Done. Restart kbots to apply changes.")
            break

        for key, _, handler, needs_cfg in MENU_ITEMS:
            if choice == key and handler:
                if needs_cfg:
                    handler(load_config())
                else:
                    handler()
                break
        else:
            err(f"Unknown option: {choice}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n\n  {YELLOW}Exited.{RESET}\n")
        sys.exit(0)
