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
# Promised machine-readable reports (BOD-195 deliverable), each a projection of
# one or more manifest step results. filename -> step_ids it must cover.
REPORT_GROUPS = {
    "test-summary.json": ("test_clean", "test_dirty"),
    "lint-type-build.json": ("ruff_check", "ruff_format", "mypy", "build"),
    "package-smoke.json": ("package_smoke",),
    "security-summary.json": ("security",),
    "docs-check.json": ("docs_check",),
}


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
    assert "${{ github.run_attempt }}" in upload_receipt["name"]
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
    # The precise numeric exit code must be captured, not just outcome success/failure,
    # so the verifier can tell a legitimate INCOMPLETE (exit 1) apart from a
    # timeout/kill (124/137/143) or an unrelated crash.
    assert "exit_code=$code" in generate["run"]
    assert "$GITHUB_OUTPUT" in generate["run"]
    check = steps["Verify INCOMPLETE evidence (not a certification gate)"]
    assert check["if"] == "${{ always() }}"
    assert check["id"] == "verify"
    assert check["env"]["EXPECTED_SHA"] == "${{ github.sha }}"
    assert check["env"]["GENERATOR_OUTCOME"] == "${{ steps.generate.outcome }}"
    assert check["env"]["GENERATOR_EXIT_CODE"] == "${{ steps.generate.outputs.exit_code }}"
    upload = steps["Upload verified SHA-bound INCOMPLETE evidence"]
    assert upload["if"] == "${{ steps.verify.outcome == 'success' }}"
    assert upload["with"]["name"].startswith("certification-evidence-incomplete-")
    assert "${{ github.run_attempt }}" in upload["with"]["name"]
    assert upload["with"]["if-no-files-found"] == "error"
    assert int(upload["with"]["retention-days"]) >= 90
    assert "${{ github.sha }}" in upload["with"]["path"]
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


@pytest.mark.parametrize("underlying_exit", [0, 1, 2, 124, 137, 143])
def test_generate_step_captures_exact_numeric_exit_code(tmp_path, underlying_exit):
    """The shell wrapper must forward the real exit code via $GITHUB_OUTPUT.

    Simulate the three classes a reviewer must be able to tell apart:
    - 0  : generator claims CERTIFIED (never expected on this manual path, but
           the wrapper must still report the real code, not swallow it)
    - 1  : generator's own INCOMPLETE/FAILED nonzero exit
    - 124/137/143: `timeout`'s own codes for TERM/KILL-after-timeout, which the
      generator never produces on its own
    - 2  : an unrelated crash (e.g. argparse usage error), distinct from 1
    """
    steps = _workflow()["jobs"]["collect-certification-evidence"]["steps"]
    command = next(
        step["run"]
        for step in steps
        if step["name"] == "Generate incomplete certification evidence"
    )
    assert "exit_code=$code" in command
    # Replace the real generator invocation with a stub that exits with the
    # parametrized code, keeping the rest of the step's shell logic intact.
    stub = command.replace(
        "timeout --signal=TERM --kill-after=10s 70m \\\n  .venv/bin/python scripts/certify_release.py",
        f"bash -c 'exit {underlying_exit}'",
    )
    assert stub != command, "stub substitution did not match the real command"
    github_output = tmp_path / "output.txt"
    env = os.environ.copy()
    env["GITHUB_OUTPUT"] = str(github_output)
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", stub],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == underlying_exit
    output_text = github_output.read_text() if github_output.exists() else ""
    assert f"exit_code={underlying_exit}" in output_text


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


