"""CLI contract tests for `verdict choose`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verdict import cli
from verdict.chooser import evidence_from_mapping


def _fixture(path: Path) -> Path:
    payload = {
        "candidates": [
            {
                "requested_alias": "free/model",
                "availability": "eligible",
                "capabilities": {"resource_class": "free", "tools": "observed", "code": "observed"},
                "observed_at": "2026-09-13T12:00:00Z",
                "ttl_seconds": 60,
                "source": "fixture",
                "freshness_seconds": 1.0,
                "quota_remaining_pct": None,
                "headroom_pct": 40,
                "route": {
                    "gateway_id": "gw-a",
                    "route_id": "route-free",
                    "provider": "provider-a",
                    "model_id": "free/model",
                    "protocol": "openai.chat",
                },
            },
            {
                "requested_alias": "premium/model",
                "availability": "eligible",
                "capabilities": {
                    "resource_class": "subscription_premium",
                    "tools": "observed",
                    "code": "observed",
                },
                "observed_at": "2026-09-13T12:00:00Z",
                "ttl_seconds": 60,
                "source": "fixture",
                "freshness_seconds": 1.0,
                "quota_remaining_pct": 90,
                "headroom_pct": 90,
                "route": {
                    "gateway_id": "gw-b",
                    "route_id": "route-premium",
                    "provider": "provider-b",
                    "model_id": "premium/model",
                    "protocol": "openai.chat",
                },
            },
        ]
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_choose_json_selects_free_for_implementation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path / "candidates.json")
    monkeypatch.setattr(
        "sys.argv",
        [
            "verdict",
            "choose",
            "--task-class",
            "implementation",
            "--requires",
            "tools,code",
            "--candidates-json",
            str(fixture),
            "--json",
        ],
    )
    cli.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected"]["model"] == "free/model"
    assert payload["selected"]["resource_pool"] == "free"
    assert payload["policy_version"] == "chooser-policy/v1"
    assert payload["ranker_version"] == "chooser-ranker/v1"
    assert "quota" in payload["unknown_evidence_fields"]
    assert payload["selected_because"].startswith("selected because")


def test_choose_human_output_explains_selection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path / "candidates.json")
    monkeypatch.setattr(
        "sys.argv",
        ["verdict", "choose", "--task-class", "implementation", "--candidates-json", str(fixture)],
    )
    cli.main()
    out = capsys.readouterr().out
    assert "selected because" in out
    assert "free" in out


def test_choose_explicit_ineligible_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = json.loads(_fixture(tmp_path / "candidates.json").read_text())
    payload["candidates"][1]["availability"] = "quota_exhausted"
    fixture = tmp_path / "ineligible.json"
    fixture.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "verdict",
            "choose",
            "--task-class",
            "implementation",
            "--model",
            "premium/model",
            "--candidates-json",
            str(fixture),
            "--json",
        ],
    )
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 1
    result = json.loads(capsys.readouterr().out)
    assert result["reason"] == "explicit_model_ineligible"
    assert result["selected"] is None


def test_evidence_from_mapping_roundtrip(tmp_path: Path) -> None:
    fixture = json.loads(_fixture(tmp_path / "candidates.json").read_text())
    evidence = evidence_from_mapping(fixture["candidates"][0])
    assert evidence.route.model_id == "free/model"
    assert evidence.capability_status("resource_class") == "free"
