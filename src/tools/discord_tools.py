"""Discord tools — channel history, messages, and channel management."""

import logging
from datetime import datetime
from pathlib import Path

import aiohttp

from src.core.base import ToolContext
from src.core.tools import tool

logger = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api/v10"


def _discord_headers(vault, bot: str = "") -> dict | None:
    """Get Discord bot auth headers from vault.

    Args:
        vault: Vault backend for token access.
        bot: Which bot account to use (e.g. 'main', 'assistant'). Empty for default.
    """
    token = None
    if vault:
        if bot:
            # Account tokens live at discord-<name> (settings.py convention);
            # discord-token-<name> is a legacy key form kept as fallback.
            token = vault.get(f"discord-{bot}") or vault.get(f"discord-token-{bot}")
            if not token:
                logger.warning(f"No Discord token for bot '{bot}' (keys tried: "
                               f"discord-{bot}, discord-token-{bot})")
                return None
        else:
            token = vault.get("active-discord-token") or vault.get("discord-token")
    if not token:
        return None
    return {
        "Authorization": f"Bot {token}",
        "User-Agent": "DiscordBot (https://github.com/karvidsson/kbots, 1.0)",
    }


async def _discord_get(vault, endpoint: str, bot: str = "") -> dict | list | None:
    """Make an authenticated GET request to the Discord API."""
    headers = _discord_headers(vault, bot=bot)
    if not headers:
        return None

    async with aiohttp.ClientSession() as session:
        async with session.get(f"{DISCORD_API}{endpoint}", headers=headers) as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                logger.error(f"Discord API {endpoint}: {resp.status} {await resp.text()}")
                return None


async def _discord_post(vault, endpoint: str, json: dict, bot: str = "") -> dict | None:
    """Make an authenticated POST request to the Discord API."""
    headers = _discord_headers(vault, bot=bot)
    if not headers:
        return None

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{DISCORD_API}{endpoint}", headers=headers, json=json
        ) as resp:
            if resp.status in (200, 201):
                return await resp.json()
            else:
                error = await resp.text()
                logger.error(f"Discord API POST {endpoint}: {resp.status} {error}")
                return {"error": True, "status": resp.status, "detail": error[:300]}


async def _discord_patch(vault, endpoint: str, json: dict, bot: str = "") -> dict | None:
    """Make an authenticated PATCH request to the Discord API."""
    headers = _discord_headers(vault, bot=bot)
    if not headers:
        return None

    async with aiohttp.ClientSession() as session:
        async with session.patch(
            f"{DISCORD_API}{endpoint}", headers=headers, json=json
        ) as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                error = await resp.text()
                logger.error(f"Discord API PATCH {endpoint}: {resp.status} {error}")
                return {"error": True, "status": resp.status, "detail": error[:300]}


async def _discord_delete(vault, endpoint: str, bot: str = "") -> dict | None:
    """Make an authenticated DELETE request to the Discord API."""
    headers = _discord_headers(vault, bot=bot)
    if not headers:
        return None

    async with aiohttp.ClientSession() as session:
        async with session.delete(f"{DISCORD_API}{endpoint}", headers=headers) as resp:
            if resp.status == 204:
                return {"success": True}
            elif resp.status == 200:
                return await resp.json()
            else:
                error = await resp.text()
                logger.error(f"Discord API DELETE {endpoint}: {resp.status} {error}")
                return {"error": True, "status": resp.status, "detail": error[:300]}


