"""Static contracts for protected independent rehearsal production."""

import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
# A screen row the frontier planner can admit: known tier <= 2, tools, 100k context.
ELIGIBLE_PASS = {
    "route_id": "p/claude-sonnet-4.5",
    "result": "pass",
    "capability_tier": 1,
    "supports_tools": True,
    "context_window": 200_000,
}


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
    # The pin itself comes from verdict.ocr_release (tests/test_ocr_release.py).
    assert 'test "${#OCR_SHA256}" = 64' in reviewer["run"]
    script = (ROOT / "scripts/run_certification_rehearsals.py").read_text()
    for flag in (
        "--capacity",
        '"free"',
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
    live = next(
        s
        for s in workflow["jobs"]["rehearse"]["steps"]
        if s.get("name") == "Build fixtures and run bounded live rehearsals"
    )
    assert live["env"]["VERDICT_CERT_CONNECTIONS"] == "${{ secrets.VERDICT_CERT_CONNECTIONS }}"
    script = (ROOT / "scripts/run_certification_rehearsals.py").read_text()
    assert "VERDICT_CONNECTIONS_SNAPSHOT" in script
    assert "VERDICT_CERT_CONNECTIONS" in script
    assert "::add-mask::" in script
    assert "0o600" in script


def test_rehearsal_missing_snapshot_fails_before_work(tmp_path, monkeypatch):
    import pytest

    from scripts import run_certification_rehearsals as script

    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("VERDICT_CERT_CONNECTIONS", raising=False)
    with pytest.raises(ValueError, match="Missing protected VERDICT_CERT_CONNECTIONS"):
        script.run()
    assert not (tmp_path / "attested-rehearsals").exists()


def test_rehearsal_secret_materializes_privately_and_masks(tmp_path, monkeypatch, capsys):
    import hashlib
    import json
    import stat
    from datetime import datetime, timezone

    from scripts import run_certification_rehearsals as script
    from verdict.orchestration.run import sanitize_connections

    payload = {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "connections": sanitize_connections(
            [{"provider": "claude", "authType": "oauth", "isActive": True}]
        ),
    }
    payload["sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    raw = json.dumps(payload, indent=2)
    monkeypatch.setenv("VERDICT_CERT_CONNECTIONS", raw)
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / "home"))
    path = script._connections_snapshot(tmp_path)
    assert path.parent == tmp_path
    assert path.read_text() == raw
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert (
        capsys.readouterr().out
        == "::add-mask::" + raw.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A") + "\n"
    )


def test_rehearsal_invalid_snapshot_fails_before_work(tmp_path, monkeypatch):
    import pytest

    from scripts import run_certification_rehearsals as script

    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("VERDICT_CERT_CONNECTIONS", "not-json")
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / "home"))
    with pytest.raises(
        script.RehearsalConfigurationError, match="Invalid protected VERDICT_CERT_CONNECTIONS"
    ):
        script.run()
    assert list(tmp_path.iterdir()) == []


def test_live_rehearsal_dispatch_is_free_only_without_subscription_escape(tmp_path, monkeypatch):
    """Policy invariant: every rehearsal subprocess is constrained FREE-only.

    This proves the Verdict dispatch filter, not OmniRoute's upstream boundary;
    BOD-332's final-target tests remain a separate release prerequisite.
    """
    import json
    import subprocess

    import pytest

    from scripts import run_certification_rehearsals as script

    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("VERDICT_CERT_GATEWAY_URL", "https://gateway.invalid")
    monkeypatch.setenv("VERDICT_OMNIROUTE_API_KEY", "test-key-canary")
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setattr(script, "_connections_snapshot", lambda root: root / "snapshot.json")
    captures = []

    def record_child(argv, **kwargs):
        if "orchestrate" in argv:
            captures.append(argv)
            return subprocess.CompletedProcess(argv, 1, b"", b"")
        # Preflight screen: return minimal valid proof so orchestrate runs
        if "identify" in argv and "--screen" in argv:
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps(
                    {
                        "routes_checked": 1,
                        "passed": 1,
                        "model_calls": 3,
                        "model_requests_sent": 3,
                        "stopped_reason": "complete",
                        "routes": [ELIGIBLE_PASS],
                        "mode": "screen",
                    }
                ).encode(),
                b"",
            )
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(script.subprocess, "run", record_child)
    with pytest.raises(ValueError):
        script.run()
    assert len(captures) == 1
    argv = captures[0]
    assert argv[argv.index("--capacity") + 1] == "free"
    assert not any(value in argv for value in ("free,subscription", "metered", "unknown"))


