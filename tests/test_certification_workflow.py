"""Structural and fail-closed checks for SHA-bound evidence collection."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/certification.yml"
STEP_IDS = (
    "test_clean",
    "test_dirty",
    "ruff_check",
    "ruff_format",
    "mypy",
    "build",
    "package_smoke",
    "security",
    "docs_check",
    "git_clean",
    "rehearsals",
)


def _workflow():
    # BaseLoader preserves the YAML 1.2 `on` key (PyYAML SafeLoader treats it as bool).
    return yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)


def test_workflow_separates_cheap_push_receipt_from_manual_evidence():
    workflow = _workflow()
    assert list(workflow["on"]) == ["push", "workflow_dispatch"]
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert workflow["permissions"] == {"contents": "read"}
    jobs = workflow["jobs"]
    assert set(jobs) == {"collect-source-receipt", "collect-certification-evidence"}

    push = jobs["collect-source-receipt"]
    assert push["if"] == "github.event_name == 'push'"
    assert 0 < int(push["timeout-minutes"]) <= 5
    receipt_steps = {step["name"]: step for step in push["steps"]}
    assert receipt_steps["Check out the pushed SHA"]["with"]["ref"] == "${{ github.sha }}"
    receipt = receipt_steps["Record source SHA only"]["run"]
    assert "git rev-parse HEAD" in receipt
    assert "INCOMPLETE (source receipt only)" in receipt
    assert "no certification checks" in receipt.lower()
    assert "scripts/certify_release.py" not in str(push)
    assert "manifest.json" not in str(push)
    assert "artifacts/certification/" not in str(push)
    upload_receipt = receipt_steps["Retain source receipt (not a certification bundle)"]["with"]
    assert upload_receipt["name"].startswith("ci-source-receipt-")
    assert upload_receipt["path"] == "artifacts/ci-source-receipts/receipt.txt"
    assert upload_receipt["if-no-files-found"] == "error"
    assert int(upload_receipt["retention-days"]) >= 90

    manual = jobs["collect-certification-evidence"]
    assert manual["if"] == "github.event_name == 'workflow_dispatch'"
    assert 0 < int(manual["timeout-minutes"]) <= 90
    steps = {step["name"]: step for step in manual["steps"]}
    assert steps["Check out the exact event SHA"]["with"]["ref"] == "${{ github.sha }}"
    assert "git rev-parse HEAD" in steps["Verify checked-out SHA"]["run"]
    assert "uv sync --frozen --extra dev --extra server" in steps["Sync locked dependencies"]["run"]
    generate = steps["Generate incomplete certification evidence"]
    assert generate["continue-on-error"] == "true"
    assert "timeout --signal=TERM --kill-after=10s 70m" in generate["run"]
    assert "--rehearsal" not in generate["run"]
    check = steps["Verify INCOMPLETE evidence (not a certification gate)"]
    assert check["if"] == "${{ always() }}"
    assert check["id"] == "verify"
    upload = steps["Upload verified SHA-bound INCOMPLETE evidence"]
    assert upload["if"] == "${{ steps.verify.outcome == 'success' }}"
    assert upload["with"]["name"].startswith("certification-evidence-incomplete-")
    assert upload["with"]["if-no-files-found"] == "error"
    assert int(upload["with"]["retention-days"]) >= 90
    assert "${{ github.sha }}" in upload["with"]["path"]
    assert check["env"]["EXPECTED_SHA"] == "${{ github.sha }}"
    assert check["env"]["GENERATOR_OUTCOME"] == "${{ steps.generate.outcome }}"
    assert not any("certified" in step["name"].lower() for step in manual["steps"])


def test_push_receipt_records_source_only(sample_checkout):
    repo, sha = sample_checkout
    steps = _workflow()["jobs"]["collect-source-receipt"]["steps"]
    command = next(step["run"] for step in steps if step["name"] == "Record source SHA only")
    summary = repo / "summary.md"
    env = os.environ.copy()
    env.update(EXPECTED_SHA=sha, RUN_ID="123", GITHUB_STEP_SUMMARY=str(summary))
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", command],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    receipt = (repo / "artifacts/ci-source-receipts/receipt.txt").read_text()
    assert f"source_sha={sha}" in receipt
    assert "event=push" in receipt
    assert "run_id=123" in receipt
    assert "source-receipt-only; no certification checks or rehearsals" in receipt
    assert "manifest.json" not in receipt
    assert not (repo / "artifacts/certification").exists()
    assert sha in summary.read_text()
    assert "INCOMPLETE (source receipt only)" in summary.read_text()


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


def _run_manifest_check(repo, event_sha, *, generator_outcome="failure", summary=None):
    steps = _workflow()["jobs"]["collect-certification-evidence"]["steps"]
    command = next(step["run"] for step in steps if step["name"].startswith("Verify INCOMPLETE"))
    # Run exactly the workflow's embedded Python verifier, without a network/action runner.
    assert command.startswith("python - <<'PY'\n") and command.endswith("\nPY\n")
    code = command[len("python - <<'PY'\n") : -len("\nPY\n")]
    env = os.environ.copy()
    env["EXPECTED_SHA"] = event_sha
    env["GENERATOR_OUTCOME"] = generator_outcome
    if summary is None:
        env.pop("GITHUB_STEP_SUMMARY", None)
    else:
        env["GITHUB_STEP_SUMMARY"] = str(summary)
    return subprocess.run(
        [sys.executable, "-c", code], cwd=repo, env=env, text=True, capture_output=True
    )


def _write_bundle(repo, sha, **overrides):
    directory = repo / "artifacts" / "certification" / sha
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "1",
        "git_sha": sha,
        "git_dirty": False,
        "verdict": "INCOMPLETE",
        "steps": [
            {"step_id": name, "status": "SKIPPED" if name == "rehearsals" else "PASS"}
            for name in STEP_IDS
        ],
    }
    manifest.update(overrides)
    (directory / "manifest.json").write_text(json.dumps(manifest))
    (directory / "environment.json").write_text(json.dumps({"env_var_names": []}))
    (directory / "CERTIFICATION.md").write_text(
        f"# Release Certification: {sha}\n**Verdict**: INCOMPLETE\n**Git SHA**: {sha}\n"
    )
    (directory / "git-clean.txt").write_text("")
    return directory, manifest


def test_valid_sha_bound_incomplete_evidence_is_green_but_not_certified(sample_checkout):
    repo, sha = sample_checkout
    _write_bundle(repo, sha)
    summary = repo / "summary.md"
    result = _run_manifest_check(repo, sha, summary=summary)
    assert result.returncode == 0, result.stderr
    assert f"INCOMPLETE: valid SHA-bound evidence for {sha}" in result.stdout
    text = summary.read_text()
    assert "Release certification: INCOMPLETE" in text
    assert sha in text
    assert "not a certification" in text
    assert "CERTIFIED" not in text


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"verdict": "CERTIFIED"}, "Unexpected verdict"),
        ({"verdict": "FAILED"}, "Unexpected verdict"),
        ({"verdict": None}, "Unexpected verdict"),
        ({"git_sha": "0" * 40}, "manifest SHA mismatch"),
        ({"git_dirty": True}, "dirty tree"),
        ({"schema_version": "2"}, "Unsupported certification manifest schema"),
        ({"steps": []}, "missing, duplicate or unknown steps"),
        (
            {"steps": [{"step_id": "rehearsals", "status": "SKIPPED"}] * 11},
            "missing, duplicate or unknown steps",
        ),
        (
            {"steps": [{"step_id": name, "status": "PASS"} for name in STEP_IDS]},
            "Unexpected rehearsal evidence",
        ),
        (
            {
                "steps": [
                    {
                        "step_id": name,
                        "status": "SKIPPED" if name in {"test_clean", "rehearsals"} else "PASS",
                    }
                    for name in STEP_IDS
                ]
            },
            "Required certification evidence steps did not pass",
        ),
        (
            {
                "steps": [
                    {
                        "step_id": name,
                        "status": "FAIL"
                        if name == "security"
                        else ("SKIPPED" if name == "rehearsals" else "PASS"),
                    }
                    for name in STEP_IDS
                ]
            },
            "contains failed steps",
        ),
        (
            {
                "steps": [
                    {
                        "step_id": name,
                        "status": "INVALID"
                        if name == "security"
                        else ("SKIPPED" if name == "rehearsals" else "PASS"),
                    }
                    for name in STEP_IDS
                ]
            },
            "invalid step status",
        ),
        ({"steps": None}, "invalid steps"),
    ],
)
def test_rejects_invalid_or_manipulated_manifest(sample_checkout, overrides, expected):
    repo, sha = sample_checkout
    _write_bundle(repo, sha, **overrides)
    summary = repo / "summary.md"
    result = _run_manifest_check(repo, sha, summary=summary)
    assert result.returncode != 0
    assert expected in result.stderr
    assert not summary.exists()


@pytest.mark.parametrize("generator_outcome", ["success", "skipped", "cancelled"])
def test_rejects_unexpected_generator_outcome(sample_checkout, generator_outcome):
    repo, sha = sample_checkout
    _write_bundle(repo, sha)
    result = _run_manifest_check(repo, sha, generator_outcome=generator_outcome)
    assert result.returncode != 0
    assert "Unexpected generator outcome" in result.stderr


def test_rejects_missing_or_invalid_bundle_and_checkout_mismatch(sample_checkout):
    repo, sha = sample_checkout
    assert "Missing or invalid certification manifest" in _run_manifest_check(repo, sha).stderr
    directory, _ = _write_bundle(repo, sha)
    (directory / "manifest.json").write_text("not json")
    assert "Missing or invalid certification manifest" in _run_manifest_check(repo, sha).stderr
    _write_bundle(repo, sha)
    (directory / "environment.json").unlink()
    assert "Missing certification bundle file" in _run_manifest_check(repo, sha).stderr
    assert "Checkout SHA mismatch" in _run_manifest_check(repo, "0" * 40).stderr


@pytest.mark.parametrize(
    "file,contents,error",
    [
        ("environment.json", "not json", "Invalid certification bundle file"),
        ("environment.json", "{}", "Invalid certification environment snapshot"),
        ("CERTIFICATION.md", "**Verdict**: CERTIFIED", "Certification summary disagrees"),
        ("git-clean.txt", " M foo.py\n", "dirty git status"),
    ],
)
def test_rejects_bundle_files_that_disagree_with_manifest(sample_checkout, file, contents, error):
    repo, sha = sample_checkout
    directory, _ = _write_bundle(repo, sha)
    (directory / file).write_text(contents)
    assert error in _run_manifest_check(repo, sha).stderr
