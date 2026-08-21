"""Reflector — periodically consolidates an agent's lessons into LESSONS.md.

A background task (sibling of heartbeat/scheduler in main.py). Once per interval
per agent it gathers that agent's `category="lesson"` memories and makes ONE
cheap LLM call (Haiku by default) to dedupe and group them into a concise
LESSONS.md, which build_startup_context injects at the next session start.

When graph memory is enabled, the same cadence also feeds the graph: each pass
walks the next batch of the agent's memories (all categories, cursor-tracked)
and one extra LLM call extracts entity-relationship edges into the shared
graph via memory_link's MERGE path — so the graph both backfills the existing
store over a few days and keeps absorbing new memories from then on.

Deliberately NOT a full agent turn: it runs in a neutral working dir (no
CLAUDE.md / .mcp.json), with no tools, so it's a single cheap completion.
"""

import asyncio
import json
import logging
import time
from pathlib import Path

from src.core import runtime_state
from src.core.base import Message, MessageRole, resolve_kbots_tmp

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are a memory-consolidation assistant for an AI agent. You are given the "
    "agent's saved lessons, each with a confidence score. Produce a concise "
    "LESSONS.md the agent reads at the start of future sessions.\n"
    "Rules: merge near-duplicates; drop noise; keep each lesson to one short "
    "bullet. Group into exactly these sections (omit a section if empty):\n"
    "## Preferred — confidence >= 0.80, things that reliably work; start here.\n"
    "## Tentative — confidence 0.40–0.79, seen useful but not yet corroborated.\n"
    "## Avoid / Dead ends — confidence < 0.40 or marked dead-end; do not retry.\n"
    "## Codify — lessons that describe a repeatable multi-step PROCEDURE the "
    "agent keeps re-doing by hand (a report pipeline, a fixed format, a "
    "recurring sequence of tool calls). One bullet each: name the procedure "
    "and what a skill for it would need to capture (inputs, steps, gotchas, "
    "output). Only procedures corroborated across lessons or confirmed by the "
    "user — never one-offs. The agent will propose these to the user as "
    "skills (see /codify).\n"
    "If a lesson contains [CORRECTION], state the corrected fact under Preferred.\n"
    "Output ONLY the markdown body (no ``` fences, no preamble)."
)

_EXTRACT_SYSTEM = (
    "You extract entity-relationship edges from an AI agent's saved memories to "
    "build a knowledge graph. Each memory has an id. Output ONLY a JSON array:\n"
    '[{"a": "<entity>", "rel": "<relation>", "b": "<entity>", '
    '"confidence": 0.0-1.0, "source": <memory id>}]\n'
    "Rules: entities are short canonical names of durable things — people, "
    "projects, tools, organizations, places, concepts. rel is a short reusable "
    "snake_case relation (works_at, uses, owns, part_of, reports_to, competes_with). "
    "Only extract relationships the memory actually states or strongly implies; "
    "skip one-off events, opinions, and procedural trivia. Reuse the exact same "
    "entity spelling across edges. confidence reflects how explicitly the memory "
    "states it. An empty array [] is a fine answer. No prose, no ``` fences."
)


def _parse_edges(text: str) -> list[dict]:
    """Parse the extraction model's JSON output; tolerate ``` fences. Returns
    only well-formed edges (a/rel/b non-empty strings, confidence clamped)."""
    body = (text or "").strip()
    if body.startswith("```"):
        body = body.strip("`\n")
        if body.startswith("json"):
            body = body[4:]
    try:
        raw = json.loads(body)
    except ValueError:
        return []
    if not isinstance(raw, list):
        return []
    edges = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        a = str(item.get("a") or "").strip()
        rel = str(item.get("rel") or "").strip()
        b = str(item.get("b") or "").strip()
        if not a or not rel or not b or a == b:
            continue
        try:
            conf = min(1.0, max(0.0, float(item.get("confidence", 0.6))))
        except (TypeError, ValueError):
            conf = 0.6
        edges.append({"a": a[:120], "rel": rel[:60], "b": b[:120],
                      "confidence": conf, "source": item.get("source")})
    return edges


