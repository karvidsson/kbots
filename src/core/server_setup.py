"""Provision the channels a kbots fleet needs in a Discord server.

Four channel roles and one category are wired by ID in config, and every one of
them used to be a "copy the channel ID out of Discord and paste it into a YAML
file" step. That is the bulk of what setting kbots up actually costs, and it is
entirely mechanical: fixed names, fixed purposes, fixed config keys.

So it is code, not a prompt. An LLM turn adds nothing here but ways to fail
halfway. The agent's own judgement is spent on its name, its avatar and how it
introduces itself; the channels are provisioned deterministically and reported.

Three rules make this safe to run against a live server:

  ADOPT BEFORE CREATE. A channel whose name matches (including a few aliases)
  is used as-is. Running this on an established server must not litter it with
  a second #approvals sitting empty beside the one people watch.

  NEVER REPOINT WHAT IS ALREADY SET. If a role already has a channel in config
  or in a runtime override, it is left exactly alone. "Set the IDs" means fill
  in the blanks, and moving a running fleet's security alerts to a fresh empty
  channel is not a setup step, it is an outage.

  IDEMPOTENT. Re-running changes nothing once every role is wired, so the
  guild-join hook, a manual re-run and a reconnect all converge.
"""

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from src.core import runtime_state

logger = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api/v10"

MARKER_FILE = "guild_setup.json"

# Discord channel type ints (only the two we create).
TYPE_TEXT = 0
TYPE_CATEGORY = 4


@dataclass(frozen=True)
class ChannelSpec:
    key: str            # what we call it in reports
    name: str           # created under this name
    aliases: tuple      # existing channels adopted under any of these names
    kind: int           # TYPE_TEXT / TYPE_CATEGORY
    topic: str
    flag: str           # runtime_state flag that wires it live, no restart
    config_path: tuple  # where the same value lives in config.yaml


SPECS: tuple[ChannelSpec, ...] = (
    ChannelSpec(
        key="approvals",
        name="kbots-approvals",
        aliases=("kbots-approvals", "approvals", "kbots-approval"),
        kind=TYPE_TEXT,
        topic="Human approval gate. Agents post a card before a gated tool runs; "
              "react ✅ to approve or ❌ to deny. No reaction means denied.",
        flag="hitl_channel",
        config_path=("security", "hitl", "channel"),
    ),
    ChannelSpec(
        key="schedules",
        name="kbots-schedules",
        aliases=("kbots-schedules", "schedules", "kbots-schedule"),
        kind=TYPE_TEXT,
        topic="Scheduled task board. Every scheduled run posts a card here; "
              "react ❌ to cancel it. /schedules shows the full board.",
        flag="schedules_channel",
        config_path=("schedules", "channel"),
    ),
    ChannelSpec(
        key="alerts",
        name="kbots-alerts",
        aliases=("kbots-alerts", "alerts", "kbots-alert"),
        kind=TYPE_TEXT,
        topic="Security alerts: prompt-injection findings in web-facing tool "
              "output, blocked goals, and anything the engine flags.",
        flag="alert_channel",
        config_path=("security", "alert_channel"),
    ),
    ChannelSpec(
        key="platform_updates",
        name="platform-updates",
        aliases=("platform-updates", "kbots-updates", "kbots-platform-updates"),
        kind=TYPE_TEXT,
        topic="What changed in the platform itself. Posted on the first boot "
              "after a deploy, with the commits that landed.",
        flag="platform_updates_channel",
        config_path=("platform", "updates_channel"),
    ),
    ChannelSpec(
        key="goals",
        name="kbots-goals",
        aliases=("kbots-goals", "goals"),
        kind=TYPE_CATEGORY,
        topic="",  # Discord categories have no topic
        flag="goals_category",
        config_path=("goals", "category_id"),
    ),
)


# --- reading what is already wired -----------------------------------------

def _config_value(config: dict, path: tuple) -> str:
    node = config or {}
    for part in path:
        if not isinstance(node, dict):
            return ""
        node = node.get(part) or {}
    return str(node) if isinstance(node, str | int) and node else ""


