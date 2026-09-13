import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def module():
    path = ROOT / "scripts/prime_workflow.py"
    assert path.exists(), "workflow validator missing"
    spec = importlib.util.spec_from_file_location("prime_workflow", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def proof():
    return {
        "schema_version": 1,
        "issue": "BOD-12",
        "head_sha": "a" * 40,
        "acceptance_criteria": ["AC1"],
        "gates": {
            name: {
                "status": "PASS",
                "command": "pytest",
                "exit_code": 0,
                "artifact": "test.log",
                "sha256": "e" * 64,
            }
            for name in ("STATIC", "UNIT", "INTEGRATION", "ACCEPTANCE-PROOF")
        },
        "acceptance": {"AC1": ["test.log"]},
    }


def receipt():
    return {
        "schema_version": 1,
        "issue": "BOD-12",
        "dispatch_id": "attempt-1",
        "issue_id": "BOD-12",
        "worker_id": "worker-1",
        "status": "COMPLETE",
        "started_at": "2026-09-13T00:00:00Z",
        "last_progress_at": "2026-09-13T00:05:00Z",
        "current_step": "LOCAL_VALIDATION",
        "objective": "Ship parity",
        "files_changed": ["contracts/src/index.ts"],
        "git_head": "a" * 40,
        "commands_run": [
            {"command": "pytest", "cwd": "/wt", "exit_code": 0, "artifact": "evidence/unit.log"}
        ],
        "tests_run": [{"name": "pytest", "passed": 30, "failed": 0}],
        "proof_collected": ["UNIT"],
        "blockers": [],
        "next_action": "verdict-proof",
        "needs_rehydration": False,
        "needs_escalation": False,
        "lease_generation": 1,
        "worktree": "/wt",
        "base_sha": "a" * 40,
        "provider": "omniroute-live",
        "model": "gc/grok-4.6",
        "attempt_id": "attempt-1",
        "changed_files": ["contracts/src/index.ts"],
        "commands": [
            {"command": "pytest", "cwd": "/wt", "exit_code": 0, "artifact": "evidence/unit.log"}
        ],
        "proof_path": "proof.json",
        "summary": "done",
        "head_sha": "a" * 40,
    }


def test_discovery():
    names = {
        "verdict-resume",
        "hydrate-context",
        "verdict-dispatch",
        "verdict-proof",
        "verdict-finish",
    }
    root = ROOT / ".prime/agent/skills"
    assert {p.parent.name for p in root.glob("*/SKILL.md")} == names
    for name in names:
        text = (root / name / "SKILL.md").read_text()
        assert text.startswith(f"---\nname: {name}\n")
        assert "description: Use when" in text


def test_ready_selection_fails_closed_and_zero_priority_is_last():
    issues = [
        {"id": "BOD-1", "priority": 1, "ready": True, "dependencies": ["unknown"]},
        {"id": "BOD-2", "priority": 0, "ready": True, "dependencies": []},
        {"id": "BOD-3", "priority": 2, "ready": True, "dependencies": ["done"]},
    ]
    assert module().select_ready(issues, {"done": True})["id"] == "BOD-3"
    assert module().select_ready(issues[:1], {}) is None


def test_complete_proof_and_stale_head():
    m = module()
    m.validate_proof(proof(), "a" * 40)
    with pytest.raises(ValueError, match="head"):
        m.validate_proof(proof(), "b" * 40)


@pytest.mark.parametrize("mutation", ["missing", "na", "exit", "acceptance", "empty"])
def test_incomplete_proof_rejected(mutation):
    p = proof()
    if mutation == "missing":
        del p["gates"]["INTEGRATION"]
    if mutation == "na":
        p["gates"]["INTEGRATION"] = {"status": "N/A"}
    if mutation == "exit":
        p["gates"]["UNIT"]["exit_code"] = 1
    if mutation == "acceptance":
        p["acceptance"] = {}
    if mutation == "empty":
        p["acceptance_criteria"] = []
    with pytest.raises(ValueError):
        module().validate_proof(p, "a" * 40)


def test_explicit_not_applicable_is_allowed():
    p = proof()
    p["gates"]["INTEGRATION"] = {
        "status": "N/A",
        "reason": "Documentation only",
        "approved_by": "hydration-policy",
    }
    module().validate_proof(p, "a" * 40)


def test_review_bound_to_exact_head():
    m = module()
    with pytest.raises(ValueError):
        m.validate_review(
            {"head_sha": "b" * 40, "decision": "APPROVED", "reviewer": "reviewer"}, "a" * 40
        )


def test_transition_cannot_skip_or_finish_without_main():
    m = module()
    with pytest.raises(ValueError):
        m.transition("LOCAL_VALIDATION", "PR_OPEN", {})
    with pytest.raises(ValueError):
        m.transition("MERGED", "MAIN_VERIFIED", {})
    assert m.transition("MERGED", "MAIN_VERIFIED", {"main_verified": True}) == "MAIN_VERIFIED"


def test_receipt_cannot_substitute_spawn_handle():
    with pytest.raises(ValueError):
        module().validate_receipt({"rlm_child_id": "child"}, {"attempt_id": "x"})


def test_packet_rejects_unknown_dependency_and_auto_target():
    m = module()
    assert hasattr(m, "validate_packet"), "packet validator missing"
    with pytest.raises(ValueError):
        m.validate_packet({"schema_version": 1, "model": "auto/best-coding"})


def test_proof_must_cover_original_packet_criteria():
    m = module()
    assert hasattr(m, "validate_packet_proof"), "packet-bound proof validation missing"
    packet = {"issue": "BOD-12", "acceptance_criteria": [{"id": "AC1"}, {"id": "AC2"}]}
    with pytest.raises(ValueError, match="criteria"):
        m.validate_packet_proof(packet, proof())


def packet(worktree):
    return {
        "schema_version": 1,
        "issue": "BOD-12",
        "issue_url": "https://linear.app/issue/BOD-12",
        "project": "Verdict",
        "team": "BOD",
        "issue_updated_at": "2026-09-13T00:00:00Z",
        "objective": "Verify workflow discovery",
        "acceptance_criteria": [{"id": "AC1", "text": "Discovered", "verification": "loader test"}],
        "dependencies": [],
        "context": {"refs": ["AGENTS.md"], "omissions": []},
        "spec": "spec.md",
        "plan": "plan.md",
        "tasks": "tasks.md",
        "worktree": str(worktree),
        "branch": "issue",
        "base_sha": "a" * 40,
        "ownership": "test-owner",
        "lease": {
            "dispatch_id": "attempt-1",
            "worker_id": "worker-1",
            "generation": 1,
            "stale_after_seconds": 600,
            "heartbeat_expectation": "progress-only",
        },
        "constraints": ["No unrelated edits"],
        "allowed_files": ["README"],
        "provider": "omniroute-live",
        "model": "gc/grok-4.6",
        "routing_evidence": "route.json",
        "available_at": "2026-09-13T00:00:00Z",
        "attempt_id": "attempt-1",
        "budgets": {"wall_seconds": 3600, "idle_seconds": 600, "ci_seconds": 1200},
        "return_evidence": "receipt.json",
        "proof_requirements": {
            g: {"required": True, "command": "pytest"}
            for g in ("STATIC", "UNIT", "INTEGRATION", "ACCEPTANCE-PROOF")
        },
    }


@pytest.mark.parametrize("bad", ["dependency", "alias", "criteria", "budget"])
def test_complete_packet_rejects_invalid_field(tmp_path, bad):
    m = module()
    p = packet(tmp_path)
    m.validate_packet(p)
    if bad == "dependency":
        p["dependencies"] = [{"issue": "BOD-1", "status": "UNKNOWN"}]
    elif bad == "alias":
        p["model"] = "auto/best-coding"
    elif bad == "criteria":
        p["acceptance_criteria"][0].pop("verification")
    else:
        p["budgets"]["idle_seconds"] = 0
    with pytest.raises(ValueError):
        m.validate_packet(p)


def test_real_bundle_rejects_modified_evidence_and_dirty_source(tmp_path):
    m = module()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "README").write_text("test\n")
    subprocess.run(["git", "-C", str(repo), "add", "README"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "baseline",
        ],
        check=True,
    )
    head = m.git(repo, "rev-parse", "HEAD")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "test.log").write_text("observed test evidence\n")
    (evidence / "review.log").write_text("independent review\n")
    p = proof()
    p["head_sha"] = head
    for gate in p["gates"].values():
        gate["sha256"] = hashlib.sha256((evidence / "test.log").read_bytes()).hexdigest()
    (evidence / "proof.json").write_text(json.dumps(p))
    (evidence / "packet.json").write_text(json.dumps(packet(repo)))
    (evidence / "review.json").write_text(
        json.dumps(
            {
                "head_sha": head,
                "decision": "APPROVED",
                "reviewer": "independent",
                "artifact": "review.log",
            }
        )
    )
    m.check_bundle(repo, evidence / "proof.json", evidence / "review.json")
    (evidence / "test.log").write_text("tampered\n")
    with pytest.raises(ValueError, match="digest"):
        m.check_bundle(repo, evidence / "proof.json", evidence / "review.json")
    (repo / "README").write_text("changed\n")
    with pytest.raises(ValueError, match="clean"):
        m.check_bundle(repo, evidence / "proof.json", evidence / "review.json")


