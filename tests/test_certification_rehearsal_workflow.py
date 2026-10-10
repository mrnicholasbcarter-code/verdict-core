"""Static contracts for protected independent rehearsal production."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_rehearsal_main_only_oidc_permissions_and_protected_environment():
    workflow = yaml.load(
        (ROOT / ".github/workflows/certify-rehearsal.yml").read_text(), Loader=yaml.BaseLoader
    )
    assert list(workflow["on"]) == ["workflow_dispatch"]
    assert workflow["permissions"] == {
        "contents": "read",
        "id-token": "write",
        "attestations": "write",
    }
    job = workflow["jobs"]["rehearse"]
    assert job["if"] == "github.ref == 'refs/heads/main'"
    assert job["environment"] == "certification"
    assert int(job["timeout-minutes"]) <= 70
    steps = job["steps"]
    checkout = next(s for s in steps if s.get("uses", "").startswith("actions/checkout"))
    assert checkout["with"]["ref"] == "${{ github.sha }}"
    assert checkout["with"]["persist-credentials"] == "false"
    attest = [s for s in steps if s.get("uses") == "actions/attest-build-provenance@v4"]
    assert len(attest) == 2
    assert {s["with"]["subject-path"] for s in attest} == {
        "${{ runner.temp }}/attested-rehearsals/clean/receipt.json",
        "${{ runner.temp }}/attested-rehearsals/chaos/receipt.json",
    }
    upload = next(s for s in steps if s.get("uses", "").startswith("actions/upload-artifact"))
    assert upload["with"]["name"] == "certify-rehearsals-${{ github.sha }}-${{ github.run_id }}"
    assert upload["with"]["if-no-files-found"] == "error"
    live = next(
        s for s in steps if s.get("name") == "Build fixtures and run bounded live rehearsals"
    )
    assert live["env"]["VERDICT_CERT_GATEWAY_URL"] == "${{ secrets.VERDICT_CERT_GATEWAY_URL }}"
    assert live["env"]["VERDICT_OMNIROUTE_API_KEY"] == "${{ secrets.VERDICT_CERT_GATEWAY_KEY }}"
    for step in steps:
        script = step.get("run", "")
        assert "set -x" not in script
        assert "${{ secrets." not in script
        secret_lines = [
            line
            for line in script.splitlines()
            if any(v in line for v in ("$VERDICT_CERT_GATEWAY_URL", "$VERDICT_OMNIROUTE_API_KEY"))
        ]
        assert all("test -n" in line or "::add-mask::" in line for line in secret_lines)
    reviewer = next(s for s in steps if s.get("name") == "Install pinned independent reviewer")
    assert "sha256sum --check --strict" in reviewer["run"]
    assert len(job["env"]["OCR_SHA256"]) == 64
    script = (ROOT / "scripts/run_certification_rehearsals.py").read_text()
    for flag in (
        "--capacity",
        "free,subscription",
        "--max-attempts-per-node",
        "--attempt-timeout",
        "--run-deadline",
        "verify_run_receipt",
        "capture_output=True",
    ):
        assert flag in script


def test_certification_download_and_release_compatible_upload():
    workflow = yaml.load(
        (ROOT / ".github/workflows/certification.yml").read_text(), Loader=yaml.BaseLoader
    )
    assert workflow["on"]["workflow_dispatch"]["inputs"]["rehearsal_run_id"]["required"] == "false"
    job = workflow["jobs"]["collect-certification-evidence"]
    assert job["permissions"] == {"contents": "read", "actions": "read"}
    download = next(
        s for s in job["steps"] if s.get("uses", "").startswith("actions/download-artifact")
    )
    assert download["if"] == "inputs.rehearsal_run_id != ''"
    assert download["with"]["run-id"] == "${{ inputs.rehearsal_run_id }}"
    assert download["with"]["github-token"] == "${{ github.token }}"
    assert (
        download["with"]["name"]
        == "certify-rehearsals-${{ github.sha }}-${{ inputs.rehearsal_run_id }}"
    )
    upload = job["steps"][-1]
    assert upload["with"]["name"] == (
        "certification-evidence-${{ steps.verify.outputs.artifact_kind }}-"
        "${{ github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}"
    )


def test_rehearsal_requires_protected_connections_snapshot():
    workflow = yaml.load(
        (ROOT / ".github/workflows/certify-rehearsal.yml").read_text(), Loader=yaml.BaseLoader
    )
    live = next(s for s in workflow["jobs"]["rehearse"]["steps"] if s.get("name") == "Build fixtures and run bounded live rehearsals")
    assert live["env"]["VERDICT_CERT_CONNECTIONS"] == "${{ secrets.VERDICT_CERT_CONNECTIONS }}"
    script = (ROOT / "scripts/run_certification_rehearsals.py").read_text()
    assert "VERDICT_CONNECTIONS_SNAPSHOT" in script
    assert "VERDICT_CERT_CONNECTIONS" in script
    assert "::add-mask::" in script
    assert "0o600" in script


def test_rehearsal_missing_snapshot_fails_before_work(tmp_path, monkeypatch):
    from scripts import run_certification_rehearsals as script
    import pytest

    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("VERDICT_CERT_CONNECTIONS", raising=False)
    with pytest.raises(ValueError, match="Missing protected VERDICT_CERT_CONNECTIONS"):
        script.run()
    assert not (tmp_path / "attested-rehearsals").exists()
