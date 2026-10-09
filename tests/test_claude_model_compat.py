"""Hermetic tests for the pure Claude Code compatibility report."""

from __future__ import annotations

import json
import socket
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict.harness_claude import DiscoverReport, StatusReport
from verdict.harness_claude_compat import SCHEMA, build_claude_compat_report

EXACT_ID = "cc/claude-opus-5-5"
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
TOP_LEVEL_KEYS = {
    "schema",
    "generated_at",
    "harness",
    "requested_mode",
    "exact_selected_ids",
    "selected_ids",
    "installed",
    "settings_present",
    "side_path_env_configured",
    "gate_hook_present",
    "credentials_present",
    "config_digest",
    "config_changed",
    "rows",
    "reasons",
    "apply_available",
    "needs_owner",
}
ROW_KEYS = {
    "route_id",
    "provider",
    "health_status",
    "checked_at",
    "fresh_until",
    "expires_at",
    "native",
    "side_path",
    "reasons",
}
NATIVE_KEYS = {
    "transport",
    "endpoint",
    "compatibility",
    "disposition",
    "owner",
    "exact_selection_tested",
    "selectable",
    "reasons",
}
SIDE_PATH_KEYS = {
    "transport",
    "endpoint",
    "configured",
    "compatibility",
    "exact_selection_tested",
    "selectable",
    "reasons",
}


@pytest.fixture
def discover_report(tmp_path: Path) -> DiscoverReport:
    return DiscoverReport(
        installed=True,
        binary_path="/tmp/fake/claude",
        config_path=tmp_path / "settings.json",
        config_exists=True,
        managed_by_verdict=True,
        base_url="http://127.0.0.1:8000/v1",
        pointing_at_verdict=True,
        pointing_at_omniroute=False,
        gate_hook_present=True,
    )


@pytest.fixture
def status_report(tmp_path: Path) -> StatusReport:
    return StatusReport(
        enabled=True,
        provider="verdict",
        base_url="http://127.0.0.1:8000/v1",
        token_env="VERDICT_SECRET_NAME_SHOULD_NOT_LEAK",
        token_env_set=True,
        config_path=tmp_path / "settings.json",
        config_exists=True,
        gate_hook_present=True,
        integration="openai-compatible",
    )


@pytest.fixture
def projection_rows() -> list[dict[str, Any]]:
    return [
        {
            "route_id": EXACT_ID,
            "provider": "cc",
            "status": "VERIFIED",
            "checked_at": "2026-09-28T11:00:00Z",
            "fresh_until": "2026-09-28T12:30:00Z",
            "expires_at": "2026-10-05T11:00:00Z",
            "identity": "verified",
        },
        {
            "route_id": "kr/claude-looking-but-stale",
            "provider": "kr",
            "status": "STALE",
            "checked_at": "2026-09-27T11:00:00Z",
            "fresh_until": "2026-09-28T11:00:00Z",
            "expires_at": "2026-10-04T11:00:00Z",
        },
        {
            "route_id": "cx/catalog-only",
            "provider": "cx",
            "status": "INVENTORY_ONLY",
            "checked_at": None,
            "fresh_until": None,
            "expires_at": None,
        },
    ]


def report(
    discover: DiscoverReport,
    status: StatusReport,
    projection_rows: list[dict[str, Any]],
    ids: list[str] | None = None,
    *,
    start_digest: str | None = "start-digest",
    end_digest: str | None = "start-digest",
) -> dict[str, Any]:
    return build_claude_compat_report(
        discover=discover,
        status=status,
        start_digest=start_digest,
        end_digest=end_digest,
        requested_mode="native",
        exact_selected_ids=ids or [EXACT_ID],
        projection_rows=projection_rows,
        now=NOW,
    )


