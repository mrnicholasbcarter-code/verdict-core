"""README assets: links resolve, chart sources exist, the demo cast and run are valid.

No network and no matplotlib: this reads committed files and runs the receipt
verifier on the committed demo run.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from urllib.parse import unquote

from verdict.orchestration.receipt import completion_verdict, verify_run_receipt

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
ASSETS = ROOT / "docs" / "assets"
DEMO_RUN = ROOT / "docs" / "proof" / "demo-run"
MAX_ASSET_BYTES = 2 * 1024 * 1024


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def _local(target: str) -> Path | None:
    if target.startswith(("http://", "https://", "mailto:", "#")):
        return None
    return ROOT / unquote(target.split("#", 1)[0])


def _image_targets(text: str) -> list[str]:
    markdown = re.findall(r"!\[[^\]]*\]\(([^)\s]+)\)", text)
    html = re.findall(r"<img\s[^>]*src=\"([^\"]+)\"", text)
    return markdown + html


def _chart_sources() -> dict[str, tuple[str, ...]]:
    """CHART_SOURCES from scripts/render_charts.py, parsed without importing matplotlib."""
    tree = ast.parse((ROOT / "scripts" / "render_charts.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "CHART_SOURCES" for t in node.targets
        ):
            value = ast.literal_eval(node.value)
            assert isinstance(value, dict)
            return value
    raise AssertionError("scripts/render_charts.py defines no CHART_SOURCES")


def test_every_readme_image_exists_and_is_small() -> None:
    # Remote status badges (shields.io, GitHub Actions) are not repository assets.
    images = [t for t in _image_targets(_readme()) if _local(t) is not None]
    assert len(images) >= 4, images  # demo + three charts
    for target in images:
        path = _local(target)
        assert path is not None
        assert path.is_file(), f"README image missing: {target}"
        assert path.stat().st_size <= MAX_ASSET_BYTES, f"asset over 2 MB: {target}"


def test_every_readme_asset_link_exists() -> None:
    text = _readme()
    links = re.findall(r"(?<!!)\]\(([^)\s]+)\)", text) + re.findall(r"href=\"([^\"]+)\"", text)
    asset_links = [t for t in links if t.startswith(("docs/assets/", "docs/proof/demo-run"))]
    assert asset_links, "README links no demo assets"
    for target in asset_links:
        path = _local(target)
        assert path is not None and path.exists(), f"README asset link missing: {target}"


def test_each_chart_has_existing_source_data_and_a_labelled_caption() -> None:
    text = _readme()
    sources = _chart_sources()
    assert len(sources) >= 2
    for chart, data_files in sources.items():
        assert (ASSETS / chart).is_file(), f"chart not rendered: {chart}"
        assert data_files, f"{chart} lists no source data"
        for rel in data_files:
            assert (ROOT / rel).is_file(), f"{chart} source data missing: {rel}"
        if f"docs/assets/{chart}" in text:
            caption = text.split(f"docs/assets/{chart})", 1)[1].split("</sub>", 1)[0]
            assert any(rel in caption for rel in data_files), f"{chart} caption omits its data"
            assert (
                "Fixture" in caption
                or "fixture" in caption
                or "Observed" in caption
                or "observed" in caption
            ), f"{chart} caption not labelled"
    embedded = set(re.findall(r"docs/assets/(chart-[\w-]+\.svg)", text))
    assert embedded <= set(sources), f"README embeds charts with no declared source: {embedded}"


def test_demo_cast_is_valid_asciinema_v2() -> None:
    lines = (ASSETS / "demo.cast").read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    assert header["version"] == 2
    assert isinstance(header["width"], int) and isinstance(header["height"], int)
    last = -1.0
    events = [json.loads(line) for line in lines[1:] if line.strip()]
    assert events, "cast has no events"
    for event in events:
        assert isinstance(event, list) and len(event) == 3, event
        at, kind, data = event
        assert isinstance(at, (int, float)) and at >= last, event
        assert kind in {"o", "i", "m", "r"} and isinstance(data, str), event
        last = float(at)
    output = "".join(e[2] for e in events if e[1] == "o")
    assert "integrity: OK (events digest verified)" in output
    assert "events_digest mismatch" in output
    for secret_marker in ("API_KEY", "Bearer ", "sk-"):
        assert secret_marker not in output


def test_committed_demo_run_verifies_and_shows_recovery() -> None:
    assert verify_run_receipt(DEMO_RUN) == []
    receipt = json.loads((DEMO_RUN / "receipt.json").read_text(encoding="utf-8"))
    assert completion_verdict(receipt)[0] == "COMPLETE"
    assert len(receipt["reassignments"]) >= 2
    injected = [
        a for n in receipt["nodes"] for a in n.get("attempts", []) if a.get("fault_injected")
    ]
    assert {a["failure_category"] for a in injected} >= {
        "quota_exhausted",
        "rate_limited",
        "no_final_answer",
    }
    implementers = {
        a["route_id"]
        for n in receipt["nodes"]
        for a in n.get("attempts", [])
        if a.get("outcome") == "success" and a.get("route_id")
    }
    assert receipt["review"]["status"] == "PASS"
    assert receipt["review"]["route_id"] not in implementers


def test_readme_demo_block_matches_committed_run_receipt() -> None:
    text = _readme()
    receipt = json.loads((DEMO_RUN / "receipt.json").read_text(encoding="utf-8"))
    for node in receipt["nodes"]:
        chain = " -> ".join(
            f"{a.get('route_id') or 'merge'}[{a.get('outcome')}"
            + (f":{a['failure_category']}" if a.get("failure_category") else "")
            + ("*" if a.get("fault_injected") else "")
            + "]"
            for a in node["attempts"]
        )
        assert f"{node['node_id']:<18} {node['final_state']:<16} {chain}" in text
