from pathlib import Path


def test_security_workflow_has_non_advisory_audits_and_secret_hygiene_gate():
    workflow = Path(".github/workflows/security.yml").read_text()
    assert "uv run pip-audit --local" in workflow
    assert "uv run bandit -r verdict -ll" in workflow
    assert "git ls-files" in workflow
    assert "|| true" not in workflow


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