def _format_message(msg: dict) -> str:
    """Format a Discord message for display."""
    author = msg.get("author", {}).get("username", "unknown")
    content = msg.get("content", "")
    timestamp = msg.get("timestamp", "")
    # Parse and simplify timestamp
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        time_str = dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, AttributeError):
        time_str = timestamp

    attachments = msg.get("attachments", [])
    attachment_str = ""
    if attachments:
        att_details = []
        for a in attachments:
            name = a.get("filename", "file")
            url = a.get("url", "")
            ct = a.get("content_type", "")
            size = a.get("size", 0)
            att_details.append(f"{name} ({ct}, {size}B): {url}")
        attachment_str = "\n  Attachments: " + "; ".join(att_details)

    embeds = msg.get("embeds", [])
    embed_str = ""
    if embeds:
        embed_str = f" [{len(embeds)} embed(s)]"

    # The id is what message-targeting tools (discord_delete_message,
    # read_message, reactions) take as input — without it here, no tool
    # surfaces it and "act on that message" tasks dead-end.
    msg_id = msg.get("id", "")
    id_str = f" (id {msg_id})" if msg_id else ""

    return f"[{time_str}] {author}{id_str}: {content}{attachment_str}{embed_str}"


@tool(
    name="read_channel_history",
    description="Read recent messages from a Discord channel. Use this when asked to check what was said in a channel.",
)
async def read_channel_history(
    ctx: ToolContext, channel_id: str, limit: int = 20
) -> str:
    """Read the last N messages from a Discord channel.

    Args:
        channel_id: The Discord channel ID to read from.
        limit: Number of messages to fetch (1-100, default 20).
    """
    if not ctx.vault:
        return "Error: no vault access — cannot authenticate with Discord."

    limit = max(1, min(100, limit))

    messages = await _discord_get(ctx.vault, f"/channels/{channel_id}/messages?limit={limit}")
    if messages is None:
        return f"Error: could not fetch messages from channel {channel_id}. Check the channel ID and bot permissions."

    if not messages:
        return f"No messages found in channel {channel_id}."

    # Messages come newest-first, reverse for chronological order
    messages.reverse()

    lines = [f"Last {len(messages)} messages from <#{channel_id}>:", ""]
    for msg in messages:
        lines.append(_format_message(msg))

    return "\n".join(lines)


@tool(
    name="read_message",
    description="Read a specific Discord message by its ID from a channel.",
)
async def read_message(ctx: ToolContext, channel_id: str, message_id: str) -> str:
    """Fetch a specific message by ID.

    Args:
        channel_id: The Discord channel ID containing the message.
        message_id: The specific message ID to fetch.
    """
    if not ctx.vault:
        return "Error: no vault access."

    msg = await _discord_get(ctx.vault, f"/channels/{channel_id}/messages/{message_id}")
    if msg is None:
        return f"Error: could not fetch message {message_id} from channel {channel_id}."

    return _format_message(msg)


@tool(
    name="search_channel_history",
    description="Search for messages containing specific text in a Discord channel's recent history.",
)
async def search_channel_history(
    ctx: ToolContext, channel_id: str, query: str, limit: int = 50
) -> str:
    """Search recent channel messages for a keyword/phrase.

    Args:
        channel_id: The Discord channel ID to search.
        query: Text to search for (case-insensitive).
        limit: How many recent messages to search through (1-100, default 50).
    """
    if not ctx.vault:
        return "Error: no vault access."

    limit = max(1, min(100, limit))

    messages = await _discord_get(ctx.vault, f"/channels/{channel_id}/messages?limit={limit}")
    if messages is None:
        return f"Error: could not fetch messages from channel {channel_id}."

    query_lower = query.lower()
    matches = [msg for msg in messages if query_lower in msg.get("content", "").lower()]

    if not matches:
        return f"No messages containing '{query}' in the last {limit} messages of <#{channel_id}>."

    matches.reverse()  # chronological order

    lines = [f"Found {len(matches)} message(s) matching '{query}' in <#{channel_id}>:", ""]
    for msg in matches:
        lines.append(_format_message(msg))

    return "\n".join(lines)


