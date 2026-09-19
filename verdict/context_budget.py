"""Context Budget Governor — total usable context account/allocate/enforce (BOD-125).

Treats the **entire** model context window as a scarce execution resource, not
merely pack include-budget.  Builds on :mod:`verdict.context_pack` token
estimates and provenance hygiene; does **not** replace ContextPackCompiler,
Context Intelligence Fabric, or BOD-69/81 compaction.

Budget receipts are designed so BOD-120 Effective Capability can consume them
later without this module depending on that planner.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from verdict.context_pack import estimate_tokens

BUDGET_SCHEMA_VERSION = "1"

BudgetSourceClass = Literal[
    "system_harness",
    "mcp_tools",
    "task_spec",
    "repo_code",
    "docs",
    "memory",
    "conversation_history",
    "proof_verification",
    "execution_handoff",
    "reserved_output",
    "reserved_reasoning",
    "reserved_tool_calls",
]

BudgetPriority = Literal["mandatory", "optional"]
TokenCountKind = Literal["exact", "estimated", "unknown"]

_CONTENT_SOURCE_CLASSES: frozenset[str] = frozenset(
    {
        "system_harness",
        "mcp_tools",
        "task_spec",
        "repo_code",
        "docs",
        "memory",
        "conversation_history",
        "proof_verification",
        "execution_handoff",
    }
)

_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{16,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"(?i)https?://[^\s/@]+:[^\s/@]+@"),
)


class ContextBudgetError(ValueError):
    """Raised when budget accounting or allocation cannot proceed safely."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError) as exc:
        raise ContextBudgetError(
            "not_json_compatible", "budget artifact must be JSON-compatible"
        ) from exc


def _digest(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical(value).encode()).hexdigest()}"


def safe_provenance_uri(value: str) -> str:
    """Render provenance without credentials or query secrets."""
    parsed = urlsplit(value)
    if parsed.scheme and parsed.netloc:
        hostname = parsed.hostname or ""
        return urlunsplit((parsed.scheme, hostname, parsed.path, "", ""))
    return value.split("?", 1)[0].split("#", 1)[0]


def content_looks_secret(content: str) -> bool:
    return any(pattern.search(content) for pattern in _SECRET_PATTERNS)


def estimate_unit_tokens(unit: BudgetUnit) -> int | None:
    """Return token cost for a unit, or None when count is explicitly unknown."""
    if unit.token_count_kind == "unknown":
        return None
    if unit.token_count is not None:
        return unit.token_count
    if unit.content is None:
        return None
    return estimate_tokens(unit.content)


@dataclass(frozen=True)
class BudgetUnit:
    """One attributed context consumer before allocation."""

    unit_id: str
    source_class: BudgetSourceClass
    priority: BudgetPriority
    content: str | None = None
    token_count: int | None = None
    token_count_kind: TokenCountKind = "estimated"
    provenance_uri: str = "urn:verdict:unattributed"
    value_score: float = 0.5
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.unit_id, str) or not self.unit_id.strip():
            raise ContextBudgetError("invalid_unit", "unit_id must be a non-empty string")
        if self.source_class not in _CONTENT_SOURCE_CLASSES:
            raise ContextBudgetError(
                "invalid_unit", f"unsupported content source_class: {self.source_class}"
            )
        if self.priority not in {"mandatory", "optional"}:
            raise ContextBudgetError("invalid_unit", "priority must be mandatory or optional")
        if self.token_count_kind not in {"exact", "estimated", "unknown"}:
            raise ContextBudgetError("invalid_unit", "token_count_kind invalid")
        if self.token_count is not None:
            if isinstance(self.token_count, bool) or not isinstance(self.token_count, int):
                raise ContextBudgetError("invalid_unit", "token_count must be an int or None")
            if self.token_count < 0:
                raise ContextBudgetError("invalid_unit", "token_count must be non-negative")
        if self.token_count_kind == "unknown" and self.token_count is not None:
            raise ContextBudgetError(
                "invalid_unit", "unknown token_count_kind cannot carry a count"
            )
        if self.token_count_kind == "exact" and self.token_count is None:
            raise ContextBudgetError("invalid_unit", "exact token_count_kind requires token_count")
        if not isinstance(self.provenance_uri, str) or not self.provenance_uri.strip():
            raise ContextBudgetError("invalid_unit", "provenance_uri must be non-empty")
        if not isinstance(self.value_score, (int, float)) or isinstance(self.value_score, bool):
            raise ContextBudgetError("invalid_unit", "value_score must be numeric")
        object.__setattr__(self, "metadata", dict(self.metadata))

    def resolved_tokens(self) -> int | None:
        return estimate_unit_tokens(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "source_class": self.source_class,
            "priority": self.priority,
            "content": self.content,
            "token_count": self.token_count,
            "token_count_kind": self.token_count_kind,
            "provenance_uri": self.provenance_uri,
            "value_score": self.value_score,
            "metadata": dict(self.metadata),
            "resolved_tokens": self.resolved_tokens(),
        }


