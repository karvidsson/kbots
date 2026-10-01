"""Email draft-before-send approval workflow tests."""

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.core.base import ToolContext
from src.core.email_drafts import EmailDraftStore, build_email_context, truncate_preview


@pytest.fixture
def draft_store(tmp_path):
    store = EmailDraftStore(tmp_path / "drafts.db")
    yield store
    store.close()


class TestEmailDraftStore:
    def test_create_and_get(self, draft_store):
        draft = draft_store.create(
            agent_id="test-agent",
            account="default",
            recipient="user@example.com",
            subject="Test Subject",
            body="Test body content",
        )
        assert draft["draft_id"]
        assert draft["status"] == "pending"
        assert draft["recipient"] == "user@example.com"
        assert draft["subject"] == "Test Subject"

        fetched = draft_store.get(draft["draft_id"])
        assert fetched["draft_id"] == draft["draft_id"]

    def test_link_ask_and_get_by_ask(self, draft_store):
        draft = draft_store.create(
            agent_id="test-agent",
            account="default",
            recipient="user@example.com",
            subject="Test",
            body="Body",
        )
        draft_store.link_ask(draft["draft_id"], "ask-123")

        by_ask = draft_store.get_by_ask("ask-123")
        assert by_ask["draft_id"] == draft["draft_id"]

    def test_resolve(self, draft_store):
        draft = draft_store.create(
            agent_id="test-agent",
            account="default",
            recipient="user@example.com",
            subject="Test",
            body="Body",
        )
        draft_store.resolve(draft["draft_id"], "sent", "Email sent successfully")

        resolved = draft_store.get(draft["draft_id"])
        assert resolved["status"] == "sent"
        assert resolved["result"] == "Email sent successfully"
        assert resolved["resolved_at"] is not None

    def test_pending_filter(self, draft_store):
        d1 = draft_store.create(
            agent_id="agent-1",
            account="default",
            recipient="a@b.c",
            subject="First",
            body="Body",
        )
        d2 = draft_store.create(
            agent_id="agent-2",
            account="default",
            recipient="x@y.z",
            subject="Second",
            body="Body",
        )
        draft_store.resolve(d1["draft_id"], "sent", "ok")

        pending = draft_store.pending()
        assert len(pending) == 1
        assert pending[0]["draft_id"] == d2["draft_id"]

        pending_agent1 = draft_store.pending("agent-1")
        assert len(pending_agent1) == 0

        pending_agent2 = draft_store.pending("agent-2")
        assert len(pending_agent2) == 1


class TestContextBuilders:
    def test_truncate_preview_short(self):
        text = "Short text"
        assert truncate_preview(text) == text

    def test_truncate_preview_long(self):
        text = "A " * 300
        result = truncate_preview(text, 100)
        assert len(result) <= 103
        assert result.endswith("...")

    def test_build_email_context(self):
        ctx = build_email_context(
            "user@example.com",
            "Important Subject",
            "This is the body of the email.",
        )
        assert "To: user@example.com" in ctx
        assert "Subject: Important Subject" in ctx
        assert "Body: This is the body" in ctx


