"""Failure evidence is a closed vocabulary, never a provider-output upload."""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts import run_certification_rehearsals as script
from tests.test_certification_chaos_proof import _write_proof
from tests.test_certification_rehearsal_workflow import ELIGIBLE_PASS, ELIGIBLE_PASSES, ROOT


@pytest.mark.parametrize(
    "reason,expected,outcome,expected_outcome",
    [
        (
            "no eligible model: harness_inventory_unavailable",
            "harness_inventory_unavailable",
            "BLOCKED",
            "BLOCKED",
        ),
        (
            "provider-secret https://private.invalid snapshot-secret",
            "unclassified",
            "BLOCKED",
            "BLOCKED",
        ),
        (
            "no eligible model",
            "unclassified",
            "provider-secret https://private.invalid snapshot-secret",
            None,
        ),
        (
            "planning failed on every eligible frontier model; last: none eligible",
            "planning_none_eligible",
            "BLOCKED",
            "BLOCKED",
        ),
        (
            "gateway /v1/models failed (HTTP 401); check --gateway and gateway credentials",
            "gateway_auth",
            "BLOCKED",
            "BLOCKED",
        ),
        (
            "gateway /v1/models failed (HTTP 403); check --gateway and gateway credentials",
            "gateway_auth",
            "BLOCKED",
            "BLOCKED",
        ),
        (
            "gateway /api/providers failed (HTTP 500); check --gateway and gateway credentials",
            "gateway_discovery",
            "BLOCKED",
            "BLOCKED",
        ),
        (
            "operator policy denied: provider-secret at https://private.invalid",
            "operator_policy",
            "BLOCKED",
            "BLOCKED",
        ),
        (
            "something new broke at startup: provider-secret https://private.invalid",
            "startup",
            "BLOCKED",
            "BLOCKED",
        ),
    ],
)
def test_failed_child_retains_only_safe_diagnostics(
    tmp_path, monkeypatch, reason, expected, outcome, expected_outcome
):
    secret = "provider-secret"
    url = "https://private.invalid"
    snapshot = "snapshot-secret"
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("VERDICT_CERT_GATEWAY_URL", url)
    monkeypatch.setenv("VERDICT_OMNIROUTE_API_KEY", secret)
    monkeypatch.setenv("VERDICT_CERT_CONNECTIONS", snapshot)
    monkeypatch.setattr(
        script, "_connections_snapshot", lambda root: root / "private-snapshot.json"
    )

    def child(command, **kwargs):
        if "identify" in command and "--screen" in command:
            # Passing preflight in the real `identify --screen --json` schema,
            # so this test still reaches the orchestration failure it covers.
            _write_proof(Path(kwargs["env"]["VERDICT_HOME"]))
            screen = {
                "mode": "screen",
                "routes": ELIGIBLE_PASSES,
                "routes_checked": 5,
                "passed": 5,
                "model_calls": 9,
                "model_requests_sent": 9,
                "stopped_reason": "complete",
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(screen).encode(), b"")
        if "orchestrate" not in command:
            return subprocess.CompletedProcess(command, 0, b"", b"")
        runs = tmp_path / "runs-clean" / "run"
        runs.mkdir(parents=True)
        event = {
            "type": "run_finished",
            "data": {
                "outcome": outcome,
                "reason": reason,
                **({"phase": "startup"} if expected == "startup" else {}),
                "secret": secret,
                "url": url,
                "snapshot": snapshot,
            },
        }
        (runs / "events.jsonl").write_text(json.dumps(event) + "\n")
        return subprocess.CompletedProcess(
            command, 1, secret.encode(), ("ValueError: " + url + snapshot).encode()
        )

    monkeypatch.setattr(script.subprocess, "run", child)
    with pytest.raises(ValueError, match="orchestration") as error:
        script.run()
    report = tmp_path / "certification-failure.json"
    raw = report.read_text()
    for sensitive in (secret, url, snapshot):
        assert sensitive not in raw
        assert sensitive not in str(error.value)
    data = json.loads(raw)
    assert data["stage"] == "orchestration"
    assert data["rehearsal"] == "clean"
    assert data["error_category"] == "child_exit"
    assert data["run_finished_reason"] == expected
    assert data["run_finished_outcome"] == expected_outcome
    # The preflight's counts travel with every orchestration failure.
    assert data["preflight"] == {
        "routes_checked": 5,
        "passed": 5,
        "planner_eligible_passed": 5,
        "planner_eligible_pools": 5,
        "planner_eligible_pools_required": 2,
        "metadata_error": None,
        "source_errors": {},
    }
    assert len(raw) < 2048
    assert not list((tmp_path / "attested-rehearsals").iterdir())


def test_workflow_always_uploads_only_safe_failure_report():
    workflow = yaml.load(
        (ROOT / ".github/workflows/certify-rehearsal.yml").read_text(), Loader=yaml.BaseLoader
    )
    steps = workflow["jobs"]["rehearse"]["steps"]
    upload = next(s for s in steps if s.get("name") == "Upload safe failure diagnostics")
    assert upload["if"] == "always()"
    assert upload["with"]["path"] == "${{ runner.temp }}/certification-failure.json"
    assert upload["with"]["if-no-files-found"] == "ignore"
    evidence = next(s for s in steps if s.get("name") == "Upload attested run evidence")
    assert evidence.get("if", "success()") == "success()"
    assert "runs-" not in upload["with"]["path"]