def configured_channel(config: dict, spec: ChannelSpec) -> str:
    """The channel already wired for this role, from the runtime override or
    config. Empty means the role is unset and setup may fill it in."""
    flag = runtime_state.get_flag(spec.flag, None)
    if flag:
        return str(flag)
    return _config_value(config, spec.config_path)


# --- the guild marker -------------------------------------------------------

def _marker_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / MARKER_FILE


def _claim_path(data_dir: Path | str, guild_id: str) -> Path:
    return Path(data_dir) / f"guild_setup.{guild_id}.claim"


def claim_guild_setup(data_dir: Path | str, guild_id: str, account: str = "") -> bool:
    """Atomically claim the right to provision a guild. True if we own it.

    The done-marker alone can't prevent duplicates: it is written AFTER
    provisioning, so two bots invited seconds apart both pass the marker
    check and race to create the same five channels — Discord allows every
    duplicate. O_CREAT|O_EXCL on a per-guild claim file makes exactly one
    winner regardless of how many processes are watching the guild.
    """
    path = _claim_path(data_dir, guild_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({"account": account}) + "\n")
        return True
    except FileExistsError:
        return False
    except OSError as e:
        # Can't claim ⇒ can't dedupe, but provisioning is adopt-before-create
        # and idempotent, so running anyway risks noise, not damage.
        logger.warning(f"Could not claim guild setup for {guild_id}: {e} — proceeding")
        return True


def release_guild_claim(data_dir: Path | str, guild_id: str) -> None:
    """Drop the claim so a later (re-)join can retry a failed provisioning."""
    try:
        _claim_path(data_dir, guild_id).unlink(missing_ok=True)
    except OSError:
        pass


def guild_is_set_up(data_dir: Path | str, guild_id: str) -> bool:
    """Has the join hook already run for this guild?

    The hook fires on every join, and Discord re-delivers a join for a server
    the bot is already in when it reconnects after being removed and re-added.
    Without the marker, a re-invite would run provisioning again.
    """
    try:
        data = json.loads(_marker_path(data_dir).read_text())
        return str(guild_id) in (data.get("guilds") or {})
    except (json.JSONDecodeError, OSError):
        return False


def record_guild_setup(data_dir: Path | str, guild_id: str, summary: dict) -> None:
    path = _marker_path(data_dir)
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            data = {}
    except (json.JSONDecodeError, OSError):
        data = {}
    data.setdefault("guilds", {})[str(guild_id)] = summary
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")
    except OSError as e:
        # A marker we cannot write means the hook may run twice. Provisioning is
        # idempotent, so that is noise rather than damage — say so and continue.
        logger.warning(f"Could not record guild setup for {guild_id}: {e}")


# --- which bot does this ----------------------------------------------------

def is_setup_account(agent_configs: dict, account: str) -> bool:
    """Only ONE bot provisions a server, or eight of them race to create the
    same five channels and Discord happily makes duplicates of each.

    The coordinator if there is one, else whoever holds the 'main' account,
    else the single configured agent.
    """
    def _account_of(cfg: dict) -> str:
        return str((((cfg or {}).get("routing") or {}).get("discord") or {}).get("account") or "")

    configs = agent_configs or {}
    for cfg in configs.values():
        if (cfg or {}).get("tier") == "coordinator":
            return _account_of(cfg) == account
    if any(_account_of(cfg) == "main" for cfg in configs.values()):
        return account == "main"
    if len(configs) == 1:
        return True
    return account == "default"


# --- the Discord calls ------------------------------------------------------

