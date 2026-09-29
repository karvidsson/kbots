"""Bound closing-card verdicts. Discord effects never undo the human's verdict."""

import asyncio
import io
import logging

import discord

from src.core import goals
from src.core.goal_notice import mark

logger = logging.getLogger(__name__)


def recipient(goal: dict, config: dict, admins: list[str]) -> str:
    """An explicit goal owner, the human initiator, or the sole install admin."""
    explicit = str(
        (config.get("goals") or {}).get("escalation_user") or (config.get("waiting_on_you") or {}).get("owner_id") or ""
    )
    candidate = explicit or (
        str(goal["created_by"]) if str(goal["created_by"]).isdecimal() else (str(admins[0]) if len(admins) == 1 else "")
    )
    return candidate if candidate.isdecimal() and candidate in admins else ""


async def _notice(message, channel, text: str) -> bool:
    """Edit the decision card; a failed edit gets one plain, visible fallback."""
    kwargs = {"allowed_mentions": discord.AllowedMentions.none()}
    try:
        await message.edit(content=mark(text)[:1900], **kwargs)
        return True
    except Exception:
        try:
            await channel.send(mark(text)[:1900], **kwargs)
            return True
        except Exception:
            logger.exception("Goal verdict notice failed in channel %s", channel.id)
            return False