class TestSendEmailDraftMode:
    @pytest.fixture
    def mock_ctx(self, tmp_path):
        manager = MagicMock()
        manager._owner_asks = MagicMock()
        manager._owner_asks.ask = AsyncMock(return_value={
            "id": "ask-123",
            "state": "open",
            "url": "https://discord.com/channels/123/456/789",
        })
        ctx = ToolContext(
            agent_id="test-agent",
            vault=MagicMock(),
            agent_manager=manager,
        )
        return ctx

    @pytest.fixture
    def google_module(self, tmp_path, monkeypatch):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "google_extra", REPO / "extras" / "google" / "google.py")
        google_extra = importlib.util.module_from_spec(spec)
        sys.modules["google_extra"] = google_extra

        monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
        (tmp_path / "data").mkdir(exist_ok=True)

        spec.loader.exec_module(google_extra)
        return google_extra

    async def test_draft_true_creates_ask(self, mock_ctx, google_module, tmp_path, monkeypatch):
        monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))

        result = await google_module.send_email(
            mock_ctx,
            "user@example.com",
            "Test Subject",
            "Test body content",
            draft=True,
        )

        assert "draft awaiting approval" in result.lower()
        assert "user@example.com" in result
        assert "Test Subject" in result
        mock_ctx.agent_manager._owner_asks.ask.assert_awaited_once()

        call_kwargs = mock_ctx.agent_manager._owner_asks.ask.call_args.kwargs
        assert call_kwargs["question"] == "Send this email?"
        assert call_kwargs["options"] == ["Send", "Edit", "Cancel"]
        assert call_kwargs["default"] == "Do not send"
        assert "user@example.com" in call_kwargs["context"]

    async def test_draft_false_sends_immediately(self, mock_ctx, google_module, monkeypatch):
        api_calls = []

        async def fake_api(ctx, url, method="GET", data=None):
            api_calls.append({"url": url, "method": method})
            return {"id": "sent-1"}

        monkeypatch.setattr(google_module, "_google_api", fake_api)

        result = await google_module.send_email(
            mock_ctx,
            "user@example.com",
            "Test Subject",
            "Test body",
            draft=False,
        )

        assert "Email sent" in result
        assert any("send" in c["url"] for c in api_calls)
        mock_ctx.agent_manager._owner_asks.ask.assert_not_awaited()

    async def test_missing_attachment_fails_before_draft(self, mock_ctx, google_module, tmp_path, monkeypatch):
        monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))

        result = await google_module.send_email(
            mock_ctx,
            "user@example.com",
            "Test",
            "Body",
            attachments="/nonexistent/file.pdf",
            draft=True,
        )

        assert "Not sent" in result
        assert "not found" in result
        mock_ctx.agent_manager._owner_asks.ask.assert_not_awaited()

    async def test_fallback_when_owner_asks_unavailable(self, google_module, monkeypatch, tmp_path):
        ctx = ToolContext(agent_id="test-agent", vault=MagicMock())
        api_calls = []

        async def fake_api(ctx, url, method="GET", data=None):
            api_calls.append({"url": url, "method": method})
            return {"id": "sent-1"}

        monkeypatch.setattr(google_module, "_google_api", fake_api)
        monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
        (tmp_path / "data").mkdir(exist_ok=True)

        result = await google_module.send_email(
            ctx,
            "user@example.com",
            "Test",
            "Body",
            draft=True,
        )

        assert "Email sent" in result


