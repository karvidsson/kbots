"""Startup context injection — provides key context on first message of a session.

Injects team roster, system profile, and codex index so agents always have
foundational knowledge without relying on memory search or LLM instructions.
Only fires on the first message of a session; subsequent messages in the same
session inherit context via Claude Code's --resume.
"""

import json
import logging
import os
from pathlib import Path

from src.core.base import PROJECT_ROOT, resolve_config_file

logger = logging.getLogger(__name__)

TEAM_FILE = resolve_config_file("team.json")

# Words allowed above the question when an agent needs a decision. A number is
# followed; "be concise" is interpreted.
DECISION_WORD_BUDGET = 100

_REPLY_CONTRACT = f"""<reply-contract>
How to end a turn. This overrides any habit of reporting your work.

Pick exactly one of three shapes.

1. NOTHING NEEDED — nothing failed and no judgement of the owner's is
   required. Reply with exactly NO_REPLY and nothing else. This is the
   default for routine completions. A finished task is not news.

2. DONE — you finished something the owner asked for and it needs no
   decision. Two or three lines: what changed, and the one fact that proves
   it (a count, an id, a path, a URL). No method, no narration of the route
   you took, no list of what you checked.

3. DECISION — you need the owner's judgement or taste. Use this shape, in
   this order, at the TOP of the message:

       DECISION: <one line: what is being chosen between>
       OPTIONS:  A <one line>  /  B <one line>
       I'D PICK: <one line, and why>
       IF NO REPLY: <what you will do by default>

   Everything above the question is at most {DECISION_WORD_BUDGET} words.
   Evidence, reasoning and alternatives go BELOW that block, or in a file you
   name so they can be opened on demand. Never make the owner read the
   evidence to find the question.

Rules that apply to all three:
- Ask for one decision per message. Two questions get one answer.
- Never ask for a decision you can make and reverse yourself. Make it, say
  which default you took in one line, and move on.
- Being thorough is about the work, not the message. Do the full
  investigation, then report the part that changes what the owner does.
- If you catch yourself explaining why something was hard, delete it.
</reply-contract>"""


def _build_reply_contract() -> str:
    """The output contract every agent gets, in every session.

    Concision guidance already existed as one line inside the codex index and
    was reliably ignored: it competed with the roster, the version banner and
    the index itself, and it was a matter of style rather than structure.
    This is a separate block with a shape and a number, because those get
    followed where adjectives do not.
    """
    return _REPLY_CONTRACT


def _resolve_codex_index() -> Path:
    """Find the shared codex index: overlay codex/ → core codex/."""
    overlay = os.environ.get("KBOTS_OVERLAY")
    if overlay:
        p = Path(overlay) / "codex" / "_index.md"
        if p.exists():
            return p
    return PROJECT_ROOT / "codex" / "_index.md"