def test_preflight_counts_never_carry_raw_text(tmp_path, monkeypatch):
    """Catches a URL or key in a preflight field leaking into the uploaded artifact."""
    secret, url = "provider-secret", "https://private.invalid"
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("VERDICT_CERT_GATEWAY_URL", url)
    monkeypatch.setenv("VERDICT_OMNIROUTE_API_KEY", secret)
    monkeypatch.setattr(script, "_connections_snapshot", lambda root: root / "s.json")
    screen = {
        "mode": "screen",
        "routes": [{**ELIGIBLE_PASS, "capability_tier": 3, "error": url + secret}],
        "routes_checked": f"{url} {secret}",
        "passed": 1,
        "metadata_error": f"metadata at {url} key {secret}",
    }

    def child(command, **kwargs):
        assert "orchestrate" not in command
        return subprocess.CompletedProcess(command, 0, json.dumps(screen).encode(), b"")

    monkeypatch.setattr(script.subprocess, "run", child)
    with pytest.raises(script.RehearsalConfigurationError) as error:
        script.run()
    raw = (tmp_path / "certification-failure.json").read_text()
    for sensitive in (secret, url):
        assert sensitive not in raw
        assert sensitive not in str(error.value)
    assert json.loads(raw)["preflight"] == {
        "routes_checked": None,
        "passed": 1,
        "planner_eligible_passed": 0,
        "planner_eligible_pools": 0,
        "planner_eligible_pools_required": 2,
        "metadata_error": "unclassified",
        "source_errors": {},
    }


def test_preflight_names_failing_sources_without_secrets(tmp_path, monkeypatch):
    """Catches a preflight failure that cannot say which read failed, or leaks text."""
    secret, url = "provider-secret", "https://private.invalid"
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("VERDICT_CERT_GATEWAY_URL", url)
    monkeypatch.setenv("VERDICT_OMNIROUTE_API_KEY", secret)
    monkeypatch.setattr(script, "_connections_snapshot", lambda root: root / "s.json")
    screen = {
        "mode": "screen",
        "routes": [],
        "routes_checked": 0,
        "passed": 0,
        "source_errors": ["providers", "v1_models", "candidates", f"{url}/{secret}"],
        "source_error_kinds": {
            "providers": "http_401",
            "v1_models": "URLError",
            "candidates": f"{url} {secret}",
            f"{url}/{secret}": "http_500",
        },
    }

    def child(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, json.dumps(screen).encode(), b"")

    monkeypatch.setattr(script.subprocess, "run", child)
    with pytest.raises(script.RehearsalConfigurationError):
        script.run()
    raw = (tmp_path / "certification-failure.json").read_text()
    assert secret not in raw and url not in raw
    assert json.loads(raw)["preflight"]["source_errors"] == {
        "providers": "http_401",
        "v1_models": "URLError",
        "candidates": "unclassified",
    }


def test_pool_count_never_carries_route_or_pool_text(tmp_path, monkeypatch):
    """Catches a route id or pool name (report text) reaching the artifact with the pool count.

    D2 (2026-11-19): floor is 1. To trigger the floor check (planner_eligible_pools < 1)
    we use non-string route_ids so _planner_eligible_pools returns 0. The sensitive text
    in backend_pool fields must still not appear in the failure artifact.
    """
    secret, url = "provider-secret", "https://private.invalid"
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("VERDICT_CERT_GATEWAY_URL", url)
    monkeypatch.setenv("VERDICT_OMNIROUTE_API_KEY", secret)
    monkeypatch.setattr(script, "_connections_snapshot", lambda root: root / "s.json")
    screen = {
        "mode": "screen",
        # All planner-eligible passes have non-string route_ids:
        # planner_eligible_passed>0 but planner_eligible_pools=0 → triggers floor check.
        # The backend_pool fields carry sensitive text; they must NOT appear in the artifact.
        "routes": [
            {**ELIGIBLE_PASS, "route_id": [url, secret], "backend_pool": url + secret},
            {**ELIGIBLE_PASS, "route_id": {"nested": secret}, "backend_pool": "agy"},
            {**ELIGIBLE_PASS, "route_id": {"other": url}, "backend_pool": "antigravity"},
        ],
        "routes_checked": 3,
        "passed": 3,
    }

    def child(command, **kwargs):
        assert "orchestrate" not in command
        return subprocess.CompletedProcess(command, 0, json.dumps(screen).encode(), b"")

    monkeypatch.setattr(script.subprocess, "run", child)
    with pytest.raises(script.RehearsalConfigurationError, match="in 0 backend pool") as error:
        script.run()
    raw = (tmp_path / "certification-failure.json").read_text()
    for sensitive in (secret, url, "agy", "antigravity", "private"):
        assert sensitive not in raw
        assert sensitive not in str(error.value)
    preflight = json.loads(raw)["preflight"]
    # The reported pool name is ignored: the pool is derived from the route id.
    assert preflight["planner_eligible_pools"] == 0
    assert preflight["planner_eligible_pools_required"] == 2
    assert all(
        value is None or type(value) is int
        for key, value in preflight.items()
        if key != "source_errors"
    )
    assert preflight["source_errors"] == {}