@dataclass(frozen=True)
class BudgetCandidateLimit:
    """Candidate/model-specific total context window and reserves."""

    candidate_id: str
    context_limit: int
    output_reserve: int = 0
    reasoning_reserve: int = 0
    tool_call_reserve: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_id, str) or not self.candidate_id.strip():
            raise ContextBudgetError("invalid_limit", "candidate_id must be non-empty")
        for name in ("context_limit", "output_reserve", "reasoning_reserve", "tool_call_reserve"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContextBudgetError("invalid_limit", f"{name} must be a non-negative int")
        if self.context_limit < 1:
            raise ContextBudgetError("invalid_limit", "context_limit must be positive")
        if self.usable_input_budget < 1:
            raise ContextBudgetError(
                "invalid_limit", "usable input budget must remain positive after reserves"
            )

    @property
    def reserved_total(self) -> int:
        return self.output_reserve + self.reasoning_reserve + self.tool_call_reserve

    @property
    def usable_input_budget(self) -> int:
        return self.context_limit - self.reserved_total

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "context_limit": self.context_limit,
            "output_reserve": self.output_reserve,
            "reasoning_reserve": self.reasoning_reserve,
            "tool_call_reserve": self.tool_call_reserve,
            "reserved_total": self.reserved_total,
            "usable_input_budget": self.usable_input_budget,
        }


@dataclass(frozen=True)
class SourceBudgetRow:
    """Per-source aggregate with provenance of the estimate kind."""

    source_class: BudgetSourceClass
    token_count: int | None
    token_count_kind: TokenCountKind
    unit_count: int
    mandatory_tokens: int
    optional_tokens: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_class": self.source_class,
            "token_count": self.token_count,
            "token_count_kind": self.token_count_kind,
            "unit_count": self.unit_count,
            "mandatory_tokens": self.mandatory_tokens,
            "optional_tokens": self.optional_tokens,
        }


@dataclass(frozen=True)
class BudgetOmission:
    """Explicit record of a unit excluded from the allocated budget."""

    unit_id: str
    source_class: BudgetSourceClass
    reason: str
    priority: BudgetPriority
    token_count: int | None
    token_count_kind: TokenCountKind
    value_score: float
    provenance_uri: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "source_class": self.source_class,
            "reason": self.reason,
            "priority": self.priority,
            "token_count": self.token_count,
            "token_count_kind": self.token_count_kind,
            "value_score": self.value_score,
            "provenance_uri": safe_provenance_uri(self.provenance_uri),
        }


@dataclass(frozen=True)
class ContextBudgetAccount:
    """Full ledger of context consumers before trimming."""

    candidate_id: str
    context_limit: int
    usable_input_budget: int
    reserved_total: int
    units: tuple[BudgetUnit, ...]
    per_source: tuple[SourceBudgetRow, ...]
    total_content_tokens: int
    unknown_unit_ids: tuple[str, ...]
    digest: str
    schema_version: str = BUDGET_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "candidate_id": self.candidate_id,
            "context_limit": self.context_limit,
            "usable_input_budget": self.usable_input_budget,
            "reserved_total": self.reserved_total,
            "units": [u.to_dict() for u in self.units],
            "per_source": [row.to_dict() for row in self.per_source],
            "total_content_tokens": self.total_content_tokens,
            "unknown_unit_ids": list(self.unknown_unit_ids),
            "digest": self.digest,
        }


@dataclass(frozen=True)
class BudgetReceipt:
    """Bounded allocation result — include/omit decisions with reasons.

    Shape is intentionally consumable by BOD-120 Effective Capability later.
    """

    candidate_id: str
    context_limit: int
    usable_input_budget: int
    reserved_total: int
    used_tokens: int
    fits: bool
    included: tuple[BudgetUnit, ...]
    omitted: tuple[BudgetOmission, ...]
    per_source_used: Mapping[str, int]
    account_digest: str
    digest: str
    schema_version: str = BUDGET_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "candidate_id": self.candidate_id,
            "context_limit": self.context_limit,
            "usable_input_budget": self.usable_input_budget,
            "reserved_total": self.reserved_total,
            "used_tokens": self.used_tokens,
            "fits": self.fits,
            "included": [
                {
                    "unit_id": u.unit_id,
                    "source_class": u.source_class,
                    "priority": u.priority,
                    "token_count": u.resolved_tokens(),
                    "token_count_kind": u.token_count_kind,
                    "value_score": u.value_score,
                    "provenance_uri": safe_provenance_uri(u.provenance_uri),
                }
                for u in self.included
            ],
            "omitted": [o.to_dict() for o in self.omitted],
            "per_source_used": dict(self.per_source_used),
            "account_digest": self.account_digest,
            "digest": self.digest,
        }


