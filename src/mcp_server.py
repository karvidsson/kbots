"""kbots MCP Tool Server — exposes Python tools to Claude Code agents.

Runs as a stdio MCP server using the official MCP SDK (FastMCP).
Claude Code connects via .mcp.json config.

Middleware chain per tool call: rate_limit → hitl → execute → audit

Usage:
    python -m src.mcp_server

Protocol: MCP (Model Context Protocol) over stdio
"""

import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
import urllib.parse
import uuid
from pathlib import Path

import aiohttp
from mcp.server.fastmcp import FastMCP

from src.core.alerts import WEB_FACING_TOOLS, AlertSender
from src.core.audit import AuditLog
from src.core.base import PROJECT_ROOT, ToolContext, ToolDef, resolve_vault_key_file
from src.core.content_safety import sanitize, score_injection
from src.core.rate_limiter import RateLimiter
from src.core.registry import Registry
from src.core.storage import resolve_db_path
from src.core.tools import get_all_tools
from src.vault.fernet import FernetVault

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
    stream=sys.stderr,  # MCP uses stdout for protocol, stderr for logs
)
logger = logging.getLogger("mcp-server")


FALLBACK_AGENT_ID = "mcp-agent"


def _resolve_agent_id() -> str:
    """Derive agent ID from environment. Falls back to 'mcp-agent'."""
    agent_id = os.environ.get("KBOTS_AGENT_ID")
    if agent_id:
        return agent_id
    project_dir = os.environ.get("KBOTS_PROJECT_DIR", "")
    if project_dir:
        return Path(project_dir).name
    # Identity collapse: every agent running without these env vars shares
    # this one ID, which defeats per-agent tool scoping and attribution.
    logger.warning(
        "No KBOTS_AGENT_ID or KBOTS_PROJECT_DIR in env — falling back to "
        f"shared identity '{FALLBACK_AGENT_ID}'. Fix the agent's .mcp.json "
        "(regenerate via install_mcp, or add KBOTS_AGENT_ID to the "
        "kbots-tools env block)."
    )
    return FALLBACK_AGENT_ID


MCP_AGENT_ID = _resolve_agent_id()


# --- HITL via Discord API (MCP server can't use connector directly) ---

