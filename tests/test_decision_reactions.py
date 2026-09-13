"""One tap to decide, without making a bot's seed into human consent."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import quote

import aiohttp
import discord
import pytest

from src.core.decision_reactions import decision_reactions

PROMPT = "React ✅ to approve or 🔴 to reject."


@pytest.mark.parametrize("text", [
    PROMPT,
    "Please react ✅ / 🔴 for this clip.",
    "**GO / NO-GO:** publish this draft?",
    "Go/no go?",
    "Reagera med ✅ eller 🔴 för klippet.",
    "Godkänn med ✅ eller markera 🔴 för nej.",
    "<@123> Tryck ✅ / 🔴.",
    "- ✅ Approve\n- 🔴 Reject",
    "✅ = GO | 🔴 = NO-GO",
    "**✅ Godkänn**\n**🔴 Avslå**",
    "The draft is attached.\n\n" + PROMPT,
])
def test_explicit_prompts_get_clickable_decisions(text):
    assert decision_reactions(text) == ("✅", "🔴")


@pytest.mark.parametrize("text, expected", [
    ("React ✅ to approve or ❌ to deny.", ("✅", "❌")),
    ("Tap 🟢 for go or 🔴 for no-go.", ("🟢", "🔴")),
])
def test_preserves_the_authors_explicit_pair(text, expected):
    assert decision_reactions(text) == expected


@pytest.mark.parametrize("text", [
    "✅ Build passed. 🔴 One test failed.",
    "The owner approved with ✅ and can reject with 🔴.",
    "We will pre-add ✅ and 🔴 to approval prompts.",
    "Go/no-go checks passed yesterday.",
    "DECISION: Pick a colour.\nA: red\nB: blue",
    "````markdown\n```\n" + PROMPT + "\n```\n````",
    "~~~\n" + PROMPT + "\n~~~",
    "```\n" + PROMPT,
    "    " + PROMPT,
    "> " + PROMPT,
    '"' + PROMPT + '"',
    "Example: `" + PROMPT + "`",
    "📨 **one agent → another:**\n" + PROMPT,
    "React ✅ to approve.",
    "React ✅, 🔴 or ❌.",
    PROMPT + "\nReact 🟢 or 🔴 for the second proposal.",
    "✅ Approve\n\n🔴 Reject",  # unrelated paragraphs are not a paired legend
])
def test_status_quotes_examples_and_ambiguous_requests_have_no_controls(text):
    assert decision_reactions(text) == ()


@pytest.fixture
def connector(tmp_path, monkeypatch):
    from src.connectors.discord import DiscordConnector

    conn = DiscordConnector(config={})
    conn.set_setup_context(
        {"defaults": {"reply": {"shorten": {"enabled": True, "threshold_chars": 300}}}},
        str(tmp_path),
    )
    messages = []

    async def send(content, files=None):
        msg = SimpleNamespace(id=100 + len(messages), content=content, files=files,
                              add_reaction=AsyncMock())
        messages.append(msg)
        return msg

    channel = SimpleNamespace(send=send)
    bot = SimpleNamespace(account_name="artist", client=SimpleNamespace(get_channel=lambda _: channel))
    monkeypatch.setattr(conn, "_get_bot", lambda _=None: bot)
    monkeypatch.setattr(conn, "_linkify_mentions", AsyncMock(side_effect=lambda content, _: content))
    return conn, messages


async def test_normal_reply_seeds_the_exact_returned_message(connector):
    conn, messages = connector
    sent = await conn.send("555", PROMPT)
    assert sent is messages[0]
    assert sent.content == PROMPT
    assert [call.args[0] for call in sent.add_reaction.await_args_list] == ["✅", "🔴"]


async def test_approval_prompt_can_still_have_the_expand_shortcut(connector):
    conn, messages = connector
    text = PROMPT + "\n\n" + "This draft is ready for review. " * 5 + "\n\n" + "Background detail. " * 50
    sent = await conn.send("555", text)
    assert len(messages) == 1
    assert PROMPT in sent.content
    assert "shortened" in sent.content
    assert [call.args[0] for call in sent.add_reaction.await_args_list] == ["🔍", "✅", "🔴"]


async def test_approval_request_is_never_hidden_in_the_remainder(connector):
    conn, messages = connector
    text = "Background. " * 20 + "\n\n" + "Evidence. " * 30 + "\n\n" + PROMPT
    sent = await conn.send("555", text)
    assert PROMPT in "\n".join(msg.content for msg in messages)
    assert "shortened" not in sent.content
    assert conn._shortener.store.take(str(sent.id)) is None
    assert sent.add_reaction.await_count == 2


async def test_remainder_is_available_while_decision_shortcuts_are_being_added(connector, monkeypatch):
    conn, _ = connector
    original = conn._get_bot().client.get_channel(555).send
    expanded = []

    async def send(*args, **kwargs):
        msg = await original(*args, **kwargs)

        async def add_reaction(emoji):
            if emoji == "✅":
                # The user can ask for "more" while a reaction API call is
                # pending. The already-visible footer must work at that point.
                expanded.append(conn._shortener.store.take(str(msg.id)))

        msg.add_reaction.side_effect = add_reaction
        return msg

    monkeypatch.setattr(conn._get_bot().client, "get_channel", lambda _: SimpleNamespace(send=send))
    text = PROMPT + "\n\n" + "The draft is ready. " * 9 + "\n\n" + "Supporting detail. " * 50
    await conn.send("555", text)
    assert len(expanded) == 1 and expanded[0] is not None
    assert "Supporting detail." in expanded[0].rest


async def test_split_delivery_keeps_controls_on_the_first_message_and_attachment(connector, tmp_path):
    conn, messages = connector
    path = tmp_path / "preview.txt"
    path.write_text("draft")
    sent = await conn.send("555", PROMPT + "\n\n" + "detail\n" * 600, files=[str(path)])
    assert len(messages) > 1
    assert all(len(msg.content) <= 2000 for msg in messages)
    assert sent.files[0].filename == "preview.txt"
    assert all(msg.files is None for msg in messages[1:])
    assert sent.add_reaction.await_count == 2
    assert all(msg.add_reaction.await_count == 0 for msg in messages[1:])
    sent.files[0].close()


async def test_reaction_error_does_not_fail_send_or_skip_other_choice(connector, monkeypatch):
    conn, messages = connector
    original = conn._get_bot().client.get_channel(555).send

    async def send(*args, **kwargs):
        msg = await original(*args, **kwargs)
        response = SimpleNamespace(status=403, reason="Forbidden")
        msg.add_reaction.side_effect = [discord.Forbidden(response, "Missing permissions"), None]
        return msg

    monkeypatch.setattr(conn._get_bot().client, "get_channel", lambda _: SimpleNamespace(send=send))
    sent = await conn.send("555", PROMPT)
    assert sent is messages[0]
    assert sent.add_reaction.await_count == 2
    assert await conn.send("555", PROMPT) is None  # existing duplicate protection
    assert len(messages) == 1


async def test_bot_seeds_are_ignored_before_any_approval_or_wake_handler():
    from src.connectors.discord import DiscordBot

    bot = DiscordBot.__new__(DiscordBot)
    bot.client = SimpleNamespace(user=SimpleNamespace(id=111))
    bot._wake_on_reaction = AsyncMock()
    # Deliberately no HITL/goal handlers: reaching one would fail this test.
    for emoji in ("✅", "🔴", "❌"):
        await bot.on_raw_reaction_add(SimpleNamespace(user_id=111, emoji=emoji))
    bot._wake_on_reaction.assert_not_awaited()


async def test_formal_hitl_card_seeds_once_after_its_id_is_registered(connector, monkeypatch):
    import aiosqlite

    from src.core.hitl import HITLGate

    conn, messages = connector
    async with aiosqlite.connect(":memory:") as db:
        gate = HITLGate({"channel": "555", "timeout": 0, "approvers": ["999"]}, db, connector=conn)
        await gate.init_schema()
        original = conn._get_bot().client.get_channel(555).send
        registered = []

        async def send(*args, **kwargs):
            msg = await original(*args, **kwargs)

            async def add_reaction(_emoji):
                async with db.execute("SELECT message_id FROM hitl_pending") as cursor:
                    registered.append((await cursor.fetchone())[0])

            msg.add_reaction.side_effect = add_reaction
            return msg

        monkeypatch.setattr(conn._get_bot().client, "get_channel", lambda _: SimpleNamespace(send=send))
        result = await gate.request_approval("artist", "send_email", {}, "Send the draft.")
        assert result["status"] == "timeout"  # bot-added ✅ is not consent
        assert [call.args[0] for call in messages[0].add_reaction.await_args_list] == ["✅", "❌"]
        assert registered == [str(messages[0].id)] * 2


class Response:
    def __init__(self, status=200, payload=None):
        self.status = status
        self.payload = payload

    async def json(self):
        return self.payload

    async def text(self):
        return "Missing Access"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


@pytest.fixture
def file_upload(tmp_path, monkeypatch):
    from src.tools import discord_tools

    class Session(Response):
        def __init__(self):
            self.upload_response = Response(payload={"id": "700"})
            self.put_results = [204, 204]
            self.posts = []
            self.puts = []

        def post(self, url, **kwargs):
            self.posts.append((url, kwargs))
            return self.upload_response

        def put(self, url, **kwargs):
            self.puts.append((url, kwargs))
            result = self.put_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return Response(status=result)

    session = Session()
    auth_calls = []

    def resolve(vault, **kwargs):
        auth_calls.append(kwargs)
        return SimpleNamespace(token="test-token", account="artist", error="")

    monkeypatch.setattr(discord_tools, "resolve_bot_token", resolve)
    monkeypatch.setattr(discord_tools.aiohttp, "ClientSession", lambda: session)
    monkeypatch.setattr("src.tools.ingest.validate_file_path", lambda _: None)
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"test clip")
    ctx = SimpleNamespace(vault=None, agent_id="artist")

    async def upload(message=PROMPT):
        return await discord_tools.send_discord_file(ctx, "555", str(path), message=message)

    return session, auth_calls, upload


async def test_attachment_shortcuts_use_created_id_and_exact_upload_identity(file_upload):
    session, auth_calls, upload = file_upload
    result = await upload()
    assert "sent" in result and "message id 700" in result
    assert len(session.posts) == 1
    assert auth_calls == [{"bot": "", "agent_id": "artist"}]
    assert [url for url, _ in session.puts] == [
        f"https://discord.com/api/v10/channels/555/messages/700/reactions/{quote(emoji, safe='')}/@me"
        for emoji in ("✅", "🔴")
    ]
    assert all(kwargs["headers"] is session.posts[0][1]["headers"] for _, kwargs in session.puts)
    assert all(kwargs["timeout"].total == 5 for _, kwargs in session.puts)


@pytest.mark.parametrize("failure", [403, 404, 429, aiohttp.ClientConnectionError(), TimeoutError()])
async def test_shortcut_failure_reports_sent_file_and_never_reuploads(file_upload, failure):
    session, _, upload = file_upload
    session.put_results = [failure, 204]
    result = await upload()
    assert "File clip.mp4 sent" in result and "message id 700" in result
    assert "do not upload it again" in result
    assert len(session.posts) == 1
    assert len(session.puts) == 2


async def test_missing_upload_id_never_guesses_a_message(file_upload):
    session, _, upload = file_upload
    session.upload_response.payload = {}
    result = await upload()
    assert "File clip.mp4 sent" in result and "shortcuts unavailable" in result
    assert not session.puts


async def test_failed_upload_never_seeds_a_reaction(file_upload):
    session, _, upload = file_upload
    session.upload_response.status = 403
    result = await upload()
    assert "Failed to send file (HTTP 403)" in result
    assert not session.puts


async def test_ordinary_file_delivery_stays_unchanged(file_upload):
    session, _, upload = file_upload
    result = await upload("The requested file.")
    assert result == "File clip.mp4 sent to channel 555 (message id 700)"
    assert not session.puts


@pytest.fixture
def text_send(file_upload, monkeypatch):
    from src.core.base import ToolContext
    from src.tools.builtin import send_message

    session, _, _ = file_upload
    auth_calls = []

    def resolve(vault, **kwargs):
        auth_calls.append(kwargs)
        return SimpleNamespace(token="text-token", account="artist", error="")

    monkeypatch.setattr("src.lib.discord_auth.resolve_bot_token", resolve)

    async def send(content=PROMPT, bot=""):
        ctx = ToolContext(agent_id="artist", vault=object())
        return await send_message(ctx, "555", content, bot=bot)

    return session, auth_calls, send


@pytest.mark.parametrize("bot", ["", "artist"])
async def test_mcp_text_sender_seeds_the_created_message_with_the_sending_identity(text_send, bot):
    session, auth_calls, send = text_send
    result = await send(bot=bot)
    assert "Message sent to 555 (message id 700)" == result
    assert auth_calls == [{"bot": bot, "agent_id": "artist"}]
    assert len(session.posts) == 1 and len(session.puts) == 2
    assert all("/messages/700/reactions/" in url for url, _ in session.puts)
    assert all(kwargs["headers"] is session.posts[0][1]["headers"] for _, kwargs in session.puts)


async def test_mcp_split_text_uses_first_message_id_not_last(text_send):
    session, _, send = text_send

    def post(url, **kwargs):
        session.posts.append((url, kwargs))
        return Response(payload={"id": str(699 + len(session.posts))})

    session.post = post
    result = await send(PROMPT + "\n" + "detail " * 500)
    assert len(session.posts) == 2
    assert "message id 700" in result
    assert all("/messages/700/reactions/" in url for url, _ in session.puts)


@pytest.mark.parametrize("failure", [403, 404, 429, aiohttp.ClientConnectionError(), TimeoutError()])
async def test_mcp_text_shortcut_failure_does_not_fail_or_repeat_the_message(text_send, failure):
    session, _, send = text_send
    session.put_results = [failure, 204]
    result = await send()
    assert "Message sent to 555" in result and "do not resend it" in result
    assert len(session.posts) == 1 and len(session.puts) == 2


async def test_mcp_text_missing_id_cannot_seed_some_other_message(text_send):
    session, _, send = text_send
    session.upload_response.payload = {}
    result = await send()
    assert "Message sent" in result and "shortcuts unavailable" in result
    assert not session.puts


async def test_mcp_failed_text_send_does_not_add_reactions(text_send):
    session, _, send = text_send
    session.upload_response.status = 403
    assert "Discord API error (403)" in await send()
    assert not session.puts


async def test_mcp_status_text_has_no_shortcuts(text_send):
    session, _, send = text_send
    assert await send("✅ Build passed. 🔴 One test failed.") == "Message sent to 555"
    assert not session.puts


async def test_text_tool_with_connector_does_not_seed_twice(connector):
    from src.core.base import ToolContext
    from src.tools.builtin import send_message

    conn, messages = connector
    ctx = ToolContext(agent_id="artist", connector_send=conn.send)
    assert await send_message(ctx, "555", PROMPT) == "Message sent to 555"
    assert len(messages) == 1
    assert [call.args[0] for call in messages[0].add_reaction.await_args_list] == ["✅", "🔴"]
