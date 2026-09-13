import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_cold_resume_status_and_full_lifecycle(tmp_path):
    state = load("prime_state_integration", "scripts/prime_state.py")
    workflow = load("prime_workflow_integration", "scripts/prime_workflow.py")
    checkpoint = state.checkpoint_skeleton(
        issue="TEST-COLD-RESUME",
        state="READY",
        project="Verdict",
        team="Bodanglin",
        worktree=str(tmp_path / "worktree"),
        branch="test/cold-resume",
        next_action="hydrate-context",
    )
    state.write_checkpoint(tmp_path, checkpoint)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/prime_workflow.py"),
            "status",
            "--state-dir",
            str(tmp_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    status = json.loads(result.stdout)
    assert status["checkpoint"]["issue"] == "TEST-COLD-RESUME"
    assert status["checkpoint"]["state"] == "READY"
    assert status["checkpoint"]["next_action"] == "hydrate-context"

    evidence = {}
    current = "READY"
    for target, key in zip(
        state.STATES[1:],
        (
            "packet_valid",
            "worker_admitted",
            "receipt_valid",
            "proof_valid",
            "review_valid",
            "pr_verified",
            "ci_verified",
            "merge_verified",
            "main_verified",
            "linear_updated",
        ),
        strict=True,
    ):
        evidence[key] = True
        current = workflow.transition(current, target, evidence)
    assert current == "DONE"
    state.write_checkpoint(
        tmp_path,
        {
            **checkpoint,
            "state": "DONE",
            "next_action": "verdict-resume",
            "completed_issues": [checkpoint["issue"]],
        },
    )
    assert state.next_action_from_checkpoint(tmp_path)["state"] == "DONE"


def test_healthy_worker_receipt_and_lease_heartbeat(tmp_path):
    state = load("prime_state_healthy", "scripts/prime_state.py")
    workflow = load("prime_workflow_healthy", "scripts/prime_workflow.py")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    packet = {
        "issue": "TEST-HEALTHY-WORKER",
        "attempt_id": "dispatch-1",
        "worktree": str(worktree),
        "base_sha": "a" * 40,
        "provider": "omniroute-live",
        "model": "antigravity/gemini-3.7-flash-low",
        "lease": {"generation": 1},
    }
    state.acquire_lease(
        tmp_path,
        issue=packet["issue"],
        dispatch_id=packet["attempt_id"],
        worker_id="worker-1",
        worktree=str(worktree),
        branch="test/healthy-worker",
        stale_after_seconds=600,
        supervisor_id="integration-test",
        now=0.0,
    )
    receipt_path = tmp_path / "receipt.json"
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "p = Path(sys.argv[1])\n"
        "p.write_text(json.dumps({\n"
        "  'schema_version': 1, 'issue': 'TEST-HEALTHY-WORKER', 'attempt_id': 'dispatch-1',\n"
        "  'dispatch_id': 'dispatch-1', 'issue_id': 'TEST-HEALTHY-WORKER', 'worker_id': 'worker-1',\n"
        "  'status': 'COMPLETE', 'started_at': '2026-09-13T00:00:00Z',\n"
        "  'last_progress_at': '2026-09-13T00:01:00Z', 'current_step': 'LOCAL_VALIDATION',\n"
        "  'objective': 'prove receipt', 'files_changed': [], 'git_head': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',\n"
        "  'commands_run': [], 'tests_run': [], 'proof_collected': ['UNIT'], 'blockers': [],\n"
        "  'next_action': 'verdict-proof', 'needs_rehydration': False, 'needs_escalation': False, 'lease_generation': 1,\n"
        "  'worktree': sys.argv[2], 'base_sha': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',\n"
        "  'provider': 'omniroute-live', 'model': 'antigravity/gemini-3.7-flash-low',\n"
        "  'changed_files': [], 'commands': [], 'proof_path': 'proof.json', 'summary': 'complete',\n"
        "  'head_sha': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'\n"
        "}))\n"
    )
    result = subprocess.run(
        [sys.executable, str(worker), str(receipt_path), str(worktree)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    workflow.validate_receipt(json.loads(receipt_path.read_text()), packet)
    state.heartbeat_lease(tmp_path, packet["issue"], generation=1, progress=True, now=10.0)
    assert state.read_lease(tmp_path, packet["issue"])["last_progress_at"] == 10.0