class MCPHitlGate:
    """HITL approval via Discord API. Separate from the connector-based HITL
    in src/core/hitl.py because MCP runs in a subprocess without connector access."""

    def __init__(self, config: dict, vault: FernetVault, admin_users: list | None = None):
        self.vault = vault
        self._config_channel = config.get("channel")
        # Empty approvers must NOT mean "anyone can approve" (that would let any
        # channel member green-light create_tool / install_mcp). Fall back to the
        # configured admin users; if there are none either, nobody can approve and
        # every gated call fails closed (times out → denied).
        self.approvers = set(config.get("approvers") or []) or set(admin_users or [])
        self.timeout = config.get("timeout", 1800)
        self.poll_interval = config.get("poll_interval", 3)
        self.fail_mode = config.get("fail_mode", "closed")
        self.gated_tools = set(config.get("gated_tools", []))

    @property
    def channel_id(self):
        """Approval channel — the set_hitl_channel runtime override wins over
        config (runtime_state is a shared overlay file, so this subprocess sees
        an admin's live change without a restart)."""
        from src.core import runtime_state
        return runtime_state.get_flag("hitl_channel") or self._config_channel

    def is_gated(self, tool_name: str) -> bool:
        if tool_name in self.gated_tools:
            return True
        for pattern in self.gated_tools:
            if pattern.endswith("*") and tool_name.startswith(pattern[:-1]):
                return True
        return False

    async def request_approval(self, tool_name: str, args: dict) -> dict:
        # HITL always uses the main kbots bot token (ops channel on production server)
        token = self.vault.get("discord-token")  # production kbots bot — always has access to ops channel
        if not token or not self.channel_id:
            if self.fail_mode == "open":
                logger.warning(f"HITL: no channel/token for '{tool_name}' — fail_mode=open, auto-approving")
                return {"status": "approved", "reason": "no channel, fail_mode=open"}
            logger.warning(
                f"HITL: '{tool_name}' requires approval but no HITL channel/token is configured — "
                "denying (fail-closed). Set security.hitl.channel to enable approvals."
            )
            return {"status": "denied", "reason": "no discord token or HITL channel"}

        hitl_id = str(uuid.uuid4())[:8]
        args_display = _redact_for_display(args)

        msg_text = (
            f"**HITL Approval Required**\n"
            f"Tool: `{tool_name}`\n"
            f"Args: `{args_display}`\n"
            f"ID: `{hitl_id}`\n\n"
            f"React with checkmark to approve or X to deny."
        )

        headers = {
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (https://github.com/karvidsson/kbots, 1.0)",
        }
        api = "https://discord.com/api/v10"

        try:
            return await asyncio.wait_for(
                self._poll_approval(api, headers, msg_text, tool_name, hitl_id),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(f"HITL {hitl_id}: timeout after {self.timeout}s")
            if self.fail_mode == "open":
                logger.warning(f"HITL {hitl_id}: fail_mode=open — auto-approving on timeout")
                return {"status": "approved", "reason": "timeout, fail_mode=open", "hitl_id": hitl_id}
            return {"status": "timeout", "hitl_id": hitl_id}
        except Exception as e:
            logger.error(f"HITL error: {e}", exc_info=True)
            if self.fail_mode == "closed":
                return {"status": "denied", "reason": str(e)}
            return {"status": "approved", "reason": f"error, fail_mode=open: {e}"}

    async def _poll_approval(self, api: str, headers: dict, msg_text: str,
                             tool_name: str, hitl_id: str) -> dict:
        """Post HITL message and poll for reactions. Called within wait_for timeout."""
        async with aiohttp.ClientSession() as session:
            # Post message
            async with session.post(
                f"{api}/channels/{self.channel_id}/messages",
                headers=headers,
                json={"content": msg_text},
            ) as resp:
                if resp.status != 200:
                    logger.error(f"HITL post failed: {resp.status}")
                    if self.fail_mode == "closed":
                        return {"status": "denied", "reason": "failed to post"}
                    return {"status": "approved", "reason": "post failed, fail_mode=open"}
                msg_data = await resp.json()
                message_id = msg_data["id"]

            # Add reactions
            for emoji in ["\u2705", "\u274c"]:
                emoji_encoded = urllib.parse.quote(emoji)
                r = await session.put(
                    f"{api}/channels/{self.channel_id}/messages/{message_id}/reactions/{emoji_encoded}/@me",
                    headers=headers,
                )
                if r.status not in (200, 204):
                    logger.warning(f"HITL add reaction failed: {r.status} {emoji}")

            # Poll for reactions (timeout handled by asyncio.wait_for in caller)
            while True:
                await asyncio.sleep(self.poll_interval)

                for emoji, status in [("\u2705", "approved"), ("\u274c", "denied")]:
                    emoji_encoded = urllib.parse.quote(emoji)
                    react_url = f"{api}/channels/{self.channel_id}/messages/{message_id}/reactions/{emoji_encoded}"
                    async with session.get(react_url, headers=headers) as resp:
                        if resp.status != 200:
                            continue
                        reactors = await resp.json()
                        for reactor in reactors:
                            if reactor.get("bot"):
                                continue
                            reactor_id = str(reactor["id"])
                            if reactor_id in self.approvers:
                                logger.info(f"HITL {hitl_id}: {status} by {reactor_id}")
                                result_icon = "Approved" if status == "approved" else "Denied"
                                await session.post(
                                    f"{api}/channels/{self.channel_id}/messages",
                                    headers=headers,
                                    json={"content": f"{result_icon}: `{tool_name}` {status} by <@{reactor_id}>"},
                                )
                                return {"status": status, "approver": reactor_id, "hitl_id": hitl_id}


def _redact_for_display(args: dict) -> str:
    sensitive = {"secret", "token", "key", "password", "passphrase", "credential"}
    safe = {k: ("[REDACTED]" if any(s in k.lower() for s in sensitive) else v)
            for k, v in args.items()}
    return json.dumps(safe)[:300]


# --- Tool log (SQLite) ---

def _init_tool_log(data_dir: str) -> "sqlite3.Connection | None":
    """Open a synchronous SQLite connection for tool logging."""
    import atexit
    import os
    import signal
    import sqlite3
    os.makedirs(data_dir, exist_ok=True)
    db_path = str(resolve_db_path(data_dir))
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tool_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                session_id TEXT,
                tool_name TEXT NOT NULL,
                input TEXT,
                output TEXT,
                success INTEGER,
                duration_ms INTEGER,
                created_at REAL DEFAULT (unixepoch())
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_log_agent ON tool_log(agent_id, created_at)")
        conn.commit()

        def _close_tool_log():
            try:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                conn.close()
            except Exception:
                pass

        atexit.register(_close_tool_log)

        def _signal_exit(signum, frame):
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, _signal_exit)

        logger.info("Tool log database ready")
        return conn
    except Exception as e:
        logger.error(f"Could not open tool log DB: {e}")
        return None


_TOOL_LOG_MAX_RETRIES = 3

def _log_tool_to_db(conn, agent_id: str, tool_name: str, input_data: dict | None,
                    output: str | None, success: bool, duration_ms: int) -> None:
    """Write a tool call to the SQLite tool_log table. Never fails the caller."""
    if not conn:
        return
    import sqlite3
    for attempt in range(_TOOL_LOG_MAX_RETRIES):
        try:
            conn.execute(
                "INSERT INTO tool_log (agent_id, session_id, tool_name, input, output, success, duration_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (agent_id, None, tool_name,
                 json.dumps(input_data)[:2000] if input_data else None,
                 output[:2000] if output else None,
                 int(success), duration_ms),
            )
            conn.commit()
            return
        except sqlite3.OperationalError as e:
            if "locked" in str(e) and attempt < _TOOL_LOG_MAX_RETRIES - 1:
                import time
                time.sleep(0.1 * (attempt + 1))
                continue
            logger.debug(f"Tool log write failed (attempt {attempt + 1}): {e}")
            return
        except Exception as e:
            logger.debug(f"Tool log write failed: {e}")
            return


