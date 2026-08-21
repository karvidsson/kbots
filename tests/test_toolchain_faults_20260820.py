"""Faults found by exercising the memory and process-mapping toolchains.

Each test pins one defect that shipped and was only visible from the outside as
"the tool did nothing" or "the renderer is broken".
"""

import asyncio
import os
import tempfile

import pytest

from src.lib import process_model as pm

# --- memory ---------------------------------------------------------------

def test_memory_forget_accepts_the_id_that_memory_store_returns():
    """The declared type has to match the column, or the tool cannot be called.

    memories.id is TEXT holding UUIDs. Declaring int meant no value both passed
    schema validation and matched a row: memory_forget could delete nothing at
    all, while reading as a working feature.
    """
    from src.core.registry import Registry
    from src.core.tools import get_all_tools
    Registry().discover()
    forget = get_all_tools()["memory_forget"]
    param = {p.name: p for p in forget.parameters}["memory_id"]
    assert param.type == "string", f"memory_id is {param.type}, but ids are UUIDs"


def test_store_then_forget_round_trips_on_the_real_backend():
    from src.memory.sqlite import SQLiteMemory

    d = tempfile.mkdtemp()
    mem = SQLiteMemory(config={"path": os.path.join(d, "m.db")})

    async def go():
        mid = await mem.store(content="round trip", type="semantic", agent_id="t")
        assert isinstance(mid, str) and "-" in mid, "ids are UUIDs, not integers"
        await mem.forget(mid)
        return mem.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

    assert asyncio.run(go()) == 0


# --- process model --------------------------------------------------------

def test_step_label_falls_back_to_name_not_to_id():
    """{"id": "s1", "name": "Log in"} must not render as a box saying "s1"."""
    m = pm.normalize({"kind": "process",
                      "steps": [{"id": "s1", "name": "Log in to the portal"}]})
    assert m["steps"][0]["label"] == "Log in to the portal"


def test_step_with_neither_label_nor_name_still_gets_one():
    m = pm.normalize({"kind": "process", "steps": [{"id": "s9"}]})
    assert m["steps"][0]["label"] == "s9"


def test_explicit_label_wins_over_name():
    m = pm.normalize({"kind": "process",
                      "steps": [{"id": "s1", "name": "raw", "label": "chosen"}]})
    assert m["steps"][0]["label"] == "chosen"


def test_wardley_links_may_use_component_ids():
    """Process edges address steps by id; carrying the habit over must work."""
    m = pm.normalize({
        "kind": "wardley",
        "components": [{"id": "c1", "name": "Checkout", "visibility": 0.8},
                       {"id": "c2", "name": "Payments", "visibility": 0.4}],
        "links": [{"from": "c1", "to": "c2"}],
    })
    errors, _ = pm.validate(m)
    assert not errors, errors
    assert m["links"][0] == {"from": "Checkout", "to": "Payments"}


def test_wardley_links_by_name_still_work():
    m = pm.normalize({
        "kind": "wardley",
        "components": [{"name": "Checkout", "visibility": 0.8},
                       {"name": "Payments", "visibility": 0.4}],
        "links": [{"from": "Checkout", "to": "Payments"}],
    })
    assert not pm.validate(m)[0]


def test_unknown_link_endpoint_names_the_valid_options():
    m = pm.normalize({"kind": "wardley",
                      "components": [{"name": "Checkout"}],
                      "links": [{"from": "Checkout", "to": "Ghost"}]})
    errors, _ = pm.validate(m)
    assert errors and "Checkout" in errors[0], errors


def test_component_without_a_name_is_a_validation_error_not_a_keyerror():
    """It used to surface as `Error executing tool process_model_save: 'name'`."""
    m = pm.normalize({"kind": "wardley", "components": [{"visibility": 0.5}]})
    errors, _ = pm.validate(m)          # must not raise
    assert errors and "name" in errors[0].lower()
    assert "components[0]" in errors[0], "the offending entry must be identified"


@pytest.mark.parametrize("kind,payload", [
    ("wardley", {"movements": [{"from": 0.2, "to": 0.7}]}),
    ("process", {"stakeholders": ["someone"]}),
])
def test_unknown_top_level_keys_are_reported_not_swallowed(kind, payload):
    """Content written into a non-existent field persisted and rendered nothing."""
    m = pm.normalize({"kind": kind, "title": "t", **payload})
    _, warnings = pm.validate(m)
    key = next(iter(payload))
    assert any(key in w for w in warnings), warnings


def test_a_well_formed_model_produces_no_unknown_key_noise():
    m = pm.normalize({"kind": "process", "title": "t", "purpose": "p",
                      "scope": {"start": "a", "end": "b"},
                      "actors": [{"id": "a1", "name": "Ops"}],
                      "steps": [{"id": "s1", "label": "Do it", "type": "start"},
                                {"id": "s2", "label": "Done", "type": "end"}],
                      "edges": [{"from": "s1", "to": "s2"}]})
    _, warnings = pm.validate(m)
    assert not [w for w in warnings if "unknown top-level" in w], warnings


# --- diagram rendering ----------------------------------------------------

def test_edge_labels_are_not_painted_the_colour_of_their_background():
    """Mermaid colours every span with nodeTextColor (white, for filled nodes).

    An edge label is a bare span on a white edgeLabelBackground, so branch
    conditions rendered white-on-white: present in the SVG, invisible in the
    image. A decision diamond then shows unlabelled arrows and the flowchart
    reads as complete while hiding the one thing the decision is for.
    """
    from src.tools.design import _edge_label_css, _mermaid_html

    css = _edge_label_css("#1E293B")
    assert "edgeLabel" in css and "#1E293B" in css
    assert "!important" in css, "it has to beat mermaid's own span rule"

    html = _mermaid_html("flowchart TD\n a-->b", {"primaryTextColor": "#FFFFFF"}, css)
    assert "themeCSS" in html, "themeCSS is what reaches the generated SVG"
    assert "#1E293B" in html


def test_edge_label_css_does_not_restyle_node_text():
    from src.tools.design import _edge_label_css

    css = _edge_label_css("#1E293B")
    # Every selector must be scoped to an edge label; a bare `span` or `.label`
    # would repaint node text and undo the white-on-red contrast.
    for selector in css.split("{")[0].split(","):
        assert "edgeLabel" in selector, f"unscoped selector: {selector.strip()}"
