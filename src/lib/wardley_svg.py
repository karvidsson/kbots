"""Self-contained Wardley map renderer: model dict → SVG string.

No network, no JavaScript, no external service — the SVG is plain markup that
`render_svg` (src/tools/design.py) rasterises in the local headless Chromium.

Conventions (Wardley / OnlineWardleyMaps):
  - coordinates are [visibility, evolution], both 0..1
  - evolution (x): 0 = Genesis … 1 = Commodity, stage bands at .25/.5/.75
  - visibility (y): 1 = visible to the user (top), 0 = invisible (bottom)
  - `A -> B` means A depends on B, drawn as a line; dependencies flow downwards
  - `evolve` draws a dashed arrow to the future position; `inertia` a bar
"""

from __future__ import annotations

from xml.sax.saxutils import escape

W, H = 1200, 820
LEFT, RIGHT, TOP, BOTTOM = 110, 1150, 70, 720
STAGES = ["Genesis", "Custom built", "Product (+rental)", "Commodity (+utility)"]
STAGES_ALT = {  # alternative stage labels by component type
    "practice": ["Novel", "Emerging", "Good", "Best"],
    "data": ["Unmodelled", "Divergent", "Convergent", "Modelled"],
    "knowledge": ["Concept", "Hypothesis", "Theory", "Accepted"],
}

_DEFAULT_COLORS = {
    "primary": "#4F46E5", "secondary": "#334155", "accent": "#14B8A6",
    "background": "#FFFFFF", "surface": "#FFFFFF", "text": "#1E293B", "muted": "#64748B",
}


def _x(evo: float) -> float:
    return LEFT + _clamp(evo) * (RIGHT - LEFT)


def _y(vis: float) -> float:
    return BOTTOM - _clamp(vis) * (BOTTOM - TOP)


def _clamp(v: float) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, v))


def _text(x: float, y: float, s: str, size: int = 13, fill: str = "#000",
          anchor: str = "start", weight: str = "normal", italic: bool = False) -> str:
    style = f"font-size:{size}px;font-weight:{weight};fill:{fill}"
    if italic:
        style += ";font-style:italic"
    return (f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}" style="{style}">'
            f'{escape(str(s))}</text>')


