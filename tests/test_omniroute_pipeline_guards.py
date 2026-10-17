"""Guard specs: forbidden endpoints and autoHideFailed (spec v6.1 section 1 Forbidden list)."""

from __future__ import annotations

import re
from pathlib import Path

import verdict.omniroute_pipeline as pipeline
from verdict.omniroute_pipeline.revive import build_pinned_test_all_payload

FORBIDDEN = (
    r"/api/providers/test-batch",
    r"/api/providers/[^\"' ]*/test\b",
    r"/api/providers/validate",
    r"health-autopilot/actions",
    r"/api/discovery/scan",
    r"pick_fastest_model",
    r"autoHideFailed\"?\s*[:=]\s*[Tt]rue",
)


def test_no_forbidden_endpoints_scan() -> None:
    pkg = Path(pipeline.__file__).parent
    sources = list(pkg.rglob("*.py"))
    assert sources
    for src in sources:
        text = src.read_text(encoding="utf-8")
        for pat in FORBIDDEN:
            assert not re.search(pat, text), f"{src.name}: {pat}"


def test_autohide_false_asserted() -> None:
    payload = build_pinned_test_all_payload(model_id="nvidia/z-ai/glm-5.3", connection_id="c1")
    assert payload["autoHideFailed"] is False
    assert payload["connectionId"] == "c1"
    assert payload["models"] == ["nvidia/z-ai/glm-5.3"]