async def handle_closing_reaction(bot, payload) -> bool:
    """True consumes every known closing card, including rejected/stale reactions.

    Existing cards predate persisted Discord bindings. Authenticate them by
    fetching the exact stored message and matching its full visible content,
    guild, channel and actual bot author. No election based on agent ownership:
    a coordinator may have posted the card using its own bot.
    """
    mid = str(payload.message_id)
    if not goals.is_closing_message(mid):
        return False
    goal = goals.goal_by_closing_message(mid)
    if not goal or goal["status"] != "done" or goal["verdict"]:
        return True
    emoji = str(payload.emoji)
    if emoji not in ("✅", "❌"):
        return True
    config = bot.connector._full_config
    admins = [str(x) for x in bot.admin_users]
    uid = recipient(goal, config, admins)
    guild_id = str((config.get("connectors", {}).get("discord") or {}).get("guild_id") or "")
    if (
        not uid
        or str(payload.user_id) != uid
        or not guild_id
        or str(getattr(payload, "guild_id", "")) != guild_id
        or str(payload.channel_id) != goal["channel_id"]
        or goal["connector"] != "discord"
        or not goal["summary"]
    ):
        return True
    bot_id = str(bot.client.user.id)
    author = getattr(payload, "message_author_id", None)
    if author is not None and str(author) != bot_id:
        return True
    member = getattr(payload, "member", None)
    if member is not None and (str(member.id) != uid or member.bot):
        return True
    try:
        user = await bot.client.fetch_user(int(uid))
        if str(user.id) != uid or user.bot or getattr(user, "system", False):
            return True
        channel = await bot.client.fetch_channel(payload.channel_id)
        if str(channel.id) != goal["channel_id"] or str(getattr(getattr(channel, "guild", None), "id", "")) != guild_id:
            return True
        message = await channel.fetch_message(payload.message_id)
        if (
            str(message.id) != mid
            or str(message.channel.id) != goal["channel_id"]
            or str(getattr(getattr(message, "guild", None), "id", "")) != guild_id
            or str(message.author.id) != bot_id
            or not message.author.bot
            or message.webhook_id is not None
            or message.content.rstrip() != mark(goal["summary"])[:1900].rstrip()
        ):
            return True
    except Exception:
        logger.exception("Cannot authenticate goal closing card %s", mid)
        return True

    # Future-proof against replacement of the in-memory configuration during
    # Discord reads. Admins currently load at startup; this is not live revocation.
    current_config = bot.connector._full_config
    current_guild = str((current_config.get("connectors", {}).get("discord") or {}).get("guild_id") or "")
    if (
        recipient(goal, current_config, [str(x) for x in bot.admin_users]) != uid
        or current_guild != guild_id
        or str(bot.client.user.id) != bot_id
    ):
        return True

    binding = dict(
        message_id=mid,
        channel_id=goal["channel_id"],
        guild_id=guild_id,
        account=bot.account_name,
        bot_id=bot_id,
        recipient_id=uid,
    )
    expected = {
        k: goal[k] for k in ("closing_message_id", "channel_id", "summary", "created_by", "owner_agent", "anchored")
    }
    decided = goals.record_verdict(goal["id"], emoji == "✅", uid, expected=expected, binding=binding)
    if decided is None:
        return True
    receipt = None
    completed = False
    removal_reported = False
    try:
        if emoji == "❌":
            text = (
                f"❌ Not reached: {goal['title']} (`{goal['id']}`).\n"
                f"Verdict recorded. Status: executing. **{goal['owner_agent']}** continues here.\n"
                "What is missing? Reply in this room; the goal owner receives your answer."
            )
            if not await _notice(message, channel, text):
                raise RuntimeError("The request for missing work could not be posted")
            goals.update_verdict_delivery(mid, "complete", "Not reached; executing. Asked what is missing in the room.")
            return True

        # A receipt outside the room must exist before the deletion is attempted.
        dm = await user.create_dm()
        if str(dm.id) == goal["channel_id"] or str(getattr(dm.recipient, "id", "")) != uid:
            raise RuntimeError("The summary destination was not the deciding owner's DM")
        text = (
            f"✅ Reached: {goal['title']} (`{goal['id']}`).\n"
            "Verdict recorded. Full stored summary attached. Room cleanup is pending."
        )
        sent = await dm.send(
            mark(text),
            file=discord.File(io.BytesIO(goal["summary"].encode()), filename="goal-summary.txt"),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        if (
            not sent.id
            or str(sent.channel.id) != str(dm.id)
            or str(sent.author.id) != bot_id
            or len(sent.attachments) != 1
            or sent.attachments[0].filename != "goal-summary.txt"
            or sent.attachments[0].size != len(goal["summary"].encode())
        ):
            raise RuntimeError("Summary delivery was not confirmed")
        receipt = sent
        goals.update_verdict_delivery(
            mid,
            "saved",
            "Full summary saved to the deciding owner's DM; cleanup pending.",
            receipt_channel_id=str(dm.id),
            receipt_message_id=str(receipt.id),
        )
        # A shared/borrowed room is never removed. Nor may a reopened goal or a
        # newly attached second goal be destroyed after the awaited DM send.
        current = goals.get_goal(goal["id"])
        if (
            not current
            or current["status"] != "done"
            or current["verdict"] != "reached"
            or current["closing_message_id"] != mid
            or current["channel_id"] != goal["channel_id"]
        ):
            raise RuntimeError("The goal changed during summary delivery; the room was retained")
        goals.drop_open_tasks(goal["id"], uid, "goal closed")
        shared = current["anchored"] or any(
            g["id"] != goal["id"] and g["channel_id"] == goal["channel_id"] for g in goals.list_goals()
        )
        if shared:
            outcome = "Reached. Summary saved in your DM. Shared room retained."
            if not await _notice(message, channel, f"✅ {goal['title']}: {outcome}"):
                raise RuntimeError("The shared-room verdict could not be displayed")
        else:
            goals.update_verdict_delivery(
                mid, "removing", "Summary saved; room removal requested but not yet confirmed."
            )
            await channel.delete(reason=f"Goal {goal['id']}: reached verdict by {uid}; summary saved in owner DM")
            removal_reported = True
            outcome = "Reached. Summary saved in your DM. Goal room removed."
        goals.update_verdict_delivery(mid, "complete", outcome)
        completed = True
        try:
            await receipt.edit(
                content=mark(f"✅ {goal['title']} (`{goal['id']}`): {outcome}"),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            # The attached archive and DB receipt remain durable even if its
            # progress line cannot be refreshed. Do not misreport deletion.
            goals.update_verdict_delivery(mid, "complete", outcome + " Final DM status update failed.")
            logger.exception("Goal verdict archive status edit failed for %s", mid)
    except (Exception, asyncio.CancelledError) as exc:
        if completed:
            goals.update_verdict_delivery(mid, "complete", outcome + " Final DM status update was interrupted.")
            raise
        if removal_reported:
            note = (
                "Verdict recorded. Removal reported by Discord, but the completion record could not be updated. "
                "The full summary is saved in your DM."
            )
        else:
            note = (
                "Verdict recorded. Follow-up failed or was interrupted; room removal is not confirmed. "
                "Check the room before manual cleanup. See goal_status for the saved record."
            )
        if isinstance(exc, RuntimeError):
            note += " " + str(exc)
        try:
            goals.update_verdict_delivery(mid, "failed", note)
        except Exception:
            # The storage outage may persist. Still tell the human using the
            # saved external receipt, and retain the observed outcome in logs.
            logger.exception("Cannot persist goal verdict follow-up failure for %s", mid)
        logger.exception("Goal verdict follow-up failed for %s: %s", mid, note)
        if not removal_reported:
            await _notice(message, channel, f"⚠️ {goal['title']} (`{goal['id']}`): {note}")
        if receipt is not None:
            try:
                await receipt.edit(
                    content=mark(f"⚠️ {goal['title']} (`{goal['id']}`): {note}"),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception:
                logger.exception("Goal verdict failure archive edit failed for %s", mid)
        if isinstance(exc, asyncio.CancelledError):
            raise
    return True
