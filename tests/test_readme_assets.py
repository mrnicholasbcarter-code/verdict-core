"""Demo assets: links resolve, chart sources exist, the demo cast and run are valid.

Fixture media lives in docs/guides/offline-demo.md (labelled "offline fixture"), never in
README.md. The README may only embed live media from a committed docs/proof/*live* bundle.

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
OFFLINE_GUIDE = ROOT / "docs" / "guides" / "offline-demo.md"
FIXTURE_MEDIA = (
    "demo.svg",
    "demo-tui.svg",
    "verified-models.svg",
    "chart-admission-funnel.svg",
    "chart-recovery.svg",
    "chart-paired-fixture.svg",
    "chart-live-savings.svg",
)
ASSETS = ROOT / "docs" / "assets"
DEMO_RUN = ROOT / "docs" / "proof" / "demo-run"
MAX_ASSET_BYTES = 2 * 1024 * 1024


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def _guide() -> str:
    return OFFLINE_GUIDE.read_text(encoding="utf-8")


def _local(target: str, base: Path = ROOT) -> Path | None:
    if target.startswith(("http://", "https://", "mailto:", "#")):
        return None
    return (base / unquote(target.split("#", 1)[0])).resolve()


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


def test_readme_embeds_no_fixture_media() -> None:
    """Operator rule: no fixture or replayed media on the product page."""
    text = _readme()
    for name in FIXTURE_MEDIA:
        assert f"docs/assets/{name}" not in text, f"README embeds fixture media: {name}"
    assert "CLAIMS VERIFIED" not in text
    assert "alpha/claude-a" not in text
    for target in _image_targets(text):
        if _local(target) is not None:
            assert target.startswith("docs/proof/") and "live" in target, (
                f"README image is not from a live proof bundle: {target}"
            )


def test_offline_guide_is_labelled_and_its_images_exist() -> None:
    text = _guide()
    assert "**Offline fixture.**" in text.split("\n## ", 1)[0]
    images = [t for t in _image_targets(text) if _local(t, OFFLINE_GUIDE.parent) is not None]
    assert len(images) >= 6, images  # 3 recordings + 3 fixture charts
    for target in images:
        path = _local(target, OFFLINE_GUIDE.parent)
        assert path is not None
        assert path.is_file(), f"offline guide image missing: {target}"
        assert path.stat().st_size <= MAX_ASSET_BYTES, f"asset over 2 MB: {target}"


def test_every_offline_guide_asset_link_exists() -> None:
    text = _guide()
    links = re.findall(r"(?<!!)\]\(([^)\s]+)\)", text) + re.findall(r"href=\"([^\"]+)\"", text)
    asset_links = [t for t in links if "/assets/" in t or "/proof/demo-run" in t]
    assert asset_links, "offline guide links no demo assets"
    for target in asset_links:
        path = _local(target, OFFLINE_GUIDE.parent)
        assert path is not None and path.exists(), f"offline guide asset link missing: {target}"


def test_each_chart_has_existing_source_data_and_a_labelled_caption() -> None:
    sources = _chart_sources()
    assert len(sources) >= 2
    for doc_path in (OFFLINE_GUIDE, ROOT / "docs" / "benchmarks" / "README.md"):
        text = doc_path.read_text(encoding="utf-8")
        for chart, data_files in sources.items():
            assert (ASSETS / chart).is_file(), f"chart not rendered: {chart}"
            assert data_files, f"{chart} lists no source data"
            for rel in data_files:
                assert (ROOT / rel).is_file(), f"{chart} source data missing: {rel}"
            marker = f"assets/{chart})"
            if marker in text:
                caption = text.split(marker, 1)[1].split("</sub>", 1)[0]
                assert any(rel.removeprefix("docs/") in caption for rel in data_files), (
                    f"{chart} caption omits its data"
                )
                assert (
                    "Fixture" in caption
                    or "fixture" in caption
                    or "Observed" in caption
                    or "observed" in caption
                ), f"{chart} caption not labelled"
        embedded = set(re.findall(r"assets/(chart-[\w-]+\.svg)", text))
        assert embedded <= set(sources), f"{doc_path.name} embeds undeclared charts: {embedded}"


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
    assert "reassign" in output.casefold()
    assert "COMPLETE" in output
    assert "offline scenario, scripted workers, injected faults" in header["title"]
    assert "scripts/record_tui_demo.py --scenario" in header["title"]
    for secret_marker in ("API_KEY", "Bearer ", "sk-"):
        assert secret_marker not in output


def test_demo_tui_cast_is_valid_asciinema_v2() -> None:
    """The full offline scenario cast has truthful provenance and completion."""
    lines = (ASSETS / "demo-tui.cast").read_text(encoding="utf-8").splitlines()
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
    # Full recording includes startup, real recovery, verification and tamper rejection.
    output = "".join(e[2] for e in events if e[1] == "o")
    assert "offline scenario, scripted workers, injected faults" in header["title"]
    assert "replay speed" in header["title"]
    assert "scripts/record_tui_demo.py --scenario" in header["title"]
    for marker in ("VERDICT", "COMPLETE", "cooldown", "reassign", "events_digest mismatch"):
        assert marker.casefold() in output.casefold()
    assert "real models" not in output
    assert "integrity: OK (events digest verified)" in output
    # No secrets in the recording
    for secret_marker in ("API_KEY", "Bearer ", "sk-"):
        assert secret_marker not in output


def _visible_svg_text(svg: str) -> str:
    return "".join(re.findall(r"<text[^>]*>([^<]*)</text>", svg))


def test_posters_are_static_and_offline_guide_prefers_reduced_motion() -> None:
    """Posters have no animation, and reduced-motion viewers get them."""
    readme = _guide()
    for animated_name, poster_name in (
        ("demo.svg", "demo-poster.svg"),
        ("demo-tui.svg", "demo-tui-poster.svg"),
    ):
        poster = (ASSETS / poster_name).read_text(encoding="utf-8")
        animated = (ASSETS / animated_name).read_text(encoding="utf-8")
        assert "<animate" not in poster and "@keyframes" not in poster
        assert "@keyframes" in animated
        assert _visible_svg_text(poster).strip()
        first = re.search(r"@keyframes\s*\w+\{0%\{[^}]*\}", animated)
        assert first is not None
        assert f'(prefers-reduced-motion: reduce)" srcset="../assets/{poster_name}"' in readme
        assert f'src="../assets/{animated_name}"' in readme


def test_posters_show_complete_cockpit_state() -> None:
    """Posters are rendered from the COMPLETE cockpit frame: all nodes VALIDATED,
    failure/reassign history visible, review PASS.
    """
    for poster_name in ("demo-poster.svg", "demo-tui-poster.svg"):
        poster = (ASSETS / poster_name).read_text(encoding="utf-8")
        text = _visible_svg_text(poster)
        assert "VALIDATED" in text, f"{poster_name}: expected VALIDATED in poster text"
        assert "REASSIGN" in text.upper(), f"{poster_name}: expected REASSIGN in poster text"
        # svg-term uses a 1.67 font size and 1.3 line-height. The viewport should
        # end after the final occupied row, not at the original 72-row PTY height.
        viewbox = re.search(r'viewBox="0 0 110 ([0-9.]+)"', poster)
        assert viewbox is not None
        row_height = 1.67 * 1.3
        content_bottom = max(float(y) for y in re.findall(r'<text[^>]* y="([0-9.]+)"', poster))
        assert 0 < float(viewbox.group(1)) - content_bottom < row_height


def test_animated_first_frame_is_not_blank() -> None:
    """Frame 0 of each recording shows text, not only a cursor."""
    for name in ("demo.svg", "demo-tui.svg"):
        svg = (ASSETS / name).read_text(encoding="utf-8")
        first = re.search(r'<symbol id="1">(.*?)</symbol>', svg)
        assert first is not None
        assert _visible_svg_text(first.group(1)).strip()


def test_verified_models_cast_is_valid_asciinema_v2() -> None:
    """Offline 0.5.0 feature demo: fixture evidence only, no live calls."""
    lines = (ASSETS / "verified-models.cast").read_text(encoding="utf-8").splitlines()
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
    assert "VERIFIED" in output
    assert "STALE" in output
    assert "Apply this exact interactive scope?" in output
    assert "offline scenario, scripted fixture evidence (no live calls)" in header["title"]
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


def test_offline_guide_demo_block_matches_committed_run_receipt() -> None:
    text = _guide()
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


def test_recording_svgs_are_small_and_use_portable_fonts() -> None:
    for name in ("demo.svg", "demo-tui.svg", "demo-poster.svg", "demo-tui-poster.svg"):
        path = ASSETS / name
        assert path.stat().st_size < 1024 * 1024, f"recording exceeds 1 MiB: {name}"
        svg = path.read_text(encoding="utf-8")
        families = re.findall(r'font-family="([^"]+)"', svg)
        assert families
        assert all(family.split(",")[-1].strip() == "monospace" for family in families)
        assert "Powerline" not in svg and "Nerd" not in svg
        assert not any(0xE000 <= ord(char) <= 0xF8FF for char in svg)
