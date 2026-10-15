"""One card per secret request: an owner-only button opens a private form, and
the value goes straight from the form into the vault.

The value is never logged, never echoed, never put on the card or in the store,
and never passed to the agent. The agent only learns that the key was saved.
"""

import asyncio
import logging
import time

import discord

from src.core.base import IncomingMessage
from src.core.owner_asks import settings
from src.core.owner_secrets import SecretStore, allowed_prefixes, validate

logger = logging.getLogger(__name__)
AMBER, GREEN, GREY = 0xD99B26, 0x2E9D59, 0x808080
NOTIFY_TIMEOUT = 1800


def card(row):
    """(embed, plain text) for a request in its current state. Never holds the value."""
    agent, label = row["agent_id"], row["label"]
    stored = f"Stored straight in the kbots vault as `{row['key']}`. {agent} never sees the value."
    if row["state"] == "saved":
        title, colour = f"Saved: {label}", GREEN
        status = f"Saved by <@{row['done_by']}>. {agent} can use it now."
    elif row["state"] == "declined":
        title, colour = f"Not provided: {label}", GREY
        status = f"Declined by <@{row['done_by']}>. Nothing was stored."
    elif row["state"] == "stale":
        title, colour = f"Expired: {label}", GREY
        status = "Nothing was stored. The agent can ask again."
    else:
        title, colour = f"{agent} needs: {label}", AMBER
        status = "Tap **Enter** to type it in a private form only you can see."
    embed = discord.Embed(title=title[:256], description=row["purpose"], colour=colour)
    embed.add_field(name="Where it goes", value=stored, inline=False)
    embed.add_field(name="Status", value=status, inline=False)
    embed.set_footer(text="Secret request")
    plain = f"**{title}**\n{row['purpose']}\n{stored}\n{status}"
    return embed, plain


class SecretModal(discord.ui.Modal):
    def __init__(self, owner, row):
        super().__init__(title=row["label"][:45], timeout=600)
        self.owner, self.request_id, self.revision = owner, row["id"], row["revision"]
        self.value = discord.ui.TextInput(
            label=row["label"][:45], style=discord.TextStyle.short, required=True, max_length=1000,
            placeholder="Only you see this form. It is not posted in the channel.")
        self.add_item(self.value)

    async def on_submit(self, interaction: discord.Interaction):
        await self.owner.submit(self.request_id, self.revision, self.value.value, interaction)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        # Never let discord.py log the exception: its locals hold the value.
        logger.warning("Secret form failed (%s)", type(error).__name__)
        if not interaction.response.is_done():
            await interaction.response.send_message("Nothing was saved. Try again.", ephemeral=True)


