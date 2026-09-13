#!/usr/bin/env python3
"""Local Prime workflow contracts. No model, memory, or issue tracker authority."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from prime_state import (  # noqa: E402
    GATES,
    STATES,
    next_action_from_checkpoint,
    require,
    require_lease_for_pr,
    supersede_stale_leases,
)


def select_ready(
    issues: list[dict[str, Any]], dependencies: dict[str, bool]
) -> dict[str, Any] | None:
    """Input is a freshly paginated, scoped Linear snapshot; unknown deps block."""
    ready = [
        i
        for i in issues
        if i.get("ready") is True
        and isinstance(i.get("dependencies"), list)
        and all(dependencies.get(d) is True for d in i["dependencies"])
    ]
    for issue in ready:
        require(
            type(issue.get("priority")) is int and 0 <= issue["priority"] <= 4,
            "invalid Linear priority",
        )
        require(isinstance(issue.get("id"), str), "missing issue id")
    return min(ready, key=lambda i: (i["priority"] or 5, i["id"]), default=None)


def validate_proof(proof: dict[str, Any], head: str) -> None:
    require(proof.get("schema_version") == 1, "proof schema_version must be 1")
    require(re.fullmatch(r"[0-9a-f]{40,64}", head), "invalid head SHA")
    require(proof.get("head_sha") == head, "proof head differs from current head")
    require(proof.get("issue"), "missing issue")
    gates = proof.get("gates", {})
    require(isinstance(gates, dict) and set(gates) == GATES, "all four gates required")
    for name, gate in gates.items():
        require(isinstance(gate, dict), f"{name}: invalid gate")
        if gate.get("status") == "N/A":
            require(
                name != "ACCEPTANCE-PROOF" and gate.get("reason") and gate.get("approved_by"),
                f"{name}: N/A requires explicit applicability justification",
            )
        else:
            require(
                gate.get("status") == "PASS"
                and type(gate.get("exit_code")) is int
                and gate["exit_code"] == 0
                and gate.get("command")
                and gate.get("artifact")
                and re.fullmatch(r"[0-9a-f]{64}", str(gate.get("sha256", ""))),
                f"{name}: passing command and evidence digest required",
            )
    criteria = proof.get("acceptance_criteria", [])
    require(
        isinstance(criteria, list)
        and criteria
        and all(isinstance(c, str) and c for c in criteria)
        and len(set(criteria)) == len(criteria),
        "nonempty unique acceptance criteria required",
    )
    acceptance = proof.get("acceptance", {})
    require(
        isinstance(acceptance, dict) and set(acceptance) == set(criteria),
        "every acceptance criterion requires evidence",
    )
    for refs in acceptance.values():
        require(
            isinstance(refs, list) and refs and all(isinstance(r, str) and r for r in refs),
            "empty acceptance evidence",
        )


def validate_review(review: dict[str, Any], head: str) -> None:
    require(
        review.get("head_sha") == head
        and review.get("decision") == "APPROVED"
        and review.get("reviewer")
        and review.get("artifact"),
        "review must approve current head with reviewer and artifact",
    )


def validate_packet(packet: dict[str, Any]) -> None:
    require(packet.get("schema_version") == 1, "packet schema required")
    for key in (
        "issue",
        "issue_url",
        "project",
        "team",
        "issue_updated_at",
        "objective",
        "worktree",
        "branch",
        "base_sha",
        "ownership",
        "lease",
        "provider",
        "model",
        "routing_evidence",
        "available_at",
        "attempt_id",
        "spec",
        "plan",
        "tasks",
        "return_evidence",
    ):
        require(packet.get(key), f"packet missing {key}")
    require(Path(packet["worktree"]).is_absolute(), "worktree must be absolute")
    require(re.fullmatch(r"[0-9a-f]{40,64}", packet["base_sha"]), "invalid base SHA")
    require(
        not any(
            part in {"auto", "default", "best", "best-coding"}
            for part in packet["model"].split("/")
        ),
        "exact model required",
    )
    for key in ("acceptance_criteria", "allowed_files", "constraints"):
        require(isinstance(packet.get(key), list) and packet[key], f"packet missing {key}")
    require(isinstance(packet.get("context"), dict), "context provenance required")
    lease = packet["lease"]
    require(isinstance(lease, dict), "lease metadata required")
    for key in (
        "dispatch_id",
        "worker_id",
        "generation",
        "stale_after_seconds",
        "heartbeat_expectation",
    ):
        require(key in lease and lease[key] not in (None, ""), f"lease missing {key}")
    require(lease["dispatch_id"] == packet["attempt_id"], "lease dispatch_id differs from attempt")
    require(
        type(lease["generation"]) is int and lease["generation"] > 0, "invalid lease generation"
    )
    require(
        type(lease["stale_after_seconds"]) in (int, float) and lease["stale_after_seconds"] > 0,
        "invalid lease staleness bound",
    )
    criteria = packet["acceptance_criteria"]
    require(
        all(
            isinstance(c, dict) and c.get("id") and c.get("text") and c.get("verification")
            for c in criteria
        ),
        "criteria need id/text/verification",
    )
    require(len({c["id"] for c in criteria}) == len(criteria), "duplicate criteria")
    deps = packet.get("dependencies", [])
    require(
        "dependencies" in packet and isinstance(deps, list), "explicit dependency list required"
    )
    require(
        all(
            isinstance(d, dict)
            and d.get("status") == "SATISFIED"
            and d.get("issue")
            and d.get("evidence")
            and d.get("observed_at")
            for d in deps
        ),
        "unverified dependency",
    )
    requirements = packet.get("proof_requirements", {})
    require(
        isinstance(requirements, dict) and set(requirements) == GATES,
        "four proof requirements required",
    )
    for name, value in requirements.items():
        require(isinstance(value, dict), "invalid proof requirement")
        require(
            (value.get("required") is True and value.get("command"))
            or (
                name != "ACCEPTANCE-PROOF"
                and value.get("required") is False
                and value.get("reason")
                and value.get("approved_by")
            ),
            "proof applicability required",
        )
    budgets = packet.get("budgets", {})
    require(
        isinstance(budgets, dict)
        and all(
            type(budgets.get(k)) is int and budgets[k] > 0
            for k in ("wall_seconds", "idle_seconds", "ci_seconds")
        ),
        "positive budgets required",
    )


def validate_packet_proof(packet: dict[str, Any], proof: dict[str, Any]) -> None:
    require(proof.get("issue") == packet.get("issue"), "proof issue mismatch")
    require(
        set(proof.get("acceptance_criteria", []))
        == {c["id"] for c in packet.get("acceptance_criteria", [])},
        "proof differs from packet criteria",
    )
    for name, requirement in packet["proof_requirements"].items():
        gate = proof["gates"][name]
        if requirement["required"]:
            require(
                gate["status"] == "PASS" and gate["command"] == requirement["command"],
                f"{name}: required command not proven",
            )
        elif gate["status"] == "N/A":
            require(
                gate.get("reason") == requirement["reason"]
                and gate.get("approved_by") == requirement["approved_by"],
                "N/A differs from packet",
            )


RECEIPT_PROGRESS_FIELDS = (
    "dispatch_id",
    "issue_id",
    "worker_id",
    "status",
    "started_at",
    "last_progress_at",
    "current_step",
    "objective",
    "files_changed",
    "git_head",
    "commands_run",
    "tests_run",
    "proof_collected",
    "blockers",
    "next_action",
    "needs_rehydration",
    "needs_escalation",
    "lease_generation",
)


def validate_receipt(receipt: dict[str, Any], packet: dict[str, Any]) -> None:
    """Validate the machine-readable worker receipt contract (progress + terminal fields)."""
    require(receipt.get("schema_version") == 1, "terminal receipt schema required")
    for key in ("issue", "attempt_id", "worktree", "base_sha", "provider", "model"):
        require(packet.get(key) and receipt.get(key) == packet[key], f"receipt mismatch: {key}")
    for key in RECEIPT_PROGRESS_FIELDS:
        require(key in receipt, f"receipt missing {key}")
    require(
        receipt.get("dispatch_id") == packet.get("attempt_id"),
        "receipt dispatch_id must match the hydrated packet attempt_id",
    )
    require(
        receipt.get("lease_generation") == packet.get("lease", {}).get("generation"),
        "receipt lease_generation must match the packet lease",
    )
    require(receipt.get("issue_id") == packet.get("issue"), "receipt issue_id mismatch")
    require(receipt.get("status") in {"COMPLETE", "BLOCKED", "FAILED"}, "terminal status required")
    require(
        isinstance(receipt.get("changed_files"), list)
        and isinstance(receipt.get("files_changed"), list)
        and isinstance(receipt.get("commands"), list)
        and isinstance(receipt.get("commands_run"), list)
        and isinstance(receipt.get("tests_run"), list)
        and isinstance(receipt.get("proof_collected"), list)
        and isinstance(receipt.get("blockers"), list),
        "receipt lists required",
    )
    require(
        isinstance(receipt.get("needs_rehydration"), bool)
        and isinstance(receipt.get("needs_escalation"), bool),
        "receipt escalation flags must be explicit booleans",
    )
    require(
        isinstance(receipt.get("started_at"), str) and receipt["started_at"],
        "receipt started_at required",
    )
    require(
        isinstance(receipt.get("last_progress_at"), str) and receipt["last_progress_at"],
        "receipt last_progress_at required",
    )
    try:
        started_at = datetime.datetime.fromisoformat(
            str(receipt["started_at"]).replace("Z", "+00:00")
        )
        last_progress_at = datetime.datetime.fromisoformat(
            str(receipt["last_progress_at"]).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ValueError("receipt progress timestamps must be ISO-8601") from exc
    require(
        last_progress_at >= started_at,
        "receipt last_progress_at must not precede started_at (no progress recorded)",
    )
    for field in ("current_step", "objective", "next_action"):
        require(isinstance(receipt.get(field), str) and receipt[field], f"receipt {field} required")
    require(
        receipt.get("head_sha") and receipt.get("git_head") and receipt.get("summary"),
        "receipt source and summary required",
    )
    require(receipt["head_sha"] == receipt["git_head"], "receipt git_head differs from head_sha")
    unknown_proof = set(receipt.get("proof_collected", [])) - GATES
    require(not unknown_proof, f"unknown proof category: {sorted(unknown_proof)}")
    if receipt["status"] == "COMPLETE":
        require(receipt.get("proof_path"), "complete receipt needs proof_path")


def transition(current: str, target: str, evidence: dict[str, Any]) -> str:
    require(current in STATES and target in STATES, "unknown state")
    require(STATES.index(target) == STATES.index(current) + 1, "cannot skip lifecycle states")
    needed = {
        "HYDRATED": "packet_valid",
        "IMPLEMENTING": "worker_admitted",
        "LOCAL_VALIDATION": "receipt_valid",
        "PROOF_COMPLETE": "proof_valid",
        "REVIEW_APPROVED": "review_valid",
        "PR_OPEN": "pr_verified",
        "CI_GREEN": "ci_verified",
        "MERGED": "merge_verified",
        "MAIN_VERIFIED": "main_verified",
        "DONE": "linear_updated",
    }
    require(evidence.get(needed[target]) is True, f"missing {needed[target]}")
    return target


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def evidence_file(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    require(
        path.is_relative_to(root.resolve()) and path.is_file(), "missing or escaped evidence file"
    )
    return path


def default_state_dir(repo: Path) -> Path:
    """Durable state lives in the git common dir, shared by every issue worktree."""
    common = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        text=True,
    ).strip()
    return Path(common) / "verdict-prime"


def check_bundle(
    repo: Path, proof_path: Path, review_path: Path
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    require(not git(repo, "status", "--porcelain"), "worktree must be clean before PR")
    head = git(repo, "rev-parse", "HEAD")
    proof = json.loads(proof_path.read_text())
    review = json.loads(review_path.read_text())
    packet = json.loads((proof_path.parent / "packet.json").read_text())
    validate_packet(packet)
    require(Path(packet["worktree"]).resolve() == repo.resolve(), "packet worktree mismatch")
    validate_proof(proof, head)
    validate_packet_proof(packet, proof)
    validate_review(review, head)
    root = proof_path.parent
    hashed_artifacts = set()
    for gate in proof["gates"].values():
        if gate["status"] == "PASS":
            digest = hashlib.sha256(evidence_file(root, gate["artifact"]).read_bytes()).hexdigest()
            require(digest == gate["sha256"], "evidence digest mismatch")
            hashed_artifacts.add(gate["artifact"])
    for refs in proof["acceptance"].values():
        for ref in refs:
            require(ref in hashed_artifacts, "acceptance evidence must have a gate digest")
            evidence_file(root, ref)
    evidence_file(review_path.parent, review["artifact"])
    return proof, head, packet


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check-proof", "open-pr"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--repo", type=Path, default=Path.cwd())
        cmd.add_argument("--proof", type=Path, required=True)
        cmd.add_argument("--review", type=Path, required=True)
        cmd.add_argument("--state-dir", type=Path, default=None)
        if name == "open-pr":
            cmd.add_argument("--title", required=True)
            cmd.add_argument("--body-file", type=Path, required=True)
            cmd.add_argument("--base", default="main")
    lease = sub.add_parser("lease-reap", help="Fence leases whose writer stopped progressing")
    lease.add_argument("--state-dir", type=Path, required=True)
    status = sub.add_parser("status", help="Print supervisor, checkpoint, lease and proof state")
    status.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "lease-reap":
            reaped = supersede_stale_leases(args.state_dir, now=time.time())
            print(json.dumps({"reaped_stale_leases": reaped}))
            return 0
        if args.command == "status":
            state_dir: Path = args.state_dir
            payload: dict[str, Any] = {"state_dir": str(state_dir)}
            supervisor_path = state_dir / "supervisor.json"
            payload["supervisor"] = (
                json.loads(supervisor_path.read_text()) if supervisor_path.is_file() else None
            )
            try:
                payload["checkpoint"] = next_action_from_checkpoint(state_dir)
            except ValueError as exc:
                payload["checkpoint"] = f"unavailable: {exc}"
            leases_root = state_dir / "leases"
            payload["leases"] = (
                [json.loads(path.read_text()) for path in sorted(leases_root.glob("*/active.json"))]
                if leases_root.is_dir()
                else []
            )
            issues_root = state_dir / "issues"
            payload["issues"] = (
                sorted(p.name for p in issues_root.iterdir() if p.is_dir())
                if issues_root.is_dir()
                else []
            )
            print(json.dumps(payload, indent=2, default=str))
            return 0
        state_dir = args.state_dir if args.state_dir is not None else default_state_dir(args.repo)
        proof, head, packet = check_bundle(args.repo, args.proof, args.review)
        if args.command == "open-pr":
            # Fencing gate: the lease holder that hydrated this packet must still own the issue.
            require_lease_for_pr(state_dir, packet)
            branch = git(args.repo, "branch", "--show-current")
            require(branch and branch != args.base, "PR requires issue branch")
            remote_head = git(args.repo, "ls-remote", "origin", f"refs/heads/{branch}").split()
            require(remote_head and remote_head[0] == head, "push verified head before opening PR")
            prs = subprocess.check_output(
                ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "url"],
                cwd=args.repo,
                text=True,
            )
            existing = json.loads(prs)
            if existing:
                print(existing[0]["url"])
                return 0
            require(
                git(args.repo, "rev-parse", "HEAD") == head
                and not git(args.repo, "status", "--porcelain"),
                "source changed during proof gate",
            )
            subprocess.run(
                [
                    "gh",
                    "pr",
                    "create",
                    "--head",
                    branch,
                    "--base",
                    args.base,
                    "--title",
                    args.title,
                    "--body-file",
                    str(args.body_file.resolve()),
                ],
                cwd=args.repo,
                check=True,
            )
            print(json.dumps({"pr": "created", "head_sha": head}))
        else:
            print(
                json.dumps(
                    {
                        "issue": proof["issue"],
                        "head_sha": head,
                        "proof": "PASS",
                        "review": "APPROVED",
                    }
                )
            )
        return 0
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"BLOCKED: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