def _reserve_rows(limit: BudgetCandidateLimit) -> list[SourceBudgetRow]:
    rows: list[SourceBudgetRow] = []
    mapping: tuple[tuple[BudgetSourceClass, int], ...] = (
        ("reserved_output", limit.output_reserve),
        ("reserved_reasoning", limit.reasoning_reserve),
        ("reserved_tool_calls", limit.tool_call_reserve),
    )
    for source_class, tokens in mapping:
        rows.append(
            SourceBudgetRow(
                source_class=source_class,
                token_count=tokens,
                token_count_kind="exact",
                unit_count=1 if tokens else 0,
                mandatory_tokens=tokens,
                optional_tokens=0,
            )
        )
    return rows


def _aggregate_sources(units: Sequence[BudgetUnit]) -> list[SourceBudgetRow]:
    buckets: dict[str, list[BudgetUnit]] = {cls: [] for cls in sorted(_CONTENT_SOURCE_CLASSES)}
    for unit in units:
        buckets[unit.source_class].append(unit)
    rows: list[SourceBudgetRow] = []
    for source_class in sorted(buckets):
        group = buckets[source_class]
        if not group:
            continue
        kinds = {u.token_count_kind for u in group}
        resolved = [estimate_unit_tokens(u) for u in group]
        if any(count is None for count in resolved) or "unknown" in kinds:
            kind: TokenCountKind = "unknown"
            total: int | None = None
        elif all(u.token_count_kind == "exact" for u in group):
            kind = "exact"
            total = sum(count or 0 for count in resolved)
        else:
            kind = "estimated"
            total = sum(count or 0 for count in resolved)
        mandatory = sum(
            (estimate_unit_tokens(u) or 0)
            for u in group
            if u.priority == "mandatory" and estimate_unit_tokens(u) is not None
        )
        optional = sum(
            (estimate_unit_tokens(u) or 0)
            for u in group
            if u.priority == "optional" and estimate_unit_tokens(u) is not None
        )
        rows.append(
            SourceBudgetRow(
                source_class=source_class,  # type: ignore[arg-type]
                token_count=total,
                token_count_kind=kind,
                unit_count=len(group),
                mandatory_tokens=mandatory,
                optional_tokens=optional,
            )
        )
    return rows


def _sort_key(unit: BudgetUnit) -> tuple[Any, ...]:
    # Mandatory first (0), then higher value_score, then stable id.
    return (0 if unit.priority == "mandatory" else 1, -unit.value_score, unit.unit_id)


