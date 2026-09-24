"""Real provider parsing, SQLite migrations, accounting and private command path."""

import asyncio
import json
import sqlite3
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from src.connectors.discord_spend import register_spend
from src.core.base import LLMResponse, Message, MessageRole
from src.core.spend import SpendLedger, TurnMeter, render_spend
from src.core.storage import Storage
from src.core.usage import inclusive_usage, money, table_cost
from src.llm.claude_code import ClaudeCodeProvider
from src.llm.codex_cli import CodexCLIProvider
from tests.test_openai_compat import _provider
from tests.test_usage_limits import DowngradeProvider, RecordingAlerter, _manager, _msg


@pytest.fixture
async def store(tmp_path):
    db = Storage(tmp_path / "db.sqlite")
    await db.init()
    await db.get_or_create_session("s", "worker", user_id="first-user")
    yield db
    await db.close()


def usage(i=100, o=20, r=50, w=30, **kwargs):
    return dict(input_tokens=i, output_tokens=o, cache_read_tokens=r, cache_write_tokens=w, **kwargs)


def response(**kwargs):
    return LLMResponse(content="ok", model="model-a", tokens_used=200, usage=usage(**kwargs))


async def record(store, resp=None, *, turn="turn1", call=1, agent="worker", user="u", provider="p", prices=None):
    await store.spend.record(
        turn_id=turn,
        call_index=call,
        session_id="s",
        agent_id=agent,
        requester_id=user,
        provider=provider,
        model=(resp.model if resp else "model-a"),
        response=resp or response(),
        duration_ms=13,
        turn_duration_ms=27,
        prices=prices or {},
    )


async def rawrows(store):
    cur = await store.spend.db.execute("SELECT * FROM turn_usage ORDER BY created_at, call_index")
    return [dict(r) for r in await cur.fetchall()]


def claude(cost="1.25", *, fresh=True, scale=1, session="cli", models=True):
    raw = dict(
        result="ok",
        session_id=session,
        total_cost_usd=cost,
        usage=dict(input_tokens=100, output_tokens=20, cache_read_input_tokens=50, cache_creation_input_tokens=30),
    )
    if models:
        raw["modelUsage"] = {
            "claude-sonnet": dict(
                inputTokens=100 * scale,
                outputTokens=20 * scale,
                cacheReadInputTokens=50 * scale,
                cacheCreationInputTokens=30 * scale,
            )
        }
    parsed = ClaudeCodeProvider({})._parse_response(json.dumps(raw))
    parsed.usage["fresh_session"] = fresh
    return parsed


async def test_claude_four_classes_and_reported_cost_reach_store(store):
    parsed = claude()
    assert parsed.tokens_used == 200
    await record(store, parsed, provider="claude_code")
    (row,) = await rawrows(store)
    assert [row[k] for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")] == [
        100,
        20,
        50,
        30,
    ]
    assert row["provider_cost_usd"] == row["cost_usd"] == "1.25"
    assert row["cost_source"] == "provider"
    assert row["model"] == "claude-sonnet"
    assert (row["duration_ms"], row["turn_duration_ms"]) == (13, 27)


async def test_codex_actual_fake_exec_through_store(store, tmp_path):
    binary = tmp_path / "fake-codex"
    events = [
        dict(type="thread.started", thread_id="thread-1"),
        dict(type="item.completed", item=dict(type="agent_message", text="reply")),
        dict(type="turn.completed", usage=dict(input_tokens=150, cached_input_tokens=50, output_tokens=20)),
    ]
    binary.write_text("#!/usr/bin/env python3\n" + "\n".join(f"print({json.dumps(e)!r})" for e in events))
    binary.chmod(0o700)
    p = CodexCLIProvider({"codex_bin": str(binary)})
    parsed = await p.complete([Message(role=MessageRole.USER, content="hello")], project_dir=str(tmp_path / "agent"))
    await record(store, parsed, provider="codex_cli")
    (row,) = await rawrows(store)
    assert [row[k] for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")] == [
        100,
        20,
        50,
        0,
    ]
    assert row["tokens_used"] == parsed.tokens_used == 170
    assert row["cost_usd"] is None


