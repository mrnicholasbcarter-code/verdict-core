from datetime import datetime, timedelta, timezone

import pytest

from verdict.capability_passports import (
    CapabilityEvidence,
    CapabilityPassport,
    CapabilityStatus,
    EvidenceAuthority,
    RouteIdentity,
)
from verdict.policy import (
    DecisionState,
    Policy,
    PolicyCandidate,
    PolicyValidationError,
    compile_policy,
)
from verdict.policy_artifacts import SignedPolicyDecisionArtifact
from verdict.runtime_passports import (
    RuntimeCapabilityPassport,
    RuntimeSubjectIdentity,
    RuntimeSubjectKind,
)
from verdict.transitions import (
    ByteState,
    ExecutionContext,
    RetrySafety,
    TransitionCompiler,
    TransitionKind,
    TransitionValidationError,
)

NOW = datetime(2026, 7, 30, 4, 30, tzinfo=timezone.utc)


def route(model: str, protocol: str = "chat") -> RouteIdentity:
    return RouteIdentity(
        gateway="gw-1",
        provider="provider",
        connection="account-a",
        endpoint="https://provider.example/v1",
        protocol=protocol,
        model_id=model,
        model_revision="rev-1",
    )


def passport(
    model: str, status: CapabilityStatus = CapabilityStatus.SUPPORTED
) -> CapabilityPassport:
    evidence = CapabilityEvidence(
        status=status,
        source="fixture:probe",
        observed_at=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(minutes=5),
        confidence=1,
        evidence_digest="sha256:" + "1" * 64,
        authority=EvidenceAuthority.VERIFIED,
        method="hermetic",
        adapter_version="test-1",
        scope="provider/account-a",
    )
    return CapabilityPassport(
        route_identity=route(model),
        qualified_at=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(minutes=5),
        observed={"tools": evidence},
    )


def candidate(
    model: str,
    *,
    availability: str = "eligible",
    status: CapabilityStatus = CapabilityStatus.SUPPORTED,
) -> PolicyCandidate:
    return PolicyCandidate(
        candidate_id=model,
        route_identity=route(model),
        passport=passport(model, status),
        availability=availability,
        evidence_ids=(f"evidence-{model}",),
        quality_score=0.5,
    )


def runtime_passport(
    model: str, status: CapabilityStatus = CapabilityStatus.SUPPORTED
) -> RuntimeCapabilityPassport:
    evidence = CapabilityEvidence(
        status=status,
        source="fixture:handshake",
        observed_at=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(minutes=5),
        confidence=1,
        evidence_digest="sha256:" + "2" * 64,
        authority=EvidenceAuthority.VERIFIED,
        method="hermetic-handshake",
        adapter_version="test-1",
        scope="account-a",
    )
    return RuntimeCapabilityPassport(
        subject=RuntimeSubjectIdentity(
            kind=RuntimeSubjectKind.MCP_SERVER,
            subject_id=f"docs-{model}",
            provider="fixture",
            protocol="mcp",
            protocol_version="2025-06-18",
            transport="https",
            auth_mode="bearer",
            endpoint_digest="sha256:" + "3" * 64,
            scope="account-a",
        ),
        qualified_at=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(minutes=5),
        negotiated={"resources.read": evidence},
    )


def runtime_candidate(
    model: str, status: CapabilityStatus = CapabilityStatus.SUPPORTED
) -> PolicyCandidate:
    item = candidate(model)
    return PolicyCandidate(
        candidate_id=item.candidate_id,
        route_identity=item.route_identity,
        passport=item.passport,
        runtime_passports=(runtime_passport(model, status),),
        availability=item.availability,
        evidence_ids=item.evidence_ids,
        quality_score=item.quality_score,
    )


def test_unknown_required_capability_never_becomes_allow() -> None:
    unknown = candidate("a/unknown", status=CapabilityStatus.UNKNOWN)
    missing = PolicyCandidate(
        candidate_id="b/missing", route_identity=route("b/missing"), availability="eligible"
    )
    policy = Policy(required_capabilities=frozenset({"tools"}))
    compiled = policy.compile([unknown, missing], at=NOW, ranking={"a/unknown": 100})
    assert compiled.eligible == ()
    assert {item.decision for item in compiled.decisions} == {DecisionState.UNKNOWN}


