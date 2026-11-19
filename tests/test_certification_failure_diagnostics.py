"""Failure evidence is a closed vocabulary, never a provider-output upload."""

import json
import subprocess

import pytest
import yaml

from scripts import run_certification_rehearsals as script
from tests.test_certification_rehearsal_workflow import ELIGIBLE_PASS, ROOT


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
            screen = {
                "mode": "screen",
                "routes": [ELIGIBLE_PASS],
                "routes_checked": 1,
                "passed": 1,
                "model_calls": 3,
                "model_requests_sent": 3,
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
        "routes_checked": 1,
        "passed": 1,
        "planner_eligible_passed": 1,
        "metadata_error": None,
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
        "metadata_error": "unclassified",
    }