@tool(
    name="send_discord_file",
    description="Send a file (image, document, etc.) to a Discord channel with an optional message.",
    category="discord",
)
async def send_discord_file(
    ctx: ToolContext, channel_id: str, file_path: str, message: str = "", bot: str = ""
) -> str:
    """Upload a file to a Discord channel.

    Args:
        channel_id: Discord channel ID to send to.
        file_path: Local file path to upload.
        message: Optional text message to accompany the file.
        bot: Which bot account to send as (e.g. 'main', 'assistant'). Leave empty for default.
    """
    if not ctx.vault:
        return "Error: no vault access."

    token = None
    if bot:
        token = ctx.vault.get(f"discord-token-{bot}")
        if not token:
            return f"Error: no Discord token for bot '{bot}'."
    else:
        token = ctx.vault.get("active-discord-token") or ctx.vault.get("discord-token")
    if not token:
        return "Error: no Discord token available."

    from src.tools.ingest import validate_file_path
    path_err = validate_file_path(file_path)
    if path_err:
        return path_err
    path = Path(file_path)
    if not path.exists():
        return f"Error: file not found: {file_path}"

    if path.stat().st_size > 25 * 1024 * 1024:
        return f"Error: file too large ({path.stat().st_size / 1024 / 1024:.1f}MB). Discord limit is 25MB."

    headers = {
        "Authorization": f"Bot {token}",
        "User-Agent": "DiscordBot (https://github.com/karvidsson/kbots, 1.0)",
    }

    async with aiohttp.ClientSession() as session:
        data = aiohttp.FormData()
        if message:
            data.add_field("content", message)
        data.add_field(
            "files[0]", path.read_bytes(),
            filename=path.name,
            content_type="application/octet-stream",
        )
        async with session.post(
            f"{DISCORD_API}/channels/{channel_id}/messages",
            headers=headers,
            data=data,
        ) as resp:
            if resp.status == 200:
                return f"File {path.name} sent to channel {channel_id}"
            else:
                error = await resp.text()
                return f"Failed to send file (HTTP {resp.status}): {error[:300]}"


# --- Channel management tools ---

CHANNEL_TYPES = {
    "text": 0,
    "voice": 2,
    "category": 4,
    "announcement": 5,
    "forum": 15,
}


@tool(
    name="discord_list_channels",
    description="List all channels in a Discord server (guild).",
    category="discord",
)
async def discord_list_channels(ctx: ToolContext, guild_id: str) -> str:
    """List all channels in a guild, grouped by category.

    Args:
        guild_id: The Discord server (guild) ID.
    """
    if not ctx.vault:
        return "Error: no vault access."

    channels = await _discord_get(ctx.vault, f"/guilds/{guild_id}/channels")
    if channels is None:
        return f"Error: could not fetch channels for guild {guild_id}."

    # Group by category
    categories = {}
    uncategorized = []
    cat_names = {}

    for ch in channels:
        if ch["type"] == 4:  # category
            cat_names[ch["id"]] = ch["name"]

    for ch in channels:
        if ch["type"] == 4:
            continue
        type_name = {0: "text", 2: "voice", 5: "announcement", 15: "forum"}.get(ch["type"], f"type-{ch['type']}")
        entry = f"  - #{ch['name']} ({type_name}, id: {ch['id']})"
        parent = ch.get("parent_id")
        if parent and parent in cat_names:
            categories.setdefault(parent, []).append(entry)
        else:
            uncategorized.append(entry)

    lines = [f"Channels in guild {guild_id}:", ""]
    for cat_id, entries in sorted(categories.items(), key=lambda x: cat_names.get(x[0], "")):
        lines.append(f"**{cat_names[cat_id]}** (id: {cat_id})")
        lines.extend(sorted(entries))
        lines.append("")
    if uncategorized:
        lines.append("**Uncategorized**")
        lines.extend(sorted(uncategorized))

    return "\n".join(lines)