class ContextBudgetGovernor:
    """Account for total context, allocate under candidate limits, emit receipts."""

    def account(
        self, units: Sequence[BudgetUnit], limit: BudgetCandidateLimit
    ) -> ContextBudgetAccount:
        normalized = tuple(sorted(units, key=lambda u: u.unit_id))
        content_rows = _aggregate_sources(normalized)
        reserve_rows = _reserve_rows(limit)
        # Stable order: content classes alpha, then reserves in fixed order.
        per_source = tuple(sorted(content_rows, key=lambda r: r.source_class) + reserve_rows)
        known_tokens: list[int] = []
        for u in normalized:
            estimated = estimate_unit_tokens(u)
            if estimated is not None:
                known_tokens.append(estimated)
        total_content = sum(known_tokens)
        unknown_ids = tuple(u.unit_id for u in normalized if estimate_unit_tokens(u) is None)
        payload = {
            "candidate_id": limit.candidate_id,
            "context_limit": limit.context_limit,
            "usable_input_budget": limit.usable_input_budget,
            "reserved_total": limit.reserved_total,
            "units": [u.to_dict() for u in normalized],
            "per_source": [row.to_dict() for row in per_source],
            "total_content_tokens": total_content,
            "unknown_unit_ids": list(unknown_ids),
            "schema_version": BUDGET_SCHEMA_VERSION,
        }
        return ContextBudgetAccount(
            candidate_id=limit.candidate_id,
            context_limit=limit.context_limit,
            usable_input_budget=limit.usable_input_budget,
            reserved_total=limit.reserved_total,
            units=normalized,
            per_source=per_source,
            total_content_tokens=total_content,
            unknown_unit_ids=unknown_ids,
            digest=_digest(payload),
        )

    def allocate(self, units: Sequence[BudgetUnit], limit: BudgetCandidateLimit) -> BudgetReceipt:
        account = self.account(units, limit)
        omitted: list[BudgetOmission] = []
        included: list[BudgetUnit] = []
        used = 0
        budget = limit.usable_input_budget

        # Pass 1: safety + unknown optional exclusions (deterministic order).
        candidates: list[BudgetUnit] = []
        for unit in sorted(units, key=_sort_key):
            if unit.content is not None and content_looks_secret(unit.content):
                omitted.append(
                    BudgetOmission(
                        unit_id=unit.unit_id,
                        source_class=unit.source_class,
                        reason="secret_or_private_data_detected",
                        priority=unit.priority,
                        token_count=estimate_unit_tokens(unit),
                        token_count_kind=unit.token_count_kind,
                        value_score=unit.value_score,
                        provenance_uri=unit.provenance_uri,
                    )
                )
                continue
            tokens = estimate_unit_tokens(unit)
            if tokens is None:
                if unit.priority == "mandatory":
                    raise ContextBudgetError(
                        "mandatory_unknown_size",
                        f"mandatory unit {unit.unit_id!r} has unknown token count",
                    )
                omitted.append(
                    BudgetOmission(
                        unit_id=unit.unit_id,
                        source_class=unit.source_class,
                        reason="token_count_unknown",
                        priority=unit.priority,
                        token_count=None,
                        token_count_kind="unknown",
                        value_score=unit.value_score,
                        provenance_uri=unit.provenance_uri,
                    )
                )
                continue
            candidates.append(replace(unit, token_count=tokens))

        # Pass 2: include mandatory first; fail closed if they cannot fit.
        for unit in candidates:
            if unit.priority != "mandatory":
                continue
            cost = estimate_unit_tokens(unit)
            assert cost is not None
            if used + cost > budget:
                raise ContextBudgetError(
                    "mandatory_overflow",
                    f"mandatory unit {unit.unit_id!r} cannot fit in usable input budget",
                )
            included.append(unit)
            used += cost

        # Pass 3: optional by descending value_score (lowest value omitted first).
        optionals = [u for u in candidates if u.priority == "optional"]
        optionals.sort(key=lambda u: (-u.value_score, u.unit_id))
        for unit in optionals:
            cost = estimate_unit_tokens(unit)
            assert cost is not None
            if used + cost > budget:
                omitted.append(
                    BudgetOmission(
                        unit_id=unit.unit_id,
                        source_class=unit.source_class,
                        reason="input_budget_exhausted",
                        priority=unit.priority,
                        token_count=cost,
                        token_count_kind=unit.token_count_kind,
                        value_score=unit.value_score,
                        provenance_uri=unit.provenance_uri,
                    )
                )
                continue
            included.append(unit)
            used += cost

        per_source_used: dict[str, int] = {}
        for unit in included:
            cost = estimate_unit_tokens(unit) or 0
            per_source_used[unit.source_class] = per_source_used.get(unit.source_class, 0) + cost

        # Deterministic included order for receipt digests.
        included_sorted = tuple(sorted(included, key=_sort_key))
        omitted_sorted = tuple(sorted(omitted, key=lambda o: (o.unit_id, o.reason)))
        receipt = BudgetReceipt(
            candidate_id=limit.candidate_id,
            context_limit=limit.context_limit,
            usable_input_budget=limit.usable_input_budget,
            reserved_total=limit.reserved_total,
            used_tokens=used,
            fits=used <= budget,
            included=included_sorted,
            omitted=omitted_sorted,
            per_source_used=per_source_used,
            account_digest=account.digest,
            digest="",  # filled below
        )
        digest = _digest({**receipt.to_dict(), "digest": None})
        return replace(receipt, digest=digest)

    def evaluate_candidates(
        self, units: Sequence[BudgetUnit], limits: Sequence[BudgetCandidateLimit]
    ) -> dict[str, BudgetReceipt]:
        """Allocate the same unit set under each candidate limit (replayable)."""
        return {limit.candidate_id: self.allocate(units, limit) for limit in limits}

    def diagnose(self, receipt: BudgetReceipt) -> dict[str, Any]:
        """Diagnostic status for CLI/ops — redacts secrets and raw content."""
        payload = receipt.to_dict()
        # Receipt.to_dict already strips content and sanitizes provenance URIs.
        # Extra pass: scrub any residual secret-looking strings in reason fields.
        blob = _canonical(payload)
        for pattern in _SECRET_PATTERNS:
            blob = pattern.sub("[REDACTED]", blob)
        loaded: dict[str, Any] = json.loads(blob)
        return loaded


__all__ = [
    "BUDGET_SCHEMA_VERSION",
    "BudgetCandidateLimit",
    "BudgetOmission",
    "BudgetPriority",
    "BudgetReceipt",
    "BudgetSourceClass",
    "BudgetUnit",
    "ContextBudgetAccount",
    "ContextBudgetError",
    "ContextBudgetGovernor",
    "SourceBudgetRow",
    "TokenCountKind",
    "content_looks_secret",
    "estimate_unit_tokens",
    "safe_provenance_uri",
]