def test_preflight_argv_is_free_only_and_bounded(tmp_path, monkeypatch):
    """Preflight screen command uses FREE-only capacity and explicit bounds."""
    import subprocess

    import pytest

    from scripts import run_certification_rehearsals as script

    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("VERDICT_CERT_GATEWAY_URL", "https://gateway.invalid")
    monkeypatch.setenv("VERDICT_OMNIROUTE_API_KEY", "test-key-canary")
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setattr(script, "_connections_snapshot", lambda root: root / "snapshot.json")
    preflight_args = []

    monkeypatch.delenv("OMNIROUTE_BASE_URL", raising=False)

    def record_child(argv, **kwargs):
        if "identify" in argv and "--screen" in argv:
            # identify has no --gateway flag; CI sets no OMNIROUTE_BASE_URL, so the
            # preflight must point the screen at the certification gateway itself.
            assert kwargs["env"]["OMNIROUTE_BASE_URL"] == "https://gateway.invalid"
            preflight_args.append(argv)
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps(
                    {
                        "routes_checked": 1,
                        "passed": 1,
                        "model_calls": 3,
                        "model_requests_sent": 3,
                        "stopped_reason": "complete",
                        "routes": [ELIGIBLE_PASS],
                        "mode": "screen",
                    }
                ).encode(),
                b"",
            )
        return subprocess.CompletedProcess(argv, 1, b"", b"")

    monkeypatch.setattr(script.subprocess, "run", record_child)
    with pytest.raises(ValueError):
        script.run()

    assert len(preflight_args) == 1
    argv = preflight_args[0]
    assert "--screen" in argv
    assert "--allow-live-probe" in argv
    assert "--capacity" in argv
    cap_idx = argv.index("--capacity")
    assert argv[cap_idx + 1] == "free"
    # Bounded: max-per-provider and max-routes are small explicit values
    assert "--max-per-provider" in argv
    assert "--max-routes" in argv
    mp_idx = argv.index("--max-per-provider")
    mr_idx = argv.index("--max-routes")
    max_per_provider = int(argv[mp_idx + 1])
    max_routes = int(argv[mr_idx + 1])
    assert 0 < max_per_provider <= script.PREFLIGHT_MAX_PER_PROVIDER
    assert 0 < max_routes <= script.PREFLIGHT_MAX_ROUTES


def test_preflight_fails_closed_when_no_free_routes_qualify(tmp_path, monkeypatch):
    """Empty preflight report prevents orchestrate from running."""
    import subprocess

    import pytest

    from scripts import run_certification_rehearsals as script

    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("VERDICT_CERT_GATEWAY_URL", "https://gateway.invalid")
    monkeypatch.setenv("VERDICT_OMNIROUTE_API_KEY", "test-key-canary")
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setattr(script, "_connections_snapshot", lambda root: root / "snapshot.json")
    orchestrate_called = []

    def record_child(argv, **kwargs):
        if "orchestrate" in argv:
            orchestrate_called.append(True)
            return subprocess.CompletedProcess(argv, 1, b"", b"")
        # Preflight returns zero qualified routes
        if "identify" in argv and "--screen" in argv:
            return subprocess.CompletedProcess(
                argv,
                0,
                b'{"routes_checked": 0, "passed": 0, "model_calls": 0, "model_requests_sent": 0, "stopped_reason": "complete", "routes": [], "mode": "screen"}',
                b"",
            )
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(script.subprocess, "run", record_child)
    with pytest.raises(script.RehearsalConfigurationError, match="no qualified FREE routes"):
        script.run()

    # Orchestrate must never have been called
    assert len(orchestrate_called) == 0


def test_preflight_failure_message_never_echoes_child_output(tmp_path, monkeypatch):
    """A failed preflight prints a fixed message, never child stderr (credentials, URL)."""
    import subprocess

    import pytest

    from scripts import run_certification_rehearsals as script

    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("VERDICT_CERT_GATEWAY_URL", "https://gateway.invalid")
    monkeypatch.setenv("VERDICT_OMNIROUTE_API_KEY", "test-key-canary")
    monkeypatch.setattr(script, "_connections_snapshot", lambda root: root / "snapshot.json")
    orchestrated = []

    def child(argv, **kwargs):
        if "orchestrate" in argv:
            orchestrated.append(argv)
        if "identify" in argv:
            return subprocess.CompletedProcess(argv, 2, b"test-key-canary", b"test-key-canary")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(script.subprocess, "run", child)
    with pytest.raises(script.RehearsalConfigurationError) as error:
        script.run()
    assert "test-key-canary" not in str(error.value)
    assert "gateway.invalid" not in str(error.value)
    assert orchestrated == []


def _preflight_only(tmp_path, monkeypatch, screen_report):
    """Run the script with a stubbed preflight; returns orchestrate argvs seen."""
    import subprocess

    from scripts import run_certification_rehearsals as script

    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("VERDICT_CERT_GATEWAY_URL", "https://gateway.invalid")
    monkeypatch.setenv("VERDICT_OMNIROUTE_API_KEY", "test-key-canary")
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setattr(script, "_connections_snapshot", lambda root: root / "snapshot.json")
    seen = {"orchestrate": [], "preflight": []}

    def child(argv, **kwargs):
        if "orchestrate" in argv:
            seen["orchestrate"].append(argv)
            return subprocess.CompletedProcess(argv, 1, b"", b"")
        if "identify" in argv:
            seen["preflight"].append(argv)
            return subprocess.CompletedProcess(argv, 0, json.dumps(screen_report).encode(), b"")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(script.subprocess, "run", child)
    return script, seen


