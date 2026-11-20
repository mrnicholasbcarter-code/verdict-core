"""Tests for context-lift spend guard, CodeQL receipt-field allowlist, and related fixes.

Covers:
- SPEND: default cost_class filter is {local, free} only; cheaper needs allow_priced=True
- SPEND: priced-only catalog → blocked with zero chat calls
- SPEND: scheduled timer command never passes --allow-priced
- CodeQL: planted lift token never written to disk or stdout
- receipt.py 3a: malformed context_bytes/prompt_tokens_estimated → null + omission
- receipt.py 3b: second hydrate event → hydrate_count recorded (no silent drop)
- parsers_autodev 3c: --install-service and --uninstall are mutually exclusive
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from verdict.context_lift import _FREE_AND_PRICED_COST_CLASSES, _FREE_COST_CLASSES, _RECEIPT_FIELDS
from verdict.live_routing import Candidate, ConcreteIdentity, RouteSelection
from verdict.orchestration.receipt import EventLog, write_run_receipt

_NOW = datetime(2025, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _identity(identity_id: str, cost_class: str) -> ConcreteIdentity:
    return ConcreteIdentity(
        identity_id=identity_id,
        provider_id=identity_id.split("/")[0] if "/" in identity_id else identity_id,
        gateway_id="gw",
        cost_class=cost_class,  # type: ignore[arg-type]
        context_limit=4096,
        output_limit=512,
        tools=True,
        modalities=("text",),
        spec_captured_at=_NOW,
    )


def _candidate(identity: ConcreteIdentity) -> Candidate:
    return Candidate(ref=identity.identity_id, status="kept", reason=None, identity=identity)


def _selection_with(*identities: ConcreteIdentity) -> RouteSelection:
    candidates = tuple(_candidate(i) for i in identities)
    chosen = candidates[0]
    return RouteSelection(
        chosen=chosen,
        paid_used=False,
        cheaper_available=any(
            c.identity is not None and c.identity.cost_class in {"local", "free", "cheaper"}
            for c in candidates
        ),
        ordered=candidates,
    )


def _blocked_result(token: str = "") -> dict[str, Any]:
    return {
        "receipt": {
            "schema_version": "1",
            "identity_id": "",
            "cost_class": None,
            "endpoint": "http://localhost:9999",
            "pack_digest": None,
            "unaided_passed": None,
            "packed_passed": None,
            "conclusion": "blocked",
            "block_reason": "no_free_identity",
            "omissions": [],
        },
        # working_state must NOT appear in evidence
        "working_state": {"goal_slot": f"The unique lift token is: {token}"},
        "code": "no_free_identity",
        "error": "no free identity",
    }


def _make_simple_run_dir(
    tmp_path: Path, *, context_bytes: Any = 512, tokens: Any = 100, extra_hydrate: bool = False
) -> Path:
    """Minimal run dir with one node, one dispatch and one (or two) hydrate events."""
    from verdict.orchestration.contracts import WorkGraph, WorkNode

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    graph = WorkGraph(
        goal="g",
        nodes=(
            WorkNode(
                "a",
                "build a",
                owned_files=("a.py",),
                verification_command=("pytest",),
                barrier="int",
            ),
            WorkNode("rev", "review", kind="review", depends_on=("a",)),
        ),
    )
    (run_dir / "graph.json").write_text(json.dumps(graph.to_dict()))
    log = EventLog(run_dir / "events.jsonl")
    log.emit("run_started", run_id="r1", goal="g")
    log.emit("dispatch", node_id="a", attempt=1, route_id="cc/m", capacity_class="free")
    base_kw: dict[str, Any] = {
        "prompt_bytes": 100,
        "budget_bytes": 200,
        "truncated": False,
        "sources": [],
        "route_id": "cc/m",
        "context_plan": {},
        "input_token_budget": 1024,
        "reserve_policy": {},
        "compression": "not_performed",
        "sources_status": "ok",
        "context_files": [],
    }
    kw = dict(base_kw)
    kw["context_bytes"] = context_bytes
    if tokens is not None:
        kw["prompt_tokens_estimated"] = tokens
    log.emit("hydrate", node_id="a", **kw)
    if extra_hydrate:
        kw2 = dict(base_kw)
        kw2["context_bytes"] = 200
        kw2["prompt_tokens_estimated"] = 90
        log.emit("hydrate", node_id="a", **kw2)
    log.emit("terminal", node_id="a", attempt=1, ok=True, duration_seconds=1.0)
    log.emit("dispatch", node_id="rev", attempt=1, route_id="cc/m2", capacity_class="free")
    log.emit("review", node_id="rev", attempt=1, verdict="PASS", rationale="ok", ok=True)
    log.emit("terminal", node_id="rev", attempt=1, ok=True, duration_seconds=0.5)
    log.emit("run_finished", run_id="r1", ok=True, duration_seconds=2.0)
    return run_dir


# ---------------------------------------------------------------------------
# 1. SPEND: default allows only local/free
# ---------------------------------------------------------------------------


def test_free_cost_classes_does_not_include_cheaper() -> None:
    """_FREE_COST_CLASSES must not contain 'cheaper'."""
    assert "cheaper" not in _FREE_COST_CLASSES
    assert "local" in _FREE_COST_CLASSES
    assert "free" in _FREE_COST_CLASSES


def test_free_and_priced_cost_classes_contains_cheaper() -> None:
    """_FREE_AND_PRICED_COST_CLASSES contains cheaper (used when opted in)."""
    assert "cheaper" in _FREE_AND_PRICED_COST_CLASSES


def test_priced_only_catalog_yields_blocked_with_no_chat_calls(tmp_path: Path) -> None:
    """A catalog with only 'cheaper' routes → blocked, zero chat calls, default allow_priced."""
    cheaper_identity = _identity("vendor/model-cheap", "cheaper")
    cheaper_cand = _candidate(cheaper_identity)
    sel = RouteSelection(
        chosen=cheaper_cand, paid_used=False, cheaper_available=True, ordered=(cheaper_cand,)
    )

    chat_calls: list[Any] = []

    def _fake_execute_chat(*a: Any, **kw: Any) -> str:
        chat_calls.append(a)
        return ""

    with (
        patch("verdict.context_lift.fetch_models", return_value=([cheaper_identity], {})),
        patch("verdict.context_lift.classify_identities", return_value=[cheaper_cand]),
        patch("verdict.context_lift.select_route", return_value=sel),
        patch("verdict.context_lift.failover_order", return_value=[cheaper_cand]),
        patch("verdict.context_lift.probe_identity", return_value=True),
        patch("verdict.context_lift.execute_chat", side_effect=_fake_execute_chat),
        patch("verdict.context_lift.plan_slices", return_value=[]),
        patch(
            "verdict.context_lift.retrieve_units",
            return_value=MagicMock(units=(), omissions=(), file_count=0),
        ),
        patch("verdict.context_lift.ingest_lift_fact"),
    ):
        from verdict.context_lift import run_context_lift

        result = run_context_lift(
            base_url="http://localhost:9999",
            proof_root=tmp_path,
            allow_priced=False,  # default
        )

    assert result["receipt"]["conclusion"] == "blocked"
    assert chat_calls == [], "must make zero chat calls for priced-only catalog by default"


def test_timer_execstart_has_no_allow_priced(tmp_path: Path) -> None:
    """The timer's ExecStart command must never include --allow-priced."""
    from verdict.context_lift_service import SERVICE, manage_service

    result = manage_service(dry_run=True, unit_dir=tmp_path)
    # The ExecStart lives in the service unit file body
    service_body = result.get("files", {}).get(SERVICE, "")
    assert "--allow-priced" not in service_body, (
        f"Timer ExecStart must not include --allow-priced, got service body: {service_body!r}"
    )
    assert "bench context-lift" in service_body


