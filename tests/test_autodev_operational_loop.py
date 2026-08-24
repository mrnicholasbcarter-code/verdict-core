"""Red lifecycle contracts for the packet-bound operational work-unit path.

These tests intentionally use local Git repositories and injected executors.
They protect the orchestration boundary; they do not claim the live-model
demonstration required by the feature's later acceptance task.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from verdict.autodev_run import run_packet_autodev
from verdict.execution_packet import ExecutionPacket, capture_source_binding
from verdict.patch_executor import PatchAttempt
from verdict.receipt_store import ReceiptStore


def _completed(command: list[str], code: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, code, "", stderr)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "owned.txt").write_text("before\n", encoding="utf-8")
    (tmp_path / "escape.txt").write_text("private\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    return tmp_path


def _packet(repo: Path) -> ExecutionPacket:
    return ExecutionPacket(
        packet_id="headroom-unknown",
        packet_version=1,
        story_id="US1",
        story_version="1",
        source=capture_source_binding(repo, repository="git@example.test:verdict.git", lock_paths=()),
        intent={
            "goal": "Change one owned file through a verified packet run.",
            "non_goals": ["Plan work.", "Change unowned files."],
            "acceptance": ["The independent command exits zero."],
            "limitations": ["This fixture is not live-model proof."],
        },
        authority={
            "owned_paths": ["owned.txt"],
            "denied_paths": ["escape.txt"],
            "tools": ["patch", "test"],
            "network": False,
            "max_spend_usd": 0.25,
            "max_concurrency": 1,
            "max_attempts": 2,
            "destructive": False,
            "production": False,
        },
        verification={"argv": ["verify-owned"], "timeout_seconds": 30},
        decisions=[],
        context_refs=[],
        tasks=[
            {
                "task_id": "headroom-unknown",
                "description": "Make the bounded edit.",
                "status": "pending",
                "dependencies": [],
            }
        ],
        route_attempts=[],
        failure_history=[],
        transitions=[],
        checkpoint_refs=[],
        receipt_refs=[],
        next_safe_action="Validate source before inference.",
        proof_level="fixture-only",
    )


def _route(requested: str, actual: str, **extra: Any) -> dict[str, Any]:
    """An input already admitted by the routing/eligibility boundary (T011)."""
    return {
        "requested_identity": requested,
        "actual_identity": actual,
        "admitted": True,
        "evidence_digest": "sha256:" + "e" * 64,
        **extra,
    }


class _WritingExecutor:
    """Offline stand-in for an actual patch executor in one disposable attempt tree."""

    def __init__(self, repo: Path, *, path: str = "owned.txt", content: str = "after\n") -> None:
        self.repo = repo
        self.path = path
        self.content = content
        self.checkpoint_id: str | None = None
        self.context_prompt: str | None = None

    def execute_packet_unit(
        self,
        *,
        packet: ExecutionPacket,
        checkpoint_id: str,
        route: Mapping[str, Any],
        context_prompt: str,
    ) -> PatchAttempt:
        self.checkpoint_id = checkpoint_id
        self.context_prompt = context_prompt
        (self.repo / self.path).write_text(self.content, encoding="utf-8")
        return PatchAttempt(
            unit_id=packet.tasks[0]["task_id"],
            model=str(route["requested_identity"]),
            outcome="applied",
            changed_files=(self.path,),
        )


class _Factory:
    def __init__(self, writers: list[dict[str, str]]) -> None:
        self.writers = iter(writers)
        self.executors: list[_WritingExecutor] = []
        self.attempt_roots: list[Path] = []

    def __call__(
        self, *, attempt_repo: Path, route: Mapping[str, Any], packet: ExecutionPacket
    ) -> _WritingExecutor:
        del route, packet
        writer = _WritingExecutor(attempt_repo, **next(self.writers))
        self.executors.append(writer)
        self.attempt_roots.append(attempt_repo)
        return writer


class _Verifier:
    def __init__(self, expected: str) -> None:
        self.expected = expected
        self.calls: list[Path] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert command == ["verify-owned"]
        root = Path(kwargs["cwd"])
        self.calls.append(root)
        observed = (root / "owned.txt").read_text(encoding="utf-8")
        return _completed(command, 0 if observed == self.expected else 1, "unexpected owned content")


def test_packet_source_drift_stops_before_any_inference_and_emits_a_drift_receipt(repo: Path) -> None:
    packet = _packet(repo)
    (repo / "owned.txt").write_text("changed after packet binding\n", encoding="utf-8")
    factory = _Factory([{}])
    store = ReceiptStore(":memory:")

    report = run_packet_autodev(
        packet,
        repo,
        admitted_route=_route("free/cheap", "gateway/free-v1"),
        executor_factory=factory,
        store=store,
        verification_runner=_Verifier("after\n"),
    )

    assert report.terminal_state == "drifted"
    assert factory.executors == []
    receipts = store.query_receipts(scope="operational-loop")
    assert any(record.payload.get("terminal_state") == "drifted" for record in receipts)


def test_validated_packet_checkpoints_before_inference_and_attributes_a_real_patch(repo: Path) -> None:
    packet = _packet(repo)
    factory = _Factory([{"content": "after\n"}])
    verifier = _Verifier("after\n")
    store = ReceiptStore(":memory:")

    report = run_packet_autodev(
        packet,
        repo,
        admitted_route=_route("free/cheap-alias", "provider/cheap-served-2026-08"),
        executor_factory=factory,
        store=store,
        verification_runner=verifier,
    )

    assert report.terminal_state == "completed"
    assert factory.executors[0].checkpoint_id == report.checkpoints["before_inference"]
    assert "Change one owned file through a verified packet run." in str(
        factory.executors[0].context_prompt
    )
    attempt = report.attempts[0]
    assert attempt.requested_identity == "free/cheap-alias"
    assert attempt.actual_identity == "provider/cheap-served-2026-08"
    assert attempt.changed_files == ("owned.txt",)
    assert attempt.artifact_digest.startswith("sha256:")
    assert attempt.verified is True
    assert verifier.calls == [factory.attempt_roots[0]]
    assert (repo / "owned.txt").read_text(encoding="utf-8") == "after\n"


def test_out_of_bounds_artifact_is_not_replayed_and_ends_in_truthful_failure(repo: Path) -> None:
    packet = _packet(repo)
    factory = _Factory([{"path": "escape.txt", "content": "leaked\n"}])

    report = run_packet_autodev(
        packet,
        repo,
        admitted_route=_route("free/cheap", "gateway/free-v1"),
        executor_factory=factory,
        store=ReceiptStore(":memory:"),
        verification_runner=_Verifier("after\n"),
    )

    assert report.terminal_state == "truthful_failure"
    assert report.attempts[0].verified is False
    assert "outside owned paths" in report.attempts[0].reason
    assert (repo / "owned.txt").read_text(encoding="utf-8") == "before\n"
    assert (repo / "escape.txt").read_text(encoding="utf-8") == "private\n"


def test_failed_attempt_is_isolated_then_one_primary_fallback_is_verified_and_replayed(repo: Path) -> None:
    packet = _packet(repo)
    factory = _Factory([{"content": "wrong\n"}, {"content": "after\n"}])
    verifier = _Verifier("after\n")

    report = run_packet_autodev(
        packet,
        repo,
        admitted_route=_route("free/cheap", "gateway/free-v1"),
        fallback_route=_route("cc/claude-sonnet-5", "anthropic/sonnet-served"),
        executor_factory=factory,
        store=ReceiptStore(":memory:"),
        verification_runner=verifier,
    )

    assert report.terminal_state == "completed"
    assert report.fallback_count == 1
    assert [attempt.requested_identity for attempt in report.attempts] == [
        "free/cheap",
        "cc/claude-sonnet-5",
    ]
    assert len(set(factory.attempt_roots)) == 2
    assert all(root != repo for root in factory.attempt_roots)
    assert all(not root.exists() for root in factory.attempt_roots)
    assert (repo / "owned.txt").read_text(encoding="utf-8") == "after\n"


def test_second_fallback_is_denied_after_one_clean_failed_fallback(repo: Path) -> None:
    packet = _packet(repo)
    factory = _Factory([{"content": "wrong-one\n"}, {"content": "wrong-two\n"}])

    report = run_packet_autodev(
        packet,
        repo,
        admitted_route=_route("free/cheap", "gateway/free-v1"),
        fallback_route=_route("cc/claude-sonnet-5", "anthropic/sonnet-served"),
        executor_factory=factory,
        store=ReceiptStore(":memory:"),
        verification_runner=_Verifier("after\n"),
    )

    assert report.terminal_state == "truthful_failure"
    assert report.fallback_count == 1
    assert len(report.attempts) == 2
    assert (repo / "owned.txt").read_text(encoding="utf-8") == "before\n"


def test_ineligible_fallback_is_not_invoked_after_a_failed_attempt(repo: Path) -> None:
    packet = _packet(repo)
    factory = _Factory([{"content": "wrong\n"}, {"content": "after\n"}])

    report = run_packet_autodev(
        packet,
        repo,
        admitted_route=_route("free/cheap", "gateway/free-v1"),
        fallback_route=_route(
            "cc/claude-sonnet-5",
            "anthropic/sonnet-served",
            admitted=False,
        ),
        executor_factory=factory,
        store=ReceiptStore(":memory:"),
        verification_runner=_Verifier("after\n"),
    )

    assert report.terminal_state == "truthful_failure"
    assert report.fallback_count == 0
    assert len(factory.executors) == 1


def test_fallback_requires_a_classified_clean_failure(repo: Path) -> None:
    packet = _packet(repo)
    factory = _Factory([{"content": "wrong\n"}, {"content": "after\n"}])

    report = run_packet_autodev(
        packet,
        repo,
        admitted_route=_route("free/cheap", "gateway/free-v1"),
        fallback_route=_route("cc/claude-sonnet-5", "anthropic/sonnet-served"),
        executor_factory=factory,
        store=ReceiptStore(":memory:"),
        verification_runner=_Verifier("after\n"),
        classify_failure=lambda _attempt: None,
    )

    assert report.terminal_state == "truthful_failure"
    assert report.fallback_count == 0
    assert len(factory.executors) == 1


def test_source_drift_during_inference_stops_before_verified_patch_replay(repo: Path) -> None:
    packet = _packet(repo)

    class _ConcurrentChangeExecutor(_WritingExecutor):
        def execute_packet_unit(self, **kwargs: Any) -> PatchAttempt:
            result = super().execute_packet_unit(**kwargs)
            (repo / "escape.txt").write_text("concurrent user edit\n", encoding="utf-8")
            return result

    class _ConcurrentFactory:
        def __call__(self, *, attempt_repo: Path, **_: Any) -> _ConcurrentChangeExecutor:
            return _ConcurrentChangeExecutor(attempt_repo)

    report = run_packet_autodev(
        packet,
        repo,
        admitted_route=_route("free/cheap", "gateway/free-v1"),
        executor_factory=_ConcurrentFactory(),
        store=ReceiptStore(":memory:"),
        verification_runner=_Verifier("after\n"),
    )

    assert report.terminal_state == "drifted"
    assert (repo / "owned.txt").read_text(encoding="utf-8") == "before\n"


def test_completed_packet_restart_resumes_without_reexecuting_or_duplicate_transition(repo: Path) -> None:
    packet = _packet(repo)
    factory = _Factory([{"content": "after\n"}])
    store = ReceiptStore(":memory:")

    first = run_packet_autodev(
        packet,
        repo,
        admitted_route=_route("free/cheap", "gateway/free-v1"),
        executor_factory=factory,
        store=store,
        verification_runner=_Verifier("after\n"),
    )
    resumed = run_packet_autodev(
        packet.for_model("cc/claude-sonnet-5"),
        repo,
        admitted_route=_route("free/cheap", "gateway/free-v1"),
        executor_factory=factory,
        store=store,
        verification_runner=_Verifier("after\n"),
        resume=True,
    )

    assert first.terminal_state == "completed"
    assert resumed.terminal_state == "completed"
    assert resumed.resumed is True
    assert len(factory.executors) == 1
    assert resumed.checkpoints == first.checkpoints


def test_receipts_redact_raw_provider_payloads_and_truthfully_preserve_failure(repo: Path) -> None:
    packet = _packet(repo)
    factory = _Factory([{"content": "wrong\n"}])
    store = ReceiptStore(":memory:")
    sensitive_route = _route(
        "free/cheap",
        "gateway/free-v1",
        provider_payload={
            "prompt": "private patch prompt",
            "completion": "private model completion",
            "authorization": "Bearer secret-token",
        },
    )

    report = run_packet_autodev(
        packet,
        repo,
        admitted_route=sensitive_route,
        executor_factory=factory,
        store=store,
        verification_runner=_Verifier("after\n"),
    )

    assert report.terminal_state == "truthful_failure"
    persisted = json.dumps(
        [record.payload for record in store.query_receipts(scope="operational-loop")], sort_keys=True
    )
    for raw in ("private patch prompt", "private model completion", "secret-token"):
        assert raw not in persisted
    assert "[REDACTED]" in persisted


def test_default_executor_builds_a_real_patch_executor_from_route_and_context(repo: Path) -> None:
    packet = _packet(repo)
    seen: dict[str, Any] = {}

    class _PatchExecutor:
        def __init__(self, attempt_repo: Path, config: Any) -> None:
            seen["repo"] = attempt_repo
            seen["config"] = config

        def execute_unit(self, unit: Any) -> PatchAttempt:
            seen["unit"] = unit
            (Path(seen["repo"]) / "owned.txt").write_text("after\n", encoding="utf-8")
            return PatchAttempt(
                unit_id=unit.unit_id,
                model=seen["config"].model,
                outcome="applied",
                changed_files=("owned.txt",),
            )

    import verdict.autodev_run as autodev_run

    original = autodev_run.PatchExecutor
    autodev_run.PatchExecutor = _PatchExecutor  # type: ignore[assignment]
    try:
        report = run_packet_autodev(
            packet,
            repo,
            admitted_route={
                **_route("openrouter/stealth/ox-alpha", "openrouter/stealth/ox-alpha"),
                "base_url": "http://127.0.0.1:20128/v1",
            },
            store=ReceiptStore(":memory:"),
            verification_runner=_Verifier("after\n"),
        )
    finally:
        autodev_run.PatchExecutor = original

    assert report.terminal_state == "completed"
    assert seen["config"].model == "openrouter/stealth/ox-alpha"
    assert seen["unit"].verification_command == ("verify-owned",)
    assert "Change one owned file through a verified packet run." in seen["unit"].context

# --- Clarified requirements: capability-aware handoff (AC-0.9/AC-0.10) and
# blocked-resumable state when no qualified worker exists (AC-0.11). ---
from verdict.autodev_routing import CandidateEvidence  # noqa: E402
from verdict.autodev_run import (  # noqa: E402
    _route_is_admitted,
    worker_capability_report,
)
from verdict.gateway_adapters import AdapterRouteIdentity  # noqa: E402


def _worker_evidence(
    alias: str,
    *,
    capabilities: Mapping[str, str] | None = None,
    fresh: bool = True,
) -> CandidateEvidence:
    from datetime import datetime, timedelta, timezone

    observed = datetime.now(timezone.utc) - (timedelta(seconds=3600) if not fresh else timedelta(0))
    return CandidateEvidence(
        requested_alias=alias,
        route=AdapterRouteIdentity(
            gateway_id="omniroute", route_id=f"route:{alias}", provider="gateway",
            model_id=alias.split("/")[-1], protocol="openai-compatible"
        ),
        availability="eligible",
        capabilities=dict(capabilities or {"patch": "observed", "test": "observed"}),
        observed_at=observed,
        ttl_seconds=300,
        source="fixture:capability-probe",
    )


def _handoff_route(alias: str, **extra: Any) -> dict[str, Any]:
    return _route("free/cheap", "gateway/free-v1", handoff_to=alias, **extra)


def test_handoff_requires_fresh_source_linked_capability_evidence(repo: Path) -> None:
    """A self-reported or stale candidate is never admitted as a handoff target."""

    stale = _worker_evidence("stale/worker", fresh=False)
    assert stale.is_fresh() is False

    self_reported = {
        "requested_identity": "free/cheap",
        "actual_identity": "gateway/free-v1",
        "admitted": False,
        "reason": "self-report claims patch capability; no source-linked evidence",
    }
    assert _route_is_admitted(self_reported) is False

    evidence_digest_absent = {
        "requested_identity": "free/cheap",
        "actual_identity": "gateway/free-v1",
        "admitted": True,
    }
    assert _route_is_admitted(evidence_digest_absent) is False


def test_incumbent_worker_without_required_capability_hands_off_with_preserved_state(
    repo: Path,
) -> None:
    """The run records a handoff to a qualified worker and never replays work."""
    packet = _packet(repo)
    factory = _Factory([{"content": "after\n"}])
    store = ReceiptStore(":memory:")
    qualified = _worker_evidence("other/qualified")

    report = run_packet_autodev(
        packet,
        repo,
        admitted_route=_handoff_route(
            "other/qualified", required_capabilities=["patch", "test"]
        ),
        executor_factory=factory,
        store=store,
        verification_runner=_Verifier("after\n"),
        worker_evidence={"other/qualified": qualified},
    )

    assert report.terminal_state == "completed"
    persisted = [r.payload for r in store.query_receipts(scope="operational-loop")]
    handoffs = [p for p in persisted if p.get("event") == "handoff"]
    assert len(handoffs) == 1
    record = handoffs[0]
    assert record["to_worker"] == "other/qualified"
    assert record["packet_id"] == packet.packet_id
    # Identity, acceptance, authority preserved — same packet object resumed.
    assert record["integrity_digest"] == packet.integrity_digest
    assert record["preserved"]["packet_id"] == packet.packet_id
    assert record["preserved"]["packet_version"] == packet.packet_version
    # Authority/acceptance are preserved by the unchanged packet, proven by its
    # integrity digest; the receipt stores digests, not duplicated sensitive
    # structures (AC-0.7 redaction applies).
    assert record["integrity_digest"] == packet.integrity_digest


def test_no_qualified_worker_blocks_resumably_and_names_every_gap(repo: Path) -> None:
    packet = _packet(repo)
    factory = _Factory([])
    store = ReceiptStore(":memory:")
    stale_only = _worker_evidence("stale/only", fresh=False)

    report = run_packet_autodev(
        packet,
        repo,
        admitted_route=_handoff_route(
            "stale/only", required_capabilities=["patch", "test", "network"]
        ),
        executor_factory=factory,
        store=store,
        verification_runner=_Verifier("after\n"),
        worker_evidence={"stale/only": stale_only},
    )

    assert report.terminal_state == "blocked_no_qualified_worker"
    assert factory.executors == []  # nothing executed without qualification
    persisted = [r.payload for r in store.query_receipts(scope="operational-loop")]
    blocked = [p for p in persisted if p.get("terminal_state") == "blocked_no_qualified_worker"]
    assert len(blocked) == 1
    gaps = blocked[0]["unsatisfied_capabilities"]
    assert set(gaps) == {"freshness", "network"}
    assert "evidence_checked" in blocked[0]
    # Packet unchanged: same digest, resumable.
    assert blocked[0]["integrity_digest"] == packet.integrity_digest
    assert blocked[0].get("resumable") is True


def test_worker_capability_report_rejects_non_evidence_qualification() -> None:
    """Name/tier/reputation/self-report cannot establish qualification."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    named = {"requested_alias": "big/tier", "reputation": 0.99}
    assert worker_capability_report(["patch"], named, {}, now)["qualified"] is False