def test_native_refusal_for_every_exact_id(
    discover_report: DiscoverReport,
    status_report: StatusReport,
    projection_rows: list[dict[str, Any]],
) -> None:
    ids = [EXACT_ID, "kr/claude-looking-but-stale", "unknown/exact-id"]
    result = report(discover_report, status_report, projection_rows, ids)
    assert len(result["rows"]) == len(ids)
    for exact_id, row in zip(ids, result["rows"], strict=True):
        assert row["route_id"] == exact_id
        assert row["native"] == {
            "transport": "anthropic-messages",
            "endpoint": "/v1/messages",
            "compatibility": "unsupported",
            "disposition": "NEEDS_OWNER",
            "owner": "BOD-102",
            "exact_selection_tested": False,
            "selectable": False,
            "reasons": ["native_messages_proxy_missing"],
        }
        assert "native_messages_proxy_missing" in row["reasons"]
        assert "selected_id_unproven" in row["reasons"]
        assert result["apply_available"] is False
        assert result["needs_owner"] == ["BOD-102"]


def test_side_path_configured_but_never_proves_selected_id(
    discover_report: DiscoverReport,
    status_report: StatusReport,
    projection_rows: list[dict[str, Any]],
) -> None:
    row = report(discover_report, status_report, projection_rows)["rows"][0]
    assert row["side_path"]["compatibility"] == "configured_unproven"
    assert row["side_path"]["configured"] is True
    assert row["side_path"]["transport"] == "openai-compatible"
    assert row["side_path"]["endpoint"] == "/v1/chat/completions"
    assert row["side_path"]["exact_selection_tested"] is False
    assert row["side_path"]["selectable"] is False
    assert "selected_id_unproven" in row["side_path"]["reasons"]


def test_side_path_not_configured_for_missing_prerequisites(
    discover_report: DiscoverReport,
    status_report: StatusReport,
    projection_rows: list[dict[str, Any]],
) -> None:
    missing_binary = replace(discover_report, installed=False)
    binary_result = report(missing_binary, status_report, projection_rows)
    assert binary_result["rows"][0]["side_path"]["compatibility"] == "not_configured"
    assert "binary_missing" in binary_result["rows"][0]["side_path"]["reasons"]

    missing_credentials = replace(status_report, token_env_set=False)
    credentials_result = report(discover_report, missing_credentials, projection_rows)
    assert credentials_result["credentials_present"] is False
    assert "credentials_missing" in credentials_result["rows"][0]["reasons"]
    assert credentials_result["rows"][0]["side_path"]["compatibility"] == "not_configured"

    missing_settings = report(
        replace(discover_report, config_exists=False),
        replace(status_report, config_exists=False),
        projection_rows,
    )
    assert missing_settings["settings_present"] is False
    assert "settings_missing" in missing_settings["rows"][0]["reasons"]
    assert missing_settings["rows"][0]["side_path"]["compatibility"] == "not_configured"


def test_side_path_unconfigured_when_environment_does_not_target_verdict(
    discover_report: DiscoverReport,
    status_report: StatusReport,
    projection_rows: list[dict[str, Any]],
) -> None:
    result = report(
        replace(discover_report, pointing_at_verdict=False), status_report, projection_rows
    )
    assert result["rows"][0]["side_path"]["compatibility"] == "not_configured"
    assert result["side_path_env_configured"] is False


def test_changed_config_digest_blocks_side_path(
    discover_report: DiscoverReport,
    status_report: StatusReport,
    projection_rows: list[dict[str, Any]],
) -> None:
    result = report(
        discover_report, status_report, projection_rows, start_digest="before", end_digest="after"
    )
    assert result["config_changed"] is True
    assert "config_changed" in result["reasons"]
    for row in result["rows"]:
        assert row["side_path"]["compatibility"] == "blocked"
        assert row["side_path"]["configured"] is False
        assert "config_changed" in row["reasons"]
        assert "config_changed" in row["side_path"]["reasons"]


def test_selected_ids_map_only_by_exact_route_id(
    discover_report: DiscoverReport,
    status_report: StatusReport,
    projection_rows: list[dict[str, Any]],
) -> None:
    exact, unknown = report(
        discover_report, status_report, projection_rows, [EXACT_ID, "opus-5-5"]
    )["rows"]
    assert exact["provider"] == "cc"
    assert exact["health_status"] == "VERIFIED"
    assert exact["checked_at"] == "2026-09-28T11:00:00Z"
    assert exact["fresh_until"] == "2026-09-28T12:30:00Z"
    assert exact["expires_at"] == "2026-10-05T11:00:00Z"
    assert "unsupported_identity" not in exact["reasons"]
    assert unknown["provider"] is None
    assert unknown["health_status"] == "UNVERIFIED"
    assert "unsupported_identity" in unknown["reasons"]


