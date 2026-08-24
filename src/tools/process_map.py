"""Business process mapping — structured model → diagrams → codex knowledge.

The agent fills a JSON model (actors, steps, decisions, handoffs, metrics …
or Wardley anchors/components/links), these tools validate it, tell the agent
what is still missing (grounded questions), render it (Mermaid flowchart /
swimlane / sequence / journey via render_diagram, Wardley via an offline SVG
emitter) and, once the user says it is ready, publish a markdown SOP into the
codex so every agent can use it.

Files live in <project_dir>/processes/<slug>/:
  model.json                the source of truth (patch-merged across turns)
  <slug>.<view>.mmd/.owm/.svg   diagram sources
  <slug>.<view>.png/.svg    rendered output (send with send_discord_file)
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from src.core.base import KBOTS_TMP, ToolContext
from src.core.tools import tool
from src.lib import process_model as pm
from src.lib.wardley_svg import emit_wardley_svg
from src.tools import design
from src.tools.ingest import validate_file_path

logger = logging.getLogger(__name__)

DEFAULT_VIEW = {"process": "flowchart", "wardley": "wardley"}
VIEW_CHOICES = list(pm.VIEWS)
LENS_CHOICES = ["", "sipoc", "raci", "vsm", "bpmn", "wastes", "wardley", "blueprint", "events"]


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def _processes_dir(ctx: ToolContext) -> Path:
    base = Path(ctx.project_dir) if ctx.project_dir else KBOTS_TMP
    return base / "processes"


def _process_dir(ctx: ToolContext, name: str) -> tuple[str, Path] | str:
    slug = pm.slugify(name)
    path = (_processes_dir(ctx) / slug).resolve()
    err = validate_file_path(str(path))
    if err:
        return err
    return slug, path


def _read_model(path: Path) -> dict | None:
    f = path / "model.json"
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"process_map: cannot read {f}: {e}")
        return None


def _write_model(path: Path, model: dict) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "model.json").write_text(json.dumps(model, indent=2, ensure_ascii=False), encoding="utf-8")


def _codex_root() -> Path:
    """Where a published process document is WRITTEN: the overlay's codex.

    This was `overlay/codex if it is a dir else core/codex`, which reads as
    overlay-first and is not. On an install whose overlay has no codex/ yet,
    the first publish landed in the Core checkout, and so did every one after
    it, because the overlay directory still did not exist. Core is replaced by
    every deploy and is read-only under a hardened systemd unit, so the
    document was either lost on the next pull or refused outright.

    The directory is created rather than tested for.
    """
    from src.core.base import install_write_root
    return install_write_root() / "codex"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _fmt_gaps(items: list[dict]) -> str:
    if not items:
        return "No gaps found — the model covers the question bank for its kind."
    lines = []
    for i, g in enumerate(items, 1):
        lines.append(f"{i}. [{g['phase']}] {g['question']}\n   why: {g['why']} ({g['method']})")
    return "\n".join(lines)


def _report(model: dict, errors: list[str], warnings: list[str], gap_list: list[dict]) -> str:
    kind = model["kind"]
    n = (f"{len(model['steps'])} steps, {len(model['actors'])} actors, {len(model['edges'])} edges"
         if kind == "process" else
         f"{len(model['components'])} components, {len(model['anchors'])} anchors, {len(model['links'])} links")
    out = [f"kind={kind} · {n} · completeness {pm.completeness(model)}%"]
    if errors:
        out.append("ERRORS (fix before rendering/publishing):\n- " + "\n- ".join(errors))
    if warnings:
        out.append("warnings:\n- " + "\n- ".join(warnings))
    out.append("Next questions (ask ONE at a time, most valuable first):\n" + _fmt_gaps(gap_list))
    return "\n\n".join(out)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@tool(name="process_model_save",
      description="Save/merge a business-process or Wardley model (JSON); returns validation + next questions to ask",
      category="process")
async def process_model_save(ctx: ToolContext, name: str, model_json: str,
                             replace: bool = False) -> str:
    """Create or update the model for process `name`.

    model_json is a JSON object. kind: "process" (flowchart/swimlane/sequence)
    or "wardley". Process keys: title, purpose, scope{start,end,out_of_scope},
    trigger[], end_states[], actors[{id,name,role}], steps[{id,label,actor,
    type: task|decision|start|end|wait|handoff, phase, systems[], inputs[],
    outputs[], metrics{process_time,lead_time,frequency,pct_complete_accurate},
    notes}], edges[{from,to,label,condition}], exceptions[{step,what_happens,
    handling}], pain_points[], open_questions[{topic,question,why}].
    Wardley keys: title, purpose, anchors[{name,visibility,evolution}],
    components[{name,visibility,evolution,type,stage_rationale,inertia,
    evolve_to,build_buy_outsource}], links[{from,to,label}], flows[],
    pipelines[{parent,children[]}], notes[{text,visibility,evolution}],
    climatic_patterns[], open_questions[]. Coordinates are 0..1
    (visibility 1 = visible to the user, evolution 1 = commodity).

    By default the JSON is PATCH-MERGED into the existing model (keyed lists
    upsert by id/name, `_remove: {steps: [ids]}` deletes), so an interview can
    add a little each turn. replace=true overwrites.
    Returns validation errors/warnings and the ranked next questions.
    """
    located = _process_dir(ctx, name)
    if isinstance(located, str):
        return located
    slug, path = located
    try:
        patch = json.loads(model_json)
    except json.JSONDecodeError as e:
        return f"model_json is not valid JSON: {e}"
    if not isinstance(patch, dict):
        return "model_json must be a JSON object"
    existing = None if replace else _read_model(path)
    model = pm.merge(existing, patch) if existing else patch
    model = pm.normalize(model)
    model.setdefault("title", name)
    model["updated"] = _today()
    errors, warnings = pm.validate(model)
    _write_model(path, model)
    gap_list = pm.gaps(model)
    head = f"Saved {slug} → {path / 'model.json'}"
    return head + "\n" + _report(model, errors, warnings, gap_list)


@tool(name="process_model_load",
      description="Load a saved process/Wardley model as JSON, or list saved processes when name is empty",
      category="process")
async def process_model_load(ctx: ToolContext, name: str = "") -> str:
    """Return the current model.json for `name`. With no name, list the
    processes saved for this agent (slug, title, kind, completeness)."""
    if not name.strip():
        root = _processes_dir(ctx)
        if not root.is_dir():
            return "No processes saved yet."
        rows = []
        for d in sorted(p for p in root.iterdir() if p.is_dir()):
            m = _read_model(d)
            if m:
                m = pm.normalize(m)
                rows.append(f"- {d.name}: {m.get('title', '')} ({m['kind']}, {pm.completeness(m)}% complete, "
                            f"updated {m.get('updated', '?')})")
        return "Saved processes:\n" + "\n".join(rows) if rows else "No processes saved yet."
    located = _process_dir(ctx, name)
    if isinstance(located, str):
        return located
    slug, path = located
    model = _read_model(path)
    if model is None:
        return f"No model saved for '{slug}'. Use process_model_save to create it."
    return json.dumps(model, indent=2, ensure_ascii=False)


@tool(name="process_model_gaps",
      description="Rank the most valuable next questions for a process/Wardley model (optional method lens)",
      category="process")
async def process_model_gaps(ctx: ToolContext, name: str,
                             lens: Annotated[str, {"choices": LENS_CHOICES}] = "",
                             limit: int = 5) -> str:
    """Grounded questions: each one targets a field that is empty, hedged
    ('usually', 'sometimes', '?') or contradictory in the saved model.
    lens narrows to a method's question bank: sipoc, raci, vsm (value stream
    / timing), bpmn (triggers, gateways, exceptions), wastes (lean pain
    points), wardley, blueprint (service blueprint), events (event storming).
    Use when coaching a live workshop or when the user asks 'what should I
    ask next?'. Ask ONE question per message.
    """
    located = _process_dir(ctx, name)
    if isinstance(located, str):
        return located
    slug, path = located
    model = _read_model(path)
    if model is None:
        return f"No model saved for '{slug}'. Save a first draft with process_model_save."
    model = pm.normalize(model)
    lens = lens if lens in LENS_CHOICES else ""
    items = pm.gaps(model, lens=lens, limit=max(1, min(limit, 10)))
    head = f"{slug}: completeness {pm.completeness(model)}%" + (f", lens={lens}" if lens else "")
    return head + "\n" + _fmt_gaps(items)


@tool(name="process_render",
      description="Render a saved process model as flowchart, swimlane, sequence, journey or Wardley map (PNG/SVG)",
      category="process")
async def process_render(ctx: ToolContext, name: str,
                         view: Annotated[str, {"choices": VIEW_CHOICES}] = "",
                         format: Annotated[str, {"choices": ["png", "svg"]}] = "png") -> str:
    """Emit diagram text from the model and render it locally.

    views: flowchart (Mermaid), swimlane (lane per actor), sequence (handoffs
    between actors), journey (user journey), wardley (self-contained SVG, no
    network — the default for kind=wardley), wardley_mermaid (Mermaid
    wardley-beta). Returns the rendered file path — send it to the user with
    send_discord_file — plus the diagram source path.
    """
    located = _process_dir(ctx, name)
    if isinstance(located, str):
        return located
    slug, path = located
    model = _read_model(path)
    if model is None:
        return f"No model saved for '{slug}'. Save it first with process_model_save."
    model = pm.normalize(model)
    errors, _ = pm.validate(model)
    if errors:
        return "Model has errors — fix them with process_model_save first:\n- " + "\n- ".join(errors)
    view = view or DEFAULT_VIEW[model["kind"]]
    if view not in pm.VIEWS:
        return f"Unknown view '{view}'. Valid: {', '.join(pm.VIEWS)}"
    if model["kind"] == "wardley" and view not in ("wardley", "wardley_mermaid"):
        return "This is a Wardley model — use view='wardley' (or 'wardley_mermaid')."
    if model["kind"] == "process" and view in ("wardley", "wardley_mermaid"):
        return "This is a process model — use flowchart, swimlane, sequence or journey."
    if format not in ("png", "svg"):
        return "format must be png or svg"

    path.mkdir(parents=True, exist_ok=True)
    out = path / f"{slug}.{view}.{format}"

    if view == "wardley":
        brand = design._load_brand()
        svg = emit_wardley_svg(model, brand["colors"], brand["font_family"])
        (path / f"{slug}.owm").write_text(pm.emit_owm(model), encoding="utf-8")
        svg_path = path / f"{slug}.wardley.svg"
        result = await design.render_svg(ctx, svg, output_file=str(svg_path), to_png=(format == "png"))
        if "PNG saved" not in result and format == "png":
            return (f"Wardley SVG saved: {svg_path} (PNG step failed: {result.splitlines()[-1]})\n"
                    f"OWM source: {path / (slug + '.owm')}")
        final = svg_path.with_suffix(".png") if format == "png" else svg_path
        return (f"Wardley map rendered: {final}\nOWM source: {path / (slug + '.owm')}\n"
                "Send the image to the user with send_discord_file; offer the .owm text on request.")

    # Mermaid views
    source = pm.emit(model, view)
    src_path = path / f"{slug}.{view}.mmd"
    src_path.write_text(source, encoding="utf-8")
    result = await design.render_diagram(ctx, source, output_file=str(out), format=format)
    if view == "swimlane" and result.startswith("Mermaid error"):
        # renderer without swimlane-beta → portable flowchart-with-lanes
        source = pm.emit_swimlane(model, beta=False)
        src_path.write_text(source, encoding="utf-8")
        result = await design.render_diagram(ctx, source, output_file=str(out), format=format)
    if not result.startswith("Diagram saved"):
        return f"{result}\nDiagram source kept at {src_path} — fix the model and re-render."
    return (f"{view} rendered: {out}\nMermaid source: {src_path}\n"
            "Send the image to the user with send_discord_file.")


# ---------------------------------------------------------------------------
# Publish to codex
# ---------------------------------------------------------------------------

def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "_none recorded_"
    def esc(s: object) -> str:
        return str(s or "").replace("|", "\\|").replace("\n", " ")
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def render_codex_doc(model: dict, slug: str, image_rel: str | None, source_text: str,
                     source_lang: str) -> str:
    """Markdown SOP/knowledge doc for the codex (pure function, tested)."""
    model = pm.normalize(model)
    kind = model["kind"]
    title = model.get("title") or slug
    lines = [f"# {title}", "", f"Last updated: {_today()}", "",
             f"_Kind: {'Wardley map' if kind == 'wardley' else 'business process'} · "
             f"completeness {pm.completeness(model)}% · source: `processes/{slug}/model.json` "
             "in the mapping agent's workspace_", ""]
    if model.get("purpose"):
        lines += ["## Purpose", "", str(model["purpose"]), ""]
    if image_rel:
        lines += [f"![{title}]({image_rel})", ""]
    if kind == "process":
        sc = model.get("scope") or {}
        if any(sc.get(k) for k in ("start", "end", "out_of_scope")):
            lines += ["## Scope", ""]
            if sc.get("start"):
                lines.append(f"- **Starts:** {sc['start']}")
            if sc.get("end"):
                lines.append(f"- **Ends:** {sc['end']}")
            if sc.get("out_of_scope"):
                lines.append(f"- **Out of scope:** {sc['out_of_scope']}")
            lines.append("")
        if model["trigger"] or model["end_states"]:
            lines += ["## Triggers and end states", ""]
            lines += [f"- **Trigger:** {t}" for t in model["trigger"]]
            lines += [f"- **End state:** {t}" for t in model["end_states"]]
            lines.append("")
        lines += ["## Actors", "",
                  _md_table(["Actor", "Role"], [[a.get("name"), a.get("role", "")] for a in model["actors"]]), ""]
        actor_name = {a["id"]: a.get("name") or a["id"] for a in model["actors"]}
        rows = []
        for s in pm._ordered_steps(model):
            m = s.get("metrics") or {}
            timing = " / ".join(x for x in (m.get("process_time"), m.get("lead_time")) if x)
            rows.append([s.get("label"), s.get("type"), actor_name.get(s.get("actor"), s.get("actor") or ""),
                         ", ".join(s.get("systems") or []),
                         (", ".join(s.get("inputs") or []) + " → " + ", ".join(s.get("outputs") or [])).strip(" →"),
                         timing])
        lines += ["## Steps", "",
                  _md_table(["Step", "Type", "Actor", "Systems", "Inputs → outputs", "PT / LT"], rows), ""]
        decisions = [s for s in model["steps"] if s.get("type") == "decision"]
        if decisions or model["exceptions"]:
            lines += ["## Decisions and exceptions", ""]
            label_of = {s["id"]: s.get("label") for s in model["steps"]}
            for d in decisions:
                outs = [e for e in model["edges"] if e.get("from") == d["id"]]
                branches = "; ".join(f"{e.get('label') or e.get('condition') or '?'} → {label_of.get(e.get('to'))}"
                                     for e in outs)
                lines.append(f"- **{d.get('label')}**: {branches}")
            for x in model["exceptions"]:
                lines.append(f"- *Exception at {label_of.get(x.get('step'), x.get('step'))}:* "
                             f"{x.get('what_happens', '')} — {x.get('handling', '')}")
            lines.append("")
        if model["pain_points"]:
            lines += ["## Pain points", ""] + [f"- {p}" for p in model["pain_points"]] + [""]
    else:
        lines += ["## Users and needs", ""]
        lines += [f"- {a['name']}" for a in model["anchors"]] or ["_none recorded_"]
        lines.append("")
        def stage(e: object) -> str:
            return ("Genesis", "Custom built", "Product", "Commodity")[min(3, int(float(e or 0) * 4))]
        rows = [[c["name"], stage(c.get("evolution")), f"{float(c.get('visibility', 0) or 0):.2f}",
                 c.get("build_buy_outsource", ""), "yes" if c.get("inertia") else "",
                 c.get("stage_rationale", "")] for c in model["components"]]
        lines += ["## Components", "",
                  _md_table(["Component", "Stage", "Visibility", "Build/buy", "Inertia", "Why this stage"], rows), ""]
        if model["climatic_patterns"]:
            lines += ["## Climatic patterns in play", ""] + [f"- {p}" for p in model["climatic_patterns"]] + [""]
    if model["open_questions"]:
        lines += ["## Open questions", ""]
        lines += [f"- {q.get('question')}" for q in model["open_questions"] if q.get("question")]
        lines.append("")
    lines += ["## Diagram source", "", "Re-render with `process_render`, or paste into any "
              f"{'OWM-compatible viewer' if source_lang == 'owm' else 'Mermaid renderer'}.", "",
              f"```{source_lang}", source_text.rstrip(), "```", ""]
    return "\n".join(lines)


def update_codex_index(index_path: Path, section: str, slug: str, title: str,
                       kind: str) -> None:
    """Idempotently add/replace `- \\`<section>/<slug>.md\\` — …` under `### <Section>`."""
    entry = f"- `{section}/{slug}.md` — {title} ({'Wardley map' if kind == 'wardley' else 'process map'})"
    heading = f"### {section.capitalize()}"
    text = index_path.read_text(encoding="utf-8") if index_path.is_file() else "# Codex Index\n\nLast updated: \n"
    lines = text.splitlines()
    # bump last updated
    for i, line in enumerate(lines):
        if line.lower().startswith("last updated:"):
            lines[i] = f"Last updated: {_today()}"
            break
    key = f"`{section}/{slug}.md`"
    replaced = False
    for i, line in enumerate(lines):
        if key in line:
            lines[i] = entry
            replaced = True
            break
    if not replaced:
        idx = next((i for i, ln in enumerate(lines) if ln.strip().lower() == heading.lower()
                    or ln.strip().lower() == f"## {section}"), None)
        if idx is None:
            if lines and lines[-1].strip():
                lines.append("")
            lines += [heading, entry]
        else:
            j = idx + 1
            while j < len(lines) and not lines[j].startswith("#"):
                j += 1
            # insert after the last non-empty line of the section
            k = j
            while k > idx + 1 and not lines[k - 1].strip():
                k -= 1
            lines.insert(k, entry)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")


@tool(name="process_publish",
      description="Publish a finished process/Wardley model into the codex as shared business knowledge",
      category="process")
async def process_publish(ctx: ToolContext, name: str, section: str = "processes") -> str:
    """Write <codex>/<section>/<slug>.md (+ the rendered image) and register it
    in codex/_index.md. Only call this when the user says the process is ready
    — it becomes shared knowledge for every agent. Refuses on validation
    errors; warns (but proceeds) on open questions. Re-publishing overwrites
    the same file and index line.
    """
    located = _process_dir(ctx, name)
    if isinstance(located, str):
        return located
    slug, path = located
    model = _read_model(path)
    if model is None:
        return f"No model saved for '{slug}'."
    model = pm.normalize(model)
    errors, warnings = pm.validate(model)
    if errors:
        return "Not published — model has errors:\n- " + "\n- ".join(errors)
    section = pm.slugify(section) or "processes"
    codex = _codex_root()
    target_dir = codex / section
    err = validate_file_path(str(target_dir))
    if err:
        return err
    target_dir.mkdir(parents=True, exist_ok=True)

    # image: reuse the latest render of the default view, else try to render now
    view = DEFAULT_VIEW[model["kind"]]
    png = path / f"{slug}.{view}.png"
    if not png.is_file():
        render_msg = await process_render(ctx, name, view=view, format="png")
        logger.info(f"process_publish: rendered on demand: {render_msg.splitlines()[0]}")
    image_rel = None
    if png.is_file():
        shutil.copyfile(png, target_dir / f"{slug}.png")
        image_rel = f"{slug}.png"

    if model["kind"] == "wardley":
        source_text, lang = pm.emit_owm(model), "owm"
    else:
        source_text, lang = pm.emit_flowchart(model), "mermaid"
    doc = render_codex_doc(model, slug, image_rel, source_text, lang)
    doc_path = target_dir / f"{slug}.md"
    doc_path.write_text(doc, encoding="utf-8")
    update_codex_index(codex / "_index.md", section, slug, model.get("title") or slug, model["kind"])

    notes = []
    if model["open_questions"]:
        notes.append(f"{len(model['open_questions'])} open question(s) are listed in the doc")
    if warnings:
        notes.append(f"{len(warnings)} validation warning(s)")
    tail = (" (" + "; ".join(notes) + ")") if notes else ""
    return (f"Published to codex: {doc_path}" + (f" + {slug}.png" if image_rel else "") +
            f"\nIndexed in {codex / '_index.md'} under '{section}'. Other agents see it in their "
            f"startup <codex-index> from their next session.{tail}")