def test_runtime_negotiated_capability_is_a_separate_hard_policy_gate() -> None:
    allowed = runtime_candidate("a/allowed")
    missing = candidate("b/missing")
    policy = Policy(required_runtime_capabilities=frozenset({"resources.read"}))

    result = policy.compile([allowed, missing], at=NOW)

    assert [item.candidate_id for item in result.eligible] == ["a/allowed"]
    assert result.decisions[1].decision is DecisionState.UNKNOWN


def test_runtime_unsupported_evidence_is_deny_not_unknown() -> None:
    denied = runtime_candidate("a/denied", CapabilityStatus.UNSUPPORTED)
    policy = Policy(required_runtime_capabilities=frozenset({"resources.read"}))

    result = policy.compile([denied], at=NOW)

    assert result.decisions[0].decision is DecisionState.DENY
    assert "runtime capability 'resources.read' is unsupported" in result.decisions[0].reasons


def test_policy_runtime_capabilities_round_trip_through_dict() -> None:
    policy = Policy(required_runtime_capabilities=frozenset({"resources.read"}))

    assert Policy.from_dict(policy.to_dict()) == policy


def test_policy_candidate_runtime_passports_round_trip_through_dict() -> None:
    original = runtime_candidate("round-trip")

    restored = PolicyCandidate.from_dict(
        {
            "candidate_id": original.candidate_id,
            "route_identity": original.route_identity.to_dict(),
            "passport": original.passport.to_dict(),
            "runtime_passports": [item.to_dict() for item in original.runtime_passports],
            "availability": original.availability,
            "evidence_ids": list(original.evidence_ids),
            "quality_score": original.quality_score,
        }
    )

    assert restored == original


def test_unsupported_capability_is_deny_and_ranking_cannot_reintroduce_it() -> None:
    denied = candidate("a/denied", status=CapabilityStatus.UNSUPPORTED)
    allowed = candidate("b/allowed")
    policy = Policy(required_capabilities=frozenset({"tools"}))
    compiled = policy.compile([denied, allowed], at=NOW, ranking={"a/denied": 1000})
    assert [item.candidate_id for item in compiled.eligible] == ["b/allowed"]
    assert compiled.decisions[0].decision is DecisionState.DENY


def test_protected_policy_requires_exact_identity_and_fresh_availability() -> None:
    no_identity = PolicyCandidate(candidate_id="opaque", availability="eligible")
    stale = candidate("stale", availability="unknown")
    policy = Policy(protected=True)
    result = policy.compile([no_identity, stale], at=NOW)
    assert all(item.decision is DecisionState.UNKNOWN for item in result.decisions)


def test_protected_policy_requires_actual_served_identity_when_configured() -> None:
    selected = candidate("selected")
    policy = Policy(
        required_capabilities=frozenset({"tools"}), protected=True, require_actual_identity=True
    )
    result = policy.compile([selected], at=NOW)
    assert result.decisions[0].decision is DecisionState.UNKNOWN
    assert "actual served route identity is unknown" in result.decisions[0].reasons


def test_policy_compiles_task_hard_predicates_without_learning_override() -> None:
    task = __import__("verdict.contracts", fromlist=["TaskSpec"]).TaskSpec(
        objective="deploy service",
        task_type="execute",
        required_capabilities=["tools"],
        risk="high",
    )
    policy = compile_policy(task)
    assert policy.protected is True
    assert policy.required_capabilities == frozenset({"tools"})


def test_pre_byte_fallback_requires_safe_retry_and_idempotency() -> None:
    primary, backup = candidate("a/primary"), candidate("b/backup")
    policy = Policy(required_capabilities=frozenset({"tools"}))
    graph = TransitionCompiler(policy).compile(
        primary,
        [backup],
        ExecutionContext("req-1", retry_safety=RetrySafety.SAFE, idempotency_key="idem-1"),
        at=NOW,
    )
    fallback = next(edge for edge in graph.edges if edge.target == "b/backup")
    assert fallback.kind is TransitionKind.FALLBACK
    assert fallback.legal is True

    unsafe = TransitionCompiler(policy).compile(
        primary,
        [backup],
        ExecutionContext("req-2", retry_safety=RetrySafety.UNSAFE, idempotency_key="idem-2"),
        at=NOW,
    )
    assert next(edge for edge in unsafe.edges if edge.target == "b/backup").legal is False