@tool(
    name="discord_create_channel",
    description="Create a new channel in a Discord server.",
    category="discord",
)
async def discord_create_channel(
    ctx: ToolContext,
    guild_id: str,
    name: str,
    channel_type: str = "text",
    topic: str = "",
    category_id: str = "",
) -> str:
    """Create a new Discord channel.

    Args:
        guild_id: The Discord server (guild) ID.
        name: Channel name (auto-lowercased, spaces become hyphens).
        channel_type: One of: text, voice, category, announcement, forum. Default: text.
        topic: Optional channel topic/description.
        category_id: Optional parent category ID to place the channel under.
    """
    if not ctx.vault:
        return "Error: no vault access."

    type_int = CHANNEL_TYPES.get(channel_type.lower())
    if type_int is None:
        return f"Error: invalid channel type '{channel_type}'. Use: {', '.join(CHANNEL_TYPES.keys())}"

    payload = {"name": name, "type": type_int}
    if topic:
        payload["topic"] = topic
    if category_id:
        payload["parent_id"] = category_id

    result = await _discord_post(ctx.vault, f"/guilds/{guild_id}/channels", payload)
    if result is None:
        return "Error: no Discord token available."
    if result.get("error"):
        return f"Error creating channel: {result['detail']}"

    return f"Channel #{result['name']} created (id: {result['id']}, type: {channel_type})"


@tool(
    name="discord_edit_channel",
    description="Edit a Discord channel's name, topic, or other settings.",
    category="discord",
)
async def discord_edit_channel(
    ctx: ToolContext,
    channel_id: str,
    name: str = "",
    topic: str = "",
    category_id: str = "",
) -> str:
    """Edit a Discord channel.

    Args:
        channel_id: The channel ID to edit.
        name: New channel name (leave empty to keep current).
        topic: New topic (leave empty to keep current).
        category_id: Move to this category (leave empty to keep current).
    """
    if not ctx.vault:
        return "Error: no vault access."

    payload = {}
    if name:
        payload["name"] = name
    if topic:
        payload["topic"] = topic
    if category_id:
        payload["parent_id"] = category_id

    if not payload:
        return "Error: nothing to change — provide at least one of: name, topic, category_id."

    result = await _discord_patch(ctx.vault, f"/channels/{channel_id}", payload)
    if result is None:
        return "Error: no Discord token available."
    if result.get("error"):
        return f"Error editing channel: {result['detail']}"

    return f"Channel #{result['name']} updated (id: {result['id']})"


@tool(
    name="discord_delete_channel",
    description="[HITL-GATED] Delete a Discord channel. This cannot be undone.",
    category="discord",
    hitl=True,
)
async def discord_delete_channel(ctx: ToolContext, channel_id: str) -> str:
    """Delete a Discord channel permanently.

    Args:
        channel_id: The channel ID to delete.
    """
    if not ctx.vault:
        return "Error: no vault access."

    result = await _discord_delete(ctx.vault, f"/channels/{channel_id}")
    if result is None:
        return "Error: no Discord token available."
    if result.get("error"):
        return f"Error deleting channel: {result['detail']}"

    return f"Channel {channel_id} deleted."


@tool(
    name="discord_list_guilds",
    description="List all Discord servers (guilds) the bot is a member of.",
    category="discord",
)
async def discord_list_guilds(ctx: ToolContext) -> str:
    """List all guilds the bot has access to."""
    if not ctx.vault:
        return "Error: no vault access."

    guilds = await _discord_get(ctx.vault, "/users/@me/guilds")
    if guilds is None:
        return "Error: could not fetch guilds."

    if not guilds:
        return "Bot is not in any guilds."

    lines = ["Bot is in these servers:", ""]
    for g in guilds:
        lines.append(f"- **{g['name']}** (id: {g['id']})")

    return "\n".join(lines)


@tool(
    name="discord_set_server_icon",
    description="Set or update a Discord server's icon from a local image file.",
    category="discord",
)
async def discord_set_server_icon(
    ctx: ToolContext, guild_id: str, image_path: str
) -> str:
    """Set a server's icon from a local image file.

    Args:
        guild_id: The Discord server (guild) ID.
        image_path: Local path to the image file (PNG or JPEG).
    """
    if not ctx.vault:
        return "Error: no vault access."

    import base64

    path = Path(image_path)
    if not path.exists():
        return f"Error: file not found: {image_path}"

    suffix = path.suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        mime = "image/jpeg"
    elif suffix == ".png":
        mime = "image/png"
    else:
        return f"Error: unsupported image format '{suffix}'. Use PNG or JPEG."

    icon_data = path.read_bytes()
    icon_b64 = f"data:{mime};base64,{base64.b64encode(icon_data).decode()}"

    result = await _discord_patch(ctx.vault, f"/guilds/{guild_id}", {"icon": icon_b64})
    if result is None:
        return "Error: no Discord token available."
    if result.get("error"):
        return f"Error setting icon: {result['detail']}"

    return f"Server icon updated for guild {guild_id}"