async def test_openai_response_through_store(store):
    p = _provider(
        reply={
            "choices": [{"message": {"content": "ok"}}],
            "model": "served-model",
            "usage": {
                "prompt_tokens": 150,
                "completion_tokens": 20,
                "total_tokens": 170,
                "prompt_tokens_details": {"cached_tokens": 50},
                "completion_tokens_details": {"reasoning_tokens": 10},
            },
        }
    )
    parsed = await p.complete([Message(role=MessageRole.USER, content="hello")])
    await record(store, parsed, provider="api")
    (row,) = await rawrows(store)
    assert [row[k] for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")] == [
        100,
        20,
        50,
        0,
    ]
    assert row["tokens_used"] == 170  # reasoning is already output, cache already input
    assert row["model"] == "served-model"
    assert row["cost_usd"] is None


async def test_ollama_native_response_through_store(store):
    p = _provider()
    p._ollama_native = True
    p._request_native = AsyncMock(
        return_value={
            "message": {"content": "ok"},
            "prompt_eval_count": 100,
            "prompt_eval_cached_count": 30,
            "eval_count": 20,
        }
    )
    parsed = await p.complete([Message(role=MessageRole.USER, content="hello")])
    await record(store, parsed, provider="local")
    (row,) = await rawrows(store)
    assert [row[k] for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")] == [
        70,
        20,
        30,
        0,
    ]


async def test_cost_precedence_and_snapshot(store):
    prices = {"p": {"model-a": dict(input=1, output=2, cache_read=3, cache_write=4)}}
    await record(store, response(provider_cost_usd="0.125"), prices=prices)
    await record(store, turn="table", prices=prices)
    prices["p"]["model-a"]["input"] = 999  # does not rewrite captured prices
    await record(store, turn="unknown")
    rows = await rawrows(store)
    assert [(r["cost_source"], r["cost_usd"]) for r in rows] == [
        ("provider", "0.125"),
        ("table", "0.00041"),
        ("unknown", None),
    ]
    assert json.loads(rows[1]["price_snapshot"])["model-a"]["input"] == 1
    assert rows[2]["tokens_used"] == 200


async def test_cumulative_cost_and_model_deltas_survive_restart(store):
    await record(store, claude(), provider="claude_code")
    await store.spend.close()
    store.spend = await SpendLedger.open(store._db_path)
    await record(store, claude("1.50", fresh=False, scale=2), turn="second", provider="claude_code")
    rows = await rawrows(store)
    assert [r["cost_usd"] for r in rows] == ["1.25", "0.25"]
    assert [r["tokens_used"] for r in rows] == [200, 200]
    # A duplicated capture must not advance the baseline twice.
    await record(store, claude("1.50", fresh=False, scale=2), turn="second", provider="claude_code")
    assert len(await rawrows(store)) == 2


async def test_first_resume_unknown_then_delta_and_counter_reset(store):
    await record(store, claude("8", fresh=False, scale=4), turn="first")
    await record(store, claude("9", fresh=False, scale=5), turn="next")
    await record(store, claude("0.5", fresh=False, scale=1), turn="reset")
    rows = await rawrows(store)
    assert [r["cost_usd"] for r in rows] == [None, "1", None]
    assert rows[0]["provider_cost_usd"] == "8"
    assert all(r["cost_source"] == "unknown" for r in (rows[0], rows[2]))


async def test_missing_observation_breaks_cost_chain(store):
    await record(store, claude("1"))
    await record(store, claude(None, fresh=False, scale=2), turn="missing")
    await record(store, claude("3", fresh=False, scale=3), turn="next")
    assert [r["cost_usd"] for r in await rawrows(store)] == ["1", None, None]


async def test_multimodel_table_fallback_uses_model_specific_counts(store):
    parsed = claude(None)
    parsed.usage["model_usage"]["other-model"] = usage(i=100, o=0, r=0, w=0)
    prices = {
        "p": {
            "claude-sonnet": dict(input=1, output=2, cache_read=3, cache_write=4),
            "other-model": dict(input=10, output=0, cache_read=0, cache_write=0),
        }
    }
    await record(store, parsed, prices=prices)
    (row,) = await rawrows(store)
    assert row["model"] == "multiple" and row["tokens_used"] == 300
    assert Decimal(row["cost_usd"]) == Decimal("0.001410")
    assert row["cost_source"] == "table"


