"""Preflight checks — run once at startup, block boot on critical failures.

Each check returns (ok: bool, message: str). Critical failures prevent startup.
Warnings log but allow boot to continue.
"""

import asyncio
import logging
import os
import shutil
from pathlib import Path

from src.core.base import PROJECT_ROOT
from src.core.config_schema import validate_config

logger = logging.getLogger(__name__)


def _find_claude_bin() -> str | None:
    """Find claude binary — check PATH, then common install locations."""
    found = shutil.which("claude")
    if found:
        return found
    for candidate in [
        Path.home() / ".local" / "bin" / "claude",
        Path("/usr/local/bin/claude"),
    ]:
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _check_claude_cli() -> tuple[bool, str]:
    """Verify Claude Code CLI is installed."""
    if _find_claude_bin():
        return True, "Claude CLI found"
    return False, ("Claude CLI not found — install with: "
                   "curl -fsSL https://claude.ai/install.sh | sh")


async def _check_claude_auth() -> tuple[bool, str]:
    """Verify Claude auth works by making a minimal call."""
    claude_bin = _find_claude_bin()
    if not claude_bin:
        return False, "Claude CLI not found"
    try:
        proc = await asyncio.create_subprocess_exec(
            claude_bin, "--print", "--output-format", "json", "--model", "haiku",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=b"Reply with exactly: pong"),
            timeout=30,
        )
        if proc.returncode == 0:
            return True, "Claude auth OK"
        err = stderr.decode().strip() or stdout.decode().strip()
        if "401" in err or "authentication" in err.lower():
            return False, f"Claude auth failed (401) — run: claude setup-token\n  Detail: {err[:200]}"
        return False, f"Claude CLI error (exit {proc.returncode}): {err[:200]}"
    except asyncio.TimeoutError:
        return False, "Claude auth check timed out (30s) — network issue?"
    except FileNotFoundError:
        return False, "Claude CLI not found"


def _check_storage(db_path: str) -> tuple[bool, str]:
    """Verify database is accessible."""
    db = Path(db_path)
    if not db.parent.exists():
        return False, f"Data directory missing: {db.parent}"
    if db.exists():
        # Check it's readable and not locked
        try:
            import sqlite3
            conn = sqlite3.connect(str(db), timeout=2)
            conn.execute("SELECT 1")
            conn.close()
            return True, "Storage OK"
        except Exception as e:
            return False, f"Database error: {e}"
    # DB doesn't exist yet — that's fine, Storage.init() creates it
    return True, "Storage OK (will be created)"


def _check_agent_paths(agent_configs: dict) -> tuple[bool, str]:
    """Verify all agent project directories exist."""
    overlay = os.environ.get("KBOTS_OVERLAY")
    missing = []
    for name, cfg in agent_configs.items():
        project_dir = cfg.get("project_dir", f"./agents/{name}")
        p = Path(project_dir).resolve()
        if not p.exists() and overlay:
            p = Path(overlay) / "agents" / name
        if not p.exists():
            missing.append(f"  {name}: {p}")
    if missing:
        return False, "Agent project directories missing:\n" + "\n".join(missing)
    return True, f"Agent paths OK ({len(agent_configs)} agents)"


def _check_vault_secret(vault, secret_name: str) -> tuple[bool, str]:
    """Verify a required secret exists in the vault."""
    val = vault.get(secret_name)
    if val:
        return True, f"Vault secret '{secret_name}' present"
    return False, f"Vault secret '{secret_name}' missing — add with: python -m src.vault.fernet set {secret_name}"


def _check_file_ownership(paths: list[str], expected_user: str = "kbots") -> list[str]:
    """Check for files not owned by the expected user. Returns warnings."""
    warnings = []
    import pwd
    try:
        expected_uid = pwd.getpwnam(expected_user).pw_uid
    except KeyError:
        return []  # User doesn't exist (dev machine), skip check

    for path_str in paths:
        p = Path(path_str)
        if not p.exists():
            continue
        try:
            stat = p.stat()
            if stat.st_uid != expected_uid:
                warnings.append(f"Wrong owner on {p} — run: sudo chown -R {expected_user}:{expected_user} {p}")
        except OSError:
            pass
    return warnings


def _running_user() -> tuple[int, str]:
    """The uid + name the service actually runs as (the owner everything should have)."""
    uid = os.geteuid()
    try:
        import pwd
        return uid, pwd.getpwuid(uid).pw_name
    except (KeyError, ImportError):
        return uid, str(uid)


