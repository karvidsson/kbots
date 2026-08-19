"""Process mapping: model validation/merge/gaps, emitters, tools, codex publish.

No Playwright here — rendering is exercised manually (scripts/test-tools.py);
these tests cover the deterministic parts the agent relies on.
"""

import json
import re
import xml.etree.ElementTree as ET

import pytest

from src.core.base import ToolContext
from src.lib import process_model as pm
from src.lib.process_questions import PROCESS_QUESTIONS, WARDLEY_QUESTIONS
from src.lib.wardley_svg import emit_wardley_svg
from src.tools import ingest, process_map


def _process():
    return {
        "kind": "process", "title": "Customer onboarding",
        "scope": {"start": "Contract signed", "end": "First login"},
        "trigger": ["Deal won in CRM"], "end_states": ["Customer active"],
        "actors": [{"id": "sales", "name": "Sales", "role": "initiator"},
                   {"id": "ops", "name": "Ops", "role": "performer"},
                   {"id": "legal", "name": "Legal", "role": "approver"}],
        "steps": [{"id": "s1", "label": "Deal won", "type": "start", "actor": "sales"},
                  {"id": "s2", "label": "Create record", "type": "task", "actor": "ops",
                   "systems": ["CRM"], "metrics": {"process_time": "15 min"}},
                  {"id": "s3", "label": "Needs review?", "type": "decision", "actor": "ops"},
                  {"id": "s4", "label": "Review contract", "type": "task", "actor": "legal"},
                  {"id": "s5", "label": "Done", "type": "end", "actor": "ops"}],
        "edges": [{"from": "s1", "to": "s2"}, {"from": "s2", "to": "s3"},
                  {"from": "s3", "to": "s4", "label": "yes"}, {"from": "s3", "to": "s5", "label": "no"},
                  {"from": "s4", "to": "s5"}],
    }


def _wardley():
    return {
        "kind": "wardley", "title": "Tea shop",
        "anchors": [{"name": "Business", "visibility": 0.95, "evolution": 0.63}],
        "components": [{"name": "Cup of Tea", "visibility": 0.79, "evolution": 0.61},
                       {"name": "Kettle", "visibility": 0.43, "evolution": 0.35, "inertia": True, "evolve_to": 0.62},
                       {"name": "Power", "visibility": 0.10, "evolution": 0.71, "build_buy_outsource": "outsource"}],
        "links": [{"from": "Business", "to": "Cup of Tea"}, {"from": "Cup of Tea", "to": "Kettle"},
                  {"from": "Kettle", "to": "Power"}],
        "notes": [{"text": "power is standardising", "visibility": 0.2, "evolution": 0.3}],
    }


# --- normalize / validate ---------------------------------------------------

def test_normalize_coerces_loose_shapes():
    m = pm.normalize({"title": "x", "actors": ["Sales"], "steps": ["Do it"], "trigger": "phone call"})
    assert m["kind"] == "process"
    assert m["actors"][0]["id"] == "sales"
    assert m["steps"][0] == {"label": "Do it", "id": "s1", "type": "task", "systems": [],
                             "inputs": [], "outputs": [], "metrics": {}}
    assert m["trigger"] == ["phone call"]
    w = pm.normalize({"components": [{"name": "A"}], "links": ["A->B"]})
    assert w["kind"] == "wardley" and w["links"] == [{"from": "A", "to": "B"}]


def test_validate_process_errors_and_warnings():
    m = pm.normalize(_process())
    errors, warnings = pm.validate(m)
    assert errors == [] and warnings == []
    m["edges"].append({"from": "s1", "to": "nope"})
    m["steps"].append({"id": "s1", "label": "dupe", "type": "task", "metrics": {},
                       "systems": [], "inputs": [], "outputs": []})
    m["edges"][2]["label"] = ""
    errors, warnings = pm.validate(m)
    assert any("unknown step id" in e for e in errors)
    assert any("duplicate step ids" in e for e in errors)
    assert any("unlabeled outgoing edges" in w for w in warnings)


def test_validate_wardley_rules():
    m = pm.normalize(_wardley())
    assert pm.validate(m) == ([], [])
    m["components"][0]["evolution"] = 1.4
    m["links"].append({"from": "Power", "to": "Cup of Tea"})   # upward dependency
    m["components"].append({"name": "Bad->Name", "visibility": 0.5, "evolution": 0.5})
    errors, warnings = pm.validate(m)
    assert any("outside [0,1]" in e for e in errors)
    assert any("must not contain" in e for e in errors)
    assert any("points upwards" in w for w in warnings)


# --- merge ----------------------------------------------------------------------

