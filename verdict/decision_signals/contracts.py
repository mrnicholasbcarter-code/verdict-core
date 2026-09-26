"""Decision signal types and validation (SHADOW decision signals)."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from verdict.gateway_adapters import NormalizedFailureClass


class DecisionSignalError(Exception):
    """Raised when decision signal validation fails."""


@dataclass(frozen=True)
class DecisionQuestionV1:
    """Typed input for decision signal request."""

    purpose: str
    task_summary: str
    complexity_hints: dict[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.purpose, str) or not self.purpose:
            raise DecisionSignalError("purpose must be non-empty string")
        if not isinstance(self.task_summary, str):
            raise DecisionSignalError("task_summary must be string")
        if not isinstance(self.complexity_hints, dict):
            raise DecisionSignalError("complexity_hints must be dict")

    def to_dict(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "task_summary": self.task_summary,
            "complexity_hints": dict(self.complexity_hints),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DecisionQuestionV1:
        extra = set(value.keys()) - {"purpose", "task_summary", "complexity_hints"}
        if extra:
            raise DecisionSignalError(f"Unknown fields in DecisionQuestionV1: {sorted(extra)}")
        return cls(
            purpose=value["purpose"],
            task_summary=value["task_summary"],
            complexity_hints=value["complexity_hints"],
        )


@dataclass(frozen=True)
class DecisionSignalSetV1:
    """Versioned decision signal set with strict validation."""

    schema_version: str
    provider: str
    model: str
    version: str
    request_id: str
    purpose: str
    signals: dict[str, float] | None
    confidence: float
    latency_ms: int
    usage: dict[str, int]
    input_digest: str
    observed_at: str
    failure_class: NormalizedFailureClass | None
    mode: str

    def __post_init__(self) -> None:
        # Validate schema_version
        if self.schema_version != "decision-signals/v1":
            raise DecisionSignalError(
                f"Wrong schema_version: expected 'decision-signals/v1', got {self.schema_version!r}"
            )

        # Validate string fields
        for name in ("provider", "model", "version", "request_id", "purpose", "mode"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise DecisionSignalError(f"{name} must be string")

        # Validate input_digest (sha256 hex)
        if not isinstance(self.input_digest, str) or len(self.input_digest) != 64:
            raise DecisionSignalError("input_digest must be 64-character hex string")
        try:
            int(self.input_digest, 16)
        except ValueError as exc:
            raise DecisionSignalError("input_digest must be hex") from exc

        # Validate observed_at (ISO8601 string)
        if not isinstance(self.observed_at, str):
            raise DecisionSignalError("observed_at must be ISO8601 string")

        # Validate confidence [0,1]
        if not isinstance(self.confidence, (int, float)):
            raise DecisionSignalError("confidence must be numeric")
        if math.isnan(self.confidence) or math.isinf(self.confidence):
            raise DecisionSignalError("confidence must not be NaN or inf")
        if not 0.0 <= self.confidence <= 1.0:
            raise DecisionSignalError(f"confidence must be in [0,1], got {self.confidence}")

        # Validate latency_ms
        if not isinstance(self.latency_ms, int) or self.latency_ms < 0:
            raise DecisionSignalError("latency_ms must be non-negative int")

        # Validate usage
        if not isinstance(self.usage, dict):
            raise DecisionSignalError("usage must be dict")
        for key in ("input_tokens", "output_tokens"):
            if key not in self.usage:
                raise DecisionSignalError(f"usage missing required key: {key}")
            if not isinstance(self.usage[key], int) or self.usage[key] < 0:
                raise DecisionSignalError(f"usage.{key} must be non-negative int")

        # Validate signals (None when failure_class is set)
        if self.signals is not None:
            if not isinstance(self.signals, dict):
                raise DecisionSignalError("signals must be dict or None")
            expected_keys = {
                "complexity",
                "decomposability",
                "ambiguity",
                "frontier_worthy",
                "security_sensitive",
                "verification_strength",
                "context_need",
            }
            extra = set(self.signals.keys()) - expected_keys
            if extra:
                raise DecisionSignalError(f"Unknown signal keys: {sorted(extra)}")
            for key, val in self.signals.items():
                if not isinstance(val, (int, float)):
                    raise DecisionSignalError(f"signals.{key} must be numeric")
                if math.isnan(val) or math.isinf(val):
                    raise DecisionSignalError(f"signals.{key} must not be NaN or inf")
                if not 0.0 <= val <= 1.0:
                    raise DecisionSignalError(f"signals.{key} must be in [0,1], got {val}")

        # Validate failure_class
        if self.failure_class is not None and not isinstance(
            self.failure_class, NormalizedFailureClass
        ):
            try:
                object.__setattr__(
                    self, "failure_class", NormalizedFailureClass(self.failure_class)
                )
            except ValueError as exc:
                raise DecisionSignalError(f"Invalid failure_class: {self.failure_class}") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "model": self.model,
            "version": self.version,
            "request_id": self.request_id,
            "purpose": self.purpose,
            "signals": dict(self.signals) if self.signals is not None else None,
            "confidence": self.confidence,
            "latency_ms": self.latency_ms,
            "usage": dict(self.usage),
            "input_digest": self.input_digest,
            "observed_at": self.observed_at,
            "failure_class": self.failure_class.value if self.failure_class else None,
            "mode": self.mode,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DecisionSignalSetV1:
        """Strict from_dict: rejects unknown fields, validates all constraints."""
        expected = {
            "schema_version",
            "provider",
            "model",
            "version",
            "request_id",
            "purpose",
            "signals",
            "confidence",
            "latency_ms",
            "usage",
            "input_digest",
            "observed_at",
            "failure_class",
            "mode",
        }
        extra = set(value.keys()) - expected
        if extra:
            raise DecisionSignalError(f"Unknown fields in DecisionSignalSetV1: {sorted(extra)}")

        missing = expected - set(value.keys())
        if missing:
            raise DecisionSignalError(
                f"Missing required fields in DecisionSignalSetV1: {sorted(missing)}"
            )

        failure_class = value["failure_class"]
        if failure_class is not None and not isinstance(failure_class, NormalizedFailureClass):
            failure_class = NormalizedFailureClass(failure_class)

        return cls(
            schema_version=value["schema_version"],
            provider=value["provider"],
            model=value["model"],
            version=value["version"],
            request_id=value["request_id"],
            purpose=value["purpose"],
            signals=value["signals"],
            confidence=value["confidence"],
            latency_ms=value["latency_ms"],
            usage=value["usage"],
            input_digest=value["input_digest"],
            observed_at=value["observed_at"],
            failure_class=failure_class,
            mode=value["mode"],
        )


class DecisionSignalProvider(Protocol):
    """Provider protocol for decision signals. NEVER raises; failures as signals with failure_class."""

    def signals(self, question: DecisionQuestionV1, *, now: datetime) -> DecisionSignalSetV1:
        """Return decision signals for question. Never raises; failures as signals with failure_class set."""
        ...


def compute_input_digest(question: DecisionQuestionV1) -> str:
    """Compute sha256 hex digest of canonical question representation."""
    canonical = json.dumps(question.to_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "DecisionQuestionV1",
    "DecisionSignalError",
    "DecisionSignalProvider",
    "DecisionSignalSetV1",
    "compute_input_digest",
]