def _check_permissions(config: dict) -> list[str]:
    """Rights/permissions preflight — the things that silently break agents.

    OS-agnostic (checks against whoever the service runs as, not a hardcoded user).
    Returns a list of plain-language warnings, each ending with the exact fix command.
    Empty list = all good.
    """
    uid, user = _running_user()
    warns: list[str] = []

    # 1) Claude Code config. Headless Claude Code IGNORES an untrusted workspace's
    #    tool allow-list, and the engine marks a workspace trusted by writing
    #    ~/.claude.json. If that file (or ~/.claude) is owned by someone else — e.g.
    #    a `claude` session run as root — the service can't write it, the workspace
    #    stays untrusted, and EVERY tool is denied ("unapproved-permission").
    cfg_env = os.environ.get("CLAUDE_CONFIG_DIR")
    claude_json = (Path(cfg_env) / ".claude.json") if cfg_env else Path.home() / ".claude.json"
    claude_dir = Path(cfg_env) if cfg_env else Path.home() / ".claude"
    for p in (claude_json, claude_dir, claude_dir / "settings.json"):
        try:
            if p.exists() and (p.stat().st_uid != uid or not os.access(p, os.W_OK)):
                warns.append(
                    f"{p} is not owned/writable by '{user}' — the service can't manage its "
                    f"Claude Code config, which breaks headless workspace-trust and can leave "
                    f"agents unable to use ANY tools. "
                    f"Fix: sudo chown -R {user} {claude_json} {claude_dir}")
                break  # one message covers the pair
        except OSError:
            pass

    # 2) Agent workspaces: must be writable, and should carry the tool allow-list
    #    (.claude/settings.json) that grants the agent its tools.
    overlay = os.environ.get("KBOTS_OVERLAY")
    for agent_id, ac in (config.get("agents", {}) or {}).items():
        pd = ac.get("project_dir")
        if not pd:
            continue
        p = Path(pd)
        if not p.is_dir() and overlay and (Path(overlay) / "agents" / agent_id).is_dir():
            p = Path(overlay) / "agents" / agent_id
        if not p.is_dir():
            continue
        if not os.access(p, os.W_OK):
            warns.append(f"Agent '{agent_id}' workspace isn't writable by '{user}': {p}. "
                         f"Fix: sudo chown -R {user} {p}")
        elif not (p / ".claude" / "settings.json").exists():
            warns.append(f"Agent '{agent_id}' has no .claude/settings.json tool allow-list — "
                         f"it will run with a restricted tool set (re-run setup for this agent).")
    return warns


def _local_models_in_use(config: dict) -> list[str]:
    """Model names the config expects a local runtime to serve ([] = not in use)."""
    llm = (config.get("defaults", {}) or {}).get("llm", {}) or {}
    models: list[str] = []
    router = llm.get("router") or {}
    if router.get("enabled"):
        models += [router.get("router_model"), router.get("local_model")]
    for cfg in (config.get("agents", {}) or {}).values():
        a_llm = cfg.get("llm") or {}
        if a_llm.get("provider") == "local":
            models.append(a_llm.get("model") or (llm.get("local") or {}).get("model"))
        a_router = a_llm.get("router") or {}
        if a_router.get("enabled"):
            models += [a_router.get("router_model") or router.get("router_model"),
                       a_router.get("local_model") or router.get("local_model")]
    return sorted({m for m in models if m})


def _check_local_models(config: dict) -> list[str]:
    """Local-model preflight: runtime reachable + configured models downloaded.

    Non-blocking — the tier router fails open to Claude when the runtime is
    down, so these are warnings, each with the exact fix command.
    """
    models = _local_models_in_use(config)
    if not models:
        return []
    import json
    import urllib.request

    base = ((config.get("defaults", {}) or {}).get("llm", {}) or {}).get("local", {}) or {}
    urls = ([base["base_url"].rstrip("/")] if base.get("base_url")
            else ["http://localhost:11434/v1", "http://localhost:1234/v1"])
    available: set[str] | None = None
    for url in urls:
        try:
            with urllib.request.urlopen(f"{url}/models", timeout=3) as resp:
                data = json.loads(resp.read())
                available = {m.get("id", "") for m in data.get("data", [])}
                break
        except Exception:
            continue
    if available is None:
        return ["local models are configured but no runtime is reachable — start "
                "Ollama (`ollama serve`) or LM Studio. Agents fall back to Claude "
                "until then."]
    missing = [m for m in models if m not in available]
    if missing and (base.get("auto_pull", True)):
        # Cache-not-install: the provider pulls missing models on first use, so
        # this is informational, not a problem (first use is just slower).
        logger.info(f"  local models not yet downloaded (auto-pull on first use): "
                    f"{', '.join(missing)}")
        return []
    return [f"local model '{m}' is configured but not downloaded — run: ollama pull {m}"
            for m in missing]