# --- Build the MCP server ---

def build_server(vault: FernetVault, config: dict) -> FastMCP:
    """Build a FastMCP server with all tools from the registry, wrapped in middleware."""

    mcp = FastMCP(
        "kbots-tools",
        instructions=("kbots tool server. Tools for memory, team, productivity, "
                      "communication, media, web search, system management, and more."),
    )

    # Auto-discover all @tool functions via registry
    registry = Registry()
    registry.discover()
    kbots_tools = get_all_tools()
    logger.info(f"Discovered {len(kbots_tools)} tools from registry")

    # Initialize memory backend.
    # Defaults to sqlite if no backend is specified — this matches main.py's
    # auto-discovery behaviour and prevents memory tools from silently failing
    # with "Memory backend not configured for this agent" when config.yaml
    # forgets to set defaults.memory.backend explicitly.
    memory = None
    defaults = config.get("defaults", {})
    mem_cfg = defaults.get("memory", {})
    backend_name = mem_cfg.get("backend", "sqlite")
    if backend_name == "sqlite":
        import atexit as _atexit

        from src.memory.sqlite import SQLiteMemory
        memory = SQLiteMemory(config=mem_cfg)
        logger.info("Memory backend: SQLite (inline)")

        def _close_memory_db():
            try:
                memory.db.execute("PRAGMA wal_checkpoint(PASSIVE)")
                memory.db.close()
            except Exception:
                pass
        _atexit.register(_close_memory_db)
    elif backend_name:
        logger.warning(
            f"Unknown memory backend '{backend_name}' in config — "
            f"memory tools will be unavailable to agents."
        )

        # Note: team module reads from team.json directly, no memory backend needed

    # Graph memory is single-writer (one process per .lbdb file) and owned by the
    # main kbots process — never open it from this subprocess. Forward graph
    # calls over the loopback internal API instead (GraphClient); without the
    # loopback env (standalone MCP run) it stays unavailable with a clear reason.
    from src.lib.graph_store import init_graph_client, set_unavailable
    if not init_graph_client():
        set_unavailable(
            "Graph memory lives in the main kbots process (single-writer database) "
            "and this MCP server has no loopback connection to it — start the MCP "
            "server from a running kbots instance to use graph tools."
        )

    # Initialize middleware
    security_cfg = config.get("security", {})

    hitl_cfg = security_cfg.get("hitl", {})
    # Always construct the gate — even with no channel configured it must be able
    # to fail closed on gated / hitl=True tools rather than letting them run
    # unapproved. Empty approvers fall back to admin_users (see MCPHitlGate).
    admin_users = (config.get("admin_users", {}) or {}).get("discord", [])
    hitl = MCPHitlGate(hitl_cfg, vault, admin_users=admin_users)
    if not hitl_cfg.get("channel"):
        logger.warning(
            "HITL: no security.hitl.channel configured — gated and hitl=True tools "
            "will be denied (fail-closed) on the MCP path until a channel is set."
        )

    rl_cfg = security_cfg.get("rate_limits", {})
    rate_limiter = RateLimiter(rl_cfg) if rl_cfg else None

    audit = AuditLog(config.get("kbots", {}).get("data_dir", "./data") + "/audit.jsonl")

    # Tool log — write to SQLite for queryable tool history
    data_dir = config.get("kbots", {}).get("data_dir", "./data")
    tool_log_db = _init_tool_log(data_dir)

    # Security alerts — send to configured Discord channel
    alerter = AlertSender(config, vault)
    if alerter.enabled:
        logger.info(f"Security alerts: channel {alerter.channel_id}")
    else:
        logger.info("Security alerts: logger only (no alert_channel configured)")

    # Register each kbots tool as an MCP tool with middleware wrapping
    for tool_name, tool_def in kbots_tools.items():
        _register_tool(mcp, tool_def, vault, hitl, rate_limiter, audit,
                       memory=memory, tool_log_db=tool_log_db, alerter=alerter)

    return mcp