async def _list_channels(guild_id: str, token: str) -> list[dict] | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{DISCORD_API}/guilds/{guild_id}/channels",
                headers={"Authorization": f"Bot {token}"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.warning(f"Could not list channels in {guild_id}: "
                               f"HTTP {resp.status} {(await resp.text())[:200]}")
    except (aiohttp.ClientError, OSError) as e:
        logger.warning(f"Could not list channels in {guild_id}: {e}")
    return None


async def _create_channel(guild_id: str, token: str, spec: ChannelSpec) -> tuple[str, str]:
    """Returns (channel_id, error). Exactly one is non-empty."""
    payload: dict = {"name": spec.name, "type": spec.kind}
    if spec.topic and spec.kind == TYPE_TEXT:
        payload["topic"] = spec.topic
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{DISCORD_API}/guilds/{guild_id}/channels",
                headers={"Authorization": f"Bot {token}"},
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status in (200, 201):
                    return str((await resp.json()).get("id", "")), ""
                detail = (await resp.text())[:200]
                if resp.status == 403:
                    # By far the most likely failure, and the one with a fix the
                    # owner can act on: the invite did not include Manage Channels.
                    return "", ("missing the Manage Channels permission "
                                "(re-invite the bot with it, then re-run setup)")
                return "", f"HTTP {resp.status}: {detail}"
    except (aiohttp.ClientError, OSError) as e:
        return "", str(e)


# --- provisioning ----------------------------------------------------------

@dataclass
class Outcome:
    key: str
    name: str
    channel_id: str
    action: str      # kept | adopted | created | failed
    detail: str = ""


async def provision_guild(guild_id: str, token: str, config: dict,
                          specs: tuple = SPECS) -> list[Outcome]:
    """Fill in every unset channel role for this guild. Nothing is repointed."""
    existing = await _list_channels(guild_id, token)
    if existing is None:
        return [Outcome(s.key, s.name, "", "failed",
                        "could not read the server's channels") for s in specs]

    by_name: dict[str, dict] = {}
    for ch in existing:
        name = str(ch.get("name", "")).lower()
        # First match wins: a server with two similarly named channels should
        # get the one Discord lists first, deterministically, not the last.
        by_name.setdefault(name, ch)

    outcomes: list[Outcome] = []
    for spec in specs:
        already = configured_channel(config, spec)
        if already:
            outcomes.append(Outcome(spec.key, spec.name, already, "kept"))
            continue

        match = next((by_name[a] for a in spec.aliases
                      if a in by_name and int(by_name[a].get("type", -1)) == spec.kind), None)
        if match:
            outcomes.append(Outcome(spec.key, str(match.get("name", spec.name)),
                                    str(match.get("id", "")), "adopted"))
            continue

        channel_id, err = await _create_channel(guild_id, token, spec)
        if channel_id:
            outcomes.append(Outcome(spec.key, spec.name, channel_id, "created"))
        else:
            outcomes.append(Outcome(spec.key, spec.name, "", "failed", err))
    return outcomes


def wire(outcomes: list[Outcome]) -> list[Outcome]:
    """Persist the ids that setup resolved, live and without a restart.

    Only what setup actually resolved: a `kept` role is already wired by
    whatever set it, and rewriting it as a runtime override would silently
    promote a config value into a flag that then outranks config forever.
    """
    spec_by_key = {s.key: s for s in SPECS}
    for out in outcomes:
        if out.action in ("adopted", "created") and out.channel_id:
            spec = spec_by_key.get(out.key)
            if spec:
                runtime_state.set_flag(spec.flag, out.channel_id)
    return outcomes


SETUP_PROMPT = (
    "⚠️ <server-setup>You were just added to the Discord server '{guild_name}' "
    "({guild_id}) and the channels a kbots fleet needs have already been "
    "provisioned for you:\n\n{summary}\n\n"
    "They are wired live, so nothing needs a restart. Finish the job with the "
    "parts that need your judgement, in this channel:\n"
    "1. `discord_set_nickname(nickname='{display_name}', bot='{account}', "
    "guild_id='{guild_id}')` — how you appear in this server. Pass the guild_id: "
    "without it the rename applies to EVERY server this bot is in.\n"
    "2. `set_agent_avatar(agent_name='{agent_id}')` — only if you do not already "
    "have one.\n"
    "3. Post ONE short message here introducing yourself: who you are, what each "
    "channel above is for, and that ✅/❌ reactions in the approvals channel are "
    "how a human approves or denies a gated tool call.{failure_note}\n\n"
    "Do not create more channels. Do not repeat the table above verbatim; write "
    "it as you would explain it to someone who has never used this."
    "</server-setup>"
)


def build_setup_message(agent_id: str, guild_id: str, guild_name: str,
                        outcomes: list, connector: str, channel_id: str,
                        display_name: str = "", account: str = ""):
    """The turn handed to the setup agent once its server is provisioned."""
    from src.core.base import IncomingMessage

    failed = [o for o in outcomes if o.action == "failed"]
    note = ""
    if failed:
        # The owner has to hear about this from the agent, not from a log line
        # nobody reads. A half-provisioned server that reports success is worse
        # than one that reports what is missing.
        note = ("\n\nSay plainly that these could NOT be set up and why: "
                + "; ".join(f"{o.key} ({o.detail or 'unknown error'})" for o in failed))
    return IncomingMessage(
        connector=connector,
        channel_id=str(channel_id),
        user_id="",
        user_name="server-setup",
        content=SETUP_PROMPT.format(
            guild_name=guild_name or guild_id,
            guild_id=guild_id,
            summary=summarize(outcomes),
            display_name=display_name or agent_id,
            agent_id=agent_id,
            account=account,
            failure_note=note,
        ),
        bot_account=account,
    )


def summarize(outcomes: list[Outcome]) -> str:
    """A report an owner can read without opening the config."""
    verb = {"kept": "already set", "adopted": "adopted existing",
            "created": "created", "failed": "FAILED"}
    lines = []
    for out in outcomes:
        where = f"<#{out.channel_id}>" if out.channel_id else "-"
        detail = f" ({out.detail})" if out.detail else ""
        lines.append(f"- **{out.key}**: {verb.get(out.action, out.action)} {where}{detail}")
    return "\n".join(lines)


# --- join introductions -----------------------------------------------------
#
# The setup agent introduces itself as part of provisioning; every OTHER bot
# used to join silently and sit there until someone asked who it was. One
# introduction per (guild, agent), in the platform-updates channel.

INTRO_FILE = "guild_intros.json"

JOIN_INTRO_PROMPT = (
    "👋 <join-intro>Your bot account was just added to the Discord server "
    "'{guild_name}' ({guild_id}). This channel is where the platform talks "
    "about itself, and your reply is posted here — so introduce yourself, in "
    "ONE short message:\n"
    "- who you are ({display_name}) and what you're for, one line\n"
    "- what you can actually do: your 3-5 standout abilities, plain language\n"
    "- the slash commands worth knowing — /help lists them all; name the two "
    "or three most useful for configuring you further\n"
    "- how to reach you: @mention {display_name}\n"
    "Do not list every tool, no headings, no wall of text — a person just "
    "added you and wants to know what you're good for.</join-intro>"
)


def intro_channel(config: dict) -> str:
    """Where a joining bot introduces itself: platform-updates, else alerts."""
    for key in ("platform_updates", "alerts"):
        spec = next(s for s in SPECS if s.key == key)
        channel = configured_channel(config, spec)
        if channel:
            return channel
    return ""


def _intro_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / INTRO_FILE


def intro_done(data_dir: Path | str, guild_id: str, agent_id: str) -> bool:
    try:
        data = json.loads(_intro_path(data_dir).read_text())
        return agent_id in (data.get("guilds", {}).get(str(guild_id)) or [])
    except (json.JSONDecodeError, OSError):
        return False


def record_intro(data_dir: Path | str, guild_id: str, agent_id: str) -> None:
    path = _intro_path(data_dir)
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            data = {}
    except (json.JSONDecodeError, OSError):
        data = {}
    agents = data.setdefault("guilds", {}).setdefault(str(guild_id), [])
    if agent_id not in agents:
        agents.append(agent_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")
    except OSError as e:
        logger.warning(f"Could not record intro for {agent_id}@{guild_id}: {e}")


def build_join_intro_message(agent_id: str, guild_id: str, guild_name: str,
                             connector: str, channel_id: str,
                             display_name: str = "", account: str = ""):
    """The turn handed to a bot's agent when its account joins a server."""
    from src.core.base import IncomingMessage

    return IncomingMessage(
        connector=connector,
        channel_id=str(channel_id),
        user_id="",
        user_name="join-intro",
        content=JOIN_INTRO_PROMPT.format(
            guild_name=guild_name or guild_id,
            guild_id=guild_id,
            display_name=display_name or agent_id,
        ),
        bot_account=account,
    )
