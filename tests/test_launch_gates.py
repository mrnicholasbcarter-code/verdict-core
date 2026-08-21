import subprocess
from pathlib import Path


def test_security_workflow_has_non_advisory_audits_and_secret_hygiene_gate():
    workflow = Path(".github/workflows/security.yml").read_text()
    ci_workflow = Path(".github/workflows/ci.yml").read_text()
    assert "- name: Run pip-audit\n        run: uv run pip-audit --local" in workflow
    assert "- name: Run bandit\n        run: uv run bandit -r verdict -ll -s B108" in workflow
    assert (
        "- name: Run dependency audit\n        run: python -m pip_audit --local --skip-editable"
        in ci_workflow
    )
    assert "git ls-files" in workflow
    assert "|| true" not in workflow + ci_workflow
    assert "continue-on-error" not in workflow + ci_workflow
    assert "if: false" not in workflow + ci_workflow
    assert "if: ${{ false }}" not in workflow + ci_workflow
    for filename in (
        ".env",
        ".env.production",
        ".envrc",
        "production.env",
        "config/test.env",
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
    assert _matches_committed_credential_file("secrets/.env.memory.example", workflow)
    assert _matches_committed_credential_file(".ENV.MEMORY.EXAMPLE", workflow)
    assert _credential_gate_passes_for_tracked_files(workflow)


def test_codeql_and_osv_remain_required_security_gates():
    codeql = Path(".github/workflows/codeql.yml").read_text()
    security = Path(".github/workflows/security.yml").read_text()
    assert "github/codeql-action/init@v4" in codeql
    assert "github/codeql-action/analyze@v4" in codeql
    assert "google/osv-scanner-action/.github/workflows/osv-scanner-reusable.yml@v2.5.0" in security
    assert "continue-on-error" not in codeql + security
    assert "if: false" not in codeql + security
    assert "if: ${{ false }}" not in codeql + security


def test_release_checklist_requires_evidence_bound_signoff():
    checklist = Path("RELEASE_CHECKLIST.md").read_text()
    assert "Evidence-bound launch signoff" in checklist
    assert "PENDING EVIDENCE" in checklist
    assert "hosted URL required for CodeQL/OSV" in checklist
    for field in (
        "Source revision",
        "Evidence URL/command",
        "Result",
        "Limitation",
        "Reviewer/date",
    ):
        assert field in checklist
    assert "| CodeQL |" in checklist
    assert "| OSV |" in checklist
    assert "| CodeQL/OSV |" not in checklist


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


def _credential_gate_passes_for_tracked_files(workflow: str) -> bool:
    """Verify the checked-in gate accepts only the repository's approved paths."""
    command = next(
        line.strip()
        for line in workflow.splitlines()
        if line.strip().startswith("! git ls-files |")
    )
    result = subprocess.run(
        ["bash", "-o", "pipefail", "-c", command], text=True, capture_output=True, check=False
    )
    return result.returncode == 0
