"""Offline adversarial checks for GitHub-attested rehearsal verification."""

import json
import subprocess

import pytest
from test_certify_release import _provenance_rehearsals
from test_certify_release import certify_release as cert

SHA = "a" * 40


def verified(receipt):
    return [
        {
            "verificationResult": {
                "statement": {
                    "predicateType": "https://slsa.dev/provenance/v1",
                    "subject": [
                        {"name": "receipt.json", "digest": {"sha256": cert.sha256_file(receipt)}}
                    ],
                }
            }
        }
    ]


@pytest.fixture
def attested(tmp_path):
    root = tmp_path / "input"
    runs = _provenance_rehearsals(root, {"git_sha": SHA, "dirty": False})
    for run in runs.values():
        (run / "attestation.json").write_text("{}")
    return root


def step(tmp_path, attested, verifier=None, sha=SHA):
    return cert.step_rehearsals(
        tmp_path,
        {},
        tmp_path / "output",
        certified_git_sha=sha,
        attested_rehearsals=attested,
        attestation_verifier=verifier or (lambda receipt, *args: verified(receipt)),
    )


def test_all_good_pass_and_preserve_verification_evidence(tmp_path, attested):
    result = step(tmp_path, attested)
    assert result.status == "PASS", result.reason
    assert result._attestation_token is cert._VERIFIED_REHEARSALS
    for name in ("clean", "chaos"):
        proof = result.evidence["runs"][name]
        assert proof["producer_git_sha"] == SHA
        assert proof["attestation"]["source_digest"] == SHA
        assert (tmp_path / "output" / "rehearsals" / name / "attestation.json").is_file()


@pytest.mark.parametrize(
    "mutation",
    [
        "receipt",
        "digest",
        "sha",
        "null_sha",
        "missing_chaos",
        "clean_fault",
        "chaos_no_fault",
        "outcome",
    ],
)
def test_forgery_and_semantic_failures_fail_closed(tmp_path, attested, mutation):
    verifier = None
    sha = SHA
    if mutation in {"receipt", "outcome"}:
        path = attested / "clean" / "receipt.json"
        receipt = json.loads(path.read_text())
        receipt["outcome"] = "FORGED"
        path.write_text(json.dumps(receipt))
    elif mutation == "digest":

        def verifier(*args):
            return [
                {
                    "verificationResult": {
                        "statement": {
                            "predicateType": "https://slsa.dev/provenance/v1",
                            "subject": [{"digest": {"sha256": "b" * 64}}],
                        }
                    }
                }
            ]
    elif mutation in {"sha", "null_sha"}:
        sha = "b" * 40 if mutation == "sha" else ""
    elif mutation == "missing_chaos":
        import shutil

        shutil.rmtree(attested / "chaos")
    else:
        import shutil

        from test_certify_release import _minimal_rehearsal_run

        name = "clean" if mutation == "clean_fault" else "chaos"
        shutil.rmtree(attested / name)
        _minimal_rehearsal_run(attested / name, producer={"git_sha": SHA}, chaos=name == "clean")
        (attested / name / "attestation.json").write_text("{}")
    assert step(tmp_path, attested, verifier, sha).status == "FAIL"


@pytest.mark.parametrize("missing", ["bundle", "verifier"])
def test_missing_proof_or_unavailable_verifier_is_incomplete(tmp_path, attested, missing):
    def unavailable(*args):
        raise cert.AttestationUnavailableError("unavailable")

    if missing == "bundle":
        (attested / "clean" / "attestation.json").unlink()
    result = step(tmp_path, attested, unavailable if missing == "verifier" else None)
    assert result.status == "INCOMPLETE"


@pytest.mark.parametrize("violation", ["workflow", "ref", "sha", "issuer", "signature"])
def test_wrong_certificate_policy_rejected_by_gh_contract(tmp_path, monkeypatch, violation):
    receipt = tmp_path / "receipt.json"
    receipt.write_text("{}")
    bundle = tmp_path / "attestation.json"
    bundle.write_text("{}")

    def fake_gh(command, **kwargs):
        assert command[:3] == ["gh", "attestation", "verify"]
        assert command[command.index("--repo") + 1] == cert.ATTESTATION_REPO
        assert command[command.index("--signer-workflow") + 1] == cert.ATTESTATION_WORKFLOW
        assert command[command.index("--source-ref") + 1] == "refs/heads/main"
        assert command[command.index("--source-digest") + 1] == SHA
        assert command[command.index("--signer-digest") + 1] == SHA
        assert command[command.index("--bundle") + 1] == str(bundle)
        assert "--deny-self-hosted-runners" in command
        assert kwargs["timeout"] == 120
        return subprocess.CompletedProcess(command, 1, "", violation)

    monkeypatch.setattr(cert, "run_command", fake_gh)
    with pytest.raises(ValueError, match="verification failed"):
        cert.verify_rehearsal_attestation(receipt, bundle, cert.ATTESTATION_REPO, SHA)


