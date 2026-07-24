"""Privacy-safe routing and execution evidence for the explain APIs.

The HTTP compatibility layer still exposes the legacy ``RoutingDecision``
model.  This module is the small adapter boundary that turns that model into
the strict v1 contracts without putting prompt or completion data on the
request path.  Candidate records are copied when the decision is made so an
explain request cannot recompute exclusions from a later cache state.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from verdict.contracts import (
    ContractValidationError,
    OutcomeEvent,
    RoutingDecisionContract,
    TaskSpec,
    redact_contract_secrets,
)
from verdict.models import RoutingDecision
from verdict.security import fingerprint_text, redact_text

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TIER_TO_POLICY_FLOOR = {0: "isolated", 1: "protected", 2: "standard", 3: "best_effort"}
_TERMINAL_OUTCOMES = frozenset(
    {"cancelled", "denied", "error", "failure", "partial", "skipped", "success", "timeout"}
)
_CANDIDATE_FIELDS = frozenset(
    {"model_id", "provider", "admitted", "verdict", "state", "source", "reason"}
)
_MAX_EVIDENCE_TEXT = 256


class AmbiguousEvidenceSelectorError(LookupError):
    """Raised when a non-unique caller-supplied evidence selector is used."""


def utc_now() -> str:
    """Return a stable wire-format timestamp for newly-created evidence."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def safe_identifier(value: object | None, *, prefix: str) -> str:
    """Keep correlation identifiers bounded and free of diagnostic content."""

    if value is not None:
        candidate = redact_text(value).strip()
        if _SAFE_IDENTIFIER.fullmatch(candidate):
            return candidate
    return f"{prefix}-{uuid4().hex}"


def _bounded_text(value: object, *, limit: int = _MAX_EVIDENCE_TEXT) -> str:
    """Retain only bounded, credential-redacted diagnostic text."""

    text = redact_text(value).strip()
    if len(text) <= limit:
        return text
    return f"{text[: limit - 12]}...[truncated]"


def _safe_tool_name(value: object) -> str:
    """Keep routable tool identifiers while hashing arbitrary caller text."""

    candidate = _bounded_text(value, limit=128)
    if _SAFE_IDENTIFIER.fullmatch(candidate):
        return candidate
    return f"redacted-{fingerprint_text(candidate, length=12).split(':', 1)[1]}"


def _safe_candidate_record(value: object) -> dict[str, Any]:
    """Allowlist the gate fields that are safe and useful in an evidence snapshot."""

    if not isinstance(value, dict):
        return {}
    record: dict[str, Any] = {}
    for key in _CANDIDATE_FIELDS:
        if key not in value:
            continue
        field_value = value[key]
        if key == "admitted":
            if isinstance(field_value, bool):
                record[key] = field_value
            continue
        record[key] = _bounded_text(field_value)
    return record


