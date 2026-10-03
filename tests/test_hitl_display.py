"""Existing approval paths, real pending stores, and bounded Discord previews."""

import asyncio
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiosqlite
import pytest

from src.core.hitl import HITLGate
from src.core.hitl_display import MCP_PENDING_SCHEMA, email_approval_card, pending_for
from src.core.morning_digest import SOURCES, jump, render
from src.mcp_server import MCPHitlGate

OWNER = "1000000000000000001"
CHANNEL = "1000000000000000002"
MESSAGE = "1000000000000000003"
GUILD = "1000000000000000004"


def credential_examples():
    opaque = "Ab3dEf7hIj9LmNpQr2StUvWxYz5_8+Z/="
    return [
        pytest.param("sk_live_" + "A" * 32, "A" * 16, id="stripe"),
        pytest.param("AWS_SECRET_ACCESS_KEY=" + opaque, opaque, id="aws-secret"),
        pytest.param("password: fixture-password", "fixture-password", id="password"),
        pytest.param("postgres://sample:fixture-password@example.com/db", "fixture-password", id="database-uri"),
        pytest.param("https://discord.com/api/webhooks/123/" + opaque, opaque, id="discord-webhook"),
        pytest.param("https://hooks.slack.com/services/T/B/" + opaque, opaque, id="slack-webhook"),
        pytest.param("SG." + "B" * 22 + "." + "C" * 43, "C" * 16, id="sendgrid"),
        pytest.param("a1b2c3d4" * 8, "a1b2c3d4" * 4, id="hex-recovery"),
        pytest.param("Your new key is ready to use: " + opaque, opaque, id="opaque-near-key"),
        pytest.param(opaque + " is the new secret.", opaque, id="opaque-before-secret"),
    ]


@pytest.mark.parametrize("value,secret", credential_examples())
@pytest.mark.parametrize("field", ["body", "subject", "to"])
def test_review_email_credential_shapes_are_not_posted(value, secret, field):
    args = {"to": "someone@example.com", "subject": "Review", "body": "Safe text", field: value}
    original = args.copy()
    card = email_approval_card("sample", "id", args)
    assert secret not in card
    assert "[REDACTED]" in card
    assert args == original


@pytest.mark.parametrize("invisible", ["\u200b", "\u00ad", "\u200d"], ids=["zero-width-space", "soft-hyphen", "joiner"])
@pytest.mark.parametrize("field", ["body", "subject", "to"])
def test_review_invisible_character_cannot_reveal_credential(invisible, field):
    value = "ghp_" + invisible + "A" * 30
    card = email_approval_card("sample", "id", {field: value})
    assert "A" * 16 not in card
    assert invisible not in card
    assert "[REDACTED]" in card


@pytest.mark.parametrize("terminated", [False, True])
def test_review_private_key_redacted_before_preview_window(terminated):
    material = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo="
    body = "-----BEGIN " + "RSA PRIVATE KEY-----\n" + (material + "\n") * 40
    if terminated:
        body += "-----END " + "RSA PRIVATE KEY-----\nSafe trailing text."
    card = email_approval_card("sample", "id", {"body": body})
    assert material not in card
    assert "PRIVATE KEY" not in card
    assert "[REDACTED]" in card
    if terminated:
        assert "Safe trailing text." in card


@pytest.mark.parametrize(
    "value,secret",
    [
        ("SG.x.y", "SG.x.y"),
        ("pwd:x", "pwd:x"),
        ('password: "one two three"', "one two three"),
        ('password="one\\"two"', "two"),
        ("password: 'unterminated value", "unterminated"),
        ("The key uses postgres://" + "LongFixtureUsername" * 3 + ":abc@example.com/db", "abc@example"),
        ("HTTPS://PTB.DISCORD.COM/api/v10/webhooks/123/" + "A" * 32, "A" * 16),
        ("https://hooks.slack-gov.com/services/T/B/" + "C" * 32, "C" * 16),
        ("rk_test_" + "D" * 32, "D" * 16),
    ],
)
def test_conservative_credential_variants_and_rule_order(value, secret):
    card = email_approval_card("sample", "id", {"body": value})
    assert secret not in card and "[REDACTED]" in card


