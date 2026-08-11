"""NO_REPLY abstain sentinel — replies that must be dropped instead of posted."""

from src.core.agent_manager import is_no_reply


def test_bare_sentinel():
    assert is_no_reply("NO_REPLY")
    assert is_no_reply("no_reply")
    assert is_no_reply("  NO_REPLY  ")


def test_wrapped_sentinel():
    assert is_no_reply("(NO_REPLY)")
    assert is_no_reply("**NO_REPLY**")
    assert is_no_reply("`NO_REPLY`")
    assert is_no_reply("> NO_REPLY")
    assert is_no_reply('"NO_REPLY"')


def test_sentinel_with_trailing_reason():
    # Models often can't resist explaining themselves — still an abstain.
    assert is_no_reply("NO_REPLY — standby restatement, nothing to add")
    assert is_no_reply("NO_REPLY (acknowledgement only)")


def test_normal_content_not_suppressed():
    assert not is_no_reply("Deployed to production, all green.")
    assert not is_no_reply("No reply needed from them, but here's my update.")
    assert not is_no_reply("Holding. Silent.")
    assert not is_no_reply("")
    assert not is_no_reply("NO_REPLYING is not a word")   # word boundary respected


def test_sentinel_mentioned_mid_text_not_suppressed():
    assert not is_no_reply("If you have nothing to add, reply NO_REPLY.")