def test_merge_upserts_keyed_lists_and_removes():
    base = pm.normalize(_process())
    patch = {"steps": [{"id": "s2", "metrics": {"lead_time": "2 days"}, "systems": ["ERP"]},
                       {"id": "s6", "label": "Notify", "type": "task", "actor": "ops"}],
             "pain_points": ["queue"], "_remove": {"edges": ["s4->s5"]}}
    merged = pm.merge(base, patch)
    s2 = next(s for s in merged["steps"] if s["id"] == "s2")
    assert s2["metrics"] == {"process_time": "15 min", "lead_time": "2 days"}
    assert s2["systems"] == ["CRM", "ERP"]           # scalar lists union
    assert any(s["id"] == "s6" for s in merged["steps"])
    assert merged["pain_points"] == ["queue"]
    assert not any(e["from"] == "s4" and e["to"] == "s5" for e in merged["edges"])
    assert len(base["steps"]) == 5                    # base untouched


# --- gaps ---------------------------------------------------------------------

def test_gaps_are_grounded_in_missing_fields():
    qs = pm.gaps(_process(), limit=10)
    texts = " ".join(q["question"] for q in qs)
    # nothing about scope start/end (covered) …
    assert "Where exactly does the process start" not in texts
    # … but metrics/pain points are missing → asked
    assert any(q["phase"] in ("metrics", "pain", "data") for q in qs)
    assert all(set(q) >= {"question", "why", "phase", "field", "method", "score"} for q in qs)


def test_gaps_open_questions_and_hedges_rank_first():
    m = _process()
    m["open_questions"] = [{"topic": "s4", "question": "Who signs when legal is away?", "why": "no backup"}]
    m["edges"][2]["label"] = "yes, usually"
    qs = pm.gaps(m, limit=5)
    assert qs[0]["question"] == "Who signs when legal is away?"
    assert any("usually" in q["question"] for q in qs)


def test_gaps_lens_filters_to_method():
    qs = pm.gaps(_process(), lens="vsm", limit=10)
    assert qs and all(q["method"].lower().startswith(("vsm", "toc", "turtle", "as-is", "jtbd"))
                      or q["phase"] in ("open",) for q in qs)
    assert any("lead time" in q["question"].lower() for q in qs)


def test_gaps_wardley_bank():
    m = _wardley()
    qs = pm.gaps(m, limit=10)
    assert any(q["phase"] == "evolution" for q in qs)
    assert any("stage_rationale" in q["field"] for q in qs)   # no rationale given


def test_completeness_moves_with_content():
    empty = pm.completeness({"kind": "process"})
    full = pm.completeness(_process())
    assert empty < full <= 100


def test_question_bank_integrity():
    for q in PROCESS_QUESTIONS + WARDLEY_QUESTIONS:
        assert q["question"].strip().endswith("?") or q["question"].strip().endswith(")")
        assert q["impact"] in (1, 2, 3)
        assert q["phase"]


# --- emitters --------------------------------------------------------------------

def test_emit_flowchart_quotes_and_shapes():
    m = _process()
    m["steps"][1]["label"] = 'Create "record" [draft]'
    src = pm.emit_flowchart(m)
    assert src.startswith("flowchart LR")
    assert 'n1(["Deal won"])' in src
    assert 'n3{"Needs review?"}' in src
    assert 'n2["Create #quot;record#quot; [draft]"]' in src
    assert 'n3 -->|"yes"| n4' in src
    assert "n5([" in src


def test_emit_flowchart_phases_become_subgraphs():
    m = _process()
    for s in m["steps"][:2]:
        s["phase"] = "Intake"
    src = pm.emit_flowchart(m)
    assert 'subgraph p0["Intake"]' in src and src.count("end") >= 1


def test_emit_swimlane_beta_and_fallback():
    beta = pm.emit_swimlane(_process())
    assert beta.startswith("swimlane-beta LR")
    assert 'subgraph lane0["Sales"]' in beta and 'subgraph lane2["Legal"]' in beta
    fallback = pm.emit_swimlane(_process(), beta=False)
    assert fallback.startswith("flowchart LR") and 'subgraph lane1["Ops"]' in fallback


def test_emit_sequence_follows_handoffs():
    src = pm.emit_sequence(_process())
    assert "participant sales as Sales" in src
    assert "sales->>ops: Create record" in src
    assert "ops->>legal: Review contract (yes)" in src
    assert "Note over ops: Decision: Needs review?" in src


def test_emit_journey():
    src = pm.emit_journey(_process())
    assert src.startswith("journey\n  title Customer onboarding")
    assert "Create record: 3: Ops" in src


def test_emit_owm_and_mermaid_wardley():
    owm = pm.emit_owm(_wardley())
    assert "title Tea shop" in owm
    assert "anchor Business [0.95, 0.63]" in owm
    assert "component Kettle [0.43, 0.35] inertia" in owm
    assert "component Power [0.10, 0.71] (outsource)" in owm
    assert "evolve Kettle 0.62" in owm
    assert "Cup of Tea->Kettle" in owm
    assert "note power is standardising [0.20, 0.30]" in owm
    mm = pm.emit_wardley_mermaid(_wardley())
    assert mm.startswith("wardley-beta")
    assert "Cup of Tea -> Kettle" in mm and 'note "power is standardising"' in mm