class TestDraftResolution:
    @pytest.fixture
    def mock_row(self, draft_store):
        draft = draft_store.create(
            agent_id="test-agent",
            account="default",
            recipient="user@example.com",
            subject="Test Subject",
            body="Test body",
        )
        return {
            "id": "ask-123",
            "agent_id": "test-agent",
            "channel_id": "456",
            "account": "default",
            "state": "answered",
            "answer": "Send",
            "payload": {
                "request_key": f"email-draft:{draft['draft_id']}",
                "question": "Send this email?",
                "context": "To: user@example.com",
                "default": "Do not send",
                "options": ["Send", "Edit", "Cancel"],
            },
        }

    async def test_resolve_send_executes_email(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
        (tmp_path / "data").mkdir(exist_ok=True)

        store = EmailDraftStore(tmp_path / "data" / "email_drafts.db")
        draft = store.create(
            agent_id="test-agent",
            account="default",
            recipient="user@example.com",
            subject="Test Subject",
            body="Test body",
        )
        store.close()

        row = {
            "id": "ask-123",
            "agent_id": "test-agent",
            "channel_id": "456",
            "account": "default",
            "state": "answered",
            "answer": "Send",
            "payload": {
                "request_key": f"email-draft:{draft['draft_id']}",
                "question": "Send this email?",
                "context": "",
                "default": "Do not send",
                "options": ["Send", "Edit", "Cancel"],
            },
        }

        from src.connectors.discord_owner_asks import DiscordOwnerAsks

        connector = MagicMock()
        manager = MagicMock()
        config = {"admin_users": {"discord": ["123"]}, "waiting_on_you": {"digest_hour": 23}}

        service = DiscordOwnerAsks(connector, manager, config, tmp_path / "asks.db")

        with patch("extras.google.google._execute_email_send", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = "Email sent to user@example.com: Test Subject"

            result = await service._resolve_email_draft(row)

            assert "approved and sent" in result.lower()
            mock_send.assert_awaited_once()

        store = EmailDraftStore(tmp_path / "data" / "email_drafts.db")
        resolved = store.get(draft["draft_id"])
        assert resolved["status"] == "sent"
        store.close()
        service.store.close()

    async def test_resolve_edit_returns_edit_message(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
        (tmp_path / "data").mkdir(exist_ok=True)

        store = EmailDraftStore(tmp_path / "data" / "email_drafts.db")
        draft = store.create(
            agent_id="test-agent",
            account="default",
            recipient="user@example.com",
            subject="Test Subject",
            body="Test body",
        )
        store.close()

        row = {
            "id": "ask-123",
            "agent_id": "test-agent",
            "channel_id": "456",
            "account": "default",
            "state": "answered",
            "answer": "Edit",
            "payload": {
                "request_key": f"email-draft:{draft['draft_id']}",
                "question": "Send this email?",
                "context": "",
                "default": "Do not send",
                "options": ["Send", "Edit", "Cancel"],
            },
        }

        from src.connectors.discord_owner_asks import DiscordOwnerAsks

        connector = MagicMock()
        manager = MagicMock()
        config = {"admin_users": {"discord": ["123"]}, "waiting_on_you": {"digest_hour": 23}}

        service = DiscordOwnerAsks(connector, manager, config, tmp_path / "asks.db")

        result = await service._resolve_email_draft(row)

        assert "requested edits" in result.lower()
        assert "user@example.com" in result

        store = EmailDraftStore(tmp_path / "data" / "email_drafts.db")
        resolved = store.get(draft["draft_id"])
        assert resolved["status"] == "edit_requested"
        store.close()
        service.store.close()

    async def test_resolve_cancel_does_not_send(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
        (tmp_path / "data").mkdir(exist_ok=True)

        store = EmailDraftStore(tmp_path / "data" / "email_drafts.db")
        draft = store.create(
            agent_id="test-agent",
            account="default",
            recipient="user@example.com",
            subject="Test Subject",
            body="Test body",
        )
        store.close()

        row = {
            "id": "ask-123",
            "agent_id": "test-agent",
            "channel_id": "456",
            "account": "default",
            "state": "answered",
            "answer": "Cancel",
            "payload": {
                "request_key": f"email-draft:{draft['draft_id']}",
                "question": "Send this email?",
                "context": "",
                "default": "Do not send",
                "options": ["Send", "Edit", "Cancel"],
            },
        }

        from src.connectors.discord_owner_asks import DiscordOwnerAsks

        connector = MagicMock()
        manager = MagicMock()
        config = {"admin_users": {"discord": ["123"]}, "waiting_on_you": {"digest_hour": 23}}

        service = DiscordOwnerAsks(connector, manager, config, tmp_path / "asks.db")

        result = await service._resolve_email_draft(row)

        assert "cancelled" in result.lower()
        assert "not sent" in result.lower()

        store = EmailDraftStore(tmp_path / "data" / "email_drafts.db")
        resolved = store.get(draft["draft_id"])
        assert resolved["status"] == "cancelled"
        store.close()
        service.store.close()

    async def test_stale_ask_does_not_send(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
        (tmp_path / "data").mkdir(exist_ok=True)

        store = EmailDraftStore(tmp_path / "data" / "email_drafts.db")
        draft = store.create(
            agent_id="test-agent",
            account="default",
            recipient="user@example.com",
            subject="Test Subject",
            body="Test body",
        )
        store.close()

        row = {
            "id": "ask-123",
            "agent_id": "test-agent",
            "channel_id": "456",
            "account": "default",
            "state": "stale",
            "answer": None,
            "payload": {
                "request_key": f"email-draft:{draft['draft_id']}",
                "question": "Send this email?",
                "context": "",
                "default": "Do not send",
                "options": ["Send", "Edit", "Cancel"],
            },
        }

        from src.connectors.discord_owner_asks import DiscordOwnerAsks

        connector = MagicMock()
        manager = MagicMock()
        config = {"admin_users": {"discord": ["123"]}, "waiting_on_you": {"digest_hour": 23}}

        service = DiscordOwnerAsks(connector, manager, config, tmp_path / "asks.db")

        result = await service._resolve_email_draft(row)

        assert "expired" in result.lower()
        assert "not sent" in result.lower()

        store = EmailDraftStore(tmp_path / "data" / "email_drafts.db")
        resolved = store.get(draft["draft_id"])
        assert resolved["status"] == "expired"
        store.close()
        service.store.close()

    async def test_non_email_ask_returns_none(self, tmp_path):
        row = {
            "id": "ask-123",
            "agent_id": "test-agent",
            "channel_id": "456",
            "account": "default",
            "state": "answered",
            "answer": "Yes",
            "payload": {
                "request_key": "other-decision",
                "question": "Do something?",
                "context": "",
                "default": "No",
                "options": ["Yes", "No"],
            },
        }

        from src.connectors.discord_owner_asks import DiscordOwnerAsks

        connector = MagicMock()
        manager = MagicMock()
        config = {"admin_users": {"discord": ["123"]}, "waiting_on_you": {"digest_hour": 23}}

        service = DiscordOwnerAsks(connector, manager, config, tmp_path / "asks.db")

        result = await service._resolve_email_draft(row)

        assert result is None
        service.store.close()
