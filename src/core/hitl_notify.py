"""Tell the approver, by DM, that something is waiting on them.

An approval card posted to the ops channel is easy to miss, and while it sits
there the requesting agent is blocked inside the gate: it cannot say "I am
waiting", because its turn has not returned. From the outside that is
indistinguishable from the agent having failed. Then the request times out
after thirty minutes, fail-closed denies it, and the first anyone hears is the
agent reporting a denial nobody remembers refusing.

So this module speaks for the blocked agent. Three moments, and each one exists
because its absence produced a specific wrong impression:

  announced  the request was posted, with a jump link. Removes "the agent is
             broken" while it waits.
  reminded   still unanswered halfway to the deadline. Removes "I would have
             said yes if I had seen it".
  resolved   only when it timed out or the gate failed. A human who reacted
             already knows what they did, so telling them again is noise, and
             noise is what makes the next DM ignorable.

DELIVERY IS BEST EFFORT AND MUST STAY THAT WAY. Every entry point swallows its
own errors. A closed DM, a rate limit or a dead network must never change
whether a tool was approved, or an unreachable approver would silently become a
denial, which is a worse failure than the one this fixes.

Raw Discord HTTP rather than the connector, because the two gates live in
different processes: src/core/hitl.py runs in the engine with a discord.py
client, src/mcp_server.py runs in a subprocess with only a token. A token is
the one thing both of them have.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp

logger = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/karvidsson/kbots, 1.0)"

#: Discord hard-caps a message at 2000 characters. The description can carry a
#: whole tool source (create_tool), so it is trimmed rather than risking a 400
#: that would drop the notice entirely.
_MAX_DESCRIPTION = 600


def _fmt_wait(seconds: float) -> str:
    m = int(seconds // 60)
    if m < 1:
        return "under a minute"
    if m < 60:
        return f"{m} min"
    return f"{m // 60}h{m % 60:02d}"


class HitlNotifier:
    """DMs the people who can actually approve.

    `approvers` is the same set the gate checks reactions against, so the DM
    always reaches someone whose click would count. Anyone else being told
    would just be watching.
    """

    def __init__(self, token: str, approvers, timeout: float = 1800.0,
                 remind_after: float | None = None, guild_id: str = ""):
        self.token = token or ""
        self.approvers = [str(a) for a in (approvers or [])]
        self.timeout = float(timeout)
        # Halfway by default: late enough that a present approver has already
        # answered and never sees it, early enough that the reminder still
        # leaves time to act. A reminder at the deadline would only ever
        # announce a failure.
        self.remind_after = (
            float(remind_after) if remind_after is not None else self.timeout / 2
        )
        self.guild_id = str(guild_id or "")

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.approvers)

    # --- transport ---------------------------------------------------------

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bot {self.token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }

    async def _dm(self, user_id: str, content: str) -> bool:
        """Open a DM channel and post. The single seam tests replace."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{DISCORD_API}/users/@me/channels", headers=self._headers(),
                json={"recipient_id": user_id},
            ) as resp:
                if resp.status not in (200, 201):
                    # A closed DM is a choice the approver made, not a fault to
                    # chase. Logged at info so it does not read as an incident.
                    level = logger.info if resp.status == 403 else logger.warning
                    level(f"HITL DM to {user_id}: could not open channel "
                          f"(HTTP {resp.status})")
                    return False
                channel_id = str((await resp.json()).get("id", ""))
            if not channel_id:
                return False
            async with session.post(
                f"{DISCORD_API}/channels/{channel_id}/messages",
                headers=self._headers(), json={"content": content},
            ) as resp:
                if resp.status not in (200, 201):
                    logger.warning(f"HITL DM to {user_id}: send failed "
                                   f"(HTTP {resp.status})")
                    return False
        return True

    async def _broadcast(self, content: str) -> int:
        """DM every approver. Returns how many were reached."""
        if not self.enabled:
            return 0
        sent = 0
        for user_id in self.approvers:
            try:
                if await self._dm(user_id, content[:2000]):
                    sent += 1
            except Exception as e:
                logger.warning(f"HITL DM to {user_id} failed: {e}")
        return sent

    # --- messages ----------------------------------------------------------

    def jump_url(self, channel_id: str, message_id: str) -> str:
        """Deep link to the approval card, so the DM is one tap from acting.

        Without a guild id Discord cannot build the link, and a wrong link is
        worse than none, so this returns empty rather than guessing.
        """
        if not (self.guild_id and channel_id and message_id):
            return ""
        return f"https://discord.com/channels/{self.guild_id}/{channel_id}/{message_id}"

    async def announced(self, *, agent_id: str, tool_name: str, hitl_id: str,
                        description: str = "", channel_id: str = "",
                        message_id: str = "") -> int:
        """"I am not stuck, I am waiting on you." Sent as the card goes up."""
        link = self.jump_url(channel_id, message_id)
        lines = [
            f"🔒 **`{agent_id}` is waiting for your approval.**",
            f"Tool: `{tool_name}`  ·  ID: `{hitl_id}`",
        ]
        if description:
            lines.append(f"> {description[:_MAX_DESCRIPTION]}")
        lines.append(
            "React ✅ or ❌ on the card in the approvals channel."
            + (f"\n{link}" if link else "")
        )
        lines.append(
            f"It is blocked until you answer, and gives up in "
            f"{_fmt_wait(self.timeout)}."
        )
        return await self._broadcast("\n".join(lines))

    async def reminded(self, *, agent_id: str, tool_name: str, hitl_id: str,
                       remaining: float, channel_id: str = "",
                       message_id: str = "") -> int:
        link = self.jump_url(channel_id, message_id)
        text = (
            f"⏳ **Still waiting:** `{agent_id}` → `{tool_name}` (`{hitl_id}`).\n"
            f"No answer yet. It gives up in {_fmt_wait(remaining)} and the tool "
            f"will not run."
            + (f"\n{link}" if link else "")
        )
        return await self._broadcast(text)

    async def resolved(self, *, agent_id: str, tool_name: str, hitl_id: str,
                       status: str) -> int:
        """Only worth sending when nobody acted. See the module docstring."""
        if status not in ("timeout", "denied_error"):
            return 0
        text = (
            f"⌛ **No answer:** `{agent_id}` → `{tool_name}` (`{hitl_id}`) "
            f"timed out after {_fmt_wait(self.timeout)}.\n"
            f"The tool did NOT run. Ask the agent again if you still want it."
        )
        return await self._broadcast(text)

    # --- the reminder timer ------------------------------------------------

    def start_reminder(self, *, agent_id: str, tool_name: str, hitl_id: str,
                       still_pending, channel_id: str = "",
                       message_id: str = "") -> asyncio.Task | None:
        """Arm a one-shot reminder, cancelled when the request resolves.

        `still_pending` is an async predicate rather than a captured flag: the
        request can be answered from another process (a reaction handled by the
        connector resolves a row this task never sees), so the timer has to
        re-read the truth rather than trust what was true when it started.
        """
        if not self.enabled or not (0 < self.remind_after < self.timeout):
            return None

        async def _run() -> None:
            try:
                await asyncio.sleep(self.remind_after)
                if not await still_pending():
                    return
                await self.reminded(
                    agent_id=agent_id, tool_name=tool_name, hitl_id=hitl_id,
                    remaining=self.timeout - self.remind_after,
                    channel_id=channel_id, message_id=message_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"HITL reminder for {hitl_id} failed: {e}")

        return asyncio.create_task(_run(), name=f"hitl-remind-{hitl_id}")


async def resolve_guild_id(token: str, channel_id: str) -> str:
    """Look up the guild a channel belongs to, for jump links.

    Neither gate is told its guild, and asking Discord once at construction is
    cheaper than threading it through config that operators would have to fill
    in correctly.
    """
    if not (token and channel_id):
        return ""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{DISCORD_API}/channels/{channel_id}",
                headers={"Authorization": f"Bot {token}", "User-Agent": USER_AGENT},
            ) as resp:
                if resp.status != 200:
                    return ""
                return str((await resp.json()).get("guild_id") or "")
    except Exception as e:
        logger.debug(f"HITL guild lookup failed: {e}")
        return ""