def test_emit_dispatch_rejects_unknown_view():
    with pytest.raises(ValueError):
        pm.emit(_process(), "pie")


def test_wardley_svg_is_valid_and_positions_components():
    svg = emit_wardley_svg(pm.normalize(_wardley()), {"primary": "#123456"})
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")
    assert "#123456" in svg
    for label in ("Genesis", "Commodity (+utility)", "Kettle", "Power (outsource)", "Business"):
        assert label in svg
    # Kettle at evolution .35 → x left of Power at .71; visibility .43 → lower than Cup of Tea .79
    def cx(name):
        i = svg.index(f">{name}<")
        return float(re.findall(r'x="([\d.]+)"', svg[:i])[-1])
    assert cx("Kettle") < cx("Power (outsource)")
    assert "stroke-dasharray=\"6 5\"" in svg            # evolve arrow
    assert svg.count("<rect") >= 2                      # background + anchor/legend squares


# --- tools -----------------------------------------------------------------------

@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "ALLOWED_PATH_ROOTS", (tmp_path.resolve(),))
    return ToolContext(agent_id="alice", project_dir=str(tmp_path / "proj"))


async def test_save_merge_load_gaps(ctx, tmp_path):
    out = await process_map.process_model_save(ctx, "Customer Onboarding", json.dumps(_process()))
    assert "Saved customer-onboarding" in out and "completeness" in out and "Next questions" in out
    out = await process_map.process_model_save(
        ctx, "Customer Onboarding",
        json.dumps({"pain_points": ["legal queue"], "steps": [{"id": "s2", "inputs": ["contract"]}]}))
    loaded = json.loads(await process_map.process_model_load(ctx, "customer onboarding"))
    assert loaded["pain_points"] == ["legal queue"]
    assert next(s for s in loaded["steps"] if s["id"] == "s2")["inputs"] == ["contract"]
    assert loaded["title"] == "Customer onboarding"
    listing = await process_map.process_model_load(ctx, "")
    assert "customer-onboarding" in listing and "process" in listing
    gaps = await process_map.process_model_gaps(ctx, "customer onboarding", lens="bpmn")
    assert "lens=bpmn" in gaps and "1. [" in gaps


async def test_save_rejects_bad_json_and_reports_errors(ctx):
    assert "not valid JSON" in await process_map.process_model_save(ctx, "x", "{nope")
    bad = _process()
    bad["edges"].append({"from": "s1", "to": "zzz"})
    out = await process_map.process_model_save(ctx, "bad", json.dumps(bad))
    assert "ERRORS" in out
    rendered = await process_map.process_render(ctx, "bad", view="flowchart")
    assert rendered.startswith("Model has errors")


async def test_render_guards_without_browser(ctx, monkeypatch):
    await process_map.process_model_save(ctx, "p", json.dumps(_process()))
    assert "No model saved" in await process_map.process_render(ctx, "missing")
    assert "Unknown view" in await process_map.process_render(ctx, "p", view="pie")
    assert "process model" in await process_map.process_render(ctx, "p", view="wardley")
    calls = {}

    async def fake_render_diagram(ctx_, diagram, output_file="", format="png"):
        calls["source"] = diagram
        return f"Diagram saved: {output_file}"
    monkeypatch.setattr(process_map.design, "render_diagram", fake_render_diagram)
    out = await process_map.process_render(ctx, "p", view="swimlane")
    assert "swimlane rendered" in out and calls["source"].startswith("swimlane-beta")


async def test_render_swimlane_falls_back_when_beta_unsupported(ctx, monkeypatch):
    await process_map.process_model_save(ctx, "p", json.dumps(_process()))
    seen = []

    async def fake_render_diagram(ctx_, diagram, output_file="", format="png"):
        seen.append(diagram.splitlines()[0])
        if diagram.startswith("swimlane-beta"):
            return "Mermaid error: No diagram type detected"
        return f"Diagram saved: {output_file}"
    monkeypatch.setattr(process_map.design, "render_diagram", fake_render_diagram)
    out = await process_map.process_render(ctx, "p", view="swimlane")
    assert "swimlane rendered" in out
    assert seen == ["swimlane-beta LR", "flowchart LR"]


