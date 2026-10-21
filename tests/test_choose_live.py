"""Live choose uses the production ladder, not the fixture economic ranker."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict import cli
from verdict.acceptance import judge_choose
from verdict.admission import RuntimeEvidence
from verdict.metadata.records import (
    FieldProvenance,
    ModelMetadataRecord,
    ProvenancedField,
    SoftScores,
)
from verdict.metadata.store import MetadataSnapshot
from verdict.orchestration.contracts import TaskRequirements
from verdict.orchestration.health_cache import CATEGORY_OK, HealthCache, ProbeResult
from verdict.subagent_selection import HealthResult


@pytest.fixture
def live_pool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    from verdict.metadata import store
    from verdict.orchestration import run

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


def _choose(monkeypatch: pytest.MonkeyPatch, *args: str) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "verdict",
            "choose",
            "--task-class",
            "implementation",
            "--requires",
            "tools,code",
            "--json",
            *args,
        ],
    )
    cli.main()


def test_shared_seam_returns_ladder_rating_provenance(live_pool: list[dict[str, Any]]) -> None:
    from verdict.orchestration.selection import select_route_for_task

    result = select_route_for_task(
        TaskRequirements(required_capabilities=frozenset({"tools"})), gateway="http://127.0.0.1:1"
    )
    assert result.route is not None
    assert result.route.route_id == "sub/strong"
    assert result.reasons
    assert result.provenance["rating"] == 80
    assert result.provenance["rating_metric"] == "aa_coding"
    assert result.provenance["rating_source"]
    assert result.provenance["rating_as_of"]


def test_live_choose_passes_acceptance_and_ignores_price(
    live_pool: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _choose(monkeypatch)
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["selected"]["route_id"] == "sub/strong"
    assert payload["reasons"]
    assert payload["provenance"]["rating"] == 80
    assert payload["gateway"] == "http://127.0.0.1:1"
    assert judge_choose(0, output, "")[0]


def test_capacity_only_narrows(
    live_pool: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _choose(monkeypatch, "--capacity", "free")
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected"]["route_id"] == "free/weak"
    assert payload["rejections"]


def test_empty_pool_fails_closed(
    live_pool: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live_pool.clear()
    with pytest.raises(SystemExit) as exc:
        _choose(monkeypatch)
    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected"] is None
    assert payload["reason"] == "no_eligible_route"
    assert payload["reasons"]


def test_explicit_model_never_falls_back(
    live_pool: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        _choose(monkeypatch, "--model", "missing/route")
    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected"] is None
    assert payload["reason"] == "explicit_model_ineligible"


def test_discovery_failure_is_named_and_fails_closed(
    live_pool: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import urllib.error

    from verdict.orchestration import run

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise urllib.error.URLError("offline")

    from unittest.mock import Mock

    model_probe = Mock(side_effect=AssertionError("discovery failure must not probe a model"))
    monkeypatch.setattr("verdict.subagent_selection.openai_health_probe", model_probe)
    monkeypatch.setattr(run, "fetch_inventory", unavailable)
    with pytest.raises(SystemExit) as exc:
        _choose(monkeypatch)
    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected"] is None
    assert payload["reason"] == "gateway_discovery_failed"
    assert "/v1/models" in payload["reasons"][0]
    model_probe.assert_not_called()


def test_live_choose_human_output(
    live_pool: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["verdict", "choose", "--task-class", "implementation", "--requires", "tools,code"],
    )
    cli.main()
    output = capsys.readouterr().out
    assert "sub/strong" in output
    assert "higher task rating" in output


def test_explicit_eligible_model_in_shared_pool_wins(
    live_pool: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from verdict.orchestration import run

    live_pool[:] = [
        live_pool[1] | {"id": route, "owned_by": "agy"}
        for route in ("agy/strong", "antigravity/other")
    ]
    monkeypatch.setattr(
        run,
        "fetch_connections",
        lambda *a, **kw: [
            {"provider": "agy", "authType": "oauth", "isActive": True, "plan_label": "max"}
        ],
    )
    _choose(monkeypatch, "--model", "agy/strong")
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected"]["route_id"] == "agy/strong"
    other = next(v for v in payload["rejections"].values() if "admission:outside_route_filter" in v)
    assert other["admission:outside_route_filter"] == 1


def test_reviewer_exclusion_still_excludes_alias_siblings(
    live_pool: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from verdict.orchestration import run
    from verdict.orchestration.selection import select_route_for_task

    live_pool[:] = [live_pool[1] | {"id": "antigravity/strong", "owned_by": "agy"}]
    monkeypatch.setattr(
        run,
        "fetch_connections",
        lambda *a, **kw: [
            {"provider": "agy", "authType": "oauth", "isActive": True, "plan_label": "max"}
        ],
    )
    result = select_route_for_task(
        TaskRequirements(exclude_routes=frozenset({"agy/other"})), gateway="http://127.0.0.1:1"
    )
    assert result.route is None
    assert "excluded_family" in result.reasons
