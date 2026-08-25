"""Every shipped skill fixture must be runnable and must name real tools.

scripts/eval_skill.py has done real trap-fixture scoring since it was written,
and until 2026-08-22 there was not a single fixture file anywhere in the repo.
A skill could be created, shipped and never evaluated, and nobody would see a
failure, because there was nothing to run.

These tests are the cheap half of that: static, offline, no provider. They
cannot tell you whether a model picks the right tool, only that the fixture is
well-formed and that the tool it expects is one the skill is actually allowed
to call. Without that, a fixture drifts silently when a skill's tool list
changes and the eval fails for a reason that has nothing to do with the model.
"""

import importlib.util

import pytest
import yaml

from src.core.base import PROJECT_ROOT

SKILLS_DIR = PROJECT_ROOT / "skills"
FIXTURES_DIR = SKILLS_DIR / "fixtures"

SCRIPT = PROJECT_ROOT / "scripts" / "eval_skill.py"
_spec = importlib.util.spec_from_file_location("eval_skill", SCRIPT)
eval_skill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eval_skill)

FIXTURE_FILES = sorted(FIXTURES_DIR.glob("*.jsonl"))


def _skill(name: str) -> dict:
    return yaml.safe_load((SKILLS_DIR / f"{name}.yaml").read_text())


def test_there_are_fixtures_at_all():
    """The state this suite exists to leave behind. Zero fixtures means the
    eval harness is dead code, however good it is.
    """
    assert FIXTURE_FILES, f"no skill fixtures in {FIXTURES_DIR}"


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.stem)
def test_the_fixture_parses_with_the_harness_that_will_run_it(path):
    """Validated by eval_skill's own loader, not by a second copy of the rules
    written here, so a change to the format cannot pass this and fail the run.
    """
    fixtures = eval_skill.load_fixtures(path)
    assert fixtures


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.stem)
def test_the_fixture_names_a_skill_that_exists(path):
    assert (SKILLS_DIR / f"{path.stem}.yaml").is_file(), (
        f"{path.name} has no matching skill; rename it or delete it")


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.stem)
def test_expected_tools_are_ones_the_skill_may_call(path):
    """The drift this catches: a skill drops or renames a tool, the fixture
    still expects it, and the eval reports the model getting it wrong.
    """
    allowed = _skill(path.stem).get("tools") or []
    for fx in eval_skill.load_fixtures(path):
        tool = fx.get("expect_tool")
        if tool:
            assert tool in allowed, (
                f"{path.name}: expects {tool!r}, which {path.stem} cannot call "
                f"(allowed: {allowed})")


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.stem)
def test_every_fixture_set_has_a_trap(path):
    """A set with no trap measures only eagerness. An agent that calls its tool
    on every input, including "thanks", scores 100% on a trapless set and is
    the exact failure the harness was built to catch.
    """
    fixtures = eval_skill.load_fixtures(path)
    assert any(fx.get("expect_no_tool") for fx in fixtures), (
        f"{path.name} has no trap fixture")


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.stem)
def test_a_fixture_set_is_big_enough_to_mean_something(path):
    """--min-pass is a ratio. On three fixtures one failure is 33%, so the
    threshold cannot express anything useful.
    """
    assert len(eval_skill.load_fixtures(path)) >= 4, f"{path.name} is too small"


def test_fixture_inputs_are_not_duplicated_within_a_set():
    for path in FIXTURE_FILES:
        inputs = [fx["input"].strip().lower() for fx in eval_skill.load_fixtures(path)]
        assert len(inputs) == len(set(inputs)), f"{path.name} repeats an input"
