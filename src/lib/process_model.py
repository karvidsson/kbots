"""Process-model core: schema checks, patch-merge, gap analysis, diagram emitters.

Pure functions over plain dicts — no I/O, no LLM — so the behaviour is
unit-testable and deterministic. `src/tools/process_map.py` wraps these as
agent tools; `src/lib/wardley_svg.py` draws the Wardley view.

Model shapes (kind = "process" | "wardley"):

process:
  title, purpose, scope{start,end,out_of_scope}, trigger[], end_states[],
  actors[{id,name,role}]           role: initiator|performer|approver|consulted|informed|system
  steps[{id,label,actor,type,phase,systems[],inputs[],outputs[],metrics{},notes}]
                                   type: task|decision|start|end|wait|handoff
  edges[{from,to,label,condition}], exceptions[{step,what_happens,handling}],
  pain_points[], open_questions[{topic,question,why}], direction

wardley:
  title, purpose, anchors[{name,visibility,evolution}],
  components[{name,visibility,evolution,type,stage_rationale,inertia,evolve_to,
              build_buy_outsource,label_offset}]
  links[{from,to,label}], flows[{from,to,label}], pipelines[{parent,children[]}],
  notes[{text,visibility,evolution}], climatic_patterns[], open_questions[]
"""

from __future__ import annotations

import re
from typing import Any

from src.lib.process_questions import HEDGES, Question, bank_for, phases_for

KINDS = ("process", "wardley")
STEP_TYPES = ("task", "decision", "start", "end", "wait", "handoff")
ACTOR_ROLES = ("initiator", "performer", "approver", "consulted", "informed", "system")
VIEWS = ("flowchart", "swimlane", "sequence", "journey", "wardley", "wardley_mermaid")

_LIST_KEYS_PROCESS = ("trigger", "end_states", "actors", "steps", "edges", "exceptions",
                      "pain_points", "open_questions")
_LIST_KEYS_WARDLEY = ("anchors", "components", "links", "flows", "pipelines", "notes",
                      "climatic_patterns", "open_questions")

# Scalar top-level keys, so an unrecognised one can be named back to the caller.
# `updated` is stamped by process_model_save itself and is not caller input.
_SCALAR_KEYS_PROCESS = ("kind", "title", "purpose", "scope", "direction", "updated")
_SCALAR_KEYS_WARDLEY = ("kind", "title", "purpose", "updated")


# ---------------------------------------------------------------------------
# Normalise + validate
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s[:64] or "process"