class DiscordOwnerSecrets:
    def __init__(self, connector, manager, config, path):
        self.connector, self.manager = connector, manager
        self.owner_id = settings(config)["owner_id"]
        self.prefixes = allowed_prefixes(config)
        self.store = SecretStore(path)
        self.locks: dict[str, asyncio.Lock] = {}
        self.tasks: set[asyncio.Task] = set()

    def lock(self, request_id):
        return self.locks.setdefault(request_id, asyncio.Lock())

    async def start(self, bot):
        self.store.expire()
        for row in self.store.rows(("open",)):
            if row["account"] == bot.account_name and row["bot_id"] == str(bot.client.user.id):
                bot.client.add_view(self.view(row), message_id=int(row["message_id"]))

    async def stop(self):
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.store.close()

    async def request(self, agent_id, key, label, purpose, request_key=""):
        if not self.owner_id:
            raise ValueError("Configure waiting_on_you.owner_id (or exactly one admin_users.discord owner) first")
        if agent_id not in self.manager.agent_configs:
            raise ValueError("Unknown requesting agent")
        fields = validate(key, label, purpose, request_key, self.prefixes)
        home = await self.manager._resolve_home_channel(agent_id)
        if not home or home[0] != "discord" or not home[2]:
            raise ValueError("This agent needs an explicit Discord home channel and bot account")
        _, channel_id, account = home
        bot = self.connector.bots.get(account)
        if not bot:
            raise ValueError("The requesting agent bot is not connected")
        channel = bot.client.get_channel(int(channel_id)) or await asyncio.wait_for(
            bot.client.fetch_channel(int(channel_id)), 15)
        row = self.store.create(
            agent_id=agent_id, recipient_id=self.owner_id, account=account, channel_id=str(channel_id),
            guild_id=str(getattr(getattr(channel, "guild", None), "id", "") or ""),
            bot_id=str(bot.client.user.id), fields=fields)
        if row["state"] == "queued":
            await self.publish(row["id"], channel)
        row = self.store.get(row["id"])
        return {"id": row["id"], "state": row["state"], "key": row["key"],
                "note": "The owner enters the value in a private form; you will be told when it is saved. "
                        "Do not ask for it in chat and do not repost this request."}

    def view(self, row):
        if row["state"] != "open":
            return None
        view = discord.ui.View(timeout=None)
        enter = discord.ui.Button(label=f"Enter {row['label']}"[:80], style=discord.ButtonStyle.primary,
                                  custom_id=f"owner-secret:{row['id']}:{row['revision']}:enter")
        decline = discord.ui.Button(label="Not now", style=discord.ButtonStyle.secondary,
                                    custom_id=f"owner-secret:{row['id']}:{row['revision']}:decline")

        async def on_enter(interaction, request_id=row["id"]):
            await self.click(request_id, interaction, decline=False)

        async def on_decline(interaction, request_id=row["id"]):
            await self.click(request_id, interaction, decline=True)

        enter.callback, decline.callback = on_enter, on_decline
        view.add_item(enter)
        view.add_item(decline)
        return view

    def bound(self, row, interaction) -> bool:
        """Only the recipient, on this exact card, through the bot that posted it."""
        bot = self.connector.bots.get(row["account"]) if row else None
        message = getattr(interaction, "message", None)
        return bool(
            bot and interaction.client is bot.client
            and str(interaction.user.id) == row["recipient_id"] and not interaction.user.bot
            and str(interaction.channel_id) == row["channel_id"]
            and str(interaction.guild_id or "") == row["guild_id"]
            and message is not None and str(message.id) == row["message_id"]
            and str(message.author.id) == row["bot_id"]
        )

    async def click(self, request_id, interaction, *, decline):
        row = self.store.get(request_id)
        if not row or not self.bound(row, interaction):
            await interaction.response.send_message("Only the owner can answer this request.", ephemeral=True)
            return
        if row["state"] != "open" or row["stale_at"] <= time.time():
            await interaction.response.send_message("This request is already closed or expired.", ephemeral=True)
            return
        if not decline:
            # A modal must be the first response; it carries the revision it was drawn for.
            await interaction.response.send_modal(SecretModal(self, row))
            return
        await interaction.response.defer(ephemeral=True)
        async with self.lock(request_id):
            ok = self.store.finish(request_id, state="declined", actor=str(interaction.user.id),
                                   revision=row["revision"])
            await self.edit(request_id)
        if ok:
            self.notify(request_id)
        await interaction.followup.send("Declined. Nothing was stored." if ok else "Already closed.",
                                        ephemeral=True)

    async def submit(self, request_id, revision, value, interaction):
        row = self.store.get(request_id)
        # A modal submit carries no card message, so bind on recipient, bot and channel.
        bot = self.connector.bots.get(row["account"]) if row else None
        if (not row or not bot or interaction.client is not bot.client
                or str(interaction.user.id) != row["recipient_id"]
                or str(interaction.channel_id) != row["channel_id"]):
            await interaction.response.send_message("Only the owner can answer this request.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        value = value.strip("\r\n")
        saved = False
        async with self.lock(request_id):
            row = self.store.get(request_id)
            if row["state"] == "open" and row["revision"] == revision and row["stale_at"] > time.time() and value:
                try:
                    self.connector.vault.set(row["key"], value)
                except Exception as exc:
                    logger.warning("Secret request %s: vault write failed (%s)", request_id, type(exc).__name__)
                else:
                    saved = self.store.finish(request_id, state="saved", actor=str(interaction.user.id),
                                              revision=revision)
            await self.edit(request_id)
        if saved:
            logger.info("Secret request %s: owner saved vault key %s", request_id, row["key"])
            self.notify(request_id)
        await interaction.followup.send(
            f"Saved to the vault as `{row['key']}`." if saved else "Nothing was saved: this request is closed.",
            ephemeral=True)

    async def publish(self, request_id, channel):
        async with self.lock(request_id):
            row = self.store.get(request_id)
            if row["state"] != "queued":
                return
            self.store.update(request_id, state="posting")      # commit intent before network I/O
            try:
                row["state"] = "open"                           # draw the card as it will be once posted
                embed, plain = card(row)
                kwargs = dict(view=self.view(row), allowed_mentions=discord.AllowedMentions.none())
                guild = getattr(channel, "guild", None)
                if not guild or channel.permissions_for(guild.me).embed_links:
                    message = await asyncio.wait_for(channel.send(embed=embed, **kwargs), 15)
                else:
                    message = await asyncio.wait_for(channel.send(plain, **kwargs), 15)
                self.store.update(request_id, message_id=str(message.id), state="open")
            except Exception as exc:
                logger.warning("Secret request card delivery uncertain (%s)", type(exc).__name__)

    async def edit(self, request_id):
        row = self.store.get(request_id)
        if not row["message_id"] or not row["dirty"]:
            return
        try:
            bot = self.connector.bots[row["account"]]
            channel = bot.client.get_channel(int(row["channel_id"])) or await bot.client.fetch_channel(
                int(row["channel_id"]))
            message = await asyncio.wait_for(channel.fetch_message(int(row["message_id"])), 15)
            embed, plain = card(row)
            kwargs = dict(view=self.view(row), allowed_mentions=discord.AllowedMentions.none())
            if message.embeds:
                await asyncio.wait_for(message.edit(content=None, embed=embed, **kwargs), 15)
            else:
                await asyncio.wait_for(message.edit(content=plain, embed=None, **kwargs), 15)
            self.store.update(request_id, dirty=0)
        except Exception as exc:
            logger.warning("Secret request edit pending (%s)", type(exc).__name__)

    def notify(self, request_id):
        """Tell the agent the outcome (never the value) so it can carry on."""
        task = asyncio.create_task(self._notify(request_id))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _notify(self, request_id):
        row = self.store.get(request_id)
        outcome = ("was saved in the vault" if row["state"] == "saved"
                   else "was declined by the owner; nothing was stored")
        content = (f"Owner secret record: '{row['label']}' (vault key {row['key']}) {outcome}. "
                   "The value is never shared with you; tools read it from the vault. "
                   "This record grants no other authority.")
        message = IncomingMessage(
            connector="discord", channel_id=row["channel_id"], user_id=row["done_by"],
            user_name="owner secret", content=content, bot_account=row["account"], source="user")
        try:
            await asyncio.wait_for(self.manager.handle_message(row["agent_id"], message), NOTIFY_TIMEOUT)
        except Exception as exc:
            logger.warning("Secret request %s: agent not notified (%s)", request_id, type(exc).__name__)