def _screen(routes, **extra):
    return {
        "mode": "screen",
        "routes": routes,
        "routes_checked": sum(r["result"] in {"pass", "fail"} for r in routes),
        "passed": sum(r["result"] == "pass" for r in routes),
        "model_calls": 3,
        "model_requests_sent": 3,
        "stopped_reason": "complete",
        "metadata_error": "metadata_unavailable",
        **extra,
    }


def test_preflight_argv_asks_for_planner_tier(tmp_path, monkeypatch):
    """Catches the preflight screening tiers the planner can never admit."""
    import pytest

    from verdict.orchestration.contracts import PLANNER_MAX_CAPABILITY_TIER

    script, seen = _preflight_only(tmp_path, monkeypatch, _screen([ELIGIBLE_PASS]))
    with pytest.raises(ValueError):
        script.run()
    argv = seen["preflight"][0]
    assert argv[argv.index("--max-capability-tier") + 1] == str(PLANNER_MAX_CAPABILITY_TIER) == "2"
    assert script.PREFLIGHT_MAX_ROUTES <= 8


def _bad(**overrides):
    return {**ELIGIBLE_PASS, **overrides}


@pytest.mark.parametrize(
    "rows",
    [
        [_bad(capability_tier=3), _bad(route_id="cf/gemma", capability_tier=3)],
        [_bad(capability_tier=None)],
        [_bad(supports_tools=False)],
        [_bad(context_window=32_000)],
        [_bad(context_window=None)],
        [_bad(result="fail"), _bad(route_id="cf/gemma", capability_tier=3)],
    ],
)
def test_preflight_passes_the_planner_rejects_fail_closed(tmp_path, monkeypatch, rows):
    """Catches #1067's `passed > 0` check being met by routes the planner rejects."""
    script, seen = _preflight_only(tmp_path, monkeypatch, _screen(rows))
    with pytest.raises(
        script.RehearsalConfigurationError,
        match=r"no planner-eligible FREE routes \(tier<=2, tools, 100k context\)",
    ):
        script.run()
    assert seen["orchestrate"] == []
    data = json.loads((tmp_path / "certification-failure.json").read_text())
    assert data["stage"] == "preflight"
    assert data["preflight"]["planner_eligible_passed"] == 0
    assert data["preflight"]["metadata_error"] == "metadata_unavailable"


def _rehearsal_with_run_file(tmp_path, monkeypatch, leak):
    """Drive run() to its evidence scan: orchestrate 'writes' a complete run whose
    extra file holds ``leak``. Returns (script, env seen by orchestrate)."""
    import subprocess

    from scripts import run_certification_rehearsals as script

    gateway, key = "https://cert-gw-7f3a9c.example.invalid:8443", "test-key-canary-9d2e"
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("VERDICT_CERT_GATEWAY_URL", gateway)
    monkeypatch.setenv("VERDICT_OMNIROUTE_API_KEY", key)
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setattr(script, "_connections_snapshot", lambda root: root / "snapshot.json")
    monkeypatch.setattr(script, "verify_run_receipt", lambda run_dir: [])
    seen = {}

    def child(argv, **kwargs):
        if "orchestrate" in argv:
            seen["env"] = kwargs.get("env") or {}
            run_dir = Path(argv[argv.index("--runs-dir") + 1]) / "run1"
            run_dir.mkdir(parents=True)
            (run_dir / "receipt.json").write_text(
                json.dumps(
                    {
                        "outcome": "COMPLETE",
                        "claimed_outcome": "COMPLETE",
                        "producer": {"git_sha": "a" * 40},
                        "nodes": [],
                    }
                )
            )
            text = {"gateway": gateway, "key": key, "none": "redacted"}[leak]
            (run_dir / "events.jsonl").write_text(json.dumps({"gateway_origin": text}) + "\n")
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if "identify" in argv:
            report = _screen([ELIGIBLE_PASS])
            return subprocess.CompletedProcess(argv, 0, json.dumps(report).encode(), b"")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(script.subprocess, "run", child)
    return script, seen


def test_rehearsal_orchestrate_is_asked_to_redact_the_gateway_origin(tmp_path, monkeypatch):
    """Catches: the rehearsal running orchestrate without the redaction setting."""
    from verdict.orchestration.runtime import REDACT_GATEWAY_ORIGIN_ENV

    script, seen = _rehearsal_with_run_file(tmp_path, monkeypatch, "none")
    with pytest.raises(ValueError, match="Chaos must contain a controlled failure"):
        script.run()  # clean passes the scan; the stub's chaos run has no failure
    assert seen["env"][REDACT_GATEWAY_ORIGIN_ENV] == "1"


@pytest.mark.parametrize("leak", ["gateway", "key"])
def test_rehearsal_scan_still_refuses_evidence_holding_a_protected_value(
    tmp_path, monkeypatch, leak
):
    """Catches: the evidence scan being narrowed (one case per protected value)."""
    script, _ = _rehearsal_with_run_file(tmp_path, monkeypatch, leak)
    with pytest.raises(ValueError, match="protected configuration; refusing upload"):
        script.run()
    assert not (tmp_path / "attested-rehearsals" / "clean").exists()