async def test_render_wardley_uses_offline_svg(ctx, monkeypatch):
    await process_map.process_model_save(ctx, "tea", json.dumps(_wardley()))
    captured = {}

    async def fake_render_svg(ctx_, svg, output_file="", to_png=False):
        captured["svg"] = svg
        captured["out"] = output_file
        return f"SVG saved: {output_file}\nPNG saved: {output_file[:-4]}.png"
    monkeypatch.setattr(process_map.design, "render_svg", fake_render_svg)
    out = await process_map.process_render(ctx, "tea")
    assert "Wardley map rendered" in out and captured["svg"].startswith("<svg")
    assert "OWM source" in out
    owm = __import__("pathlib").Path(ctx.project_dir) / "processes" / "tea" / "tea.owm"
    assert owm.read_text().startswith("title Tea shop")


# --- codex publish ------------------------------------------------------------------

def test_render_codex_doc_contains_sections():
    m = _process()
    m["open_questions"] = [{"question": "Who covers legal when away?"}]
    doc = process_map.render_codex_doc(m, "customer-onboarding", "customer-onboarding.png",
                                       pm.emit_flowchart(m), "mermaid")
    assert doc.startswith("# Customer onboarding")
    assert re.search(r"^Last updated: \d{4}-\d{2}-\d{2}$", doc, re.M)
    for h in ("## Scope", "## Actors", "## Steps", "## Decisions and exceptions",
              "## Open questions", "## Diagram source"):
        assert h in doc
    assert "![Customer onboarding](customer-onboarding.png)" in doc
    assert "```mermaid\nflowchart LR" in doc
    assert "| Legal | approver |" in doc
    assert "**Needs review?**: yes → Review contract; no → Done" in doc


def test_update_codex_index_is_idempotent(tmp_path):
    idx = tmp_path / "_index.md"
    idx.write_text("# Codex Index\n\nLast updated: 2020-01-01\n\n### Business\n- `business/a.md` — a\n\n"
                   "### Processes\n- `processes/old.md` — old\n\n## What Gets Queried\n- foo\n")
    process_map.update_codex_index(idx, "processes", "onboarding", "Customer onboarding", "process")
    text = idx.read_text()
    assert "- `processes/onboarding.md` — Customer onboarding (process map)" in text
    assert text.index("processes/onboarding.md") < text.index("## What Gets Queried")
    assert "Last updated: 2020-01-01" not in text
    process_map.update_codex_index(idx, "processes", "onboarding", "Customer onboarding v2", "process")
    text2 = idx.read_text()
    assert text2.count("processes/onboarding.md") == 1 and "v2" in text2
    # no section yet → appended
    idx2 = tmp_path / "i2.md"
    idx2.write_text("# Codex Index\n")
    process_map.update_codex_index(idx2, "processes", "x", "X", "wardley")
    assert "### Processes\n- `processes/x.md` — X (Wardley map)" in idx2.read_text()


async def test_publish_writes_doc_image_and_index(ctx, tmp_path, monkeypatch):
    codex = tmp_path / "codex"
    codex.mkdir()
    (codex / "_index.md").write_text("# Codex Index\n\nLast updated: YYYY-MM-DD\n")
    monkeypatch.setattr(process_map, "_codex_root", lambda: codex)
    await process_map.process_model_save(ctx, "Onboarding", json.dumps(_process()))
    # pretend a render exists
    png = tmp_path / "proj" / "processes" / "onboarding" / "onboarding.flowchart.png"
    png.write_bytes(b"\x89PNG fake")
    out = await process_map.process_publish(ctx, "Onboarding")
    assert "Published to codex" in out
    assert (codex / "processes" / "onboarding.md").is_file()
    assert (codex / "processes" / "onboarding.png").read_bytes() == b"\x89PNG fake"
    index = (codex / "_index.md").read_text()
    assert "`processes/onboarding.md`" in index and "YYYY" not in index
    # errors block publishing
    bad = _process()
    bad["edges"].append({"from": "s1", "to": "zzz"})
    await process_map.process_model_save(ctx, "Broken", json.dumps(bad))
    assert (await process_map.process_publish(ctx, "Broken")).startswith("Not published")


async def test_publish_renders_on_demand_when_no_image(ctx, tmp_path, monkeypatch):
    codex = tmp_path / "codex"
    codex.mkdir()
    monkeypatch.setattr(process_map, "_codex_root", lambda: codex)
    await process_map.process_model_save(ctx, "tea", json.dumps(_wardley()))

    async def fake_render_svg(ctx_, svg, output_file="", to_png=False):
        __import__("pathlib").Path(output_file[:-4] + ".png").write_bytes(b"png")
        return f"SVG saved: {output_file}\nPNG saved: {output_file[:-4]}.png"
    monkeypatch.setattr(process_map.design, "render_svg", fake_render_svg)
    out = await process_map.process_publish(ctx, "tea")
    assert "tea.png" in out
    doc = (codex / "processes" / "tea.md").read_text()
    assert "## Components" in doc and "```owm" in doc and "| Kettle | Custom built |" in doc