def _run_manifest_check(
    repo, event_sha, *, generator_outcome="failure", exit_code="1", summary=None
):
    steps = _workflow()["jobs"]["collect-certification-evidence"]["steps"]
    command = next(step["run"] for step in steps if step["name"].startswith("Verify INCOMPLETE"))
    # Run exactly the workflow's embedded Python verifier, without a network/action runner.
    assert command.startswith("python - <<'PY'\n") and command.endswith("\nPY\n")
    code = command[len("python - <<'PY'\n") : -len("\nPY\n")]
    env = os.environ.copy()
    env["EXPECTED_SHA"] = event_sha
    env["GENERATOR_OUTCOME"] = generator_outcome
    if exit_code is None:
        env.pop("GENERATOR_EXIT_CODE", None)
    else:
        env["GENERATOR_EXIT_CODE"] = exit_code
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
            {
                "step_id": name,
                "status": "SKIPPED" if name == "rehearsals" else "PASS",
                "reason": "",
                "evidence": {} if name == "rehearsals" else {"command": ["tool"], "exit_code": 0},
            }
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
    manifest_steps = {s["step_id"]: s for s in (manifest["steps"] or [])}
    for filename, step_ids in REPORT_GROUPS.items():
        report = {}
        for step_id in step_ids:
            step = manifest_steps.get(step_id, {})
            entry = {
                "status": step.get("status", "PASS"),
                "reason": step.get("reason", ""),
                **step.get("evidence", {}),
            }
            report[step_id] = entry
        (directory / filename).write_text(json.dumps(report))
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


@pytest.mark.parametrize(
    "exit_code,expected",
    [
        ("0", "expected exactly 1"),
        ("2", "expected exactly 1"),
        ("124", "killed or timed out"),
        ("137", "killed or timed out"),
        ("143", "killed or timed out"),
        ("-1", "expected exactly 1"),
        ("", "Missing or non-numeric"),
        ("not-a-number", "Missing or non-numeric"),
    ],
)
def test_rejects_wrong_or_missing_generator_exit_code(sample_checkout, exit_code, expected):
    """P2 fix: distinguish the generator's own exit 1 (legitimate INCOMPLETE)
    from a `timeout`-imposed kill (124/137/143) or any other crash/exit code.
    Each of these would previously pass because only steps.generate.outcome
    ('success' vs 'failure') was checked, and anything nonzero counts as
    'failure' to GitHub Actions.
    """
    repo, sha = sample_checkout
    _write_bundle(repo, sha)
    result = _run_manifest_check(repo, sha, exit_code=exit_code if exit_code else "")
    assert result.returncode != 0
    assert expected in result.stderr


def test_rejects_missing_generator_exit_code_env_var(sample_checkout):
    repo, sha = sample_checkout
    _write_bundle(repo, sha)
    result = _run_manifest_check(repo, sha, exit_code=None)
    assert result.returncode != 0
    assert "Missing or non-numeric generator exit code" in result.stderr


def test_accepts_exact_exit_code_one_only(sample_checkout):
    repo, sha = sample_checkout
    _write_bundle(repo, sha)
    result = _run_manifest_check(repo, sha, exit_code="1")
    assert result.returncode == 0, result.stderr


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


@pytest.mark.parametrize("filename", sorted(REPORT_GROUPS))
def test_rejects_missing_promised_machine_readable_report(sample_checkout, filename):
    """P2 fix: the verifier must require every report the bundle promises
    (test-summary.json, lint-type-build.json, package-smoke.json,
    security-summary.json, docs-check.json), not just manifest.json/
    environment.json/CERTIFICATION.md/git-clean.txt.
    """
    repo, sha = sample_checkout
    directory, _ = _write_bundle(repo, sha)
    (directory / filename).unlink()
    result = _run_manifest_check(repo, sha)
    assert result.returncode != 0
    assert f"Missing promised machine-readable report: {filename}" in result.stderr


@pytest.mark.parametrize("filename", sorted(REPORT_GROUPS))
def test_rejects_unparseable_machine_readable_report(sample_checkout, filename):
    repo, sha = sample_checkout
    directory, _ = _write_bundle(repo, sha)
    (directory / filename).write_text("not json")
    result = _run_manifest_check(repo, sha)
    assert result.returncode != 0
    assert f"Invalid machine-readable report {filename}" in result.stderr