def _make_middleware_handler(
    tool_def: ToolDef,
    vault: FernetVault,
    hitl: MCPHitlGate | None,
    rate_limiter: RateLimiter | None,
    audit: AuditLog | None,
    memory=None,
    tool_log_db=None,
    alerter: AlertSender | None = None,
):
    """Create a middleware-wrapped handler for a tool.

    Returns a callable that FastMCP will use. The function gets a proper
    signature dynamically so FastMCP can extract the JSON schema, but
    internally delegates to the original @tool function with middleware.
    """
    _td = tool_def
    _name = tool_def.name

    async def _execute(**kwargs) -> str:
        """Middleware chain: rate_limit -> hitl -> execute -> audit."""
        start_time = time.time()

        # --- Rate limit check ---
        if rate_limiter:
            rl_check = rate_limiter.check(MCP_AGENT_ID, _name)
            if not rl_check["allowed"]:
                if audit:
                    audit.log_rate_limit(
                        MCP_AGENT_ID, _name,
                        rl_check.get("count", 0),
                        rl_check.get("limit", 0),
                        rl_check.get("window", ""),
                    )
                return f"Rate limit exceeded: {rl_check['reason']}"

        # --- HITL gate ---
        # A tool needs approval if config lists it (is_gated) OR it declares
        # hitl=True on its @tool decorator. The decorator flag was previously
        # ignored here, so tools like create_tool ran with no approval despite
        # advertising "[HITL-GATED]". `hitl` is always constructed (even with no
        # channel) so request_approval can fail closed.
        hitl_result = None
        if hitl and (hitl.is_gated(_name) or _td.hitl):
            logger.info(f"HITL gate: {_name} requires approval")
            hitl_result = await hitl.request_approval(_name, kwargs)
            if hitl_result["status"] != "approved":
                if audit:
                    audit.log_hitl(
                        hitl_result.get("hitl_id", "?"), MCP_AGENT_ID, _name,
                        hitl_result["status"], hitl_result.get("approver"),
                    )
                from src.core.hitl import hitl_result_message
                return hitl_result_message(_name, hitl_result)

        # Record for rate limiting BEFORE execution (prevents TOCTOU race)
        if rate_limiter:
            rate_limiter.record(MCP_AGENT_ID, _name)

        # --- Execute tool ---
        _project_dir_raw = os.environ.get("KBOTS_PROJECT_DIR")
        _project_dir = str(Path(_project_dir_raw).resolve()) if _project_dir_raw else None
        # Conversation context injected by the engine into the CLI env (inherited
        # by this stdio child) so channel-aware tools (schedule_task, triggers,
        # send_message) know where they're running.
        ctx = ToolContext(agent_id=MCP_AGENT_ID, vault=vault, memory=memory,
                          project_dir=_project_dir,
                          channel_id=os.environ.get("KBOTS_CHANNEL_ID") or None,
                          user_id=os.environ.get("KBOTS_USER_ID") or None)
        try:
            result = await _td.func(ctx, **kwargs)
            duration_ms = int((time.time() - start_time) * 1000)

            # Audit log
            if audit:
                audit.log_tool(
                    MCP_AGENT_ID, _name, kwargs,
                    str(result)[:200] if result else "", True, duration_ms,
                    hitl=({"status": hitl_result["status"], "approver": hitl_result.get("approver")}
                          if hitl_result else None),
                )

            # SQLite tool log
            if tool_log_db:
                _log_tool_to_db(tool_log_db, MCP_AGENT_ID, _name, kwargs,
                                str(result)[:2000] if result else "", True, duration_ms)

            logger.info(f"Tool {_name} completed in {duration_ms}ms")

            # --- Content safety scan for web-facing tools ---
            result_str = str(result) if result is not None else "(no output)"
            if _name in WEB_FACING_TOOLS and result_str:
                score, matched = score_injection(result_str)
                if score >= 3:
                    alert_msg = (
                        f"\u26a0\ufe0f **Content Safety Alert**\n"
                        f"Tool: `{_name}`\n"
                        f"Injection score: **{score}/10**\n"
                        f"Patterns: {', '.join(matched[:5])}"
                    )
                    logger.warning(f"Injection detected in {_name} output: score={score} patterns={matched}")
                    if audit:
                        audit.log_content_safety(MCP_AGENT_ID, score, _name, matched)
                    if alerter:
                        alerter.send_bg(alert_msg)

                # Sanitization — log-only mode: alert on what would be stripped
                # but pass original content through to the LLM unchanged.
                # Flip to active stripping later once false-positive rate is known.
                sanitized = sanitize(result_str)
                chars_removed = len(result_str) - len(sanitized)
                if chars_removed > 50:
                    logger.info(f"Sanitize check on {_name}: {chars_removed} chars would be removed")
                    if alerter:
                        alerter.send_bg(
                            f"\U0001f9f9 **Sanitize Check (log-only)**\n"
                            f"Tool: `{_name}`\n"
                            f"Would remove: **{chars_removed}** chars (hidden/invisible content)"
                        )

            return result_str

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            logger.error(f"Tool {_name} failed: {e}", exc_info=True)
            if audit:
                audit.log_tool(MCP_AGENT_ID, _name, kwargs, str(e), False, duration_ms)
            if tool_log_db:
                _log_tool_to_db(tool_log_db, MCP_AGENT_ID, _name, kwargs,
                                str(e)[:2000], False, duration_ms)
            raise

    return _execute


