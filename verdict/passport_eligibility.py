"""Fail-closed hard eligibility checks for qualified capability passports."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from verdict.capability_passports import CapabilityPassport, CapabilityPassportError


@dataclass(frozen=True)
class PassportEligibilityRecord:
    """Decision-time explanation for one route key."""

    route_key: str
    admitted: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"route_key": self.route_key, "admitted": self.admitted, "reason": self.reason}


@dataclass(frozen=True)
class PassportEligibilityResult:
    """Deterministic admitted routes and per-route hard-gate explanations."""

    required: tuple[str, ...]
    records: tuple[PassportEligibilityRecord, ...]

    @property
    def admitted(self) -> tuple[str, ...]:
        return tuple(record.route_key for record in self.records if record.admitted)

    def to_dict(self) -> dict[str, Any]:
        return {
            "required": list(self.required),
            "admitted": list(self.admitted),
            "records": [record.to_dict() for record in self.records],
        }


def evaluate_passports(
    passports: Mapping[str, CapabilityPassport],
    required: set[str] | frozenset[str],
    *,
    at: datetime | None = None,
) -> PassportEligibilityResult:
    """Evaluate exact routes against freshly observed hard capabilities.

    The mapping key is an integrity boundary: a passport cannot authorize a
    different route merely because it satisfies the same capability set.
    """

    if not isinstance(passports, Mapping):
        raise ValueError("passports must be a mapping")
    if not isinstance(required, (set, frozenset)):
        raise ValueError("required must be a set or frozenset")
    if at is not None and (not isinstance(at, datetime) or at.tzinfo is None):
        raise ValueError("at must be timezone-aware")
    current = at.astimezone(timezone.utc) if at is not None else None
    required_names = tuple(sorted(_capability_name(name) for name in required))
    records: list[PassportEligibilityRecord] = []
    for route_key in sorted(passports):
        if not isinstance(route_key, str) or not route_key:
            raise ValueError("passport route keys must be non-empty strings")
        passport = passports[route_key]
        if not isinstance(passport, CapabilityPassport):
            raise ValueError(f"passport for {route_key!r} must be a CapabilityPassport")
        if passport.route_identity.key != route_key:
            records.append(PassportEligibilityRecord(route_key, False, "route identity mismatch"))
            continue
        decisions = [passport.resolve(name, at=current) for name in required_names]
        rejected = next((decision for decision in decisions if not decision.admitted), None)
        if rejected is not None:
            records.append(
                PassportEligibilityRecord(
                    route_key,
                    False,
                    f"required capability {rejected.capability}: {rejected.reason}",
                )
            )
        else:
            records.append(
                PassportEligibilityRecord(route_key, True, "all required capabilities admitted")
            )
    return PassportEligibilityResult(required_names, tuple(records))


def _capability_name(value: Any) -> str:
    try:
        # Reuse the passport contract's strict capability-name validation.
        from verdict.capability_passports import _capability_name as validate

        return validate(value)
    except CapabilityPassportError as exc:
        raise ValueError(str(exc)) from exc


__all__ = ["PassportEligibilityRecord", "PassportEligibilityResult", "evaluate_passports"]
