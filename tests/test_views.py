"""CLI goldens for `verdict routing` and `verdict context`.

The parity tests spy on the domain view builders (routing_view /
context_view), never on verdict.actions.
"""

from __future__ import annotations

import json
import os
import re
import sys
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROOF_LIVE = ROOT / "docs" / "proof" / "live-controller-run"
PROOF_DEMO = ROOT / "docs" / "proof" / "demo-run"
SECRET_RE = re.compile(r"(sk-|api[_-]?key|Bearer\s+[A-Za-z0-9._\-]{8,})", re.IGNORECASE)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def run_cli(*argv: str, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Call verdict.cli.main() with a forced-plain environment."""
    old_argv = sys.argv[:]
    old_stdout, old_stderr = sys.stdout, sys.stderr
    old_env = os.environ.copy()
    out, err = StringIO(), StringIO()
    sys.argv = ["verdict", *argv]
    sys.stdout, sys.stderr = out, err
    os.environ["VERDICT_PLAIN"] = "1"
    os.environ["NO_COLOR"] = "1"
    os.environ.pop("FORCE_COLOR", None)
    if env:
        os.environ.update(env)
    code = 0
    try:
        from verdict.cli import main

        main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.argv = old_argv
        sys.stdout, sys.stderr = old_stdout, old_stderr
        os.environ.clear()
        os.environ.update(old_env)
    return code, out.getvalue(), err.getvalue()


def _assert_no_secrets(text: str) -> None:
    assert not SECRET_RE.search(text), text[:400]
    assert "BEGIN PRIVATE" not in text


@pytest.mark.parametrize("command", ["routing", "context"])
@pytest.mark.parametrize("proof", [PROOF_LIVE, PROOF_DEMO], ids=["live", "demo"])
def test_text_golden_shape(command: str, proof: Path) -> None:
    code, out, err = run_cli(command, str(proof))
    assert code == 0, err
    assert err == ""
    assert ANSI_RE.search(out) is None
    _assert_no_secrets(out)
    if command == "routing":
        assert out.startswith("routing\n")
        assert "source=recorded" in out
        assert "node " in out
    else:
        assert out.startswith("context\n")
        assert "budget  run=" in out
        assert "node " in out


@pytest.mark.parametrize("command", ["routing", "context"])
@pytest.mark.parametrize("proof", [PROOF_LIVE, PROOF_DEMO], ids=["live", "demo"])
def test_json_is_stable_and_secret_free(command: str, proof: Path) -> None:
    code, out, err = run_cli(command, str(proof), "--json")
    assert code == 0, err
    first = json.loads(out)
    again_code, again, _ = run_cli(command, str(proof), "--json")
    assert again_code == 0
    second = json.loads(again)
    # generated_at is stamped at read time; every other fact is stable.
    first.pop("generated_at", None)
    second.pop("generated_at", None)
    assert second == first
    rendered = json.dumps(json.loads(out), indent=2, sort_keys=True) + "\n"
    assert rendered == out
    _assert_no_secrets(out)
    if command == "routing":
        assert first["source"] == "recorded"
        assert first["evaluations"]
        assert "schema_version" in first
    else:
        assert "aggregate" in first
        assert first["nodes"]
        assert "schema_version" in first


def _run_with_candidates(tmp_path: Path) -> Path:
    """A tiny recorded run whose eligibility events carry candidate rows."""
    run = tmp_path / "run"
    run.mkdir()
    events = [
        {
            "type": "eligibility",
            "node_id": "alpha",
            "seq": 1,
            "at": "2026-01-01T00:00:00+00:00",
            "data": {
                "funnel": {"DISCOVERED": 2, "SELECTED": 1},
                "selected": "cc/alpha",
                "candidates": [
                    {"route_id": "cc/alpha", "provider": "cc", "reached": "SELECTED"},
                    {
                        "route_id": "kr/beta",
                        "provider": "kr",
                        "reached": "ENTITLED",
                        "failed_stage": "HEALTHY",
                        "reason": "cooldown",
                    },
                ],
            },
        }
    ]
    (run / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return run


def test_routing_filters_and_paging(tmp_path: Path) -> None:
    run = _run_with_candidates(tmp_path)
    code, out, _err = run_cli(
        "routing", str(run), "--json", "--state", "selected", "--page", "0", "--page-size", "1"
    )
    assert code == 0
    payload = json.loads(out)
    seen = []
    for evaluation in payload["evaluations"]:
        candidates = evaluation["candidates"] or []
        assert len(candidates) <= 1
        for cand in candidates:
            assert cand["state"] == "selected"
            seen.append(cand["route_id"])
    assert seen, "expected at least one selected candidate"

    missing, empty, err = run_cli(
        "routing", str(PROOF_DEMO), "--json", "--provider", "no-such-provider"
    )
    assert missing == 0, err
    filtered = json.loads(empty)
    for evaluation in filtered["evaluations"]:
        assert not evaluation["candidates"]


def test_node_filter_and_unknown_node() -> None:
    code, out, _err = run_cli("context", str(PROOF_LIVE), "--node", "alpha", "--json")
    assert code == 0
    payload = json.loads(out)
    assert [node["node_id"] for node in payload["nodes"]] == ["alpha"]

    code, _out, err = run_cli("routing", str(PROOF_LIVE), "--node", "missing-node")
    assert code == 3
    assert "no node" in err

    code, _out, err = run_cli("context", str(PROOF_LIVE), "--node", "missing-node", "--json")
    assert code == 3
    assert "no node" in err


def test_run_not_found_and_usage() -> None:
    code, _out, err = run_cli("routing", "does-not-exist-run-id")
    assert code == 3
    assert "no run" in err
    code, _out, err = run_cli("context", "does-not-exist-run-id")
    assert code == 3
    assert "no run" in err
    code, _out, err = run_cli("routing")
    assert code == 2
    code, _out, err = run_cli("context")
    assert code == 2


def test_inventory_never_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """--inventory builds the view with probe=False and an explicit inventory."""
    calls: list[dict[str, Any]] = []

    def fake_inventory(task_profile: dict[str, Any], **kwargs: Any) -> Any:
        calls.append({"profile": task_profile, **kwargs})
        from verdict.orchestration.routing_view import EligibilityEvaluation, RoutingView

        return RoutingView(
            schema_version="1",
            source="inventory",
            generated_at="2026-01-01T00:00:00+00:00",
            evaluations=[
                EligibilityEvaluation(
                    node_id="",
                    seq=0,
                    at="2026-01-01T00:00:00+00:00",
                    funnel={},
                    rejections={},
                    selected_route=None,
                    candidates=[],
                    candidates_omitted=0,
                    selected_because=[],
                    observed_route=None,
                    session_ref=None,
                    selected_observed_mismatch=False,
                )
            ],
        )

    monkeypatch.setattr(
        "verdict.orchestration.routing_view.routing_view_from_inventory", fake_inventory
    )
    code, out, err = run_cli("routing", "--inventory", "--json")
    assert code == 0, err
    assert calls and calls[0]["probe"] is False
    assert json.loads(out)["source"] == "inventory"


def test_cli_and_palette_share_the_view_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Parity: CLI and the TUI palette both reach routing_view / context_view."""
    routing_calls: list[Any] = []
    context_calls: list[Any] = []
    real_routing = __import__(
        "verdict.orchestration.routing_view", fromlist=["routing_view"]
    ).routing_view
    real_context = __import__(
        "verdict.orchestration.context_view", fromlist=["context_view"]
    ).context_view

    def spy_routing(source: Any) -> Any:
        routing_calls.append(source)
        return real_routing(source)

    def spy_context(events: Any) -> Any:
        context_calls.append(events)
        return real_context(events)

    monkeypatch.setattr("verdict.orchestration.routing_view.routing_view", spy_routing)
    monkeypatch.setattr("verdict.orchestration.context_view.context_view", spy_context)

    from verdict.home import run_palette_action

    code, _out, err = run_cli("routing", str(PROOF_LIVE), "--json")
    assert code == 0, err
    ok, data = run_palette_action("routing.view", {"run": str(PROOF_LIVE)})
    assert ok
    assert data["payload"]["source"] == "recorded"
    assert len(routing_calls) == 2

    code, _out, err = run_cli("context", str(PROOF_DEMO), "--json")
    assert code == 0, err
    ok, data = run_palette_action("context.view", {"run": str(PROOF_DEMO)})
    assert ok
    assert data["payload"]["nodes"]
    assert len(context_calls) == 2