def _register_tool(
    mcp: FastMCP,
    tool_def: ToolDef,
    vault: FernetVault,
    hitl: MCPHitlGate | None,
    rate_limiter: RateLimiter | None,
    audit: AuditLog | None,
    memory=None,
    tool_log_db=None,
    alerter: AlertSender | None = None,
) -> None:
    """Register a single kbots tool as an MCP tool with full middleware chain.

    Uses Tool.from_function on the ORIGINAL @tool function to get proper
    schema extraction from type hints. Then swaps the fn to the middleware
    wrapper so execution goes through the middleware chain.
    """
    from mcp.server.fastmcp.tools import Tool as MCPTool

    # Build description
    desc = tool_def.description
    if tool_def.hitl or (hitl and hitl.is_gated(tool_def.name)):
        desc = f"[HITL-GATED] {desc}"

    # Create the middleware handler and give it the same signature as the
    # original function (minus 'ctx') so FastMCP extracts the right schema.
    handler = _make_middleware_handler(tool_def, vault, hitl, rate_limiter, audit,
                                      memory=memory, tool_log_db=tool_log_db, alerter=alerter)

    # Copy the original function's signature minus 'ctx' onto the handler.
    # FastMCP.from_function will introspect this to build the JSON schema.
    import inspect
    orig_sig = inspect.signature(tool_def.func)
    new_params = [p for name, p in orig_sig.parameters.items() if name != "ctx"]
    handler.__signature__ = orig_sig.replace(parameters=new_params)
    handler.__name__ = tool_def.name
    handler.__doc__ = desc
    # Copy type hints minus 'ctx'
    orig_hints = getattr(tool_def.func, "__annotations__", {})
    handler.__annotations__ = {k: v for k, v in orig_hints.items() if k != "ctx"}

    mcp_tool = MCPTool.from_function(
        handler,
        name=tool_def.name,
        description=desc,
    )

    # Register directly in the tool manager
    mcp._tool_manager._tools[tool_def.name] = mcp_tool
    logger.debug(f"Registered MCP tool: {tool_def.name}")