def _build_team_summary() -> str | None:
    """Build a compact team roster block from team.json."""
    if not TEAM_FILE.exists():
        return None

    try:
        team = json.loads(TEAM_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    lines = ["<team-roster>"]

    for h in team.get("humans", []):
        name = h.get("name", "?")
        role = h.get("role", "")
        access = h.get("access", "")
        discord = h.get("contact", {}).get("discord", "")
        line = f"  - {name}: {role} ({access})"
        if discord:
            line += f" [discord:{discord}]"
        lines.append(line)

    for a in team.get("agents", []):
        name = a.get("name", "?")
        role = a.get("role", "")
        domain = a.get("domain", "")
        discord = a.get("discord", "")
        reports_to = a.get("reports_to", "")
        line = f"  - {name}: {role}" if role else f"  - {name}"
        if domain:
            line += f" — {domain}"
        if reports_to:
            line += f" → reports to {reports_to}"
        if discord:
            line += f" [discord:{discord}]"
        lines.append(line)

    lines.append(
        "  NOTE: the people and agents above are your team. A message whose Discord "
        "ID matches an entry here is a teammate — treat them as such. If you're unsure "
        "whether a sender belongs to the team, check their Discord ID against this "
        "roster before treating them as an outsider or unverified guest."
    )
    lines.append("</team-roster>")
    return "\n".join(lines)


def _build_codex_index(project_dir: str | None = None) -> str | None:
    """Build codex awareness blocks: shared codex index plus the agent's own.

    The shared index (overlay codex/ → core codex/) applies to every agent.
    An agent may additionally keep a private codex at <project_dir>/codex/;
    when its _index.md exists it is injected as a separate block so the agent
    knows its role-specific knowledge base on top of the shared one.
    """
    blocks = []

    shared = _resolve_codex_index()
    if shared.exists():
        try:
            blocks.append(f"<codex-index>\n{shared.read_text().strip()}\n</codex-index>")
        except OSError:
            pass

    if project_dir:
        own = Path(project_dir) / "codex" / "_index.md"
        if own.exists():
            try:
                blocks.append(
                    "<agent-codex-index>\n"
                    f"Your own codex (role-specific knowledge) lives in {own.parent}/ — "
                    "read files from it as needed.\n"
                    f"{own.read_text().strip()}\n</agent-codex-index>"
                )
            except OSError:
                pass

    return "\n\n".join(blocks) if blocks else None


def _build_lessons(project_dir: str | None) -> str | None:
    """Build the curated-lessons block from the agent's LESSONS.md, if present.

    LESSONS.md is written per-agent by the reflector (src/core/reflector.py) —
    a deduped digest of what worked and what to avoid. Injected once per session.
    """
    if not project_dir:
        return None
    path = Path(project_dir) / "LESSONS.md"
    if not path.exists():
        return None
    try:
        content = path.read_text().strip()
    except OSError:
        return None
    if not content:
        return None
    if len(content) > 4000:
        content = content[:4000] + "\n…(truncated)"
    return f"<lessons>\n{content}\n</lessons>"


def _build_platform_version() -> str | None:
    """Tell the agent which platform version is running (so it can confirm
    updates). Reads the commit captured at boot (src/core/version.py)."""
    try:
        from src.core import version
        v = version.read_running_version()
    except Exception:
        return None
    if not v:
        return None
    line = f"running {v.get('version') or v.get('short', '?')}"
    if v.get("subject"):
        line += f" — {v['subject']}"
    detail = v.get("short", "")
    if detail:
        line += f" (git {detail}{', ' + v['date'] if v.get('date') else ''})"
    return f"<platform-version>\n{line}\n</platform-version>"


async def build_startup_context(agent_id: str, memory=None, project_dir=None) -> str | None:
    """Assemble startup context for a new agent session.

    Returns a context string to prepend to the first message, or None.
    """
    blocks = []

    # Platform version (so the agent knows which build is running)
    version_block = _build_platform_version()
    if version_block:
        blocks.append(version_block)

    # Curated lessons (what worked / dead ends) — reward-based working memory
    lessons = _build_lessons(project_dir)
    if lessons:
        blocks.append(lessons)

    # Team roster
    team = _build_team_summary()
    if team:
        blocks.append(team)

    # Pinned memories from DB (operational knowledge only — NOT team/codex data)
    if memory:
        try:
            pinned = await memory.context(agent_id, limit=10)
            if pinned:
                # Filter out team-member entries (team.json is the source of truth)
                filtered = [
                    m for m in pinned
                    if "team-member" not in (m.get("tags", "") or "")
                    and not (m.get("content", "")).startswith("Team member:")
                ]
                if filtered:
                    lines = ["<pinned-context>"]
                    for m in filtered:
                        content = m.get("content", "")
                        if len(content) > 500:
                            content = content[:500] + "..."
                        lines.append(f"  - {content}")
                    lines.append("</pinned-context>")
                    blocks.append("\n".join(lines))
        except Exception as e:
            logger.debug(f"Pinned memory fetch failed: {e}")

    # Codex index (awareness of available business knowledge, shared + own)
    codex = _build_codex_index(project_dir)
    if codex:
        blocks.append(codex)

    # Reply contract LAST, so it is the final thing read before the owner's
    # message. It is unconditional: an agent with no roster, no codex and no
    # memory still has to answer in the agreed shape.
    blocks.append(_build_reply_contract())

    return "\n\n".join(blocks)