def normalize(model: dict) -> dict:
    """Fill defaults and coerce loose shapes (strings for list items, etc.)."""
    m = dict(model or {})
    kind = m.get("kind") or ("wardley" if ("components" in m or "anchors" in m) else "process")
    m["kind"] = kind
    list_keys = _LIST_KEYS_WARDLEY if kind == "wardley" else _LIST_KEYS_PROCESS
    for key in list_keys:
        val = m.get(key)
        if val is None:
            m[key] = []
        elif not isinstance(val, list):
            m[key] = [val]
    if kind == "process":
        m.setdefault("scope", {})
        if not isinstance(m["scope"], dict):
            m["scope"] = {"start": str(m["scope"])}
        m["actors"] = [({"name": a} if isinstance(a, str) else dict(a)) for a in m["actors"]]
        for a in m["actors"]:
            a.setdefault("id", slugify(a.get("name", "")))
            a.setdefault("name", a["id"])
        m["steps"] = [({"label": s} if isinstance(s, str) else dict(s)) for s in m["steps"]]
        for i, s in enumerate(m["steps"]):
            s.setdefault("id", f"s{i + 1}")
            # Fall back to `name` before `id`, matching what actors already do
            # a few lines up. Without this a step written as {"id","name"} —
            # the obvious shape, and the one actors use — rendered every box as
            # its own id, which reads as a broken renderer rather than a
            # mislabelled field.
            if not s.get("label"):
                s["label"] = s.get("name") or s["id"]
            s.setdefault("type", "task")
            for k in ("systems", "inputs", "outputs"):
                v = s.get(k)
                if v is None:
                    s[k] = []
                elif isinstance(v, str):
                    s[k] = [v]
            s.setdefault("metrics", {})
        m["edges"] = [dict(e) for e in m["edges"] if isinstance(e, dict)]
        m["open_questions"] = [({"question": q} if isinstance(q, str) else dict(q))
                               for q in m["open_questions"]]
    else:
        m["anchors"] = [({"name": a} if isinstance(a, str) else dict(a)) for a in m["anchors"]]
        m["components"] = [({"name": c} if isinstance(c, str) else dict(c)) for c in m["components"]]
        m["links"] = [_coerce_link(lk) for lk in m["links"]]
        m["flows"] = [_coerce_link(lk) for lk in m["flows"]]
        # Wardley links address components by name, while process edges address
        # steps by id. Someone carrying the habit across writes {"from":"c1"}
        # and gets "unknown component" for a component that plainly exists. If
        # an entry carries an `id`, accept it and resolve to the name here, so
        # validation, the SVG emitter and the OWM output all agree.
        by_id = {str(c["id"]): c["name"] for c in m["components"] + m["anchors"]
                 if c.get("id") and c.get("name")}
        if by_id:
            for lk in m["links"] + m["flows"]:
                for end in ("from", "to"):
                    lk[end] = by_id.get(str(lk.get(end)), lk.get(end))
        m["notes"] = [({"text": n} if isinstance(n, str) else dict(n)) for n in m["notes"]]
        m["open_questions"] = [({"question": q} if isinstance(q, str) else dict(q))
                               for q in m["open_questions"]]
    return m


def _coerce_link(link: Any) -> dict:
    if isinstance(link, dict):
        return dict(link)
    if isinstance(link, str) and "->" in link:
        a, b = link.split("->", 1)
        return {"from": a.strip(), "to": b.strip()}
    return {"from": str(link), "to": ""}


