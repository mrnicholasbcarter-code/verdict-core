"""Chart SVGs: valid XML, no font dependency, preserved fixture and source strings."""

from __future__ import annotations

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
