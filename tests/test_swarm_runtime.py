from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

import pytest

from verdict.swarm_runtime import (
    RUNTIME_ADAPTER_PROTOCOL_VERSION,
    FakeSwarmRuntimeAdapter,
    RuntimeFailure,
    RuntimeRequest,
    RuntimeResponse,
    RuntimeState,
    SwarmRuntimeAdapter,
)


def test_request_response_round_trip_includes_protocol_and_route_attempts() -> None:
    request = RuntimeRequest(
        request_id="req-1",
        task_id="task-1",
        objective="Run focused slice",
        route_attempts=[{"adapter_id": "fake", "status": "selected"}],
        metadata={"priority": "low"},
    )

    payload = request.to_dict()

    assert payload["protocol_version"] == RUNTIME_ADAPTER_PROTOCOL_VERSION
    assert RuntimeRequest.from_dict(payload) == request

    response = RuntimeResponse(
        request_id="req-1",
        task_id="task-1",
        state=RuntimeState.COMPLETED,
        output={"ok": True},
        route_attempts=payload["route_attempts"],
    )

    assert RuntimeResponse.from_dict(response.to_dict()) == response


def test_structured_failure_round_trips_and_marks_terminal_response() -> None:
    failure = RuntimeFailure(
        code="adapter_unavailable",
        message="adapter unavailable",
        retryable=True,
        details={"adapter_id": "fake"},
    )

    response = RuntimeResponse(
        request_id="req-1", task_id="task-1", state=RuntimeState.FAILED, failure=failure
    )

    assert RuntimeFailure.from_dict(failure.to_dict()) == failure
    assert RuntimeResponse.from_dict(response.to_dict()).failure == failure


def test_fake_adapter_lifecycle_and_cancel_deadline() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    adapter = FakeSwarmRuntimeAdapter(now=lambda: now)
    request = RuntimeRequest(request_id="req-1", task_id="task-1", objective="Run slice")

    submitted = adapter.submit(request)
    assert submitted.state == RuntimeState.SUBMITTED

    running = adapter.start("task-1")
    assert running.state == RuntimeState.RUNNING

    cancelled = adapter.cancel("task-1", deadline_at=now)
    assert cancelled.state == RuntimeState.CANCELLED
    assert cancelled.cancel_deadline_at == now.isoformat()

    assert adapter.status("task-1") == cancelled


def test_fake_adapter_conforms_to_runtime_protocol() -> None:
    assert isinstance(FakeSwarmRuntimeAdapter(), SwarmRuntimeAdapter)


@pytest.mark.parametrize("response_contract", [False, True])
def test_runtime_contracts_reject_unknown_protocol_version(response_contract: bool) -> None:
    request = RuntimeRequest(request_id="req-1", task_id="task-1", objective="Run slice")
    payload = request.to_dict()
    if response_contract:
        payload = FakeSwarmRuntimeAdapter().submit(request).to_dict()
    payload["protocol_version"] = "runtime-adapter/v0"

    with pytest.raises(ValueError, match="protocol_version"):
        if response_contract:
            RuntimeResponse.from_dict(payload)
        else:
            RuntimeRequest.from_dict(payload)


def test_runtime_module_keeps_governance_and_evidence_import_boundary() -> None:
    runtime_path = Path(__file__).resolve().parents[1] / "verdict" / "swarm_runtime.py"
    tree = ast.parse(runtime_path.read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }

    assert not imports & {"verdict.swarm_governance", "verdict.evidence"}


def test_governance_does_not_import_ruflo_transport() -> None:
    root = Path(__file__).resolve().parents[1] / "verdict"
    forbidden = {
        "RufloAdapter",
        "RufloTransport",
        "verdict.ruflo_adapter",
        "verdict.ruflo_transport",
    }
    for name in ("swarm_governance.py", "swarm_runtime.py", "swarm_evidence.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add(node.module or "")
                imports.update(alias.name for alias in node.names)
        assert not imports & forbidden, name


def test_swarm_runtime_v1_rejects_unsupported_and_malformed_messages() -> None:
    from verdict.swarm_runtime import SWARM_RUNTIME_PROTOCOL_VERSION

    with pytest.raises(ValueError, match="protocol_version"):
        RuntimeRequest.from_dict(
            {
                "request_id": "req-1",
                "task_id": "task-1",
                "objective": "run",
                "protocol_version": "swarm-runtime/v0",
            }
        )
    with pytest.raises(ValueError, match="unknown field"):
        RuntimeRequest.from_dict(
            {
                "request_id": "req-1",
                "task_id": "task-1",
                "objective": "run",
                "protocol_version": SWARM_RUNTIME_PROTOCOL_VERSION,
                "unexpected": True,
            }
        )


def test_core_and_ruflo_structured_failure_categories() -> None:
    from verdict.ruflo_adapter import RufloAdapter, RufloAdapterConfig
    from verdict.swarm_runtime import SWARM_RUNTIME_PROTOCOL_VERSION, validate_response

    class Transport:
        def __call__(self, envelope: dict[str, object]) -> dict[str, object]:
            method = envelope["method"]
            params = envelope["params"]
            assert isinstance(params, dict)
            if method == "submit":
                return {
                    "task_id": "ruflo-task-1",
                    "workflow_id": None,
                    "status": "queued",
                    "accepted": True,
                }
            if method == "control":
                return {
                    "task_id": params["task_id"],
                    "action": params["action"],
                    "success": False,
                    "previous_status": "queued",
                    "new_status": "queued",
                    "reason": "unauthorized",
                }
            raise AssertionError(method)

    request = RuntimeRequest(
        request_id="req-fail",
        task_id="task-fail",
        objective="bounded work",
        protocol_version=SWARM_RUNTIME_PROTOCOL_VERSION,
        swarm_id="swarm-1",
        slice_id="slice-1",
        envelope_digest="sha256:abc",
        approved_bounds={"max_concurrency": 1, "tools": ["read_file"]},
        verification_profile={
            "required_checks": ["pytest"],
            "required_evidence": ["swarm/demo:ev"],
        },
        allowed_controls=("pause",),
        metadata={"evidence_scope": "swarm/demo"},
    )
    core = FakeSwarmRuntimeAdapter()
    ruflo = RufloAdapter(RufloAdapterConfig(fake_mode=False), Transport())

    payload = request.to_dict()
    payload["protocol_version"] = "swarm-runtime/v0"
    with pytest.raises(ValueError, match="protocol_version"):
        RuntimeRequest.from_dict(payload)

    submitted = core.submit(request)
    broader = RuntimeResponse.from_dict(
        {
            **submitted.to_dict(),
            "metadata": {
                **dict(submitted.metadata),
                "observed_bounds": {"max_concurrency": 99, "tools": ["read_file", "shell"]},
            },
        }
    )
    with pytest.raises(ValueError, match="exceeds approved bound"):
        validate_response(broader, request)

    ruflo.submit_runtime(request)
    for action in ("resume", "cancel"):
        denied = ruflo.runtime_control(request, "ruflo-task-1", action)
        assert denied.failure is not None
        assert denied.failure.category == "unauthorized_control"
        assert denied.swarm_id == "swarm-1"
        assert denied.envelope_digest == "sha256:abc"
