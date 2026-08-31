"""The sanitize alert has to carry signal, not volume.

The alert it replaces said "would remove N chars (hidden/invisible content)".
On any real page that N is stripped markup and collapsed whitespace, so the
label was wrong and the number was unreadable: a reader could not tell a news
site from a smuggled payload, and the code's own open question, whether to move
from log-only to active stripping, could not be answered from the alerts
because one figure conflated "had tags" with "had hidden characters".
"""

import re
import unicodedata

import pytest

from src.core.alert_details import AlertDetailStore, render_detail
from src.core.alerts import describe_target
from src.core.content_safety import (
    SANITIZE_SIGNAL_CATEGORIES,
    format_sanitize_detail,
    sanitize,
    sanitize_report,
)

ZWSP = "​"
BIDI = "‮"


def _original_sanitize(text: str) -> str:
    """The implementation as it stood before instrumentation.

    Kept verbatim so the refactor is pinned against the behaviour it replaced
    rather than against its own output. Instrumenting a pass is exactly the
    kind of change that quietly alters what it produces.
    """
    if not text:
        return text
    text = "".join(
        c for c in text
        if unicodedata.category(c) not in ("Cf", "Cc", "Co")
        or c in ("\n", "\r", "\t")
    )
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    import html
    text = html.unescape(text)
    text = re.sub(r"data:[a-zA-Z/+]+;base64,[A-Za-z0-9+/=]{200,}", "[base64-removed]", text)
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


SAMPLES = [
    "",
    "plain text with nothing to strip",
    "<p>hello</p>\n\n\n<b>world</b>",
    f"visible{ZWSP}{BIDI}text",
    "<script>alert(1)</script><style>a{color:red}</style>body",
    "café vs café",
    "a   b\t\tc",
    "data:image/png;base64," + "A" * 300,
    "&amp;&lt;tag&gt; &nbsp;entities",
    "<div>​ nested <span>tags</span> </div>\n\n\n\ntail",
]


@pytest.mark.parametrize("text", SAMPLES)
def test_sanitize_output_is_unchanged_by_instrumentation(text):
    assert sanitize(text) == _original_sanitize(text)


def test_a_plain_html_page_raises_no_concealment_signal():
    """The false positive that made the old alert noise."""
    page = "<html><body>" + "<p>ordinary paragraph text</p>\n\n\n" * 40 + "</body></html>"
    report = sanitize_report(page)
    assert report.total_removed > 50, "this page really does lose a lot of markup"
    assert report.signal_removed == 0, "and none of it is concealment"


def test_hidden_characters_are_counted_apart_from_markup():
    page = f"<p>visible{ZWSP * 5}{BIDI}</p>"
    report = sanitize_report(page)
    assert report.removed["invisible"] == 6
    assert report.signal_removed == 6
    assert report.removed.get("markup", 0) > 0
    # The distinction the old single number destroyed.
    assert report.signal_removed < report.total_removed


def test_base64_blobs_count_as_concealment():
    report = sanitize_report("data:image/png;base64," + "A" * 400)
    assert report.removed.get("base64", 0) > 0
    assert report.signal_removed > 0


def test_summary_names_categories_rather_than_a_bare_total():
    report = sanitize_report(f"<p>{ZWSP}text</p>")
    assert "invisible" in report.summary()
    assert "markup" in report.summary()


def test_invisible_samples_are_code_points_not_the_characters():
    """Printing an invisible character shows nothing and proves nothing."""
    report = sanitize_report(f"a{ZWSP}b")
    samples = report.samples["invisible"]
    assert samples and samples[0].startswith("U+200B")
    assert "ZERO WIDTH SPACE" in samples[0]


def test_detail_shows_concealed_content_and_only_counts_for_the_rest():
    page = f"<p>{ZWSP}hi</p>\n\n\n\n"
    detail = format_sanitize_detail(sanitize_report(page))
    assert "U+200B" in detail
    assert "invisible" in detail
    # Markup is accounted for but its content is never carried.
    assert "Not shown" in detail
    assert "<p>" not in detail


def test_only_signal_categories_retain_samples():
    page = "<script>" + "x" * 500 + "</script>"
    report = sanitize_report(page)
    assert set(report.samples) <= set(SANITIZE_SIGNAL_CATEGORIES)


# --- the alert's source line ---

def test_describe_target_prefers_the_url():
    assert describe_target("read_url", {"url": "https://example.com/a"}) == "https://example.com/a"


def test_describe_target_falls_back_to_the_query():
    assert describe_target("web_search", {"query": "kbots"}) == "kbots"


def test_describe_target_survives_a_tool_naming_its_target_something_else():
    """A new web-facing tool must not silently lose attribution."""
    assert describe_target("browser", {"destination": "https://example.com"}) \
        == "https://example.com"


def test_describe_target_truncates():
    out = describe_target("read_url", {"url": "https://x.test/" + "a" * 400}, limit=40)
    assert len(out) == 40 and out.endswith("…")


def test_describe_target_is_empty_when_there_is_nothing_to_name():
    assert describe_target("read_url", {}) == ""
    assert describe_target("read_url", {"timeout": 30}) == ""


# --- the held detail ---

def test_detail_round_trips_through_the_store(tmp_path):
    store = AlertDetailStore(tmp_path)
    store.put("12345", "Sanitize detail", "hidden stuff", {"tool": "read_url"})
    record = store.get("12345")
    assert record["detail"] == "hidden stuff"
    assert record["meta"]["tool"] == "read_url"


def test_reading_the_detail_does_not_consume_it(tmp_path):
    """Two people look at the same alert; the second must not find it gone."""
    store = AlertDetailStore(tmp_path)
    store.put("1", "k", "d")
    assert store.get("1") is not None
    assert store.get("1") is not None


