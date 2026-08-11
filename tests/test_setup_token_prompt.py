"""Regression: a Discord token pasted at the yes/no prompt must not be
mistaken for 'no' and silently skipped (setup.py Step 7 trap)."""

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "setup_mod", Path(__file__).resolve().parent.parent / "setup.py")
setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup)


def test_real_token_detected():
    tokens = [
        "MTAwMDAwMDAwMDAwMDAwMDAwMA.FakeTs.NotARealToken-JustAFixture0123456",
        "OTAxMjM0NTY3ODkwMTIzNDU2.YzAbc.dEf-GhIjKlMnOpQrStUvWxYz012345678",
    ]
    for t in tokens:
        assert setup._looks_like_discord_token(t), t


def test_yes_no_not_detected():
    for s in ("y", "yes", "n", "no", "", "  ", "maybe", "1234567890"):
        assert not setup._looks_like_discord_token(s), s
