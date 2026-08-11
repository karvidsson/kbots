"""Skill loader — reads YAML skill definitions and makes them available to agents and slash commands."""

import logging
from pathlib import Path

import yaml

from src.core.base import Skill, SkillParam

logger = logging.getLogger(__name__)

# Global skill registry
_skill_registry: dict[str, Skill] = {}


def load_skills(skills_dir: str | Path = "skills") -> dict[str, Skill]:
    """Load all skill YAML files from a skills directory.

    Called multiple times during discovery — once per layer (Core, modules, overlay).
    Later calls override earlier entries with the same name.
    Agent-specific skills (agents/*/skills/) are loaded by the registry, not here.
    """
    skills_path = Path(skills_dir)
    if not skills_path.exists():
        return {}

    loaded = 0
    for yaml_file in sorted(skills_path.glob("**/*.yaml")):
        try:
            skill = _load_skill_file(yaml_file)
            _skill_registry[skill.name] = skill
            loaded += 1
        except Exception as e:
            logger.error(f"Failed to load skill {yaml_file}: {e}")

    if loaded:
        logger.info(f"Loaded {loaded} skills from {skills_path}")
    return dict(_skill_registry)


def _load_skill_file(path: Path) -> Skill:
    """Parse a single skill YAML file into a Skill object."""
    with open(path) as f:
        data = yaml.safe_load(f)

    if not data or "name" not in data:
        raise ValueError(f"Skill file {path} missing 'name' field")

    params = []
    if "parameters" in data:
        raw_params = data["parameters"]
        if isinstance(raw_params, list):
            # List format: [{name: topic, type: string, ...}, ...]
            for item in raw_params:
                params.append(SkillParam(
                    name=item["name"],
                    type=item.get("type", "string"),
                    description=item.get("description", ""),
                    required=item.get("required", False),
                    choices=item.get("choices"),
                ))
        elif isinstance(raw_params, dict):
            # Dict format: {topic: {type: string, ...}, ...}
            for param_name, param_def in raw_params.items():
                if isinstance(param_def, str):
                    params.append(SkillParam(name=param_name, type=param_def))
                elif isinstance(param_def, dict):
                    params.append(SkillParam(
                        name=param_name,
                        type=param_def.get("type", "string"),
                        description=param_def.get("description", ""),
                        required=param_def.get("required", False),
                        choices=param_def.get("choices"),
                    ))

    llm = data.get("llm")
    if llm is not None and not isinstance(llm, dict):
        raise ValueError(f"Skill file {path}: 'llm' must be a mapping (provider/model)")

    return Skill(
        name=data["name"],
        description=data.get("description", ""),
        prompt=data.get("prompt", ""),
        tools=data.get("tools", []),
        parameters=params,
        command=data.get("command"),
        llm=llm,
        restrict_tools=bool(data.get("restrict_tools", False)),
        max_rounds=int(data.get("max_rounds", 0) or 0),
    )


def get_skill(name: str) -> Skill | None:
    """Get a registered skill by name."""
    return _skill_registry.get(name)


def get_all_skills() -> dict[str, Skill]:
    """Get all registered skills."""
    return dict(_skill_registry)


def render_skill_prompt(skill: Skill, params: dict[str, str]) -> str:
    """Render a skill's prompt template with the given parameters."""
    prompt = skill.prompt
    for key, value in params.items():
        prompt = prompt.replace(f"{{{key}}}", str(value))
    return prompt