def test_expired_detail_is_gone(tmp_path):
    import os
    import time
    store = AlertDetailStore(tmp_path, ttl_hours=1)
    store.put("1", "k", "d")
    path = next(tmp_path.glob("*.json"))
    old = time.time() - 7200
    os.utime(path, (old, old))
    assert store.get("1") is None


def test_store_is_capped(tmp_path):
    store = AlertDetailStore(tmp_path, max_files=5)
    for i in range(20):
        store.put(str(i), "k", "d")
    assert len(list(tmp_path.glob("*.json"))) <= 5


def test_payload_is_capped(tmp_path):
    from src.core.alert_details import MAX_PAYLOAD_CHARS
    store = AlertDetailStore(tmp_path)
    store.put("1", "k", "x" * (MAX_PAYLOAD_CHARS * 2))
    record = store.get("1")
    assert len(record["detail"]) == MAX_PAYLOAD_CHARS
    assert record["truncated"] is True


def test_a_message_id_cannot_escape_the_store_directory(tmp_path):
    store = AlertDetailStore(tmp_path)
    store.put("../../etc/passwd", "k", "d")
    assert not (tmp_path.parent.parent / "etc").exists()
    assert list(tmp_path.glob("*.json"))


# --- rendering untrusted content ---

def test_reveal_fences_the_content():
    out = render_detail({"kind": "Sanitize detail", "detail": "plain", "meta": {}})
    assert "```" in out


def test_reveal_defuses_a_fence_break_out():
    """Stripped content that closes the fence would render as live markdown."""
    out = render_detail({"kind": "k", "detail": "```\n# HEADING\n```", "meta": {}})
    body = out.split("```")[1] if out.count("```") >= 2 else out
    assert "```" not in body


def test_reveal_defuses_mentions():
    out = render_detail({"kind": "k", "detail": "@everyone @here <@1234>", "meta": {}})
    assert "@everyone" not in out
    assert "@here" not in out


def test_reveal_truncates_and_says_so():
    out = render_detail({"kind": "k", "detail": "x" * 5000, "meta": {}}, limit=100)
    assert "truncated" in out
    assert len(out) < 400


def test_reveal_names_the_tool_and_agent():
    out = render_detail({"kind": "Sanitize detail", "detail": "d",
                         "meta": {"tool": "read_url", "agent": "rainmaker"}})
    assert "read_url" in out and "rainmaker" in out


# --- the emoji collision ---
#
# The reveal reuses 🔍, which the reply shortener already claimed. Its handler
# returned unconditionally on that emoji, so a 🔍 on any message it had not
# shortened was swallowed: the reveal would have been dead on arrival with
# nothing in the logs to explain it.

class _FakeStore:
    def __init__(self, payloads=None):
        self.payloads = payloads or {}
        self.taken = []

    def take(self, message_id):
        self.taken.append(message_id)
        return self.payloads.pop(message_id, None)


async def _run_reaction(handler_self, message_id, user_id, emoji="\U0001f50d"):
    import types as _t
    payload = _t.SimpleNamespace(user_id=user_id, message_id=message_id,
                                 channel_id="555", emoji=emoji)
    await handler_self.on_raw_reaction_add(payload)


def _handler(tmp_path, shortener_payloads, admin_users, sent):
    import types as _t

    from src.connectors.discord import DiscordBot

    handler = DiscordBot.__new__(DiscordBot)
    handler.client = _t.SimpleNamespace(user=_t.SimpleNamespace(id=999))
    handler.account_name = "main"
    handler.admin_users = admin_users

    async def _send(channel_id, content, **kw):
        sent.append(content)

    handler.connector = _t.SimpleNamespace(
        _shortener=_t.SimpleNamespace(emoji="\U0001f50d",
                                      store=_FakeStore(shortener_payloads)),
        send=_send,
        _hitl=None,
    )
    return handler


@pytest.mark.asyncio
async def test_reveal_fires_when_the_shortener_has_nothing_stored(tmp_path, monkeypatch):
    import src.core.alert_details as ad
    monkeypatch.setattr(ad, "_default_dir", lambda: tmp_path)
    ad.AlertDetailStore(tmp_path).put("77", "Sanitize detail", "U+200B ZERO WIDTH SPACE")

    sent = []
    handler = _handler(tmp_path, {}, ["42"], sent)
    await _run_reaction(handler, "77", 42)

    assert sent, "the reveal never fired — the shortener swallowed the reaction"
    assert "ZERO WIDTH SPACE" in sent[0]


@pytest.mark.asyncio
async def test_the_shortener_still_wins_for_its_own_messages(tmp_path, monkeypatch):
    import src.core.alert_details as ad
    monkeypatch.setattr(ad, "_default_dir", lambda: tmp_path)

    sent = []
    handler = _handler(tmp_path, {"88": "the rest of the reply"}, ["42"], sent)
    await _run_reaction(handler, "88", 42)

    assert sent == ["the rest of the reply"]


@pytest.mark.asyncio
async def test_a_non_admin_gets_no_reveal(tmp_path, monkeypatch):
    """The detail is attacker-controlled text; not everyone who can see the
    alert channel should be able to print it into it."""
    import src.core.alert_details as ad
    monkeypatch.setattr(ad, "_default_dir", lambda: tmp_path)
    ad.AlertDetailStore(tmp_path).put("77", "Sanitize detail", "secret")

    sent = []
    handler = _handler(tmp_path, {}, ["42"], sent)
    await _run_reaction(handler, "77", 1234)

    assert sent == []
