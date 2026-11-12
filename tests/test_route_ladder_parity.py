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


def test_allow_legacy_selector_flag(
    live_pool: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["verdict", "route", "fix a failing unit test", "--allow-legacy-selector", "--terse"],
    )
    try:
        cli.main()
    except SystemExit as exc:
        assert exc.code in {0, 1}