def test_stale_unverified_and_inventory_only_labels_are_preserved(
    discover_report: DiscoverReport,
    status_report: StatusReport,
    projection_rows: list[dict[str, Any]],
) -> None:
    result = report(
        discover_report,
        status_report,
        projection_rows,
        ["kr/claude-looking-but-stale", "cx/catalog-only", "missing"],
    )
    stale, inventory, absent = result["rows"]
    assert stale["health_status"] == "STALE"
    assert "stale_or_unverified" in stale["reasons"]
    assert inventory["health_status"] == "INVENTORY_ONLY"
    assert "stale_or_unverified" in inventory["reasons"]
    assert absent["health_status"] == "UNVERIFIED"
    assert "unsupported_identity" in absent["reasons"]
    assert all(row["side_path"]["selectable"] is False for row in result["rows"])


def test_duplicate_projection_ids_fail_closed_as_unsupported_identity(
    discover_report: DiscoverReport,
    status_report: StatusReport,
    projection_rows: list[dict[str, Any]],
) -> None:
    duplicated = [*projection_rows, dict(projection_rows[0])]
    row = report(discover_report, status_report, duplicated)["rows"][0]
    assert row["health_status"] == "UNVERIFIED"
    assert row["provider"] is None
    assert "unsupported_identity" in row["reasons"]


def test_report_has_stable_schema_keys_and_timezone_normalization(
    discover_report: DiscoverReport,
    status_report: StatusReport,
    projection_rows: list[dict[str, Any]],
) -> None:
    result = report(discover_report, status_report, projection_rows)
    assert result["schema"] == SCHEMA == "verdict.claude-model-compat/v1"
    assert set(result) == TOP_LEVEL_KEYS
    row = result["rows"][0]
    assert set(row) == ROW_KEYS
    assert set(row["native"]) == NATIVE_KEYS
    assert set(row["side_path"]) == SIDE_PATH_KEYS
    assert result["generated_at"] == "2026-09-28T12:00:00Z"
    assert result["exact_selected_ids"] == result["selected_ids"] == [EXACT_ID]


def test_no_mutation_no_network_and_no_secret_leakage(
    tmp_path: Path,
    discover_report: DiscoverReport,
    status_report: StatusReport,
    projection_rows: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {"apiKey": "SHOULD_NEVER_APPEAR", "auth": {"accessToken": "ALSO_SHOULD_NEVER_APPEAR"}}
        ),
        encoding="utf-8",
    )
    before = {path: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}

    def deny_network(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("network access is forbidden in the pure report")

    monkeypatch.setattr(socket, "socket", deny_network)
    monkeypatch.setattr(socket, "create_connection", deny_network)
    result = report(discover_report, status_report, projection_rows)
    after = {path: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}

    rendered = json.dumps(result)
    assert after == before
    assert "SHOULD_NEVER_APPEAR" not in rendered
    assert "ALSO_SHOULD_NEVER_APPEAR" not in rendered
    assert "VERDICT_SECRET_NAME_SHOULD_NOT_LEAK" not in rendered
    assert "127.0.0.1" not in rendered
    assert "apiKey" not in rendered
    assert "accessToken" not in rendered


def test_credentials_unknown_is_not_treated_as_ready(
    discover_report: DiscoverReport,
    status_report: StatusReport,
    projection_rows: list[dict[str, Any]],
) -> None:
    unknown_status = replace(status_report, token_env_set=None)  # type: ignore[arg-type]
    result = report(discover_report, unknown_status, projection_rows)
    assert result["credentials_present"] is None
    assert result["rows"][0]["side_path"]["compatibility"] == "not_configured"
    assert "credentials_unknown" in result["rows"][0]["reasons"]


def test_naive_now_is_rejected(
    discover_report: DiscoverReport,
    status_report: StatusReport,
    projection_rows: list[dict[str, Any]],
) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        build_claude_compat_report(
            discover_report,
            status_report,
            "same",
            "same",
            "native",
            [EXACT_ID],
            projection_rows,
            datetime(2026, 9, 28, 12, 0),
        )
