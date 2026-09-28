"""Routing explorer projection from recorded eligibility evidence (BOD-277).

Public surface
--------------
``routing_view(events_or_run_dir)``
    Build a :class:`RoutingView` from a recorded run's eligibility events.

``routing_view_from_inventory(task_profile, *, ...)``
    Build a :class:`RoutingView` by running the real eligibility ladder
    read-only against a live inventory.  No dispatch; probes only when
    the caller passes ``probe=True``.

``RoutingView.to_dict()``
    Stable, schema-versioned JSON projection.

Design rules
------------
* UNKNOWN stays ``None`` - never defaulted or guessed.
* Candidates beyond the recorded 25 are reported as ``candidates_omitted``.
* Legacy events without ``candidates`` -> ``candidates_unknown=True``.
* ``selected_route`` vs ``observed_route`` mismatch is flagged.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "routing-view-v1"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateRecord:
    """One route from the recorded eligibility evaluation."""

    route_id: str
    provider: str
    # Funnel
    reached: str | None  # highest EligibilityStage value reached, or None
    failed_stage: str | None
    rejection_reason: str | None
    # Rank (None when the route did not reach ranking)
    rank: int | None
    rank_components: dict[str, Any] | None
    # Capability facts
    capability_tier: int | None
    context_window: int | None
    supports_tools: bool | None
    supports_structured_output: bool | None
    # Economics
    capacity_class: str | None
    plan_label: str | None
    cooldown_until: str | None
    price: float | None  # metered price per 1M tokens; None = UNKNOWN

    @classmethod
    def from_verdict_dict(cls, d: Mapping[str, Any]) -> CandidateRecord:
        return cls(
            route_id=str(d.get("route_id") or ""),
            provider=str(d.get("provider") or ""),
            reached=d.get("reached") or None,
            failed_stage=d.get("failed_stage") or None,
            rejection_reason=d.get("reason") or None,
            rank=d.get("rank"),
            rank_components=(
                dict(d["rank_components"])
                if isinstance(d.get("rank_components"), Mapping)
                else None
            ),
            capability_tier=d.get("capability_tier"),
            context_window=d.get("context_window"),
            supports_tools=d.get("supports_tools"),
            supports_structured_output=d.get("supports_structured_output"),
            capacity_class=d.get("capacity_class") or None,
            plan_label=d.get("plan_label") or None,
            cooldown_until=d.get("cooldown_until") or None,
            price=d.get("price"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "provider": self.provider,
            "reached": self.reached,
            "failed_stage": self.failed_stage,
            "rejection_reason": self.rejection_reason,
            "rank": self.rank,
            "rank_components": self.rank_components,
            "capability_tier": self.capability_tier,
            "context_window": self.context_window,
            "supports_tools": self.supports_tools,
            "supports_structured_output": self.supports_structured_output,
            "capacity_class": self.capacity_class,
            "plan_label": self.plan_label,
            "cooldown_until": self.cooldown_until,
            "price": self.price,
        }


@dataclass(frozen=True)
class EligibilityEvaluation:
    """Projection of one eligibility event (one per node per selection attempt)."""

    node_id: str
    seq: int  # event sequence number for ordering
    at: str  # ISO-8601 UTC

    # Funnel counts: routes that reached AT LEAST each stage.
    funnel: dict[str, int]  # stage_name -> count

    # Rejection reasons per stage: {stage: {reason: count}}
    rejections: dict[str, dict[str, int]]

    # The winning route id (from the eligibility event ``selected`` field).
    selected_route: str | None

    # Candidate records (up to 25, selected always first when present).
    # None means the event pre-dates PR #711 and candidates were not recorded.
    candidates: list[CandidateRecord] | None

    # Count of routes evaluated but not recorded (total - len(candidates)).
    candidates_omitted: int

    # explain_selection output built from the candidates above.
    selected_because: list[str]

    # Execution identity from the terminal event for this node+attempt.
    observed_route: str | None  # reported_model from terminal
    session_ref: str | None  # harness session_ref from terminal

    # Flag when selected != observed (and both are non-empty).
    selected_observed_mismatch: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "seq": self.seq,
            "at": self.at,
            "funnel": dict(sorted(self.funnel.items())),
            "rejections": {
                stage: dict(sorted(reasons.items()))
                for stage, reasons in sorted(self.rejections.items())
            },
            "selected_route": self.selected_route,
            "candidates": (
                [c.to_dict() for c in self.candidates] if self.candidates is not None else None
            ),
            "candidates_omitted": self.candidates_omitted,
            "selected_because": self.selected_because,
            "observed_route": self.observed_route,
            "session_ref": self.session_ref,
            "selected_observed_mismatch": self.selected_observed_mismatch,
        }


@dataclass(frozen=True)
class RoutingView:
    """Authoritative routing explorer projection for one run or inventory query."""

    schema_version: str
    source: str  # "recorded" | "inventory"
    generated_at: str  # ISO-8601 UTC
    evaluations: list[EligibilityEvaluation]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source": self.source,
            "generated_at": self.generated_at,
            "evaluations": [e.to_dict() for e in self.evaluations],
        }

    def to_json(self, **kwargs: Any) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, **kwargs)


# ---------------------------------------------------------------------------
# Helpers: build EligibilityEvaluation from a raw eligibility event
# ---------------------------------------------------------------------------

_FUNNEL_STAGES = ("DISCOVERED", "ENTITLED", "HEALTHY", "AVAILABLE", "TASK_ELIGIBLE", "SELECTED")

# Mapping from the lower-cased key used in _ladder_counts / summary() to the
# canonical EligibilityStage value.
_COUNTS_KEY_TO_STAGE: dict[str, str] = {
    "discovered": "DISCOVERED",
    "entitled": "ENTITLED",
    "healthy": "HEALTHY",
    "available": "AVAILABLE",
    "eligible": "TASK_ELIGIBLE",
    "selected": "SELECTED",
}


def _funnel_from_event(data: Mapping[str, Any]) -> dict[str, int]:
    """Extract funnel counts from a raw eligibility event data dict."""
    funnel: dict[str, int] = {s: 0 for s in _FUNNEL_STAGES}
    for key, stage in _COUNTS_KEY_TO_STAGE.items():
        v = data.get(key)
        if isinstance(v, int):
            funnel[stage] = v
    # ``selected`` is also a string field (route_id); treat as count 1 when set.
    if (
        data.get("selected")
        and isinstance(data["selected"], str)
        and funnel.get("SELECTED", 0) == 0
    ):
        funnel["SELECTED"] = 1
    return funnel


def _rejections_from_event(data: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    """Extract rejections dict from event data, normalising the structure."""
    raw = data.get("rejections")
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, dict[str, int]] = {}
    for stage, reasons in raw.items():
        if not isinstance(reasons, Mapping):
            continue
        out[str(stage)] = {str(r): int(c) for r, c in reasons.items() if isinstance(c, int)}
    return out


def _candidates_from_event(data: Mapping[str, Any]) -> tuple[list[CandidateRecord] | None, int]:
    """Return (candidates, omitted).  None means legacy event (no candidates key)."""
    if "candidates" not in data:
        return None, 0
    raw = data["candidates"]
    if not isinstance(raw, list):
        return None, 0
    records = [CandidateRecord.from_verdict_dict(d) for d in raw if isinstance(d, Mapping)]
    omitted = data.get("candidates_omitted")
    omitted_count = int(omitted) if isinstance(omitted, int) else 0
    return records, omitted_count


def _selected_because_from_candidates(candidates: list[CandidateRecord] | None) -> list[str]:
    """Run explain_selection logic on in-memory CandidateRecord objects."""
    if not candidates:
        return []
    from verdict.orchestration.contracts import CapacityClass, EligibilityStage, RouteVerdict
    from verdict.orchestration.explain import explain_selection

    verdicts: list[RouteVerdict] = []
    for c in candidates:
        try:
            reached = EligibilityStage(c.reached) if c.reached else None
        except ValueError:
            reached = None
        try:
            failed = EligibilityStage(c.failed_stage) if c.failed_stage else None
        except ValueError:
            failed = None
        try:
            cap_class = (
                CapacityClass(c.capacity_class) if c.capacity_class else CapacityClass.UNKNOWN
            )
        except ValueError:
            cap_class = CapacityClass.UNKNOWN
        v = RouteVerdict(
            route_id=c.route_id,
            provider=c.provider,
            reached=reached,
            failed_stage=failed,
            reason=c.rejection_reason or "",
            capacity_class=cap_class,
            plan_label=c.plan_label or "",
            cooldown_until=c.cooldown_until,
            rank=c.rank,
            rank_components=c.rank_components,
            capability_tier=c.capability_tier,
            context_window=c.context_window,
            supports_tools=c.supports_tools,
            supports_structured_output=c.supports_structured_output,
            price=c.price,
        )
        verdicts.append(v)
    result = explain_selection(verdicts)
    return list(result.get("selected_because", []))


# ---------------------------------------------------------------------------
# Core: build from a sequence of RunEvent-like dicts
# ---------------------------------------------------------------------------


def _build_evaluations(events: Sequence[Mapping[str, Any]]) -> list[EligibilityEvaluation]:
    """Project eligibility + terminal events into EligibilityEvaluation records."""
    # Collect terminal events: node_id -> latest terminal data
    terminals: dict[str, dict[str, Any]] = {}
    for ev in events:
        if ev.get("type") == "terminal":
            nid = str(ev.get("node_id") or "")
            terminals[nid] = {
                "reported_model": str(ev.get("data", {}).get("reported_model") or ""),
                "session_ref": str(ev.get("data", {}).get("session_ref") or ""),
            }

    evals: list[EligibilityEvaluation] = []
    for ev in events:
        if ev.get("type") != "eligibility":
            continue
        node_id = str(ev.get("node_id") or "")
        data: Mapping[str, Any] = ev.get("data") or {}
        seq = int(ev.get("seq") or 0)
        at = str(ev.get("at") or "")

        funnel = _funnel_from_event(data)
        rejections = _rejections_from_event(data)
        selected_route = str(data["selected"]) if data.get("selected") else None
        candidates, omitted = _candidates_from_event(data)
        selected_because = _selected_because_from_candidates(candidates)

        terminal = terminals.get(node_id) or {}
        observed = terminal.get("reported_model") or None
        session_ref = terminal.get("session_ref") or None

        mismatch = bool(selected_route and observed and selected_route != observed)

        evals.append(
            EligibilityEvaluation(
                node_id=node_id,
                seq=seq,
                at=at,
                funnel=funnel,
                rejections=rejections,
                selected_route=selected_route,
                candidates=candidates,
                candidates_omitted=omitted,
                selected_because=selected_because,
                observed_route=observed if observed else None,
                session_ref=session_ref if session_ref else None,
                selected_observed_mismatch=mismatch,
            )
        )
    return evals


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Public: routing_view from recorded run
# ---------------------------------------------------------------------------


def routing_view(source: Path | Sequence[Mapping[str, Any]]) -> RoutingView:
    """Build a :class:`RoutingView` from a recorded run.

    ``source`` may be:
    * A ``Path`` to a run directory containing ``events.jsonl``.
    * A ``Path`` directly to an ``events.jsonl`` file.
    * A sequence of event dicts (already loaded).
    """
    if isinstance(source, Path):
        events_path = source / "events.jsonl" if source.is_dir() else source
        from verdict.orchestration.receipt import EventLog

        events_raw: list[Mapping[str, Any]] = [e.to_dict() for e in EventLog(events_path).read()]
    else:
        events_raw = list(source)

    evals = _build_evaluations(events_raw)
    return RoutingView(
        schema_version=SCHEMA_VERSION, source="recorded", generated_at=_now_iso(), evaluations=evals
    )


# ---------------------------------------------------------------------------
# Helpers: build candidates / rejections from RouteVerdict sequences
# (Mirrors the logic from verdict.orchestration.runtime PR #711; inlined here
# so this module works on the explorer-data branch before #711 merges.)
# ---------------------------------------------------------------------------

_MAX_CANDIDATES = 25


def _build_rejections_from_verdicts(verdicts: Sequence[Any]) -> dict[str, dict[str, int]]:
    """Aggregate rejection counts by stage and reason over all considered routes."""
    rejections: dict[str, dict[str, int]] = {}
    for v in verdicts:
        stage = getattr(v, "failed_stage", None)
        if stage is None:
            continue
        stage_key = stage.value if hasattr(stage, "value") else str(stage)
        reason = getattr(v, "reason", "") or "unknown"
        bucket = rejections.setdefault(stage_key, {})
        bucket[reason] = bucket.get(reason, 0) + 1
    return rejections


def _build_candidates_from_verdicts(
    verdicts: Sequence[Any], selected_route: str | None
) -> tuple[list[dict[str, Any]], int]:
    """Build at most _MAX_CANDIDATES candidate dicts; selected route always included."""
    total = len(verdicts)
    if total == 0:
        return [], 0

    selected_verdict: dict[str, Any] | None = None
    others: list[dict[str, Any]] = []

    for v in verdicts:
        d = v.to_dict() if hasattr(v, "to_dict") else {"route_id": str(getattr(v, "route_id", ""))}
        rid = d.get("route_id", "")
        if selected_route and rid == selected_route and selected_verdict is None:
            selected_verdict = d
        else:
            others.append(d)

    budget = _MAX_CANDIDATES - (1 if selected_verdict else 0)
    kept = others[:budget]
    result = [selected_verdict, *kept] if selected_verdict else kept[:_MAX_CANDIDATES]
    omitted = total - len(result)
    return result, omitted


# ---------------------------------------------------------------------------
# Public: routing_view from live inventory (read-only)
# ---------------------------------------------------------------------------


def routing_view_from_inventory(
    task_profile: Mapping[str, Any],
    *,
    gateway: str = "http://localhost:20128",
    api_key: str | None = None,
    state_path: Path | None = None,
    probe: bool = False,
    # Test seams: supply rows/connections directly without hitting the network.
    _rows_override: list[dict[str, Any]] | None = None,
    _conns_override: list[dict[str, Any]] | None = None,
) -> RoutingView:
    """Build a :class:`RoutingView` by running the real eligibility ladder read-only.

    ``task_profile`` keys (all optional, fall back to sane defaults):
    * ``required_capabilities``: list[str] - default ``["tools"]``
    * ``min_context_tokens``: int - default 32 000
    * ``coding``: bool - default True
    * ``reasoning``: bool - default False
    * ``frontier_worthy``: bool - default False
    * ``max_capability_tier``: int - default 3

    The ladder is called with ``evaluate`` (no probing) unless ``probe=True``,
    in which case ``select`` is used (max_probes_per_select=8).
    """
    import tempfile

    from verdict.orchestration.contracts import TaskRequirements
    from verdict.orchestration.eligibility import EligibilityLadder
    from verdict.orchestration.run import fetch_connections, fetch_inventory
    from verdict.subagent_selection import HealthResult

    if _rows_override is not None:
        inventory_rows = _rows_override
    else:
        inventory_rows = fetch_inventory(gateway, api_key=api_key)

    if _conns_override is not None:
        connections = _conns_override
    else:
        connections = fetch_connections(gateway, api_key=api_key)

    # Build TaskRequirements from profile dict.
    caps_raw = task_profile.get("required_capabilities", ["tools"])
    caps = frozenset(str(c) for c in caps_raw) if caps_raw else frozenset({"tools"})
    requirements = TaskRequirements(
        required_capabilities=caps,
        min_context_tokens=int(task_profile.get("min_context_tokens", 32_000)),
        coding=bool(task_profile.get("coding", True)),
        reasoning=bool(task_profile.get("reasoning", False)),
        frontier_worthy=bool(task_profile.get("frontier_worthy", False)),
        max_capability_tier=int(task_profile.get("max_capability_tier", 3)),
    )

    def _null_probe(route_id: str) -> HealthResult:
        return HealthResult(healthy=True, category="")

    with tempfile.TemporaryDirectory() as _td:
        sp = state_path or Path(_td) / "elig_state.json"

        ladder = EligibilityLadder(
            inventory_rows=inventory_rows, connections=connections, probe=_null_probe, state_path=sp
        )
        now = datetime.now(timezone.utc)
        if probe:
            _selected_v, verdicts = ladder.select(requirements, now=now)
        else:
            verdicts = ladder.evaluate(requirements, now=now)

        # Reconstruct event-like dicts from verdicts so we can reuse
        # _build_evaluations / _candidates_from_event.
        selected_v = next(
            (
                v
                for v in verdicts
                if hasattr(v, "reached") and v.reached is not None and v.reached.value == "SELECTED"
            ),
            None,
        )
        selected_route = selected_v.route_id if selected_v is not None else None

        cands_list, omitted = _build_candidates_from_verdicts(verdicts, selected_route)
        rejections_raw = _build_rejections_from_verdicts(verdicts)

        # Compute funnel counts from verdicts directly.
        funnel_counts: dict[str, int] = {s: 0 for s in _FUNNEL_STAGES}
        _order = list(_FUNNEL_STAGES)
        for v in verdicts:
            reached = getattr(v, "reached", None)
            if reached is None:
                continue
            idx = _order.index(reached.value)
            for s in _order[: idx + 1]:
                funnel_counts[s] += 1

        candidates_records, _ = _candidates_from_event(
            {"candidates": cands_list, "candidates_omitted": omitted}
        )
        selected_because = _selected_because_from_candidates(candidates_records)

        # Normalize rejections to dict[str, dict[str, int]]
        norm_rejections: dict[str, dict[str, int]] = {}
        for stage, reasons in rejections_raw.items():
            norm_rejections[str(stage)] = {str(r): int(c) for r, c in reasons.items()}

        evaluation = EligibilityEvaluation(
            node_id="",
            seq=0,
            at=_now_iso(),
            funnel=funnel_counts,
            rejections=norm_rejections,
            selected_route=selected_route,
            candidates=candidates_records,
            candidates_omitted=omitted,
            selected_because=selected_because,
            observed_route=None,
            session_ref=None,
            selected_observed_mismatch=False,
        )

    return RoutingView(
        schema_version=SCHEMA_VERSION,
        source="inventory",
        generated_at=_now_iso(),
        evaluations=[evaluation],
    )


__all__ = [
    "SCHEMA_VERSION",
    "CandidateRecord",
    "EligibilityEvaluation",
    "RoutingView",
    "routing_view",
    "routing_view_from_inventory",
]