async def test_report_arithmetic_unknown_bucket_and_sorting(store):
    await record(store, response(provider_cost_usd="0.10"), turn="a", agent="a")
    await record(store, response(provider_cost_usd="0.20"), turn="a", call=2, agent="a")
    await record(store, turn="b", agent="a")
    await record(store, response(provider_cost_usd="1"), turn="c", agent="b")
    await record(store, turn="d", agent="z")
    await record(store, response(i=500), turn="e", agent="y")
    report = render_spend(await store.spend.rows(7), 7)
    lines = report.splitlines()
    assert [s.split()[0] for s in lines[3:7]] == ["b", "a", "y", "z"]
    assert "a                         2" in report
    assert "$0.3000 + ?" in report
    assert "TOTAL" in report and "$1.3000 + ?" in report
    assert "1,600" in report
    assert "Unknown-cost turns: 3." in report
    assert "not a subscription bill" in report
    assert "```text\n" in report and "|---" not in report


async def test_zero_is_known_and_unknown_is_never_zero(store):
    await record(store, response(provider_cost_usd="0"), turn="zero")
    await record(store, turn="unknown", agent="unknown-agent")
    report = render_spend(await store.spend.rows(7), 7)
    assert "unknown-agent" in report and "unknown" in report
    assert "$0.0000" in report and "Unknown-cost turns: 1." in report


@pytest.mark.parametrize("bad", [None, -1, True, "NaN", "Infinity", "garbage", {}, "1e999"])
def test_invalid_cost_is_unknown(bad):
    assert money(bad) is None
    assert table_cost(usage(), "p", "m", {"p": {"m": dict(input=bad, output=1, cache_read=1, cache_write=1)}}) is None


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"prompt_tokens": 10, "completion_tokens": 2},
        {"prompt_tokens": 10, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": 11}},
    ],
)
def test_incomplete_or_overlapping_usage_cannot_be_priced(raw):
    normalized = inclusive_usage(raw)
    assert table_cost(normalized, "p", "m", {"p": {"m": dict(input=1, output=1, cache_read=1, cache_write=1)}}) is None


async def test_old_database_migration_preserves_history_and_old_token_report(tmp_path):
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.executescript("""CREATE TABLE sessions(id TEXT PRIMARY KEY,agent_id TEXT,channel_id TEXT,user_id TEXT,
        cli_session_id TEXT,created_at REAL,last_active REAL,summary TEXT);
        CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT,name TEXT,
        tool_calls TEXT,tool_results TEXT,tokens_used INTEGER,created_at REAL);
        INSERT INTO sessions VALUES('s','old','shared','first-user',NULL,unixepoch(),unixepoch(),NULL);
        INSERT INTO messages VALUES(1,'s','assistant','old reply',NULL,NULL,NULL,123,unixepoch());""")
    original = db.execute("SELECT * FROM messages").fetchone()
    db.close()
    store = Storage(path)
    await store.init()
    try:
        cursor = await store._db.execute(
            "SELECT id,session_id,role,content,name,tool_calls,tool_results,tokens_used,created_at FROM messages"
        )
        assert await cursor.fetchone() == original
        rows = await store.spend.rows(7)
        assert len(rows) == 1 and rows[0]["tokens_used"] == 123 and rows[0]["cost_usd"] is None
        assert await store.spend.rows(7, requester_id="first-user") == []
        old = await store.get_token_usage(7)
        assert old[0]["tokens"] == 123
    finally:
        await store.close()


async def test_message_binding_avoids_double_count_and_detects_missing_call(store):
    await record(store, response(provider_cost_usd="1"))
    await store.save_message("s", "assistant", "ok", tokens_used=400, usage_turn_id="turn1", usage_call_count=2)
    report = render_spend(await store.spend.rows(7), 7)
    assert "TOTAL                     1" in report and "400" in report
    assert "$1.0000 + ?" in report and "Unknown-cost turns: 1." in report


async def test_actual_initiator_scope_not_shared_session_creator(store):
    await record(store, response(provider_cost_usd="1"), user="second-user")
    await store.save_message("s", "assistant", "ok", usage_turn_id="turn1", usage_call_count=1)
    assert await store.spend.rows(7, requester_id="first-user") == []
    assert len(await store.spend.rows(7, requester_id="second-user")) == 1