@pytest.mark.parametrize("label", ["key", "token", "secret", "password"])
@pytest.mark.parametrize("value", ["Ab3dEf7hIj9LmNpQr2StUvWxYz5_8+Z/=", "A!b@C#d$E%f^G&h*I(j)K{l}M[n]O:p;"])
def test_context_fallback_without_assignment(label, value):
    card = email_approval_card("sample", "id", {"body": f"The {label} you asked about:\n{value}"})
    assert value not in card and "[REDACTED]" in card


def test_conservative_mode_keeps_benign_prose_and_default_audit_hashes():
    from src.core.audit import redact_secrets

    text = "The key point is that every token counts. Keep the password manager updated."
    assert text in email_approval_card("sample", "id", {"body": text})
    digest = "a1b2c3d4" * 8
    assert redact_secrets({"body": digest}) == {"body": digest}
    assert redact_secrets({"body": digest}, conservative=True) == {"body": "[REDACTED]"}


def test_conservative_redaction_is_recursive_and_idempotent():
    from src.core.audit import redact_secrets

    secret = "sk_live_" + "A" * 32
    data = {"payload": [{"body": secret}], "safe": "Ordinary prose."}
    expected = {"payload": [{"body": "[REDACTED]"}], "safe": "Ordinary prose."}
    assert redact_secrets(data, conservative=True) == expected
    assert redact_secrets(expected, conservative=True) == expected
    assert data["payload"][0]["body"] == secret


@pytest.mark.parametrize("path", ["engine", "mcp"])
async def test_gate_transports_only_receive_redacted_email_card(gates, monkeypatch, path):
    engine, mcp, connector = gates
    transport = Transport()
    transport.verdict = "denied"
    monkeypatch.setattr("src.mcp_server.aiohttp.ClientSession", lambda: transport)
    secret = "sk_live_" + "A" * 32
    body = "password: a-fixture-secret\nghp_\u200b" + "B" * 30 + "\n" + secret
    args = {"to": "someone@example.com", "subject": "Review", "body": body}
    original = args.copy()
    if path == "mcp":
        result = await mcp.request_approval("send_email", args)
        card = transport.posts[0]["content"]
    else:
        task = asyncio.create_task(engine.request_approval("sample", "send_email", args, "Send email"))
        try:
            await wait_pending(engine)
            for _ in range(100):
                if connector.send.await_count:
                    break
                await asyncio.sleep(0.001)
            card = connector.send.call_args.args[1]
            async with engine.db.execute("SELECT hitl_id FROM hitl_pending") as cursor:
                hitl_id = (await cursor.fetchone())[0]
            await engine.deny(hitl_id, OWNER)
            result = await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    assert result["status"] == "denied" and args == original
    assert secret not in card and "a-fixture-secret" not in card and "B" * 16 not in card
    assert card.count("```") == 2
    assert "React ✅ to approve or ❌ to deny" in card


@pytest.fixture
async def gates(tmp_path, monkeypatch):
    path = tmp_path / "engine.db"
    db = await aiosqlite.connect(path)
    sync = sqlite3.connect(path)
    cfg = {"channel": CHANNEL, "approvers": [OWNER], "timeout": 2, "poll_interval": 0.001}
    connector = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=MESSAGE, add_reaction=AsyncMock())))
    engine = HITLGate(cfg, db, connector=connector)
    await engine.init_schema()
    vault = SimpleNamespace(get=lambda key: "fixture-only")
    mcp = MCPHitlGate(cfg, vault)
    mcp.pending_db = sync
    mcp._get_notifier = AsyncMock(return_value=None)
    monkeypatch.setattr("src.mcp_server.MCP_AGENT_ID", "sample")
    yield engine, mcp, connector
    await db.close()
    mcp.pending_db.close()


