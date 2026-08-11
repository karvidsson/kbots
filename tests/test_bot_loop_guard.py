"""Bot-to-bot loop guard — content + rate detection with a hard-mute cooldown."""

from types import SimpleNamespace

from src.connectors.discord import (
    _BOT_CHAIN_DECAY,
    _BOT_CHAIN_LIMIT,
    _BOT_LOOP_COOLDOWN,
    _BOT_LOOP_MAX_HITS,
    _BOT_REPEAT_LIMIT,
    DiscordBot,
    _normalize_bot_content,
)


def _client(config: dict | None = None):
    c = DiscordBot.__new__(DiscordBot)
    c.account_name = "test"
    c._bot_loop_hits = {}
    c._bot_cooldown = {}
    c._bot_recent_content = {}
    c._bot_chain = {}
    c.connector = SimpleNamespace(config=config or {})
    return c


def test_normalize_strips_mentions_punct_emoji():
    assert _normalize_bot_content("<@123> (idle)") == "idle"
    assert _normalize_bot_content("<@!456> Standing by. 🏠") == "standing by"
    assert _normalize_bot_content("<@&789>") == ""              # bare mention → empty
    assert _normalize_bot_content("Real update A") == "real update a"


def test_single_contentless_ack_allowed():
    c = _client()
    suppress, _ = c._bot_loop_check(1, "<@1>", now=0.0)   # normalizes to ""
    assert not suppress   # a one-off mention-only/short reply is not a loop


def test_repeated_contentless_ack_trips():
    c = _client()
    results = [c._bot_loop_check(1, "<@1>", now=float(i)) for i in range(4)]
    trips = [i for i, (s, r) in enumerate(results) if s and r]
    assert trips and trips[0] == _BOT_REPEAT_LIMIT   # only repeated empties trip
    assert results[trips[0]][1] == "repeated/empty acknowledgements"


def test_repeated_ack_trips_at_repeat_limit():
    c = _client()
    results = [c._bot_loop_check(2, "<@2> (idle)", now=float(i)) for i in range(5)]
    trips = [i for i, (s, r) in enumerate(results) if s and r]
    assert trips and trips[0] == _BOT_REPEAT_LIMIT   # 3rd identical ack trips it


def test_rate_limit_trips_on_distinct_content():
    c = _client()
    res = [c._bot_loop_check(3, f"distinct msg {i}", now=float(i))
           for i in range(_BOT_LOOP_MAX_HITS + 2)]
    assert not res[0][0]              # first is allowed
    assert any(s for s, _ in res)     # rate guard trips once the window overflows


def test_cooldown_is_a_hard_mute():
    c = _client()
    c._bot_loop_check(4, "<@4>", now=0.0)             # first empty — allowed
    c._bot_loop_check(4, "<@4>", now=1.0)
    s, r = c._bot_loop_check(4, "<@4>", now=2.0)      # 3rd identical empty → trip
    assert s and r
    s, r = c._bot_loop_check(4, "a real substantive message", now=10.0)
    assert s and r is None                            # muted silently within cooldown
    s2, _ = c._bot_loop_check(4, "later message", now=_BOT_LOOP_COOLDOWN + 3.0)
    assert not s2                                     # allowed again after cooldown


def test_legit_slow_substantive_exchange_not_suppressed():
    c = _client()
    for i in range(3):
        s, _ = c._bot_loop_check(5, f"distinct point {i}", now=i * 20.0)
        assert not s


# --- Chain breaker: slow, paraphrased loops the rate/repeat guard can't see ---


def test_chain_suppresses_past_limit():
    c = _client()
    results = [c._bot_chain_check(100, from_bot=True, now=float(i))
               for i in range(_BOT_CHAIN_LIMIT + 3)]
    assert not any(results[:_BOT_CHAIN_LIMIT])   # first N turns pass
    assert all(results[_BOT_CHAIN_LIMIT:])       # everything after is muted


def test_human_message_resets_chain():
    c = _client()
    for i in range(_BOT_CHAIN_LIMIT + 2):
        c._bot_chain_check(200, from_bot=True, now=float(i))
    assert c._bot_chain_check(200, from_bot=True, now=100.0)         # muted
    assert not c._bot_chain_check(200, from_bot=False, now=101.0)    # human never suppressed
    assert not c._bot_chain_check(200, from_bot=True, now=102.0)     # chain restarted


def test_chain_is_per_channel():
    c = _client()
    for i in range(_BOT_CHAIN_LIMIT + 1):
        c._bot_chain_check(300, from_bot=True, now=float(i))
    assert c._bot_chain[300][0] > _BOT_CHAIN_LIMIT
    assert not c._bot_chain_check(301, from_bot=True, now=0.0)       # other channel unaffected


def test_chain_limit_configurable():
    c = _client(config={"bot_chain_limit": 2})
    assert not c._bot_chain_check(400, from_bot=True, now=0.0)
    assert not c._bot_chain_check(400, from_bot=True, now=1.0)
    assert c._bot_chain_check(400, from_bot=True, now=2.0)           # 3rd turn muted


def test_chain_self_heals_after_decay_gap():
    """A quiet gap longer than the decay window resets a tripped chain — no human
    needed. This is what lets genuine paused-then-resumed collaboration recover."""
    c = _client()
    for i in range(_BOT_CHAIN_LIMIT + 1):
        c._bot_chain_check(500, from_bot=True, now=float(i))
    muted_at = float(_BOT_CHAIN_LIMIT + 1)
    assert c._bot_chain_check(500, from_bot=True, now=muted_at)      # muted
    assert not c._bot_chain_check(500, from_bot=True,
                                  now=muted_at + _BOT_CHAIN_DECAY + 1)  # self-healed


def test_chain_decay_configurable():
    c = _client(config={"bot_chain_decay_seconds": 10})
    for i in range(_BOT_CHAIN_LIMIT + 1):
        c._bot_chain_check(600, from_bot=True, now=float(i))
    muted_at = float(_BOT_CHAIN_LIMIT + 1)
    assert c._bot_chain_check(600, from_bot=True, now=muted_at)      # muted
    assert not c._bot_chain_check(600, from_bot=True, now=muted_at + 11)  # gap > 10s resets
