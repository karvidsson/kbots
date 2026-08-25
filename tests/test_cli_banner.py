"""The CLI banner is built, not hand-drawn, and says what kbots is now.

Two problems, both from drawing a box by hand in two files:

  - the tagline said "The Agent Routing System". Routing is one line of what
    kbots does. It is a team of persistent agents with memory, tools, schedules
    and human-in-the-loop approval, and README and pyproject had already
    settled on a better line that the banners did not use.
  - settings.py rendered 38 characters inside a 40-character border, so it
    printed visibly crooked. Nobody notices a wrong space count in a string
    literal; centring in code makes it impossible.
"""

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


def _load(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(params=[("setup.py", "setup_mod"), ("scripts/settings.py", "settings_mod")])
def cli(request):
    return _load(*request.param)


def _rendered(mod, capsys, **kwargs):
    mod.banner(**kwargs)
    out = capsys.readouterr().out
    plain = re.sub(r"\033\[[0-9;]*m", "", out)
    return [ln for ln in plain.splitlines() if ln.strip()]


def test_every_line_of_the_box_is_the_same_width(cli, capsys):
    """The defect: a border and a body that disagreed."""
    lines = _rendered(cli, capsys)
    widths = {len(ln) for ln in lines}
    assert len(widths) == 1, f"ragged box, widths {sorted(widths)}: {lines}"


def test_the_box_is_actually_closed(cli, capsys):
    lines = _rendered(cli, capsys)
    assert lines[0].startswith("╔") and lines[0].endswith("╗")
    assert lines[-1].startswith("╚") and lines[-1].endswith("╝")
    for mid in lines[1:-1]:
        assert mid.startswith("║") and mid.endswith("║")


def test_the_tagline_makes_the_same_claims_as_pyproject(cli):
    """Three variants of the same sentence is two too many.

    Matched on the claims rather than the exact string: punctuation differs
    between a TOML description and a boxed banner, and an earlier version of
    this test broke when README's image alt-text was reworded, which is not a
    drift anyone needed telling about.
    """
    canonical = (ROOT / "pyproject.toml").read_text().lower()
    for claim in ("one process", "llm-agnostic", "trains itself"):
        assert claim in cli.TAGLINE.lower(), f"banner dropped {claim!r}"
        assert claim in canonical, f"pyproject no longer claims {claim!r}"


def test_the_old_tagline_is_gone_everywhere():
    for path in ("setup.py", "scripts/settings.py"):
        assert "Agent Routing System" not in (ROOT / path).read_text()


def test_a_long_title_does_not_break_the_box(cli, capsys):
    """Retitling is the change that broke alignment last time."""
    lines = _rendered(cli, capsys, title="kbots Something With A Much Longer Name")
    assert len({len(ln) for ln in lines}) == 1
    assert "kbots Something With A Much Longer Name" in "\n".join(lines)


def test_the_title_and_tagline_both_appear(cli, capsys):
    text = "\n".join(_rendered(cli, capsys))
    assert "kbots" in text
    assert cli.TAGLINE in text
