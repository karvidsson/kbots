"""Discord connector — multi-bot, typing indicators, slash commands, no inline buttons."""

import asyncio
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import discord
from discord import app_commands

from src.core.base import Attachment, Connector, IncomingMessage, VaultBackend

logger = logging.getLogger(__name__)

# Bot-to-bot loop detection settings
_BOT_LOOP_WINDOW = 60       # seconds to track interactions
_BOT_LOOP_MAX_HITS = 5      # max substantive bot-to-bot exchanges in window
_BOT_LOOP_COOLDOWN = 600    # once a loop is detected, hard-mute the pair this long (s)
_BOT_REPEAT_LIMIT = 2       # same/near-empty content this many times → loop
_BOT_CHAIN_LIMIT = 12       # consecutive bot-triggered turns per channel with no human
_BOT_CHAIN_DECAY = 300      # gap (s) between bot turns that resets the chain (self-heal)


def _normalize_bot_content(text: str) -> str:
    """Lowercased content with mentions, punctuation and emoji stripped — used to
    spot repeated/contentless bot acknowledgements (the signature of an ack-storm)."""
    t = re.sub(r"<@[!&]?\d+>", " ", text or "")   # strip user/role mentions
    t = re.sub(r"[^\w\s]", " ", t)                # strip punctuation + emoji
    return re.sub(r"\s+", " ", t).strip().lower()


class DiscordConnector(Connector):
    """Multi-bot Discord connector with slash command support.

    Each bot account runs its own discord.py Client. Slash commands (skills, status,
    admin) are auto-registered per bot. No inline buttons, no modals, no components.
    """
    name = "discord"

    def __init__(self, config: dict, vault: VaultBackend | None = None):
        super().__init__(config, vault)
        self.bots: dict[str, DiscordBot] = {}
        self._admin_users: list[str] = config.get("admin_users", [])
        self._command_sync: str = config.get("command_sync", "auto")
        # Set by the system after init — agent configs and skills for slash commands
        self._agent_configs: dict[str, dict] = {}
        self._skills: dict[str, Any] = {}
        # Duplicate message detection: channel_id -> (content, timestamp)
        self._recent_sends: dict[str, tuple[str, float]] = {}
        # Outgoing mention resolution: (guild_id, name_lower) -> (markup|None, timestamp)
        self._mention_cache: dict[tuple[int, str], tuple[str | None, float]] = {}

    def set_agent_configs(self, agent_configs: dict[str, dict]) -> None:
        """Set agent configs so the connector knows which agents route to which bots."""
        self._agent_configs = agent_configs

    def set_skills(self, skills: dict[str, Any]) -> None:
        """Set available skills for slash command registration."""
        self._skills = skills

    async def start(self) -> None:
        """Start all configured bot accounts."""
        accounts = self.config.get("accounts", {})

        if not accounts:
            # Single-bot mode — use top-level token
            token_key = self.config.get("token_key", "DISCORD_BOT_TOKEN")
            token = self._resolve_token(token_key)
            if not token:
                logger.error("No Discord token found")
                return

            bot = DiscordBot(
                account_name="default",
                connector=self,
                admin_users=self._admin_users,
            )
            self.bots["default"] = bot
            await bot.start(token)
            return

        # Multi-bot mode
        for account_name, account_cfg in accounts.items():
            token_key = account_cfg.get("token_key", f"DISCORD_TOKEN_{account_name.upper()}")
            token = self._resolve_token(token_key)
            if not token:
                logger.error(f"No token for Discord account '{account_name}' (key: {token_key})")
                continue

            intents_list = account_cfg.get("intents", [
                "guilds", "guild_messages", "dm_messages", "message_content", "guild_reactions",
            ])
            max_messages = account_cfg.get("max_messages", 1000)

            bot = DiscordBot(
                account_name=account_name,
                connector=self,
                admin_users=self._admin_users,
                intents_list=intents_list,
                max_messages=max_messages,
            )
            self.bots[account_name] = bot
            await bot.start(token)

        logger.info(f"Discord connector started with {len(self.bots)} bot(s)")

    async def stop(self) -> None:
        """Stop all bot accounts."""
        for name, bot in self.bots.items():
            logger.info(f"Stopping Discord bot: {name}")
            await bot.close()
        self.bots.clear()

    async def send(self, channel_id: str, content: str, **kwargs) -> None:
        """Send a message to a Discord channel.

        kwargs:
            reply_to: discord.Message to reply to
            ephemeral: bool — for interaction responses
            bot_account: str — which bot to send from (default: first available)
        """
        bot_account = kwargs.get("bot_account")
        reply_to = kwargs.get("reply_to")
        ephemeral = kwargs.get("ephemeral", False)

        bot = self._get_bot(bot_account)
        if not bot:
            logger.error(f"No bot available to send to {channel_id}")
            return

        # Handle interaction responses (slash commands)
        if reply_to and isinstance(reply_to, discord.Interaction):
            await self._send_interaction_response(reply_to, content, ephemeral)
            return

        # Regular message send
        channel = bot.client.get_channel(int(channel_id))
        if not channel:
            try:
                channel = await bot.client.fetch_channel(int(channel_id))
            except discord.NotFound:
                logger.error(f"Channel {channel_id} not found")
                return None

        # Check for file attachments in kwargs
        files = kwargs.get("files")  # list of file paths or discord.File objects
        discord_files = None
        if files:
            discord_files = []
            for f in files:
                if isinstance(f, discord.File):
                    discord_files.append(f)
                elif isinstance(f, (str, Path)):
                    fpath = Path(f)
                    if fpath.exists():
                        discord_files.append(discord.File(str(fpath), filename=fpath.name))
                    else:
                        logger.warning(f"File not found for attachment: {fpath}")

        # Linkify plain-text @Name into real mention markup. Agents write
        # natural "@Data.Bot can you…" — as plain text that never pings, and a
        # mentions-only bot never hears it. Resolve names against the guild.
        content = await self._linkify_mentions(content, channel)

        # Auto-mention: when replying to a bot, prepend @mention so they can hear us
        if reply_to and isinstance(reply_to, discord.Message) and reply_to.author.bot:
            mention = f"<@{reply_to.author.id}>"
            if mention not in content:
                content = f"{mention} {content}"

        # Duplicate detection: don't send the same content to the same channel twice in a row
        content_for_dedup = content.strip()
        prev = self._recent_sends.get(channel_id)
        if prev and prev[0] == content_for_dedup and (time.monotonic() - prev[1]) < 120:
            logger.warning(f"Suppressing duplicate message to {channel_id}: {content_for_dedup[:80]!r}")
            return None
        self._recent_sends[channel_id] = (content_for_dedup, time.monotonic())

        # Split long messages (Discord 2000 char limit)
        first_msg = None
        for i, chunk in enumerate(_split_message(content)):
            # Attach files to the first chunk only
            send_files = discord_files if (i == 0 and discord_files) else None
            if reply_to and isinstance(reply_to, discord.Message):
                try:
                    sent = await reply_to.reply(chunk, files=send_files)
                except discord.Forbidden:
                    logger.warning(f"Cannot reply in channel {channel_id} "
                                   f"(missing Read Message History?) — falling back to send")
                    sent = await channel.send(chunk, files=send_files)
                reply_to = None  # Only reply to first chunk
            else:
                sent = await channel.send(chunk, files=send_files)
            if first_msg is None:
                first_msg = sent

        return first_msg

    # Plain-text mention token: one or two words after @ (covers names like
    # "Data.Bot" and "Engineer Bot"). Not preceded by a word char or '<', so
    # emails and existing <@id> markup never match.
    _MENTION_TOKEN = re.compile(r"(?<![\w<])@([\w.\-]+(?: [\w.\-]+)?)")
    _CODE_SPAN = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)
    _MENTION_NEG_TTL = 300.0  # retry failed lookups — members join mid-conversation

    async def _linkify_mentions(self, content: str, channel) -> str:
        """Convert plain-text @Name tokens to real <@id>/<@&id> markup.

        LLM output addresses people as "@Data.Bot" — literal text Discord never
        turns into a ping. Names are resolved against guild roles and members;
        unresolvable tokens are left untouched. Code blocks are skipped.
        """
        guild = getattr(channel, "guild", None)
        if not guild or "@" not in content:
            return content
        out = []
        for part in self._CODE_SPAN.split(content):
            if part.startswith("`"):
                out.append(part)
            else:
                out.append(await self._linkify_segment(part, guild))
        return "".join(out)

    async def _linkify_segment(self, text: str, guild) -> str:
        result: list[str] = []
        last = 0
        for m in self._MENTION_TOKEN.finditer(text):
            token = m.group(1)
            # Try the full (possibly two-word) capture, then the first word
            candidates = [token]
            if " " in token:
                candidates.append(token.split(" ", 1)[0])
            for cand in candidates:
                if cand.lower() in ("everyone", "here"):
                    break
                markup = await self._resolve_mention(guild, cand)
                if markup:
                    result.append(text[last:m.start()])
                    result.append(markup)
                    last = m.start() + 1 + len(cand)  # +1 for the '@'
                    break
        result.append(text[last:])
        return "".join(result)

    @staticmethod
    def _norm_name(s: str) -> str:
        """Alphanumeric-only comparison: 'Data.Bot' == 'Data Bot' == 'Data.Bot'.
        Agents type names from the roster; Discord identities differ in
        punctuation and spacing."""
        return re.sub(r"[^a-z0-9]", "", (s or "").lower())

    @classmethod
    def _member_match(cls, member, norm: str) -> bool:
        return (cls._norm_name(getattr(member, "display_name", "")) == norm
                or cls._norm_name(getattr(member, "name", "")) == norm)

    async def _resolve_mention(self, guild, name: str) -> str | None:
        """Name -> mention markup: members first, roles as fallback.

        Members MUST win over the auto-created managed role that shares every
        bot's name — the bot-to-bot gates key on user mentions, and a role
        mention is only as good as the receiver's role detection.
        """
        norm = self._norm_name(name)
        if not norm:
            return None
        key = (guild.id, norm)
        cached = self._mention_cache.get(key)
        if cached:
            markup, ts = cached
            if markup is not None or (time.monotonic() - ts) < self._MENTION_NEG_TTL:
                return markup
        markup = None
        for member in getattr(guild, "members", []):
            if self._member_match(member, norm):
                markup = f"<@{member.id}>"
                break
        if markup is None and hasattr(guild, "search_members"):
            # HTTP prefix search — works without the privileged members intent,
            # which this client does not request (member cache is disabled).
            # The search is a literal prefix match, so "Data.Bot" never finds
            # "Data Bot" — retry with the leading alphanumeric run.
            queries = [name]
            lead = re.match(r"[A-Za-z0-9]+", name)
            if lead and lead.group(0).lower() != name.lower():
                queries.append(lead.group(0))
            try:
                for query in queries:
                    for member in await guild.search_members(query, limit=25):
                        if self._member_match(member, norm):
                            markup = f"<@{member.id}>"
                            break
                    if markup:
                        break
            except Exception as e:
                logger.debug(f"Member search for {name!r} failed: {e}")
                return None  # transient failure — don't negative-cache
        if markup is None:
            for role in getattr(guild, "roles", []):
                if self._norm_name(role.name) == norm:
                    markup = f"<@&{role.id}>"
                    break
        if len(self._mention_cache) > 512:
            self._mention_cache.clear()
        self._mention_cache[key] = (markup, time.monotonic())
        return markup

    async def _send_interaction_response(
        self, interaction: discord.Interaction, content: str, ephemeral: bool
    ) -> None:
        """Send response to a slash command interaction."""
        chunks = _split_message(content)

        if interaction.response.is_done():
            # Already responded, use followup
            for chunk in chunks:
                await interaction.followup.send(chunk, ephemeral=ephemeral)
        else:
            # First response
            await interaction.response.send_message(chunks[0], ephemeral=ephemeral)
            for chunk in chunks[1:]:
                await interaction.followup.send(chunk, ephemeral=ephemeral)

    @asynccontextmanager
    async def typing(self, channel_id: str, **kwargs):
        """Context manager: typing indicator + 'working' presence while busy."""
        bot_account = kwargs.get("bot_account")
        channel = None
        bot = None

        # Prefer the specific bot account if given
        if bot_account and bot_account in self.bots:
            bot = self.bots[bot_account]
            channel = bot.client.get_channel(int(channel_id))
            if not channel:
                try:
                    channel = await bot.client.fetch_channel(int(channel_id))
                except discord.NotFound:
                    pass

        # Fallback — find any bot that has access
        if not channel:
            for b in self.bots.values():
                channel = b.client.get_channel(int(channel_id))
                if channel:
                    bot = b
                    break

        if not channel:
            for b in self.bots.values():
                try:
                    channel = await b.client.fetch_channel(int(channel_id))
                    bot = b
                    break
                except discord.NotFound:
                    continue

        # Set the bot's status to "working" (red dot) for the duration, with a
        # short hint of the task. DMs stay generic so private content never
        # leaks into the (globally visible) status.
        if bot:
            is_dm = isinstance(channel, discord.DMChannel)
            hint = "" if is_dm else _short_task_hint(kwargs.get("task_detail", ""))
            await bot.task_started(hint)
        try:
            if channel:
                async with channel.typing():
                    yield
            else:
                yield
        finally:
            if bot:
                await bot.task_finished()

    async def post_progress(self, channel_id: str, text: str,
                            bot_account: str | None = None):
        """Post an in-channel progress message; returns the discord Message."""
        bot = self._get_bot(bot_account)
        if not bot:
            return None
        channel = bot.client.get_channel(int(channel_id))
        if not channel:
            try:
                channel = await bot.client.fetch_channel(int(channel_id))
            except Exception:
                return None
        try:
            return await channel.send(text)
        except Exception:
            return None

    async def edit_progress(self, progress_msg, text: str) -> None:
        try:
            await progress_msg.edit(content=text)
        except Exception:
            pass

    async def delete_progress(self, progress_msg) -> None:
        try:
            await progress_msg.delete()
        except Exception:
            pass

    async def update_task_status(self, bot_account: str | None, detail: str) -> None:
        """Update the live 'working' status with the current step (throttled)."""
        bot = self._get_bot(bot_account)
        if bot:
            await bot.update_task_detail(detail)

    def _get_bot(self, account_name: str | None = None) -> "DiscordBot | None":
        """Get a bot by account name, or the first available."""
        if account_name and account_name in self.bots:
            return self.bots[account_name]
        if self.bots:
            return next(iter(self.bots.values()))
        return None

    def _resolve_token(self, key: str) -> str | None:
        """Resolve a token from vault or environment."""
        if self.vault:
            token = self.vault.get(key)
            if token:
                return token

        # Fallback to environment (for development)
        import os
        return os.environ.get(key)

    def _find_bot_for_agent(self, agent_id: str) -> str | None:
        """Find which bot account an agent routes through."""
        agent_cfg = self._agent_configs.get(agent_id, {})
        routing = agent_cfg.get("routing", {}).get("discord", {})
        return routing.get("account")

    def get_agent_for_channel(self, channel_id: str, bot_account: str,
                              category_id: str | None = None) -> str | None:
        """Find which agent handles messages in this channel from this bot.

        Priority: specific channel > category > wildcard (empty channels list).
        Each bot account is an independent Discord client, so routing is
        scoped to the bot_account — other bots' claims are irrelevant.
        """
        wildcard_agent = None
        category_agent = None
        dm_fallback_agent = None

        for agent_id, agent_cfg in self._agent_configs.items():
            routing = agent_cfg.get("routing", {}).get("discord", {})
            account = routing.get("account", "default")
            if account != bot_account:
                continue
            channels = routing.get("channels", [])
            categories = routing.get("categories", [])
            if channels and channel_id in channels:
                return agent_id  # Specific channel match — immediate return
            if category_id and categories and category_id in categories and category_agent is None:
                category_agent = agent_id
            if not channels and not categories and wildcard_agent is None:
                wildcard_agent = agent_id
            # DMs have no category — any agent bound to this bot can handle them
            if category_id is None and dm_fallback_agent is None:
                dm_fallback_agent = agent_id

        return category_agent or wildcard_agent or dm_fallback_agent