async def test_capture_failure_leaves_actual_router_turn_and_message_unchanged(store, tmp_path):
    from src.core.router import Router

    mgr = _manager(tmp_path, DowngradeProvider({}), RecordingAlerter())
    mgr.storage = store
    store.spend.record = AsyncMock(side_effect=sqlite3.OperationalError("synthetic write failure"))
    await Router(mgr).route(_msg())
    assert mgr.connectors["stub"].sent == ["ok on sonnet"]
    cursor = await store._db.execute("SELECT content FROM messages WHERE role='assistant'")
    assert (await cursor.fetchone())[0] == "ok on sonnet"
    assert len(await store.spend.rows(7)) == 1  # missing capture stays unknown history
    assert len(await store.spend.rows(7, requester_id="u")) == 1
    assert await store.spend.rows(7, requester_id="someone-else") == []


async def test_optional_migration_failure_does_not_fail_storage_or_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(SpendLedger, "open", AsyncMock(side_effect=sqlite3.OperationalError("failure")))
    store = Storage(tmp_path / "db")
    await store.init()
    try:
        assert store.spend is None
        await store.get_or_create_session("s", "a")
        await store.save_message("s", "assistant", "still works")
    finally:
        await store.close()


async def test_meter_counts_provider_switch_and_raised_call(store):
    one = SimpleNamespace(complete=AsyncMock(return_value=response(provider_cost_usd="1")))
    two = SimpleNamespace(complete=AsyncMock(side_effect=RuntimeError("error")))
    mgr = SimpleNamespace(storage=store, defaults={}, llm_providers={"one": one, "two": two})
    meter = TurnMeter(mgr, "s", "a", "u")
    await meter.complete(one, [], model="m")
    with pytest.raises(RuntimeError):
        await meter.complete(two, [], model="m")
    rows = await rawrows(store)
    assert [r["provider"] for r in rows] == ["one", "two"]
    assert len({r["turn_id"] for r in rows}) == 1
    assert rows[1]["cost_usd"] is None and meter.tokens == 200


async def test_concurrent_records_are_atomic(store):
    await asyncio.gather(*(record(store, turn=f"t{i}") for i in range(10)))
    assert len(await rawrows(store)) == 10


def slash_client(store, admin=False):
    client = discord.Client(intents=discord.Intents.none())
    tree = discord.app_commands.CommandTree(client)
    wrapper = SimpleNamespace(
        tree=tree, connector=SimpleNamespace(_agent_manager=SimpleNamespace(storage=store)), _is_admin=lambda uid: admin
    )
    register_spend(wrapper)
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=42),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    return tree.get_command("spend"), interaction


@pytest.mark.parametrize("admin", [False, True])
async def test_slash_default_scope_and_ephemeral_output(store, admin):
    await record(store, user="42", turn="mine", agent="mine")
    await record(store, user="99", turn="other", agent="other")
    command, interaction = slash_client(store, admin)
    await command.callback(interaction)
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    report = interaction.followup.send.call_args.args[0]
    assert "Last 7 days" in report and "mine" in report
    assert ("other" in report) == admin
    assert interaction.followup.send.call_args.kwargs["ephemeral"] is True
    (parameter,) = command.parameters
    assert parameter.min_value == 1 and parameter.max_value == 365


async def test_long_report_is_complete_private_attachment(store):
    await asyncio.gather(*(record(store, turn=f"t{i}", agent=f"agent{i}", user="42") for i in range(40)))
    command, interaction = slash_client(store)
    await command.callback(interaction, 7)
    call = interaction.followup.send.call_args
    assert call.kwargs["ephemeral"] is True
    report = call.kwargs["file"].fp.read().decode()
    assert "agent39" in report and "Unknown-cost turns: 40." in report


async def test_report_failure_is_not_presented_as_zero(store):
    store.spend.rows = AsyncMock(side_effect=sqlite3.OperationalError("secret database path"))
    command, interaction = slash_client(store)
    await command.callback(interaction)
    assert interaction.followup.send.call_args.args[0] == "Spend ledger is unavailable."