def test_open_pr_cli_is_blocked_by_incomplete_proof(tmp_path, monkeypatch):
    import sys as _sys

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "README").write_text("x\n")
    subprocess.run(["git", "-C", str(repo), "add", "README"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@e.invalid",
            "commit",
            "-qm",
            "base",
        ],
        check=True,
    )
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    ev = tmp_path / "state"
    ev.mkdir()
    p = proof()
    p["head_sha"] = head
    p["gates"]["UNIT"]["exit_code"] = 1  # failing required proof
    (ev / "test.log").write_text("failed\n")
    (ev / "proof.json").write_text(json.dumps(p))
    (ev / "packet.json").write_text(json.dumps(packet(repo)))
    (ev / "review.json").write_text(
        json.dumps(
            {"head_sha": head, "decision": "APPROVED", "reviewer": "r", "artifact": "review.log"}
        )
    )
    (ev / "review.log").write_text("ok\n")
    marker = tmp_path / "gh-called"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gh").write_text(
        f"#!{_sys.executable}\nopen({str(marker)!r}, 'w').write('called')\n"
    )
    (fake_bin / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    result = subprocess.run(
        [
            _sys.executable,
            str(ROOT / "scripts/prime_workflow.py"),
            "open-pr",
            "--repo",
            str(repo),
            "--proof",
            str(ev / "proof.json"),
            "--review",
            str(ev / "review.json"),
            "--title",
            "t",
            "--body-file",
            str(ev / "review.log"),
            "--state-dir",
            str(ev),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "BLOCKED" in result.stdout
    assert not marker.exists(), "gh must not be invoked when proof is incomplete"


@pytest.mark.parametrize(
    "field", ["started_at", "last_progress_at", "current_step", "objective", "lease_generation"]
)
def test_receipt_rejects_malformed_progress_fields(field):
    r = receipt()
    if field in {"started_at", "last_progress_at"}:
        r[field] = "not-an-iso-timestamp"
    elif field == "lease_generation":
        r[field] = 99
    else:
        r[field] = ""
    with pytest.raises(ValueError):
        module().validate_receipt(r, packet("/wt"))