@pytest.mark.parametrize("filename", sorted(REPORT_GROUPS))
def test_rejects_machine_readable_report_that_is_not_an_object(sample_checkout, filename):
    repo, sha = sample_checkout
    directory, _ = _write_bundle(repo, sha)
    (directory / filename).write_text("[]")
    result = _run_manifest_check(repo, sha)
    assert result.returncode != 0
    assert f"Machine-readable report {filename} is not a JSON object" in result.stderr


def test_rejects_machine_readable_report_missing_step_entry(sample_checkout):
    repo, sha = sample_checkout
    directory, _ = _write_bundle(repo, sha)
    report = json.loads((directory / "test-summary.json").read_text())
    del report["test_dirty"]
    (directory / "test-summary.json").write_text(json.dumps(report))
    result = _run_manifest_check(repo, sha)
    assert result.returncode != 0
    assert "missing entry for step test_dirty" in result.stderr


def test_rejects_machine_readable_report_that_disagrees_with_manifest_status(sample_checkout):
    """A report claiming a different status than the manifest for the same
    step_id must be rejected: this is exactly the drift a forged or stale
    report file would show, and the manifest alone cannot catch it.
    """
    repo, sha = sample_checkout
    directory, _ = _write_bundle(repo, sha)
    report = json.loads((directory / "security-summary.json").read_text())
    report["security"]["status"] = "FAIL"
    (directory / "security-summary.json").write_text(json.dumps(report))
    result = _run_manifest_check(repo, sha)
    assert result.returncode != 0
    assert "disagrees with manifest for step security" in result.stderr


def test_rejects_machine_readable_report_with_no_evidence_beyond_status(sample_checkout):
    """A report entry that is just `{"status": "PASS"}` with no command,
    exit_code, junit data, or other tool evidence is not machine-readable
    evidence; it is a restatement of the manifest and must be rejected.
    """
    repo, sha = sample_checkout
    directory, _ = _write_bundle(repo, sha)
    report = json.loads((directory / "docs-check.json").read_text())
    report["docs_check"] = {"status": "PASS"}
    (directory / "docs-check.json").write_text(json.dumps(report))
    result = _run_manifest_check(repo, sha)
    assert result.returncode != 0
    assert "disagrees with manifest for step docs_check" in result.stderr


def test_accepts_skipped_report_entry_without_extra_evidence(sample_checkout):
    """A SKIPPED step (e.g. docs_check when check_doc_links.py is absent) has
    nothing to execute, so a bare {"status": "SKIPPED", "reason": ...} entry
    is legitimate and must not be rejected as 'no evidence'.
    """
    repo, sha = sample_checkout
    directory, _manifest = _write_bundle(
        repo,
        sha,
        steps=[
            {
                "step_id": name,
                "status": "SKIPPED" if name in {"docs_check", "rehearsals"} else "PASS",
                "reason": "",
                "evidence": {}
                if name in {"docs_check", "rehearsals"}
                else {"command": ["tool"], "exit_code": 0},
            }
            for name in STEP_IDS
        ],
    )
    report = json.loads((directory / "docs-check.json").read_text())
    report["docs_check"] = {"status": "SKIPPED", "reason": "check_doc_links.py not found"}
    manifest = json.loads((directory / "manifest.json").read_text())
    next(step for step in manifest["steps"] if step["step_id"] == "docs_check")["reason"] = (
        "check_doc_links.py not found"
    )
    (directory / "manifest.json").write_text(json.dumps(manifest))
    (directory / "docs-check.json").write_text(json.dumps(report))
    result = _run_manifest_check(repo, sha)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "replacement",
    [
        {"status": "PASS", "reason": "", "command": ["FORGED"], "exit_code": 255},
        {"status": "PASS", "reason": "", "evidence": None},
        {"status": "PASS", "reason": "", "command": ["tool"], "exit_code": 0, "extra": "forged"},
    ],
)
def test_rejects_report_evidence_not_identical_to_manifest(sample_checkout, replacement):
    repo, sha = sample_checkout
    directory, _ = _write_bundle(repo, sha)
    path = directory / "test-summary.json"
    report = json.loads(path.read_text())
    report["test_clean"] = replacement
    path.write_text(json.dumps(report))
    result = _run_manifest_check(repo, sha)
    assert result.returncode != 0
    assert "disagrees with manifest for step test_clean" in result.stderr


