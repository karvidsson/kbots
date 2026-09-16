"""Admin-owned DM setup and private alert rooms. No LLM in the provisioning path."""

import asyncio
import importlib
import json
import re
from pathlib import Path

import discord
from discord import app_commands
from discord.state import ConnectionState

from src.core.alert_channels import AlertError, AlertStore, ensure_operation
from src.core.alert_credentials import CredentialEntry
from src.core.alert_diagnosis import AlertWorker, public_text
from src.core.alert_lifecycle import AlertLifecycle


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


class DiscordAlertTransport:
    def __init__(self, connector, store):
        self.connector, self.store = connector, store

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

    async def guild_status(self, source):
        try:
            async with asyncio.timeout(15):
                client = self.bot(source).client
                guild = await client.fetch_guild(int(source["guild_id"]))
                if str(guild.id) != source["guild_id"]:
                    return "unknown"
                await guild.fetch_member(client.user.id)
            return "present"
        except discord.Forbidden:
            return "access denied"
        except discord.NotFound:
            return "guild unavailable"
        except Exception:
            return "unknown"

    @staticmethod
    def obfuscated(channel):
        flags = getattr(channel, "flags", 0)
        return bool(getattr(flags, "value", flags) & (1 << 17))

    async def channel_status(self, source, event=None):
        client = self.bot(source).client
        state = getattr(client, "_connection", None)
        if source["channel_id"] in getattr(state, "alert_obfuscated", {}):
            return "obfuscated"
        cached = getattr(client, "get_channel", lambda _: None)(int(source["channel_id"]))
        if cached is not None and self.obfuscated(cached):
            return "obfuscated"
        if event is not None and self.obfuscated(event):
            return "obfuscated"
        try:
            async with asyncio.timeout(15):
                channel = await self.channel(source)
            return "obfuscated" if self.obfuscated(channel) else "present"
        except discord.NotFound as error:
            # Other 404 codes can describe lost guild access rather than this channel.
            return "missing" if error.status == 404 and error.code == 10003 else "unknown"
        except discord.Forbidden:
            return "access denied"
        except Exception:
            return "unknown"

    async def lifecycle_notice(self, notice):
        source = json.loads(notice["context"])
        home = await self.connector._agent_manager._resolve_home_channel(source["owner"])
        if (
            not home
            or home[0] != "discord"
            or home[2] not in {None, source["account"]}
            or home[1] == source["channel_id"]
            or self.store.channel(home[1])
        ):
            raise AlertError("Responsible agent home channel is unavailable; lifecycle notice retained")
        client = self.bot(source).client
        async with asyncio.timeout(20):
            channel = await client.fetch_channel(int(home[1]))
            marker = f"[alert-lifecycle:{notice['id']}]"
            matches = [
                str(m.id)
                async for m in channel.history(limit=100)
                if m.author.id == client.user.id and not m.webhook_id and m.content.endswith(marker)
            ]
            if matches:
                message_id = matches[0]
            elif notice["state"] == "sending":
                raise AlertError("Lifecycle notice delivery is uncertain; no duplicate submitted")
            else:
                self.store.db.execute("UPDATE lifecycle_notices SET state='sending' WHERE id=?", (notice["id"],))
                try:
                    message = await channel.send(
                        public_text(notice["text"], 1750) + "\n" + marker,
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
                            view_channel=True, send_messages=True, manage_webhooks=True, read_message_history=True
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
        marker = f"[alert:{receipt['id']}:{step}]"
        channel = await self.channel(source)
        bot_id = self.bot(source).client.user.id

        async def find():
            return [
                {"id": str(m.id)}
                async for m in channel.history(limit=100)
                if m.author.id == bot_id and not m.webhook_id and m.content.endswith(marker)
            ]

        async def create():
            current = self.store.get(source["id"])
            if (
                not current
                or current["revision"] != source["revision"]
                or current["state"] not in {"active", "provisional"}
            ):
                raise AlertError("Registration changed before posting")
            message = await channel.send(
                public_text(text, 1750) + "\n" + marker, allowed_mentions=discord.AllowedMentions.none()
            )
            return {"id": str(message.id)}

        return await ensure_operation(self.store, source, step + ":" + receipt["id"], find, create)

    async def progress(self, source, receipt, stage=0):
        return await self._notice(
            source,
            receipt,
            "start" if not stage else f"progress-{stage}",
            f"Investigating issue {receipt['issue_id']}. "
            + ("Reading incident and source evidence." if not stage else "Diagnosis is still running; no fix applied."),
        )

    async def report(self, source, receipt):
        prefix = "Diagnosis complete. Proposed fix for review:\n" if receipt["success"] else "Diagnosis held:\n"
        return await self._notice(source, receipt, "result", prefix + receipt["result"])


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
        async def status(interaction: discord.Interaction, source_id: str):
            await self.command(bot, interaction, "status", source_id)

        @group.command(name="resume", description="Resume a previously confirmed setup")
        async def resume(interaction: discord.Interaction, source_id: str):
            await self.command(bot, interaction, "resume", source_id)

        @group.command(name="unsubscribe", description="Stop this registration and disable its destination")
        async def unsubscribe(interaction: discord.Interaction, source_id: str):
            await self.command(bot, interaction, "unsubscribe", source_id)

        @group.command(name="rotate", description="Replace an owned alert webhook and destination")
        async def rotate(interaction: discord.Interaction, source_id: str):
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
        source = self.store.begin(owner, interaction.user.id, bot.account_name, interaction.channel_id)
        if not source["config"]:
            source = self.store.update(source["id"], config={"service": service})
        await interaction.response.send_message(
            self.question(source, bot), allowed_mentions=discord.AllowedMentions.none()
        )

    def question(self, source, bot):
        config = source["config"]
        adapter = self.adapters[config["service"]]
        if "app" not in config:
            return "What app should I monitor? Use a short name for its alert channel. Type CANCEL to stop setup."
        if "repo" not in config:
            return "Where is the app's Git repository on this machine?"
        if "project" not in config:
            return adapter.project_prompt
        if "api_key" not in config:
            return (
                "Which existing vault reference holds the service API key? "
                "Send its secrets/name reference only, never the secret value. "
                "For a new key, use scripts/alert-credential.py "
                f"with --socket {self.credentials.path} and the project's API host."
            )
        if not source["guild_id"]:
            choices = ", ".join(f"{g.name} ({g.id})" for g in bot.client.guilds)
            return "Which server should contain the private alert channel? Reply with its ID: " + public_text(choices)
        if "triggers" not in config:
            return "Which issues should trigger alerts? Choose a comma-separated combination: " + ", ".join(
                adapter.triggers
            )
        return (
            f"Create alerts-{config['app']} in server {source['guild_id']} "
            f"for {config['service']} project {config['project']}? "
            f"Events: {', '.join(config['triggers'])}. Repo: {config['repo']}. "
            f"API host: {config['host']}. Key reference: {config['api_key']}. "
            "This creates a private channel, webhook and service destination, then sends a diagnostic test. "
            f"Reply CREATE to proceed or CANCEL to stop. Setup ID: {source['id']}"
        )

    async def answer(self, source, bot, text):
        if text.strip().upper() == "CANCEL":
            self.store.disable(source["id"])
            return "Setup stopped. Existing resources, if any, are retained for review."
        if source["state"] != "draft":
            return self.status(source) + " Use /alerts resume with the setup ID to continue."
        config = dict(source["config"])
        text = text.strip()
        if "app" not in config:
            if not re.fullmatch(r"[a-z][a-z0-9-]{1,40}", text):
                raise AlertError("Use 2 to 41 lowercase letters, digits or hyphens for the app name")
            config["app"] = text
        elif "repo" not in config:
            root = Path(text).expanduser().resolve(strict=True)
            roots = [Path(p).expanduser().resolve() for p in self.config.get("repository_roots", [])]
            if not roots or not any(root.is_relative_to(p) for p in roots) or not (root / ".git").exists():
                raise AlertError("Choose a Git repository inside a configured alerts.repository_roots directory")
            config["repo"] = str(root)
        elif "project" not in config:
            config.update(self.adapters[config["service"]].parse_project(text))
        elif "api_key" not in config:
            if not re.fullmatch(r"secrets/[A-Za-z0-9_-]{1,100}", text):
                raise AlertError("Supply one vault reference; never paste credential values here")
            config["api_key"] = text
        elif not source["guild_id"]:
            if text not in {str(g.id) for g in bot.client.guilds}:
                raise AlertError("Select a server available to this bot")
            source = self.store.update(source["id"], guild_id=text)
        elif "triggers" not in config:
            kinds = list(dict.fromkeys(x.strip() for x in text.split(",")))
            if not kinds or set(kinds) - set(self.adapters[config["service"]].triggers):
                raise AlertError("Choose lifecycle events from the setup prompt")
            config["triggers"] = kinds
        elif text.upper() == "CREATE":
            for row in self.store.db.execute(
                "SELECT id FROM sources WHERE guild_id=? AND id!=? AND state!='disabled'",
                (source["guild_id"], source["id"]),
            ):
                other = self.store.get(row["id"])
                if all(other["config"].get(k) == config.get(k) for k in ("service", "host", "project", "repo")):
                    raise AlertError(f"This application is already registered as {other['id']}; use status or rotate")
            await self.adapters[config["service"]].check_credentials(config)
            source = self.store.update(source["id"], state="provisioning")
            return await self.provision(source)
        else:
            return self.question(source, bot)
        source = self.store.update(source["id"], config=config)
        return self.question(source, bot)

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
            f"Setup {source['id']} is provisional. Waiting for a real test delivery and completed diagnosis "
            f"in <#{source['channel_id']}>. It is not active yet."
        )

    def status(self, source):
        if not source:
            return "Alert setup has been removed."
        job = self.store.db.execute(
            "SELECT state,attempts,error FROM teardowns WHERE source_id=?", (source["id"],)
        ).fetchone()
        cleanup = " Cleanup: " + json.dumps(dict(job)) + "." if job else ""
        return (
            f"Setup {source['id']}: {source['state']}. Receipts: {json.dumps(self.store.counts(source['id']))}."
            + cleanup
        )

    async def command(self, bot, interaction, action, source_id):
        source = self.store.get(source_id)
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
            except AlertError as error:
                response = str(error)
            except Exception:
                response = "Operation failed or has an unknown outcome. Inspect setup status before retrying."
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
                    waiting = self.store.counts(source["id"]).get("pending", 0)
                    await self.transport.say(
                        source,
                        f"Received issue {event['issue_id']}. "
                        f"Queued for read-only diagnosis; {waiting} pending. "
                        "Processing is limited to 12 diagnoses per hour.",
                    )
                    self.worker.wake.set()
                elif outcome == "overflow":
                    await self.transport.say(source, "Alert queue is full. Registration paused; review required.")
            except AlertError:
                pass  # Invalid untrusted messages get no amplification or tools.
            return True
        if message.guild or message.author.bot or not self.admin(message.author.id):
            return False
        source = self.store.draft(bot.account_name, message.author.id, channel_id)
        if not source:
            return False
        async with self.locks.setdefault(source["id"], asyncio.Lock()):
            try:
                response = await self.answer(self.store.get(source["id"]), bot, message.content)
            except AlertError as error:
                response = str(error)
            except Exception:
                response = "Setup could not continue. No successful outcome is assumed; use /alerts status."
        await message.channel.send(response, allowed_mentions=discord.AllowedMentions.none())
        return True