def validate(model: dict) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Errors block rendering/publishing."""
    errors: list[str] = []
    warnings: list[str] = []
    kind = model.get("kind")
    if kind not in KINDS:
        errors.append(f"kind must be one of {KINDS}")
        return errors, warnings
    if not (model.get("title") or "").strip():
        warnings.append("title is empty")

    # An unrecognised top-level key was kept verbatim by the patch-merge and
    # then read by nothing. Content written into one persists, validates clean
    # and never appears in any diagram, so the renderer looks like it dropped
    # the work. Say the key is unknown instead.
    known = ((_LIST_KEYS_WARDLEY + _SCALAR_KEYS_WARDLEY) if kind == "wardley"
             else (_LIST_KEYS_PROCESS + _SCALAR_KEYS_PROCESS))
    unknown = sorted(k for k in model if k not in known)
    if unknown:
        warnings.append(f"unknown top-level key(s) {unknown} — stored but never rendered. "
                        f"Known keys for kind='{kind}': {sorted(known)}")

    if kind == "process":
        ids = [s["id"] for s in model["steps"]]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            errors.append(f"duplicate step ids: {sorted(dupes)}")
        actor_ids = {a["id"] for a in model["actors"]}
        for s in model["steps"]:
            if s.get("type") not in STEP_TYPES:
                errors.append(f"step {s['id']}: type '{s.get('type')}' not in {STEP_TYPES}")
            if s.get("actor") and s["actor"] not in actor_ids:
                warnings.append(f"step {s['id']}: actor '{s['actor']}' is not in actors[]")
        for a in model["actors"]:
            if a.get("role") and a["role"] not in ACTOR_ROLES:
                warnings.append(f"actor {a['id']}: role '{a['role']}' not in {ACTOR_ROLES}")
        idset = set(ids)
        for e in model["edges"]:
            if e.get("from") not in idset or e.get("to") not in idset:
                errors.append(f"edge {e.get('from')} -> {e.get('to')}: unknown step id")
        if model["steps"]:
            starts = [s for s in model["steps"] if s.get("type") == "start"]
            ends = [s for s in model["steps"] if s.get("type") == "end"]
            if len(starts) > 1:
                warnings.append("more than one start step")
            if not starts:
                warnings.append("no start step (type='start')")
            if not ends:
                warnings.append("no end step (type='end')")
        for s in model["steps"]:
            if s.get("type") == "decision":
                outs = [e for e in model["edges"] if e.get("from") == s["id"]]
                if len(outs) < 2:
                    warnings.append(f"decision '{s['label']}' has fewer than 2 outgoing edges")
                elif any(not (e.get("label") or e.get("condition")) for e in outs):
                    warnings.append(f"decision '{s['label']}' has unlabeled outgoing edges")
    else:
        # A component or anchor with no name used to escape here as a bare
        # KeyError ("Error executing tool process_model_save: 'name'"), naming
        # neither the list nor the entry. Report it and stop: every check below
        # is keyed on name, so continuing would only produce noise.
        for key in ("components", "anchors"):
            for i, item in enumerate(model[key]):
                if not str(item.get("name") or "").strip():
                    errors.append(f"{key}[{i}] has no 'name' — a Wardley "
                                  f"component/anchor is identified by its name: {item}")
        if errors:
            return errors, warnings

        names = [c["name"] for c in model["components"]] + [a["name"] for a in model["anchors"]]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            errors.append(f"duplicate component/anchor names: {sorted(dupes)}")
        for item in model["components"] + model["anchors"]:
            for key in ("visibility", "evolution"):
                v = item.get(key)
                if v is None:
                    continue
                try:
                    if not 0 <= float(v) <= 1:
                        errors.append(f"{item['name']}: {key}={v} outside [0,1]")
                except (TypeError, ValueError):
                    errors.append(f"{item['name']}: {key}={v!r} is not a number")
            if "->" in item["name"] or ";" in item["name"]:
                errors.append(f"name '{item['name']}' must not contain '->' or ';'")
        for c in model["components"]:
            if c.get("evolve_to") is not None:
                try:
                    if not 0 <= float(c["evolve_to"]) <= 1:
                        errors.append(f"{c['name']}: evolve_to outside [0,1]")
                except (TypeError, ValueError):
                    errors.append(f"{c['name']}: evolve_to is not a number")
        nameset = set(names)
        vis = {n["name"]: float(n.get("visibility", 0.5) or 0.5) for n in model["components"] + model["anchors"]}
        for link in model["links"] + model["flows"]:
            if link.get("from") not in nameset or link.get("to") not in nameset:
                errors.append(
                    f"link {link.get('from')} -> {link.get('to')}: unknown component. "
                    f"Wardley links reference component/anchor NAMES (or an 'id' you "
                    f"gave the component). Known names: {sorted(nameset)}")
            elif vis.get(link["from"], 0) + 1e-9 < vis.get(link["to"], 0):
                warnings.append(f"link {link['from']} -> {link['to']}: dependency points upwards "
                                "(the dependent should be more visible than what it depends on)")
        if len(model["components"]) > 30:
            warnings.append(f"{len(model['components'])} components — consider splitting (≤20 is readable)")
        linked = {lk.get("from") for lk in model["links"]} | {lk.get("to") for lk in model["links"]}
        orphans = [c["name"] for c in model["components"] if c["name"] not in linked]
        if orphans and model["links"]:
            warnings.append(f"components without links: {orphans}")
    return errors, warnings


# ---------------------------------------------------------------------------
# Merge (interviews accumulate — patch semantics)
# ---------------------------------------------------------------------------

def _key_of(item: Any) -> str | None:
    if isinstance(item, dict):
        for k in ("id", "name"):
            if item.get(k):
                return str(item[k])
        if "from" in item and "to" in item:
            return f"{item['from']}->{item['to']}"
        if "question" in item:
            return str(item["question"])
        if "text" in item:
            return str(item["text"])
        if "parent" in item:
            return str(item["parent"])
    return None


def merge(base: dict, patch: dict) -> dict:
    """Merge `patch` into `base`: keyed lists upsert, scalar lists union,
    dicts recurse, scalars replace. `patch["_remove"]` = {list_key: [keys]}
    deletes items by id/name/'from->to'."""
    out = dict(base)
    removals = patch.get("_remove") or {}
    for key, val in patch.items():
        if key == "_remove":
            continue
        cur = out.get(key)
        if isinstance(val, dict) and isinstance(cur, dict):
            out[key] = merge(cur, val)
        elif isinstance(val, list) and isinstance(cur, list):
            out[key] = _merge_lists(cur, val)
        else:
            out[key] = val
    for key, keys in removals.items():
        if isinstance(out.get(key), list):
            drop = {str(k) for k in keys}
            out[key] = [i for i in out[key] if _key_of(i) not in drop and str(i) not in drop]
    return out


def _merge_lists(cur: list, new: list) -> list:
    if all(isinstance(i, dict) for i in new + cur) and any(_key_of(i) for i in new):
        result = list(cur)
        index = {_key_of(i): n for n, i in enumerate(result)}
        for item in new:
            k = _key_of(item)
            if k is not None and k in index:
                result[index[k]] = merge(result[index[k]], item)
            else:
                result.append(item)
                index[k] = len(result) - 1
        return result
    # scalar (or unkeyed) list → union preserving order
    seen = [i for i in cur]
    for item in new:
        if item not in seen:
            seen.append(item)
    return seen


# ---------------------------------------------------------------------------
# Gap analysis → ranked questions
# ---------------------------------------------------------------------------

def _empty(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        return not v.strip()
    if isinstance(v, (list, dict)):
        return len(v) == 0
    return False


def _ratio_missing(items: list[dict], key: str) -> float:
    if not items:
        return 1.0
    missing = sum(1 for i in items if _empty(_dig(i, key)))
    return missing / len(items)


def _dig(d: dict, path: str) -> Any:
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _uncertainty(model: dict, field: str) -> float:
    """0 = field is covered, 1 = completely missing. Field-specific heuristics."""
    kind = model["kind"]
    if field == "":
        return 0.35  # meta / lens questions: always available, ranked low
    if kind == "process":
        steps = model["steps"]
        tasks = [s for s in steps if s.get("type") in ("task", "handoff", "wait")]
        actors = model["actors"]
        edges = model["edges"]
        if field == "actors.approver":
            return 1.0 if actors and not any(a.get("role") == "approver" for a in actors) else 0.0
        if field == "actors.system":
            has_sys = any(a.get("role") == "system" for a in actors) or any(s.get("systems") for s in steps)
            return 0.0 if has_sys else 0.6
        if field == "steps.handoff":
            actor_of = {s["id"]: s.get("actor") for s in steps}
            cross = [e for e in edges if actor_of.get(e.get("from")) and actor_of.get(e.get("to"))
                     and actor_of[e["from"]] != actor_of[e["to"]]]
            if len(actors) > 1 and not cross and not any(s.get("type") == "handoff" for s in steps):
                return 0.8
            return 0.0 if not cross else sum(1 for e in cross if _empty(e.get("label"))) / len(cross) * 0.7
        if field == "steps.decision":
            decisions = [s for s in steps if s.get("type") == "decision"]
            if not decisions:
                return 0.7 if len(steps) >= 3 else 0.3
            return 0.0
        if field == "edges.label":
            decisions = [s for s in steps if s.get("type") == "decision"]
            if not decisions:
                return 0.0
            bad = 0
            for d in decisions:
                outs = [e for e in edges if e.get("from") == d["id"]]
                if len(outs) < 2 or any(_empty(e.get("label")) and _empty(e.get("condition")) for e in outs):
                    bad += 1
            return bad / len(decisions)
        if field.startswith("steps.metrics."):
            return _ratio_missing(tasks, field[len("steps."):])
        if field.startswith("steps."):
            return _ratio_missing(tasks, field[len("steps."):])
        if field == "edges":
            return 1.0 if steps and not edges else (0.0 if edges else 0.5)
        val = _dig(model, field)
        return 1.0 if _empty(val) else 0.0
    # wardley
    comps = model["components"]
    if field.startswith("components."):
        sub = field[len("components."):]
        if sub == "evolve_to":
            return 0.0 if any(c.get("evolve_to") is not None for c in comps) else 0.5
        if sub == "inertia":
            return 0.0 if any(c.get("inertia") for c in comps) else 0.4
        if sub == "build_buy_outsource":
            mature = [c for c in comps if float(c.get("evolution", 0) or 0) >= 0.5]
            return _ratio_missing(mature, sub) if mature else 0.0
        return _ratio_missing(comps, sub)
    if field == "links":
        if not comps:
            return 0.5
        linked = {lk.get("from") for lk in model["links"]} | {lk.get("to") for lk in model["links"]}
        return sum(1 for c in comps if c["name"] not in linked) / len(comps)
    val = _dig(model, field)
    return 1.0 if _empty(val) else 0.0


def _hedge_questions(model: dict) -> list[dict]:
    """Turn hedge words in free text into grounded follow-ups."""
    found: list[dict] = []
    texts: list[tuple[str, str, bool]] = []  # (where, text, question-mark-is-suspicious)
    if model["kind"] == "process":
        for s in model["steps"]:
            # a decision label naturally ends in '?', so only flag '?' inside notes there
            is_decision = s.get("type") == "decision"
            texts.append((f"step '{s.get('label')}'", f"{s.get('label', '')} {s.get('notes', '')}", not is_decision))
            if is_decision and s.get("notes"):
                texts.append((f"step '{s.get('label')}'", str(s.get("notes")), True))
        for e in model["edges"]:
            texts.append((f"the path {e.get('from')} → {e.get('to')}",
                          f"{e.get('label', '')} {e.get('condition', '')}", True))
        for x in model["exceptions"]:
            texts.append((f"the exception at {x.get('step')}",
                          f"{x.get('what_happens', '')} {x.get('handling', '')}", True))
    else:
        for c in model["components"]:
            texts.append((f"component '{c.get('name')}'", str(c.get("stage_rationale", "")), True))
    for where, text, qmark in texts:
        low = f" {text.lower()} "
        for h in HEDGES:
            if h == "?":
                if qmark and "?" in text:
                    found.append({"phase": "decisions", "field": where, "impact": 3, "uncertainty": 1.0,
                                  "question": f"You left a '?' in {where} — what is unknown there, and who would know?",
                                  "why": "Unknowns written into the map become silent assumptions.",
                                  "method": "Ambiguity probe"})
                continue
            if f" {h} " in low:
                found.append({"phase": "decisions", "field": where, "impact": 3, "uncertainty": 1.0,
                              "question": (f"You said '{h}' about {where} — what decides when it is *not* "
                                           "the case, and what happens then?"),
                              "why": "Hedge words mark an unstated rule or an alternative path.",
                              "method": "Ambiguity probe"})
                break
    return found


def gaps(model: dict, lens: str = "", limit: int = 5) -> list[dict]:
    """Ranked list of grounded questions: [{question, why, phase, field, method, score}]."""
    model = normalize(model)
    kind = model["kind"]
    phase_order = {p: i for i, p in enumerate(phases_for(kind))}
    candidates: list[dict] = []
    # 1. the model's own open questions first (the agent/user already flagged them)
    for oq in model.get("open_questions", []):
        q = oq.get("question") if isinstance(oq, dict) else str(oq)
        if q:
            candidates.append({"phase": "open", "field": oq.get("topic", "") if isinstance(oq, dict) else "",
                               "question": q,
                               "why": oq.get("why", "Flagged as open while mapping.") if isinstance(oq, dict) else "",
                               "method": "open question", "score": 3.5, "impact": 3, "uncertainty": 1.0})
    # 2. hedges in the text
    for hq in _hedge_questions(model):
        hq["score"] = hq["impact"] * hq["uncertainty"]
        candidates.append(hq)
    # 3. the bank, filtered by field emptiness
    bank: list[Question] = bank_for(kind)
    for q in bank:
        if lens and q["lens"] != lens:
            continue
        u = _uncertainty(model, q["field"])
        if lens:
            u = max(u, 0.5)  # a requested lens always yields its questions
        if u <= 0.0:
            continue
        candidates.append({**q, "uncertainty": u, "score": q["impact"] * u})
    # stable ranking: score desc, then phase order, then bank order
    candidates.sort(key=lambda c: (-round(c["score"], 3), phase_order.get(c["phase"], -1)))
    seen: set[str] = set()
    out: list[dict] = []
    for c in candidates:
        if c["question"] in seen:
            continue
        seen.add(c["question"])
        out.append({k: c[k] for k in ("question", "why", "phase", "field", "method", "score")})
        if len(out) >= limit:
            break
    return out


def completeness(model: dict) -> int:
    """0-100 rough completeness score (share of bank fields covered, impact-weighted)."""
    model = normalize(model)
    bank = bank_for(model["kind"])
    total = sum(q["impact"] for q in bank if q["field"])
    if not total:
        return 0
    covered = sum(q["impact"] * (1 - min(1.0, _uncertainty(model, q["field"])))
                  for q in bank if q["field"])
    return int(round(100 * covered / total))


# ---------------------------------------------------------------------------
# Emitters — Mermaid
# ---------------------------------------------------------------------------

def _mm_label(s: str) -> str:
    """Quote a label for Mermaid: strip quotes, escape via #quot;."""
    s = str(s or "").replace('"', "#quot;").replace("\n", " ").strip()
    return f'"{s}"' if s else '" "'


