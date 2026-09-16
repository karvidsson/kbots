"""Admin-owned DM setup and private alert rooms. No LLM in the provisioning path."""

import asyncio
import importlib
import json
import re
import time
import unicodedata
from pathlib import Path

import discord
from discord import app_commands
from discord.state import ConnectionState

from src.core.alert_channels import AlertError, AlertStore, ensure_operation
from src.core.alert_credentials import CredentialEntry
from src.core.alert_diagnosis import AlertWorker, incident_label, public_prose, public_text
from src.core.alert_errors import failure_reason, log_failure
from src.core.alert_lifecycle import AlertLifecycle
from src.core.alert_operator import OperatorRehearsal
from src.core.alert_repositories import resolve_repository


def channel_app_name(text):
    """Convert a human app name to the bounded channel suffix used in setup."""
    folded = unicodedata.normalize("NFKD", text.casefold())
    folded = "".join(c for c in folded if unicodedata.category(c) not in {"Mn", "Cf"})
    return re.sub(r"[^a-z0-9]+", "-", folded).strip("-")[:41].rstrip("-") or "app"


class AlertChannelState(ConnectionState):
    """Retain one gateway flag discarded by discord.py 2.7 TextChannel.

    No raw payload or channel metadata is retained. The normal parser still
    owns every event; this shim must be covered when upgrading discord.py.
    """

    def clear(self, **kwargs):
        super().clear(**kwargs)
        self.alert_obfuscated = {}

    def _alert_channel_flags(self, data, guild_id=None):
        channel_id = str(data["id"])
        if int(data.get("flags", 0)) & (1 << 17):
            self.alert_obfuscated[channel_id] = str(guild_id or data.get("guild_id", ""))
        else:
            self.alert_obfuscated.pop(channel_id, None)

    def parse_channel_create(self, data):
        self._alert_channel_flags(data)
        super().parse_channel_create(data)

    def parse_channel_update(self, data):
        self._alert_channel_flags(data)
        super().parse_channel_update(data)

    def parse_channel_delete(self, data):
        self.alert_obfuscated.pop(str(data["id"]), None)
        super().parse_channel_delete(data)

    def parse_guild_create(self, data):
        if not data.get("unavailable"):
            self.alert_obfuscated = {k: v for k, v in self.alert_obfuscated.items() if v != str(data["id"])}
            for channel in data.get("channels", []):
                self._alert_channel_flags(channel, data["id"])
        super().parse_guild_create(data)


class AlertChannelClient(discord.Client):
    def _get_state(self, **options):
        return AlertChannelState(
            dispatch=self.dispatch, handlers=self._handlers, hooks=self._hooks, http=self.http, **options
        )


def has_marker(message, marker):
    return (message.content.endswith(marker) or message.content.endswith("||" + marker + "||")) or any(
        getattr(getattr(embed, "footer", None), "text", None) == marker for embed in getattr(message, "embeds", [])
    )


def marked_message(text, marker, channel):
    guild = getattr(channel, "guild", None)
    member = getattr(guild, "me", None)
    embeds = guild is None or (member is not None and channel.permissions_for(member).embed_links is True)
    return {
        "content": public_prose(text, min(1920, 2000 - len(marker) - 5) if not embeds else 1920)
        + ("" if embeds else "\n||" + marker + "||"),
        "embed": discord.Embed().set_footer(text=marker) if embeds else None,
    }


