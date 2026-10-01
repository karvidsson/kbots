"""Tests for owner digest — gather, format, and DM delivery."""

import asyncio
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import pytest

from src.core.owner_digest import (
    OwnerDigestTask,
    create_owner_digest_task,
    format_owner_digest,
    gather_active_goals,
    gather_owner_asks,
    gather_pending_hitl,
    send_digest_dm,
)


# --- gather_owner_asks tests ---

def test_gather_owner_asks_empty_store():
    """Empty or None store returns empty list."""
    assert gather_owner_asks(None) == []


def test_gather_owner_asks_with_rows():
    """Extracts id, agent_id, question, created, url from store rows."""
    store = Mock()
    store.rows.return_value = [
        {
            "id": "ask1",
            "agent_id": "agent1",
            "payload": {"question": "Publish the draft?"},
            "created": 1700000000.0,
            "message_id": "123",
            "channel_id": "456",
            "guild_id": "789",
        }
    ]
    with patch("src.core.owner_asks.jump", return_value="https://discord.com/..."):
        result = gather_owner_asks(store)
    assert len(result) == 1
    assert result[0]["id"] == "ask1"
    assert result[0]["agent_id"] == "agent1"
    assert "Publish" in result[0]["question"]


def test_gather_owner_asks_truncates_long_questions():
    """Questions are truncated to 100 characters."""
    store = Mock()
    store.rows.return_value = [
        {
            "id": "ask1",
            "agent_id": "agent1",
            "payload": {"question": "x" * 200},
            "created": 1700000000.0,
            "message_id": "",
            "channel_id": "",
            "guild_id": "",
        }
    ]
    with patch("src.core.owner_asks.jump", return_value=""):
        result = gather_owner_asks(store)
    assert len(result[0]["question"]) == 100


# --- gather_pending_hitl tests ---

@pytest.mark.asyncio
async def test_gather_pending_hitl_none_db():
    """None db returns empty list."""
    assert await gather_pending_hitl(None) == []