def _node_ids(model: dict) -> dict[str, str]:
    return {s["id"]: f"n{i + 1}" for i, s in enumerate(model["steps"])}


def _mm_node(nid: str, step: dict) -> str:
    label = _mm_label(step.get("label"))
    t = step.get("type", "task")
    if t in ("start", "end"):
        return f"{nid}([{label}])"
    if t == "decision":
        return f"{nid}{{{label}}}"
    if t == "wait":
        return f"{nid}[/{label}/]"
    if t == "handoff":
        return f"{nid}[[{label}]]"
    return f"{nid}[{label}]"


def _mm_edges(model: dict, ids: dict[str, str]) -> list[str]:
    lines = []
    for e in model["edges"]:
        a, b = ids.get(e.get("from")), ids.get(e.get("to"))
        if not a or not b:
            continue
        label = e.get("label") or e.get("condition") or ""
        if label:
            lines.append(f"  {a} -->|{_mm_label(label)}| {b}")
        else:
            lines.append(f"  {a} --> {b}")
    return lines


def emit_flowchart(model: dict) -> str:
    model = normalize(model)
    ids = _node_ids(model)
    direction = model.get("direction") or ("TD" if len(model["steps"]) > 14 else "LR")
    lines = [f"flowchart {direction}"]
    phases: dict[str, list[dict]] = {}
    for s in model["steps"]:
        phases.setdefault(s.get("phase") or "", []).append(s)
    if len(phases) > 1 or (len(phases) == 1 and "" not in phases):
        for i, (phase, steps) in enumerate(phases.items()):
            if phase:
                lines.append(f"  subgraph p{i}[{_mm_label(phase)}]")
                lines.extend(f"    {_mm_node(ids[s['id']], s)}" for s in steps)
                lines.append("  end")
            else:
                lines.extend(f"  {_mm_node(ids[s['id']], s)}" for s in steps)
    else:
        lines.extend(f"  {_mm_node(ids[s['id']], s)}" for s in model["steps"])
    lines.extend(_mm_edges(model, ids))
    return "\n".join(lines) + "\n"