def test_bytes_emitted_forbids_cross_route_switch_even_when_target_is_eligible() -> None:
    primary, backup = candidate("a/primary"), candidate("b/backup")
    graph = TransitionCompiler(Policy(required_capabilities=frozenset({"tools"}))).compile(
        primary,
        [backup],
        ExecutionContext(
            "req-3",
            retry_safety=RetrySafety.SAFE,
            idempotency_key="idem-3",
            byte_state=ByteState.BYTES_EMITTED,
        ),
        at=NOW,
    )
    edge = next(edge for edge in graph.edges if edge.target == "b/backup")
    assert edge.kind is TransitionKind.CHECKPOINT_RESUME
    assert edge.legal is False
    assert "model switching after bytes is forbidden" in edge.reasons


def test_verified_same_route_checkpoint_can_resume() -> None:
    primary = candidate("a/primary")
    graph = TransitionCompiler(Policy(required_capabilities=frozenset({"tools"}))).compile(
        primary,
        [candidate("a/primary-retry")],
        ExecutionContext(
            "req-4",
            retry_safety=RetrySafety.SAFE,
            idempotency_key="idem-4",
            byte_state=ByteState.BYTES_EMITTED,
            checkpoint_verified=True,
        ),
        at=NOW,
    )
    assert next(edge for edge in graph.edges if edge.target == "a/primary-retry").legal is False


def test_actual_identity_can_be_attached_without_collapsing_requested_alias() -> None:
    selected = route("selected")
    actual = route("served")
    item = PolicyCandidate(
        candidate_id="alias",
        route_identity=selected,
        requested_alias="auto/coding",
        actual_route=actual,
        availability="eligible",
    )
    assert item.requested_alias == "auto/coding"
    assert item.effective_route == actual
    assert item.route_key == actual.key


def test_policy_compilation_is_canonical_and_digestable() -> None:
    policy = Policy(required_capabilities=frozenset({"tools"}))
    first = policy.compile([candidate("a")], at=NOW)
    second = policy.compile([candidate("a")], at=NOW)
    assert first.to_dict() == second.to_dict()
    assert first.digest.startswith("sha256:")


def test_signed_policy_artifact_verifies_integrity_but_not_fact_truth() -> None:
    policy = Policy(required_capabilities=frozenset({"tools"}))
    compilation = policy.compile([candidate("a")], at=NOW)
    artifact = SignedPolicyDecisionArtifact.issue(
        "verdict-test", policy, compilation, "test-key", issued_at=NOW
    )
    assert artifact.verify("test-key") is True
    assert artifact.verify("wrong-key") is False
    assert SignedPolicyDecisionArtifact.from_dict(artifact.to_dict()).verify("test-key") is True


def test_policy_and_transition_inputs_are_strict() -> None:
    with pytest.raises(PolicyValidationError):
        Policy.from_dict({"protected": False, "unexpected": True})
    with pytest.raises(TransitionValidationError):
        ExecutionContext("req", retry_safety="maybe")


def test_policy_candidate_rejects_mismatched_passport_identity() -> None:
    with pytest.raises(PolicyValidationError, match="exactly match"):
        PolicyCandidate(
            candidate_id="mismatch",
            route_identity=route("mismatch"),
            passport=passport("other"),
            availability="eligible",
        )


def test_policy_rejects_duplicate_candidate_ids() -> None:
    with pytest.raises(PolicyValidationError, match="unique"):
        Policy().compile([candidate("duplicate"), candidate("duplicate")], at=NOW)


def test_terminal_context_has_no_new_execution_edges() -> None:
    primary = candidate("a/primary")
    graph = TransitionCompiler(Policy()).compile(
        primary, [candidate("b/backup")], ExecutionContext("req-5", terminal=True), at=NOW
    )
    assert len(graph.legal_edges) == 1
    assert graph.legal_edges[0].kind is TransitionKind.TERMINAL