class Response:
    def __init__(self, data=None, status=200):
        self.data, self.status = data, status

    async def json(self):
        return self.data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def __await__(self):
        async def result():
            return self

        return result().__await__()


class Transport:
    def __init__(self):
        self.posts = []
        self.verdict = None
        self.post_status = 200
        self.error = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def post(self, url, *, json, **kwargs):
        self.posts.append(json)
        return Response({"id": MESSAGE}, self.post_status)

    async def put(self, *args, **kwargs):
        return Response(status=204)

    def get(self, url, **kwargs):
        if self.error:
            raise OSError("fixture transport failed")
        yes = "%E2%9C%85" in url
        matching = self.verdict == ("approved" if yes else "denied")
        return Response([{"id": OWNER, "bot": False}] if matching else [])


async def wait_pending(engine):
    for _ in range(100):
        rows = await pending_for(engine, OWNER, time.time())
        if rows:
            return rows
        await asyncio.sleep(0.005)
    pytest.fail("No pending approval was visible")


@pytest.mark.parametrize("path", ["engine", "mcp"])
@pytest.mark.parametrize("verdict", ["approved", "denied"])
async def test_existing_gate_posts_email_preview_and_requires_one_decision(gates, monkeypatch, path, verdict):
    engine, mcp, connector = gates
    transport = Transport()
    monkeypatch.setattr("src.mcp_server.aiohttp.ClientSession", lambda: transport)
    args = {"to": "someone@example.com", "subject": "Reviewed draft", "body": "Hello, here is the exact draft."}
    if path == "engine":
        task = asyncio.create_task(engine.request_approval("sample", "send_email", args, "Legacy summary"))
    else:
        task = asyncio.create_task(mcp.request_approval("send_email", args))
    try:
        rows = await wait_pending(engine)
        assert len(rows) == 1 and rows[0]["tool_name"] == "send_email"
        assert not task.done()
        if path == "engine":
            # Wait for the card's identity, not just the pre-send intent row.
            for _ in range(100):
                if connector.send.await_count:
                    break
                await asyncio.sleep(0.001)
            text = connector.send.call_args.args[1]
            async with engine.db.execute("SELECT hitl_id FROM hitl_pending") as cursor:
                hitl_id = (await cursor.fetchone())[0]
            assert not await engine.approve(hitl_id, "1000000000000000009")
            assert await (engine.approve if verdict == "approved" else engine.deny)(hitl_id, OWNER)
        else:
            text = transport.posts[0]["content"]
            # Metadata stores no email body, recipient, attachment path or approval.
            row = mcp.pending_db.execute("SELECT * FROM hitl_mcp_pending").fetchone()
            assert args["body"] not in str(row) and args["to"] not in str(row)
            transport.verdict = verdict
        assert args["body"] in text and args["subject"] in text and args["to"] in text
        assert "React ✅ to approve or ❌ to deny" in text
        result = await asyncio.wait_for(task, 1)
        assert result["status"] == verdict
        assert await pending_for(engine, OWNER, time.time()) == []
        if path == "engine":
            connector.send.assert_awaited_once()
        else:
            assert sum("HITL Approval Required" in p["content"] for p in transport.posts) == 1
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("end", ["timeout", "cancel", "error"])
async def test_mcp_metadata_clears_on_every_terminal_path(gates, monkeypatch, end):
    engine, mcp, _ = gates
    transport = Transport()
    monkeypatch.setattr("src.mcp_server.aiohttp.ClientSession", lambda: transport)
    mcp.timeout = 0.15
    task = asyncio.create_task(mcp.request_approval("send_email", {"body": "A draft"}))
    await wait_pending(engine)
    if end == "cancel":
        task.cancel()
    elif end == "error":
        transport.error = True
    result = await asyncio.gather(task, return_exceptions=True)
    if end == "timeout":
        assert result[0]["status"] == "timeout"
    elif end == "error":
        assert result[0]["status"] == "denied"
    else:
        assert isinstance(result[0], asyncio.CancelledError)
    assert mcp.pending_db.execute("SELECT count(*) FROM hitl_mcp_pending").fetchone()[0] == 0


