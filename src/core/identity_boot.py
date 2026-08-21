"""First-boot identity reconciliation — let an agent own its Discord account name.

The Discord account is named in the developer portal, at the moment the token is
created. Nothing ever reconciled that name with the agent's configuration, so an
account could keep the name it was registered under while its config called it
something else, and the agent had no way to correct it: `discord_set_nickname`
sets a per-server nickname only, and no tool touched the account username at all.

On boot the engine compares each agent's live account name with its configured
one and, on a mismatch, hands that agent ONE synthetic turn asking it to fix its
own identity. The engine notices and asks; the agent does the work with its own
tools.

Self-limiting: the trigger is "live name != configured name", so a rename that
succeeds removes the reason for the next boot's turn. A rename that Discord
REFUSES would otherwise fire a turn on every restart forever, so attempts are
capped per (agent, target name) and the cap is recorded under data/.
"""

import json
import logging
import time
from pathlib import Path

from src.core.base import IncomingMessage

logger = logging.getLogger(__name__)

FILENAME = "identity_attempts.json"

# Discord allows two username changes per hour. Two tries is enough to survive a
# transient failure and few enough that a name Discord will never accept costs a
# bounded number of turns instead of one per restart, forever.
MAX_ATTEMPTS = 2

IDENTITY_PROMPT = (
    "⚠️ <identity-reconcile>Your Discord ACCOUNT is currently named "
    "'{live_name}', but you are configured as '{configured_name}'. That name is "
    "what people see in DMs, what the API returns, and what the roster records, "
    "so the mismatch makes you look like a different bot.\n\n"
    "Fix your own identity now:\n"
    "1. `discord_set_bot_name(name='{configured_name}', bot='{account}')` — the "
    "account username, not a per-server nickname. Discord allows only two "
    "changes per hour, so do NOT retry if it is refused: report and stop.\n"
    "2. `set_agent_avatar(agent_name='{agent_id}')` — only if you do not "
    "already have one.\n"
    "3. `discord_send_dm(user_id='{owner_id}', bot='{account}', content=...)` — "
    "one short greeting to your owner introducing yourself and what you are for."
    "{owner_note}\n\n"
    "Then reply with exactly NO_REPLY. This turn has no human waiting on it and "
    "nothing should be posted in this channel.</identity-reconcile>"
)


# --- account to agent ------------------------------------------------------

def agent_for_account(agent_configs: dict, connector: str, account: str) -> str | None:
    """The agent id bound to a bot account, via its routing block.

    This is what the roster needed and never had: `on_ready` recorded
    `client.user.name`, the LIVE Discord name, so a mismatched account added a
    roster row under a name no config knows. That row is then the name the agent
    reads back as its own and passes to `set_agent_avatar`, which resolves it to
    a vault key that was never created and fails with "no bot token found". The
    avatar was unreachable for a reason that had nothing to do with avatars.
    """
    for agent_id, cfg in (agent_configs or {}).items():
        block = ((cfg or {}).get("routing") or {}).get(connector) or {}
        if isinstance(block, dict) and block.get("account") == account:
            return agent_id
    # Single-agent installs routinely omit the account key entirely.
    if account in ("default", "main") and len(agent_configs or {}) == 1:
        return next(iter(agent_configs))
    return None


def configured_name(agent_configs: dict, agent_id: str) -> str:
    cfg = (agent_configs or {}).get(agent_id) or {}
    return str(cfg.get("display_name") or agent_id)


def names_differ(live: str, configured: str) -> bool:
    """Is this a mismatch worth spending a rename on?

    Case and surrounding whitespace do not count. Discord's own casing rules
    are not ours to fight, and burning one of two hourly changes to correct a
    capital letter would be a turn every boot for a difference nobody sees.
    """
    if not live or not configured:
        return False
    return live.strip().casefold() != configured.strip().casefold()


# --- owner lookup ----------------------------------------------------------

def owner_discord_id(team: dict) -> str:
    """The owner's Discord id, else the first human who has one."""
    humans = (team or {}).get("humans") or []
    for h in humans:
        if str(h.get("access", "")).lower() == "owner":
            contact = (h.get("contact") or {}).get("discord")
            if contact:
                return str(contact)
    for h in humans:
        contact = (h.get("contact") or {}).get("discord")
        if contact:
            return str(contact)
    return ""


# --- attempt cap -----------------------------------------------------------

def _attempts_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / FILENAME


def _load_attempts(data_dir: Path | str) -> dict:
    try:
        data = json.loads(_attempts_path(data_dir).read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def attempts_for(data_dir: Path | str, agent_id: str, target: str) -> int:
    return int(_load_attempts(data_dir).get(f"{agent_id}|{target}", {}).get("count", 0))


def record_attempt(data_dir: Path | str, agent_id: str, target: str) -> int:
    """Count one identity turn for (agent, target name). Returns the new count.

    Best effort: if the store cannot be written the feature degrades to the
    uncapped behaviour rather than refusing to run, because the mismatch is
    still real and the cap is a courtesy, not a safety property.
    """
    data = _load_attempts(data_dir)
    key = f"{agent_id}|{target}"
    entry = data.get(key) or {}
    entry["count"] = int(entry.get("count", 0)) + 1
    entry["last"] = time.time()
    data[key] = entry
    try:
        path = _attempts_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")
    except OSError as e:
        logger.warning(f"Could not record identity attempt: {e}")
    return entry["count"]


# --- what needs fixing -----------------------------------------------------

def pending_renames(live_names: dict, agent_configs: dict, data_dir: Path | str,
                    connector: str = "discord") -> list[dict]:
    """One record per account whose live name disagrees with its config.

    live_names maps bot account name to the account's current Discord username.
    """
    pending = []
    for account, live in (live_names or {}).items():
        agent_id = agent_for_account(agent_configs, connector, account)
        if not agent_id:
            logger.debug(f"identity: no agent routes to {connector} account {account!r}")
            continue
        target = configured_name(agent_configs, agent_id)
        if not names_differ(live, target):
            continue
        if attempts_for(data_dir, agent_id, target) >= MAX_ATTEMPTS:
            logger.info(
                f"identity: {agent_id} is still named {live!r} rather than {target!r} "
                f"after {MAX_ATTEMPTS} attempts — not asking again. Rename it by hand "
                f"in the Discord developer portal, or change display_name to match."
            )
            continue
        pending.append({"agent_id": agent_id, "account": account,
                        "live_name": live, "configured_name": target})
    return pending


def build_identity_message(pending: dict, owner_id: str, connector: str,
                           channel_id: str) -> IncomingMessage:
    """The synthetic turn handed to the agent whose name is wrong."""
    owner_note = "" if owner_id else (
        "\n\nNo owner Discord id is on the roster, so skip the greeting rather "
        "than guessing a recipient.")
    return IncomingMessage(
        connector=connector,
        channel_id=str(channel_id),
        user_id="",
        user_name="identity-reconcile",
        content=IDENTITY_PROMPT.format(
            live_name=pending["live_name"],
            configured_name=pending["configured_name"],
            account=pending["account"],
            agent_id=pending["agent_id"],
            owner_id=owner_id,
            owner_note=owner_note,
        ),
        bot_account=pending["account"],
    )
