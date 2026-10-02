"""Structural and fail-closed checks for manual certification evidence collection."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/certification.yml"


def _workflow():
    # BaseLoader preserves the YAML 1.2 `on` key (PyYAML SafeLoader treats it as bool).
    return yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)


def test_certification_evidence_workflow_is_bounded_and_manual_only():
    workflow = _workflow()
    assert list(workflow["on"]) == ["workflow_dispatch"]
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["collect-certification-evidence"]
    assert 0 < int(job["timeout-minutes"]) <= 90
    steps = {step["name"]: step for step in job["steps"]}
    assert steps["Check out the exact event SHA"]["with"]["ref"] == "${{ github.sha }}"
    assert "git rev-parse HEAD" in steps["Verify checked-out SHA"]["run"]
    assert "uv sync --frozen --extra dev --extra server" in steps["Sync locked dependencies"]["run"]
    generate = steps["Generate incomplete certification evidence"]
    assert generate["continue-on-error"] == "true"
    assert "timeout --signal=TERM --kill-after=10s 70m" in generate["run"]
    assert "--rehearsal" not in generate["run"]
    upload = steps["Upload SHA-bound evidence even after generator failure"]
    assert upload["if"] == "${{ always() }}"
    assert upload["with"]["if-no-files-found"] == "error"
    assert int(upload["with"]["retention-days"]) >= 90
    assert "${{ github.sha }}" in upload["with"]["path"]
    check = steps["Check manifest and fail closed on incomplete certification"]
    assert check["if"] == "${{ always() }}"
    assert check["env"]["EXPECTED_SHA"] == "${{ github.sha }}"


@pytest.fixture
def sample_checkout(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.email=ci@example.com",
            "-c",
            "user.name=CI",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "test",
        ],
        check=True,
    )
    sha = subprocess.check_output(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True
    ).strip()
    return tmp_path, sha


def _run_manifest_check(repo, event_sha):
    steps = _workflow()["jobs"]["collect-certification-evidence"]["steps"]
    command = next(step["run"] for step in steps if step["name"].startswith("Check manifest"))
    # Run exactly the workflow's embedded Python verifier, without a network/action runner.
    assert command.startswith("python - <<'PY'\n") and command.endswith("\nPY\n")
    code = command[len("python - <<'PY'\n") : -len("\nPY\n")]
    env = os.environ.copy()
    env["EXPECTED_SHA"] = event_sha
    env.pop("GITHUB_STEP_SUMMARY", None)
    return subprocess.run(
        [sys.executable, "-c", code], cwd=repo, env=env, text=True, capture_output=True
    )


@pytest.mark.parametrize(
    "verdict,sha_override,steps,dirty,expected",
    [
        (
            "INCOMPLETE",
            None,
            [{"step_id": "rehearsals", "status": "SKIPPED"}],
            False,
            "Certification gate failed closed",
        ),
        (
            "CERTIFIED",
            None,
            [{"step_id": "rehearsals", "status": "SKIPPED"}],
            False,
            "Unexpected verdict",
        ),
        (
            "FAILED",
            None,
            [{"step_id": "rehearsals", "status": "SKIPPED"}],
            False,
            "Unexpected verdict",
        ),
        (
            "INCOMPLETE",
            "0" * 40,
            [{"step_id": "rehearsals", "status": "SKIPPED"}],
            False,
            "manifest SHA mismatch",
        ),
        (
            "INCOMPLETE",
            None,
            [{"step_id": "rehearsals", "status": "PASS"}],
            False,
            "Unexpected rehearsal evidence",
        ),
        (
            "INCOMPLETE",
            None,
            [
                {"step_id": "rehearsals", "status": "SKIPPED"},
                {"step_id": "security", "status": "FAIL"},
            ],
            False,
            "contains failed steps",
        ),
        ("INCOMPLETE", None, [{"step_id": "rehearsals", "status": "SKIPPED"}], True, "dirty tree"),
    ],
)
def test_manifest_check_never_accepts_uncertified_evidence(
    sample_checkout, verdict, sha_override, steps, dirty, expected
):
    repo, sha = sample_checkout
    path = repo / "artifacts" / "certification" / sha / "manifest.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {"git_sha": sha_override or sha, "git_dirty": dirty, "steps": steps, "verdict": verdict}
        )
    )
    result = _run_manifest_check(repo, sha)
    assert result.returncode != 0
    assert expected in result.stderr


def test_manifest_check_rejects_missing_bundle(sample_checkout):
    repo, sha = sample_checkout
    result = _run_manifest_check(repo, sha)
    assert result.returncode != 0
    assert "Missing or invalid certification manifest" in result.stderr