class Reflector:
    def __init__(self, agent_manager, config: dict | None = None,
                 graph_cfg: dict | None = None):
        self.mgr = agent_manager
        cfg = config or {}
        self.enabled = cfg.get("enabled", True)
        self.model = cfg.get("model", "haiku")
        self.interval_h = float(cfg.get("interval_hours", 24))
        self.min_lessons = int(cfg.get("min_lessons", 3))
        self.tick = float(cfg.get("tick_seconds", 3600))
        gcfg = graph_cfg or {}
        self.extract_enabled = bool(gcfg.get("enabled")) and gcfg.get("extract", True)
        self.extract_batch = int(gcfg.get("extract_batch", 60))

    def _due(self, agent_id: str, now: float) -> bool:
        last = runtime_state.get_flag(f"reflector_last_{agent_id}", 0) or 0
        return (now - float(last)) >= self.interval_h * 3600

    def _work_dir(self) -> str:
        """Neutral cwd for the LLM call — no agent CLAUDE.md/.mcp.json to load.

        Via resolve_kbots_tmp() rather than a hand-rolled $KBOTS_OVERLAY/tmp:
        that bypassed the KBOTS_TMP override, which is the one escape hatch a
        hardened host has when the overlay root is mounted read-only. The mkdir
        raised there, and it is the first thing the reflector does.
        """
        d = resolve_kbots_tmp() / "reflector"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    async def run(self) -> None:
        logger.info(f"Reflector started (model={self.model}, every {self.interval_h}h, "
                    f"graph extraction {'ON' if self.extract_enabled else 'off'})")
        while True:
            try:
                now = time.time()
                for agent_id in list(getattr(self.mgr, "agent_configs", {})):
                    if self.enabled and self._due(agent_id, now):
                        await self._reflect(agent_id)
                        runtime_state.set_flag(f"reflector_last_{agent_id}", time.time())
                    # Extraction runs every tick, not on the reflection interval:
                    # _extract_graph early-returns on an empty batch (one cheap
                    # cursor query), so a quiet agent costs nothing, while a
                    # busy agent's backlog drains at extract_batch/tick instead
                    # of extract_batch/24h.
                    if self.extract_enabled:
                        try:
                            await self._extract_graph(agent_id)
                        except Exception as e:
                            logger.error(f"Graph extraction failed for {agent_id}: {e}")
            except Exception as e:
                logger.error(f"Reflector tick failed: {e}", exc_info=True)
            await asyncio.sleep(self.tick)

    async def _reflect(self, agent_id: str) -> bool:
        memory = self.mgr._get_agent_memory(agent_id)
        if not memory or not hasattr(memory, "list_by_category"):
            return False
        lessons = await memory.list_by_category(agent_id, "lesson", limit=100)
        if len(lessons) < self.min_lessons:
            logger.debug(f"Reflector: {agent_id} has {len(lessons)} lessons "
                         f"(<{self.min_lessons}) — skipping")
            return False

        digest = "\n".join(
            f"- (confidence {float(m.get('confidence', 0.7) or 0.7):.2f}) "
            f"{(m.get('content') or '').strip()}"
            for m in lessons
        )
        try:
            llm = self.mgr._get_agent_llm(agent_id)
        except Exception:
            return False

        messages = [
            Message(role=MessageRole.SYSTEM, content=_SYSTEM),
            Message(role=MessageRole.USER, content=f"Agent's saved lessons:\n\n{digest}"),
        ]
        resp = await llm.complete(
            messages, tools=None, project_dir=self._work_dir(),
            model=self.model, timeout=180,
        )
        body = (resp.content or "").strip()
        if not body:
            return False

        header = ("# LESSONS\n\n> Auto-generated by the reflector — what has worked "
                  "and what to avoid. Consulted at the start of each session.\n\n")
        out = Path(self.mgr._get_project_dir(agent_id)) / "LESSONS.md"
        out.write_text(header + body + "\n")
        logger.info(f"Reflector wrote {out} for {agent_id} "
                    f"({len(lessons)} lessons, model={self.model})")
        return True

    async def _extract_graph(self, agent_id: str) -> int:
        """One extraction pass: walk the next batch of the agent's memories
        (cursor on updated_at) and link the extracted edges into graph memory.

        Runs in the main process, so it uses the real GraphMemory directly.
        Edge scope inherits the source memory's scope (a private memory never
        becomes a global edge); link() is MERGE-idempotent, so re-reading an
        updated memory is harmless. Returns number of edges linked.
        """
        from src.lib.graph_store import GraphUnavailableError, get_graph
        try:
            graph = get_graph()
        except GraphUnavailableError:
            return 0
        memory = self.mgr._get_agent_memory(agent_id)
        if not memory or not hasattr(memory, "list_since"):
            return 0

        cursor_key = f"graph_extract_cursor_{agent_id}"
        raw = runtime_state.get_flag(cursor_key) or ["", ""]
        cursor = (str(raw[0]), str(raw[1])) if isinstance(raw, list) and len(raw) == 2 else ("", "")
        memories = await memory.list_since(agent_id, since=cursor,
                                           limit=self.extract_batch)
        if not memories:
            return 0

        digest = "\n".join(
            f"[{m['id']}] ({m.get('category') or m.get('type') or 'general'}) "
            f"{(m.get('content') or '').strip()[:500]}"
            for m in memories
        )
        try:
            llm = self.mgr._get_agent_llm(agent_id)
        except Exception:
            return 0
        resp = await llm.complete(
            [Message(role=MessageRole.SYSTEM, content=_EXTRACT_SYSTEM),
             Message(role=MessageRole.USER, content=f"Agent's memories:\n\n{digest}")],
            tools=None, project_dir=self._work_dir(), model=self.model, timeout=180,
        )
        edges = _parse_edges(resp.content or "")

        # scope of each edge = scope of the memory it came from
        scope_by_id = {str(m["id"]): (m.get("scope") or f"agent:{agent_id}")
                       for m in memories}
        linked = 0
        for e in edges:
            scope = scope_by_id.get(str(e.get("source")), f"agent:{agent_id}")
            try:
                await graph.link(e["a"], e["rel"], e["b"], confidence=e["confidence"],
                                 scope=scope, created_by=agent_id)
                linked += 1
            except (GraphUnavailableError, ValueError) as err:
                logger.debug(f"Graph extraction: skipped edge {e['a']}→{e['b']}: {err}")

        # advance the cursor even when the batch yielded nothing — these
        # memories are processed; the next pass reads the next batch
        last = memories[-1]
        runtime_state.set_flag(
            cursor_key, [str(last.get("updated_at") or ""), str(last.get("id") or "")])
        logger.info(f"Graph extraction: {agent_id} — {len(memories)} memories read, "
                    f"{linked} edges linked (model={self.model})")
        return linked