def request_features(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract protocol shape without retaining messages or tool arguments."""

    tools = payload.get("tools")
    response_format = payload.get("response_format")
    messages = payload.get("messages")
    if not isinstance(messages, list):
        messages = []
    feature_payload: dict[str, Any] = {
        "stream": payload.get("stream") is True,
        "tools": isinstance(tools, list) and bool(tools),
        "parallel_tool_calls": payload.get("parallel_tool_calls") is True,
        "response_format": (
            response_format.get("type")
            if isinstance(response_format, dict) and isinstance(response_format.get("type"), str)
            else None
        ),
        "vision": any(
            isinstance(message, dict)
            and isinstance(message.get("content"), list)
            and any(
                isinstance(part, dict) and part.get("type") == "image_url"
                for part in message["content"]
            )
            for message in messages
        ),
    }
    if isinstance(tools, list):
        names = [
            item.get("function", {}).get("name")
            for item in tools
            if isinstance(item, dict)
            and isinstance(item.get("function"), dict)
            and isinstance(item["function"].get("name"), str)
        ]
        feature_payload["tool_count"] = len(tools)
        feature_payload["tool_names"] = [_safe_tool_name(name) for name in names if name]
    else:
        feature_payload["tool_count"] = 0
        feature_payload["tool_names"] = []
    return feature_payload


def _task_spec_for_request(task: str, criticality: str, features: dict[str, Any]) -> TaskSpec:
    """Build a valid TaskSpec whose objective is a fingerprinted preview."""

    required: list[str] = []
    if features.get("tools"):
        required.append("tools")
    if features.get("response_format"):
        required.append("structured_output")
    if features.get("vision"):
        required.append("vision")
    return TaskSpec(
        objective=f"[redacted:{fingerprint_text(task, length=12)} len={len(task)}]",
        task_type="chat_completion",
        required_capabilities=required,
        tools=[_safe_tool_name(name) for name in features.get("tool_names", [])],
        criticality=criticality
        if criticality in {"low", "medium", "high", "critical"}
        else "unknown",
        privacy="unknown",
        risk="unknown",
        metadata={"request_features": redact_contract_secrets(features)},
    )


def _copy_json(value: Any) -> Any:
    """Copy JSON-shaped evidence without importing a mutable application model."""

    if isinstance(value, dict):
        return {str(key): _copy_json(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_copy_json(child) for child in value]
    if isinstance(value, tuple):
        return [_copy_json(child) for child in value]
    return value


def _compact_json(value: Any) -> Any:
    """Remove null object members that strict v1 contracts model as optional."""

    if isinstance(value, dict):
        return {str(key): _compact_json(child) for key, child in value.items() if child is not None}
    if isinstance(value, list):
        return [_compact_json(child) for child in value]
    if isinstance(value, tuple):
        return [_compact_json(child) for child in value]
    return value


def build_routing_decision_contract(
    decision: RoutingDecision,
    *,
    task: str,
    criticality: str,
    features: dict[str, Any] | None = None,
    request_id: str | None = None,
    correlation_id: str | None = None,
    occurred_at: str | None = None,
) -> RoutingDecisionContract:
    """Convert a legacy decision into a strict, redacted v1 route contract."""

    rid = safe_identifier(request_id or decision.request_id, prefix="req")
    cid = safe_identifier(correlation_id, prefix="corr")
    feature_snapshot = _copy_json(features or {})
    if isinstance(feature_snapshot, dict) and isinstance(feature_snapshot.get("tool_names"), list):
        feature_snapshot["tool_names"] = [
            _safe_tool_name(name) for name in feature_snapshot["tool_names"]
        ]
    candidate_records = [
        _safe_candidate_record(record)
        for record in decision.candidate_states
        if isinstance(record, dict)
    ]
    # This is intentionally computed once, from the immutable decision copy.
    exclusions = [
        _copy_json(record)
        for record in candidate_records
        if isinstance(record, dict) and record.get("admitted") is False
    ]
    task_spec = _task_spec_for_request(task, criticality, feature_snapshot)
    model_id = str(decision.model)
    runtime_id = model_id if "/" in model_id else f"{decision.provider}/{model_id}"
    payload: dict[str, Any] = {
        "selected_route": {
            "runtime_id": runtime_id,
            "provider": decision.provider,
            "model": decision.model,
            "decision": decision.decision,
            "tier": decision.tier,
            "headroom_pct": decision.headroom_pct,
            "latency_ms": decision.latency_ms,
            "escalated": decision.escalated,
            "protected": decision.protected,
            "degraded_mode": decision.degraded_mode,
            "transport_outcome": decision.transport_outcome,
            "quality_outcome": decision.quality_outcome,
        },
        "task_spec": _compact_json(task_spec.to_dict()),
        "candidate_snapshot": {
            "captured_at": occurred_at or utc_now(),
            "records": candidate_records,
        },
        # Preserve the decision-time gate records, but keep the public v1
        # exclusion list compact and stable for explain consumers.
        "exclusions": [
            {
                "model": record.get("model_id", record.get("model", "unknown")),
                "reason": record.get("reason") or record.get("verdict", "excluded"),
                "verdict": record.get("verdict", "excluded"),
            }
            for record in exclusions
            if isinstance(record, dict)
        ],
        "policy_floor": _TIER_TO_POLICY_FLOOR.get(decision.tier, "none"),
        "planner_mode": "legacy-routing-adapter",
        "explanation": redact_text(decision.reason),
        "adaptive_influence": {
            "escalated": decision.escalated,
            "escalation_reason": redact_text(decision.escalation_reason or ""),
            "request_features": feature_snapshot,
        },
        "fallback_plan": [],
        "correlation_id": cid,
        "request_id": rid,
        "policy_version": decision.policy_version,
    }
    try:
        return RoutingDecisionContract.from_dict(cast_json(redact_contract_secrets(payload)))
    except ContractValidationError:
        # Keep the contract boundary fail-closed.  This should only be reached
        # if a future legacy field violates a frozen v1 rule.
        raise


def build_outcome_event(
    decision: RoutingDecisionContract,
    *,
    event_type: str,
    outcome: str,
    event_id: str | None = None,
    occurred_at: str | None = None,
    status_code: int | None = None,
    features: dict[str, Any] | None = None,
    streaming_phase: str | None = None,
    retries: int = 0,
    fallbacks: Iterable[Any] = (),
    error_class: str | None = None,
    abort_observed: bool = False,
    latency_ms: float | None = None,
    details: dict[str, Any] | None = None,
) -> OutcomeEvent:
    """Create a strict outcome record containing protocol metadata only."""

    # Caller-provided diagnostic fields are advisory.  Canonical lifecycle
    # fields are assigned last so a future transport adapter cannot overwrite
    # the request shape or completion phase recorded by this boundary.
    detail_payload: dict[str, Any] = _copy_json(details or {})
    detail_payload.update(
        {
            "request_features": _copy_json(features or {}),
            "streaming_phase": streaming_phase,
            "abort_observed": abort_observed,
        }
    )
    if status_code is not None:
        detail_payload["http_status"] = status_code
    if error_class:
        detail_payload["error_class"] = error_class
    detail_payload = {key: value for key, value in detail_payload.items() if value is not None}
    payload = {
        "event_id": event_id or f"evt-{uuid4().hex}",
        "event_type": event_type,
        "correlation_id": decision.correlation_id,
        "outcome": outcome,
        "occurred_at": occurred_at or utc_now(),
        "request_id": decision.request_id,
        "verification": {"status": "not_observed"},
        "quality": {"outcome": "not_observed"},
        "latency_ms": latency_ms
        if latency_ms is not None
        else decision.selected_route.get("latency_ms"),
        "cost": {},
        "retries": retries,
        "fallbacks": list(fallbacks),
        "provider_version": None,
        "model_version": None,
        "details": detail_payload,
    }
    return OutcomeEvent.from_dict(cast_json(redact_contract_secrets(payload)))


@dataclass(frozen=True)
class ExplainEvidence:
    """The two correlated v1 contracts returned by the explain endpoint."""

    routing_decision: RoutingDecisionContract
    outcome_event: OutcomeEvent
    events: tuple[OutcomeEvent, ...] = ()
    # This is deliberately envelope metadata, not part of either v1 contract.
    # It is generated by the local store so repeated caller request IDs remain
    # independently addressable.
    evidence_id: str | None = None
    scope: str = "default"

    def __post_init__(self) -> None:
        events = tuple(self.events or (self.outcome_event,))
        if not events:
            raise ContractValidationError("evidence requires at least one lifecycle event")
        object.__setattr__(self, "events", events)
        object.__setattr__(self, "outcome_event", events[-1])

    def to_dict(self) -> dict[str, Any]:
        events = self.events
        payload: dict[str, Any] = {
            "kind": "execution_evidence",
            "envelope_version": "1",
            "routing_decision": self.routing_decision.to_dict(),
            "events": [event.to_dict() for event in events],
            "outcome_event": self.outcome_event.to_dict(),
        }
        if self.evidence_id is not None:
            payload["evidence_id"] = self.evidence_id
        payload["scope"] = self.scope
        return payload


class EvidenceStore:
    """Bounded process-local evidence store for explain-by-ID queries."""

    def __init__(self, max_entries: int = 256) -> None:
        self.max_entries = max(1, max_entries)
        self._records: OrderedDict[str, ExplainEvidence] = OrderedDict()

    def put(self, evidence: ExplainEvidence, *, scope: str) -> str:
        """Retain evidence under a collision-safe server-owned key.

        Caller supplied IDs remain correlation metadata, but they are not safe
        as the store's primary key: concurrent requests can legitimately reuse
        an idempotency/request id.  The returned key is intentionally opaque
        and is used by the API's private finalization path.
        """

        if not scope:
            raise ValueError("evidence storage scope is required")
        key = f"evidence-{uuid4().hex}"
        stored = ExplainEvidence(
            evidence.routing_decision,
            evidence.outcome_event,
            events=tuple(evidence.events or (evidence.outcome_event,)),
            evidence_id=key,
            scope=scope,
        )
        self._records[key] = stored
        self._records.move_to_end(key)
        while len(self._records) > self.max_entries:
            self._records.popitem(last=False)
        return key

    def append_event(self, key: str, event: OutcomeEvent) -> ExplainEvidence | None:
        """Append one lifecycle event, preserving the first terminal event."""
        if key not in self._records:
            return None
        current = self._records[key]
        if (
            event.request_id != current.routing_decision.request_id
            or event.correlation_id != current.routing_decision.correlation_id
        ):
            raise ContractValidationError("evidence event identity does not match its decision")
        events = tuple(current.events or (current.outcome_event,))
        if current.outcome_event.outcome in _TERMINAL_OUTCOMES:
            return current
        updated = ExplainEvidence(
            current.routing_decision,
            event,
            events=(*events, event),
            evidence_id=key,
            scope=current.scope,
        )
        self._records[key] = updated
        self._records.move_to_end(key)
        return updated

    def find(
        self,
        *,
        evidence_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
        scope: str,
    ) -> ExplainEvidence | None:
        if not scope:
            raise ValueError("evidence lookup scope is required")
        if evidence_id:
            evidence = self._records.get(evidence_id)
            if evidence is not None and evidence.scope == scope:
                return evidence
            return None
        matches = [
            evidence
            for evidence in self._records.values()
            if (
                (request_id is not None and evidence.routing_decision.request_id == request_id)
                or (
                    correlation_id is not None
                    and evidence.routing_decision.correlation_id == correlation_id
                )
            )
            and evidence.scope == scope
        ]
        if len(matches) > 1:
            raise AmbiguousEvidenceSelectorError("evidence selector matches multiple executions")
        if matches:
            return matches[0]
        return None

    def update_outcome(self, key: str, event: OutcomeEvent) -> ExplainEvidence | None:
        return self.append_event(key, event)


def cast_json(value: Any) -> dict[str, Any]:
    """Type narrow a redacted JSON object for strict contract constructors."""

    if not isinstance(value, dict):
        raise ContractValidationError("evidence payload must be a JSON object")
    return value


__all__ = [
    "AmbiguousEvidenceSelectorError",
    "EvidenceStore",
    "ExplainEvidence",
    "build_outcome_event",
    "build_routing_decision_contract",
    "request_features",
    "safe_identifier",
    "utc_now",
]
