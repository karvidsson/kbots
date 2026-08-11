"""The /codify skill — parses cleanly and wires the propose-then-create loop."""

from src.core.base import PROJECT_ROOT
from src.core.skills import _load_skill_file


def test_codify_skill_parses():
    skill = _load_skill_file(PROJECT_ROOT / "skills" / "codify.yaml")
    assert skill.name == "codify"
    # optional focus param — /codify must work with no arguments
    assert [p.name for p in skill.parameters] == ["focus"]
    assert not skill.parameters[0].required


def test_codify_skill_has_creation_and_memory_tools():
    skill = _load_skill_file(PROJECT_ROOT / "skills" / "codify.yaml")
    for name in ("create_skill", "create_tool", "list_capabilities",
                 "memory_search", "remember_lesson"):
        assert name in skill.tools


def test_codify_prompt_proposes_before_creating():
    skill = _load_skill_file(PROJECT_ROOT / "skills" / "codify.yaml")
    # the approval gate is the point of the skill — guard the wording
    assert "Propose, don't install" in skill.prompt
    assert "$KBOTS_TMP" in skill.prompt  # asset-pinning warning


def test_codify_prompt_defaults_to_pipeline_tool():
    skill = _load_skill_file(PROJECT_ROOT / "skills" / "codify.yaml")
    # deterministic work goes in ONE tool; the skill stays a thin wrapper
    assert "Default to a pipeline tool" in skill.prompt
    assert "code over prompt" in skill.prompt