def emit_swimlane(model: dict, beta: bool = True) -> str:
    """Swimlane per actor. beta=True uses Mermaid `swimlane-beta` (≥11.16);
    beta=False is the portable fallback: flowchart with a subgraph per actor."""
    model = normalize(model)
    ids = _node_ids(model)
    direction = model.get("direction") or "LR"
    header = f"swimlane-beta {direction}" if beta else f"flowchart {direction}"
    lines = [header]
    actor_name = {a["id"]: a.get("name") or a["id"] for a in model["actors"]}
    lanes: dict[str, list[dict]] = {}
    for s in model["steps"]:
        lanes.setdefault(s.get("actor") or "", []).append(s)
    for i, (actor, steps) in enumerate(lanes.items()):
        title = actor_name.get(actor, actor) or "Unassigned"
        lines.append(f"  subgraph lane{i}[{_mm_label(title)}]")
        lines.extend(f"    {_mm_node(ids[s['id']], s)}" for s in steps)
        lines.append("  end")
    lines.extend(_mm_edges(model, ids))
    return "\n".join(lines) + "\n"


def _ordered_steps(model: dict) -> list[dict]:
    """Topological-ish order following edges from the start step; unreached appended."""
    by_id = {s["id"]: s for s in model["steps"]}
    succ: dict[str, list[str]] = {}
    indeg = {sid: 0 for sid in by_id}
    for e in model["edges"]:
        if e.get("from") in by_id and e.get("to") in by_id:
            succ.setdefault(e["from"], []).append(e["to"])
            indeg[e["to"]] += 1
    starts = [s["id"] for s in model["steps"] if s.get("type") == "start"] or \
             [sid for sid in by_id if indeg[sid] == 0] or list(by_id)
    order: list[str] = []
    seen: set[str] = set()
    stack = list(reversed(starts))
    while stack:
        sid = stack.pop()
        if sid in seen:
            continue
        seen.add(sid)
        order.append(sid)
        for nxt in reversed(succ.get(sid, [])):
            if nxt not in seen:
                stack.append(nxt)
    for sid in by_id:
        if sid not in seen:
            order.append(sid)
    return [by_id[s] for s in order]