def test_rejects_null_manifest_evidence_even_with_matching_report(sample_checkout):
    repo, sha = sample_checkout
    directory, manifest = _write_bundle(repo, sha)
    next(step for step in manifest["steps"] if step["step_id"] == "test_clean")["evidence"] = None
    (directory / "manifest.json").write_text(json.dumps(manifest))
    result = _run_manifest_check(repo, sha)
    assert result.returncode != 0
    assert "Invalid manifest evidence for step test_clean" in result.stderr


def test_real_certifier_bundle_passes_workflow_verifier(sample_checkout):
    """Exercise the real generator serializer and publisher, not a hand-written manifest."""
    from scripts import certify_release as certifier

    repo, _ = sample_checkout
    # The managed output must be ignored to keep a real git-clean status.
    (repo / ".git/info/exclude").write_text("artifacts/\n")
    sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    steps = [
        certifier.StepResult(
            step_id=name,
            name=name,
            status="SKIPPED" if name == "rehearsals" else "PASS",
            reason="Live rehearsals not provided" if name == "rehearsals" else "",
            evidence={}
            if name == "rehearsals"
            else {"command": ["offline-fixture", name], "exit_code": 0},
        )
        for name in STEP_IDS
    ]
    assert all({"status", "reason"}.isdisjoint(step.evidence) for step in steps)
    manifest = certifier.CertificationManifest(
        git_sha=sha, steps=steps, verdict=certifier.compute_verdict(steps, git_dirty=False)
    )
    assert manifest.verdict == "INCOMPLETE"
    root = repo / "artifacts/certification"
    stage = root / ".cert-stage-offline"
    output = root / sha
    env_snapshot = certifier.EnvironmentSnapshot(
        python_version=sys.version.split()[0], platform=sys.platform, uv_version="fixture"
    )
    certifier.write_bundle(stage, manifest, env_snapshot, repo_path=repo)
    certifier.publish_bundle(stage, output, repo, sha)
    generated = json.loads((output / "manifest.json").read_text())
    assert all({"status", "reason"}.isdisjoint(step["evidence"]) for step in generated["steps"])
    result = _run_manifest_check(repo, sha)
    assert result.returncode == 0, result.stderr
    assert "INCOMPLETE:" in result.stdout

    report = output / "test-summary.json"
    report.unlink()
    assert "Missing promised machine-readable report" in _run_manifest_check(repo, sha).stderr
    certifier.write_bundle(output, manifest, env_snapshot, repo_path=repo)
    data = json.loads(report.read_text())
    data["test_clean"]["exit_code"] = 55
    report.write_text(json.dumps(data))
    assert "disagrees with manifest" in _run_manifest_check(repo, sha).stderr


def test_certifier_never_constructs_reserved_evidence_keys():
    """Check generator evidence constructors, not just the synthetic fixture."""
    import ast

    source = Path(__file__).resolve().parents[1] / "scripts/certify_release.py"
    tree = ast.parse(source.read_text())
    evidence_maps = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            evidence_maps.extend(
                keyword.value for keyword in node.keywords if keyword.arg == "evidence"
            )
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(
                isinstance(target, ast.Attribute) and target.attr == "evidence"
                for target in targets
            ):
                evidence_maps.append(node.value)
    assert evidence_maps, "generator evidence assignments were not inspected"
    for evidence in evidence_maps:
        for item in ast.walk(evidence):
            if isinstance(item, ast.Dict):
                keys = {key.value for key in item.keys if isinstance(key, ast.Constant)}
                assert keys.isdisjoint({"status", "reason"}), (evidence.lineno, keys)