@tool(
    name="discord_set_nickname",
    description=(
        "Set a bot's per-server nickname (e.g. rename Milo's bot to 'Milo' "
        "on a guild). Uses that bot's own token; the bot needs the Change "
        "Nickname permission. Empty guild_id applies to every mutual server."
    ),
)
async def discord_set_nickname(
    ctx: ToolContext, nickname: str, bot: str = "", guild_id: str = ""
) -> str:
    """Rename a bot on a server via PATCH /guilds/{id}/members/@me."""
    headers = _discord_headers(ctx.vault, bot=bot)
    if not headers:
        return f"No Discord token found for bot '{bot or 'main'}' in the vault."

    async with aiohttp.ClientSession() as session:
        if guild_id:
            guild_ids = [guild_id]
        else:
            async with session.get(f"{DISCORD_API}/users/@me/guilds", headers=headers) as resp:
                if resp.status != 200:
                    return f"Could not list guilds (HTTP {resp.status}) — pass guild_id explicitly."
                guild_ids = [g["id"] for g in await resp.json()]
        if not guild_ids:
            return f"Bot '{bot or 'main'}' is not in any server yet — invite it first."

        results = []
        for gid in guild_ids:
            async with session.patch(
                f"{DISCORD_API}/guilds/{gid}/members/@me",
                headers=headers, json={"nick": nickname},
            ) as resp:
                if resp.status == 200:
                    results.append(f"{gid}: nickname set to '{nickname}'")
                elif resp.status == 403:
                    results.append(f"{gid}: FAILED — bot lacks the Change Nickname permission")
                elif resp.status == 429:
                    results.append(f"{gid}: FAILED — rate limited, try again in a minute")
                else:
                    results.append(f"{gid}: FAILED — HTTP {resp.status}: {(await resp.text())[:120]}")
        return "\n".join(results)


@tool(
    name="discord_set_bot_name",
    description=(
        "Rename a bot's Discord ACCOUNT username (what shows in DMs, the API "
        "and the roster), not just a per-server nickname. Discord allows only "
        "two username changes per hour, so the name is validated before the "
        "request is sent and a rate limit is never retried automatically."
    ),
    category="discord",
)
async def discord_set_bot_name(ctx: ToolContext, name: str, bot: str = "") -> str:
    """Rename the bot account via PATCH /users/@me.

    Args:
        name: The new account username, 2 to 32 characters.
        bot: Which bot account to rename (e.g. 'milo'). Empty for default.
    """
    name = (name or "").strip()
    # Validate BEFORE sending. A rejected request still spends one of the two
    # changes Discord allows per hour, so a typo would cost half the budget
    # and the retry would be the one that gets rate limited.
    if not 2 <= len(name) <= 32:
        return (f"Not sent: a Discord username must be 2 to 32 characters, "
                f"{name!r} is {len(name)}. A rejected request still spends one "
                f"of the two changes allowed per hour.")

    headers = _discord_headers(ctx.vault, bot=bot)
    if not headers:
        return f"No Discord token found for bot '{bot or 'main'}' in the vault."

    async with aiohttp.ClientSession() as session:
        async with session.patch(
            f"{DISCORD_API}/users/@me", headers=headers, json={"username": name},
        ) as resp:
            if resp.status == 200:
                body = await resp.json()
                return f"Bot account renamed to {body.get('username', name)!r}."
            text = (await resp.text())[:300]
            if resp.status == 429:
                return (f"Rate limited by Discord (two username changes per hour). "
                        f"NOT retried automatically: {text}")
            if resp.status == 400:
                return (f"Discord rejected the name {name!r} (it may be taken, "
                        f"reserved, or contain disallowed characters): {text}")
            return f"Rename failed: HTTP {resp.status}: {text}"