class DiscordAlertTransport:
    def __init__(self, connector, store):
        self.connector, self.store = connector, store
        self.message_locks = {}

    def bot(self, source):
        bot = self.connector.bots.get(source["account"])
        if not bot or not bot.client.user:
            raise AlertError("Registered bot is unavailable")
        return bot

    async def channel(self, source):
        channel = await self.bot(source).client.fetch_channel(int(source["channel_id"]))
        if str(getattr(getattr(channel, "guild", None), "id", "")) != source["guild_id"]:
            raise AlertError("Alert channel is in a different guild")
        return channel

    access_retry_delay = 1

    async def access_status(self, scope, check):
        """Retry a transient read once. Only a specific channel 404 proves absence."""
        for attempt in range(2):
            try:
                async with asyncio.timeout(15):
                    return await check()
            except Exception as error:
                status = getattr(error, "status", None)
                transient = isinstance(error, (TimeoutError, OSError)) or (
                    isinstance(error, discord.HTTPException) and (status == 429 or status >= 500)
                )
                if transient and attempt == 0:
                    await asyncio.sleep(self.access_retry_delay)
                    continue
                if isinstance(error, discord.NotFound) and scope == "channel" and status == 404 and error.code == 10003:
                    return "missing"
                log_failure(error, f"{scope} access check")
                if isinstance(error, discord.Forbidden):
                    return "access denied"
                if isinstance(error, discord.NotFound):
                    if scope == "guild" and error.code == 10007:
                        return "membership missing"
                    return "guild unavailable" if scope == "guild" or error.code == 10004 else "resource unavailable"
                if transient:
                    return "temporarily unavailable"
                if isinstance(error, discord.HTTPException):
                    return "request refused"
                return "check failed"

    async def guild_status(self, source):
        async def check():
            bot = self.connector.bots.get(source["account"])
            if not bot or not bot.client.user:
                return "bot unavailable"
            client = bot.client
            guild = await client.fetch_guild(int(source["guild_id"]))
            if str(guild.id) != source["guild_id"]:
                return "identity mismatch"
            await guild.fetch_member(client.user.id)
            return "present"

        return await self.access_status("guild", check)

    @staticmethod
    def obfuscated(channel):
        flags = getattr(channel, "flags", 0)
        return bool(getattr(flags, "value", flags) & (1 << 17))

    async def channel_status(self, source, event=None):
        async def check():
            bot = self.connector.bots.get(source["account"])
            if not bot or not bot.client.user:
                return "bot unavailable"
            client = bot.client
            state = getattr(client, "_connection", None)
            if source["channel_id"] in getattr(state, "alert_obfuscated", {}):
                return "obfuscated"
            cached = getattr(client, "get_channel", lambda _: None)(int(source["channel_id"]))
            if cached is not None and self.obfuscated(cached):
                return "obfuscated"
            if event is not None and self.obfuscated(event):
                return "obfuscated"
            channel = await client.fetch_channel(int(source["channel_id"]))
            if str(getattr(getattr(channel, "guild", None), "id", "")) != source["guild_id"]:
                return "identity mismatch"
            return "obfuscated" if self.obfuscated(channel) else "present"

        return await self.access_status("channel", check)

    async def lifecycle_notice(self, notice):
        source = json.loads(notice["context"])
        if source.get("target") == "operator":
            # The original text remains in the durable local transcript. Never
            # fabricate a DM or deliver rehearsal notices to the parent owner.
            self.store.db.execute("UPDATE lifecycle_notices SET state='complete' WHERE id=?", (notice["id"],))
            return
        client = self.bot(source).client
        if source.get("target") == "dm":
            current = self.store.get(source["id"])
            if source.get("outcome") != "expired" and (
                not current
                or current["revision"] != source.get("revision")
                or current["state"] in {"disabled", "deleting"}
                or (source.get("outcome") == "active" and current["state"] != "active")
                or (source.get("outcome", "").endswith("held") and current["state"] == "active")
            ):
                self.store.db.execute("UPDATE lifecycle_notices SET state='cancelled' WHERE id=?", (notice["id"],))
                return
            channel = await client.fetch_channel(int(source["dm_id"]))
            if (
                getattr(channel, "guild", None) is not None
                or str(getattr(getattr(channel, "recipient", None), "id", "")) != source["user_id"]
                or str(channel.id) != source["dm_id"]
            ):
                raise AlertError("Setup DM identity could not be verified; notice retained")
        else:
            home = await self.connector._agent_manager._resolve_home_channel(source["owner"])
            if (
                not home
                or home[0] != "discord"
                or home[2] not in {None, source["account"]}
                or home[1] == source["channel_id"]
                or self.store.channel(home[1])
            ):
                raise AlertError("Responsible agent home channel is unavailable; lifecycle notice retained")
            channel = await client.fetch_channel(int(home[1]))
        async with asyncio.timeout(20):
            marker = f"[alert-lifecycle:{notice['id']}]"
            matches = [
                str(m.id)
                async for m in channel.history(limit=100)
                if m.author.id == client.user.id and not m.webhook_id and has_marker(m, marker)
            ]
            if matches:
                message_id = matches[0]
            elif notice["state"] == "sending":
                raise AlertError("Lifecycle notice delivery is uncertain; no duplicate submitted")
            else:
                self.store.db.execute("UPDATE lifecycle_notices SET state='sending' WHERE id=?", (notice["id"],))
                try:
                    message = await channel.send(
                        **marked_message(notice["text"], marker, channel),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except (discord.Forbidden, discord.NotFound):
                    # Discord explicitly refused the send, so a later retry is safe.
                    self.store.db.execute("UPDATE lifecycle_notices SET state='pending' WHERE id=?", (notice["id"],))
                    raise
                message_id = str(message.id)
            self.store.db.execute(
                "UPDATE lifecycle_notices SET state='complete',message_id=? WHERE id=?", (message_id, notice["id"])
            )

    async def say(self, source, text):
        channel = await self.channel(source)
        return await channel.send(public_text(text), allowed_mentions=discord.AllowedMentions.none())

    async def provision(self, source):
        source = self.store.require_setup(source)
        bot = self.bot(source)
        guild = bot.client.get_guild(int(source["guild_id"]))
        if not guild:
            raise AlertError("Selected guild is unavailable to this bot")
        marker = "kbots-alert:" + source["id"]
        if not source["channel_id"]:

            async def find_channel():
                return [
                    {"id": str(c.id)}
                    for c in await guild.fetch_channels()
                    if isinstance(c, discord.TextChannel) and c.topic == marker
                ]

            async def create_channel():
                member = await guild.fetch_member(int(source["user_id"]))
                bot_member = await guild.fetch_member(bot.client.user.id)
                self.store.require_setup(source)
                channel = await guild.create_text_channel(
                    "alerts-" + source["config"]["app"],
                    topic=marker,
                    overwrites={
                        guild.default_role: discord.PermissionOverwrite(view_channel=False),
                        member: discord.PermissionOverwrite(
                            view_channel=True, send_messages=True, read_message_history=True
                        ),
                        bot_member: discord.PermissionOverwrite(
                            view_channel=True,
                            send_messages=True,
                            manage_webhooks=True,
                            read_message_history=True,
                            embed_links=True
                            if getattr(getattr(bot_member, "guild_permissions", None), "embed_links", False)
                            else None,
                        ),
                    },
                )
                return {"id": str(channel.id)}

            channel = await ensure_operation(self.store, source, "channel", find_channel, create_channel)
            self.store.require_setup(source)
            source = self.store.update(source["id"], channel_id=channel["id"])
        channel = await self.channel(source)
        if getattr(channel, "topic", None) != marker:
            raise AlertError("Channel setup marker changed; refusing provisioning")
        webhook_name = f"kbots-{source['id']}-r{source['revision']}"
        secret_ref = f"secrets/alert-webhook-{source['id']}-r{source['revision']}"

        def retain(webhook):
            if not webhook.token:
                raise AlertError("Owned webhook cannot be recovered with this bot")
            self.connector.vault.set(secret_ref, webhook.url)
            return {"id": str(webhook.id)}

        async def find_webhook():
            return [
                retain(w)
                for w in await channel.webhooks()
                if w.name == webhook_name and w.user and w.user.id == bot.client.user.id
            ]

        async def create_webhook():
            self.store.require_setup(source)
            return retain(await channel.create_webhook(name=webhook_name))

        self.store.require_setup(source)
        webhook = await ensure_operation(self.store, source, "webhook", find_webhook, create_webhook)
        self.store.require_setup(source)
        source = self.store.update(source["id"], webhook_id=webhook["id"], state="provisional")
        url = self.connector.vault.get(secret_ref)
        if not url:
            raise AlertError("Setup webhook credential is unavailable; rotate the registration")
        return source, url

    async def _notice(self, source, receipt, step, text):
        """One editable status per receipt; recover old start/result messages too."""
        async with self.message_locks.setdefault(receipt["id"], asyncio.Lock()):
            current = self.store.get(source["id"])
            row = self.store.db.execute("SELECT state FROM receipts WHERE id=?", (receipt["id"],)).fetchone()
            if (
                not current
                or current["revision"] != source["revision"]
                or current["state"] not in {"active", "provisional"}
            ):
                raise AlertError("Registration changed before posting")
            if row and (row["state"] == "complete" or (step != "result" and row["state"] == "ready")):
                return None
            if step == "queued" and row and row["state"] != "pending":
                return None
            channel = await self.channel(source)
            bot_id = self.bot(source).client.user.id
            marker = f"[alert:{receipt['id']}:status]"
            old_result = f"[alert:{receipt['id']}:result]"
            old_start = f"[alert:{receipt['id']}:start]"

            async def find():
                messages = [m async for m in channel.history(limit=100) if m.author.id == bot_id and not m.webhook_id]
                for tag in (marker, old_result, old_start):
                    matches = [{"id": str(m.id)} for m in messages if has_marker(m, tag)]
                    if matches:
                        return matches
                return []

            async def create():
                current = self.store.get(source["id"])
                if (
                    not current
                    or current["revision"] != source["revision"]
                    or current["state"] not in {"active", "provisional"}
                ):
                    raise AlertError("Registration changed before posting")
                message = await channel.send(
                    **marked_message(text, marker, channel), allowed_mentions=discord.AllowedMentions.none()
                )
                return {"id": str(message.id)}

            # A completed legacy result is already delivered; never duplicate it.
            legacy = self.store.db.execute(
                "SELECT state,result FROM operations WHERE source_id=? AND revision=? AND step=?",
                (source["id"], source["revision"], "result:" + receipt["id"]),
            ).fetchone()
            if step == "result" and legacy and legacy["state"] == "complete":
                return json.loads(legacy["result"])
            result = await ensure_operation(self.store, source, "status:" + receipt["id"], find, create)
            message = await channel.fetch_message(int(result["id"]))
            if (
                message.author.id != bot_id
                or message.webhook_id
                or not any(has_marker(message, tag) for tag in (marker, old_result, old_start))
            ):
                raise AlertError("Diagnosis status message changed; refusing to edit it")
            # Repeat edits are safe after a lost edit response. Repeat sends are not.
            formatted = marked_message(text, marker, channel)
            if message.content != formatted["content"] or not has_marker(message, marker):
                current = self.store.get(source["id"])
                if (
                    not current
                    or current["revision"] != source["revision"]
                    or current["state"] not in {"active", "provisional"}
                ):
                    raise AlertError("Registration changed before editing")
                await message.edit(
                    **formatted,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            return result

    @staticmethod
    def incident_title(source, receipt):
        config = source["config"]
        label = receipt.get("issue_title")
        if not label:
            if not receipt.get("issue_name"):
                # Before the first issue read, show status without inventing a link label.
                return public_text(config.get("app", "Application"), 80)
            label = incident_label(source, {"name": receipt["issue_name"]})
        title = public_text(label, 80).replace("[", "(").replace("]", ")")
        if config.get("host") and config.get("project"):
            title = f"[{title}]({config['host']}/project/{config['project']}/error_tracking/{receipt['issue_id']})"
        return title

    async def queued(self, source, receipt):
        status = "Queued for diagnosis."
        if receipt.get("evidence", {}).get("status") == "waiting":
            status = "Waiting for stack trace. PostHog has not returned the exception evidence yet."
        elif receipt["available"] > time.time():
            until = time.strftime("%H:%M UTC", time.gmtime(receipt["available"]))
            status = f"Queued until {until}: this app has reached its 12 diagnoses per hour."
        return await self._notice(source, receipt, "queued", self.incident_title(source, receipt) + "\n" + status)

    async def progress(self, source, receipt, stage=0):
        label = "Setup test. " if receipt.get("setup_test") else "Drill. " if receipt.get("drill") else ""
        if not label:
            label = {
                "drill": "Sampled exception: declared drill. ",
                "unmarked": "Sampled exception: no declared drill marker. ",
                "unknown": "Sampled exception: drill status unknown. ",
            }.get(receipt.get("sample_drill_status"), "")
        return await self._notice(
            source,
            receipt,
            "progress",
            self.incident_title(source, receipt)
            + "\n"
            + label
            + (
                "Waiting for stack trace. PostHog has not returned the exception evidence yet."
                if receipt.get("evidence", {}).get("status") == "waiting"
                else "Investigating."
                if not stage
                else "Diagnosis is still running."
            ),
        )

    async def report(self, source, receipt):
        prefix = "Diagnosis held:"
        if receipt["success"]:
            prefix = (
                "Setup check received. Alert path works."
                if receipt.get("setup_test")
                else "Drill received. Alert path works; no fix needed."
                if receipt.get("drill")
                else "Drill sample received. Alert path works; no fix needed for this sample."
                if receipt.get("sample_drill_status") == "drill"
                else "Diagnosis complete. Proposed fix for review:"
            )
        prefix += "\n\n"
        return await self._notice(
            source, receipt, "result", self.incident_title(source, receipt) + "\n" + prefix + receipt["result"]
        )


class DiscordAlerts:
    def __init__(self, connector, config, directory):
        self.connector, self.config = connector, config
        self.store = AlertStore(Path(directory))
        self.adapters = {}
        for name, qualified in config.get("adapters", {}).items():
            module, attribute = qualified.split(":", 1)
            cls = getattr(importlib.import_module(module), attribute)
            self.adapters[name] = cls(connector.vault, self.store)
        self.transport = DiscordAlertTransport(connector, self.store)
        self.worker = AlertWorker(self.store, self.adapters, connector._agent_manager, self.transport, str(directory))
        self.task = None
        self.locks = {}
        self.start_lock = asyncio.Lock()
        self.lifecycle = AlertLifecycle(
            self.store, self.adapters, self.transport, self.worker, self.locks, connector.vault
        )
        self.worker.accounts = set()
        self.lifecycle_task = None
        self.operator = OperatorRehearsal(self, directory)
        self.lifecycle.expire_rehearsals = self.operator.expire
        self.credentials = CredentialEntry(
            directory,
            connector.vault,
            {host for adapter in self.adapters.values() for host in adapter.credential_hosts},
        )

    async def start(self, account):
        async with self.start_lock:
            await self.lifecycle.reconcile(account)
            self.lifecycle.accounts.add(account)
            self.worker.accounts.add(account)
            if self.task is None:
                await self.credentials.start()
                if self.config.get("operator_rehearsal") is True:
                    try:
                        await self.operator.start()
                    except Exception as error:
                        # The optional local socket must not interrupt monitoring.
                        log_failure(error, "operator rehearsal startup")
                self.task = asyncio.create_task(self.worker.run(), name="alert-worker")
                self.lifecycle_task = asyncio.create_task(self.lifecycle.run(), name="alert-lifecycle")

    async def stop(self):
        self.worker.stopped = True
        for task in (self.task, self.lifecycle_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await self.operator.stop()
        await self.credentials.stop()
        self.store.close()

    def admin(self, user_id):
        return str(user_id) in {str(x) for x in self.connector._admin_users}

    def register_commands(self, bot):
        group = app_commands.Group(name="alerts", description="Set up and manage application alert channels")

        @group.command(name="create", description="Set up application monitoring in this DM")
        async def create(interaction: discord.Interaction, service: str = ""):
            selected = service or next(iter(self.adapters), "")
            await self.begin(bot, interaction, selected)

        @group.command(name="status", description="Show alert setup and pending incidents")
        async def status(interaction: discord.Interaction, source_id: str = ""):
            await self.command(bot, interaction, "status", source_id)

        @group.command(name="resume", description="Resume a previously confirmed setup")
        async def resume(interaction: discord.Interaction, source_id: str = ""):
            await self.command(bot, interaction, "resume", source_id)

        @group.command(name="unsubscribe", description="Stop this registration and disable its destination")
        async def unsubscribe(interaction: discord.Interaction, source_id: str = ""):
            await self.command(bot, interaction, "unsubscribe", source_id)

        @group.command(name="rotate", description="Replace an owned alert webhook and destination")
        async def rotate(interaction: discord.Interaction, source_id: str = ""):
            await self.command(bot, interaction, "rotate", source_id)

        bot.tree.add_command(group)
        alias = app_commands.Group(name="alert", description="Application alert setup")

        def register_alias(service):
            @alias.command(name=service, description="Set up application monitoring in this DM")
            async def launch(interaction: discord.Interaction):
                await self.begin(bot, interaction, service)

        for service in self.adapters:
            register_alias(service)

        bot.tree.add_command(alias)

    async def begin(self, bot, interaction, service):
        if interaction.guild or not self.admin(interaction.user.id) or interaction.user.bot:
            await interaction.response.send_message(
                "Start alert setup in a DM as a configured administrator.", ephemeral=True
            )
            return
        owner = bot._resolve_agent(interaction)
        if not owner or service not in self.adapters:
            await interaction.response.send_message("This service adapter or responsible agent is not configured.")
            return
        self.store.expire_drafts(bot.account_name, interaction.user.id, interaction.channel_id)
        source = self.store.begin(owner, interaction.user.id, bot.account_name, interaction.channel_id)
        if not source["waiting"]:
            await interaction.response.send_message(
                "I am still checking the previous setup answer. You can chat normally."
            )
            return
        if not source["config"]:
            source = self.store.update(source["id"], config={"service": service, "message_format": 2})
        await interaction.response.send_message(
            self.question(source, bot), allowed_mentions=discord.AllowedMentions.none()
        )

    def credential_names(self, service):
        listing = getattr(self.connector.vault, "list_keys", None)
        return sorted(
            k
            for k in (listing() if listing else [])
            if isinstance(k, str)
            and service.casefold() in k.casefold()
            and re.fullmatch(r"secrets/[A-Za-z0-9_-]{1,100}", k)
        )

    @staticmethod
    def repository_label(path):
        root = Path(path)
        try:
            label = str(Path("~") / root.relative_to(Path.home()))
        except ValueError:
            label = str(root)
        return public_text(label, 300).replace("`", "'").replace("\n", " ").replace("\r", " ")

    def question(self, source, bot):
        config = dict(source["config"])
        adapter = self.adapters[config["service"]]
        notices = []
        if "project" in config and "api_key" not in config:
            choices = self.credential_names(config["service"])
            config["credential_choices"] = choices
            if len(choices) == 1:
                config["api_key"] = choices[0]
                notices.append(f"Using the existing vault key {choices[0]}.")
            source = self.store.update(source["id"], config=config)
        if "api_key" in config and not source["guild_id"] and bot:
            if len(bot.client.guilds) == 1:
                guild = bot.client.guilds[0]
                source = self.store.update(source["id"], guild_id=str(guild.id))
                notices.append(f"Using server {public_text(guild.name, 100)}.")
        if "app" not in config:
            prompt = (
                "What is the app called? Spaces and capitals are fine; I will format its alert channel name. "
                "Type CANCEL to stop setup."
            )
        elif "repo" not in config:
            prompt = "Send the app's Git repository URL, or its local filesystem path. I will find the clone."
        elif "project" not in config:
            prompt = adapter.project_prompt
        elif "api_key" not in config:
            choices = config.get("credential_choices", [])
            if choices:
                prompt = (
                    "I found these existing vault keys. Choose a number or name (never paste a key value):\n"
                    + "\n".join(f"{i}. {name}" for i, name in enumerate(choices, 1))
                )
            else:
                prompt = (
                    "No matching vault key was found. Send an existing secrets/name reference, "
                    "or enter a new key privately with scripts/alert-credential.py "
                    f"--socket {self.credentials.path} and the project's API host. Never paste the key here."
                )
        elif not source["guild_id"]:
            prompt = "Which server? Choose its number, name or server ID:\n" + "\n".join(
                f"{i}. {public_text(g.name, 100)} ({g.id})" for i, g in enumerate(bot.client.guilds, 1)
            )
        elif "triggers" not in config:
            prompt = (
                "Alert on all issue events: created, reopened and spiking? Reply yes or all (the default), "
                "or choose a comma-separated subset."
            )
        else:
            guild = (
                next((g.name for g in bot.client.guilds if str(g.id) == source["guild_id"]), source["guild_id"])
                if bot
                else source["guild_id"]
            )
            prompt = (
                f"Create alerts-{config['app']} in server {public_text(guild, 100)} "
                f"for {config['service']} project {config['project']}? "
                f"Repository: `{self.repository_label(config['repo'])}`. "
                f"Events: {', '.join(config['triggers'])}. "
                f"Project: {config['host']}/project/{config['project']}. Key: {config['api_key']}. "
                "This creates a private channel, webhook and service destination, then sends a diagnostic test. "
                "Reply CREATE to proceed or CANCEL to stop."
            )
        return "\n".join([*notices, prompt])

    async def answer(self, source, bot, text):
        if text.strip().upper() == "CANCEL":
            self.store.disable(source["id"])
            return "Setup stopped. Existing resources, if any, are retained for review."
        if source["state"] != "draft":
            return self.status(source) + " Use /alerts resume with the setup ID to continue."
        config = dict(source["config"])
        text = text.strip()
        repo_notice = ""
        if "app" not in config:
            config["app"] = channel_app_name(text)
        elif "repo" not in config:
            root = await asyncio.to_thread(resolve_repository, text, self.config.get("repository_roots", []))
            current = self.store.get(source["id"])
            if not current or current["state"] != "draft" or current["config"] != config:
                raise AlertError("Setup changed during repository lookup; continue from its current prompt")
            config["repo"] = str(root)
            repo_notice = f"Found the clone at `{self.repository_label(root)}`.\n\n"
        elif "project" not in config:
            config.update(self.adapters[config["service"]].parse_project(text))
        elif "api_key" not in config:
            choices = config.get("credential_choices", self.credential_names(config["service"]))
            if text.isdigit() and 1 <= int(text) <= len(choices):
                text = choices[int(text) - 1]
            if text.casefold() in {
                "use existing one",
                "use the existing one",
                "look for posthog",
            } or not text.startswith("secrets/"):
                return self.question(source, bot)
            if not re.fullmatch(r"secrets/[A-Za-z0-9_-]{1,100}", text):
                raise AlertError("Choose a vault key by number or name; never paste credential values")
            listing = getattr(self.connector.vault, "list_keys", None)
            if listing and text not in listing():
                raise AlertError("That vault key name was not found. Choose one of the listed names")
            config["api_key"] = text
        elif not source["guild_id"]:
            guilds = bot.client.guilds
            matches = [g for g in guilds if str(g.id) == text or g.name.casefold() == text.casefold()]
            if not matches and text.isdigit() and 1 <= int(text) <= len(guilds):
                matches = [guilds[int(text) - 1]]
            if len(matches) != 1:
                raise AlertError("Choose a listed server by number, name or server ID. A channel ID is not a server ID")
            source = self.store.update(source["id"], guild_id=str(matches[0].id))
        elif "triggers" not in config:
            kinds = (
                list(self.adapters[config["service"]].triggers)
                if text.casefold() in {"", "yes", "all"}
                else list(dict.fromkeys(x.strip().lower() for x in text.split(",")))
            )
            if not kinds or set(kinds) - set(self.adapters[config["service"]].triggers):
                raise AlertError("Choose created, reopened, spiking, or all")
            config["triggers"] = kinds
        elif text.upper() == "CREATE":
            for row in self.store.db.execute(
                "SELECT id FROM sources WHERE guild_id=? AND id!=? AND state!='disabled'",
                (source["guild_id"], source["id"]),
            ):
                other = self.store.get(row["id"])
                if all(other["config"].get(k) == config.get(k) for k in ("service", "host", "project", "repo")):
                    if self.operator.allow_duplicate(source, other):
                        continue
                    raise AlertError("This app already has an alert registration. Use /alerts status or /alerts rotate")
            await self.adapters[config["service"]].check_credentials(config)
            self.operator.check_creation(source)
            source = self.store.update(source["id"], state="provisioning")
            return await self.provision(source)
        else:
            return self.question(source, bot)
        source = self.store.update(source["id"], config=config)
        return repo_notice + self.question(source, bot)

    async def provision(self, source):
        source, webhook = await self.transport.provision(source)
        source = self.store.require_setup(source)
        adapter = self.adapters[source["config"]["service"]]
        destination = await adapter.destination(source, webhook)
        source = self.store.require_setup(source)
        config = {**source["config"], "destination_id": destination["id"]}
        source = self.store.update(source["id"], config=config)
        await adapter.test_delivery(source, destination["id"])
        self.worker.wake.set()
        return (
            f"The channel is ready: https://discord.com/channels/{source['guild_id']}/{source['channel_id']}. "
            "Checking test delivery and diagnosis now. I will confirm here when alerts are active."
        )

    def status(self, source):
        if not source:
            return "Alert setup has been removed."
        job = self.store.db.execute(
            "SELECT state,attempts,error FROM teardowns WHERE source_id=?", (source["id"],)
        ).fetchone()
        waiting = sum(self.store.counts(source["id"]).get(k, 0) for k in ("pending", "running", "ready"))
        cleanup = f" Cleanup {job['state']} after {job['attempts']} attempts." if job else ""
        return (
            f"Alerts for {source['config'].get('app', 'your app')}: {source['state']}. "
            f"{waiting} diagnoses pending." + cleanup
        )

    async def command(self, bot, interaction, action, source_id):
        if interaction.user.bot or not self.admin(interaction.user.id):
            await interaction.response.send_message(
                "This registration is not managed by you through this bot.", ephemeral=True
            )
            return
        candidates = [
            self.store.get(row["id"])
            for row in self.store.db.execute(
                "SELECT id FROM sources WHERE account=? AND user_id=? ORDER BY created DESC",
                (bot.account_name, str(interaction.user.id)),
            )
        ]
        if source_id:
            candidates = [s for s in candidates if source_id in {s["id"], s["channel_id"], s["config"].get("app")}]
        else:
            candidates = [s for s in candidates if s["state"] != "disabled"]
        if len(candidates) > 1:
            choices = "\n".join(
                f"{s['config'].get('app', 'Unnamed setup')}: {s['state']}"
                + (f" <#{s['channel_id']}>" if s["channel_id"] else "")
                for s in candidates
            )
            await interaction.response.send_message(
                "Choose an app name or its alert-channel ID in source_id:\n" + public_text(choices),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        source = candidates[0] if candidates else None
        source_id = source["id"] if source else source_id
        if (
            not source
            or interaction.user.bot
            or not self.admin(interaction.user.id)
            or source["user_id"] != str(interaction.user.id)
            or source["account"] != bot.account_name
        ):
            await interaction.response.send_message(
                "This registration is not managed by you through this bot.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        async with self.locks.setdefault(source_id, asyncio.Lock()):
            try:
                source = self.store.get(source_id)
                adapter = self.adapters[source["config"]["service"]]
                if action == "unsubscribe":
                    self.lifecycle.request(source_id, "unsubscribe")
                elif action == "rotate":
                    if source["state"] != "active":
                        raise AlertError("Only active registrations can rotate; resume uncertain setup first")
                    old = source
                    source = self.store.rotate(source_id)
                    if old["config"].get("destination_id"):
                        await adapter.disable(old, old["config"]["destination_id"])
                    await self.provision(source)
                elif (
                    action == "resume"
                    and self.store.db.execute("SELECT 1 FROM teardowns WHERE source_id=?", (source_id,)).fetchone()
                ):
                    self.store.retry_teardown(source_id)
                    self.lifecycle.wake.set()
                elif action == "resume" and source["state"] == "paused":
                    prior = source["config"].get("paused_from", "provisional")
                    source = self.store.update(
                        source_id, state=prior if prior in {"active", "provisional"} else "provisional"
                    )
                    # Resume restores the previous diagnostic state, never an execution grant.
                    self.worker.wake.set()
                elif action == "resume":
                    if source["state"] not in {"provisioning", "provisional"}:
                        raise AlertError("Only previously confirmed setup can resume")
                    await self.provision(source)
                response = self.status(self.store.get(source_id))
            except Exception as error:
                log_failure(error, action)
                response = public_text(failure_reason(error))
        await interaction.followup.send(response, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def on_message(self, bot, message):
        channel_id = str(message.channel.id)
        source = self.store.channel(channel_id)
        if source:
            if bot.account_name != source["account"]:
                return True
            if not message.author.bot:
                if self.admin(message.author.id):
                    await message.channel.send(self.status(source), allowed_mentions=discord.AllowedMentions.none())
                return True
            if (
                str(getattr(message, "webhook_id", "")) != source["webhook_id"]
                or str(getattr(getattr(message, "guild", None), "id", "")) != source["guild_id"]
                or source["state"] not in {"active", "provisional"}
            ):
                return True
            try:
                adapter = self.adapters[source["config"]["service"]]
                event = adapter.parse_event(source, message.content)
                outcome = self.store.receive(source, message_id=message.id, **event)
                if outcome == "queued":
                    self.worker.wake.set()
                elif outcome == "overflow":
                    await self.transport.say(source, "Alert queue is full. Registration paused; review required.")
            except AlertError:
                pass  # Invalid untrusted messages get no amplification or tools.
            return True
        if message.guild or message.author.bot or not self.admin(message.author.id):
            return False
        self.store.expire_drafts(bot.account_name, message.author.id, channel_id)
        source = self.store.draft(bot.account_name, message.author.id, channel_id)
        if not source:
            return False
        async with self.locks.setdefault(source["id"], asyncio.Lock()):
            current = self.store.get(source["id"])
            if not current or current["state"] != "draft" or not current["waiting"]:
                return False
            self.store.update(source["id"], waiting=0)
            try:
                response = await self.answer(self.store.get(source["id"]), bot, message.content)
            except Exception as error:
                log_failure(error, "setup")
                response = public_text(failure_reason(error))
                current = self.store.get(source["id"])
                if current and current["state"] in {"provisioning", "provisional"}:
                    self.store.notify_setup(current, "Alert setup is held: " + response, "provision-held")
                    self.lifecycle.wake.set()
                    response = None
            finally:
                current = self.store.get(source["id"])
                if current and current["state"] == "draft":
                    self.store.update(source["id"], waiting=1)
        if response is not None:
            await message.channel.send(response, allowed_mentions=discord.AllowedMentions.none())
        return True