@pytest.mark.parametrize("internal", [False, True])
async def test_actual_manager_multiround_and_no_reply_capture(store, tmp_path, internal):
    from src.core.router import Router

    provider = DowngradeProvider({})
    first = response(provider_cost_usd="0.10")
    first.tool_calls = [{"id": "t", "name": "fake", "arguments": {}}]
    final = response(provider_cost_usd="0.20")
    final.content = "NO_REPLY"
    provider.complete = AsyncMock(side_effect=[first, final])
    mgr = _manager(tmp_path, provider, RecordingAlerter())
    mgr.storage = store
    mgr._dispatch_tools = AsyncMock(return_value=[{"name": "fake", "content": "done"}])
    if internal:
        assert await mgr.handle_internal_message("main", _msg()) == "NO_REPLY"
    else:
        await Router(mgr).route(_msg())
        assert mgr.connectors["stub"].sent == []
    rows = await rawrows(store)
    assert len(rows) == 2 and len({r["turn_id"] for r in rows}) == 1
    cursor = await store._db.execute(
        "SELECT tokens_used,provider,usage_turn_id,usage_call_count FROM messages WHERE role='assistant'"
    )
    (saved,) = await cursor.fetchall()
    assert saved == (400, "mock", rows[0]["turn_id"], 2)
    report = render_spend(await store.spend.rows(7), 7)
    assert "TOTAL                     1" in report and "$0.3000" in report
    assert "Unknown-cost turns: 0." in report


async def test_claude_fake_cli_fresh_and_resume_use_actual_command(store, tmp_path, monkeypatch):
    from src.llm import claude_code

    monkeypatch.setattr(claude_code, "_ensure_workspace_trusted", lambda cwd: None)
    p = ClaudeCodeProvider({"claude_bin": str(tmp_path / "fake-claude")})
    monkeypatch.setattr(p, "_session_file_exists", lambda *args: True)
    result = dict(
        type="result",
        result="done",
        session_id="fake-session",
        total_cost_usd="1.25",
        usage=dict(input_tokens=100, output_tokens=20, cache_read_input_tokens=50, cache_creation_input_tokens=30),
        modelUsage={
            "actual-model": dict(inputTokens=100, outputTokens=20, cacheReadInputTokens=50, cacheCreationInputTokens=30)
        },
    )
    binary = tmp_path / "fake-claude"
    binary.write_text("#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\n" + f"print({json.dumps(result)!r})\n")
    binary.chmod(0o700)
    fresh = await p.complete([Message(role=MessageRole.USER, content="x")], project_dir=str(tmp_path))
    await record(store, fresh, turn="fresh")
    assert fresh.usage["fresh_session"] is True
    result["total_cost_usd"] = "1.50"
    result["modelUsage"]["actual-model"]["outputTokens"] = 40
    binary.write_text("#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\n" + f"print({json.dumps(result)!r})\n")
    resumed = await p.complete(
        [Message(role=MessageRole.USER, content="y")], project_dir=str(tmp_path), session_id="fake-session"
    )
    await record(store, resumed, turn="resumed")
    assert resumed.usage["fresh_session"] is False
    rows = await rawrows(store)
    assert [r["cost_usd"] for r in rows] == ["1.25", "0.25"]
    assert rows[1]["output_tokens"] == 20 and rows[1]["input_tokens"] == 0


async def test_error_result_preserves_usage(store):
    data = {
        "is_error": True,
        "result": "failed",
        "total_cost_usd": 0.75,
        "session_id": "s",
        "usage": {
            "input_tokens": 1,
            "output_tokens": 2,
            "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 4,
        },
    }
    parsed = ClaudeCodeProvider({})._parse_response(json.dumps(data))
    parsed.usage["fresh_session"] = True
    await record(store, parsed)
    assert parsed.stop_reason == "error" and parsed.tokens_used == 10
    (row,) = await rawrows(store)
    assert row["cost_usd"] == "0.75"


async def test_window_boundaries_and_invalid_days(store):
    await record(store, turn="old")
    await store.spend.db.execute("UPDATE turn_usage SET created_at=created_at-8*86400")
    await store.spend.db.commit()
    await record(store, turn="new")
    assert len(await store.spend.rows(7)) == 1
    assert len(await store.spend.rows(9)) == 2
    for days in (0, 366, -1, True, "7"):
        with pytest.raises(ValueError):
            await store.spend.rows(days)