# --- Entry point ---

def main():
    """Load vault and config, build MCP server, run on stdio."""
    import os

    import yaml

    # Load config — respect layers: overlay → modules → core
    profile = os.environ.get("KBOTS_PROFILE", "")
    logger.info(f"MCP server cwd: {os.getcwd()}, KBOTS_PROFILE={profile!r}")
    project_root = PROJECT_ROOT

    # Build config search dirs (same layer order as main.py)
    config_dirs: list[Path] = []
    overlay = os.environ.get("KBOTS_OVERLAY")
    if overlay:
        p = Path(overlay) / "config"
        if p.is_dir():
            config_dirs.append(p)
    modules_raw = os.environ.get("KBOTS_MODULES", "")
    for mod_path in modules_raw.split(":"):
        if not mod_path.strip():
            continue
        p = Path(mod_path.strip()) / "config"
        if p.is_dir():
            config_dirs.append(p)
    config_dirs.append(project_root / "config")

    def _find_cfg(name: str) -> Path | None:
        for d in config_dirs:
            f = d / name
            if f.exists():
                return f
        return None

    suffix = f".{profile}" if profile else ""
    config_file = _find_cfg(f"config{suffix}.yaml") or _find_cfg("config.yaml")

    config = {}
    if config_file and config_file.exists():
        with open(config_file) as f:
            config = yaml.safe_load(f) or {}
    logger.info(f"MCP server config: {config_file}")

    # Load vault — resolve from config dirs
    vault_file = _find_cfg("secrets.enc")
    vault_path = vault_file if vault_file else project_root / "config/secrets.enc"
    vault = FernetVault(str(vault_path))

    if vault_path.exists():
        key_file = resolve_vault_key_file()
        if key_file.exists():
            passphrase = key_file.read_text().strip()
        else:
            import getpass
            passphrase = getpass.getpass("Vault passphrase: ")
        vault.unlock(passphrase)
        del passphrase
    else:
        vault.unlock_from_env()

    # Store all bot tokens in vault so discord tools can send as any bot
    discord_cfg = config.get("connectors", {}).get("discord", {})
    accounts = discord_cfg.get("accounts", {})
    if accounts:
        for account_name, account_cfg in accounts.items():
            token_key = account_cfg.get("token_key", f"discord-token-{account_name}")
            token = vault.get(token_key)
            if token:
                vault._secrets[f"discord-token-{account_name}"] = token
                logger.info(f"Discord token stored for bot: {account_name}")
        # Use KBOTS_BOT_ACCOUNT env var to set active token for this agent's session
        bot_account = os.environ.get("KBOTS_BOT_ACCOUNT", "")
        if bot_account and bot_account in accounts:
            token_key = accounts[bot_account].get("token_key", f"discord-token-{bot_account}")
            active_token = vault.get(token_key)
            if active_token:
                vault._secrets["active-discord-token"] = active_token
                logger.info(f"Active discord token set from KBOTS_BOT_ACCOUNT={bot_account}")
        else:
            # Fallback: first account (backward compat)
            first_account = next(iter(accounts.values()))
            token_key = first_account.get("token_key", "discord-token")
            active_token = vault.get(token_key)
            if active_token:
                vault._secrets["active-discord-token"] = active_token
                logger.info("Active discord token set from first account (no KBOTS_BOT_ACCOUNT)")

    # Build and run
    server = build_server(vault, config)
    logger.info(f"kbots MCP server starting (stdio) — {len(server._tool_manager._tools)} tools")
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