# ---------------------------------------------------------------------------
# 2. CodeQL: planted token never reaches disk or stdout
# ---------------------------------------------------------------------------


def test_planted_token_never_in_evidence_file(tmp_path: Path) -> None:
    """working_state (which may contain the planted token) must not reach the evidence file."""
    planted_token = "verdict-lift-deadbeefdeadbeef00001111aaaa"
    home_dir = tmp_path / "home"
    home_dir.mkdir()

    from verdict.commands import dispatch as _dispatch

    args = argparse.Namespace(
        base_url="http://localhost:9999",
        dry_run=False,
        allow_priced=False,
        json=False,
        install_service=False,
        uninstall=False,
    )

    with (
        patch("verdict.context_lift.run_context_lift", return_value=_blocked_result(planted_token)),
        patch("verdict.paths.verdict_home", return_value=home_dir),
    ):
        _dispatch._cmd_bench_context_lift(args)

    out_dir = home_dir / "context-lift"
    written_files = [*out_dir.glob("*.json"), out_dir / "latest.json"]
    for f in (f for f in written_files if f.exists()):
        content = f.read_text()
        assert planted_token not in content, (
            f"Planted token leaked into {f.name}. working_state must not be written to disk."
        )


def test_planted_token_never_on_stdout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """working_state must not appear on stdout (JSON mode)."""
    planted_token = "verdict-lift-cafecafecafe0000333344445555"
    home_dir = tmp_path / "home"
    home_dir.mkdir()

    from verdict.commands import dispatch as _dispatch

    args = argparse.Namespace(
        base_url="http://localhost:9999",
        dry_run=False,
        allow_priced=False,
        json=True,  # JSON mode → receipt is printed to stdout
        install_service=False,
        uninstall=False,
    )

    with (
        patch("verdict.context_lift.run_context_lift", return_value=_blocked_result(planted_token)),
        patch("verdict.paths.verdict_home", return_value=home_dir),
    ):
        _dispatch._cmd_bench_context_lift(args)

    captured = capsys.readouterr()
    assert planted_token not in captured.out, (
        "Planted token must not appear on stdout (CodeQL py/clear-text-storage)"
    )


