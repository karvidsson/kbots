"""Generated avatars must survive Discord's circular crop.

Discord displays a bot avatar clipped to the circle *inscribed* in the square
image, so anything outside that circle is simply cut off. The generator drew a
rounded badge against a 160-unit face whose furthest painted point was 89 units
from centre, against a crop radius of 80 — every avatar lost about 11% of its
corners, which was visible on the live bots as sliced-off frame.

The maths here is deliberately re-derived rather than imported from
avatar_gen: a test that calls the same helper the code uses would agree with a
wrong implementation. Everything is computed from the emitted SVG, so it also
catches artwork that outgrows the frame later.

No rasterising needed — the shapes are analytic.
"""

import math
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.lib.avatar_gen import ACCENTS, EYES, build_svg  # noqa: E402

_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def _floats(text: str) -> list[float]:
    return [float(m) for m in _NUM.findall(text or "")]


def _stroke(el) -> float:
    """Half the stroke width — how far past its own path a shape paints.

    Round caps and joins extend by the same half width, so this bounds them too.
    """
    return float(el.get("stroke-width", 0) or 0) / 2


def _rect_reach(el) -> float:
    """Furthest distance from the rect's own centre that the rect paints.

    On a rounded rect the extreme is on a corner arc, not on a flat edge: take
    the arc's centre, then travel out along the diagonal by the arc radius.
    """
    w, h = float(el.get("width")), float(el.get("height"))
    rx = float(el.get("rx", 0) or 0)
    half = _stroke(el)
    if rx <= 0:
        return math.hypot(w / 2 + half, h / 2 + half)
    corner = rx + half
    return math.hypot(w / 2 + half - corner, h / 2 + half - corner) + corner


def _painted_points(svg: str) -> list[tuple[float, float, float]]:
    """(x, y, reach) for every painted shape: a centre and a bounding radius."""
    root = ET.fromstring(svg)
    out = []
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "rect":
            cx = float(el.get("x", 0)) + float(el.get("width")) / 2
            cy = float(el.get("y", 0)) + float(el.get("height")) / 2
            out.append((cx, cy, _rect_reach(el)))
        elif tag == "circle":
            out.append((float(el.get("cx")), float(el.get("cy")),
                        float(el.get("r")) + _stroke(el)))
        elif tag == "ellipse":
            # A rotate() about the ellipse's own centre cannot push it further
            # from that centre than its longer semi-axis, so this bounds any
            # rotation without having to apply the transform.
            reach = max(float(el.get("rx")), float(el.get("ry"))) + _stroke(el)
            out.append((float(el.get("cx")), float(el.get("cy")), reach))
        elif tag == "path":
            # A Bézier lies inside the convex hull of its control points, so
            # bounding the control points bounds the curve. These paths carry
            # only coordinate pairs (no arc flags), so every number is a coord.
            nums = _floats(el.get("d"))
            for x, y in zip(nums[::2], nums[1::2]):
                out.append((x, y, _stroke(el)))
    return out


def _crop(svg: str) -> tuple[float, float, float]:
    """Centre and radius of the circle Discord crops to (inscribed in viewBox)."""
    vb = _floats(ET.fromstring(svg).get("viewBox"))
    min_x, min_y, width, height = vb
    return min_x + width / 2, min_y + height / 2, min(width, height) / 2


def _overflow_ratio(svg: str) -> float:
    """Furthest painted point as a fraction of the crop radius. >1 means clipped."""
    cx, cy, radius = _crop(svg)
    return max(math.hypot(x - cx, y - cy) + reach
               for x, y, reach in _painted_points(svg)) / radius


@pytest.mark.parametrize("style", sorted(EYES))
def test_avatar_fits_inside_discords_circular_crop(style):
    svg = build_svg(style, ACCENTS["red"])
    ratio = _overflow_ratio(svg)
    assert ratio <= 1.0, (
        f"'{style}' avatar overflows Discord's circular crop by "
        f"{(ratio - 1) * 100:.1f}% — it will be visibly sliced off.")


@pytest.mark.parametrize("style", sorted(EYES))
def test_avatar_still_fills_the_crop_circle(style):
    """The other half of the fit: shrinking the artwork must not be the fix.

    Without this, widening the viewBox until anything fits would pass the test
    above while turning the avatar into a dot in a sea of transparency.
    """
    ratio = _overflow_ratio(build_svg(style, ACCENTS["red"]))
    assert ratio >= 0.85, (
        f"'{style}' avatar fills only {ratio * 100:.0f}% of the crop circle — "
        "it is inset far more than needed and will look small.")


def test_badge_is_centred_in_the_view():
    """An off-centre badge clips on one side while looking fine on the other."""
    svg = build_svg("ring", ACCENTS["red"])
    crop_x, crop_y, _ = _crop(svg)
    rect = next(el for el in ET.fromstring(svg).iter() if el.tag.endswith("rect"))
    badge_x = float(rect.get("x")) + float(rect.get("width")) / 2
    badge_y = float(rect.get("y")) + float(rect.get("height")) / 2
    assert (badge_x, badge_y) == pytest.approx((crop_x, crop_y))


SHIPPED_AVATARS = sorted(
    (Path(__file__).resolve().parent.parent / "assets" / "avatars").glob("*.svg"))


def test_there_are_shipped_avatars_to_check():
    """A glob that silently matches nothing would make the next test a no-op."""
    assert SHIPPED_AVATARS, "no assets/avatars/*.svg found — has the path moved?"


@pytest.mark.parametrize("svg_path", SHIPPED_AVATARS, ids=lambda p: p.stem)
def test_shipped_avatar_assets_fit_the_crop(svg_path):
    """The sample avatars are hand-authored, so the generator cannot vouch for them.

    assets/README.md points people at these as the starting point for a new
    avatar, which makes them a second source of the clipping bug: fixing
    build_svg does nothing for someone who copies a file. They are checked with
    the same maths as generated output so the two cannot drift apart.
    """
    ratio = _overflow_ratio(svg_path.read_text())
    assert ratio <= 1.0, (
        f"{svg_path.name} overflows Discord's circular crop by "
        f"{(ratio - 1) * 100:.1f}% — copying it reproduces the clipped avatar.")


def test_the_original_face_really_did_clip():
    """Guards the checks above against passing vacuously.

    If the reach calculation were wrong it would likely report 'fits' for
    anything, including the geometry that demonstrably clipped on Discord.
    Re-running it against the original 160-unit face must still say clipped.
    """
    svg = build_svg("ring", ACCENTS["red"]).replace(
        ET.fromstring(build_svg("ring", ACCENTS["red"])).get("viewBox"), "0 0 160 160")
    ratio = _overflow_ratio(svg)
    assert ratio > 1.10, f"expected the old face to clip by ~11%, measured {ratio:.3f}"
