"""Failure evidence is a closed vocabulary, never a provider-output upload."""
import json
import subprocess

import pytest
import yaml

from scripts import run_certification_rehearsals as script
from tests.test_certification_rehearsal_workflow import ROOT


@pytest.mark.parametrize("reason,expected", [("no eligible model: harness_inventory_unavailable", "harness_inventory_unavailable"), ("provider-secret https://private.invalid snapshot-secret", "unclassified")])
def test_failed_child_retains_only_safe_diagnostics(tmp_path, monkeypatch, reason, expected):
    secret = "provider-secret"
    url = "https://private.invalid"
    snapshot = "snapshot-secret"
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("VERDICT_CERT_GATEWAY_URL", url)
    monkeypatch.setenv("VERDICT_OMNIROUTE_API_KEY", secret)
    monkeypatch.setenv("VERDICT_CERT_CONNECTIONS", snapshot)
    monkeypatch.setattr(script, "_connections_snapshot", lambda root: root / "private-snapshot.json")

    def child(command, **kwargs):
        if "orchestrate" not in command:
            return subprocess.CompletedProcess(command, 0, b"", b"")
        runs = tmp_path / "runs-clean" / "run"
        runs.mkdir(parents=True)
        event = {"type": "run_finished", "data": {"outcome": "BLOCKED", "reason": reason, "secret": secret, "url": url, "snapshot": snapshot}}
        (runs / "events.jsonl").write_text(json.dumps(event) + "\n")
        return subprocess.CompletedProcess(command, 1, secret.encode(), ("ValueError: " + url + snapshot).encode())

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
    assert data["run_finished_outcome"] == "BLOCKED"
    assert len(raw) < 2048
    assert not list((tmp_path / "attested-rehearsals").iterdir())


def test_workflow_always_uploads_only_safe_failure_report():
    workflow = yaml.load((ROOT / ".github/workflows/certify-rehearsal.yml").read_text(), Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["rehearse"]["steps"]
    upload = next(s for s in steps if s.get("name") == "Upload safe failure diagnostics")
    assert upload["if"] == "always()"
    assert upload["with"]["path"] == "${{ runner.temp }}/certification-failure.json"
    assert upload["with"]["if-no-files-found"] == "ignore"
    evidence = next(s for s in steps if s.get("name") == "Upload attested run evidence")
    assert evidence.get("if", "success()") == "success()"
    assert "runs-" not in upload["with"]["path"]