async def run_preflight(config: dict, vault, storage_path: str) -> bool:
    """Run all preflight checks. Returns True if OK to proceed.

    Critical failures (auth, storage, paths) block startup.
    Warnings (ownership) log but allow boot.
    """
    logger.info("Running preflight checks...")
    passed = 0
    failed = 0
    warnings = 0

    # --- Config schema validation (missing required keys + wrong types) ---
    # Unknown-key warnings are emitted at DEBUG only — deployments legitimately
    # add custom sections; treating those as boot-time noise is wrong.
    schema_errors, schema_warnings = validate_config(config)
    for e in schema_errors:
        logger.error(f"  ✗ config_schema: {e}")
        failed += 1
    for w in schema_warnings:
        logger.debug(f"  config_schema (unknown key): {w}")
    if not schema_errors:
        logger.info("  ✓ config_schema: OK")
        passed += 1

    # --- Critical checks ---
    checks: list[tuple[str, tuple[bool, str]]] = []

    # CLI exists
    ok, msg = _check_claude_cli()
    checks.append(("claude_cli", (ok, msg)))

    # Auth works (async) — warn only, don't block startup
    ok, msg = await _check_claude_auth()
    if ok:
        logger.info(f"  ✓ claude_auth: {msg}")
        passed += 1
    else:
        logger.warning(f"  ⚠ claude_auth: {msg}")
        warnings += 1

    # Storage
    ok, msg = _check_storage(storage_path)
    checks.append(("storage", (ok, msg)))

    # Agent paths
    agent_configs = config.get("agents", {})
    ok, msg = _check_agent_paths(agent_configs)
    checks.append(("agent_paths", (ok, msg)))

    # Discord tokens in vault — check each configured account
    connectors = config.get("connectors", {})
    discord_cfg = connectors.get("discord", {})
    if discord_cfg.get("enabled"):
        accounts = discord_cfg.get("accounts", {})
        if accounts:
            token_specs = [(f"discord_{n}", c.get("token_key", f"DISCORD_TOKEN_{n.upper()}"))
                           for n, c in accounts.items()]
        else:
            token_specs = [("discord_token", discord_cfg.get("token_key", "DISCORD_BOT_TOKEN"))]
        for name, token_key in token_specs:
            ok, msg = _check_vault_secret(vault, token_key)
            if ok:
                checks.append((name, (ok, msg)))
            else:
                # Non-fatal: a missing / not-yet-provisioned / rotated token must NOT
                # abort the whole boot. The connector already skips a tokenless bot and
                # keeps the others running, so preflight matches that — one bad token
                # can't wedge the entire service.
                logger.warning(f"  ⚠ {name}: {msg} — skipping this bot until its token is set")
                warnings += 1

    # Log results
    for name, (ok, msg) in checks:
        if ok:
            logger.info(f"  ✓ {name}: {msg}")
            passed += 1
        else:
            logger.error(f"  ✗ {name}: {msg}")
            failed += 1

    # --- Warnings (non-blocking) ---
    ownership_warnings = _check_file_ownership([
        str(PROJECT_ROOT / "data"),
        str(PROJECT_ROOT / "agents"),
        str(PROJECT_ROOT / "config"),
        str(PROJECT_ROOT / ".venv"),
    ])
    for w in ownership_warnings:
        logger.warning(f"  ⚠ ownership: {w}")
        warnings += 1

    # Rights/permissions that silently break agents (Claude config + workspaces).
    # Non-blocking but loud, each with the exact fix command.
    perm_warnings = _check_permissions(config)
    for w in perm_warnings:
        logger.warning(f"  ⚠ permissions: {w}")
        warnings += 1
    if not perm_warnings:
        logger.info("  ✓ permissions: Claude config + agent workspaces writable")
        passed += 1

    # Local models (only when configured): runtime up + models downloaded.
    local_warnings = _check_local_models(config)
    for w in local_warnings:
        logger.warning(f"  ⚠ local_models: {w}")
        warnings += 1
    if _local_models_in_use(config) and not local_warnings:
        logger.info("  ✓ local_models: runtime reachable, configured models present")
        passed += 1

    # Summary
    total = passed + failed
    if failed:
        logger.error(f"Preflight FAILED: {passed}/{total} passed, {failed} critical failure(s)")
        return False

    suffix = f", {warnings} warning(s)" if warnings else ""
    logger.info(f"Preflight passed: {total}/{total} checks OK{suffix}")
    return True