class DiscordBot:
    """A single Discord bot instance with slash commands."""

    def __init__(
        self,
        account_name: str,
        connector: DiscordConnector,
        admin_users: list[str],
        intents_list: list[str] | None = None,
        max_messages: int = 1000,
    ):
        self.account_name = account_name
        self.connector = connector
        self.admin_users = admin_users

        # Bot-to-bot loop detection: bot_user_id -> [timestamps]
        self._bot_loop_hits: dict[int, list[float]] = {}
        # bot_user_id -> monotonic time until which the pair is hard-muted
        self._bot_cooldown: dict[int, float] = {}
        # bot_user_id -> recent normalized message contents (for repeat detection)
        self._bot_recent_content: dict[int, list[str]] = {}
        # channel_id -> (consecutive bot-triggered turns, last bot-turn monotonic ts)
        self._bot_chain: dict[int, tuple[int, float]] = {}

        # Message dedup: ignore replayed messages after Discord RESUME
        self._seen_message_ids: set[int] = set()
        self._seen_message_cap = 500  # max IDs to track

        # Presence: reference-count concurrent tasks so the bot shows a
        # "working" status (red dot) while busy and reverts to online when idle.
        self._active_tasks = 0
        self._presence_enabled = connector.config.get("presence", True)
        self._task_detail = ""
        self._task_started_at = 0.0
        self._last_presence_at = 0.0
        self._heartbeat_task: "asyncio.Task | None" = None
        # Set when a presence update could not be sent (gateway not ready).
        # Presence is the one bit of state Discord holds for us rather than the
        # other way round, so a dropped update is not self-correcting: it stays
        # wrong until something else writes. Reconciled on reconnect.
        self._presence_dirty = False

        # Build intents
        intents = discord.Intents.none()
        for intent_name in (intents_list or ["guilds", "guild_messages", "dm_messages", "message_content"]):
            setattr(intents, intent_name, True)

        self.client = discord.Client(
            intents=intents,
            max_messages=max_messages,
            member_cache_flags=discord.MemberCacheFlags.none(),
        )
        self.tree = app_commands.CommandTree(self.client)

        # Register event handlers
        self.client.event(self.on_ready)
        self.client.event(self.on_resumed)
        self.client.event(self.on_message)
        self.client.event(self.on_raw_reaction_add)

    async def start(self, token: str) -> None:
        """Start the bot (non-blocking — runs in background task)."""
        self._register_commands()

        import asyncio
        asyncio.create_task(self.client.start(token), name=f"discord-{self.account_name}")
        logger.info(f"Discord bot '{self.account_name}' starting...")

    async def on_resumed(self) -> None:
        """Gateway RESUMEd — re-assert presence if it drifted.

        A RESUME does not fire on_ready, so nothing else re-states our status.
        Any update dropped while the socket was down is still unsent, and
        Discord may have kept whatever we last set, so reconcile here.
        """
        if self._presence_dirty:
            logger.info(f"[{self.account_name}] gateway resumed — "
                        f"re-applying presence (was stale)")
            await self._apply_presence()

    async def close(self) -> None:
        """Close the bot connection, leaving an honest status behind.

        Clearing presence first is the point. Restarting while busy is the
        normal case, not the exception — self-deploy restarts the very service
        the agent runs in, and the drain is capped, so an in-flight turn is
        killed by design. Without this the last '🛠 <task>' stays on the
        account, advertising work that died with the process: a status showing
        a task that finished twenty minutes ago is worse than no status.

        Best-effort and time-boxed — a slow gateway must not hold up shutdown,
        because whatever is stopping us will not wait for long either.
        """
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        self._active_tasks = 0
        self._task_detail = ""
        try:
            await asyncio.wait_for(self._apply_presence(), timeout=5)
        except Exception as e:
            logger.debug(f"[{self.account_name}] could not clear presence "
                         f"on shutdown: {e}")
        await self.client.close()

    async def on_ready(self) -> None:
        """Called when bot connects to Discord."""
        logger.info(
            f"Discord bot '{self.account_name}' ready as {self.client.user} "
            f"(guilds: {len(self.client.guilds)})"
        )

        # Record this bot's Discord identity in the roster so other agents recognize
        # it as a teammate (resolve_discord_user / user-context) rather than an
        # unknown guest — the bots know their own id only once connected.
        try:
            from src.tools.team import record_bot_identity
            if self.client.user:
                record_bot_identity(self.client.user.name, str(self.client.user.id))
        except Exception as e:
            logger.debug(f"roster identity sync failed: {e}")

        # Clear any stale guild-specific commands, then sync global only.
        # Global commands work everywhere (servers + DMs). Guild commands
        # would duplicate them, so we remove any that exist from prior runs.
        for guild in self.client.guilds:
            try:
                guild_obj = discord.Object(id=guild.id)
                self.tree.clear_commands(guild=guild_obj)
                await self.tree.sync(guild=guild_obj)
                logger.info(f"[{self.account_name}] Cleared guild commands for {guild.name!r} ({guild.id})")
            except Exception as e:
                logger.error(f"[{self.account_name}] Failed to clear guild commands for {guild.id}: {e}")

        try:
            synced = await self.tree.sync()
            logger.info(f"[{self.account_name}] Synced {len(synced)} global commands (servers + DMs)")
        except Exception as e:
            logger.error(f"[{self.account_name}] Failed to sync global commands: {e}")

        # Start in the idle/online state.
        await self._apply_presence()

    async def _apply_presence(self) -> None:
        """Reflect busy/idle in the bot's Discord presence (best-effort).

        While busy, shows '🛠 <task> · m:ss' so a user can follow a long task
        by the running clock. A heartbeat refreshes the elapsed time.
        """
        if not self._presence_enabled:
            return
        if not self.client.is_ready():
            # Remember that the shown status no longer matches reality. Without
            # this a task_finished() landing during a gateway blip leaves us
            # advertising work that has already ended, until the next task
            # happens to overwrite it.
            self._presence_dirty = True
            return
        try:
            if self._active_tasks > 0:
                base = self._task_detail or "Working…"
                elapsed = int(time.monotonic() - self._task_started_at)
                if elapsed >= 20:  # only show a clock once it's been a while
                    base = f"{base} · {elapsed // 60}:{elapsed % 60:02d}"
                await self.client.change_presence(
                    status=discord.Status.dnd,
                    activity=discord.CustomActivity(name=f"🛠 {base}"[:128]),
                )
            else:
                await self.client.change_presence(
                    status=discord.Status.online, activity=None
                )
            self._last_presence_at = time.monotonic()
            self._presence_dirty = False
        except Exception as e:
            self._presence_dirty = True
            logger.debug(f"[{self.account_name}] presence update failed: {e}")

    async def _presence_heartbeat(self) -> None:
        """Refresh the elapsed-time clock in the status every 20s while busy."""
        try:
            while self._active_tasks > 0:
                await asyncio.sleep(20)
                if self._active_tasks > 0:
                    await self._apply_presence()
        except asyncio.CancelledError:
            pass

    async def task_started(self, detail: str = "") -> None:
        self._active_tasks += 1
        if self._active_tasks == 1:  # 0 → busy
            self._task_detail = detail
            self._task_started_at = time.monotonic()
            await self._apply_presence()
            if self._presence_enabled:
                self._heartbeat_task = asyncio.create_task(
                    self._presence_heartbeat(), name=f"presence-{self.account_name}")

    async def update_task_detail(self, detail: str) -> None:
        """Change the shown task text mid-task, throttled to Discord's presence
        rate limit (~5/20s). The heartbeat still refreshes the elapsed clock."""
        if not self._presence_enabled or self._active_tasks <= 0 or not detail:
            return
        if detail == self._task_detail:
            return
        self._task_detail = detail
        if time.monotonic() - self._last_presence_at >= 5:
            await self._apply_presence()

    async def task_finished(self) -> None:
        self._active_tasks = max(0, self._active_tasks - 1)
        if self._active_tasks == 0:  # busy → idle
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
                self._heartbeat_task = None
            self._task_detail = ""
            await self._apply_presence()

    def _bot_loop_check(self, bot_id: int, content: str, now: float) -> tuple[bool, str | None]:
        """Decide whether a bot-to-bot message is a loop and update state.

        Returns (suppress, reason). `reason` is set only when THIS call newly
        trips the guard (so the caller logs once). A trip hard-mutes the pair for
        _BOT_LOOP_COOLDOWN, so a naturally-paced ack-storm can't oscillate at the
        window edge. Two signatures: repeated/contentless acks, or too many
        exchanges within the sliding window.
        """
        if now < self._bot_cooldown.get(bot_id, 0.0):
            return True, None  # already muted from an earlier trip

        norm = _normalize_bot_content(content)
        recent = self._bot_recent_content.setdefault(bot_id, [])
        # A single empty/mention-only message is not a loop — only repeated
        # identical (or repeatedly empty) content is. This lets a one-off short
        # reply like "@AgentB 👍 on it" through while still catching ack-storms.
        repetitive = recent.count(norm) >= _BOT_REPEAT_LIMIT
        recent.append(norm)
        del recent[:-6]  # keep only the last 6

        hits = [t for t in self._bot_loop_hits.get(bot_id, []) if now - t < _BOT_LOOP_WINDOW]
        hits.append(now)
        self._bot_loop_hits[bot_id] = hits

        if repetitive or len(hits) > _BOT_LOOP_MAX_HITS:
            self._bot_cooldown[bot_id] = now + _BOT_LOOP_COOLDOWN
            self._bot_loop_hits[bot_id] = []
            return True, ("repeated/empty acknowledgements" if repetitive
                          else f"{len(hits)} exchanges in {_BOT_LOOP_WINDOW}s")
        return False, None

    def _bot_chain_check(self, channel_id: int, from_bot: bool, now: float) -> bool:
        """Backstop for LLM-paced loops the rate/repeat guard can't see: turns
        arrive ~1/min (under the sliding window) and each restatement is
        paraphrased (never an exact repeat). Count consecutive bot-triggered
        turns per channel; past the limit, stay quiet until a human posts —
        any human message resets the chain.

        Self-heals without a human: a genuine ack-storm keeps turns close
        together, but a real collaboration has gaps (an agent goes off to do
        work and returns minutes later). If more than the decay window has
        passed since the last bot turn, the chain resets. So a fast loop still
        trips at the limit while a paused-then-resumed exchange recovers on its
        own.

        Returns True when the message should be suppressed.
        """
        if not from_bot:
            self._bot_chain.pop(channel_id, None)
            return False
        decay = float(self.connector.config.get("bot_chain_decay_seconds", _BOT_CHAIN_DECAY))
        prev_count, last_ts = self._bot_chain.get(channel_id, (0, now))
        if now - last_ts > decay:
            prev_count = 0  # quiet gap → treat as a fresh exchange
        chain = prev_count + 1
        self._bot_chain[channel_id] = (chain, now)
        limit = int(self.connector.config.get("bot_chain_limit", _BOT_CHAIN_LIMIT))
        if chain > limit:
            if chain == limit + 1:  # log once per trip, not per suppressed message
                logger.warning(
                    f"[{self.account_name}] {limit} consecutive bot-to-bot turns in "
                    f"channel {channel_id} with no human message — going quiet until "
                    f"a human posts"
                )
            return True
        return False

    def _render_mentions(self, message: discord.Message) -> str:
        """Replace raw mention markup with readable @names.

        Discord delivers mentions as <@id>/<@!id>/<@&id>/<#id>; the numeric
        IDs are opaque to the LLM. Resolve every mention this message carries
        — including the bot's own, so multi-party sentences stay grammatical.
        Mentions of users outside the resolved lists stay as raw markup.
        """
        content = message.content
        for user in message.mentions:
            name = getattr(user, "display_name", None) or user.name
            for pattern in (f"<@{user.id}>", f"<@!{user.id}>"):
                content = content.replace(pattern, f"@{name}")
        for role in message.role_mentions:
            content = content.replace(f"<@&{role.id}>", f"@{role.name}")
        for channel in message.channel_mentions:
            content = content.replace(f"<#{channel.id}>", f"#{channel.name}")
        return content.strip()

    async def on_message(self, message: discord.Message) -> None:
        """Handle incoming Discord messages."""
        # Ignore own messages
        if message.author == self.client.user:
            return

        # Dedup: skip messages already seen (replayed after Discord RESUME)
        if message.id in self._seen_message_ids:
            return
        self._seen_message_ids.add(message.id)
        if len(self._seen_message_ids) > self._seen_message_cap:
            # Evict oldest ~half to keep memory bounded
            to_remove = sorted(self._seen_message_ids)[:self._seen_message_cap // 2]
            self._seen_message_ids -= set(to_remove)

        # Check if this bot was mentioned (or it's a DM).
        # Covers both @user mentions and @role mentions for the bot's managed
        # role — computed up front so the bot-author gate honors role mentions too
        # (a bot addressing another only via its managed role must not be dropped).
        now = time.monotonic()
        is_dm = isinstance(message.channel, discord.DMChannel)
        is_mentioned = self.client.user in message.mentions if self.client.user else False
        if not is_mentioned and self.client.user:
            # Check role mentions — Discord auto-creates a managed role for
            # bots, and its tags carry the owning bot's id. This works without
            # the member cache (disabled on this client), where the
            # get_member() fallback below silently returns None.
            for role in message.role_mentions:
                tags = getattr(role, "tags", None)
                if tags and getattr(tags, "bot_id", None) == self.client.user.id:
                    is_mentioned = True
                    break
        if not is_mentioned and self.client.user:
            # Fallback for regular (non-managed) roles assigned to the bot —
            # only effective when the member cache holds our own entry
            bot_role_ids = {r.id for r in message.role_mentions}
            if bot_role_ids and message.guild:
                member = message.guild.get_member(self.client.user.id)
                if member:
                    for role in member.roles:
                        if role.id in bot_role_ids:
                            is_mentioned = True
                            break

        # Get routing config for this bot's agents (needed before the
        # bot-author gate — a watched channel admits unmentioned bot messages)
        has_category = hasattr(message.channel, 'category_id') and message.channel.category_id
        category_id = str(message.channel.category_id) if has_category else None
        agent_id = self.connector.get_agent_for_channel(
            str(message.channel.id), self.account_name, category_id
        )

        if not agent_id:
            return

        agent_cfg = self.connector._agent_configs.get(agent_id, {})
        routing = agent_cfg.get("routing", {}).get("discord", {})
        mentions_only = routing.get("mentions", False)
        # Watched channels: the agent hears every message here — human or bot,
        # mentioned or not — so channel activity itself can drive its work.
        watch_channels = {str(c) for c in (routing.get("watch_channels") or [])}
        is_watched = str(message.channel.id) in watch_channels

        # Ignore bot messages unless this bot was @mentioned (user or role)
        # or the channel is explicitly watched
        if message.author.bot:
            if not (is_mentioned or is_watched):
                return

            # Chain breaker — too many bot-triggered turns with no human around.
            if self._bot_chain_check(message.channel.id, from_bot=True, now=now):
                return

            # Loop detection — either signature hard-mutes the pair (see method).
            suppress, reason = self._bot_loop_check(
                message.author.id, message.content, now)
            if suppress:
                if reason:  # set only when this message newly trips the guard
                    logger.warning(
                        f"[{self.account_name}] Bot-to-bot loop with {message.author} "
                        f"({reason}) — muting this pair for {_BOT_LOOP_COOLDOWN}s"
                    )
                return
            logger.info(
                f"[{self.account_name}] Bot-to-bot message from {message.author} — processing"
            )
        else:
            # A human spoke — the bot-to-bot chain in this channel is over.
            self._bot_chain_check(message.channel.id, from_bot=False, now=now)

        # Check mentions-only routing (watched channels are exempt)
        if mentions_only and not is_mentioned and not is_dm and not is_watched:
            logger.debug(f"[{self.account_name}] Skipping — mentions_only and not mentioned")
            return

        # Render mention markup as readable @names. Raw <@123…> IDs mean
        # nothing to the LLM — in a multi-party message ("@A and @B are you
        # both here?") stripping our own mention and leaving others as
        # numeric IDs makes the agent guess who is being addressed.
        content = self._render_mentions(message)

        # Build attachments
        attachments = [
            Attachment(
                filename=a.filename,
                url=a.url,
                content_type=a.content_type,
                size=a.size,
            )
            for a in message.attachments
        ]

        # Emit normalized message
        await self.connector.emit(IncomingMessage(
            connector="discord",
            channel_id=str(message.channel.id),
            channel_name=getattr(message.channel, "name", None),
            user_id=str(message.author.id),
            user_name=message.author.display_name,
            content=content,
            attachments=attachments,
            reply_to=str(message.reference.message_id) if message.reference else None,
            raw=message,
            bot_account=self.account_name,
            watched=is_watched and not is_mentioned,
        ))

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """Handle reactions: HITL approvals (✅/❌) and lesson feedback (👍/👎)."""
        # Ignore own reactions
        if payload.user_id == self.client.user.id:
            return

        emoji = str(payload.emoji)

        # Lesson feedback: 👍/👎 on a reply nudges the lessons it used.
        if emoji in ("👍", "👎"):
            self._record_training_reward(str(payload.message_id), emoji, str(payload.user_id))
            await self._handle_lesson_feedback(str(payload.message_id), emoji)
            return

        # Schedule oversight: ❌ on a schedule card in the schedules channel
        # cancels that task. Scoped to the schedules channel so it never
        # collides with HITL's ❌ elsewhere.
        from src.core import runtime_state
        sched_channel = runtime_state.get_flag("schedules_channel", None)
        if sched_channel is None:
            sched_channel = getattr(self.connector, "_schedules_channel", "")
        if emoji == "❌" and sched_channel and str(payload.channel_id) == sched_channel:
            await self._handle_schedule_cancel(payload)
            return

        # Only process HITL reactions (checkmark or X)
        if emoji not in ("✅", "❌"):
            return

        # Check if there's a HITL gate with pending requests for this message
        hitl = getattr(self.connector, '_hitl', None)
        if not hitl:
            return

        message_id = str(payload.message_id)
        user_id = str(payload.user_id)

        # Look up pending request by message_id
        try:
            async with hitl.db.execute(
                "SELECT hitl_id FROM hitl_pending WHERE message_id = ? AND status = 'pending'",
                (message_id,)
            ) as cursor:
                row = await cursor.fetchone()

            if not row:
                return

            hitl_id = row[0]

            if emoji == "✅":
                ok = await hitl.approve(hitl_id, user_id)
                if ok:
                    logger.info(f"HITL {hitl_id} approved by {user_id}")
            elif emoji == "❌":
                ok = await hitl.deny(hitl_id, user_id)
                if ok:
                    logger.info(f"HITL {hitl_id} denied by {user_id}")
        except Exception as e:
            logger.error(f"HITL reaction handling failed: {e}", exc_info=True)

    async def _handle_schedule_cancel(self, payload: discord.RawReactionActionEvent) -> None:
        """❌ on a schedule card → cancel that schedule. Parses the `sN` id from
        the card text; posts a confirmation in the channel."""
        import re

        from src.core import schedules as sched
        if not self._is_admin(payload.user_id):
            return  # only admins may cancel schedules (parity with HITL approvers)
        try:
            channel = (self.client.get_channel(payload.channel_id)
                       or await self.client.fetch_channel(payload.channel_id))
            message = await channel.fetch_message(payload.message_id)
        except Exception as e:
            logger.error(f"schedule cancel: could not fetch message: {e}")
            return
        # Only the ⏰ card cancels — not the ▶️ ran / 🛑 finished / ❌ Cancelled lines.
        if not (message.content or "").startswith("⏰"):
            return
        m = re.search(r"`(s\d+)`", message.content or "")
        if not m:
            return
        sid = m.group(1)
        try:
            if sched.delete_schedule(sid):
                await channel.send(f"❌ Cancelled `{sid}` (by <@{payload.user_id}>).")
                logger.info(f"Schedule {sid} cancelled via ❌ by {payload.user_id}")
            else:
                await channel.send(f"`{sid}` was already gone.")
        except Exception as e:
            logger.error(f"schedule cancel failed for {sid}: {e}", exc_info=True)

    def _record_training_reward(self, message_id: str, emoji: str, user_id: str) -> None:
        """Log a 👍/👎 on a reply as a training reward (keyed by reply_message_id).

        Fires for ANY reply (not just ones that used lessons); the exporter joins it
        to the turn by reply_message_id. Best-effort, never raises.
        """
        try:
            mgr = getattr(self.connector, "_agent_manager", None)
            tc = getattr(mgr, "training_collector", None) if mgr else None
            if not tc:
                return
            agent = None
            try:
                from src.core import feedback_map
                entry = feedback_map.get(message_id)
                agent = entry.get("agent_id") if entry else None
            except Exception:
                pass
            tc.record_reward(message_id, agent, "up" if emoji == "👍" else "down", user_id)
        except Exception as e:
            logger.debug(f"training reward record failed: {e}")

    async def _handle_lesson_feedback(self, message_id: str, emoji: str) -> None:
        """👍/👎 on a reply → nudge the confidence of the lessons that reply used."""
        try:
            from src.core import feedback_map
            entry = feedback_map.get(message_id)
            if not entry:
                return
            mgr = getattr(self.connector, "_agent_manager", None)
            if not mgr:
                return
            memory = mgr._get_agent_memory(entry.get("agent_id"))
            if not memory:
                return
            delta = 0.1 if emoji == "👍" else -0.2
            n = 0
            for mid in entry.get("lessons", []):
                m = await memory.get(mid)
                if not m:
                    continue
                new = max(0.0, min(0.99, round(float(m.get("confidence", 0.7) or 0.7) + delta, 2)))
                await memory.update(mid, confidence=new)
                n += 1
            logger.info(f"Lesson feedback {emoji} on reply {message_id}: adjusted {n} lesson(s)")
        except Exception as e:
            logger.error(f"Lesson feedback handling failed: {e}", exc_info=True)

    def _register_commands(self) -> None:
        """Register all slash commands on this bot's command tree."""
        self._register_status_commands()
        self._register_admin_commands()
        self._register_skill_commands()

    # --- Helpers ---

    def _resolve_agent(self, interaction: discord.Interaction) -> str | None:
        """Resolve agent_id from a slash command interaction (channel + category)."""
        channel = interaction.channel
        category_id = (
            str(channel.category_id)
            if hasattr(channel, "category_id") and channel.category_id
            else None
        )
        return self.connector.get_agent_for_channel(
            str(interaction.channel_id), self.account_name, category_id
        )

    # --- Status commands ---

    def _register_status_commands(self) -> None:
        """Register built-in status/info commands."""

        @self.tree.command(name="status", description="Show agent status and info")
        async def cmd_status(interaction: discord.Interaction):
            agent_id = self._resolve_agent(interaction)
            if not agent_id:
                await interaction.response.send_message(
                    "No agent configured for this channel.", ephemeral=True
                )
                return

            # Defer since we might need to gather info
            # Access through connector's reference (set during startup)
            if hasattr(self.connector, '_agent_manager') and self.connector._agent_manager:
                status = await self.connector._agent_manager.get_agent_status(agent_id)
                lines = [
                    f"**{status['display_name']}** (`{agent_id}`)",
                    f"LLM: `{status['llm_provider']}` / `{status['llm_model']}`",
                    f"Active sessions: {status['active_sessions']}",
                    f"Messages handled: {status['total_messages']}",
                    f"Tools: {', '.join(status['tools']) or 'none'}",
                ]
                await interaction.response.send_message("\n".join(lines), ephemeral=True)
            else:
                await interaction.response.send_message("Agent manager not available.", ephemeral=True)

        @self.tree.command(name="schedules", description="List scheduled tasks across agents, or cancel one")
        @app_commands.describe(cancel="Schedule id to cancel (e.g. s3); omit to list all")
        async def cmd_schedules(interaction: discord.Interaction, cancel: str | None = None):
            from src.core import schedules as sched
            from src.core.scheduler import _timing
            if cancel:
                if not self._is_admin(interaction.user.id):
                    await interaction.response.send_message(
                        "Not authorized to cancel schedules.", ephemeral=True)
                    return
                ok = sched.delete_schedule(cancel.strip())
                await interaction.response.send_message(
                    (f"❌ Cancelled `{cancel.strip()}`." if ok else f"No schedule `{cancel.strip()}`."),
                    ephemeral=True)
                return
            items = [s for s in sched.list_schedules() if s.get("enabled")]
            if not items:
                await interaction.response.send_message("No active scheduled tasks.", ephemeral=True)
                return
            lines = ["**Scheduled tasks:**"]
            for s in items:
                lines.append(f"`{s['id']}` · `{s.get('agent_id')}` · {_timing(s)} — "
                             f"{(s.get('instruction') or '')[:80]}")
            lines.append("\nCancel with `/schedules cancel:<id>` or ❌ on its card.")
            await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)

        @self.tree.command(name="help", description="List available skills and commands")
        async def cmd_help(interaction: discord.Interaction):
            # Built from the live command tree so it can never go stale —
            # skill commands are listed separately below with their params.
            skill_cmd_names = {
                s.replace("_", "-")[:32]
                for s in self.connector._skills if ":" not in s
            }
            lines = ["**Available Commands:**", ""]
            for cmd in sorted(self.tree.get_commands(), key=lambda c: c.name):
                if cmd.name in skill_cmd_names:
                    continue
                if isinstance(cmd, app_commands.Group):
                    subs = "|".join(sorted(sub.name for sub in cmd.commands))
                    lines.append(f"`/{cmd.name} {subs}` — {cmd.description}")
                else:
                    lines.append(f"`/{cmd.name}` — {cmd.description}")
            lines.append("")

            skills = self.connector._skills
            if skills:
                lines.append("**Skills:**")
                for skill_name, skill in skills.items():
                    if ":" in skill_name:
                        continue  # Skip agent-prefixed skills in global help
                    params = ""
                    if skill.parameters:
                        param_strs = [
                            f"`{p.name}`{'*' if p.required else ''}"
                            for p in skill.parameters
                        ]
                        params = f" ({', '.join(param_strs)})"
                    lines.append(f"  `/{skill_name.replace('_', '-')}`{params} — {skill.description}")
            else:
                lines.append("*No skills configured.*")

            # Discord caps a message at 2000 chars — overflow goes to followups.
            chunks, chunk = [], ""
            for line in lines:
                if len(chunk) + len(line) + 1 > 1900:
                    chunks.append(chunk)
                    chunk = ""
                chunk += line + "\n"
            chunks.append(chunk)
            await interaction.response.send_message(chunks[0], ephemeral=True)
            for extra in chunks[1:]:
                await interaction.followup.send(extra, ephemeral=True)

        @self.tree.command(name="model", description="View or change the LLM model for this agent")
        @app_commands.describe(model="Model to switch to (omit to view; 'default' clears override)")
        @app_commands.choices(model=[
            app_commands.Choice(name="opus", value="opus"),
            app_commands.Choice(name="sonnet", value="sonnet"),
            app_commands.Choice(name="haiku", value="haiku"),
            app_commands.Choice(name="default", value="default"),
        ])
        async def cmd_model(interaction: discord.Interaction, model: str | None = None):
            agent_id = self._resolve_agent(interaction)
            if not agent_id:
                await interaction.response.send_message("No agent in this channel.", ephemeral=True)
                return

            agent_cfg = self.connector._agent_configs.get(agent_id, {})
            storage = getattr(self.connector._agent_manager, "storage", None) \
                if getattr(self.connector, "_agent_manager", None) else None
            yaml_model = agent_cfg.get("llm", {}).get("model", "unknown")

            if model is None:
                override = await storage.get_agent_override(agent_id, "model") if storage else None
                effective = override or yaml_model
                suffix = " (override)" if override else " (config)"
                await interaction.response.send_message(
                    f"**{agent_id}** model: `{effective}`{suffix}", ephemeral=True
                )
                return

            if not self._is_admin(interaction.user.id):
                await interaction.response.send_message("Not authorized.", ephemeral=True)
                return
            if not storage:
                await interaction.response.send_message("Storage unavailable.", ephemeral=True)
                return
            if model == "default":
                await storage.set_agent_override(agent_id, "model", None)
                await interaction.response.send_message(
                    f"Cleared model override for **{agent_id}** — using config (`{yaml_model}`).",
                    ephemeral=True,
                )
            else:
                await storage.set_agent_override(agent_id, "model", model)
                await interaction.response.send_message(
                    f"**{agent_id}** model set to `{model}` (persists across restarts).",
                    ephemeral=True,
                )

        @self.tree.command(name="effort", description="View or change the thinking effort level for this agent")
        @app_commands.describe(level="Effort level (omit to view; 'default' clears override)")
        @app_commands.choices(level=[
            app_commands.Choice(name="low", value="low"),
            app_commands.Choice(name="medium", value="medium"),
            app_commands.Choice(name="high", value="high"),
            app_commands.Choice(name="xhigh", value="xhigh"),
            app_commands.Choice(name="max", value="max"),
            app_commands.Choice(name="default", value="default"),
        ])
        async def cmd_effort(interaction: discord.Interaction, level: str | None = None):
            agent_id = self._resolve_agent(interaction)
            if not agent_id:
                await interaction.response.send_message("No agent in this channel.", ephemeral=True)
                return

            agent_cfg = self.connector._agent_configs.get(agent_id, {})
            storage = getattr(self.connector._agent_manager, "storage", None) \
                if getattr(self.connector, "_agent_manager", None) else None
            yaml_effort = agent_cfg.get("effort")

            if level is None:
                override = await storage.get_agent_override(agent_id, "effort") if storage else None
                effective = override or yaml_effort or "unset"
                if override:
                    suffix = " (override)"
                elif yaml_effort:
                    suffix = " (config)"
                else:
                    suffix = " — CLI picks default"
                await interaction.response.send_message(
                    f"**{agent_id}** effort: `{effective}`{suffix}", ephemeral=True
                )
                return

            if not self._is_admin(interaction.user.id):
                await interaction.response.send_message("Not authorized.", ephemeral=True)
                return
            if not storage:
                await interaction.response.send_message("Storage unavailable.", ephemeral=True)
                return
            if level == "default":
                await storage.set_agent_override(agent_id, "effort", None)
                fallback = yaml_effort or "CLI default"
                await interaction.response.send_message(
                    f"Cleared effort override for **{agent_id}** — using {fallback}.",
                    ephemeral=True,
                )
            else:
                await storage.set_agent_override(agent_id, "effort", level)
                await interaction.response.send_message(
                    f"**{agent_id}** effort set to `{level}` (persists across restarts).",
                    ephemeral=True,
                )

        @self.tree.command(
            name="email-watch",
            description="View or change how often this agent checks its email inbox")
        @app_commands.describe(
            interval="Seconds between checks, min 15 ('default' clears the override)")
        async def cmd_email_watch(interaction: discord.Interaction,
                                  interval: str | None = None):
            agent_id = self._resolve_agent(interaction)
            if not agent_id:
                await interaction.response.send_message("No agent in this channel.", ephemeral=True)
                return

            agent_cfg = self.connector._agent_configs.get(agent_id, {})
            ew_cfg = agent_cfg.get("email_watch") or {}
            if not ew_cfg.get("enabled"):
                await interaction.response.send_message(
                    f"Email watch is not enabled for **{agent_id}** — add an "
                    "`email_watch` block to agents.yaml and reboot.", ephemeral=True)
                return
            storage = getattr(self.connector._agent_manager, "storage", None) \
                if getattr(self.connector, "_agent_manager", None) else None
            yaml_interval = max(15, int(ew_cfg.get("interval", 60)))

            if interval is None:
                override = await storage.get_agent_override(
                    agent_id, "email_watch_interval") if storage else None
                effective = override or yaml_interval
                suffix = " (override)" if override else " (config)"
                await interaction.response.send_message(
                    f"**{agent_id}** checks email every `{effective}s`{suffix}. "
                    "Changes apply from the next check — no restart needed.",
                    ephemeral=True)
                return

            if not self._is_admin(interaction.user.id):
                await interaction.response.send_message("Not authorized.", ephemeral=True)
                return
            if not storage:
                await interaction.response.send_message("Storage unavailable.", ephemeral=True)
                return
            if interval == "default":
                await storage.set_agent_override(agent_id, "email_watch_interval", None)
                await interaction.response.send_message(
                    f"Cleared interval override for **{agent_id}** — using config "
                    f"(`{yaml_interval}s`).", ephemeral=True)
                return
            try:
                seconds = max(15, int(interval))
            except ValueError:
                await interaction.response.send_message(
                    "Interval must be a number of seconds (min 15) or 'default'.",
                    ephemeral=True)
                return
            await storage.set_agent_override(
                agent_id, "email_watch_interval", str(seconds))
            await interaction.response.send_message(
                f"**{agent_id}** now checks email every `{seconds}s` "
                "(persists across restarts; applies from the next check).",
                ephemeral=True)

        @self.tree.command(name="stop", description="Stop the agent's current task")
        async def cmd_stop(interaction: discord.Interaction):
            if not self._is_admin(interaction.user.id):
                await interaction.response.send_message("Not authorized.", ephemeral=True)
                return
            agent_id = self._resolve_agent(interaction)
            if not agent_id:
                await interaction.response.send_message("No agent in this channel.", ephemeral=True)
                return

            if hasattr(self.connector, '_agent_manager') and self.connector._agent_manager:
                result = await self.connector._agent_manager.stop_agent(agent_id)
                if result["stopped"]:
                    await interaction.response.send_message(
                        f"Stopped **{agent_id}**.", ephemeral=False
                    )
                else:
                    await interaction.response.send_message(
                        result["reason"], ephemeral=True
                    )
            else:
                await interaction.response.send_message("Agent manager not available.", ephemeral=True)

        @self.tree.command(
            name="hold",
            description="Interrupt the agent so you can interject — it will resume with your message")
        async def cmd_hold(interaction: discord.Interaction):
            if not self._is_admin(interaction.user.id):
                await interaction.response.send_message("Not authorized.", ephemeral=True)
                return
            agent_id = self._resolve_agent(interaction)
            if not agent_id:
                await interaction.response.send_message("No agent in this channel.", ephemeral=True)
                return

            if hasattr(self.connector, '_agent_manager') and self.connector._agent_manager:
                result = await self.connector._agent_manager.hold_agent(agent_id)
                if result["held"]:
                    await interaction.response.send_message(
                        f"**{agent_id}** paused. Send your message now — it'll resume with full context.",
                        ephemeral=False
                    )
                else:
                    await interaction.response.send_message(
                        result["reason"], ephemeral=True
                    )
            else:
                await interaction.response.send_message("Agent manager not available.", ephemeral=True)

        @self.tree.command(name="tools", description="List tools available to the agent in this channel")
        async def cmd_tools(interaction: discord.Interaction):
            agent_id = self._resolve_agent(interaction)
            if not agent_id:
                await interaction.response.send_message("No agent in this channel.", ephemeral=True)
                return

            agent_cfg = self.connector._agent_configs.get(agent_id, {})
            tools = agent_cfg.get("tools", [])
            if tools == "all":
                tool_list = "All tools available"
            elif tools:
                tool_list = "\n".join(f"  `{t}`" for t in sorted(tools))
            else:
                tool_list = "No tools configured"

            blocked = agent_cfg.get("disallow_builtins", [])
            blocked_str = ", ".join(f"`{b}`" for b in blocked) if blocked else "none"

            await interaction.response.send_message(
                f"**{agent_id}** tools:\n{tool_list}\n\n"
                f"Blocked builtins: {blocked_str}",
                ephemeral=True,
            )

        @self.tree.command(name="session", description="View or reset the agent session")
        @app_commands.describe(action="What to do with the session")
        @app_commands.choices(action=[
            app_commands.Choice(name="info", value="info"),
            app_commands.Choice(name="reset", value="reset"),
        ])
        async def cmd_session(interaction: discord.Interaction, action: str = "info"):
            agent_id = self._resolve_agent(interaction)
            if not agent_id:
                await interaction.response.send_message("No agent in this channel.", ephemeral=True)
                return

            if not (hasattr(self.connector, '_agent_manager') and self.connector._agent_manager):
                await interaction.response.send_message("Agent manager not available.", ephemeral=True)
                return

            am = self.connector._agent_manager

            if action == "reset":
                if not self._is_admin(interaction.user.id):
                    await interaction.response.send_message("Not authorized.", ephemeral=True)
                    return
                result = await am.reset_session(agent_id, str(interaction.channel_id))
                if result.get("reset"):
                    await interaction.response.send_message(
                        f"Session reset for **{agent_id}**. Next message starts fresh.",
                        ephemeral=False,
                    )
                else:
                    await interaction.response.send_message(
                        result.get("reason", "Failed."), ephemeral=True
                    )
            else:
                # Info
                import time as _time
                session = None
                for s in am.sessions.values():
                    if s.agent_id == agent_id and s.channel_id == str(interaction.channel_id):
                        session = s
                        break

                if not session:
                    await interaction.response.send_message(
                        f"No active session for **{agent_id}** in this channel.", ephemeral=True
                    )
                    return

                age_mins = int((_time.time() - session.created_at) / 60)
                running = agent_id in am._running_procs
                lines = [
                    f"**{agent_id}** session:",
                    f"Messages: {session.message_count}",
                    f"Age: {age_mins} min",
                    f"CLI session: `{session.cli_session_id or 'none'}`",
                    f"Currently running: {'yes' if running else 'no'}",
                ]
                await interaction.response.send_message("\n".join(lines), ephemeral=True)

        @self.tree.command(name="recall", description="Search agent memory")
        @app_commands.describe(query="What to search for")
        async def cmd_recall(interaction: discord.Interaction, query: str):
            agent_id = self._resolve_agent(interaction)
            if not agent_id:
                await interaction.response.send_message("No agent in this channel.", ephemeral=True)
                return

            if not (hasattr(self.connector, '_agent_manager') and self.connector._agent_manager):
                await interaction.response.send_message("Agent manager not available.", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True)
            am = self.connector._agent_manager
            memory = am._get_agent_memory(agent_id)
            if not memory:
                await interaction.followup.send("No memory backend configured.", ephemeral=True)
                return

            try:
                results = await memory.search(agent_id, query, limit=5)
                if not results:
                    await interaction.followup.send(f"No results for: `{query}`", ephemeral=True)
                    return

                lines = [f"**Memory results for:** `{query}`\n"]
                for r in results:
                    content = r.get("content", "")[:200]
                    tags = r.get("tags", "")
                    lines.append(f"- {content}")
                    if tags:
                        lines.append(f"  *tags: {tags}*")
                await interaction.followup.send("\n".join(lines), ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"Search error: {e}", ephemeral=True)

    # --- Admin commands ---

    def _register_admin_commands(self) -> None:
        """Register admin-only commands."""
        admin_group = app_commands.Group(name="admin", description="Admin commands")

        @admin_group.command(name="restart", description="Restart an agent session")
        @app_commands.describe(agent="Agent to restart (default: this channel's agent)")
        async def cmd_restart(interaction: discord.Interaction, agent: str | None = None):
            if not self._is_admin(interaction.user.id):
                await interaction.response.send_message("Not authorized.", ephemeral=True)
                return

            agent_id = agent or self.connector.get_agent_for_channel(
                str(interaction.channel_id), self.account_name
            )
            if not agent_id:
                await interaction.response.send_message("No agent found.", ephemeral=True)
                return

            # TODO: actually restart the session
            await interaction.response.send_message(
                f"Restarting agent `{agent_id}`... *(not yet implemented)*", ephemeral=True
            )

        @admin_group.command(name="pause", description="Pause an agent")
        @app_commands.describe(agent="Agent to pause")
        async def cmd_pause(interaction: discord.Interaction, agent: str | None = None):
            if not self._is_admin(interaction.user.id):
                await interaction.response.send_message("Not authorized.", ephemeral=True)
                return
            await interaction.response.send_message("Pause not yet implemented.", ephemeral=True)

        @admin_group.command(name="resume", description="Resume a paused agent")
        @app_commands.describe(agent="Agent to resume")
        async def cmd_resume(interaction: discord.Interaction, agent: str | None = None):
            if not self._is_admin(interaction.user.id):
                await interaction.response.send_message("Not authorized.", ephemeral=True)
                return
            await interaction.response.send_message("Resume not yet implemented.", ephemeral=True)

        @admin_group.command(name="sync", description="Re-sync slash commands with Discord")
        async def cmd_sync(interaction: discord.Interaction):
            if not self._is_admin(interaction.user.id):
                await interaction.response.send_message("Not authorized.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            try:
                target_guilds = list(self.client.guilds)
                if not target_guilds:
                    synced = await self.tree.sync()
                    await interaction.followup.send(
                        f"Synced {len(synced)} global commands.", ephemeral=True
                    )
                    return
                lines = []
                for guild in target_guilds:
                    try:
                        guild_obj = discord.Object(id=guild.id)
                        self.tree.clear_commands(guild=guild_obj)
                        await self.tree.sync(guild=guild_obj)
                        lines.append(f"✓ {guild.name}: guild commands cleared")
                    except Exception as ge:
                        lines.append(f"✗ {guild.name}: {ge}")
                synced_global = await self.tree.sync()
                lines.append(f"✓ Global: {len(synced_global)} commands (servers + DMs)")
                await interaction.followup.send(
                    "**Sync results:**\n" + "\n".join(lines), ephemeral=True
                )
            except Exception as e:
                await interaction.followup.send(f"Sync failed: {e}", ephemeral=True)

        @admin_group.command(
            name="hitl",
            description="Turn the human-approval gate on/off (off = full autonomy)",
        )
        @app_commands.describe(action="on, off, or status")
        async def cmd_hitl(interaction: discord.Interaction, action: str = "status"):
            if not self._is_admin(interaction.user.id):
                await interaction.response.send_message("Not authorized.", ephemeral=True)
                return
            hitl = getattr(self.connector, "_hitl", None)
            if hitl is None:
                await interaction.response.send_message(
                    "HITL is not configured on this deployment (no approvals channel).",
                    ephemeral=True,
                )
                return
            action = action.strip().lower()
            if action in ("on", "enable", "enabled"):
                await hitl.set_enabled(True)
                msg = "🔒 HITL approval gate is now **ON** — sensitive tools require approval."
            elif action in ("off", "disable", "disabled"):
                await hitl.set_enabled(False)
                msg = ("🔓 HITL approval gate is now **OFF** — tools run with no approval "
                       "prompts. The agent acts autonomously.")
            else:
                state = "ON" if hitl.enabled else "OFF"
                msg = f"HITL approval gate is currently **{state}**. Use `/admin hitl on|off` to change."
            await interaction.response.send_message(msg, ephemeral=True)

        @admin_group.command(
            name="triggers",
            description="Killswitch for event-triggered automation (on/off/status)",
        )
        @app_commands.describe(action="on, off, or status")
        async def cmd_triggers(interaction: discord.Interaction, action: str = "status"):
            if not self._is_admin(interaction.user.id):
                await interaction.response.send_message("Not authorized.", ephemeral=True)
                return
            from src.core import triggers as _trig
            action = action.strip().lower()
            if action in ("off", "disable", "stop"):
                _trig.set_enabled(False)
                msg = ("🛑 Event triggers are now **OFF** — no incoming webhook event "
                       "will run an agent (existing triggers are kept, just paused).")
            elif action in ("on", "enable", "start"):
                _trig.set_enabled(True)
                msg = "✅ Event triggers are now **ON** — webhook events will run agents again."
            else:
                n = len(_trig.list_triggers())
                state = "ON" if _trig.is_enabled() else "OFF"
                msg = (f"Event triggers are **{state}** — {n} registered. "
                       f"Use `/admin triggers on|off` to flip the killswitch.")
            await interaction.response.send_message(msg, ephemeral=True)

        @admin_group.command(
            name="schedule",
            description="Killswitch for scheduled tasks (on/off/status)",
        )
        @app_commands.describe(action="on, off, or status")
        async def cmd_schedule(interaction: discord.Interaction, action: str = "status"):
            if not self._is_admin(interaction.user.id):
                await interaction.response.send_message("Not authorized.", ephemeral=True)
                return
            from src.core import schedules as _sched
            action = action.strip().lower()
            if action in ("off", "disable", "stop"):
                _sched.set_enabled(False)
                msg = "🛑 Scheduled tasks are now **OFF** — nothing will fire (schedules kept, paused)."
            elif action in ("on", "enable", "start"):
                _sched.set_enabled(True)
                msg = "✅ Scheduled tasks are now **ON** — due tasks will fire again."
            else:
                n = len(_sched.list_schedules())
                state = "ON" if _sched.is_enabled() else "OFF"
                msg = f"Scheduled tasks are **{state}** — {n} registered. Use `/admin schedule on|off`."
            await interaction.response.send_message(msg, ephemeral=True)

        @admin_group.command(
            name="version",
            description="Show the running platform version (and whether an update is pending)",
        )
        async def cmd_version(interaction: discord.Interaction):
            if not self._is_admin(interaction.user.id):
                await interaction.response.send_message("Not authorized.", ephemeral=True)
                return
            from src.core import version as _v
            running = _v.read_running_version()
            checkout = _v.current_commit()
            run_v = (running.get("version") or running.get("short", "?")) if running else "unknown"
            run_sha = running.get("short", "?") if running else "?"
            lines = [f"**Running:** {run_v} (`{run_sha}`)"
                     + (f" — {running.get('subject','')}" if running else "")]
            lines.append(f"**On disk:** {checkout.get('version') or checkout['short']} "
                         f"(`{checkout['short']}`) — {checkout.get('subject','')}")
            if running and running.get("commit") == checkout.get("commit"):
                lines.append("✅ up to date")
            elif running:
                lines.append(f"⚠️ update `{checkout['short']}` on disk but not running — restart to apply.")
            await interaction.response.send_message("\n".join(lines), ephemeral=True)

        @admin_group.command(name="reload", description="Hot-reload skills, tools, and config")
        async def cmd_reload(interaction: discord.Interaction):
            if not self._is_admin(interaction.user.id):
                await interaction.response.send_message("Not authorized.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            try:
                from src.core.digest import reload_skills, reload_tools
                skills_count = reload_skills()
                tools_count = reload_tools()

                # Update connector's skill reference
                from src.core.skills import get_all_skills
                self.connector.set_skills(get_all_skills())

                await interaction.followup.send(
                    f"Reloaded: {tools_count} tools, {skills_count} skills.\n"
                    f"Run `/admin sync` to update slash commands.",
                    ephemeral=True
                )
            except Exception as e:
                await interaction.followup.send(f"Reload failed: {e}", ephemeral=True)

        @admin_group.command(name="reboot", description="Full process restart")
        async def cmd_reboot(interaction: discord.Interaction):
            if not self._is_admin(interaction.user.id):
                await interaction.response.send_message("Not authorized.", ephemeral=True)
                return
            await interaction.response.send_message(
                "Restarting kbots... back in ~3 seconds.", ephemeral=True
            )
            import subprocess
            import sys
            if sys.platform == "darwin":
                label = os.environ.get("KBOTS_LAUNCHD_LABEL", "com.kbots.agent")
                subprocess.Popen(
                    ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"]
                )
            else:
                service = os.environ.get("KBOTS_SERVICE_NAME", "kbots")
                subprocess.Popen(["sudo", "systemctl", "restart", service])

        @admin_group.command(
            name="update",
            description="Pull latest engine, sync deps, hot-reload or restart as needed",
        )
        async def cmd_update(interaction: discord.Interaction):
            if not self._is_admin(interaction.user.id):
                await interaction.response.send_message("Not authorized.", ephemeral=True)
                return
            await interaction.response.send_message(
                "Updating kbots... (restarts the service if core code changed)",
                ephemeral=True,
            )
            import asyncio

            from src.core.base import PROJECT_ROOT
            script = PROJECT_ROOT / "scripts" / "update.sh"
            try:
                proc = await asyncio.create_subprocess_exec(
                    str(script),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=str(PROJECT_ROOT),
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
                output = stdout.decode().strip()[-1800:] or "(no output)"
                await interaction.followup.send(f"```\n{output}\n```", ephemeral=True)
            except asyncio.TimeoutError:
                await interaction.followup.send("Update timed out (600s).", ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"Update failed: {e}", ephemeral=True)

        @admin_group.command(
            name="usage",
            description="Token usage per agent + current tool-call rates",
        )
        async def cmd_usage(interaction: discord.Interaction):
            if not self._is_admin(interaction.user.id):
                await interaction.response.send_message("Not authorized.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            mgr = getattr(self.connector, "_agent_manager", None)
            if mgr is None:
                await interaction.followup.send("Agent manager unavailable.", ephemeral=True)
                return

            lines = ["**Token usage (recorded per message)**"]
            storage = getattr(mgr, "storage", None)
            if storage is None:
                lines.append("  (no storage — token usage not tracked)")
            else:
                today = await storage.get_token_usage(days=1)
                week = await storage.get_token_usage(days=7)
                if not week:
                    lines.append("  (no usage recorded yet)")
                else:
                    today_map = {r["agent_id"]: r for r in today}
                    lines.append("  agent — today | 7 days")
                    for r in week:
                        t = today_map.get(r["agent_id"], {"tokens": 0, "messages": 0})
                        lines.append(
                            f"  `{r['agent_id']}` — {t['tokens']:,} tok / {t['messages']} msg "
                            f"| {r['tokens']:,} tok / {r['messages']} msg"
                        )

            if storage is not None:
                counters = await storage.get_counters(days=7)
                if counters:
                    local = counters.get("router.local", 0) + counters.get("skill.pinned", 0)
                    lines.append("\n**Local vs Claude (7 days)**")
                    lines.append(
                        f"  stayed local: {local} · to Claude: {counters.get('router.claude', 0)} "
                        f"· local ok/err: {counters.get('local.success', 0)}/{counters.get('local.error', 0)}"
                    )
                    extra = []
                    if counters.get("action.direct"):
                        extra.append(f"zero-LLM actions: {counters['action.direct']}")
                    if counters.get("local.fallback"):
                        extra.append(f"fallbacks to Claude: {counters['local.fallback']}")
                    if extra:
                        lines.append("  " + " · ".join(extra))

            rl = getattr(mgr, "rate_limiter", None)
            stats = rl.get_stats() if rl else {}
            lines.append("\n**Tool calls (last hour)**")
            if stats:
                for key, s in sorted(stats.items()):
                    lines.append(f"  `{key}` — {s['last_hour']}")
            else:
                lines.append("  (none this hour)")

            lines.append(
                "\n_Note: subscription usage limits are enforced by Claude, not shown here — "
                "agents auto-downgrade to cheaper models when a cap is hit._"
            )
            await interaction.followup.send("\n".join(lines)[:1900], ephemeral=True)

        @admin_group.command(
            name="claude-auth",
            description="Check or refresh Claude Code authentication",
        )
        @app_commands.describe(action="status or refresh")
        async def cmd_claude_auth(interaction: discord.Interaction, action: str = "status"):
            if not self._is_admin(interaction.user.id):
                await interaction.response.send_message("Not authorized.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            import asyncio
            import json as _json
            import shutil

            claude = shutil.which("claude")
            if not claude:
                await interaction.followup.send("Claude Code CLI not found on PATH.", ephemeral=True)
                return

            async def _status() -> dict:
                try:
                    p = await asyncio.create_subprocess_exec(
                        claude, "auth", "status",
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    )
                    out, _ = await asyncio.wait_for(p.communicate(), timeout=30)
                    try:
                        return _json.loads(out.decode())
                    except ValueError:
                        return {"loggedIn": p.returncode == 0, "raw": out.decode().strip()[:300]}
                except Exception as e:
                    return {"loggedIn": False, "raw": str(e)}

            action = action.strip().lower()
            if action in ("refresh", "reauth"):
                # 'auth status' triggers the CLI's own token refresh when the
                # access token is expired but the refresh token is still valid.
                await _status()
                st = await _status()
                if st.get("loggedIn"):
                    await interaction.followup.send(
                        f"✅ Auth healthy after refresh (`{st.get('email', '?')}`, "
                        f"{st.get('subscriptionType', '?')}). Agents can respond.",
                        ephemeral=True,
                    )
                else:
                    await interaction.followup.send(
                        "❌ Refresh didn't help — the login is fully expired. Someone must "
                        "run `claude setup-token` (or `claude auth login`) on the host and "
                        "authorize in a browser, then `/admin reboot`. This step needs a "
                        "terminal — it can't be done from Discord.",
                        ephemeral=True,
                    )
                return

            st = await _status()
            if st.get("loggedIn"):
                await interaction.followup.send(
                    f"✅ Logged in: `{st.get('email', '?')}` "
                    f"({st.get('subscriptionType', '?')}, {st.get('authMethod', '?')})",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    f"❌ Not logged in. Try `/admin claude-auth refresh`.\n"
                    f"```\n{st.get('raw', '')}\n```",
                    ephemeral=True,
                )

        @admin_group.command(
            name="update-claude",
            description="Update the Claude Code CLI to the latest version",
        )
        async def cmd_update_claude(interaction: discord.Interaction):
            if not self._is_admin(interaction.user.id):
                await interaction.response.send_message("Not authorized.", ephemeral=True)
                return
            await interaction.response.send_message(
                "Checking for Claude Code updates...", ephemeral=True
            )
            import asyncio
            import shutil

            claude = shutil.which("claude")
            if not claude:
                await interaction.followup.send(
                    "Claude Code CLI not found on PATH.", ephemeral=True
                )
                return

            async def _version() -> str:
                try:
                    p = await asyncio.create_subprocess_exec(
                        claude, "--version",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                    )
                    out, _ = await asyncio.wait_for(p.communicate(), timeout=20)
                    return out.decode().strip()
                except Exception:
                    return "unknown"

            before = await _version()
            try:
                proc = await asyncio.create_subprocess_exec(
                    claude, "update",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
                output = stdout.decode().strip()[-1200:] or "(no output)"
            except asyncio.TimeoutError:
                await interaction.followup.send("Claude update timed out (300s).", ephemeral=True)
                return
            except Exception as e:
                await interaction.followup.send(f"Claude update failed: {e}", ephemeral=True)
                return

            after = await _version()
            note = (
                f"Updated: `{before}` → `{after}`. The next message uses the new version "
                f"(no restart needed)."
                if after != before
                else f"Already up to date (`{after}`)."
            )
            await interaction.followup.send(f"{note}\n```\n{output}\n```", ephemeral=True)

        self.tree.add_command(admin_group)

    # --- Skill commands ---

    @staticmethod
    async def _run_direct_command(interaction: discord.Interaction, command: str) -> None:
        """Run a skill command directly and post output to Discord, bypassing the LLM.

        The command field comes from skill YAML on disk (not user input).
        Uses subprocess_exec with shlex.split to avoid shell injection.
        """
        import asyncio
        import os
        import shlex
        kbots_home = os.environ.get("KBOTS_HOME", "/opt/kbots")
        env = {**os.environ, "TERM": "dumb", "KBOTS_HOME": kbots_home}
        try:
            parts = shlex.split(command)
            if not parts[0].startswith("/"):
                parts[0] = os.path.join(kbots_home, parts[0])
            if not os.path.isfile(parts[0]):
                output = f"Command not found: {parts[0]}"
                await interaction.followup.send(f"```\n{output}\n```")
                return
            proc = await asyncio.create_subprocess_exec(
                *parts,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            output = stdout.decode().strip()
            if not output and stderr:
                output = f"stderr: {stderr.decode().strip()}"
            if not output:
                output = "(no output)"
        except asyncio.TimeoutError:
            output = "Command timed out (60s)"
        except Exception as e:
            output = f"Error: {e}"

        if len(output) > 1900:
            import io
            file = discord.File(io.BytesIO(output.encode()), filename="audit-report.txt")
            summary = output.split("\n")[1] if "\n" in output else output[:100]
            await interaction.followup.send(summary, file=file)
        else:
            await interaction.followup.send(f"```\n{output}\n```")

    def _register_skill_commands(self) -> None:
        """Auto-register each skill as a slash command."""
        skills = self.connector._skills
        if not skills:
            return

        for skill_name, skill in skills.items():
            # Skip agent-prefixed skills
            if ":" in skill_name:
                continue

            self._register_single_skill_command(skill)

        logger.info(f"Registered {len([s for s in skills if ':' not in s])} skill commands")

    def _register_single_skill_command(self, skill: Any) -> None:
        """Register a single skill as a slash command.

        Dynamically builds a callback function with typed parameters so discord.py
        can introspect the signature and register proper slash command options.
        """
        cmd_name = skill.name.replace("_", "-")[:32]  # Discord: max 32 chars, no underscores
        cmd_desc = (skill.description or f"Run {skill.name} skill")[:100]  # Discord: max 100 chars
        skill_name = skill.name  # capture for closure
        connector = self.connector

        # Build parameter annotations and defaults for the callback
        # discord.py inspects the callback signature to build slash command params
        param_annotations = {"interaction": discord.Interaction}
        param_defaults = {}
        param_descriptions = {}

        type_map = {
            "string": str,
            "integer": int,
            "boolean": bool,
            "number": float,
        }

        import re
        safe_ident_re = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
        for p in skill.parameters:
            if not safe_ident_re.match(p.name):
                logger.warning(f"Skill {skill.name}: skipping invalid param name {p.name!r}")
                continue
            py_type = type_map.get(p.type, str)
            if not p.required:
                py_type = py_type | None
                param_defaults[p.name] = None
            param_annotations[p.name] = py_type
            if p.description:
                param_descriptions[p.name] = p.description

        # Build callback via closure factory — no exec() needed
        safe_params = [p for p in skill.parameters if safe_ident_re.match(p.name)]
        kwarg_names = [p.name for p in safe_params]

        def _make_skill_callback(_connector, _skill_name, _kwarg_names, _account_name, _command=None):
            async def _skill_cmd(interaction: discord.Interaction, **kwargs):
                await interaction.response.defer()
                if _command:
                    logger.debug(f"Skill {_skill_name}: direct command path → {_command}")
                    await DiscordBot._run_direct_command(interaction, _command)
                    return
                logger.debug(f"Skill {_skill_name}: LLM path (no command field)")
                await _connector.emit(IncomingMessage(
                    connector="discord",
                    channel_id=str(interaction.channel_id),
                    channel_name=getattr(interaction.channel, "name", None),
                    user_id=str(interaction.user.id),
                    user_name=interaction.user.display_name,
                    content="",
                    raw=interaction,
                    bot_account=_account_name,
                    skill=_skill_name,
                    skill_params={k: kwargs.get(k) for k in _kwarg_names},
                ))
            # Set parameter annotations so discord.py builds the slash command signature
            annotations = {"interaction": discord.Interaction}
            for p in safe_params:
                py_type = type_map.get(p.type, str)
                annotations[p.name] = py_type if p.required else (py_type | None)
            _skill_cmd.__annotations__ = annotations
            # Set defaults for optional params
            import inspect
            params = [inspect.Parameter("interaction", inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                        annotation=discord.Interaction)]
            for p in safe_params:
                py_type = type_map.get(p.type, str)
                if p.required:
                    params.append(inspect.Parameter(p.name, inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                                    annotation=py_type))
                else:
                    params.append(inspect.Parameter(p.name, inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                                    default=None, annotation=py_type | None))
            _skill_cmd.__signature__ = inspect.Signature(params)
            return _skill_cmd

        callback = _make_skill_callback(connector, skill_name, kwarg_names, self.account_name, skill.command)

        # Add choice decorators
        if any(p.choices for p in skill.parameters):
            for p in skill.parameters:
                if p.choices:
                    choices = [app_commands.Choice(name=c, value=c) for c in p.choices]
                    callback = app_commands.choices(**{p.name: choices})(callback)

        # Add descriptions
        if param_descriptions:
            callback = app_commands.describe(**param_descriptions)(callback)

        # Create and register the command
        cmd = app_commands.Command(
            name=cmd_name,
            description=cmd_desc,
            callback=callback,
        )
        self.tree.add_command(cmd)

    def _is_admin(self, user_id: int) -> bool:
        """Check if a user is an admin."""
        return str(user_id) in self.admin_users


# === Helpers ===

def _short_task_hint(text: str, limit: int = 40) -> str:
    """A short, single-line hint of the task for the bot's status."""
    import re
    text = re.sub(r"<@[!&]?\d+>", "", text or "")   # strip mentions
    text = " ".join(text.split())                    # collapse whitespace
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _split_message(content: str, limit: int = 2000) -> list[str]:
    """Split a message into chunks that fit Discord's character limit."""
    if len(content) <= limit:
        return [content]

    chunks = []
    while content:
        if len(content) <= limit:
            chunks.append(content)
            break

        # Try to split at a newline
        split_at = content.rfind("\n", 0, limit)
        if split_at == -1:
            # Try space
            split_at = content.rfind(" ", 0, limit)
        if split_at == -1:
            # Hard split
            split_at = limit

        chunks.append(content[:split_at])
        content = content[split_at:].lstrip("\n")

    return chunks


def _skill_type_to_discord(type_str: str) -> discord.AppCommandOptionType:
    """Map skill parameter types to Discord option types."""
    mapping = {
        "string": discord.AppCommandOptionType.string,
        "integer": discord.AppCommandOptionType.integer,
        "boolean": discord.AppCommandOptionType.boolean,
        "number": discord.AppCommandOptionType.number,
        "user": discord.AppCommandOptionType.user,
        "channel": discord.AppCommandOptionType.channel,
    }
    return mapping.get(type_str, discord.AppCommandOptionType.string)


class _NoopTyping:
    """No-op async context manager for when no bot can reach a channel."""
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
