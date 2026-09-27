"""S2-A: eligibility output is complete, reconciled, and filterable (never truncated)."""

from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest

from tests.test_orch_eligibility import conn, row
from verdict.orchestration import cli as orch_cli
from verdict.orchestration import run as orch_run
from verdict.subagent_selection import HealthResult, LaunchCandidate


def _rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i in range(50):
        rows.append(row(f"cc/claude-m{i:03d}", owned_by="claude"))
    for i in range(40):
        rows.append(row(f"kr/kiro-m{i:03d}", owned_by="kiro"))
    for i in range(30):
        rows.append(row(f"zz/orphan-m{i:03d}", owned_by="zz"))  # no account -> ENTITLED
    for i in range(20):
        rows.append(row(f"gc/gem-m{i:03d}", owned_by="gemini", tools=False))  # TASK_ELIGIBLE
    for i in range(10):
        rows.append(row(f"cc/claude-m{i:03d}-high", owned_by="claude"))  # effort_duplicate
    assert len(rows) == 150
    return rows


@pytest.fixture
def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    rows = _rows()
    probed: list[str] = []
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / "vh"))
    monkeypatch.setattr(orch_run, "fetch_inventory", lambda gateway, api_key: list(rows))
    monkeypatch.setattr(
        orch_run,
        "fetch_connections",
        lambda gateway, api_key: [conn("claude"), conn("kiro"), conn("gemini")],
    )

    def fake_probe_factory(*_a: Any, **_k: Any) -> Any:
        def probe(candidate: LaunchCandidate) -> HealthResult:
            probed.append(candidate.route_id)
            if candidate.route_id.startswith("cc/"):
                return HealthResult(False, "payment_required", 402)
            return HealthResult(True, "healthy", 200)

        return probe

    import verdict.subagent_selection as sel

    monkeypatch.setattr(sel, "openai_health_probe", fake_probe_factory)
    return probed


def _args(**kw: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "command": "eligibility",
        "gateway": "http://127.0.0.1:1",
        "scope": "",
        "prefer": "claude",
        "probe": True,
        "reasoning": False,
        "frontier": False,
        "json": False,
        "no_pager": True,
        "provider_family": [],
    }
    base.update(kw)
    return argparse.Namespace(**base)


def _run(capsys: pytest.CaptureFixture[str], **kw: Any) -> str:
    assert orch_cli.dispatch(_args(**kw)) == 0
    return capsys.readouterr().out


def test_every_route_appears_in_human_and_json(
    wired: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    ids = {str(r["id"]) for r in _rows()}
    human = _run(capsys)
    missing = [i for i in ids if not re.search(rf"\s{re.escape(i)}\s", human)]
    assert missing == []
    assert "filters: " in human.splitlines()[0]
    for header in (
        "SELECTED (1)",
        "RANKED / ELIGIBLE",
        "REJECTED AT ENTITLED",
        "REJECTED AT TASK_ELIGIBLE",
    ):
        assert header in human
    data = json.loads(_run(capsys, json=True))
    assert {v["route_id"] for v in data["verdicts"]} == ids


def test_json_counts_reconcile_without_filter(
    wired: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    data = json.loads(_run(capsys, json=True))
    assert data["filters"]["provider_family"] == []
    assert data["evaluated_count"] == len(data["verdicts"]) == 150
    buckets = data["summary"]["by_reached_stage"]
    assert sum(buckets.values()) == data["evaluated_count"]
    for stage, count in buckets.items():
        want = None if stage == "NONE" else stage
        assert count == sum(1 for v in data["verdicts"] if v["reached"] == want)
    assert data["selected"] is not None
    assert data["summary"]["selected"] == data["selected"]["route_id"]
    # cc/* failed with 402 once; the rest of the claude provider was skipped, not probed
    assert data["selected"]["route_id"].startswith("kr/")
    assert sum(1 for p in wired if p.startswith("cc/")) == 1
    assert buckets["SELECTED"] == 1


def test_provider_family_filter_is_listed_and_applied(
    wired: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    data = json.loads(_run(capsys, json=True, provider_family=["kr", "zz,KR"]))
    assert data["filters"]["provider_family"] == ["kr", "zz"]
    assert data["evaluated_count"] == 70 == len(data["verdicts"])
    assert {v["route_id"].split("/")[0] for v in data["verdicts"]} == {"kr", "zz"}
    assert all(p.startswith("kr/") for p in wired)
    human = _run(capsys, provider_family=["kr,zz"])
    assert human.splitlines()[0] == "filters: provider_family=kr,zz"


def test_no_implicit_slices_in_eligibility_renderers() -> None:
    tree = ast.parse(Path(orch_cli.__file__).read_text(encoding="utf-8"))
    names = {"_eligibility", "render_eligibility_text", "eligibility_payload", "_page"}
    funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {f.name for f in funcs} == names
    for func in funcs:
        for node in ast.walk(func):
            if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice):
                pytest.fail(f"slice in {func.name} at line {node.lineno}: output may truncate")
    assert "--limit" not in Path(orch_cli.__file__).read_text(encoding="utf-8")
