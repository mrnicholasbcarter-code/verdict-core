"""Guards run on every phase (spec v6.1 section 6 guard list)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import verdict.omniroute_pipeline as pipeline

PKG = Path(pipeline.__file__).parent

FORBIDDEN = (
    r"/api/providers/test-batch",
    r"/api/providers/[^\"' ]*/test\b",
    r"/api/providers/validate",
    r"health-autopilot/actions",
    r"/api/discovery/scan",
    r"pick_fastest_model",
    r"autoHideFailed\"?\s*[:=]\s*[Tt]rue",
)
ADMIN_KEY_MARKERS = ("omniroute.env", "OMNIROUTE_API_KEY", "omniroute-admin", "admin_key")
WRITE_CALL = re.compile(r"\.(put|post|delete|patch)\(|method\s*=\s*[\"'](PUT|POST|DELETE|PATCH)")
WRITE_TARGET = re.compile(r"/api/combos|/api/providers|sync-models")
REQUEST_BUILDER = "revive.py"


def _sources() -> list[Path]:
    found = sorted(PKG.rglob("*.py"))
    assert found
    return found


def test_no_forbidden_endpoints_scan() -> None:
    for src in _sources():
        text = src.read_text(encoding="utf-8")
        for pat in FORBIDDEN:
            assert not re.search(pat, text), f"{src.name}: {pat}"


def test_no_admin_key_reference() -> None:
    for src in _sources():
        text = src.read_text(encoding="utf-8")
        for marker in ADMIN_KEY_MARKERS:
            assert marker not in text, f"{src.name} references {marker}"


def test_no_combo_or_provider_writes() -> None:
    for src in _sources():
        text = src.read_text(encoding="utf-8")
        if src.name == REQUEST_BUILDER:
            assert not WRITE_CALL.search(text), "revive builds requests, never sends them"
            continue
        for line in text.splitlines():
            assert not (WRITE_CALL.search(line) and WRITE_TARGET.search(line)), (
                f"{src.name}: {line}"
            )


def test_paid_and_blocklisted_prefixes_never_free() -> None:
    from verdict.omniroute_pipeline.free_rule import FreeStatus, classify_free

    for route in ("cc/x", "claude/x", "cx/x", "kc/glm-5.2:free", "opencode/x", "ahm/x"):
        assert classify_free(route, None, None) is not FreeStatus.FREE


@pytest.mark.xfail(strict=True, raises=ImportError, reason="phase b: probe policy gate")
def test_probe_refuses_non_free_route() -> None:
    from verdict.omniroute_pipeline.probes import probe_allowed  # type: ignore[attr-defined]

    for route in ("cc/x", "claude/x", "cx/x", "kc/some/paid", "opencode/x", "ahm/x"):
        assert probe_allowed(route, free_status="not_free") is False
    assert probe_allowed("kc/reflection/beam-free", free_status="unverified_free") is False


@pytest.mark.xfail(strict=True, raises=ImportError, reason="phase b/e: ToS-avoid policy")
def test_tos_avoid_fails_closed() -> None:
    from verdict.omniroute_pipeline.probes import probe_allowed  # type: ignore[attr-defined]
    from verdict.omniroute_pipeline.switcher import (  # type: ignore[import-not-found]
        Candidate,
        build_pool,
    )

    for route in ("agy/gemini-3.8-flash-high", "kiro/x"):
        assert probe_allowed(route, free_status="free") is False
    cand = Candidate(
        route="agy/gemini-3.8-flash-high",
        provider="agy",
        elo=1496,
        usable_context=1_000_000,
        eligible=True,
        tool_ok=True,
        free=True,
        tos_avoid=True,
    )
    assert build_pool([cand]).members == ()


@pytest.mark.xfail(strict=True, raises=ImportError, reason="phase f: revive requests")
def test_autohide_false_asserted() -> None:
    from verdict.omniroute_pipeline.revive import (  # type: ignore[import-not-found]
        build_pinned_test_all_payload,
    )

    payload = build_pinned_test_all_payload(model_id="nvidia/z-ai/glm-5.3", connection_id="c1")
    assert payload["autoHideFailed"] is False
    assert payload["connectionId"] == "c1"
    assert payload["models"] == ["nvidia/z-ai/glm-5.3"]
