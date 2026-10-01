"""Scheduled owner digest DM — proactive summary of what's waiting on the owner.

A morning DM combining:
  - Open owner_asks (decisions waiting for a human answer)
  - Pending HITL approvals (tool calls blocked on human approval)
  - Active goals (goal workstreams in progress)

Built on the existing waiting_on_you infrastructure: same owner_id, digest_hour,
timezone settings. Runs as a background task alongside the scheduler. Silent
when nothing is pending (no empty DMs). Failures log and continue — best effort.

The DM pattern matches hitl_notify.py: raw Discord HTTP via aiohttp, so both
the engine and a separate MCP process can use the same code.
"""

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp

logger = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/karvidsson/kbots, 1.0)"


def gather_owner_asks(store) -> list[dict]:
    """Gather open owner_asks from the store.

    Args:
        store: AskStore instance (from discord_owner_asks).

    Returns list of open ask dicts with id, agent_id, question, created, url.
    """
    if not store:
        return []
    try:
        from src.core.owner_asks import OPEN, jump
        rows = store.rows(OPEN)
        return [
            {
                "id": r["id"],
                "agent_id": r["agent_id"],
                "question": r["payload"]["question"][:100],
                "created": r["created"],
                "url": jump(r),
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning(f"Owner digest: could not gather owner_asks: {e}")
        return []


async def gather_pending_hitl(db) -> list[dict]:
    """Gather pending HITL approvals from the database.

    Args:
        db: aiosqlite connection with the hitl_pending table.

    Returns list of pending HITL dicts with hitl_id, agent_id, tool_name,
    description, created_at, channel_id.
    """
    if not db:
        return []
    try:
        async with db.execute(
            "SELECT hitl_id, agent_id, tool_name, description, created_at, channel_id "
            "FROM hitl_pending WHERE status = 'pending' ORDER BY created_at DESC"
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            {
                "hitl_id": r[0],
                "agent_id": r[1],
                "tool_name": r[2],
                "description": (r[3] or "")[:100],
                "created_at": r[4],
                "channel_id": r[5],
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning(f"Owner digest: could not gather pending HITL: {e}")
        return []


def gather_active_goals() -> list[dict]:
    """Gather active goals from the goals store.

    Returns list of active goal dicts with id, title, status, owner_agent,
    channel_id, updated_at, tasks_open, blocked_brief.
    """
    try:
        from src.core import goals as store
        from src.core.goals import ACTIVE_STATUSES, ROUTED_STATUSES
        active_statuses = ("proposed", "brainstorm", "strategy", "executing",
                          "blocked_on_user")
        goals = store.list_goals(statuses=active_statuses)
        result = []
        for g in goals:
            tasks = store.list_tasks(g["id"])
            result.append({
                "id": g["id"],
                "title": g["title"][:60],
                "status": g["status"],
                "owner_agent": g["owner_agent"],
                "channel_id": g["channel_id"],
                "updated_at": g["updated_at"],
                "tasks_open": len(tasks),
                "blocked_brief": g.get("blocked_brief", ""),
            })
        return result
    except Exception as e:
        logger.warning(f"Owner digest: could not gather active goals: {e}")
        return []


def format_owner_digest(
    owner_asks: list[dict],
    pending_hitl: list[dict],
    active_goals: list[dict],
    now: float | None = None,
) -> str:
    """Format the digest into a clear, readable message.

    Returns empty string if nothing is pending (caller should skip sending).
    """
    now = time.time() if now is None else now
    sections: list[str] = []
    links: list[str] = []
    link_num = 0

    def age_str(ts: float) -> str:
        minutes = max(0, int((now - ts) / 60))
        if minutes >= 1440:
            return f"{minutes // 1440}d"
        if minutes >= 60:
            return f"{minutes // 60}h"
        return f"{minutes}m"

    # Owner asks section
    if owner_asks:
        lines = ["**Decisions waiting for your answer:**"]
        for ask in owner_asks[:10]:
            link_num += 1
            q = ask["question"].replace("`", "'")
            if len(q) > 60:
                q = q[:57] + "..."
            lines.append(f"  {link_num}. `{ask['agent_id']}` — {q} ({age_str(ask['created'])})")
            links.append(f"[{link_num}: Open ask]({ask['url']})" if "http" in ask.get("url", "") else "")
        if len(owner_asks) > 10:
            lines.append(f"  ...and {len(owner_asks) - 10} more")
        sections.append("\n".join(lines))

    # Pending HITL section
    if pending_hitl:
        lines = ["**Tool approvals waiting (HITL):**"]
        for hitl in pending_hitl[:10]:
            link_num += 1
            desc = hitl["description"].replace("`", "'")
            if len(desc) > 50:
                desc = desc[:47] + "..."
            lines.append(
                f"  {link_num}. `{hitl['agent_id']}` → `{hitl['tool_name']}` ({age_str(hitl['created_at'])})"
            )
            if hitl.get("channel_id"):
                links.append(f"[{link_num}: Approvals channel](https://discord.com/channels/@me/{hitl['channel_id']})")
            else:
                links.append("")
        if len(pending_hitl) > 10:
            lines.append(f"  ...and {len(pending_hitl) - 10} more")
        sections.append("\n".join(lines))

    # Active goals section
    if active_goals:
        lines = ["**Active goals:**"]
        for goal in active_goals[:8]:
            status_emoji = {
                "proposed": "📋",
                "brainstorm": "💭",
                "strategy": "🎯",
                "executing": "⚙️",
                "blocked_on_user": "🧱",
            }.get(goal["status"], "📌")
            tasks = f", {goal['tasks_open']} task(s)" if goal["tasks_open"] else ""
            blocked = " — **waiting on you**" if goal["status"] == "blocked_on_user" else ""
            lines.append(
                f"  {status_emoji} `{goal['id']}` — {goal['title']} "
                f"[{goal['status']}]{tasks}{blocked}"
            )
        if len(active_goals) > 8:
            lines.append(f"  ...and {len(active_goals) - 8} more")
        sections.append("\n".join(lines))

    if not sections:
        return ""

    header = "☀️ **Morning digest — what's waiting on you**"
    body = "\n\n".join(sections)
    # Filter empty links
    link_lines = [ln for ln in links if ln]
    footer = "\n".join(link_lines) if link_lines else ""

    result = f"{header}\n\n{body}"
    if footer:
        result += f"\n\n{footer}"
    return result


async def send_digest_dm(token: str, user_id: str, content: str) -> bool:
    """Send a DM to the owner. Returns True on success.

    Best effort: a closed DM or rate limit logs and returns False.
    """
    if not token or not user_id or not content:
        return False
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    try:
        async with aiohttp.ClientSession() as session:
            # Open DM channel
            async with session.post(
                f"{DISCORD_API}/users/@me/channels",
                headers=headers,
                json={"recipient_id": user_id},
            ) as resp:
                if resp.status not in (200, 201):
                    level = logger.info if resp.status == 403 else logger.warning
                    level(f"Owner digest DM to {user_id}: could not open channel (HTTP {resp.status})")
                    return False
                channel_id = str((await resp.json()).get("id", ""))
            if not channel_id:
                return False
            # Send message (truncate to Discord limit)
            async with session.post(
                f"{DISCORD_API}/channels/{channel_id}/messages",
                headers=headers,
                json={"content": content[:2000]},
            ) as resp:
                if resp.status not in (200, 201):
                    logger.warning(f"Owner digest DM to {user_id}: send failed (HTTP {resp.status})")
                    return False
        return True
    except Exception as e:
        logger.warning(f"Owner digest DM to {user_id} failed: {e}")
        return False


async def send_to_fallback_channel(
    token: str, channel_id: str, content: str
) -> bool:
    """Fall back to posting in a channel if DM fails. Returns True on success."""
    if not token or not channel_id or not content:
        return False
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{DISCORD_API}/channels/{channel_id}/messages",
                headers=headers,
                json={"content": content[:2000]},
            ) as resp:
                if resp.status not in (200, 201):
                    logger.warning(f"Owner digest fallback to {channel_id}: send failed (HTTP {resp.status})")
                    return False
        return True
    except Exception as e:
        logger.warning(f"Owner digest fallback to {channel_id} failed: {e}")
        return False


class OwnerDigestTask:
    """Background task that sends the owner digest on schedule.

    Runs alongside the scheduler, checking once per tick whether the digest
    hour has passed and no digest has been sent today. Uses the same config
    as waiting_on_you: owner_id, digest_hour, timezone.

    The fallback_channel (security.alert_channel) is used if DMs fail.
    """

    def __init__(
        self,
        *,
        owner_id: str,
        digest_hour: int,
        timezone: ZoneInfo,
        vault,
        hitl_db,
        owner_asks_store=None,
        fallback_channel: str = "",
        tick_seconds: int = 60,
    ):
        self.owner_id = str(owner_id or "")
        self.digest_hour = int(digest_hour)
        self.timezone = timezone
        self.vault = vault
        self.hitl_db = hitl_db
        self.owner_asks_store = owner_asks_store
        self.fallback_channel = str(fallback_channel or "")
        self.tick_seconds = tick_seconds
        # Track which day we last sent for
        self._last_digest_day: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.owner_id)

    def _get_token(self) -> str:
        """Get Discord bot token from vault."""
        if not self.vault:
            return ""
        try:
            return self.vault.get("discord-token") or ""
        except Exception as e:
            logger.debug(f"Owner digest: no token ({e})")
            return ""

    async def tick(self, now: float | None = None) -> dict:
        """One pass. Returns {"sent": bool, "skipped_reason": str, "content": str}."""
        now = time.time() if now is None else now
        result = {"sent": False, "skipped_reason": "", "content": ""}

        if not self.enabled:
            result["skipped_reason"] = "no owner_id configured"
            return result

        # Check if it's past digest_hour in the configured timezone
        local_dt = datetime.fromtimestamp(now, self.timezone)
        today = local_dt.date().isoformat()

        if local_dt.hour < self.digest_hour:
            result["skipped_reason"] = "before digest_hour"
            return result

        if self._last_digest_day == today:
            result["skipped_reason"] = "already sent today"
            return result

        # Gather all pending items
        owner_asks = gather_owner_asks(self.owner_asks_store)
        pending_hitl = await gather_pending_hitl(self.hitl_db)
        active_goals = gather_active_goals()

        # Format the digest
        content = format_owner_digest(owner_asks, pending_hitl, active_goals, now)
        result["content"] = content

        if not content:
            result["skipped_reason"] = "nothing pending"
            self._last_digest_day = today
            return result

        # Try to send
        token = self._get_token()
        if not token:
            result["skipped_reason"] = "no discord token"
            return result

        sent = await send_digest_dm(token, self.owner_id, content)
        if sent:
            result["sent"] = True
            self._last_digest_day = today
            logger.info(f"Owner digest sent to {self.owner_id}")
            return result

        # Fallback to channel if configured
        if self.fallback_channel:
            fallback_content = f"<@{self.owner_id}> (DM delivery failed)\n\n{content}"
            sent = await send_to_fallback_channel(token, self.fallback_channel, fallback_content)
            if sent:
                result["sent"] = True
                self._last_digest_day = today
                logger.info(f"Owner digest sent to fallback channel {self.fallback_channel}")
                return result

        result["skipped_reason"] = "DM and fallback both failed"
        logger.warning(f"Owner digest: both DM and fallback failed for {self.owner_id}")
        return result

    async def run(self) -> None:
        """Run the digest task forever."""
        logger.info(
            f"Owner digest: ON (owner={self.owner_id}, hour={self.digest_hour}, "
            f"tz={self.timezone}, fallback={'<#' + self.fallback_channel + '>' if self.fallback_channel else 'none'})"
        )
        while True:
            try:
                await self.tick()
            except Exception as e:
                logger.warning(f"Owner digest tick failed: {e}")
            await asyncio.sleep(self.tick_seconds)


def create_owner_digest_task(config: dict, vault, hitl_db, owner_asks_store=None) -> OwnerDigestTask | None:
    """Factory to create an OwnerDigestTask from config.

    Uses waiting_on_you settings. Returns None if disabled or not configured.
    """
    woy_cfg = config.get("waiting_on_you", {}) or {}
    if not woy_cfg.get("enabled", True):
        return None

    # Get owner_id — same logic as owner_asks
    admins = [str(x) for x in config.get("admin_users", {}).get("discord", [])]
    owner_id = str(woy_cfg.get("owner_id") or (admins[0] if len(admins) == 1 else ""))
    if not owner_id:
        logger.info("Owner digest: OFF (no owner_id configured and admin_users.discord is empty or has multiple entries)")
        return None

    digest_hour = woy_cfg.get("digest_hour", 8)
    if not isinstance(digest_hour, int) or not 0 <= digest_hour <= 23:
        logger.warning(f"Owner digest: invalid digest_hour {digest_hour}, using 8")
        digest_hour = 8

    try:
        tz = ZoneInfo(woy_cfg.get("timezone", "UTC"))
    except Exception:
        logger.warning(f"Owner digest: invalid timezone, using UTC")
        tz = ZoneInfo("UTC")

    # Fallback channel is security.alert_channel
    alert_channel = str((config.get("security", {}) or {}).get("alert_channel", "") or "")

    return OwnerDigestTask(
        owner_id=owner_id,
        digest_hour=digest_hour,
        timezone=tz,
        vault=vault,
        hitl_db=hitl_db,
        owner_asks_store=owner_asks_store,
        fallback_channel=alert_channel,
    )