def emit_wardley_svg(model: dict, colors: dict | None = None,
                     font_family: str = "Inter, -apple-system, 'Segoe UI', Roboto, sans-serif") -> str:
    """Return a complete <svg> document for a Wardley model."""
    c = dict(_DEFAULT_COLORS)
    if colors:
        c.update({k: v for k, v in colors.items() if v})
    title = model.get("title") or "Wardley map"
    anchors = model.get("anchors") or []
    components = model.get("components") or []
    links = model.get("links") or []
    flows = model.get("flows") or []
    pipelines = model.get("pipelines") or []
    notes = model.get("notes") or []

    # Position lookup by name (anchors and components share a namespace)
    pos: dict[str, tuple[float, float]] = {}
    for a in anchors:
        pos[a["name"]] = (_x(a.get("evolution", 0.65)), _y(a.get("visibility", 0.97)))
    for comp in components:
        pos[comp["name"]] = (_x(comp.get("evolution", 0.5)), _y(comp.get("visibility", 0.5)))

    out: list[str] = []
    out.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
               f'viewBox="0 0 {W} {H}" style="font-family:{escape(font_family)};background:{c["background"]}">')
    out.append(f'<rect width="{W}" height="{H}" fill="{c["background"]}"/>')
    out.append('<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
               'markerHeight="7" orient="auto-start-reverse">'
               f'<path d="M0,0 L10,5 L0,10 z" fill="{c["secondary"]}"/></marker></defs>')

    # Title
    out.append(_text(LEFT, 38, title, 20, c["text"], weight="bold"))

    # Stage bands + grid
    for i in range(1, 4):
        x = _x(i / 4)
        out.append(f'<line x1="{x:.1f}" y1="{TOP}" x2="{x:.1f}" y2="{BOTTOM}" '
                   f'stroke="{c["muted"]}" stroke-width="1" stroke-dasharray="4 6" opacity="0.6"/>')
    for i, label in enumerate(STAGES):
        x = _x((i + 0.5) / 4)
        out.append(_text(x, BOTTOM + 24, label, 13, c["muted"], anchor="middle"))
    # Axes
    out.append(f'<line x1="{LEFT}" y1="{TOP}" x2="{LEFT}" y2="{BOTTOM}" stroke="{c["text"]}" stroke-width="1.5"/>')
    out.append(f'<line x1="{LEFT}" y1="{BOTTOM}" x2="{RIGHT}" y2="{BOTTOM}" stroke="{c["text"]}" '
               f'stroke-width="1.5" marker-end="url(#arrow)"/>')
    out.append(_text(RIGHT, BOTTOM + 48, "Evolution →", 13, c["text"], anchor="end", weight="bold"))
    out.append(_text(LEFT - 12, TOP + 4, "Visible", 12, c["muted"], anchor="end"))
    out.append(_text(LEFT - 12, BOTTOM, "Invisible", 12, c["muted"], anchor="end"))
    out.append(f'<text transform="translate(28,{(TOP + BOTTOM) / 2:.0f}) rotate(-90)" text-anchor="middle" '
               f'style="font-size:13px;font-weight:bold;fill:{c["text"]}">Value chain</text>')

    # Pipelines (behind components)
    for p in pipelines:
        parent = p.get("parent")
        if parent not in pos:
            continue
        px, py = pos[parent]
        children = p.get("children") or []
        xs = [pos[ch][0] for ch in children if ch in pos]
        if children and not xs:
            # children given as evolution numbers
            xs = [_x(ch) for ch in children if isinstance(ch, (int, float))]
        if not xs:
            continue
        x1, x2 = min(xs) - 12, max(xs) + 12
        out.append(f'<rect x="{x1:.1f}" y="{py - 6:.1f}" width="{x2 - x1:.1f}" height="24" '
                   f'fill="none" stroke="{c["secondary"]}" stroke-width="1.2"/>')
        for xc in xs:
            out.append(f'<circle cx="{xc:.1f}" cy="{py + 12:.1f}" r="4" fill="{c["surface"]}" '
                       f'stroke="{c["secondary"]}" stroke-width="1.2"/>')

    # Links (dependencies) and flows
    for link in links:
        a, b = link.get("from"), link.get("to")
        if a in pos and b in pos:
            (x1, y1), (x2, y2) = pos[a], pos[b]
            out.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                       f'stroke="{c["secondary"]}" stroke-width="1.2" opacity="0.8"/>')
            if link.get("label"):
                out.append(_text((x1 + x2) / 2 + 4, (y1 + y2) / 2 - 4, link["label"], 10, c["muted"], italic=True))
    for flow in flows:
        a, b = flow.get("from"), flow.get("to")
        if a in pos and b in pos:
            (x1, y1), (x2, y2) = pos[a], pos[b]
            out.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                       f'stroke="{c["accent"]}" stroke-width="2.4" opacity="0.9"/>')
            if flow.get("label"):
                out.append(_text((x1 + x2) / 2 + 4, (y1 + y2) / 2 - 4, flow["label"], 10, c["accent"], italic=True))

    # Evolve arrows
    for comp in components:
        target = comp.get("evolve_to")
        if target is None:
            continue
        x1, y1 = pos[comp["name"]]
        x2 = _x(target)
        out.append(f'<line x1="{x1 + 8:.1f}" y1="{y1:.1f}" x2="{x2 - 8:.1f}" y2="{y1:.1f}" '
                   f'stroke="{c["primary"]}" stroke-width="1.6" stroke-dasharray="6 5" marker-end="url(#arrow)"/>')
        out.append(f'<circle cx="{x2:.1f}" cy="{y1:.1f}" r="7" fill="{c["surface"]}" '
                   f'stroke="{c["primary"]}" stroke-width="1.6" stroke-dasharray="3 3"/>')

    # Components
    for comp in components:
        x, y = pos[comp["name"]]
        out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="7" fill="{c["surface"]}" '
                   f'stroke="{c["primary"]}" stroke-width="2"/>')
        label = comp["name"]
        tag = comp.get("build_buy_outsource")
        if tag:
            label = f"{label} ({tag})"
        dx, dy = (comp.get("label_offset") or ([26, -8] if comp.get("inertia") else [12, -8]))[:2]
        out.append(_text(x + float(dx), y + float(dy), label, 13, c["text"]))
        if comp.get("inertia"):
            out.append(f'<line x1="{x + 18:.1f}" y1="{y - 14:.1f}" x2="{x + 18:.1f}" y2="{y + 14:.1f}" '
                       f'stroke="{c["text"]}" stroke-width="4"/>')

    # Anchors (user / user need)
    for a in anchors:
        x, y = pos[a["name"]]
        out.append(f'<rect x="{x - 7:.1f}" y="{y - 7:.1f}" width="14" height="14" fill="{c["primary"]}"/>')
        out.append(_text(x + 12, y - 8, a["name"], 14, c["text"], weight="bold"))

    # Notes
    for n in notes:
        text = n.get("text") if isinstance(n, dict) else str(n)
        vis = n.get("visibility", 0.1) if isinstance(n, dict) else 0.1
        evo = n.get("evolution", 0.05) if isinstance(n, dict) else 0.05
        out.append(_text(_x(evo), _y(vis), text, 11, c["muted"], italic=True))

    # Legend
    ly = H - 28
    out.append(f'<circle cx="{LEFT + 6}" cy="{ly}" r="6" fill="{c["surface"]}" '
               f'stroke="{c["primary"]}" stroke-width="2"/>')
    out.append(_text(LEFT + 18, ly + 4, "component", 11, c["muted"]))
    out.append(f'<rect x="{LEFT + 100}" y="{ly - 6}" width="12" height="12" fill="{c["primary"]}"/>')
    out.append(_text(LEFT + 118, ly + 4, "user / need", 11, c["muted"]))
    out.append(f'<line x1="{LEFT + 210}" y1="{ly}" x2="{LEFT + 250}" y2="{ly}" stroke="{c["primary"]}" '
               f'stroke-width="1.6" stroke-dasharray="6 5"/>')
    out.append(_text(LEFT + 258, ly + 4, "evolving", 11, c["muted"]))
    out.append(f'<line x1="{LEFT + 330}" y1="{ly - 8}" x2="{LEFT + 330}" y2="{ly + 8}" '
               f'stroke="{c["text"]}" stroke-width="4"/>')
    out.append(_text(LEFT + 340, ly + 4, "inertia", 11, c["muted"]))
    out.append(f'<line x1="{LEFT + 400}" y1="{ly}" x2="{LEFT + 440}" y2="{ly}" '
               f'stroke="{c["accent"]}" stroke-width="2.4"/>')
    out.append(_text(LEFT + 448, ly + 4, "flow", 11, c["muted"]))

    out.append("</svg>")
    return "\n".join(out)
