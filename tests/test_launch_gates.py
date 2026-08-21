import subprocess
from pathlib import Path


def test_security_workflow_has_non_advisory_audits_and_secret_hygiene_gate():
    workflow = Path(".github/workflows/security.yml").read_text()
    assert "uv run pip-audit --local" in workflow
    assert "uv run bandit -r verdict -ll" in workflow
    assert "git ls-files" in workflow
    assert "|| true" not in workflow
    assert "continue-on-error" not in workflow
    assert "if: false" not in workflow
    for filename in (
        ".env",
        ".env.production",
        "client.pem",
        "client.key",
        "client.crt",
        "client.cer",
        "identity.p12",
        "identity.pfx",
        "id_rsa",
        "id_ed25519",
    ):
        assert _matches_committed_credential_file(filename, workflow)


def test_release_checklist_requires_evidence_bound_signoff():
    checklist = Path("RELEASE_CHECKLIST.md").read_text()
    assert "Evidence-bound launch signoff" in checklist
    assert "PENDING EVIDENCE" in checklist
    for field in (
        "Source revision",
        "Evidence URL/command",
        "Result",
        "Limitation",
        "Reviewer/date",
    ):
        assert field in checklist


def _matches_committed_credential_file(filename: str, workflow: str) -> bool:
    """Run the workflow's checked-in regex through grep for a representative path."""
    pattern = next(
        line.split("grep -Ei '", 1)[1].rsplit("'", 1)[0]
        for line in workflow.splitlines()
        if "grep -Ei" in line
    )
    result = subprocess.run(
        ["grep", "-Ei", pattern], input=f"{filename}\n", text=True, capture_output=True, check=False
    )
    return result.returncode == 0