async def test_tokenless_turn_is_not_silently_zero(store):
    await record(store, LLMResponse(content="no usage"))
    report = render_spend(await store.spend.rows(7), 7)
    assert "0 + ?" in report and "unknown" in report


def test_small_known_cost_never_rounds_to_zero_and_names_cannot_escape_fence():
    report = render_spend(
        [
            dict(
                turn_id="t",
                agent_id="bad```\n<@42>",
                model="m```\n",
                tokens_used=1,
                cost_usd="0.000000001",
                cost_source="provider",
            )
        ],
        7,
    )
    assert "<$0.0001" in report and "$0.0000 " not in report
    assert report.count("```") == 2 and "<@42>" not in report


async def test_older_ollama_usage_keeps_total_without_inventing_cache_counts(store):
    p = _provider()
    p._ollama_native = True
    p._request_native = AsyncMock(
        return_value={"message": {"content": "ok"}, "prompt_eval_count": 100, "eval_count": 20}
    )
    parsed = await p.complete([Message(role=MessageRole.USER, content="hello")])
    await record(store, parsed)
    (row,) = await rawrows(store)
    assert row["tokens_used"] == 120
    assert row["input_tokens"] is None and row["cache_read_tokens"] is None
    assert row["cost_usd"] is None


async def test_malformed_usage_does_not_lose_valid_provider_reply(store):
    bad = {
        "input_tokens": "wrong",
        "output_tokens": True,
        "cache_read_input_tokens": -3,
        "cache_creation_input_tokens": 2**90,
    }
    parsed = ClaudeCodeProvider({})._parse_response(
        json.dumps({"result": "valid reply", "usage": bad, "total_cost_usd": "NaN"})
    )
    assert parsed.content == "valid reply" and parsed.tokens_used is None
    await record(store, parsed)
    p = _provider(reply={"choices": [{"message": {"content": "valid API reply"}}], "usage": ["bad"]})
    parsed = await p.complete([Message(role=MessageRole.USER, content="x")])
    assert parsed.content == "valid API reply" and parsed.tokens_used is None
    await record(store, parsed, turn="api")
    _, _, tokens = CodexCLIProvider._parse_events(
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": "bad", "output_tokens": True}}).encode(), ""
    )
    assert tokens is None
    assert len(await rawrows(store)) == 2


async def test_missing_model_counters_do_not_price_entire_resumed_session(store):
    prices = {"p": {"claude-sonnet": dict(input=1, output=2, cache_read=3, cache_write=4)}}
    await record(store, claude(None, models=False), turn="missing-models", prices=prices)
    await record(store, claude(None, fresh=False, scale=50), turn="first-models", prices=prices)
    await record(store, claude(None, fresh=False, scale=51), turn="next-models", prices=prices)
    rows = await rawrows(store)
    assert [r["cost_usd"] for r in rows[:2]] == [None, None]
    assert Decimal(rows[2]["cost_usd"]) == Decimal("0.00041")
    assert rows[2]["tokens_used"] == 200


async def test_failed_capture_and_reopen_cannot_charge_previous_users_missing_turn(store):
    await record(store, claude("1"), turn="first")
    await store.save_message("s", "assistant", "first", usage_turn_id="first", usage_call_count=1)
    # Second turn's optional ledger write was absent, but its reply was saved.
    await store.save_message("s", "assistant", "uncaptured", usage_turn_id="gap", usage_call_count=1)
    await store.spend.close()
    store.spend = await SpendLedger.open(store._db_path)
    await record(store, claude("3", fresh=False, scale=3), turn="third", user="other-user")
    await store.save_message("s", "assistant", "third", usage_turn_id="third", usage_call_count=1)
    await record(store, claude("4", fresh=False, scale=4), turn="fourth")
    rows = await rawrows(store)
    assert [r["cost_usd"] for r in rows] == ["1", None, "1"]
    assert rows[1]["provider_cost_usd"] == "3"  # retained, not charged to other user


