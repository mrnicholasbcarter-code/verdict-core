"""Append-only claims ledger with deterministic selection and hydration."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any, Literal, cast
from uuid import uuid4

from verdict.claims_models import (
    _STATUSES,
    _TRANSITIONS,
    CLAIMS_LEDGER_SCHEMA_VERSION,
    Claim,
    ClaimHydrationOmission,
    ClaimHydrationResult,
    ClaimsLedgerError,
    ClaimStatus,
    ClaimTransition,
    ClaimTransitionError,
    FreshnessPolicy,
    FreshnessResult,
    FreshnessStatus,
    _as_datetime,
    _identifier,
    _now,
    _timestamp,
)
from verdict.context_pack import ContextUnit
from verdict.proof_receipts import claim_hash


class ClaimsLedger:
    """Append-only claims ledger with deterministic selection and audit events."""

    def __init__(
        self,
        policies: Mapping[str, FreshnessPolicy],
        *,
        receipt_store: Any | None = None,
        scope: str = "claims",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(policies, Mapping) or not policies:
            raise ClaimsLedgerError("explicit per-source freshness policies are required")
        self.policies = dict(policies)
        for key, policy in self.policies.items():
            if key != policy.source_type:
                raise ClaimsLedgerError("freshness policy key must match source_type")
        self.receipt_store = receipt_store
        self.scope = scope
        self.clock = clock or _now
        self._claims: dict[str, Claim] = {}
        self._transitions: list[ClaimTransition] = []
        self._receipt_ids: dict[str, str] = {}

    def _freshness(self, claim: Claim, now: datetime | str | None = None) -> FreshnessResult:
        policy = self.policies.get(claim.source_type)
        if policy is None:
            return FreshnessResult(
                claim.source_type,
                "policy_missing",
                _timestamp(claim.observed_at, "observed_at"),
                None,
                0.0,
            )
        expires = policy.expiry(claim.observed_at)
        return FreshnessResult(
            claim.source_type,
            policy.evaluate(claim.observed_at, now=now or self.clock()),
            _timestamp(claim.observed_at, "observed_at"),
            _timestamp(expires, "expires_at") if expires is not None else None,
            policy.clock_skew_seconds,
        )

    def freshness(self, claim_id: str, *, now: datetime | str | None = None) -> FreshnessResult:
        return self._freshness(self.get(claim_id), now)

    def _persist_claim(self, claim: Claim) -> None:
        if self.receipt_store is None:
            return
        record = self.receipt_store.put_receipt(
            "context",
            self.scope,
            {"kind": "claim", "claim": claim.to_dict()},
            receipt_id=f"claim-{claim.claim_id}",
            provenance={"source": "verdict.claims_ledger", "version": CLAIMS_LEDGER_SCHEMA_VERSION},
        )
        self._receipt_ids[claim.claim_id] = record.receipt_id

    def _persist_transition(self, transition: ClaimTransition) -> None:
        if self.receipt_store is None:
            return
        parent = self._receipt_ids[transition.claim_id]
        self.receipt_store.append_event(
            parent,
            scope=self.scope,
            payload={
                "kind": "claim_transition",
                "transition": {
                    **transition.to_dict(),
                    "reason": claim_hash(transition.reason),
                },
            },
            event_id=transition.transition_id,
            event_type="claim_status_transition",
        )

    def add(self, claim: Claim) -> Claim:
        """Append a claim version, superseding its predecessor when requested."""
        if not isinstance(claim, Claim):
            raise ClaimsLedgerError("claim must be a Claim")
        existing = self._claims.get(claim.claim_id)
        if existing is not None:
            if existing.to_dict() == claim.to_dict():
                return existing
            raise ClaimsLedgerError("claim_id already exists with different content")
        if claim.supersedes is not None:
            if claim.supersedes not in self._claims:
                raise ClaimsLedgerError("supersedes references an unavailable claim")
            if self._claims[claim.supersedes].status == "superseded":
                raise ClaimsLedgerError("superseded claims cannot be superseded again")
        claims_before = self._claims.copy()
        transitions_before = self._transitions.copy()
        receipt_ids_before = self._receipt_ids.copy()
        try:
            self._claims[claim.claim_id] = claim
            self._persist_claim(claim)
            if claim.supersedes is not None:
                self.transition(
                    claim.supersedes,
                    "superseded",
                    reason="superseded_by_new_claim",
                    superseded_by=claim.claim_id,
                    occurred_at=claim.observed_at,
                )
        except BaseException:
            self._claims = claims_before
            self._transitions = transitions_before
            self._receipt_ids = receipt_ids_before
            raise
        return claim

    append = add

    def get(self, claim_id: str) -> Claim:
        _identifier(claim_id, "claim_id")
        try:
            return self._claims[claim_id]
        except KeyError as exc:
            raise ClaimsLedgerError(f"unknown claim: {claim_id}") from exc

    def transition(
        self,
        claim_id: str,
        to_status: ClaimStatus,
        *,
        reason: str,
        occurred_at: datetime | str | None = None,
        superseded_by: str | None = None,
    ) -> Claim:
        current = self.get(claim_id)
        if to_status not in _STATUSES:
            raise ClaimTransitionError("claim status is invalid")
        if to_status not in _TRANSITIONS[current.status]:
            raise ClaimTransitionError(f"cannot transition {current.status} to {to_status}")
        transition = ClaimTransition(
            transition_id=f"transition-{uuid4().hex}",
            claim_id=claim_id,
            from_status=current.status,
            to_status=to_status,
            occurred_at=_timestamp(occurred_at or self.clock(), "occurred_at"),
            reason=reason,
            superseded_by=superseded_by,
        )
        self._persist_transition(transition)
        updated = current.with_status(to_status)
        self._claims[claim_id] = updated
        self._transitions.append(transition)
        return updated

    def transitions(self, claim_id: str | None = None) -> tuple[ClaimTransition, ...]:
        if claim_id is None:
            return tuple(self._transitions)
        return tuple(item for item in self._transitions if item.claim_id == claim_id)

    def to_dict(self) -> dict[str, Any]:
        """Export claims, transitions, and the explicit freshness policy."""
        return {
            "schema_version": CLAIMS_LEDGER_SCHEMA_VERSION,
            "claims": [claim.to_dict() for claim in self.query()],
            "transitions": [item.to_dict() for item in self._transitions],
            "freshness_policies": [
                {
                    "source_type": policy.source_type,
                    "ttl_seconds": policy.ttl_seconds,
                    "clock_skew_seconds": policy.clock_skew_seconds,
                }
                for policy in sorted(self.policies.values(), key=lambda item: item.source_type)
            ],
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        claim_texts: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> ClaimsLedger:
        """Restore a ledger without writing duplicate receipts."""
        if (
            not isinstance(value, Mapping)
            or value.get("schema_version") != CLAIMS_LEDGER_SCHEMA_VERSION
        ):
            raise ClaimsLedgerError("unsupported claims ledger artifact")
        claims_value = value.get("claims")
        transitions_value = value.get("transitions")
        policies_value = value.get("freshness_policies")
        if (
            not isinstance(claims_value, list)
            or not isinstance(transitions_value, list)
            or not isinstance(policies_value, list)
        ):
            raise ClaimsLedgerError("claims ledger artifact has malformed collections")
        policies = {}
        for item in policies_value:
            if not isinstance(item, Mapping):
                raise ClaimsLedgerError("freshness policy must be an object")
            policy = FreshnessPolicy(
                item["source_type"], item["ttl_seconds"], item["clock_skew_seconds"]
            )
            policies[policy.source_type] = policy
        ledger = cls(policies, clock=clock)
        text_map = claim_texts or {}
        for item in claims_value:
            claim = Claim.from_dict(item)
            text = text_map.get(claim.claim_id)
            if text is not None:
                if claim_hash(text) != claim.text_hash:
                    raise ClaimsLedgerError("claim text does not match persisted text_hash")
                claim = replace(claim, text=text)
            ledger._claims[claim.claim_id] = claim
        for item in transitions_value:
            if not isinstance(item, Mapping):
                raise ClaimsLedgerError("claim transition must be an object")
            transition = ClaimTransition(
                transition_id=item["transition_id"],
                claim_id=item["claim_id"],
                from_status=item["from_status"],
                to_status=item["to_status"],
                occurred_at=item["occurred_at"],
                reason=item["reason"],
                superseded_by=item.get("superseded_by"),
            )
            ledger._transitions.append(transition)
        return ledger

    @classmethod
    def from_receipt_store(
        cls,
        receipt_store: Any,
        *,
        policies: Mapping[str, FreshnessPolicy],
        scope: str,
        clock: Callable[[], datetime] | None = None,
    ) -> ClaimsLedger:
        """Rebuild current state and transition history from append-only receipts."""
        ledger = cls(policies, receipt_store=receipt_store, scope=scope, clock=clock)
        claim_records = receipt_store.query_receipts(
            receipt_type="context", scope=scope, limit=100_000, include_tombstones=False
        )
        for record in sorted(claim_records, key=lambda item: item.sequence):
            if record.payload.get("kind") != "claim":
                continue
            claim = Claim.from_dict(record.payload["claim"])
            ledger._claims[claim.claim_id] = claim
            ledger._receipt_ids[claim.claim_id] = record.receipt_id
        event_records = receipt_store.query_receipts(
            receipt_type="execution", scope=scope, limit=100_000, include_tombstones=False
        )
        for record in sorted(event_records, key=lambda item: item.sequence):
            if record.event_type != "claim_status_transition":
                continue
            payload = record.payload.get("transition")
            if not isinstance(payload, Mapping):
                continue
            transition = ClaimTransition(
                transition_id=payload["transition_id"],
                claim_id=payload["claim_id"],
                from_status=payload["from_status"],
                to_status=payload["to_status"],
                occurred_at=payload["occurred_at"],
                reason=payload["reason"],
                superseded_by=payload.get("superseded_by"),
            )
            current = ledger._claims.get(transition.claim_id)
            if current is None:
                raise ClaimsLedgerError("transition references an unavailable claim")
            ledger._claims[transition.claim_id] = current.with_status(transition.to_status)
            ledger._transitions.append(transition)
        return ledger

    def query(
        self,
        *,
        subject: str | None = None,
        statuses: Sequence[ClaimStatus] | None = None,
        include_superseded: bool = True,
    ) -> tuple[Claim, ...]:
        allowed = set(statuses) if statuses is not None else set(_STATUSES)
        claims = [
            claim
            for claim in self._claims.values()
            if (subject is None or claim.subject == subject)
            and claim.status in allowed
            and (include_superseded or claim.status != "superseded")
        ]
        return tuple(sorted(claims, key=lambda item: (item.subject, item.claim_id)))

    history = query

    def select_active(
        self,
        *,
        subject: str | None = None,
        now: datetime | str | None = None,
        include_observed: bool = False,
    ) -> tuple[Claim, ...]:
        """Select fresh current claims in a stable, conflict-resolving order."""
        candidates: list[Claim] = []
        statuses = {"active", "verified"} | ({"observed"} if include_observed else set())
        for claim in self._claims.values():
            if claim.subject != subject and subject is not None:
                continue
            if claim.status not in statuses:
                continue
            if self._freshness(claim, now).fresh:
                candidates.append(claim)
        candidates.sort(
            key=lambda item: (
                item.subject,
                -(1 if item.status == "verified" else 0),
                -(
                    _as_datetime(
                        item.verified_at or item.observed_at, "claim timestamp"
                    ).timestamp()
                ),
                -_as_datetime(item.observed_at, "observed_at").timestamp(),
                -float(item.confidence),
                item.claim_id,
            )
        )
        if subject is None:
            selected: list[Claim] = []
            seen_subjects: set[str] = set()
            for claim in candidates:
                if claim.subject not in seen_subjects:
                    selected.append(claim)
                    seen_subjects.add(claim.subject)
            return tuple(selected)
        return tuple(candidates[:1])

    def hydrate(
        self,
        *,
        required_facts: Sequence[str] = (),
        claim_texts: Mapping[str, str] | None = None,
        now: datetime | str | None = None,
    ) -> ClaimHydrationResult:
        """Hydrate only selected claims, refusing disputed/unsafe required facts."""
        facts = tuple(fact for fact in required_facts if isinstance(fact, str) and fact.strip())
        text_map = claim_texts or {}
        selected: list[Claim] = []
        omissions: list[ClaimHydrationOmission] = []
        satisfied: list[str] = []
        if not facts:
            for claim in self.select_active(now=now):
                supplied_text = text_map.get(claim.claim_id, claim.text)
                if supplied_text is None or claim_hash(supplied_text) != claim.text_hash:
                    omissions.append(
                        ClaimHydrationOmission(
                            claim.claim_id, "text_unavailable_or_hash_mismatch", "blocked"
                        )
                    )
                    continue
                selected.append(claim)
        for fact in facts:
            matching = [
                claim
                for claim in self._claims.values()
                if (claim.text is not None and fact in claim.text)
                or (claim.claim_id in text_map and fact in text_map[claim.claim_id])
            ]
            disputed = [claim for claim in matching if claim.status == "disputed"]
            if disputed:
                omissions.extend(
                    ClaimHydrationOmission(claim.claim_id, "disputed_claim", "disputed", fact)
                    for claim in disputed
                )
            eligible = [
                claim
                for claim in matching
                if claim.status in {"active", "verified"} and self._freshness(claim, now).fresh
            ]
            if not eligible:
                if not disputed:
                    omissions.append(
                        ClaimHydrationOmission(None, "required_fact_not_satisfied", "blocked", fact)
                    )
                continue
            eligible.sort(
                key=lambda claim: (
                    -(1 if claim.status == "verified" else 0),
                    -(
                        _as_datetime(
                            claim.verified_at or claim.observed_at, "claim timestamp"
                        ).timestamp()
                    ),
                    -float(claim.confidence),
                    claim.claim_id,
                )
            )
            claim = eligible[0]
            supplied_text = text_map.get(claim.claim_id, claim.text)
            if supplied_text is None or claim_hash(supplied_text) != claim.text_hash:
                omissions.append(
                    ClaimHydrationOmission(
                        claim.claim_id, "text_unavailable_or_hash_mismatch", "blocked", fact
                    )
                )
                continue
            if claim.claim_id not in {item.claim_id for item in selected}:
                selected.append(claim)
            if fact not in satisfied:
                satisfied.append(fact)
        units = tuple(
            self._context_unit(claim, text_map.get(claim.claim_id, claim.text))
            for claim in selected
        )
        return ClaimHydrationResult(
            tuple(selected), units, tuple(omissions), facts, tuple(satisfied)
        )

    def _context_unit(self, claim: Claim, text: str | None) -> ContextUnit:
        if text is None:
            raise ClaimsLedgerError("selected claim has no transient text")
        source_uri = (
            f"claim:{claim.claim_id}"
            if not claim.source_refs
            else f"source:{claim.source_refs[0].source_id}"
        )
        return ContextUnit(
            unit_id=f"claim:{claim.claim_id}",
            slot_type="evidence",
            key=claim.subject,
            content=text,
            source_uri=source_uri,
            source_digest=claim.text_hash,
            observed_at=_timestamp(claim.observed_at, "observed_at"),
            valid_until=self._freshness(claim).expires_at,
            trust="verified" if claim.status == "verified" else "observed",
            authority="verified" if claim.status == "verified" else "unverified",
            status="active"
            if claim.status in {"active", "verified"}
            else cast(Literal["observed", "superseded", "disputed"], claim.status),
            confidence=float(claim.confidence),
        )


def evaluate_claim_freshness(
    claim: Claim, policies: Mapping[str, FreshnessPolicy], *, now: datetime | str | None = None
) -> FreshnessResult:
    """Evaluate one claim against an explicit source-type policy."""
    ledger = ClaimsLedger(policies, clock=_now)
    return ledger._freshness(claim, now)


def hydrate_claims(
    ledger: ClaimsLedger,
    *,
    required_facts: Sequence[str] = (),
    claim_texts: Mapping[str, str] | None = None,
    now: datetime | str | None = None,
) -> ClaimHydrationResult:
    """Hydrate a claims ledger through the same bounded context-unit shape."""
    if not isinstance(ledger, ClaimsLedger):
        raise ClaimsLedgerError("ledger must be a ClaimsLedger")
    return ledger.hydrate(required_facts=required_facts, claim_texts=claim_texts, now=now)


__all__ = [
    "CLAIMS_LEDGER_SCHEMA_VERSION",
    "Claim",
    "ClaimHydrationOmission",
    "ClaimHydrationResult",
    "ClaimStatus",
    "ClaimTransition",
    "ClaimTransitionError",
    "ClaimsLedger",
    "ClaimsLedgerError",
    "FreshnessPolicy",
    "FreshnessResult",
    "FreshnessStatus",
    "evaluate_claim_freshness",
    "hydrate_claims",
]