async def test_mcp_metadata_reopens_expires_and_cannot_grant_approval(gates, tmp_path):
    engine, mcp, _ = gates
    mcp._record_pending("a", "send_email", MESSAGE)
    rows = await pending_for(engine, OWNER, time.time())
    assert len(rows) == 1
    # An engine reaction cannot resolve an MCP request through the visibility row.
    await engine.approve("a", OWNER)
    async with engine.db.execute("SELECT count(*) FROM hitl_pending") as cursor:
        assert (await cursor.fetchone())[0] == 0
    assert len(await pending_for(engine, OWNER, time.time())) == 1
    assert await pending_for(engine, OWNER, time.time() + mcp.timeout + 1) == []
    mcp.pending_db.close()
    mcp.pending_db = sqlite3.connect(tmp_path / "engine.db")
    assert len(await pending_for(engine, OWNER, time.time())) == 1


async def test_mcp_failed_post_creates_no_pending_metadata(gates, monkeypatch):
    engine, mcp, _ = gates
    transport = Transport()
    transport.post_status = 403
    monkeypatch.setattr("src.mcp_server.aiohttp.ClientSession", lambda: transport)
    result = await mcp.request_approval("send_email", {"body": "A draft"})
    assert result["status"] == "denied"
    assert await pending_for(engine, OWNER, time.time()) == []


def test_preview_is_bounded_redacted_and_literal_without_reading_attachments(tmp_path, monkeypatch):
    from pathlib import Path

    secret = "ghp_" + "A" * 30
    body = "First line\n" + secret + "\n```\n@everyone\n" + "😀" * 2000 + "HIDDEN END"
    attachment = tmp_path / "do-not-read.txt"
    attachment.write_text("PRIVATE FILE CONTENTS")
    monkeypatch.setattr(Path, "read_bytes", lambda *a: pytest.fail("Read attachment"))
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: pytest.fail("Read attachment"))
    text = email_approval_card(
        "😀" * 100, "id", {"to": "😀" * 500, "subject": "😀" * 500, "body": body, "attachments": str(attachment)}
    )
    assert len(text.encode("utf-16-le")) // 2 <= 2000
    assert "First line\n[REDACTED]" in text
    assert secret not in text and "@everyone" not in text
    assert text.count("```") == 2
    assert "HIDDEN END" not in text and "…" in text
    assert "Attachments: present; contents not shown" in text
    assert "PRIVATE FILE CONTENTS" not in text and str(attachment) not in text
    assert text.endswith("deny and ask the agent for a revised email.")


def test_missing_email_body_and_attachment_are_explicit():
    text = email_approval_card("sample", "id", {})
    assert "(empty body)" in text and "Attachments: none" in text


def test_preview_line_limit_is_real():
    text = email_approval_card("sample", "id", {"body": "\n".join(f"line {n}" for n in range(1000))})
    preview = text.split("Body preview (redacted, shortened if needed):\n", 1)[1].split("\n```", 1)[0]
    assert len(preview.splitlines()) == 12
    assert preview.endswith("line 10\n…") and "line 11" not in preview