def test_verifier_rejects_reserved_evidence_keys(sample_checkout):
    repo, sha = sample_checkout
    directory, manifest = _write_bundle(repo, sha)
    next(step for step in manifest["steps"] if step["step_id"] == "test_clean")["evidence"][
        "status"
    ] = "PASS"
    (directory / "manifest.json").write_text(json.dumps(manifest))
    assert "Reserved manifest evidence key" in _run_manifest_check(repo, sha).stderr


RELEASE_WORKFLOW = WORKFLOW.with_name("release.yml")


def _release_gate():
    release = yaml.load(RELEASE_WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    gate = release["jobs"]["certification-gate"]
    assert gate["permissions"] == {"actions": "read"}
    assert set(release["jobs"]["release"]["needs"]) == {"acceptance", "certification-gate"}
    steps = gate["steps"]
    assert steps[1]["if"] == "steps.locate.outputs.found == 'true'"
    assert steps[1]["with"]["artifact-ids"] == "${{ steps.locate.outputs.artifact_id }}"
    assert steps[1]["with"]["run-id"] == "${{ steps.locate.outputs.run_id }}"
    assert steps[1]["with"]["github-token"] == "${{ github.token }}"
    assert steps[1]["with"]["merge-multiple"] == "true"
    assert steps[2]["if"] == "steps.locate.outputs.found == 'true'"
    return steps


def _embedded_python(command):
    assert command.startswith("python - <<'PY'\n") and command.endswith("\nPY\n")
    return command[len("python - <<'PY'\n") : -len("\nPY\n")]


def _run_gate_step(code, repo, sha, *, required, responses=None):
    import stat

    summary = repo / "release-summary.md"
    output = repo / "gate-outputs.txt"
    env = os.environ.copy()
    env.update(
        REQUIRE_CERTIFIED_BUNDLE="true" if required else "false",
        EXPECTED_SHA=sha,
        GITHUB_REPOSITORY="owner/project",
        GITHUB_STEP_SUMMARY=str(summary),
        GITHUB_OUTPUT=str(output),
    )
    if responses is not None:
        bindir = repo / "bin"
        bindir.mkdir(exist_ok=True)
        mock = bindir / "gh"
        mock.write_text(
            "#!" + sys.executable + "\n"
            "import json, os, sys\n"
            "endpoint = sys.argv[-1]\n"
            'data = json.loads(os.environ["GH_FIXTURES"])\n'
            "if endpoint not in data: sys.exit(1)\n"
            "print(json.dumps(data[endpoint]))\n"
        )
        mock.chmod(mock.stat().st_mode | stat.S_IXUSR)
        env["PATH"] = str(bindir) + os.pathsep + env["PATH"]
        env["GH_FIXTURES"] = json.dumps(responses)
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=repo, env=env, text=True, capture_output=True
    )
    return result, summary, output


def _artifact_responses(sha, *, artifact_name=None, expired=False):
    run_id = 321
    name = artifact_name or f"certification-evidence-certified-{sha}-{run_id}-2"
    return {
        f"repos/owner/project/actions/workflows/certification.yml/runs?head_sha={sha}&status=success&per_page=100": [
            {
                "workflow_runs": [
                    {
                        "id": run_id,
                        "run_attempt": 2,
                        "head_sha": sha,
                        "event": "workflow_dispatch",
                        "conclusion": "success",
                        "status": "completed",
                    }
                ]
            }
        ],
        f"repos/owner/project/actions/runs/{run_id}/artifacts?per_page=100": [
            {"artifacts": [{"id": 123, "name": name, "expired": expired}]}
        ],
    }