def _seq_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", s or "unassigned") or "unassigned"


def emit_sequence(model: dict) -> str:
    """Handoffs between actors as a sequence diagram."""
    model = normalize(model)
    lines = ["sequenceDiagram", "  autonumber"]
    actors = model["actors"] or [{"id": "team", "name": "Team"}]
    alias = {a["id"]: _seq_name(a["id"]) for a in actors}
    for a in actors:
        lines.append(f"  participant {alias[a['id']]} as {a.get('name') or a['id']}")
    default = actors[0]["id"]
    edge_label = {(e.get("from"), e.get("to")): (e.get("label") or e.get("condition") or "")
                  for e in model["edges"]}
    prev: dict | None = None
    for s in _ordered_steps(model):
        actor = s.get("actor") or default
        if actor not in alias:
            alias[actor] = _seq_name(actor)
            lines.insert(2, f"  participant {alias[actor]} as {actor}")
        label = s.get("label", "")
        if s.get("type") == "decision":
            lines.append(f"  Note over {alias[actor]}: Decision: {label}")
        elif prev is not None and (prev.get("actor") or default) != actor:
            src = alias[prev.get("actor") or default]
            el = edge_label.get((prev["id"], s["id"]), "")
            msg = f"{label}" + (f" ({el})" if el else "")
            lines.append(f"  {src}->>{alias[actor]}: {msg}")
        elif s.get("type") in ("start", "end"):
            lines.append(f"  Note over {alias[actor]}: {label}")
        else:
            lines.append(f"  {alias[actor]}->>{alias[actor]}: {label}")
        prev = s
    return "\n".join(lines) + "\n"


