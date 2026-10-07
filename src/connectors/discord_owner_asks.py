"""One persistent decision card, bound owner controls, and a quiet notification loop."""

import asyncio
import hashlib
import io
import json
import logging
import time
from datetime import datetime

import discord

from src.core.base import IncomingMessage, MessageDelivery
from src.core.owner_asks import (
    DELIVERY_ERRORS,
    MAX_DELIVERY_ATTEMPTS,
    AskStore,
    jump,
    pending_report,
    settings,
    validate,
)

logger = logging.getLogger(__name__)
AMBER, GREEN, GREY = 0xD99B26, 0x2E9D59, 0x808080
DELIVERY_TIMEOUT = 1800


def card_marker(ask_id):
    # A recovery marker belongs in the card, but must not become visible plumbing.
    return "\u2063" + "".join("\u200c" if bit == "1" else "\u200b" for bit in f"{int(ask_id, 16):0128b}") + "\u2063"


def card(row, *, legacy=False):
    payload = row["payload"]
    title, colour = "Waiting for your answer", AMBER
    outcome = ""
    if row["state"] == "answered":
        title, colour = "Answered", GREEN
        outcome = f"{row['answer']}\nAnswered by <@{row['answered_by']}>."
    elif row["state"] == "stale":
        title, colour = "Expired without an answer", GREY
        outcome = "No approval was given. The stated default is not an approval."
    instructions = (
        "Choose a button or reply to this card."
        if payload["options"]
        else "React ✅ for yes or 🔴 for no, or reply to this card."
    )
    if row["seed_failed"]:
        instructions = "Reactions could not be added. Reply to this card with your answer."
    reply_notice = (
        "Only recipient replies up to 400 characters are passed on. Other replies are not."
        if row["state"] in ("queued", "posting", "open")
        else "Replies to closed cards are not passed to the agent."
    )
    continuation = ""
    if row.get("continuation_error"):
        reason = DELIVERY_ERRORS.get(row["continuation_error"], "delivery not confirmed")
        continuation = f"Agent not notified: {reason}. Contact the agent."
        colour = AMBER
    embed = discord.Embed(title=title, description=payload["question"], colour=colour)
    embed.add_field(name="If no reply", value=payload["default"], inline=False)
    if payload["context"]:
        embed.add_field(name="Context", value=payload["context"], inline=False)
    if payload["options"]:
        embed.add_field(name="Choices", value="\n".join(payload["options"]), inline=False)
    embed.add_field(name="Answer" if outcome else "Your decision", value=outcome or instructions, inline=False)
    if not legacy:
        embed.add_field(name="Replies", value=reply_notice, inline=False)
    if continuation:
        embed.add_field(name="Agent delivery", value=continuation, inline=False)
    # Also lets a restart adopt a confirmed own send whose acknowledgement was lost.
    embed.set_footer(text="Waiting on you" + card_marker(row["id"]))
    plain = f"{title}\n{payload['question']}\n\nIf no reply: {payload['default']}"
    if payload["context"]:
        plain += "\n\n" + payload["context"]
    if payload["options"]:
        plain += "\n\nChoices: " + " / ".join(payload["options"])
    plain += "\n\n" + (outcome or instructions)
    if not legacy:
        plain += "\n" + reply_notice
    if continuation:
        plain += "\n" + continuation
    plain += "\nWaiting on you" + card_marker(row["id"])
    return embed, plain


def goal_summary(goal, reason):
    """One line on what the goal decision is actually about.

    "verdict: Chrome Web Store listing" names the room, not the question, so
    the digest row reads as a nag with no subject. Each reason has the field
    that answers it: a blocked goal has the brief it is blocked on, a closed
    one has its close-out summary, and everything else is the goal itself.
    """
    fields = {
        "blocked": ("blocked_brief", "description"),
        "verdict": ("summary", "description"),
        "nominees": ("description", "strategy"),
    }.get(reason, ("description", "strategy"))
    for field in fields:
        value = (goal.get(field) or "").strip()
        if value:
            return value
    return ""