@pytest.mark.asyncio
async def test_gather_pending_hitl_with_rows(tmp_path):
    """Extracts pending HITL requests from database."""
    import aiosqlite
    db_path = tmp_path / "test.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE hitl_pending (
                hitl_id TEXT PRIMARY KEY,
                agent_id TEXT,
                tool_name TEXT,
                description TEXT,
                created_at REAL,
                channel_id TEXT,
                status TEXT
            )
        """)
        await db.execute(
            "INSERT INTO hitl_pending VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("h1", "agent1", "send_email", "Send to boss", 1700000000.0, "chan1", "pending")
        )
        await db.execute(
            "INSERT INTO hitl_pending VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("h2", "agent2", "install_mcp", "Install server", 1700000001.0, "chan2", "approved")
        )
        await db.commit()
        result = await gather_pending_hitl(db)
    assert len(result) == 1
    assert result[0]["hitl_id"] == "h1"
    assert result[0]["tool_name"] == "send_email"


# --- gather_active_goals tests ---

def test_gather_active_goals_empty():
    """Returns empty list when goals module not available or empty."""
    with patch("src.core.goals.list_goals", return_value=[]):
        with patch("src.core.goals.list_tasks", return_value=[]):
            result = gather_active_goals()
    assert result == []


def test_gather_active_goals_with_data():
    """Extracts active goals with task counts."""
    with patch("src.core.goals.list_goals") as mock_list_goals:
        mock_list_goals.return_value = [
            {
                "id": "g-test",
                "title": "Test Goal",
                "status": "executing",
                "owner_agent": "atlas",
                "channel_id": "123",
                "updated_at": 1700000000.0,
                "blocked_brief": "",
            }
        ]
        with patch("src.core.goals.list_tasks") as mock_list_tasks:
            mock_list_tasks.return_value = [
                {"id": 1, "title": "Task 1"},
                {"id": 2, "title": "Task 2"},
            ]
            result = gather_active_goals()
    assert len(result) == 1
    assert result[0]["id"] == "g-test"
    assert result[0]["tasks_open"] == 2


# --- format_owner_digest tests ---

def test_format_owner_digest_all_empty():
    """Empty inputs produce empty string (no DM)."""
    result = format_owner_digest([], [], [])
    assert result == ""


def test_format_owner_digest_only_asks():
    """Formats owner_asks section correctly."""
    asks = [
        {
            "id": "ask1",
            "agent_id": "atlas",
            "question": "Publish the draft?",
            "created": time.time() - 3600,
            "url": "https://discord.com/channels/123/456/789",
        }
    ]
    result = format_owner_digest(asks, [], [], now=time.time())
    assert "Morning digest" in result
    assert "Decisions waiting" in result
    assert "atlas" in result
    assert "Publish" in result
    assert "1h" in result


def test_format_owner_digest_only_hitl():
    """Formats pending HITL section correctly."""
    hitl = [
        {
            "hitl_id": "h1",
            "agent_id": "redline",
            "tool_name": "send_email",
            "description": "Send report to team",
            "created_at": time.time() - 1800,
            "channel_id": "chan1",
        }
    ]
    result = format_owner_digest([], hitl, [], now=time.time())
    assert "Morning digest" in result
    assert "Tool approvals" in result
    assert "redline" in result
    assert "send_email" in result


def test_format_owner_digest_only_goals():
    """Formats active goals section correctly."""
    goals = [
        {
            "id": "g-launch",
            "title": "Launch new feature",
            "status": "executing",
            "owner_agent": "atlas",
            "channel_id": "123",
            "updated_at": time.time(),
            "tasks_open": 3,
            "blocked_brief": "",
        }
    ]
    result = format_owner_digest([], [], goals, now=time.time())
    assert "Morning digest" in result
    assert "Active goals" in result
    assert "g-launch" in result
    assert "3 task(s)" in result


def test_format_owner_digest_blocked_goal():
    """Blocked goals show 'waiting on you' emphasis."""
    goals = [
        {
            "id": "g-blocked",
            "title": "Blocked Goal",
            "status": "blocked_on_user",
            "owner_agent": "atlas",
            "channel_id": "123",
            "updated_at": time.time(),
            "tasks_open": 1,
            "blocked_brief": '{"do": ["answer question"]}',
        }
    ]
    result = format_owner_digest([], [], goals, now=time.time())
    assert "waiting on you" in result.lower()
    assert "🧱" in result


def test_format_owner_digest_all_sections():
    """All three sections appear when all have data."""
    asks = [{"id": "a1", "agent_id": "ag1", "question": "Q?", "created": time.time(), "url": ""}]
    hitl = [{"hitl_id": "h1", "agent_id": "ag2", "tool_name": "tool", "description": "", "created_at": time.time(), "channel_id": ""}]
    goals = [{"id": "g1", "title": "Goal", "status": "executing", "owner_agent": "ag3", "channel_id": "", "updated_at": time.time(), "tasks_open": 0, "blocked_brief": ""}]
    result = format_owner_digest(asks, hitl, goals, now=time.time())
    assert "Decisions waiting" in result
    assert "Tool approvals" in result
    assert "Active goals" in result


def test_format_owner_digest_truncates_lists():
    """Long lists are truncated with '...and N more'."""
    asks = [
        {"id": f"a{i}", "agent_id": f"ag{i}", "question": f"Q{i}?", "created": time.time(), "url": ""}
        for i in range(15)
    ]
    result = format_owner_digest(asks, [], [], now=time.time())
    assert "...and 5 more" in result


# --- OwnerDigestTask tests ---

@pytest.mark.asyncio
async def test_owner_digest_task_disabled_no_owner():
    """Task is disabled when no owner_id."""
    task = OwnerDigestTask(
        owner_id="",
        digest_hour=8,
        timezone=ZoneInfo("UTC"),
        vault=None,
        hitl_db=None,
    )
    assert not task.enabled
    result = await task.tick()
    assert result["skipped_reason"] == "no owner_id configured"


@pytest.mark.asyncio
async def test_owner_digest_task_before_digest_hour():
    """Task skips before digest_hour."""
    task = OwnerDigestTask(
        owner_id="123",
        digest_hour=10,
        timezone=ZoneInfo("UTC"),
        vault=None,
        hitl_db=None,
    )
    # 8 AM UTC
    now = datetime(2024, 1, 1, 8, 0, 0, tzinfo=ZoneInfo("UTC")).timestamp()
    result = await task.tick(now=now)
    assert result["skipped_reason"] == "before digest_hour"


@pytest.mark.asyncio
async def test_owner_digest_task_nothing_pending():
    """Task skips when nothing is pending."""
    task = OwnerDigestTask(
        owner_id="123",
        digest_hour=8,
        timezone=ZoneInfo("UTC"),
        vault=None,
        hitl_db=None,
    )
    # 9 AM UTC
    now = datetime(2024, 1, 1, 9, 0, 0, tzinfo=ZoneInfo("UTC")).timestamp()
    result = await task.tick(now=now)
    assert result["skipped_reason"] == "nothing pending"
    assert result["content"] == ""


@pytest.mark.asyncio
async def test_owner_digest_task_sends_once_per_day():
    """Task only sends once per day."""
    vault = Mock()
    vault.get.return_value = "fake-token"

    task = OwnerDigestTask(
        owner_id="123",
        digest_hour=8,
        timezone=ZoneInfo("UTC"),
        vault=vault,
        hitl_db=None,
        owner_asks_store=None,
    )

    # Mock to have pending data
    with patch("src.core.owner_digest.gather_owner_asks") as mock_asks:
        mock_asks.return_value = [
            {"id": "a1", "agent_id": "ag1", "question": "Q?", "created": time.time(), "url": ""}
        ]
        with patch("src.core.owner_digest.send_digest_dm", new_callable=AsyncMock) as mock_dm:
            mock_dm.return_value = True

            # First tick at 9 AM
            now = datetime(2024, 1, 1, 9, 0, 0, tzinfo=ZoneInfo("UTC")).timestamp()
            result1 = await task.tick(now=now)
            assert result1["sent"] is True

            # Second tick same day
            result2 = await task.tick(now=now + 3600)
            assert result2["skipped_reason"] == "already sent today"


@pytest.mark.asyncio
async def test_owner_digest_task_fallback_on_dm_failure():
    """Task falls back to channel when DM fails."""
    vault = Mock()
    vault.get.return_value = "fake-token"

    task = OwnerDigestTask(
        owner_id="123",
        digest_hour=8,
        timezone=ZoneInfo("UTC"),
        vault=vault,
        hitl_db=None,
        fallback_channel="alert-chan",
    )

    with patch("src.core.owner_digest.gather_owner_asks") as mock_asks:
        mock_asks.return_value = [
            {"id": "a1", "agent_id": "ag1", "question": "Q?", "created": time.time(), "url": ""}
        ]
        with patch("src.core.owner_digest.send_digest_dm", new_callable=AsyncMock) as mock_dm:
            mock_dm.return_value = False  # DM fails
            with patch("src.core.owner_digest.send_to_fallback_channel", new_callable=AsyncMock) as mock_fallback:
                mock_fallback.return_value = True

                now = datetime(2024, 1, 1, 9, 0, 0, tzinfo=ZoneInfo("UTC")).timestamp()
                result = await task.tick(now=now)

                assert result["sent"] is True
                mock_fallback.assert_awaited_once()
                # Check fallback mentions the owner
                call_args = mock_fallback.call_args
                assert "<@123>" in call_args[0][2]


# --- create_owner_digest_task tests ---

def test_create_owner_digest_task_disabled():
    """Returns None when waiting_on_you.enabled is false."""
    config = {"waiting_on_you": {"enabled": False}}
    result = create_owner_digest_task(config, None, None)
    assert result is None


def test_create_owner_digest_task_no_owner():
    """Returns None when no owner_id and multiple admins."""
    config = {
        "waiting_on_you": {"enabled": True},
        "admin_users": {"discord": ["user1", "user2"]},
    }
    result = create_owner_digest_task(config, None, None)
    assert result is None


def test_create_owner_digest_task_single_admin():
    """Infers owner_id from single admin."""
    config = {
        "waiting_on_you": {"enabled": True, "digest_hour": 9, "timezone": "UTC"},
        "admin_users": {"discord": ["user123"]},
    }
    result = create_owner_digest_task(config, Mock(), Mock())
    assert result is not None
    assert result.owner_id == "user123"
    assert result.digest_hour == 9


def test_create_owner_digest_task_with_fallback():
    """Uses security.alert_channel as fallback."""
    config = {
        "waiting_on_you": {"enabled": True},
        "admin_users": {"discord": ["user123"]},
        "security": {"alert_channel": "alerts"},
    }
    result = create_owner_digest_task(config, Mock(), Mock())
    assert result is not None
    assert result.fallback_channel == "alerts"