@pytest.mark.parametrize("output", ["{}", "[]", "null", "invalid JSON"])
def test_unparseable_verifier_output_fails(tmp_path, monkeypatch, output):
    monkeypatch.setattr(
        cert, "run_command", lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, output, "")
    )
    with pytest.raises(ValueError):
        cert.verify_rehearsal_attestation(
            tmp_path / "receipt", tmp_path / "bundle", cert.ATTESTATION_REPO, SHA
        )


def test_missing_binary_is_unavailable(tmp_path, monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(cert.AttestationUnavailableError):
        cert.verify_rehearsal_attestation(
            tmp_path / "receipt", tmp_path / "bundle", cert.ATTESTATION_REPO, SHA
        )


def test_verifier_exception_and_timeout_are_failures(tmp_path, attested, monkeypatch):
    def broken(*args):
        raise RuntimeError("verifier error")

    assert step(tmp_path, attested, broken).status == "FAIL"

    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 120)

    monkeypatch.setattr(subprocess, "run", timed_out)
    with pytest.raises(ValueError, match="verification failed"):
        cert.verify_rehearsal_attestation(
            tmp_path / "receipt", tmp_path / "bundle", cert.ATTESTATION_REPO, SHA
        )


def _other_pass_steps():
    command = {"command": ["offline"], "exit_code": 0}
    junit = {
        "tests": 1,
        "passed": 1,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
        "testcases": {"test::case": "passed"},
    }
    steps = [
        cert.StepResult(k, k, "PASS", evidence={**command, "junit": junit})
        for k in ("test_clean", "test_dirty")
    ]
    steps += [
        cert.StepResult(k, k, "PASS", evidence=dict(command))
        for k in ("ruff_check", "ruff_format", "mypy", "docs_check", "git_clean")
    ]
    steps += [
        cert.StepResult(
            "build",
            "build",
            "PASS",
            evidence={**command, "artifacts": [{"name": "verdict.whl", "sha256": "abc"}]},
        ),
        cert.StepResult(
            "package_smoke",
            "smoke",
            "PASS",
            evidence={
                "commands": [{"command": ["uv"], "exit_code": 0}] * 3,
                "help_has_usage": True,
            },
        ),
        cert.StepResult(
            "security",
            "security",
            "PASS",
            evidence={"bandit": {"report_sha256": "abc"}, "pip_audit": {"report_sha256": "def"}},
        ),
    ]
    return steps


def test_verified_rehearsals_allow_full_certified_verdict(tmp_path, attested, monkeypatch):
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    monkeypatch.setattr(cert, "get_git_sha", lambda *args: SHA)
    monkeypatch.setattr(cert, "assert_git_sha", lambda *args: None)
    monkeypatch.setattr(cert, "is_git_dirty", lambda *args: False)
    monkeypatch.setattr(cert, "prepare_bundle_dir", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        cert, "run_command", lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, "", "")
    )
    steps = {s.step_id: s for s in _other_pass_steps()}
    for function, key in (
        ("step_test_clean_shell", "test_clean"),
        ("step_test_dirty_shell", "test_dirty"),
        ("step_ruff_check", "ruff_check"),
        ("step_ruff_format", "ruff_format"),
        ("step_mypy", "mypy"),
        ("step_build", "build"),
        ("step_package_smoke", "package_smoke"),
        ("step_security", "security"),
        ("step_docs_check", "docs_check"),
        ("step_git_clean", "git_clean"),
    ):
        monkeypatch.setattr(cert, function, lambda *args, _key=key: steps[_key])
    manifest, _ = cert.run_certification(
        tmp_path,
        attested_rehearsals=attested,
        attestation_verifier=lambda receipt, *args: verified(receipt),
        output_dir=tmp_path / "bundle",
    )
    assert len(manifest.steps) == 11
    assert manifest.verdict == "CERTIFIED"
    assert (
        cert.compute_verdict(
            [
                *_other_pass_steps(),
                cert.StepResult(
                    "rehearsals",
                    "forged",
                    "PASS",
                    evidence={"independent_producer_attestation": True},
                ),
            ],
            git_dirty=False,
        )
        == "INCOMPLETE"
    )
