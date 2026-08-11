"""Working-memory lessons — reward-based memory on top of the existing store.

A "lesson" is just a memory with category="lesson". What's new here is the
OUTCOME signal: record_outcome moves a lesson's confidence up (it helped) or
down (dead end / wrong). The existing machinery does the rest — a lesson that
keeps helping crosses the 0.8 auto-inject line (memory.context) and rides into
the next session's startup context; a dead end sinks and decays out.
"""

import logging

from src.core.base import ToolContext
from src.core.tools import tool

logger = logging.getLogger(__name__)

LESSON_CATEGORY = "lesson"
# Outcome → confidence delta. Positive corroborates; negative demotes toward decay.
_DELTAS = {"useful": 0.1, "dead_end": -0.2, "corrected": -0.2}
_CONF_MIN, _CONF_MAX = 0.0, 0.99


def _get_memory(ctx: ToolContext):
    if ctx.memory is None:
        raise RuntimeError("Memory backend not configured for this agent")
    return ctx.memory


def _clamp(v: float) -> float:
    return max(_CONF_MIN, min(_CONF_MAX, v))


@tool(
    name="remember_lesson",
    description=(
        "Save a durable lesson so you get smarter over time: a working approach, "
        "a dead end to avoid, or a correction. Lessons that prove useful are "
        "surfaced automatically at the start of future sessions. Record the "
        "generalisable insight, not the one-off answer."
    ),
    category="memory",
)
async def remember_lesson(ctx: ToolContext, content: str, tags: str = "") -> str:
    """Store a lesson. `tags` is optional extra comma-separated tags."""
    if not content.strip():
        return "ERROR: lesson content is required."
    memory = _get_memory(ctx)
    tag_list = ["lesson"] + [t.strip() for t in tags.split(",") if t.strip()]
    mid = await memory.store(
        content=content.strip(),
        type="procedural",
        agent_id=ctx.agent_id,
        tags=tag_list,
        category=LESSON_CATEGORY,
        scope="agent",
        scope_target=ctx.agent_id,
    )
    return (f"📝 Lesson saved (id: {mid}). It'll resurface in future sessions "
            f"once it proves useful.")


@tool(
    name="record_outcome",
    description=(
        "Give feedback on a lesson so memory becomes reward-based. outcome is "
        "'useful' (it helped — promote it), 'dead_end' (led nowhere — demote), "
        "or 'corrected' (was wrong — demote and attach the fix). `lesson` is the "
        "lesson's id or a few words that identify it."
    ),
    category="memory",
)
async def record_outcome(
    ctx: ToolContext, lesson: str, outcome: str, correction: str = ""
) -> str:
    outcome = outcome.strip().lower()
    if outcome not in _DELTAS:
        return "ERROR: outcome must be one of: useful, dead_end, corrected."
    memory = _get_memory(ctx)

    # Accept an id directly, else find the lesson by content.
    target = await memory.get(lesson.strip())
    if target is None:
        hits = await memory.search(
            query=lesson, agent_id=ctx.agent_id, limit=1, category=LESSON_CATEGORY
        )
        target = hits[0] if hits else None
    if not target:
        return f"No matching lesson found for '{lesson}'."

    old_conf = float(target.get("confidence", 0.7) or 0.7)
    # round to avoid float drift so 0.7 + 0.1 reliably reaches the 0.8 auto-inject line
    new_conf = _clamp(round(old_conf + _DELTAS[outcome], 2))
    fields = {"confidence": new_conf}
    if outcome == "corrected" and correction.strip():
        fields["content"] = f"{target['content']}\n\n[CORRECTION] {correction.strip()}"
    await memory.update(target["id"], agent_id=ctx.agent_id, **fields)

    logger.info(f"record_outcome {outcome} on {target['id']}: {old_conf:.2f}→{new_conf:.2f}")
    trend = "promoted" if _DELTAS[outcome] > 0 else "demoted"
    return (f"Recorded '{outcome}' — lesson {trend}, confidence "
            f"{old_conf:.2f} → {new_conf:.2f}"
            + (" (≥0.8 → will auto-surface next session)" if new_conf >= 0.8 else ""))
