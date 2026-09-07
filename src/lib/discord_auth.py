"""Resolve which Discord bot token a tool call should use.

Every agent posts as its own Discord bot. Getting that identity wrong is not
cosmetic: a bot may only touch channels it is in, so sending into another
agent's DM with the wrong token comes back as HTTP 403 "Missing Access"
(code 50001) — which reads like a network or permission problem and is
neither.

Two defects lived here before this module existed, both only on the in-process
tool path (the MCP subprocess masked them because src/mcp_server.py pre-seeds
`active-discord-token` from KBOTS_BOT_ACCOUNT and aliases every account token
to `discord-token-<account>`):

  - With no explicit `bot=`, tools fell back to `discord-token` — the shared
    main bot — instead of the calling agent's own account.
  - With an explicit `bot=`, tools looked only at `discord-token-<account>`,
    the alias the MCP server invents. The real vault key written by config is
    `token_key` (conventionally `discord-<account>`), so the lookup missed.

Resolution is therefore: explicit bot wins; otherwise the calling agent's own
account; otherwise the session's active token; otherwise the default token.
The agent-derived step is skipped silently when that token is absent, so an
agent without its own bot account keeps the old shared-token behaviour rather
than failing.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def bot_account_for_agent(agent_id: str) -> str:
    """The Discord account `agent_id` posts as — not always its own name.

    The primary agent runs on the shared 'main' account, so account and agent
    name diverge there. Agent config carries the real account under its
    discord routing; fall back to the agent name, correct for every
    scaffolded agent.
    """
    if not agent_id:
        return ""
    overlay = os.environ.get("KBOTS_OVERLAY", "")
    if overlay:
        try:
            from src.core.agent_scaffold import agent_entries

            entry = agent_entries(Path(overlay)).get(agent_id) or {}
            account = ((entry.get("routing") or {}).get("discord") or {}).get("account")
            if account:
                return account
        except Exception:  # config unreadable — fall through to the name
            logger.debug(f"Could not read discord routing for agent '{agent_id}'", exc_info=True)
    return agent_id


def token_key_for_account(account: str) -> str:
    """The vault key holding `account`'s bot token, per connector config."""
    if not account:
        return ""
    try:
        import yaml

        from src.core.base import resolve_config_file

        cfg_file = resolve_config_file("config.yaml")
        if cfg_file.exists():
            cfg = yaml.safe_load(cfg_file.read_text()) or {}
            accounts = (((cfg.get("connectors") or {}).get("discord") or {}).get("accounts") or {})
            key = (accounts.get(account) or {}).get("token_key")
            if key:
                return key
    except Exception:
        logger.debug(f"Could not read token_key for account '{account}'", exc_info=True)
    return f"discord-{account}"


def _account_token(vault, account: str) -> str | None:
    """Token for a named account: configured key first, MCP alias second."""
    if not vault or not account:
        return None
    return vault.get(token_key_for_account(account)) or vault.get(f"discord-token-{account}")


def resolve_bot_token(vault, bot: str = "", agent_id: str = "") -> tuple[str | None, str | None]:
    """Return (token, error). Exactly one of the two is set.

    An explicit `bot` never falls back to another bot's token — a wrong-identity
    send is worse than a failed one.
    """
    if not vault:
        return None, "Error: no vault access."

    if bot:
        token = _account_token(vault, bot)
        if not token:
            return None, (
                f"Error: no Discord token for bot '{bot}' (tried vault keys "
                f"{token_key_for_account(bot)}, discord-token-{bot})."
            )
        return token, None

    # The calling agent's own bot — the identity its DM channels belong to.
    own = _account_token(vault, bot_account_for_agent(agent_id))
    if own:
        return own, None

    token = vault.get("active-discord-token") or vault.get("discord-token")
    if not token:
        return None, "Error: no Discord token available."
    return token, None
