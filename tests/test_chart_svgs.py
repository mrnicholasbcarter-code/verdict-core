"""Chart SVGs: valid XML, no font dependency, preserved fixture and source strings."""

from __future__ import annotations

import ast
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "docs" / "assets"
NS = "{http://www.w3.org/2000/svg}"

# chart -> strings that must survive in <title>/<desc> (text is rendered as paths)
PRESERVED = {
    "chart-admission-funnel.svg": (
        "10 candidates, 6 admitted",
        "fixture data",
        "Source: docs/proof/demo-run/admission.json",
        "opaque_route",
        "provider_rate_limited",
    ),
    "chart-recovery.svg": (
        "fixture data, injected faults",
        "Source: docs/proof/demo-run/receipt.json",
        "quota_exhausted (injected)",
        "demo-free2/elm-coder",
    ),
    "chart-paired-fixture.svg": (
        "claims_allowed=false",
        "cache hit: not model savings",
        "quality miss: no claim",
        "Source: benchmarks/fixtures/legit_paired_savings.json",
    ),
    "chart-live-savings.svg": (
        "no routing saving measured",
        "Source: docs/proof/live-savings-2026-09-28/report.json",
    ),
}


@pytest.mark.parametrize("chart", sorted(PRESERVED))
def test_chart_svg_is_valid_self_contained_and_labelled(chart: str) -> None:
    path = ASSETS / chart
    raw = path.read_text(encoding="utf-8")
    assert path.stat().st_size < 150_000, f"{chart} too large"
    root = ET.fromstring(raw)
    assert root.tag == f"{NS}svg"
    assert root.find(f"{NS}text") is None and not list(root.iter(f"{NS}text")), chart
    assert "font-family" not in raw and "@font-face" not in raw, chart
    assert "url(http" not in raw and 'href="http' not in raw, chart
    title = root.find(f"{NS}title")
    desc = root.find(f"{NS}desc")
    assert title is not None and desc is not None, chart
    meta = f"{title.text} {desc.text}"
    for needle in PRESERVED[chart]:
        assert needle in meta, f"{chart} lost {needle!r}"


def _palette(name: str) -> dict[str, str]:
    """Read literal chart tokens without importing the optional matplotlib."""
    source = (ROOT / "scripts" / "render_charts.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            result: dict[str, str] = ast.literal_eval(node.value)
            return result
    raise AssertionError(f"Missing chart palette {name}")


def test_chart_palette_matches_verdict_design() -> None:
    from verdict import design

    if not hasattr(design, "PALETTE"):
        pytest.skip("PR #724 visual-system PALETTE is not merged into main yet")
    expected = {name.lower(): token.hex for name, token in design.PALETTE.items()}
    assert _palette("PALETTE") == expected


def test_chart_only_colours_follow_documented_derivation() -> None:
    palette = _palette("PALETTE")

    def lighten(colour: str, amount: int) -> str:
        return "#" + "".join(f"{int(colour[i : i + 2], 16) + amount:02x}" for i in (1, 3, 5))

    assert _palette("CHART_PALETTE") == {
        "secondary": lighten(palette["secondary"], 2),
        "neutral": lighten(palette["border"], 25),
        "guide": lighten(palette["border"], 25),
    }


def _contrast(foreground: str, background: str) -> float:
    """WCAG 2 relative luminance in sRGB, not a brightness approximation."""

    def luminance(colour: str) -> float:
        channels = [int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return sum(c * weight for c, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))

    dark, light = sorted((luminance(foreground), luminance(background)))
    return (light + 0.05) / (dark + 0.05)


@pytest.mark.parametrize("chart", sorted(PRESERVED))
def test_all_rendered_text_and_marks_meet_wcag_contrast(chart: str) -> None:
    """Inspect actual SVG paints, including outlined text, legends and arrows.

    Every paint is checked against both surfaces (the card and recovery tiles).
    Admission tracks are unfilled, so bars never have a third background.
    Font definitions are not paints; background fills are not data marks.
    All strokes, including guides and the decorative card border, must pass.
    """
    palette = _palette("PALETTE")
    chart_palette = _palette("CHART_PALETTE")
    backgrounds = {palette["background"], palette["surface"]}
    text_minimum = {
        palette["text"]: 7.0,
        chart_palette["secondary"]: 7.0,
        **{palette[name]: 4.5 for name in ("muted", "purple", "cyan", "amber", "red", "success")},
    }
    mark_colours = set(text_minimum) | {chart_palette["neutral"], chart_palette["guide"]}
    root = ET.parse(ASSETS / chart).getroot()
    checked = {"text": set(), "mark": set()}

    def inspect_paints(element: ET.Element, inherited: dict[str, str], text: bool = False) -> None:
        if element.tag == f"{NS}defs":
            return
        text = text or element.get("id", "").startswith("text_")
        styles = dict(inherited)
        styles.update(
            (key, element.attrib[key])
            for key in ("fill", "stroke", "opacity", "fill-opacity", "stroke-opacity")
            if key in element.attrib
        )
        styles.update(
            (key.strip(), value.strip())
            for item in element.get("style", "").split(";")
            if item.strip()
            for key, value in [item.split(":", 1)]
        )
        # Alpha would change the effective contrast. Do not silently ignore it.
        for key in ("opacity", "fill-opacity", "stroke-opacity"):
            assert float(styles.get(key, "1")) == 1, (chart, key, styles)
        if element.tag.removeprefix(NS) in {"path", "use", "rect", "line", "circle", "polygon"}:
            kind = "text" if text else "mark"
            for paint in ("fill", "stroke"):
                colour = styles.get(paint, "#000000" if paint == "fill" else "none")
                if colour == "none" or (not text and paint == "fill" and colour in backgrounds):
                    continue
                allowed = text_minimum if text else mark_colours
                assert colour in allowed, (chart, kind, "unclassified paint", colour)
                minimum = text_minimum[colour] if text else 3.0
                for background in backgrounds:
                    ratio = _contrast(colour, background)
                    assert ratio >= minimum, (
                        f"{chart}: {kind} {colour} on {background}: {ratio:.2f}:1 < {minimum}:1"
                    )
                    checked[kind].add((colour, background))
        for child in element:
            inspect_paints(child, styles, text)

    inspect_paints(root, {})
    assert checked["text"] and checked["mark"], "contrast test must inspect actual painted content"