async def test_rollback_does_not_advance_baseline_and_breaks_in_memory_chain(store, monkeypatch):
    await record(store, claude("1"), turn="first")
    execute = store.spend.db.execute

    async def fail_insert(sql, *args, **kwargs):
        if sql.startswith("INSERT INTO turn_usage"):
            raise sqlite3.OperationalError("synthetic full disk")
        return await execute(sql, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(store.spend.db, "execute", fail_insert)
        with pytest.raises(sqlite3.OperationalError):
            await record(store, claude("2", fresh=False, scale=2), turn="failed")
    cursor = await store.spend.db.execute("SELECT cost_usd FROM usage_baselines")
    assert (await cursor.fetchone())[0] == "1"
    await record(store, claude("3", fresh=False, scale=3), turn="third")
    assert (await rawrows(store))[-1]["cost_usd"] is None


def zeroed_claude_error(reason="error"):
    data = {
        "is_error": True,
        "subtype": "error_during_execution",
        "result": "synthetic failed turn",
        "session_id": "cli",
        "total_cost_usd": 0,
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
        "modelUsage": {
            "claude-sonnet": {
                "inputTokens": 0,
                "outputTokens": 0,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 0,
            }
        },
    }
    parsed = ClaudeCodeProvider({})._parse_response(json.dumps(data))
    parsed.usage["fresh_session"] = False
    parsed.stop_reason = reason
    return parsed


@pytest.mark.parametrize("reason", ["error", "auth_error", "usage_limit"])
@pytest.mark.parametrize("priced", [False, True])
async def test_decreased_cost_five_zero_six_never_prices_whole_session_for_next_user(store, reason, priced):
    prices = {"p": {"claude-sonnet": dict(input=1, output=2, cache_read=3, cache_write=4)}} if priced else {}
    await record(store, claude("5.00", scale=5), turn="one", user="user-a", prices=prices)
    await record(store, zeroed_claude_error(reason), turn="two", user="user-a", prices=prices)
    cursor = await store.spend.db.execute("SELECT cost_usd,model_usage FROM usage_baselines WHERE cli_session_id='cli'")
    baseline = await cursor.fetchone()
    assert baseline["cost_usd"] is None and json.loads(baseline["model_usage"]) == {}
    await store.spend.close()
    store.spend = await SpendLedger.open(store._db_path)
    await record(store, claude("6.00", fresh=False, scale=6), turn="three", user="user-b", prices=prices)
    rows = await rawrows(store)
    assert [r["provider_cost_usd"] for r in rows] == ["5.00", "0", "6.00"]
    assert [r["cost_usd"] for r in rows] == ["5.00", None, None]
    assert rows[2]["cost_source"] == "unknown"
    report = render_spend(await store.spend.rows(7), 7)
    assert "$5.0000 + ?" in report and "Unknown-cost turns: 2." in report
    own = render_spend(await store.spend.rows(7, requester_id="user-b"), 7, own=True)
    assert "$6.0000" not in own and "unknown" in own
    # Recovery starts at the first trustworthy observation, not the zeroed one.
    await record(store, claude("7.00", fresh=False, scale=7), turn="four", user="user-b", prices=prices)
    assert (await rawrows(store))[-1]["cost_usd"] == "1.00"


@pytest.mark.parametrize("raised", [False, True])
async def test_unparsed_or_raised_turn_breaks_only_its_session_chain_through_meter(store, raised):
    parsed = ClaudeCodeProvider({})._parse_response("plain output without JSON usage")
    assert parsed.usage is None and parsed.session_id is None
    llm = SimpleNamespace(
        complete=AsyncMock(side_effect=RuntimeError("synthetic failure")) if raised else AsyncMock(return_value=parsed)
    )
    manager = SimpleNamespace(storage=store, defaults={}, llm_providers={"alias": llm})
    await record(store, claude("5", scale=5), turn="first", provider="alias")
    for agent, provider, session_id, cli in [
        ("other-agent", "alias", "s", "cli"),
        ("worker", "other-provider", "s", "cli"),
        ("worker", "alias", "other-session", "other-cli"),
    ]:
        await store.get_or_create_session(session_id, agent)
        await store.spend.record(
            turn_id=f"{agent}:{provider}:{session_id}",
            call_index=1,
            session_id=session_id,
            agent_id=agent,
            requester_id="user-a",
            provider=provider,
            model="claude-sonnet",
            response=claude("10", session=cli),
            duration_ms=1,
            turn_duration_ms=1,
            prices={},
        )
    meter = TurnMeter(manager, "s", "worker", "user-a")
    if raised:
        with pytest.raises(RuntimeError):
            await meter.complete(llm, [], session_id="cli")
    else:
        assert await meter.complete(llm, [], session_id="cli") is parsed
    cursor = await store.spend.db.execute("SELECT * FROM usage_baselines")
    baselines = {(r["agent_id"], r["provider"], r["session_id"]): r["cost_usd"] for r in await cursor.fetchall()}
    assert baselines[("worker", "alias", "s")] is None
    assert all(v == "10" for k, v in baselines.items() if k != ("worker", "alias", "s"))
    await store.spend.close()
    store.spend = await SpendLedger.open(store._db_path)
    await record(store, claude("6", fresh=False, scale=6), turn="next", provider="alias", user="user-b")
    assert (await rawrows(store))[-1]["cost_usd"] is None
    # A replay of the already-recorded missing observation cannot undo recovery.
    await store.spend.record(
        turn_id=meter.id,
        call_index=1,
        session_id="s",
        agent_id="worker",
        requester_id="user-a",
        provider="alias",
        model=None,
        response=parsed,
        duration_ms=1,
        turn_duration_ms=1,
        prices={},
    )
    await record(store, claude("7", fresh=False, scale=7), turn="final", provider="alias", user="user-b")
    assert (await rawrows(store))[-1]["cost_usd"] == "1"


async def test_known_cost_with_missing_model_counters_marks_tokens_partial_durably(store):
    # Main-loop fields are all known, but subagent tokens are not represented.
    await record(store, claude("2.00", models=False), turn="no-models")
    await store.spend.close()
    store.spend = await SpendLedger.open(store._db_path)
    (row,) = await rawrows(store)
    assert all(row[f] is not None for f in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"))
    assert json.loads(row["reported_usage"])["token_scope"] == "main_loop"
    report = render_spend(await store.spend.rows(7), 7)
    assert "200 + ?" in report and "$2.0000" in report
    assert "subagent tokens are unconfirmed" in report
    assert "Unknown-cost turns: 0." in report  # token uncertainty is independent of reported cost


async def test_first_resume_main_loop_tokens_are_partial_until_next_model_delta(store):
    await record(store, claude("5", fresh=False, scale=5), turn="unknown-baseline")
    await record(store, claude("6", fresh=False, scale=6), turn="confirmed-delta")
    first, second = await rawrows(store)
    assert json.loads(first["reported_usage"])["token_scope"] == "main_loop"
    assert "token_scope" not in json.loads(second["reported_usage"])
    assert "400 + ?" in render_spend(await store.spend.rows(7), 7)


async def test_unchanged_cumulative_model_counters_are_known_zero(store):
    await record(store, claude("5", scale=5), turn="first")
    await record(store, claude("5", fresh=False, scale=5), turn="unchanged")
    row = (await rawrows(store))[-1]
    assert row["tokens_used"] == 0 and row["cost_usd"] == "0"
    assert "token_scope" not in json.loads(row["reported_usage"])


async def test_decreased_model_counters_cannot_rebase_table_prices(store):
    prices = {"p": {"claude-sonnet": dict(input=1, output=2, cache_read=3, cache_write=4)}}
    await record(store, claude(None, scale=5), turn="first", prices=prices)
    await record(store, claude(None, fresh=False, scale=0), turn="zeroed", prices=prices)
    await record(store, claude(None, fresh=False, scale=6), turn="next", prices=prices)
    assert [r["cost_usd"] for r in (await rawrows(store))[1:]] == [None, None]


async def test_failed_unparsed_capture_without_cli_id_breaks_next_cost_chain(store, monkeypatch):
    await record(store, claude("5"), turn="first")
    original_execute = store.spend.db.execute

    async def fail_insert(sql, *args, **kwargs):
        if sql.startswith("INSERT INTO turn_usage"):
            raise sqlite3.OperationalError("synthetic capture failure")
        return await original_execute(sql, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(store.spend.db, "execute", fail_insert)
        with pytest.raises(sqlite3.OperationalError):
            await record(store, ClaudeCodeProvider({})._parse_response("unparsed"), turn="missing")
    await record(store, claude("6", fresh=False, scale=2), turn="next")
    assert (await rawrows(store))[-1]["cost_usd"] is None