async def test_real_connector_keeps_preview_and_controls_on_one_card(gates):
    from src.connectors.discord import DiscordConnector

    engine, _, _ = gates
    message = SimpleNamespace(id=MESSAGE, add_reaction=AsyncMock())
    channel = SimpleNamespace(send=AsyncMock(return_value=message), guild=None)
    connector = DiscordConnector(config={})
    connector.bots = {"sample": SimpleNamespace(client=SimpleNamespace(get_channel=lambda cid: channel))}
    connector._shortener = SimpleNamespace(shorten=Mock(side_effect=AssertionError("Approval card was shortened")))
    engine.connector = connector
    body = "Paragraph of the email. " * 100
    task = asyncio.create_task(engine.request_approval("sample", "send_email", {"body": body}, "Send email"))
    try:
        for _ in range(100):
            if channel.send.await_count:
                break
            await asyncio.sleep(0.005)
        channel.send.assert_awaited_once()
        text = channel.send.call_args.args[0]
        assert "Paragraph of the email." in text
        assert "React ✅ to approve or ❌ to deny" in text
        connector._shortener.shorten.assert_not_called()
        async with engine.db.execute("SELECT hitl_id FROM hitl_pending") as cursor:
            hitl_id = (await cursor.fetchone())[0]
        await engine.approve(hitl_id, OWNER)
        assert (await task)["status"] == "approved"
        assert [call.args[0] for call in message.add_reaction.await_args_list] == ["✅", "❌"]
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_metadata_failure_releases_write_lock_and_does_not_approve(gates):
    engine, mcp, _ = gates
    mcp._record_pending("old", "send_email", MESSAGE, expires_at=time.time() - 1)
    mcp.pending_db.executescript("""
        CREATE TRIGGER refuse_insert BEFORE INSERT ON hitl_mcp_pending
        BEGIN SELECT RAISE(ABORT, 'fixture'); END;
    """)
    mcp._record_pending("new", "send_email", MESSAGE)
    assert not mcp.pending_db.in_transaction
    assert mcp.pending_db.execute("SELECT hitl_id FROM hitl_mcp_pending").fetchone()[0] == "old"
    assert await pending_for(engine, OWNER, time.time()) == []
    mcp.pending_db.execute("DROP TRIGGER refuse_insert")
    mcp._record_pending("new", "send_email", MESSAGE)
    assert len(await pending_for(engine, OWNER, time.time())) == 1
    mcp.pending_db.executescript("""
        CREATE TRIGGER refuse_delete BEFORE DELETE ON hitl_mcp_pending
        BEGIN SELECT RAISE(ABORT, 'fixture'); END;
    """)
    mcp._clear_pending("new")
    assert not mcp.pending_db.in_transaction
    assert await pending_for(engine, OWNER, time.time() + mcp.timeout + 1) == []


def test_metadata_schema_is_additive_and_idempotent(tmp_path):
    with sqlite3.connect(tmp_path / "db") as db:
        db.execute("CREATE TABLE existing (value TEXT)")
        db.execute("INSERT INTO existing VALUES ('unchanged')")
        db.executescript(MCP_PENDING_SCHEMA)
        db.executescript(MCP_PENDING_SCHEMA)
        assert db.execute("SELECT value FROM existing").fetchone()[0] == "unchanged"


def test_digest_has_bounded_lines_counts_and_tappable_links():
    url = jump("9" * 20, "8" * 20, "7" * 20)
    rows = [{"text": "``` @everyone " + "😀" * 1000, "created_at": 1, "url": url} for _ in range(100)]
    text = render(dict.fromkeys(SOURCES, rows), 90000)
    assert len(text.encode("utf-16-le")) // 2 <= 2000
    assert text.count("+ 97 more") == 3
    assert text.count("https://discord.com/channels/") == 9
    assert text.count("```") == 2 and "@everyone" not in text
    assert "https://" not in text.split("```")[1]
    assert max(len(line.encode("utf-16-le")) // 2 for line in text.split("```")[1].splitlines()) <= 68


@pytest.mark.parametrize(
    "guild,channel,message",
    [
        ("bad/path", CHANNEL, MESSAGE),
        (GUILD, "@everyone", MESSAGE),
        (GUILD, CHANNEL, "１２３"),
        (GUILD, CHANNEL, "9" * 21),
    ],
)
def test_digest_rejects_untrusted_link_components(guild, channel, message):
    assert jump(guild, channel, message) == ""