def emit_journey(model: dict) -> str:
    model = normalize(model)
    lines = ["journey", f"  title {model.get('title') or 'Process'}"]
    actor_name = {a["id"]: a.get("name") or a["id"] for a in model["actors"]}
    pains = " ".join(model.get("pain_points") or []).lower()
    current_phase = None
    for s in _ordered_steps(model):
        phase = s.get("phase") or "Process"
        if phase != current_phase:
            lines.append(f"  section {phase}")
            current_phase = phase
        score = 2 if s.get("label", "").lower() in pains else (4 if s.get("type") in ("start", "end") else 3)
        who = actor_name.get(s.get("actor"), s.get("actor") or "Team")
        label = str(s.get("label", "")).replace(":", " -")
        lines.append(f"    {label}: {score}: {who}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Emitters — Wardley (OWM text and Mermaid wardley-beta)
# ---------------------------------------------------------------------------

def _coord(item: dict, default_vis: float = 0.5, default_evo: float = 0.5) -> str:
    vis = item.get("visibility", default_vis)
    evo = item.get("evolution", default_evo)
    return f"[{float(vis):.2f}, {float(evo):.2f}]"


def emit_owm(model: dict) -> str:
    """OnlineWardleyMaps DSL (also readable by VS Code/Obsidian plugins, wardleyToGo, etc.)."""
    model = normalize(model)
    lines = [f"title {model.get('title') or 'Wardley map'}"]
    for a in model["anchors"]:
        lines.append(f"anchor {a['name']} {_coord(a, 0.97, 0.65)}")
    for c in model["components"]:
        line = f"component {c['name']} {_coord(c)}"
        if c.get("label_offset"):
            dx, dy = c["label_offset"][:2]
            line += f" label [{int(dx)}, {int(dy)}]"
        if c.get("inertia"):
            line += " inertia"
        if c.get("build_buy_outsource"):
            line += f" ({c['build_buy_outsource']})"
        lines.append(line)
    for c in model["components"]:
        if c.get("evolve_to") is not None:
            lines.append(f"evolve {c['name']} {float(c['evolve_to']):.2f}")
    for p in model["pipelines"]:
        children = p.get("children") or []
        lines.append(f"pipeline {p['parent']} {{")
        for ch in children:
            if isinstance(ch, (int, float)):
                lines.append(f"  component {p['parent']} stage [{float(ch):.2f}]")
            else:
                lines.append(f"  component {ch}")
        lines.append("}")
    for lk in model["links"]:
        line = f"{lk['from']}->{lk['to']}"
        if lk.get("label"):
            line += f"; {lk['label']}"
        lines.append(line)
    for f in model["flows"]:
        lines.append(f"{f['from']}+>{f['to']}")
    for n in model["notes"]:
        lines.append(f"note {n.get('text', '')} {_coord(n, 0.1, 0.05)}")
    lines.append("style wardley")
    return "\n".join(lines) + "\n"


def emit_wardley_mermaid(model: dict) -> str:
    """Mermaid `wardley-beta` (mermaid ≥ 11.14) — OWM-compatible subset."""
    model = normalize(model)
    lines = ["wardley-beta", f'  title "{model.get("title") or "Wardley map"}"']
    for a in model["anchors"]:
        lines.append(f"  anchor {a['name']} {_coord(a, 0.97, 0.65)}")
    for c in model["components"]:
        line = f"  component {c['name']} {_coord(c)}"
        if c.get("inertia"):
            line += " inertia"
        if c.get("build_buy_outsource"):
            line += f" ({c['build_buy_outsource']})"
        lines.append(line)
    for c in model["components"]:
        if c.get("evolve_to") is not None:
            lines.append(f"  evolve {c['name']} {float(c['evolve_to']):.2f}")
    for lk in model["links"]:
        lines.append(f"  {lk['from']} -> {lk['to']}")
    for f in model["flows"]:
        lines.append(f"  {f['from']} +> {f['to']}")
    for n in model["notes"]:
        text = str(n.get("text", "")).replace('"', "'")
        lines.append(f'  note "{text}" {_coord(n, 0.1, 0.05)}')
    return "\n".join(lines) + "\n"


def emit(model: dict, view: str) -> str:
    """Diagram text for a view (not the SVG — see wardley_svg for that)."""
    if view == "flowchart":
        return emit_flowchart(model)
    if view == "swimlane":
        return emit_swimlane(model)
    if view == "sequence":
        return emit_sequence(model)
    if view == "journey":
        return emit_journey(model)
    if view == "wardley":
        return emit_owm(model)
    if view == "wardley_mermaid":
        return emit_wardley_mermaid(model)
    raise ValueError(f"unknown view {view!r}; valid: {VIEWS}")