@tool(
    name="discord_send_dm",
    description=(
        "Send a direct message to a user, opening the DM channel first. Use "
        "when there is no shared channel to post in. Reports a closed-DM "
        "privacy setting distinctly from a real failure."
    ),
    category="discord",
)
async def discord_send_dm(
    ctx: ToolContext, user_id: str, content: str, bot: str = ""
) -> str:
    """Open a DM channel and post to it.

    Args:
        user_id: The Discord user ID to message.
        content: The message text, at most 2000 characters.
        bot: Which bot account sends it (e.g. 'milo'). Empty for default.
    """
    if not str(user_id).strip():
        return "Not sent: user_id is required."
    if not (content or "").strip():
        return "Not sent: content is empty."
    if len(content) > 2000:
        return f"Not sent: Discord messages cap at 2000 characters, this is {len(content)}."

    headers = _discord_headers(ctx.vault, bot=bot)
    if not headers:
        return f"No Discord token found for bot '{bot or 'main'}' in the vault."

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{DISCORD_API}/users/@me/channels", headers=headers,
            json={"recipient_id": str(user_id).strip()},
        ) as resp:
            if resp.status not in (200, 201):
                text = (await resp.text())[:300]
                # A closed DM is a choice the user made, not a fault to debug.
                if resp.status == 403:
                    return (f"Could not open a DM with {user_id}: they do not accept "
                            f"direct messages from this bot (privacy setting or no "
                            f"shared server). Nothing was sent.")
                return f"Could not open a DM channel with {user_id}: HTTP {resp.status}: {text}"
            channel_id = str((await resp.json()).get("id", ""))

        if not channel_id:
            return f"Discord returned no DM channel id for {user_id}. Nothing was sent."

        async with session.post(
            f"{DISCORD_API}/channels/{channel_id}/messages",
            headers=headers, json={"content": content},
        ) as resp:
            if resp.status in (200, 201):
                return f"DM sent to {user_id} (channel {channel_id})."
            text = (await resp.text())[:300]
            if resp.status == 403:
                return (f"DM channel {channel_id} opened, but posting was refused: "
                        f"{user_id} has DMs closed to this bot.")
            return f"DM channel opened but the message failed: HTTP {resp.status}: {text}"


@tool(
    name="discord_delete_message",
    description=(
        "Delete a message the bot itself sent (works in DMs, where no human "
        "can delete a bot's message, and in servers). Own messages only — "
        "refuses to delete anyone else's; use Discord moderation for those."
    ),
    category="discord",
)
async def discord_delete_message(
    ctx: ToolContext, channel_id: str, message_id: str, bot: str = ""
) -> str:
    """Delete one of the bot's own messages.

    Args:
        channel_id: The channel (or DM channel) ID containing the message.
        message_id: The message ID to delete.
        bot: Which bot account sent it (e.g. 'milo'). Empty for default.
    """
    if not ctx.vault:
        return "Error: no vault access."

    me = await _discord_get(ctx.vault, "/users/@me", bot=bot)
    if not me:
        return f"Error: could not resolve bot identity for '{bot or 'default'}'."

    msg = await _discord_get(
        ctx.vault, f"/channels/{channel_id}/messages/{message_id}", bot=bot)
    if not msg:
        return (f"Error: could not fetch message {message_id} from channel "
                f"{channel_id}. Check the IDs and that this bot can see it.")

    author = msg.get("author", {})
    if author.get("id") != me.get("id"):
        return (f"Refused: message {message_id} was sent by "
                f"'{author.get('username', 'unknown')}', not this bot. This "
                "tool only deletes the bot's own messages.")

    result = await _discord_delete(
        ctx.vault, f"/channels/{channel_id}/messages/{message_id}", bot=bot)
    if result is None:
        return "Error: no Discord token available."
    if result.get("error"):
        return f"Error deleting message: {result['detail']}"
    return f"Deleted message {message_id} from channel {channel_id}."
