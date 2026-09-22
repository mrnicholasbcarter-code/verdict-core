"""BOD-156: Verdict-selected Prime controller launch contracts and verification.

This module owns immutable controller-launch contracts and the fail-closed
assembly/validation/observed-identity seam used by the supervisor.

Authority boundaries (unchanged):
- BOD-104 remains final automatic execution-path authority.
- BOD-142/143/119/144 remain eligibility, ContextPlan, continuity, and receipt
  authorities outside this module's synthesis role.
- Automatic mode never synthesizes live eligibility or offers here. It requires
  an already authoritative persisted BOD-104-based decision and a pre-launch
  persisted RoutingReceiptV1 reference.
- Explicit override requires both provider and model and records provenance.
- No hidden fallback, no ``auto/*``, no opaque/default identities.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

ControllerMode = Literal["automatic", "override"]


class ControllerLaunchError(Exception):
    """Named fail-closed refusal for controller launch."""

    def __init__(self, reason_code: str, detail: str) -> None:
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(f"{reason_code}: {detail}")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_nonempty(value: str, field_name: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ControllerLaunchError("invalid_field", f"{field_name} must be non-empty")
    return text


def _reject_auto_or_opaque(value: str, field_name: str) -> str:
    text = _require_nonempty(value, field_name)
    lowered = text.lower()
    if lowered.startswith("auto/") or lowered in {"auto", "default", "*"}:
        raise ControllerLaunchError(
            "forbidden_identity",
            f"{field_name} rejects auto/default/opaque identity: {text!r}",
        )
    if "/" not in text and field_name.endswith("provider"):
        # providers may be bare tokens; models often are too — only block known opaques
        pass
    if text.startswith("auto/") or "/auto/" in lowered:
        raise ControllerLaunchError(
            "forbidden_identity",
            f"{field_name} rejects auto/* identity: {text!r}",
        )
    return text


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _digest(payload: Mapping[str, Any]) -> str:
    return sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ControllerMission:
    """Mission/story/attempt burden contract for controller selection."""

    mission_id: str
    story_id: str
    attempt_id: str
    objective: str
    spend_ceiling_usd: float | None = None
    security_floor: str | None = None
    required_tools: tuple[str, ...] = ()
    required_mcp: tuple[str, ...] = ()
    orchestration_burden: str | None = None
    context_burden: str | None = None
    proof_burden: str | None = None
    durable_context_refs: tuple[str, ...] = ()
    task_profile_digest: str | None = None
    task_slice_digest: str | None = None
    trajectory_digest: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "mission_id", _require_nonempty(self.mission_id, "mission_id"))
        object.__setattr__(self, "story_id", _require_nonempty(self.story_id, "story_id"))
        object.__setattr__(self, "attempt_id", _require_nonempty(self.attempt_id, "attempt_id"))
        object.__setattr__(self, "objective", _require_nonempty(self.objective, "objective"))


@dataclass(frozen=True)
class OperatorOverride:
    """CLI-created explicit Prime provider/model override with provenance.

    Mission/repo/child text cannot construct a trusted override. Both provider
    and model are required. ``auto/*`` and opaque identities are rejected.
    """

    provider: str
    model: str
    source: str
    reason: str
    timestamp: str
    reasoning_effort: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _reject_auto_or_opaque(self.provider, "provider"))
        object.__setattr__(self, "model", _reject_auto_or_opaque(self.model, "model"))
        object.__setattr__(self, "source", _require_nonempty(self.source, "source"))
        object.__setattr__(self, "reason", _require_nonempty(self.reason, "reason"))
        object.__setattr__(self, "timestamp", _require_nonempty(self.timestamp, "timestamp"))
        if self.source != "cli":
            raise ControllerLaunchError(
                "untrusted_override_source",
                "OperatorOverride.source must be 'cli'; mission/repo text cannot forge it",
            )
        if self.reasoning_effort is not None:
            effort = _require_nonempty(self.reasoning_effort, "reasoning_effort")
            if effort.lower().startswith("auto"):
                raise ControllerLaunchError(
                    "forbidden_identity",
                    f"reasoning_effort rejects auto identity: {effort!r}",
                )
            object.__setattr__(self, "reasoning_effort", effort)


@dataclass(frozen=True)
class PrimeLaunchTarget:
    """Trusted binding from a concrete upstream route to Prime CLI identity.

    Upstream provider/model and Prime CLI provider/model remain separate.
    """

    upstream_provider: str
    upstream_model: str
    prime_provider: str
    prime_model: str
    binding_digest: str
    reasoning_effort: str | None = None
    binding_evidence_ref: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "upstream_provider", _reject_auto_or_opaque(self.upstream_provider, "upstream_provider")
        )
        object.__setattr__(
            self, "upstream_model", _reject_auto_or_opaque(self.upstream_model, "upstream_model")
        )
        object.__setattr__(
            self, "prime_provider", _reject_auto_or_opaque(self.prime_provider, "prime_provider")
        )
        object.__setattr__(
            self, "prime_model", _reject_auto_or_opaque(self.prime_model, "prime_model")
        )
        object.__setattr__(
            self, "binding_digest", _require_nonempty(self.binding_digest, "binding_digest")
        )
        if self.reasoning_effort is not None:
            effort = _require_nonempty(self.reasoning_effort, "reasoning_effort")
            if effort.lower().startswith("auto"):
                raise ControllerLaunchError(
                    "forbidden_identity",
                    f"reasoning_effort rejects auto identity: {effort!r}",
                )
            object.__setattr__(self, "reasoning_effort", effort)


@dataclass(frozen=True)
class ObservedControllerIdentity:
    """Observed owned Prime root identity from the live roster."""

    session_id: str
    session_file: str
    runtime_kind: str
    rlm_depth: int
    provider: str
    model: str
    thinking_level: str | None
    observed_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_id", _require_nonempty(self.session_id, "session_id"))
        object.__setattr__(self, "session_file", _require_nonempty(self.session_file, "session_file"))
        object.__setattr__(self, "runtime_kind", _require_nonempty(self.runtime_kind, "runtime_kind"))
        object.__setattr__(self, "provider", _require_nonempty(self.provider, "provider"))
        object.__setattr__(self, "model", _require_nonempty(self.model, "model"))
        object.__setattr__(self, "observed_at", _require_nonempty(self.observed_at, "observed_at"))
        if self.rlm_depth < 0:
            raise ControllerLaunchError("invalid_field", "rlm_depth must be >= 0")


@dataclass(frozen=True)
class ControllerLaunchDecision:
    """Versioned controller launch decision. Append-only receipt remains authority."""

    version: str
    mission_id: str
    story_id: str
    attempt_id: str
    mode: ControllerMode
    task_profile_digest: str
    task_slice_digest: str
    trajectory_digest: str
    pool_ref: str
    evidence_refs: tuple[str, ...]
    execution_path_decision_digest: str
    selected_upstream_route: str
    prime_target: PrimeLaunchTarget
    context_plan_digest: str
    context_pack_digest: str
    context_receipt_digest: str
    selected_prompt_digest: str
    constituent_freshness: Mapping[str, str]
    minimum_expiry: str
    session_decision: str
    why_selected: str
    canonical_digest: str
    override_provenance: Mapping[str, str] | None = None
    routing_receipt_ref: str | None = None
    decided_at: str = field(default_factory=lambda: _utc_now().isoformat())

    def __post_init__(self) -> None:
        if self.version != "controller_launch.v1":
            raise ControllerLaunchError(
                "unsupported_version",
                f"unsupported ControllerLaunchDecision.version: {self.version!r}",
            )
        if self.mode not in ("automatic", "override"):
            raise ControllerLaunchError("invalid_mode", f"mode must be automatic|override, got {self.mode!r}")
        object.__setattr__(self, "mission_id", _require_nonempty(self.mission_id, "mission_id"))
        object.__setattr__(self, "story_id", _require_nonempty(self.story_id, "story_id"))
        object.__setattr__(self, "attempt_id", _require_nonempty(self.attempt_id, "attempt_id"))
        for name in (
            "task_profile_digest",
            "task_slice_digest",
            "trajectory_digest",
            "pool_ref",
            "execution_path_decision_digest",
            "selected_upstream_route",
            "context_plan_digest",
            "context_pack_digest",
            "context_receipt_digest",
            "selected_prompt_digest",
            "minimum_expiry",
            "session_decision",
            "why_selected",
            "canonical_digest",
        ):
            object.__setattr__(self, name, _require_nonempty(getattr(self, name), name))
        if self.mode == "automatic" and not self.routing_receipt_ref:
            raise ControllerLaunchError(
                "missing_receipt_ref",
                "automatic mode requires pre-launch persisted routing_receipt_ref",
            )
        if self.mode == "override" and not self.override_provenance:
            raise ControllerLaunchError(
                "missing_override_provenance",
                "override mode requires override_provenance",
            )
        # Reject auto/* in selected route / digests surfaces that carry identity
        if self.selected_upstream_route.lower().startswith("auto/") or "/auto/" in self.selected_upstream_route.lower():
            raise ControllerLaunchError(
                "forbidden_identity",
                f"selected_upstream_route rejects auto/*: {self.selected_upstream_route!r}",
            )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return payload


def compute_decision_digest(decision_body: Mapping[str, Any]) -> str:
    """Canonical digest over decision body excluding canonical_digest itself."""
    body = {k: v for k, v in decision_body.items() if k != "canonical_digest"}
    return _digest(body)


def build_prime_argv(
    *,
    prime: str,
    target: PrimeLaunchTarget,
    session_dir: str | Path,
    prompt: str,
    include_thinking: bool = True,
) -> list[str]:
    """Exact Prime argv from an approved target. No auto/*, no silent downgrade."""
    if not prompt.strip():
        raise ControllerLaunchError("invalid_prompt", "compiled prompt must be non-empty")
    argv = [
        prime,
        "--provider",
        target.prime_provider,
        "--model",
        target.prime_model,
        "--session-dir",
        str(session_dir),
    ]
    if include_thinking and target.reasoning_effort:
        argv.extend(["--thinking", target.reasoning_effort])
    argv.append(prompt)
    return argv


def _parse_iso(ts: str) -> datetime:
    text = ts.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ControllerLaunchError("invalid_timestamp", f"unparseable timestamp: {ts!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def validate_controller_decision(
    decision: ControllerLaunchDecision,
    *,
    mission: ControllerMission,
    attempt_id: str,
    now: datetime | None = None,
) -> ControllerLaunchDecision:
    """Validate a persisted decision against the mission/attempt and freshness."""
    now = now or _utc_now()
    if decision.attempt_id != attempt_id:
        raise ControllerLaunchError(
            "attempt_mismatch",
            f"decision attempt_id {decision.attempt_id!r} != {attempt_id!r}",
        )
    if decision.mission_id != mission.mission_id:
        raise ControllerLaunchError(
            "mission_mismatch",
            f"decision mission_id {decision.mission_id!r} != {mission.mission_id!r}",
        )
    if decision.story_id != mission.story_id:
        raise ControllerLaunchError(
            "story_mismatch",
            f"decision story_id {decision.story_id!r} != {mission.story_id!r}",
        )

    body = decision.to_dict()
    expected = compute_decision_digest(body)
    if decision.canonical_digest != expected:
        raise ControllerLaunchError(
            "digest_mismatch",
            "ControllerLaunchDecision.canonical_digest does not match canonical body",
        )

    expiry = _parse_iso(decision.minimum_expiry)
    if now >= expiry:
        raise ControllerLaunchError(
            "stale_decision",
            f"decision expired at {decision.minimum_expiry} (now={now.isoformat()})",
        )

    if decision.mode == "automatic":
        if not decision.routing_receipt_ref:
            raise ControllerLaunchError(
                "missing_receipt_ref",
                "automatic mode requires pre-launch persisted routing_receipt_ref",
            )
        if not decision.execution_path_decision_digest:
            raise ControllerLaunchError(
                "missing_bod104_decision",
                "automatic mode requires authoritative BOD-104 execution_path_decision_digest",
            )
    elif decision.mode == "override":
        prov = decision.override_provenance or {}
        for key in ("provider", "model", "source", "reason", "timestamp"):
            if not str(prov.get(key, "")).strip():
                raise ControllerLaunchError(
                    "missing_override_provenance",
                    f"override provenance missing {key}",
                )
        if prov.get("source") != "cli":
            raise ControllerLaunchError(
                "untrusted_override_source",
                "override provenance source must be cli",
            )
        if (
            prov["provider"] != decision.prime_target.prime_provider
            or prov["model"] != decision.prime_target.prime_model
        ):
            raise ControllerLaunchError(
                "override_identity_mismatch",
                "override provenance provider/model must match PrimeLaunchTarget",
            )
    else:
        raise ControllerLaunchError("invalid_mode", f"unknown mode {decision.mode!r}")

    # No auto/* anywhere on the approved Prime identity
    for label, value in (
        ("prime_provider", decision.prime_target.prime_provider),
        ("prime_model", decision.prime_target.prime_model),
        ("upstream_provider", decision.prime_target.upstream_provider),
        ("upstream_model", decision.prime_target.upstream_model),
    ):
        _reject_auto_or_opaque(value, label)

    return decision


def _session_file_under_root(session_file: str | Path, session_dir: str | Path) -> bool:
    try:
        file_path = Path(session_file).resolve()
        root = Path(session_dir).resolve()
    except OSError:
        return False
    try:
        file_path.relative_to(root)
        return True
    except ValueError:
        return False


def select_owned_root_sessions(
    roster: Mapping[str, Any] | Sequence[Any] | None,
    *,
    session_dir: str | Path,
) -> list[dict[str, Any]]:
    """Return owned live root sessions under session_dir.

    Ignores unrelated well-formed ``draft`` rows. Rejects malformed owned live
    identity rows. Does not stop unrelated sessions.
    """
    if roster is None:
        return []

    if isinstance(roster, Mapping):
        sessions = roster.get("sessions")
        if sessions is None:
            sessions = roster.get("session")
        if sessions is None and all(isinstance(v, dict) for v in roster.values()):
            # sometimes a map of id -> session
            sessions = list(roster.values())
        if sessions is None:
            raise ControllerLaunchError(
                "malformed_roster",
                "roster must contain a sessions list or mapping",
            )
    elif isinstance(roster, Sequence) and not isinstance(roster, (str, bytes)):
        sessions = list(roster)
    else:
        raise ControllerLaunchError("malformed_roster", "roster must be mapping or sequence")

    if not isinstance(sessions, Sequence) or isinstance(sessions, (str, bytes)):
        raise ControllerLaunchError("malformed_roster", "sessions must be a list")

    owned: list[dict[str, Any]] = []
    for raw in sessions:
        if not isinstance(raw, Mapping):
            raise ControllerLaunchError("malformed_roster", "session row must be an object")
        status = str(raw.get("status") or raw.get("state") or "").lower()
        # Ignore unrelated well-formed drafts
        if status == "draft":
            session_file = raw.get("sessionFile") or raw.get("session_file")
            if session_file and _session_file_under_root(str(session_file), session_dir):
                # draft under owned root is still not a live root identity
                continue
            continue

        session_file = raw.get("sessionFile") or raw.get("session_file")
        if not session_file:
            # Unrelated rows without files are ignored if they don't claim ownership
            continue
        if not _session_file_under_root(str(session_file), session_dir):
            continue

        # Owned live row: require well-formed identity fields
        session_id = raw.get("id") or raw.get("sessionId") or raw.get("session_id")
        runtime_kind = raw.get("runtimeKind") or raw.get("runtime_kind")
        rlm_depth = raw.get("rlmDepth") if "rlmDepth" in raw else raw.get("rlm_depth")
        model_obj = raw.get("model")
        if isinstance(model_obj, Mapping):
            provider = model_obj.get("provider")
            model_id = model_obj.get("id") or model_obj.get("model")
            thinking = model_obj.get("thinkingLevel") or model_obj.get("thinking_level")
        else:
            provider = raw.get("provider")
            model_id = raw.get("model")
            thinking = raw.get("thinkingLevel") or raw.get("thinking_level")

        missing = [
            name
            for name, value in (
                ("session_id", session_id),
                ("runtime_kind", runtime_kind),
                ("rlm_depth", rlm_depth),
                ("provider", provider),
                ("model", model_id),
            )
            if value is None or (isinstance(value, str) and not value.strip())
        ]
        if missing:
            raise ControllerLaunchError(
                "malformed_owned_identity",
                f"owned live session missing required fields: {', '.join(missing)}",
            )
        try:
            depth = int(rlm_depth)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ControllerLaunchError(
                "malformed_owned_identity",
                f"owned live session rlm_depth is not an int: {rlm_depth!r}",
            ) from exc

        owned.append(
            {
                "session_id": str(session_id),
                "session_file": str(session_file),
                "runtime_kind": str(runtime_kind),
                "rlm_depth": depth,
                "provider": str(provider),
                "model": str(model_id),
                "thinking_level": None if thinking is None else str(thinking),
                "raw": dict(raw),
            }
        )
    return owned


def observe_owned_controller_identity(
    roster: Mapping[str, Any] | Sequence[Any] | None,
    *,
    session_dir: str | Path,
    now: datetime | None = None,
) -> ObservedControllerIdentity:
    """Identify exactly one owned root under session_dir from a roster snapshot.

    Requires ``runtimeKind=top-level`` and ``rlmDepth=0``. Missing, ambiguous, or
    mismatched cardinality fails closed. Unrelated drafts are ignored.
    """
    now = now or _utc_now()
    owned = select_owned_root_sessions(roster, session_dir=session_dir)
    roots = [
        row
        for row in owned
        if row["runtime_kind"] == "top-level" and row["rlm_depth"] == 0
    ]
    if not roots:
        raise ControllerLaunchError(
            "missing_owned_root",
            f"no owned top-level rlmDepth=0 session under {session_dir}",
        )
    if len(roots) > 1:
        ids = [r["session_id"] for r in roots]
        raise ControllerLaunchError(
            "ambiguous_owned_root",
            f"multiple owned roots under {session_dir}: {ids}",
        )
    row = roots[0]
    return ObservedControllerIdentity(
        session_id=row["session_id"],
        session_file=row["session_file"],
        runtime_kind=row["runtime_kind"],
        rlm_depth=row["rlm_depth"],
        provider=row["provider"],
        model=row["model"],
        thinking_level=row["thinking_level"],
        observed_at=now.isoformat(),
    )


def verify_observed_controller_identity(
    decision: ControllerLaunchDecision,
    observed: ObservedControllerIdentity,
    *,
    session_dir: str | Path | None = None,
) -> ObservedControllerIdentity:
    """Exact match of approved Prime provider/model/thinking vs observed roster.

    ``models.json`` and argv are not observed identity. Mismatch fails closed.
    """
    if session_dir is not None and not _session_file_under_root(observed.session_file, session_dir):
        raise ControllerLaunchError(
            "session_boundary_violation",
            f"observed sessionFile {observed.session_file!r} escapes owned root {session_dir}",
        )
    if observed.runtime_kind != "top-level":
        raise ControllerLaunchError(
            "identity_mismatch",
            f"runtime_kind must be top-level, got {observed.runtime_kind!r}",
        )
    if observed.rlm_depth != 0:
        raise ControllerLaunchError(
            "identity_mismatch",
            f"rlm_depth must be 0, got {observed.rlm_depth}",
        )

    target = decision.prime_target
    if observed.provider != target.prime_provider:
        raise ControllerLaunchError(
            "identity_mismatch",
            f"provider observed={observed.provider!r} expected={target.prime_provider!r}",
        )
    if observed.model != target.prime_model:
        raise ControllerLaunchError(
            "identity_mismatch",
            f"model observed={observed.model!r} expected={target.prime_model!r}",
        )

    expected_thinking = target.reasoning_effort
    if expected_thinking is None:
        # Unsupported/not-selected reasoning must remain omitted; treat empty/None as match
        if observed.thinking_level not in (None, "", "none", "null"):
            raise ControllerLaunchError(
                "identity_mismatch",
                f"thinking must be omitted/empty, observed={observed.thinking_level!r}",
            )
    else:
        if observed.thinking_level != expected_thinking:
            raise ControllerLaunchError(
                "identity_mismatch",
                f"thinking observed={observed.thinking_level!r} expected={expected_thinking!r}",
            )
    return observed


@dataclass(frozen=True)
class PersistedAuthoritativeDecision:
    """Already-authoritative BOD-104 decision + pre-launch receipt reference.

    Automatic mode consumes this; it does not synthesize eligibility or offers.
    """

    execution_path_decision_digest: str
    selected_upstream_route: str
    prime_target: PrimeLaunchTarget
    context_plan_digest: str
    context_pack_digest: str
    context_receipt_digest: str
    selected_prompt_digest: str
    routing_receipt_ref: str
    pool_ref: str
    evidence_refs: tuple[str, ...]
    constituent_freshness: Mapping[str, str]
    minimum_expiry: str
    session_decision: str
    why_selected: str
    task_profile_digest: str
    task_slice_digest: str
    trajectory_digest: str

    def __post_init__(self) -> None:
        for name in (
            "execution_path_decision_digest",
            "selected_upstream_route",
            "context_plan_digest",
            "context_pack_digest",
            "context_receipt_digest",
            "selected_prompt_digest",
            "routing_receipt_ref",
            "pool_ref",
            "minimum_expiry",
            "session_decision",
            "why_selected",
            "task_profile_digest",
            "task_slice_digest",
            "trajectory_digest",
        ):
            object.__setattr__(self, name, _require_nonempty(getattr(self, name), name))
        if not self.evidence_refs:
            raise ControllerLaunchError(
                "missing_evidence",
                "persisted authoritative decision requires evidence_refs",
            )
        if self.selected_upstream_route.lower().startswith("auto/") or self.selected_upstream_route.lower() in {
            "auto",
            "default",
        }:
            raise ControllerLaunchError(
                "forbidden_identity",
                f"selected_upstream_route rejects auto/default: {self.selected_upstream_route!r}",
            )


def decide_controller_launch(
    mission: ControllerMission,
    *,
    persisted: PersistedAuthoritativeDecision | None = None,
    override: OperatorOverride | None = None,
    now: datetime | None = None,
) -> ControllerLaunchDecision:
    """Assemble a ControllerLaunchDecision fail-closed.

    Automatic mode (override is None):
      Requires an already authoritative persisted BOD-104-based decision and a
      pre-launch persisted receipt reference. Does **not** synthesize live
      eligibility or offers.

    Override mode:
      Requires both provider and model with CLI provenance. Does not invent
      ``auto/*`` or one-sided identities.
    """
    now = now or _utc_now()

    if override is not None and persisted is not None:
        # Override still needs the same hard evidence envelope when provided; the
        # selected Prime identity must match the override pair.
        if (
            override.provider != persisted.prime_target.prime_provider
            or override.model != persisted.prime_target.prime_model
        ):
            raise ControllerLaunchError(
                "override_target_mismatch",
                "explicit override provider/model must match persisted eligible PrimeLaunchTarget",
            )
        if override.reasoning_effort is not None:
            if persisted.prime_target.reasoning_effort is None:
                raise ControllerLaunchError(
                    "unsupported_reasoning",
                    "override requested reasoning_effort but target has no supported evidence",
                )
            if override.reasoning_effort != persisted.prime_target.reasoning_effort:
                raise ControllerLaunchError(
                    "reasoning_mismatch",
                    "override reasoning_effort must match supported target evidence exactly",
                )
        mode: ControllerMode = "override"
        provenance: Mapping[str, str] | None = {
            "provider": override.provider,
            "model": override.model,
            "source": override.source,
            "reason": override.reason,
            "timestamp": override.timestamp,
            **(
                {"reasoning_effort": override.reasoning_effort}
                if override.reasoning_effort is not None
                else {}
            ),
        }
        target = persisted.prime_target
        auth = persisted
    elif override is not None:
        raise ControllerLaunchError(
            "override_requires_eligible_target",
            "explicit override requires an eligible persisted PrimeLaunchTarget binding; "
            "do not synthesize live eligibility here",
        )
    else:
        mode = "automatic"
        provenance = None
        if persisted is None:
            raise ControllerLaunchError(
                "missing_authoritative_decision",
                "automatic mode requires an already authoritative persisted BOD-104 "
                "decision and pre-launch persisted receipt reference; refuse to synthesize",
            )
        auth = persisted
        target = persisted.prime_target
        if not auth.routing_receipt_ref:
            raise ControllerLaunchError(
                "missing_receipt_ref",
                "automatic mode requires pre-launch persisted routing_receipt_ref",
            )

    body: dict[str, Any] = {
        "version": "controller_launch.v1",
        "mission_id": mission.mission_id,
        "story_id": mission.story_id,
        "attempt_id": mission.attempt_id,
        "mode": mode,
        "task_profile_digest": auth.task_profile_digest,
        "task_slice_digest": auth.task_slice_digest,
        "trajectory_digest": auth.trajectory_digest,
        "pool_ref": auth.pool_ref,
        "evidence_refs": list(auth.evidence_refs),
        "execution_path_decision_digest": auth.execution_path_decision_digest,
        "selected_upstream_route": auth.selected_upstream_route,
        "prime_target": asdict(target),
        "context_plan_digest": auth.context_plan_digest,
        "context_pack_digest": auth.context_pack_digest,
        "context_receipt_digest": auth.context_receipt_digest,
        "selected_prompt_digest": auth.selected_prompt_digest,
        "constituent_freshness": dict(auth.constituent_freshness),
        "minimum_expiry": auth.minimum_expiry,
        "session_decision": auth.session_decision,
        "why_selected": auth.why_selected,
        "override_provenance": dict(provenance) if provenance else None,
        "routing_receipt_ref": auth.routing_receipt_ref,
        "decided_at": now.isoformat(),
    }
    digest = compute_decision_digest(body)
    decision = ControllerLaunchDecision(
        version="controller_launch.v1",
        mission_id=mission.mission_id,
        story_id=mission.story_id,
        attempt_id=mission.attempt_id,
        mode=mode,
        task_profile_digest=auth.task_profile_digest,
        task_slice_digest=auth.task_slice_digest,
        trajectory_digest=auth.trajectory_digest,
        pool_ref=auth.pool_ref,
        evidence_refs=tuple(auth.evidence_refs),
        execution_path_decision_digest=auth.execution_path_decision_digest,
        selected_upstream_route=auth.selected_upstream_route,
        prime_target=target,
        context_plan_digest=auth.context_plan_digest,
        context_pack_digest=auth.context_pack_digest,
        context_receipt_digest=auth.context_receipt_digest,
        selected_prompt_digest=auth.selected_prompt_digest,
        constituent_freshness=dict(auth.constituent_freshness),
        minimum_expiry=auth.minimum_expiry,
        session_decision=auth.session_decision,
        why_selected=auth.why_selected,
        canonical_digest=digest,
        override_provenance=dict(provenance) if provenance else None,
        routing_receipt_ref=auth.routing_receipt_ref,
        decided_at=now.isoformat(),
    )
    return validate_controller_decision(
        decision, mission=mission, attempt_id=mission.attempt_id, now=now
    )


def fence_owned_root_plan(
    *,
    session_dir: str | Path,
    observed: ObservedControllerIdentity | None,
    reason_code: str,
) -> dict[str, Any]:
    """Describe fail-closed fencing actions for an owned root. Does not execute."""
    return {
        "action": "fence_owned_root",
        "session_dir": str(session_dir),
        "session_id": None if observed is None else observed.session_id,
        "reason_code": reason_code,
        "stop_unrelated": False,
        "confirm_absence": True,
    }


__all__ = [
    "ControllerLaunchDecision",
    "ControllerLaunchError",
    "ControllerMission",
    "ObservedControllerIdentity",
    "OperatorOverride",
    "PersistedAuthoritativeDecision",
    "PrimeLaunchTarget",
    "build_prime_argv",
    "compute_decision_digest",
    "decide_controller_launch",
    "fence_owned_root_plan",
    "observe_owned_controller_identity",
    "select_owned_root_sessions",
    "validate_controller_decision",
    "verify_observed_controller_identity",
]