def test_receipt_fields_allowlist_covers_lift_receipt() -> None:
    """Every field LiftReceipt.to_dict() emits must be in _RECEIPT_FIELDS."""
    from verdict.context_intelligence import Omission
    from verdict.context_lift import LiftReceipt

    sample = LiftReceipt(
        identity_id="test/model",
        cost_class="free",
        endpoint="http://localhost",
        pack_digest="sha256:abc",
        unaided_passed=False,
        packed_passed=True,
        conclusion="lift",
        omissions=(Omission(category="test", reason="r", ref="x"),),
    )
    emitted_keys = set(sample.to_dict().keys())
    allowed_keys = set(_RECEIPT_FIELDS)
    unguarded = emitted_keys - allowed_keys
    assert not unguarded, (
        f"LiftReceipt emits keys not in _RECEIPT_FIELDS: {unguarded}. "
        "Add them to _RECEIPT_FIELDS or strip them from to_dict()."
    )


# ---------------------------------------------------------------------------
# 3a. receipt.py: malformed hydrate fields → null + omission, no exception
# ---------------------------------------------------------------------------


def test_malformed_context_bytes_records_null_not_exception(tmp_path: Path) -> None:
    """Non-numeric context_bytes must not raise; receipt gets null + omission string."""
    run_dir = _make_simple_run_dir(tmp_path, context_bytes="not-a-number")
    write_run_receipt(run_dir)
    receipt = json.loads((run_dir / "receipt.json").read_text())
    node_a = next(n for n in receipt["nodes"] if n["node_id"] == "a")
    attempt = node_a["attempts"][0]
    cp = attempt.get("context_pack")
    assert cp is not None, "context_pack entry must still be written even on bad input"
    assert cp["context_bytes"] is None, "non-numeric context_bytes must be null"
    assert "context_bytes_non_numeric" in cp.get("omissions", [])


def test_malformed_prompt_tokens_records_null_not_exception(tmp_path: Path) -> None:
    """Non-numeric prompt_tokens_estimated must not raise; valid fields still written."""
    run_dir = _make_simple_run_dir(tmp_path, context_bytes=512, tokens="bad-value")
    write_run_receipt(run_dir)
    receipt = json.loads((run_dir / "receipt.json").read_text())
    node_a = next(n for n in receipt["nodes"] if n["node_id"] == "a")
    attempt = node_a["attempts"][0]
    cp = attempt.get("context_pack")
    assert cp is not None
    assert cp.get("context_bytes") == 512, "valid context_bytes should still be recorded"
    assert cp.get("prompt_tokens_estimated") is None
    assert "prompt_tokens_estimated_non_numeric" in cp.get("omissions", [])


# ---------------------------------------------------------------------------
# 3b. receipt.py: second hydrate for same attempt → hydrate_count, no silent drop
# ---------------------------------------------------------------------------


def test_second_hydrate_records_hydrate_count_not_silent_drop(tmp_path: Path) -> None:
    """A second hydrate for the same attempt must set hydrate_count >= 2 (not silently lost)."""
    run_dir = _make_simple_run_dir(tmp_path, context_bytes=100, tokens=50, extra_hydrate=True)
    write_run_receipt(run_dir)
    receipt = json.loads((run_dir / "receipt.json").read_text())
    node_a = next(n for n in receipt["nodes"] if n["node_id"] == "a")
    attempt = node_a["attempts"][0]
    cp = attempt.get("context_pack")
    assert cp is not None
    assert cp.get("hydrate_count", 1) >= 2, (
        "hydrate_count must be >= 2 when two hydrate events arrive for the same attempt"
    )
    # Last hydrate's context_bytes wins (200 not 100)
    assert cp["context_bytes"] == 200


# ---------------------------------------------------------------------------
# 3c. parsers_autodev: --install-service and --uninstall are mutually exclusive
# ---------------------------------------------------------------------------


def test_install_service_and_uninstall_are_mutually_exclusive() -> None:
    """Passing both --install-service and --uninstall must cause argparse error (SystemExit)."""
    from verdict.commands.parsers_autodev import register

    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="command")
    register(subs)

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["bench", "context-lift", "--install-service", "--uninstall"])

    assert exc_info.value.code != 0


def test_install_service_alone_is_accepted() -> None:
    """--install-service alone must be accepted without error."""
    from verdict.commands.parsers_autodev import register

    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="command")
    register(subs)
    args = parser.parse_args(["bench", "context-lift", "--install-service"])
    assert args.install_service is True
    assert args.uninstall is False


def test_uninstall_alone_is_accepted() -> None:
    """--uninstall alone must be accepted without error."""
    from verdict.commands.parsers_autodev import register

    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="command")
    register(subs)
    args = parser.parse_args(["bench", "context-lift", "--uninstall"])
    assert args.uninstall is True
    assert args.install_service is False
