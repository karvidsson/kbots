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

The shared-token fallback applies only to an agent the deployment has NOT
configured a Discord account for. When an account IS configured and its token
is missing, resolution fails closed: posting under some other agent's identity
is a worse outcome than not posting, and it is the failure this module exists
to stop.
"""

import logging
import os
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger(__name__)


class BotAuth(NamedTuple):
    """A resolved sender. `account` is the identity the token really belongs
    to, empty when only a shared token was available and nothing states whose
    it is — callers must report it as unknown rather than guess."""

    token: str | None
    account: str
    error: str | None


def _configured_accounts() -> dict:
    """The `connectors.discord.accounts` map, or empty if unreadable."""
    try:
        import yaml

        from src.core.base import resolve_config_file

        cfg_file = resolve_config_file("config.yaml")
        if cfg_file.exists():
            cfg = yaml.safe_load(cfg_file.read_text()) or {}
            return (((cfg.get("connectors") or {}).get("discord") or {}).get("accounts") or {})
    except Exception:
        logger.debug("Could not read discord connector accounts", exc_info=True)
    return {}


def own_account_for_agent(agent_id: str) -> tuple[str, bool]:
    """(account, configured) for `agent_id`.

    `configured` means the deployment states this agent posts as that account —
    either its discord routing names one, or the account exists in connector
    config. That flag is what separates "this agent has no bot of its own, use
    the shared token" from "this agent has a bot and its token is missing",
    which must not silently borrow another identity.
    """
    if not agent_id:
        return "", False
    overlay = os.environ.get("KBOTS_OVERLAY", "")
    if overlay:
        try:
            from src.core.agent_scaffold import agent_entries

            entry = agent_entries(Path(overlay)).get(agent_id) or {}
            account = ((entry.get("routing") or {}).get("discord") or {}).get("account")
            if account:
                return account, True
        except Exception:  # config unreadable — fall through to the name
            logger.debug(f"Could not read discord routing for agent '{agent_id}'", exc_info=True)
    return agent_id, agent_id in _configured_accounts()


def bot_account_for_agent(agent_id: str) -> str:
    """The Discord account `agent_id` posts as — not always its own name.

    The primary agent runs on the shared 'main' account, so account and agent
    name diverge there. Agent config carries the real account under its
    discord routing; fall back to the agent name, correct for every
    scaffolded agent.
    """
    return own_account_for_agent(agent_id)[0]


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


def resolve_bot_token(vault, bot: str = "", agent_id: str = "") -> BotAuth:
    """Which bot a call authenticates as. Exactly one of token/error is set.

    Neither an explicit `bot` nor a configured own account ever falls back to
    another bot's token — a wrong-identity send is worse than a failed one.
    """
    if not vault:
        return BotAuth(None, "", "Error: no vault access.")

    if bot:
        token = _account_token(vault, bot)
        if not token:
            return BotAuth(None, bot, (
                f"Error: no Discord token for bot '{bot}' (tried vault keys "
                f"{token_key_for_account(bot)}, discord-token-{bot})."
            ))
        return BotAuth(token, bot, None)

    # The calling agent's own bot — the identity its DM channels belong to.
    account, configured = own_account_for_agent(agent_id)
    own = _account_token(vault, account)
    if own:
        return BotAuth(own, account, None)
    if configured:
        return BotAuth(None, account, (
            f"Error: agent '{agent_id}' is configured to post as bot '{account}' "
            f"but no token for it is in the vault (tried {token_key_for_account(account)}, "
            f"discord-token-{account}). Refusing to send as a different bot."
        ))

    # No bot of its own: the shared session token, whose identity is only known
    # when the process was launched for a specific account.
    active = vault.get("active-discord-token")
    if active:
        return BotAuth(active, os.environ.get("KBOTS_BOT_ACCOUNT", ""), None)
    token = vault.get("discord-token")
    if token:
        return BotAuth(token, "", None)
    return BotAuth(None, "", "Error: no Discord token available.")