async def send_report(target, report, *, ephemeral=False, nonce=None):
    kwargs = dict(allowed_mentions=discord.AllowedMentions.none())
    if ephemeral:
        kwargs["ephemeral"] = True
    elif nonce is not None:
        kwargs["nonce"] = nonce
    if len(report.encode("utf-16-le")) // 2 <= 2000:
        return await target.send(report, **kwargs)
    return await target.send(
        "Full waiting list attached.", file=discord.File(io.BytesIO(report.encode()), filename="pending.txt"), **kwargs
    )


class DiscordOwnerAsks:
    def __init__(self, connector, manager, config, path):
        self.connector, self.manager = connector, manager
        self.config = config
        self.cfg = settings(config)
        self.store = AskStore(path)
        self.task = None
        self.wake = asyncio.Event()
        self.events = {}
        self.expiry_messages = {}
        self.locks = {}
        self.ready_accounts = set()
        self.recovery_checked = set()
        # Event IDs survive replay. An interrupted continuation is retried,
        # never a new answer or another reminder.
        self.store.db.execute(
            "UPDATE owner_ask_events SET state='pending',last_error='interrupted' WHERE state='running'"
        )

    def lock(self, ask_id):
        return self.locks.setdefault(ask_id, asyncio.Lock())

    async def start(self, bot):
        self.ready_accounts.add(bot.account_name)
        for row in self.store.rows(("open",)):
            if (
                row["account"] == bot.account_name
                and row["bot_id"] == str(bot.client.user.id)
                and row["payload"]["options"]
            ):
                bot.client.add_view(self.view(row), message_id=int(row["message_id"]))
        if not self.task:
            self.task = asyncio.create_task(self.run(), name="owner-asks")

    async def stop(self):
        tasks = [t for t in [self.task, *self.events.values()] if t]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.store.close()

    def bot(self, row):
        bot = self.connector.bots.get(row["account"])
        if not bot or row["account"] not in self.ready_accounts or str(bot.client.user.id) != row["bot_id"]:
            raise ValueError("The original bot is not connected")
        return bot

    async def channel(self, row):
        bot = self.bot(row)
        channel = bot.client.get_channel(int(row["channel_id"])) or await asyncio.wait_for(
            bot.client.fetch_channel(int(row["channel_id"])), 15
        )
        if str(getattr(getattr(channel, "guild", None), "id", "") or "") != row["guild_id"]:
            raise ValueError("The card channel binding changed")
        return channel

    async def ask(self, agent_id, question, default, options=None, context="", request_key=""):
        if not self.cfg["enabled"] or not self.cfg["owner_id"]:
            raise ValueError("Configure waiting_on_you.owner_id (or exactly one admin_users.discord owner) first")
        if agent_id not in self.manager.agent_configs:
            raise ValueError("Unknown requesting agent")
        payload = validate(question, default, options, context, request_key)
        home = await self.manager._resolve_home_channel(agent_id)
        if not home or home[0] != "discord" or not home[2]:
            raise ValueError("This agent needs an explicit Discord home channel and bot account")
        _, channel_id, account = home
        bot = self.connector.bots.get(account)
        if not bot or account not in self.ready_accounts:
            raise ValueError("The requesting agent bot is not connected")
        channel = bot.client.get_channel(int(channel_id)) or await asyncio.wait_for(
            bot.client.fetch_channel(int(channel_id)), 15
        )
        if self.connector._reserved_alert(str(channel_id)):
            raise ValueError("Use an agent home channel, not an application alert channel")
        row = self.store.create(
            agent_id=agent_id,
            recipient_id=self.cfg["owner_id"],
            account=account,
            channel_id=str(channel_id),
            guild_id=str(getattr(getattr(channel, "guild", None), "id", "") or ""),
            bot_id=str(bot.client.user.id),
            payload=payload,
            cfg=self.cfg,
        )
        if row["state"] in ("queued", "posting"):
            await self.publish(row["id"])
        row = self.store.get(row["id"])
        return {
            "id": row["id"],
            "state": row["state"],
            "url": jump(row),
            "note": "The answer will return in this agent channel. Do not wait inside this tool or repost the ask.",
        }

    def view(self, row, disabled=False):
        if not row["payload"]["options"]:
            return None
        view = discord.ui.View(timeout=None)
        for index, label in enumerate(row["payload"]["options"]):
            button = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.primary,
                custom_id=f"owner-ask:{row['id']}:{row['revision']}:{index}",
                disabled=disabled or row["state"] != "open",
            )

            async def clicked(interaction, index=index, revision=row["revision"], ask_id=row["id"]):
                await self.click(ask_id, revision, index, interaction)

            button.callback = clicked
            view.add_item(button)
        return view

    def bound(self, row, bot, user, channel_id, guild_id, message):
        return bool(
            row
            and not user.bot
            and str(user.id) == row["recipient_id"]
            and bot.account_name == row["account"]
            and str(bot.client.user.id) == row["bot_id"]
            and str(channel_id) == row["channel_id"]
            and str(guild_id or "") == row["guild_id"]
            and str(message.id) == row["message_id"]
            and str(message.author.id) == row["bot_id"]
            and not getattr(message, "webhook_id", None)
        )

    def finish(self, ask_id, **kwargs):
        changed = self.store.finish(ask_id, **kwargs)
        if changed:
            self.wake.set()
        return changed

    async def click(self, ask_id, revision, index, interaction):
        row = self.store.get(ask_id)
        bot = self.connector.bots.get(row["account"]) if row else None
        if (
            not bot
            or interaction.client is not bot.client
            or not self.bound(
                row, bot, interaction.user, interaction.channel_id, interaction.guild_id, interaction.message
            )
        ):
            await interaction.response.send_message(
                "Only the person this ask is addressed to can answer this card.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        async with self.lock(ask_id):
            row = self.store.get(ask_id)
            options = row["payload"]["options"] or []
            ok = (
                row["revision"] == revision
                and 0 <= index < len(options)
                and self.finish(ask_id, answer=options[index], actor=str(interaction.user.id))
            )
            await self.edit(ask_id)
        await interaction.followup.send(
            "Answer recorded."
            if ok and self.store.get(ask_id)["state"] == "answered"
            else "This ask is already closed or expired.",
            ephemeral=True,
        )

    async def react(self, bot, payload):
        row = self.store.for_message(payload.message_id)
        if not row:
            return False
        # Consume all events on known cards, including other clients and stale
        # cards, so the generic approval reaction handler cannot wake an agent.
        if row["account"] != bot.account_name or row["payload"]["options"] or str(payload.emoji) not in ("✅", "🔴"):
            return True
        try:
            user = getattr(payload, "member", None) or await bot.client.fetch_user(payload.user_id)
            message = await (await self.channel(row)).fetch_message(payload.message_id)
            if self.bound(row, bot, user, payload.channel_id, payload.guild_id, message):
                async with self.lock(row["id"]):
                    self.finish(row["id"], answer="Yes" if str(payload.emoji) == "✅" else "No", actor=str(user.id))
                    await self.edit(row["id"])
        except Exception as exc:
            logger.warning("Owner ask reaction unavailable (%s)", type(exc).__name__)
        return True

    async def reply(self, bot, message):
        reference = getattr(message, "reference", None)
        row = self.store.for_message(getattr(reference, "message_id", "")) if reference else None
        if not row:
            return False
        if row["account"] != bot.account_name:
            return True
        try:
            original = await (await self.channel(row)).fetch_message(int(row["message_id"]))
            if self.bound(
                row,
                bot,
                message.author,
                message.channel.id,
                getattr(getattr(message, "guild", None), "id", None),
                original,
            ):
                answer = message.content.strip()
                # Full meaning must survive; do not silently cut an answer.
                if answer and len(answer.encode("utf-16-le")) // 2 <= 400:
                    async with self.lock(row["id"]):
                        self.finish(row["id"], answer=answer, actor=str(message.author.id))
                        await self.edit(row["id"])
                else:
                    await message.add_reaction("⚠️")
        except Exception as exc:
            logger.warning("Owner ask reply unavailable (%s)", type(exc).__name__)
        return True

    @staticmethod
    def owns_message(row, message):
        if str(message.author.id) != row["bot_id"] or getattr(message, "webhook_id", None):
            return False
        marker = "Waiting on you" + card_marker(row["id"])
        return any(getattr(e.footer, "text", "") == marker for e in message.embeds) or message.content.endswith(
            "\n" + marker
        )

    @staticmethod
    def initial_card_matches(row, message):
        embed, plain = card(row, legacy=row["card_version"] == 1)
        if not message.embeds:
            return message.content == plain
        if len(message.embeds) != 1 or message.content:
            return False
        actual = message.embeds[0]

        def fields(value):
            return [(f.name, f.value, f.inline) for f in value.fields]

        return (
            actual.title == embed.title
            and actual.description == embed.description
            and actual.colour == embed.colour
            and fields(actual) == fields(embed)
            and actual.footer.text == embed.footer.text
        )

    @staticmethod
    def embeds_allowed(channel):
        guild = getattr(channel, "guild", None)
        return not guild or bool(channel.permissions_for(guild.me).embed_links)

    async def publish(self, ask_id):
        async with self.lock(ask_id):
            row = self.store.get(ask_id)
            if row["state"] not in ("queued", "posting"):
                return
            try:
                channel = await self.channel(row)
                if row["state"] == "posting":
                    if ask_id in self.recovery_checked:
                        return
                    # An uncertain send is never repeated. Adopt only our exact
                    # card if it is found in bounded recent history.
                    async with asyncio.timeout(15):
                        candidates = [
                            m
                            async for m in channel.history(limit=100)
                            if self.owns_message(row, m) and self.initial_card_matches(row, m)
                        ]
                    self.recovery_checked.add(ask_id)
                    if len(candidates) != 1:
                        return
                    message = candidates[0]
                else:
                    embed, plain = card(row, legacy=row["card_version"] == 1)
                    self.store.update(ask_id, state="posting")  # Commit intent before network I/O.
                    kwargs = dict(
                        view=self.view(row, disabled=True),
                        allowed_mentions=discord.AllowedMentions.none(),
                        nonce=row["id"][:24],
                    )
                    try:
                        message = (
                            await asyncio.wait_for(channel.send(embed=embed, **kwargs), 15)
                            if self.embeds_allowed(channel)
                            else await asyncio.wait_for(channel.send(plain, **kwargs), 15)
                        )
                    except discord.Forbidden:
                        # Explicit rejection is not an uncertain send. Text can
                        # still succeed when Embed Links alone is denied.
                        message = await asyncio.wait_for(channel.send(plain, **kwargs), 15)
                if not self.owns_message(row, message) or not self.initial_card_matches(row, message):
                    raise ValueError("Posted card does not match the request")
                self.store.update(ask_id, message_id=str(message.id), state="open", dirty=1, delivery_error=None)
                if not row["payload"]["options"]:
                    try:
                        for emoji in ("✅", "🔴"):
                            await asyncio.wait_for(message.add_reaction(emoji), timeout=5)
                    except Exception:
                        self.store.update(ask_id, seed_failed=1)
                await self.edit(ask_id)
            except Exception as exc:
                self.store.update(ask_id, delivery_error=type(exc).__name__)
                logger.warning("Owner ask card delivery uncertain (%s)", type(exc).__name__)

    async def edit(self, ask_id):
        row = self.store.get(ask_id)
        if not row["message_id"] or not row["dirty"]:
            return
        try:
            channel = await self.channel(row)
            message = await asyncio.wait_for(channel.fetch_message(int(row["message_id"])), 15)
            if not self.owns_message(row, message):
                raise ValueError("Card ownership changed")
            embed, plain = card(row)
            kwargs = dict(view=self.view(row), allowed_mentions=discord.AllowedMentions.none())
            try:
                if self.embeds_allowed(channel):
                    await asyncio.wait_for(message.edit(content=None, embed=embed, **kwargs), 15)
                else:
                    await asyncio.wait_for(message.edit(content=plain, embed=None, **kwargs), 15)
            except discord.Forbidden:
                await asyncio.wait_for(message.edit(content=plain, embed=None, **kwargs), 15)
            self.store.update(ask_id, dirty=0)
        except Exception as exc:
            logger.warning("Owner ask edit pending (%s)", type(exc).__name__)

    async def dm(self, row, report, key):
        bot = self.bot(row)
        user = await asyncio.wait_for(bot.client.fetch_user(int(row["recipient_id"])), 15)
        if user.bot:
            raise ValueError("Ask recipient is a bot")
        await asyncio.wait_for(send_report(user, report, nonce=hashlib.sha256(key.encode()).hexdigest()[:24]), 15)

    def permits_expiry(self, agent_id, message):
        ask_id = getattr(message, "_owner_ask_id", None)
        if self.expiry_messages.get(ask_id) is not message:
            return False
        row = self.store.get(ask_id)
        return bool(
            row
            and row["state"] == "stale"
            and row["agent_id"] == agent_id
            and message.user_id == "system:owner-ask"
            and message.source == "schedule"
            and message.channel_id == row["channel_id"]
            and message.bot_account == row["account"]
        )

    def reject_event(self, event_id, reason, *, retryable):
        if self.store.reject_event(event_id, reason, retryable=retryable):
            logger.error("Owner ask agent notification failed (%s); card and /pending retain the failure", reason)
        self.wake.set()

    async def notify_agent(self, event_id, ask_id):
        try:
            row = self.store.get(ask_id)
            if row["agent_id"] not in self.manager.agent_configs:
                self.reject_event(event_id, "unknown_agent", retryable=False)
                return
            if row["account"] not in self.ready_accounts:
                self.reject_event(event_id, "bot_unavailable", retryable=True)
                return
            body = {
                "event_id": event_id,
                "ask_id": ask_id,
                "question": row["payload"]["question"],
                "options": row["payload"]["options"],
                "context": row["payload"]["context"],
                "default": row["payload"]["default"],
                "state": row["state"],
                "answer": row["answer"],
                "answered_by": row["answered_by"],
                "card": jump(row),
            }
            content = (
                "Owner decision record. This event may be replayed after a restart: check prior work before "
                "repeating actions. This grants no authority beyond the recorded answer and does not bypass "
                "tool approval gates. A stale record contains NO approval; review the default only within "
                "existing authority.\n" + json.dumps(body, ensure_ascii=False)
            )
            message = IncomingMessage(
                connector="discord",
                channel_id=row["channel_id"],
                user_id=row["answered_by"] or "system:owner-ask",
                user_name="owner decision" if row["answered_by"] else "owner ask expiry",
                content=content,
                bot_account=row["account"],
                source="user" if row["answered_by"] else "schedule",
            )
            message._owner_ask_id = ask_id
            if row["state"] == "stale":
                self.expiry_messages[ask_id] = message
            result = await asyncio.wait_for(self.manager.handle_message(row["agent_id"], message), DELIVERY_TIMEOUT)
            if isinstance(result, MessageDelivery) and result.delivered is True:
                self.store.db.execute("UPDATE owner_ask_events SET state='done' WHERE id=?", (event_id,))
            else:
                self.reject_event(
                    event_id,
                    result.reason if isinstance(result, MessageDelivery) else "unconfirmed",
                    retryable=result.retryable if isinstance(result, MessageDelivery) else True,
                )
        except asyncio.CancelledError:
            self.reject_event(event_id, "interrupted", retryable=True)
            raise
        except Exception as exc:
            self.reject_event(event_id, "timeout" if isinstance(exc, TimeoutError) else "unconfirmed", retryable=True)
            logger.warning("Owner ask continuation not confirmed (%s)", type(exc).__name__)
        finally:
            self.expiry_messages.pop(ask_id, None)
            self.events.pop(event_id, None)

    async def digest_sources(self, recipient, now):
        from src.connectors.discord_goal_verdicts import recipient as goal_recipient
        from src.core import goals
        from src.core.hitl_display import pending_for
        from src.core.morning_digest import jump as digest_jump

        rows = self.store.rows(recipient=recipient)
        sources = {
            "Asks": [
                {
                    "text": r["agent_id"] + ": " + r["payload"]["question"],
                    "summary": r["payload"]["context"] or ("if no reply: " + r["payload"]["default"]),
                    "created_at": r["created"],
                    "url": digest_jump(r["guild_id"], r["channel_id"], r["message_id"]) if r["message_id"] else "",
                }
                for r in sorted(rows, key=lambda r: (r["created"], r["id"]))
            ],
            "HITL approvals": [],
            "Goals": [],
        }
        # Old asks retain their recipient. The current owner's operational
        # sources must not leak to a former recipient's otherwise private list.
        if recipient != self.cfg["owner_id"]:
            return sources
        guild = str((self.config.get("connectors", {}).get("discord") or {}).get("guild_id") or "")
        try:
            gate = getattr(self.connector, "_hitl", None)
            if gate is None:
                raise RuntimeError("HITL source unavailable")
            pending = await pending_for(gate, recipient, now)
            sources["HITL approvals"] = [
                {
                    # No summary line: a gate description carries raw tool
                    # arguments, and only the card redacts them (#136).
                    "text": r["agent_id"] + ": " + r["tool_name"],
                    "created_at": r["created_at"],
                    "url": digest_jump(guild, r["channel_id"], r["message_id"]),
                }
                for r in pending
            ]
        except Exception:
            sources["HITL approvals"] = None
            logger.warning("Morning digest HITL source unavailable", exc_info=True)
        try:
            admins = [str(x) for x in (self.config.get("admin_users") or {}).get("discord", [])]
            for goal in sorted(goals.list_goals(), key=lambda g: g["updated_at"]):
                if goal["connector"] != "discord" or goal_recipient(goal, self.config, admins) != recipient:
                    continue
                status, reason, message = goal["status"], "", goal["card_message_id"]
                if status == "proposed":
                    reason, message = "kickoff", goal["kickoff_message_id"] or message
                elif status == "blocked_on_user":
                    reason = "blocked"
                elif status == "done" and not goal["verdict"]:
                    reason, message = "verdict", goal["closing_message_id"] or message
                elif status in goals.ACTIVE_STATUSES and goal["turns_since_human"] >= goal["turn_budget"]:
                    reason = "check-in"
                if status in goals.ROUTED_STATUSES:
                    nominations = goals.list_nominations(goal["id"], "pending")
                    if nominations and not reason:
                        reason, message = "nominees", nominations[0]["message_id"] or message
                if reason:
                    sources["Goals"].append(
                        {
                            "text": reason + ": " + goal["title"],
                            "summary": goal_summary(goal, reason),
                            "created_at": goal["updated_at"],
                            "url": digest_jump(guild, goal["channel_id"], message),
                        }
                    )
        except Exception:
            sources["Goals"] = None
            logger.warning("Morning digest goals source unavailable", exc_info=True)
        return sources

    def digest_accounts(self):
        """Ready accounts in configured order, primary first.

        The digest speaks for the whole fleet, so it must not arrive from
        whichever bot happens to sort first: a work bot delivering a summary of
        another project's goals reads as that bot having done the work. The
        first configured account is the deployment's primary bot, and only an
        account nobody configured falls back to alphabetical order.
        """
        config = getattr(self.connector, "config", None) or {}
        configured = list((config.get("accounts") or {}).keys())
        ordered = [a for a in configured if a in self.ready_accounts]
        return ordered + sorted(self.ready_accounts.difference(ordered))

    async def digest(self, now):
        from src.core.morning_digest import render

        local = datetime.fromtimestamp(now, self.cfg["timezone"])
        if local.hour < self.cfg["digest_hour"]:
            return
        recipients = {r["recipient_id"] for r in self.store.rows()}
        if self.cfg["owner_id"]:
            recipients.add(self.cfg["owner_id"])
        day = local.date().isoformat()
        for recipient in sorted(recipients):
            if self.store.db.execute(
                "SELECT 1 FROM owner_ask_digests WHERE recipient_id=? AND day=?", (recipient, day)
            ).fetchone():
                continue
            sender = None
            for account in self.digest_accounts():
                bot = self.connector.bots.get(account)
                if bot and bot.client.user:
                    sender = {"account": account, "bot_id": str(bot.client.user.id), "recipient_id": recipient}
                    break
            if sender is None:
                continue
            report = render(await self.digest_sources(recipient, now), now)
            if not report or not self.store.claim_digest(recipient, day):
                continue
            try:
                await self.dm(sender, report, "digest:" + recipient + ":" + day)
                status = "sent"
            except Exception as exc:
                status = "failed"
                logger.warning("Owner ask digest not confirmed (%s)", type(exc).__name__)
            self.store.db.execute(
                "UPDATE owner_ask_digests SET status=? WHERE recipient_id=? AND day=?", (status, recipient, day)
            )

    async def tick(self, now=None):
        now = time.time() if now is None else now
        if not self.cfg["enabled"]:
            return
        for row in self.store.rows():
            async with self.lock(row["id"]):
                if row["stale_at"] <= now:
                    self.finish(row["id"], now=now)
            row = self.store.get(row["id"])
            if row["account"] not in self.ready_accounts:
                continue
            if row["state"] in ("queued", "posting"):
                await self.publish(row["id"])
            elif self.store.claim_reminder(row["id"], now):
                try:
                    await self.dm(
                        row,
                        "One reminder: your answer is still needed.\n"
                        + pending_report([row], now)
                        + "\nIf no reply: "
                        + row["payload"]["default"],
                        "reminder:" + row["id"],
                    )
                    status = "sent"
                except Exception as exc:
                    status = "failed"
                    logger.warning("Owner ask reminder not confirmed (%s)", type(exc).__name__)
                self.store.update(row["id"], reminder_status=status)
        for row in self.store.db.execute("SELECT id FROM owner_asks WHERE dirty=1").fetchall():
            async with self.lock(row["id"]):
                await self.edit(row["id"])
        await self.digest(now)
        for event in self.store.db.execute("SELECT * FROM owner_ask_events WHERE state='pending'").fetchall():
            if event["attempts"] >= MAX_DELIVERY_ATTEMPTS:
                self.reject_event(event["id"], event["last_error"] or "interrupted", retryable=False)
            elif self.store.claim_event(event["id"], now):
                self.events[event["id"]] = asyncio.create_task(self.notify_agent(event["id"], event["ask_id"]))

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception as exc:
                logger.warning("Owner ask maintenance pending (%s)", type(exc).__name__)
            try:
                await asyncio.wait_for(self.wake.wait(), 30)
            except TimeoutError:
                pass
            self.wake.clear()


def register_pending(bot):
    @bot.tree.command(name="pending", description="List decisions waiting for your answer")
    async def pending(interaction: discord.Interaction):
        service = getattr(bot.connector, "_owner_asks", None)
        if interaction.user.bot or not service:
            await interaction.response.send_message("Waiting list is unavailable.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        recipient = None if str(interaction.user.id) == service.cfg["owner_id"] else str(interaction.user.id)
        await send_report(
            interaction.followup,
            pending_report(
                service.store.rows(recipient=recipient), failed_rows=service.store.failed_rows(recipient=recipient)
            ),
            ephemeral=True,
        )
