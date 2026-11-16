"""Unit tests for verdict/operator_policy: the approved operator policy authority.

Covered: source loading and digest verification, fail-closed on missing/
corrupt/stale/unapproved policy, rule matching and precedence, override token
validation (expiry, nonce, replay, digest, operator/action/scope), and
secret-free audit records. No network, no memory-source authority, no secrets.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from verdict.operator_policy import (
    POLICY_SCHEMA_VERSION,
    ActionContext,
    Decision,
    OverrideRejectedError,
    PolicySource,
    PolicyUnavailableError,
    audit_record,
    check_override_token,
    evaluate_action,
    load_policy_source,
)
from verdict.orchestration.run import run_golden_path

POLICY_DIR = Path(__file__).resolve().parent.parent / "verdict" / "operator_policy" / "v1"


class NonceStore:
    def __init__(self) -> None:
        self.used_nonces: set[str] = set()
        self.recorded: list[str] = []

    def used(self, nonce: str) -> bool:
        return nonce in self.used_nonces

    def record(self, nonce: str) -> None:
        self.used_nonces.add(nonce)
        self.recorded.append(nonce)


def _context(**overrides: object) -> ActionContext:
    data = dict(
        action="doctor.diagnose",
        argv=(),
        env={},
        cwd="/repo",
        worktree="/repo",
        actor=None,
        linear_issue=None,
        acceptance_criteria=(),
        brief_refs=(),
        ownership_refs=(),
        safety_refs=(),
        proof_refs=(),
        runtime_proof=None,
    )
    data.update(overrides)
    return ActionContext(**data)  # type: ignore[arg-type]


def _token(now: datetime, **overrides: object) -> dict[str, object]:
    base = dict(
        operator="nick@nickcarter.dev",
        action="source.edit.maintenance",
        scope="governing-artifacts",
        expiry=(now + timedelta(hours=1)).isoformat(),
        nonce="nonce-once",
        policy_digest="",
    )
    base.update(overrides)
    return base  # type: ignore[return-value]


def test_load_shipped_source_matches_manifest() -> None:
    source = load_policy_source(POLICY_DIR)
    assert isinstance(source, PolicySource)
    assert source.version == 1
    assert source.digest.startswith("sha256:")
    rule_ids = {rule.rule_id for rule in source.rules}
    assert "pol.block-push-no-verify" in rule_ids
    assert "pol.require-tracked-issue-ac" in rule_ids
    assert "pol.block-vgate-mutation" in rule_ids
    assert "pol.require-cited-brief-dispatch" in rule_ids
    # Digest covers rules.json bytes; the manifest digest must equal it.
    rules_bytes = (POLICY_DIR / "rules.json").read_bytes()

    expected = "sha256:" + hashlib.sha256(rules_bytes).hexdigest()
    assert source.digest == expected


def test_missing_source_dir_fails_closed() -> None:
    with pytest.raises(PolicyUnavailableError, match="missing"):
        load_policy_source(Path("/no/such/policy/dir"))


def test_tampered_rules_fail_closed(tmp_path: Path) -> None:
    tampered = tmp_path / "tampered"
    tampered.mkdir()
    import shutil

    shutil.copy(POLICY_DIR / "manifest.json", tampered / "manifest.json")
    rules = (POLICY_DIR / "rules.json").read_bytes()
    rules = rules.replace(b"pol.block-push-no-verify", b"pol.tampered-rule")
    (tampered / "rules.json").write_bytes(rules)
    with pytest.raises(PolicyUnavailableError, match="digest mismatch"):
        load_policy_source(tampered)


def test_malformed_rule_fails_closed(tmp_path: Path) -> None:
    tampered = tmp_path / "malformed"
    tampered.mkdir()
    manifest = json.loads((POLICY_DIR / "manifest.json").read_text())
    rules = json.loads((POLICY_DIR / "rules.json").read_text())
    rules["rules"][0]["effect"] = "not-an-effect"
    (tampered / "manifest.json").write_text(json.dumps(manifest))
    (tampered / "rules.json").write_text(json.dumps(rules))
    with pytest.raises(PolicyUnavailableError):
        load_policy_source(tampered)


def test_benign_read_only_allowed() -> None:
    source = load_policy_source(POLICY_DIR)
    verdict = evaluate_action(_context(action="doctor.diagnose"), source)
    assert verdict.decision is Decision.ALLOW
    assert verdict.rule_id == "pol.allow-benign-read-only"
    assert not verdict.override_required


def test_source_edit_requires_issue_and_ac() -> None:
    source = load_policy_source(POLICY_DIR)
    # No Linear issue or acceptance criteria: the rule does not match, default
    # deny applies.
    verdict = evaluate_action(
        _context(action="source.edit", argv=["edit", "/repo/verdict/x.py"]), source
    )
    assert verdict.decision is Decision.DENY
    assert not verdict.override_required
    # With issue + AC the deny rule matches; override is required (maintenance
    # disposition), not an automatic pass.
    verdict = evaluate_action(
        _context(
            action="source.edit",
            argv=["edit", "/repo/verdict/x.py"],
            linear_issue="BOD-340",
            acceptance_criteria=("AC1",),
        ),
        source,
    )
    assert verdict.decision is Decision.DENY
    assert verdict.rule_id == "pol.require-tracked-issue-ac"
    assert verdict.override_required
    assert verdict.reason.startswith("override_required:")


def test_git_push_no_verify_denied_no_override() -> None:
    source = load_policy_source(POLICY_DIR)
    verdict = evaluate_action(
        _context(action="git.push", argv=["git", "push", "--no-verify", "origin", "main"]), source
    )
    assert verdict.decision is Decision.DENY
    assert verdict.rule_id == "pol.block-push-no-verify"
    assert not verdict.override_required


def test_git_push_without_no_verify_is_allowed_through_gate() -> None:
    source = load_policy_source(POLICY_DIR)
    verdict = evaluate_action(
        _context(action="git.push", argv=["git", "push", "origin", "main"]), source
    )
    assert verdict.decision is Decision.ALLOW
    assert verdict.rule_id == "pol.allow-git-push-through-gate"


def test_vgate_mutation_denied() -> None:
    source = load_policy_source(POLICY_DIR)
    verdict = evaluate_action(_context(action="gate.mutate", argv=["vgate", "--force"]), source)
    assert verdict.decision is Decision.DENY
    assert verdict.rule_id == "pol.block-vgate-mutation"
    assert not verdict.override_required


def test_vgate_help_not_mutating() -> None:
    source = load_policy_source(POLICY_DIR)
    verdict = evaluate_action(_context(action="gate.mutate", argv=["vgate", "--help"]), source)
    assert verdict.decision is Decision.DENY  # no rule allows gate.mutate
    assert not verdict.override_required


def test_unauthorized_service_restart_denied() -> None:
    source = load_policy_source(POLICY_DIR)
    verdict = evaluate_action(
        _context(
            action="service.restart", argv=["systemctl", "--user", "restart", "verdict-serve"]
        ),
        source,
    )
    assert verdict.decision is Decision.DENY
    assert verdict.rule_id == "pol.block-unauthorized-service-restart"
    assert not verdict.override_required


def test_kill_running_work_denied() -> None:
    source = load_policy_source(POLICY_DIR)
    verdict = evaluate_action(
        _context(action="process.kill", argv=["kill", "-9", "owned-worker-123"]), source
    )
    assert verdict.decision is Decision.DENY
    assert verdict.rule_id == "pol.block-kill-running-work"


def test_subagent_dispatch_requires_cited_brief() -> None:
    source = load_policy_source(POLICY_DIR)
    verdict = evaluate_action(
        _context(action="subagent.dispatch", argv=["spawn", "worker"]), source
    )
    assert verdict.decision is Decision.DENY
    assert not verdict.override_required
    verdict = evaluate_action(
        _context(
            action="subagent.dispatch",
            argv=["spawn", "worker"],
            brief_refs=["brief-1"],
            ownership_refs=["owner-1"],
            safety_refs=["safety-1"],
            proof_refs=["proof-1"],
        ),
        source,
    )
    assert verdict.decision is Decision.DENY
    assert verdict.rule_id == "pol.require-cited-brief-dispatch"
    assert verdict.override_required


def test_unknown_action_denied() -> None:
    source = load_policy_source(POLICY_DIR)
    verdict = evaluate_action(_context(action="nuclear.launch"), source)
    assert verdict.decision is Decision.DENY
    assert verdict.rule_id is None
    assert verdict.reason == "no_rule_authorizes_action"


def test_override_token_valid_allows() -> None:
    source = load_policy_source(POLICY_DIR)
    now = datetime.now(timezone.utc)
    store = NonceStore()
    rule = next(r for r in source.rules if r.rule_id == "pol.require-tracked-issue-ac")
    assert rule.override is not None
    token = _token(now)
    token["policy_digest"] = source.digest
    check_override_token(token, source, rule, nonce_store=store, now=now)
    assert store.recorded == ["nonce-once"]


def test_override_token_replay_rejected() -> None:
    source = load_policy_source(POLICY_DIR)
    now = datetime.now(timezone.utc)
    store = NonceStore()
    rule = next(r for r in source.rules if r.rule_id == "pol.require-tracked-issue-ac")
    token = _token(now, policy_digest=source.digest)
    check_override_token(token, source, rule, nonce_store=store, now=now)
    assert store.used("nonce-once") is True
    assert "nonce-once" in store.recorded
    # Replaying the same nonce is rejected (single-use).
    with pytest.raises(OverrideRejectedError, match="replay_or_reused"):
        check_override_token(token, source, rule, nonce_store=store, now=now)
    # A fresh nonce on the same rule is still accepted.
    fresh = _token(now, policy_digest=source.digest, nonce="nonce-two")
    check_override_token(fresh, source, rule, nonce_store=store, now=now)
    assert store.used("nonce-two") is True


def test_override_token_expired_rejected() -> None:
    source = load_policy_source(POLICY_DIR)
    now = datetime.now(timezone.utc)
    store = NonceStore()
    rule = next(r for r in source.rules if r.rule_id == "pol.require-tracked-issue-ac")
    token = _token(now - timedelta(hours=2), policy_digest=source.digest)
    with pytest.raises(OverrideRejectedError, match="expired"):
        check_override_token(token, source, rule, nonce_store=store, now=now)


def test_override_token_wrong_digest_rejected() -> None:
    source = load_policy_source(POLICY_DIR)
    now = datetime.now(timezone.utc)
    store = NonceStore()
    rule = next(r for r in source.rules if r.rule_id == "pol.require-tracked-issue-ac")
    token = _token(now, policy_digest="sha256:" + "0" * 64)
    with pytest.raises(OverrideRejectedError, match="digest_mismatch"):
        check_override_token(token, source, rule, nonce_store=store, now=now)


def test_override_token_wrong_operator_rejected() -> None:
    source = load_policy_source(POLICY_DIR)
    now = datetime.now(timezone.utc)
    store = NonceStore()
    rule = next(r for r in source.rules if r.rule_id == "pol.require-tracked-issue-ac")
    token = _token(now, policy_digest=source.digest, operator="someone-else@example.invalid")
    with pytest.raises(OverrideRejectedError, match="operator_mismatch"):
        check_override_token(token, source, rule, nonce_store=store, now=now)


def test_override_token_wrong_action_rejected() -> None:
    source = load_policy_source(POLICY_DIR)
    now = datetime.now(timezone.utc)
    store = NonceStore()
    rule = next(r for r in source.rules if r.rule_id == "pol.require-tracked-issue-ac")
    token = _token(now, policy_digest=source.digest, action="nuclear.launch")
    with pytest.raises(OverrideRejectedError, match="action_mismatch"):
        check_override_token(token, source, rule, nonce_store=store, now=now)


def test_no_override_on_non_waivable_rule() -> None:
    source = load_policy_source(POLICY_DIR)
    rule = next(r for r in source.rules if r.rule_id == "pol.block-push-no-verify")
    assert rule.override is None
    assert not rule.waivable


def test_audit_record_is_secret_free() -> None:
    source = load_policy_source(POLICY_DIR)
    ctx = _context(
        action="git.push",
        argv=["git", "push", "https://github.com/owner/repo.git"],
        actor="nick@nickcarter.dev",
        worktree="/home/nick/dev/verdict-core",
    )
    verdict = evaluate_action(ctx, source)
    record = audit_record(verdict, ctx)
    text = json.dumps(record)
    assert "nick@nickcarter.dev" not in text
    assert "/home/nick" not in text
    assert "https://github.com/owner/repo.git" not in text
    assert record["decision_id"].startswith("sha256:")
    assert record["policy_digest"] == source.digest


def test_decision_is_deterministic() -> None:
    source = load_policy_source(POLICY_DIR)
    ctx = _context(action="file.read", argv=["cat", "README.md"])
    first = evaluate_action(ctx, source)
    second = evaluate_action(ctx, source)
    assert first.decision_id == second.decision_id
    assert first == second


def test_orchestrate_fails_closed_when_policy_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_orch_stages import _Classifier, _Executor, _repo, _Reviewer, _Selector
    from verdict.orchestration.contracts import NodeKind, WorkGraph, WorkNode

    bad = tmp_path / "bad-policy"
    bad.mkdir()
    (bad / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": POLICY_SCHEMA_VERSION,
                "version": 1,
                "digest": "sha256:" + "0" * 64,
                "title": "tampered",
            }
        )
    )
    (bad / "rules.json").write_text(json.dumps({"rules": []}))
    monkeypatch.setenv("VERDICT_OPERATOR_POLICY_DIR", str(bad))

    repo = _repo(tmp_path)
    runs_root = tmp_path / "runs"

    node = WorkNode(
        "a",
        "ship the widget",
        kind=NodeKind.IMPLEMENT,
        owned_files=("a.txt",),
        verification_command=("true",),
    )
    graph = WorkGraph("goal", (node,))

    result = asyncio.run(
        run_golden_path(
            "ship the widget",
            repo=repo,
            runs_root=runs_root,
            selector=_Selector(),
            executor=_Executor(),
            classifier=_Classifier(),
            reviewer=_Reviewer(),
            graph=graph,
            run_id="r_blocked",
            summary=lambda: {
                "discovered": 9,
                "entitled": 4,
                "healthy": 3,
                "available": 2,
                "eligible": 1,
            },
        )
    )
    assert result.outcome == "BLOCKED"
    assert "operator policy unavailable" in result.reason


def test_canonicalize_resolves_symlinks_and_relative_paths(tmp_path: Path) -> None:
    from verdict.operator_policy import _canonicalize

    real = tmp_path / "real" / "deep"
    real.mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "real")
    canon = _canonicalize(
        ActionContext(
            action="file.read",
            argv=("cat", str(link / "deep" / "note.txt")),
            cwd=str(tmp_path / "sub"),
            worktree=str(tmp_path),
        )
    )
    assert str(real / "note.txt") in canon.paths
    assert canon.cwd == str((tmp_path / "sub").resolve())
