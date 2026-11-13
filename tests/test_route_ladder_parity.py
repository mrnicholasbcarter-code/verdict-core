"""Parity tests between verdict choose and verdict route using the ladder."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict import cli
from verdict.admission import RuntimeEvidence
from verdict.metadata.records import (
    FieldProvenance,
    ModelMetadataRecord,
    ProvenancedField,
    SoftScores,
)
from verdict.metadata.store import MetadataSnapshot
from verdict.orchestration.health_cache import CATEGORY_OK, HealthCache, ProbeResult
from verdict.subagent_selection import HealthResult


@pytest.fixture
def live_pool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    from verdict.metadata import store
    from verdict.orchestration import run

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    monkeypatch.setenv("VERDICT_HEALTH_CACHE", str(tmp_path / "health-cache.json"))
    monkeypatch.setenv("VERDICT_SESSION_EVIDENCE", str(tmp_path / "sessions.jsonl"))
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://127.0.0.1:1/v1")
    rows = [
        {"id": "free/weak", "owned_by": "free", "pricing": {"input": 0, "output": 0}},
        {"id": "sub/strong", "owned_by": "sub", "pricing": {"input": 5, "output": 10}},
    ]
    now = datetime.now(timezone.utc)
    records = []
    cache = HealthCache(tmp_path / "health-cache.json")
    for row, score in zip(rows, (30, 80), strict=True):
        row.update(context_length=200_000, capabilities={"tool_calling": True})
        route = row["id"]
        records.append(
            ModelMetadataRecord(
                id=route,
                omniroute_ids=(route,),
                provider=row["owned_by"],
                scores=SoftScores(
                    aa_coding=ProvenancedField(
                        value=score,
                        provenance=FieldProvenance(
                            source="openrouter_aa", fetched_at=now.isoformat()
                        ),
                    )
                ),
            )
        )
        cache.record(
            route, ProbeResult(CATEGORY_OK, True, True, probe_class="agentic", agentic_ok=True), now
        )
    cache.save()
    snapshot = MetadataSnapshot(
        schema_version="1", refreshed_at=now.isoformat(), sources={}, records=tuple(records)
    )
    monkeypatch.setattr(store, "load_store", lambda *a, **kw: snapshot)
    monkeypatch.setattr(run, "fetch_inventory", lambda *a, **kw: rows)
    monkeypatch.setattr(
        run,
        "fetch_connections",
        lambda *a, **kw: [
            {
                "provider": "free",
                "authType": "apikey",
                "isActive": True,
                "plan_label": "free",
                "import_free_only": True,
            },
            {"provider": "sub", "authType": "oauth", "isActive": True, "plan_label": "max"},
        ],
    )
    monkeypatch.setattr(
        "verdict.omniroute_health.load_omniroute_health", lambda *a, **kw: RuntimeEvidence()
    )
    monkeypatch.setattr(
        "verdict.subagent_selection.openai_health_probe",
        lambda *a, **kw: lambda candidate: HealthResult(True, "healthy"),
    )
    return rows


def test_choose_and_route_terse_parity(
    live_pool: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Run choose --json
    monkeypatch.setattr(
        "sys.argv",
        [
            "verdict",
            "choose",
            "--task",
            "fix a failing unit test",
            "--task-class",
            "implementation",
            "--requires",
            "tools,code",
            "--json",
        ],
    )
    cli.main()
    choose_out = capsys.readouterr().out
    choose_payload = json.loads(choose_out)
    chosen_id = choose_payload["selected"]["route_id"]

    # Run route --terse
    monkeypatch.setattr("sys.argv", ["verdict", "route", "fix a failing unit test", "--terse"])
    cli.main()
    route_out = capsys.readouterr().out.strip()
    assert route_out == chosen_id


def test_orchestrate_selector_and_choose_share_first_route(live_pool, monkeypatch, capsys):
    from verdict.orchestration.cli import build_selector
    from verdict.orchestration.contracts import TaskRequirements

    monkeypatch.setattr("sys.argv", ["verdict", "choose", "--task", "fix code", "--json"])
    cli.main()
    chosen = json.loads(capsys.readouterr().out)["selected"]["route_id"]
    selector = build_selector(
        "http://127.0.0.1:1", scope="", prefer="", visibility_executor="direct-gateway"
    )
    route, _ = selector.select(TaskRequirements(), now=datetime.now(timezone.utc))
    assert route is not None
    assert route.route_id == chosen == "free/weak"


def test_route_preserves_explicit_requirement_floors(live_pool, monkeypatch, capsys):
    from verdict.orchestration.selection import SelectionResult

    captured = []

    def select(requirements, **kwargs):
        captured.append(requirements)
        return SelectionResult(None, ("no_candidates",), {}, {})

    monkeypatch.setattr("verdict.orchestration.selection.select_route_for_task", select)
    with pytest.raises(SystemExit, match="1"):
        cli.cmd_route(
            "fix code",
            "medium",
            terse=True,
            min_context_tokens=200_000,
            reasoning_required=True,
            vision_required=True,
            structured_output_required=True,
        )
    assert captured[0].min_context_tokens == 200_000
    assert captured[0].required_capabilities >= {"vision", "structured_output", "reasoning"}
    assert json.loads(capsys.readouterr().out)["reason"] == "no_eligible_route"


def test_default_route_does_not_construct_gate(live_pool, monkeypatch, capsys):
    def forbidden(**kwargs):
        pytest.fail("default route constructed the legacy Gate")

    monkeypatch.setattr(cli._action_helpers, "build_route_gate", forbidden)
    cli.cmd_route("fix a failing unit test", "medium", terse=True)
    assert capsys.readouterr().out == "free/weak\n"


def test_serve_uses_only_ladder_candidates_in_ladder_order(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import test_live_candidate_shortlist as fixtures

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    service = fixtures._service([], top_k=4)
    service.admit_snapshot = None
    monkeypatch.setattr(
        service,
        "_load_admit_snapshot",
        lambda: (fixtures._snapshot(), ("http://127.0.0.1:1/v1", ""), False, None),
    )
    candidates = [SimpleNamespace(route_id=rid) for rid in (fixtures.FAST, fixtures.STRONG)]
    selector = SimpleNamespace(_assess_all=lambda *args: ([], candidates))
    monkeypatch.setattr(
        "verdict.orchestration.eligibility_report.build_selector", lambda *a, **kw: selector
    )
    request = fixtures._request(fixtures.NEW, fixtures.STRONG, fixtures.FAST)
    prepared = service._prepare_execution_path_request(
        "fix code", "low", fixtures._context(request), request
    )
    assert [o.route.route_id for o in prepared.offers] == [fixtures.FAST, fixtures.STRONG]
    assert [entry.route_id for entry in prepared.pool_receipt.shortlist] == [
        fixtures.FAST,
        fixtures.STRONG,
    ]


def test_allow_legacy_selector_flag_uses_the_gate_and_never_the_ladder(
    live_pool: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from verdict.models import RoutingDecision
    from verdict.serve_path import CONTEXT_ALLOW_LEGACY

    built: list[bool] = []
    contexts: list[Any] = []

    class _LegacyGate:
        def route(self, task: str, criticality: str, context: Any = None) -> RoutingDecision:
            del task, criticality
            contexts.append(context)
            return RoutingDecision(
                model="legacy/route",
                provider="omniroute",
                tier=3,
                reason="legacy stub",
                decision="selected",
                transport_outcome="sent",
                request_id="req-legacy",
            )

    def build(allow_offline: bool = False) -> _LegacyGate:
        built.append(allow_offline)
        return _LegacyGate()

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("--allow-legacy-selector reached the selection ladder")

    monkeypatch.setattr(cli._action_helpers, "build_route_gate", build)
    monkeypatch.setattr("verdict.orchestration.selection.select_route_for_task", forbidden)
    monkeypatch.setattr(
        "sys.argv",
        ["verdict", "route", "fix a failing unit test", "--allow-legacy-selector", "--terse"],
    )
    try:
        cli.main()
    except SystemExit as exc:
        assert exc.code in (0, None)

    # The flag builds the legacy gate once, online, and prints the gate's route, not the
    # ladder's ("free/weak" for this inventory). The escape is recorded on the request.
    assert built == [False]
    assert capsys.readouterr().out == "legacy/route\n"
    assert len(contexts) == 1
    assert contexts[0][CONTEXT_ALLOW_LEGACY] is True