def test_release_gate_disabled_warns_without_claiming_certification(sample_checkout):
    repo, sha = sample_checkout
    steps = _release_gate()
    assert (
        steps[0]["env"]["REQUIRE_CERTIFIED_BUNDLE"]
        == "${{ vars.VERDICT_REQUIRE_CERTIFIED_BUNDLE }}"
    )
    result, summary, output = _run_gate_step(
        _embedded_python(steps[0]["run"]), repo, sha, required=False
    )
    assert result.returncode == 0, result.stderr
    assert "::warning::" in result.stdout
    assert "NOT certification-gated" in result.stdout
    assert "NOT certification-gated" in summary.read_text()
    assert not output.exists()


@pytest.mark.parametrize("kind", ["missing", "expired", "incomplete-name"])
def test_release_gate_enabled_requires_retained_exact_sha_artifact(sample_checkout, kind):
    repo, sha = sample_checkout
    steps = _release_gate()
    responses = _artifact_responses(
        sha,
        expired=(kind == "expired"),
        artifact_name=f"certification-evidence-incomplete-{sha}-321-2"
        if kind == "incomplete-name"
        else None,
    )
    if kind == "missing":
        responses[next(key for key in responses if key.endswith("/artifacts?per_page=100"))] = [
            {"artifacts": []}
        ]
    result, summary, output = _run_gate_step(
        _embedded_python(steps[0]["run"]), repo, sha, required=True, responses=responses
    )
    assert result.returncode != 0
    assert "No retained certified" in result.stderr
    assert not summary.exists()
    assert not output.exists()


@pytest.mark.parametrize("merge_multiple", [False, True], ids=["nested", "flattened"])
def test_release_gate_requires_manifest_at_exact_download_path(sample_checkout, merge_multiple):
    """Artifact IDs alone nest the v4 download under the artifact name."""
    repo, sha = sample_checkout
    steps = _release_gate()
    directory = repo / "certification-bundle"
    if not merge_multiple:
        directory /= f"certification-evidence-certified-{sha}-321-2"
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "git_sha": sha,
                "git_dirty": False,
                "verdict": "CERTIFIED",
                "steps": [{"step_id": name, "status": "PASS"} for name in STEP_IDS],
            }
        )
    )
    (directory / "git-clean.txt").write_text("")
    verified, summary, _ = _run_gate_step(
        _embedded_python(steps[2]["run"]), repo, sha, required=True
    )
    if merge_multiple:
        assert verified.returncode == 0, verified.stderr
        assert f"CERTIFIED bundle for exact release SHA {sha}" in verified.stdout
        assert sha in summary.read_text()
    else:
        assert verified.returncode != 0
        assert (
            "Missing certified manifest at expected path: certification-bundle/manifest.json"
            in verified.stderr
        )
        assert not summary.exists()


@pytest.mark.parametrize(
    "verdict,matching_sha,success",
    [("CERTIFIED", True, True), ("INCOMPLETE", True, False), ("CERTIFIED", False, False)],
)
def test_release_gate_enabled_downloaded_manifest_verification(
    sample_checkout, verdict, matching_sha, success
):
    repo, sha = sample_checkout
    steps = _release_gate()
    locate, _, output = _run_gate_step(
        _embedded_python(steps[0]["run"]),
        repo,
        sha,
        required=True,
        responses=_artifact_responses(sha),
    )
    assert locate.returncode == 0, locate.stderr
    assert "artifact_id=123" in output.read_text()
    assert "run_id=321" in output.read_text()
    directory = repo / "certification-bundle"
    directory.mkdir()
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "git_sha": sha if matching_sha else "0" * 40,
                "git_dirty": False,
                "verdict": verdict,
                "steps": [{"step_id": name, "status": "PASS"} for name in STEP_IDS],
            }
        )
    )
    (directory / "git-clean.txt").write_text("")
    verified, summary, _ = _run_gate_step(
        _embedded_python(steps[2]["run"]), repo, sha, required=True
    )
    assert (verified.returncode == 0) is success
    if success:
        assert "CERTIFIED bundle for exact release SHA" in verified.stdout
        assert sha in summary.read_text()
    else:
        assert "does not match" in verified.stderr or "does not have a CERTIFIED" in verified.stderr
        assert not summary.exists()
