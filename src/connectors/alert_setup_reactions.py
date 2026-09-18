"""Durable, owner-bound reactions for the two binary alert setup questions."""

import asyncio
import hashlib
import json

import discord

from src.core.alert_errors import log_failure
from src.core.decision_reactions import decision_reactions


class SetupReactions:
    def __init__(self, alerts):
        self.alerts = alerts
        self.pair = decision_reactions("Go/no-go?")
        alerts.store.db.execute(
            "CREATE TABLE IF NOT EXISTS alert_setup_reactions ("
            "message_id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE, "
            "binding TEXT NOT NULL, phase TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0)"
        )

    @staticmethod
    def phase(source):
        if not source or source["state"] != "draft" or not source["waiting"]:
            return None
        config = source["config"]
        if not source["guild_id"] or not all(
            k in config for k in ("service", "app", "repo", "project", "api_key", "triggers")
        ):
            return None
        return "confirm" if type(config.get("auto_fix_pr")) is bool else "automatic"

    @staticmethod
    def binding(source):
        fields = {
            key: source[key] for key in ("id", "revision", "account", "user_id", "owner", "dm_id", "guild_id", "config")
        }
        return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()

    def invalidate(self, source):
        self.alerts.store.db.execute("UPDATE alert_setup_reactions SET used=1 WHERE source_id=?", (source["id"],))

    def prompt(self, source, text):
        phase = self.phase(source)
        if not phase:
            return text
        yes, no = self.pair
        legend = (
            f"React {yes} to create or {no} to cancel."
            if phase == "confirm"
            else f"React {yes} for yes or {no} for no."
        )
        return text + "\n\n" + legend

    async def send(self, bot, channel, source, text):
        message = await channel.send(self.prompt(source, text), allowed_mentions=discord.AllowedMentions.none())
        await self.seed(bot, message, source, text)
        return message

    async def seed(self, bot, message, source, text):
        # Callers hold the source lock across send, binding and seeding. A real
        # reaction may arrive during seeding, but cannot race a partial prompt.
        phase = self.phase(source)
        if not phase:
            return
        self.invalidate(source)
        self.alerts.store.db.execute(
            "INSERT INTO alert_setup_reactions(message_id,source_id,binding,phase) VALUES(?,?,?,?)",
            (str(message.id), source["id"], self.binding(source), phase),
        )
        try:
            for emoji in self.pair:
                await asyncio.wait_for(message.add_reaction(emoji), 5)
        except Exception as error:
            self.invalidate(source)
            log_failure(error, "setup reaction seeding")
            fallback = (
                text
                + "\n\nReaction controls are unavailable. "
                + ("Reply CREATE to proceed or CANCEL to stop." if phase == "confirm" else "Reply yes or no.")
            )
            try:
                await message.edit(content=fallback, allowed_mentions=discord.AllowedMentions.none())
            except Exception as edit_error:
                log_failure(edit_error, "setup reaction fallback edit")
                try:
                    await message.channel.send(fallback, allowed_mentions=discord.AllowedMentions.none())
                except Exception as send_error:
                    log_failure(send_error, "setup reaction fallback send")

    async def react(self, bot, payload):
        row = self.alerts.store.db.execute(
            "SELECT * FROM alert_setup_reactions WHERE message_id=?", (str(payload.message_id),)
        ).fetchone()
        if row is None:
            return False
        # Consume reactions on our bound prompts even when unauthorized or old,
        # so they cannot become unrelated general-agent approval/chat triggers.
        async with self.alerts.locks.setdefault(row["source_id"], asyncio.Lock()):
            source = self.alerts.store.get(row["source_id"])
            if source:
                self.alerts.store.expire_drafts(source["account"], source["user_id"], source["dm_id"])
            source = self.alerts.store.get(row["source_id"])
            row = self.alerts.store.db.execute(
                "SELECT * FROM alert_setup_reactions WHERE message_id=?", (str(payload.message_id),)
            ).fetchone()
            if (
                not row
                or row["used"]
                or not self.phase(source)
                or bot.account_name != source["account"]
                or str(payload.user_id) != source["user_id"]
                or not self.alerts.admin(payload.user_id)
                or payload.user_id == bot.client.user.id
                or getattr(getattr(payload, "member", None), "bot", False)
                or str(payload.channel_id) != source["dm_id"]
                or getattr(payload, "guild_id", None) is not None
                or row["binding"] != self.binding(source)
                or row["phase"] != self.phase(source)
                or str(payload.emoji) not in self.pair
            ):
                return True
            try:
                channel = await asyncio.wait_for(bot.client.fetch_channel(int(source["dm_id"])), 15)
                message = await asyncio.wait_for(channel.fetch_message(payload.message_id), 15)
                if (
                    getattr(channel, "guild", None) is not None
                    or str(channel.id) != source["dm_id"]
                    or message.author.id != bot.client.user.id
                    or getattr(message, "webhook_id", None)
                    or str(message.id) != str(payload.message_id)
                ):
                    return True
            except Exception as error:
                log_failure(error, "setup reaction binding")
                return True
            # State can change during Discord reads through lifecycle actions.
            current = self.alerts.store.get(source["id"])
            if not self.phase(current) or self.binding(current) != row["binding"]:
                return True
            positive = str(payload.emoji) == self.pair[0]
            text = ("CREATE" if positive else "CANCEL") if row["phase"] == "confirm" else ("yes" if positive else "no")
            response = await self.alerts.setup_answer(current, bot, text)
            if response is not None:
                await self.send(bot, channel, self.alerts.store.get(source["id"]), response)
        return True
