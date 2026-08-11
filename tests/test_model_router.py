"""Model-tier router — escalation rules, classification, fail-open, session apply."""

from src.core.base import LLMResponse
from src.core.model_router import ModelRouter, RouteDecision


class _FakeLocal:
    """Stands in for the `local` provider; returns a canned classifier reply."""
    def __init__(self, content="", error=False, exc=None):
        self._content, self._error, self._exc = content, error, exc

    async def complete(self, messages, tools=None, stream=False, **kwargs):
        if self._exc:
            raise self._exc
        return LLMResponse(content=self._content,
                           stop_reason="error" if self._error else "end")


class _FakeSession:
    cli_session_id = None
    routed_local = False


def _router(local=None):
    providers = {"claude_code": object()}
    if local is not None:
        providers["local"] = local
    return ModelRouter(providers)


CFG = {"enabled": True, "confidence": 0.75}


# --- deterministic escalation rules (no classifier call) ---

async def test_rules_escalate_to_claude():
    r = _router(_FakeLocal('{"route": "simple", "confidence": 0.99}'))
    assert (await r.route("hi", True, CFG)).reason == "attachments"
    assert (await r.route("⏰ Scheduled task: check prices", False, CFG)).reason == "scheduled task"
    assert (await r.route("fix this ```py\nx=1\n```", False, CFG)).reason == "code block"
    assert (await r.route("x" * 700, False, CFG)).reason == "long message"


async def test_no_local_provider_escalates():
    d = await _router(local=None).route("hi", False, CFG)
    assert d.target == "claude" and "no local provider" in d.reason


async def test_action_directive_escalates_before_classifier():
    # Classifier would confidently say "simple" — the deterministic guard must
    # win, because the local workhorse has no CLI/repo access and answers
    # build directives with promises it cannot execute.
    r = _router(_FakeLocal('{"route": "simple", "confidence": 1.0}'))
    for msg in [
        "you build what you think is best for the user experience",
        "go implement the wizard",
        "please fix the failing tests and open a PR",
        "kan du bygga klart appen?",
        "deploya när du är klar",
    ]:
        d = await r.route(msg, False, CFG)
        assert d.target == "claude" and d.reason == "action directive", msg


async def test_plain_chat_still_reaches_classifier():
    r = _router(_FakeLocal('{"route": "simple", "confidence": 0.95}'))
    assert (await r.route("thanks, looks great!", False, CFG)).target == "local"


# --- classification ---

async def test_confident_simple_goes_local():
    r = _router(_FakeLocal('{"route": "simple", "confidence": 0.92}'))
    d = await r.route("thanks, that worked!", False, CFG)
    assert d.target == "local"


async def test_complex_goes_claude():
    r = _router(_FakeLocal('{"route": "complex", "confidence": 0.9}'))
    assert (await r.route("deploy the fix", False, CFG)).target == "claude"


async def test_low_confidence_simple_escalates():
    r = _router(_FakeLocal('{"route": "simple", "confidence": 0.6}'))
    assert (await r.route("hmm what about that thing", False, CFG)).target == "claude"


async def test_classifier_junk_or_error_fails_open():
    assert (await _router(_FakeLocal("sure! happy to help")).route("hi", False, CFG)).target == "claude"
    assert (await _router(_FakeLocal(error=True, content="down")).route("hi", False, CFG)).target == "claude"
    assert (await _router(_FakeLocal(exc=RuntimeError("boom"))).route("hi", False, CFG)).target == "claude"


async def test_json_extracted_from_chatty_reply():
    r = _router(_FakeLocal('Here you go: {"route": "simple", "confidence": 0.9} hope that helps'))
    assert (await r.route("hello!", False, CFG)).target == "local"


# --- session apply (switching safety) ---

def test_apply_local_sets_flag():
    s = _FakeSession()
    assert ModelRouter.apply(RouteDecision("local", "simple @0.9"), s) is True
    assert s.routed_local is True


def test_escalation_after_local_clears_stale_cli_session():
    s = _FakeSession()
    s.routed_local, s.cli_session_id = True, "cli-123"   # local turns happened
    assert ModelRouter.apply(RouteDecision("claude", "complex"), s) is False
    assert s.cli_session_id is None                       # rebuilt from history
    assert s.routed_local is False


def test_claude_only_session_keeps_cli_session():
    s = _FakeSession()
    s.cli_session_id = "cli-123"                          # no local turns
    ModelRouter.apply(RouteDecision("claude", "complex"), s)
    assert s.cli_session_id == "cli-123"                  # resume as normal


async def test_active_claude_thread_is_sticky():
    """Mid-task follow-ups must not downgrade to the toolless local model
    (regression: 'approve' routed to local → confabulated tool execution)."""
    from src.core.model_router import ModelRouter

    class S:
        cli_session_id = "sess-123"

    r = ModelRouter({"local": object()})
    d = await r.route("aprrove", False, {"enabled": True}, session=S())
    assert d.target == "claude"
    assert "active claude thread" in d.reason


async def test_sticky_can_be_disabled_and_fresh_sessions_unaffected():
    from src.core.model_router import ModelRouter

    class Fresh:
        cli_session_id = None

    r = ModelRouter({})
    d = await r.route("hi", False, {"sticky_claude": True}, session=Fresh())
    assert d.reason == "no local provider"  # fell through the sticky rule
    class Active:
        cli_session_id = "x"
    d2 = await r.route("hi", False, {"sticky_claude": False}, session=Active())
    assert d2.reason == "no local provider"  # sticky disabled → fell through
