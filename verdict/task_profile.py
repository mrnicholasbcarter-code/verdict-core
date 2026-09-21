"""Deterministic task profile: the pre-hydration contract for routing.

One immutable :class:`TaskProfile` is derived from explicit task evidence —
request fields, derived capability requirements, and the worthiness hint —
*before* any candidate admission or context planning happens. Downstream
authorities consume the profile rather than re-interpreting raw task text:

- ``spend_policy`` is an explicit economic policy group (who may compete),
  never a score bonus and never inferred from model names;
- ``required_capabilities`` come from :func:`~verdict.capability_gate.derive_requirements`,
  which invents nothing from task text;
- ``digest`` makes the profile replayable and receipt-joinable.

This extends — does not replace — ``TaskFingerprint`` (candidate-pool-local)
and ``classify_worthiness`` (which becomes one signal inside the profile).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Spend policy groups: policy decides WHO MAY COMPETE; utility decides who wins
# inside the allowed economic set. There is no silent paid escalation: every
# transition out of the free set requires an explicit policy that permits it.
SPEND_FREE_ONLY = "free_only"
SPEND_FREE_PREFERRED = "free_preferred"
SPEND_PAID_ALLOWED = "paid_allowed"
SPEND_FRONTIER_REQUIRED = "frontier_required"

SPEND_POLICIES = frozenset(
    {SPEND_FREE_ONLY, SPEND_FREE_PREFERRED, SPEND_PAID_ALLOWED, SPEND_FRONTIER_REQUIRED}
)

# Canonical default: free-first unless the request says otherwise.
DEFAULT_SPEND_POLICY = SPEND_FREE_PREFERRED

_POLICY_ALIASES = {
    "free-only": SPEND_FREE_ONLY,
    "freeonly": SPEND_FREE_ONLY,
    "free-preferred": SPEND_FREE_PREFERRED,
    "freefirst": SPEND_FREE_PREFERRED,
    "free-first": SPEND_FREE_PREFERRED,
    "free_preferred": SPEND_FREE_PREFERRED,
    "paid-allowed": SPEND_PAID_ALLOWED,
    "paid_allowed": SPEND_PAID_ALLOWED,
    "frontier-required": SPEND_FRONTIER_REQUIRED,
    "frontier_required": SPEND_FRONTIER_REQUIRED,
}


class TaskProfileError(ValueError):
    """Raised when a task profile cannot be derived under fail-closed rules."""


def normalize_spend_policy(raw: str | None) -> str:
    """Normalize an explicit spend-policy request; refuse unknown values.

    Missing/empty means the canonical free-first default. An unrecognized
    value is a refusal, never a guess: silently treating ``"cheapest_possible"``
    as paid_allowed would be exactly the silent economic escalation the policy
    group exists to prevent.
    """
    if raw is None:
        return DEFAULT_SPEND_POLICY
    if not isinstance(raw, str):
        raise TaskProfileError(f"spend_policy must be a string, got {type(raw).__name__}")
    key = re.sub(r"\s+", "-", raw.strip().lower())
    if not key:
        return DEFAULT_SPEND_POLICY
    if key in _POLICY_ALIASES:
        return _POLICY_ALIASES[key]
    if key.replace("-", "_") in SPEND_POLICIES:
        return key.replace("-", "_")
    raise TaskProfileError(f"unknown spend_policy {raw!r}; must be one of {sorted(SPEND_POLICIES)}")


@dataclass(frozen=True)
class TaskProfile:
    """Structured, replayable description of the requested work.

    Attributes:
        digest: ``sha256:`` over the canonical profile payload.
        task_family: coarse family from explicit evidence (``general`` when unknown).
        required_capabilities: hard capability names (tools/vision/structured...).
        min_context: hard minimum context window when the request states one.
        spend_policy: normalized economic policy group.
        task_class_hint: untrusted client worthiness hint (``worthy``/``ordinary``/None).
        requirements_reasons: provenance strings for each derived requirement.
        objective_preview: bounded task-text preview for receipts (never the oracle).
    """

    digest: str
    task_family: str
    required_capabilities: tuple[str, ...]
    min_context: int | None
    spend_policy: str
    task_class_hint: str | None
    requirements_reasons: tuple[str, ...]
    objective_preview: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "task_family": self.task_family,
            "required_capabilities": list(self.required_capabilities),
            "min_context": self.min_context,
            "spend_policy": self.spend_policy,
            "task_class_hint": self.task_class_hint,
            "requirements_reasons": list(self.requirements_reasons),
            "objective_preview": self.objective_preview,
        }


def profile_task(
    task: str, *, context: Mapping[str, Any] | None = None, requirements: Any | None = None
) -> TaskProfile:
    """Derive the profile once, deterministically, before admission/planning.

    Task text is not a capability oracle: capabilities come only from explicit
    request fields via ``derive_requirements``. The worthiness hint is carried
    untrusted; server classification remains authoritative downstream.
    """
    from verdict.capability_gate import derive_requirements  # lazy: avoids import cycle

    if not isinstance(task, str):
        raise TaskProfileError("task must be a string")
    payload = dict(context) if isinstance(context, Mapping) else {}
    reqs = requirements or derive_requirements(task, payload)
    spend_policy = normalize_spend_policy(
        payload.get("spend_policy") if isinstance(payload.get("spend_policy"), str | None) else None
    )
    hint_raw = payload.get("task_class")
    hint = hint_raw.strip().lower() if isinstance(hint_raw, str) else None
    if hint not in (None, "worthy", "ordinary"):
        hint = None
    family_raw = payload.get("task_family")
    family = (
        family_raw.strip().lower()
        if isinstance(family_raw, str) and family_raw.strip()
        else "general"
    )
    preview = re.sub(r"\s+", " ", task.strip())[:120]
    canonical = {
        "task_family": family,
        "required_capabilities": list(reqs.names),
        "min_context": reqs.min_context,
        "spend_policy": spend_policy,
        "task_class_hint": hint,
        "requirements_reasons": list(reqs.reasons),
        "objective": task.strip(),
    }
    digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )
    return TaskProfile(
        digest=digest,
        task_family=family,
        required_capabilities=tuple(reqs.names),
        min_context=reqs.min_context,
        spend_policy=spend_policy,
        task_class_hint=hint,
        requirements_reasons=tuple(reqs.reasons),
        objective_preview=preview,
    )


__all__ = [
    "DEFAULT_SPEND_POLICY",
    "SPEND_FREE_ONLY",
    "SPEND_FREE_PREFERRED",
    "SPEND_FRONTIER_REQUIRED",
    "SPEND_PAID_ALLOWED",
    "SPEND_POLICIES",
    "TaskProfile",
    "TaskProfileError",
    "normalize_spend_policy",
    "profile_task",
]
