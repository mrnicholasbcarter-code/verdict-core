"""Recovery and failure classification for orchestration workers.

BOD-152: Normalized failure detection and bounded corrective actions based on
status code, error patterns, and parsed reset hints. Failure classification drives
reassignment, repair, or fail-closed decisions via RecoveryBudget.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from verdict.orchestration.contracts import FailureClassification, WorkerTerminal

CLASSIFIER_VERSION = "v1"

_GATEWAY_BUSY_RE = re.compile(
    r"chat_admission_busy|admission capacity is (?:temporarily unavailable|busy)"
    r"|Structurally heavy chat request capacity is busy",
    re.IGNORECASE,
)

# Maximum cooldown cap (24 hours in seconds)
MAX_COOLDOWN_SECONDS = 86400

# Common patterns for parsing retry-after hints
# Callable that takes a Match object and returns float | None
_DURATION_UNITS = {
    "d": 86400,
    "day": 86400,
    "days": 86400,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "hour": 3600,
    "hours": 3600,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
}
_DURATION_PART = re.compile(
    r"(\d+(?:\.\d+)?)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b", re.IGNORECASE
)


def _parse_duration(text: str) -> float | None:
    """'2h', '5h 30m', '1 hour 5 minutes', '90s' -> seconds."""
    total = 0.0
    found = False
    for number, unit in _DURATION_PART.findall(text):
        total += float(number) * _DURATION_UNITS[unit.lower()]
        found = True
    return total if found else None


RETRY_AFTER_PATTERNS: list[tuple[str, Any]] = [
    (r"retry-after:\s*(\d+)\b(?!\s*[a-z])", lambda m: float(m.group(1))),  # retry-after: N
    (r"retry after (\d+) seconds", lambda m: float(m.group(1))),  # retry after N seconds
    (r"resets? at ([\d\-T:.Z+]+)", lambda m: _parse_iso8601(m.group(1))),  # resets at ISO8601
    # "resets in 2h", "reset in 5h 30m", "try again in 3 minutes", "retry after 20s",
    # "available again in 1 hour 5 minutes"
    (
        r"(?:resets?|try again|retry(?:-after| after)?|available again|reset)\s*(?:in|after)?\s*"
        r"((?:\d+(?:\.\d+)?\s*(?:days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b"
        r"[\s,and]*)+)",
        lambda m: _parse_duration(m.group(1)),
    ),
]


def _parse_iso8601(timestamp_str: str) -> float | None:
    """Try to parse ISO8601 timestamp and return seconds until then from now."""
    try:
        # Simple heuristic: if it contains 'Z' or offset, treat as UTC ISO8601
        # Basic ISO parsing without full dateutil dependency
        if "T" in timestamp_str:
            ts = timestamp_str.replace("Z", "+00:00")
            # Try to parse - if it has offset, calculate delta
            if "+" in ts or ts.count("-") > 2:
                # Has timezone offset; simplified parse
                # For now, return a heuristic: assume it's soon
                return 3600.0
        return None
    except (ValueError, AttributeError):
        return None


def _sanitize_error(error_text: str) -> str:
    """Remove bearer tokens, sk-* keys, and other secrets from error text.

    Returns up to 300 characters of sanitized error.
    """
    sanitized = error_text
    # Remove bearer tokens
    sanitized = re.sub(r"bearer\s+[a-z0-9._-]+", "REDACTED", sanitized, flags=re.IGNORECASE)
    # Remove sk-* style API keys
    sanitized = re.sub(r"sk-[a-z0-9]+", "REDACTED", sanitized, flags=re.IGNORECASE)
    # Remove common API key patterns
    sanitized = re.sub(
        r"(api[_-]?key|token|secret)[=:\s]+[a-z0-9._-]+", "REDACTED", sanitized, flags=re.IGNORECASE
    )
    # Truncate to 300 chars
    return sanitized[:300]


def _parse_reset_hint(error_text: str) -> float | None:
    """Try to extract reset/retry-after hint from error text.

    Returns cooldown_seconds if found, None otherwise.
    """
    if not error_text:
        return None

    for pattern, converter in RETRY_AFTER_PATTERNS:
        match = re.search(pattern, error_text, re.IGNORECASE)
        if match:
            result = converter(match)
            if result is not None and result > 0:
                return min(float(result), MAX_COOLDOWN_SECONDS)

    return None


class FailureIntelligence:
    """Classifies worker terminal failures per BOD-152.

    Implements the FailureClassifier protocol. Uses status code as primary
    signal, then falls back to text pattern matching. Always reports evidence
    and respects retry-after hints and reset timestamps in error text.
    """

    def classify(self, terminal: WorkerTerminal, *, now: datetime) -> FailureClassification:
        """Classify a terminal failure into category, action, and cooldown.

        Classification precedence:
        1. explicit status_code (if present)
        2. versioned text patterns (fallback only)

        All categories with their default cooldowns and scope:
        - 429 + quota indicators -> quota_exhausted, REROUTE, provider, 3600
        - 429 otherwise -> rate_limited, REROUTE, provider, 60
        - 401 -> authentication, REROUTE, provider, 3600
        - 402 -> payment_required, REROUTE, provider, 3600
        - 403 -> permission, REROUTE, route, 3600
        - 400 + unsupported -> unsupported, REROUTE, route, 86400
        - 404 or model_not_found -> model_unavailable, REROUTE, route, 3600
        - 5xx -> upstream_temporary, REROUTE, route, 60
        - timeout -> timeout, REROUTE, route, 120
        - connection refused/reset -> transport_temporary, REROUTE, route, 60
        - empty/whitespace output -> empty_output, REROUTE, route, 300
        - no_final_answer -> no_final_answer, REROUTE, route, 300
        - malformed -> malformed_response, REROUTE, route, 300
        - model_mismatch -> model_mismatch, REROUTE, route, 3600
        - verification_failed -> verification_failed, CORRECT_IMPLEMENTATION, none, 0
        - ownership_violation -> ownership_violation, REHYDRATE, none, 0
        - unknown -> unknown, REROUTE, route, 120
        """
        # Handle successful completion (ok=True)
        if terminal.ok:
            # Empty or whitespace output is a failure even with ok=True
            if not terminal.output or not terminal.output.strip():
                return FailureClassification(
                    category="empty_output",
                    action="REROUTE",
                    cooldown_seconds=300,
                    scope="route",
                    evidence=_sanitize_error("ok but empty output"),
                )
            # Success case
            return FailureClassification(
                category="success", action="BLOCK", cooldown_seconds=0, scope="none"
            )

        # Extract base error text (already sanitized by executor)
        error_text = terminal.error or ""
        error_lower = error_text.lower()

        # Check retry-after on terminal first
        cooldown = terminal.retry_after_seconds or 0.0

        # Try to parse reset hint from error text if no retry-after
        if not cooldown:
            parsed_hint = _parse_reset_hint(error_text)
            if parsed_hint:
                cooldown = parsed_hint

        # Gateway-local admission shed (OmniRoute chat_admission_busy, upstream
        # issue #13648): the provider/model was never contacted, so it must not
        # cool down the route or provider. Retry the same route after a short wait.
        if _GATEWAY_BUSY_RE.search(error_text):
            return FailureClassification(
                category="gateway_busy",
                action="RETRY_INFRA",
                cooldown_seconds=cooldown or 15.0,
                scope="none",
                evidence=_sanitize_error(error_text),
            )

        # Priority 1: explicit status code
        if terminal.status_code is not None:
            status = terminal.status_code

            # 429 - Rate limit / Quota
            if status == 429:
                # Check for quota indicators
                quota_indicators = (
                    "quota",
                    "exceeded your",
                    "usage limit",
                    "insufficient_quota",
                    "weekly limit",
                    "monthly limit",
                    "daily limit",
                )
                is_quota = any(indicator in error_lower for indicator in quota_indicators)

                if is_quota:
                    if not cooldown:
                        cooldown = 3600
                    # A model-scoped cap ("for this model", per-model weekly
                    # limit) leaves the account's other models usable.
                    model_scoped = any(
                        marker in error_lower
                        for marker in ("for this model", "model usage limit", "per-model")
                    )
                    return FailureClassification(
                        category="quota_exhausted",
                        action="REROUTE",
                        cooldown_seconds=cooldown,
                        scope="route" if model_scoped else "provider",
                        evidence=_sanitize_error(error_text),
                    )
                else:
                    if not cooldown:
                        cooldown = 60
                    return FailureClassification(
                        category="rate_limited",
                        action="REROUTE",
                        cooldown_seconds=cooldown,
                        scope="provider",
                        evidence=_sanitize_error(error_text),
                    )

            # 401 - Authentication
            elif status == 401:
                if not cooldown:
                    cooldown = 3600
                return FailureClassification(
                    category="authentication",
                    action="REROUTE",
                    cooldown_seconds=cooldown,
                    scope="provider",
                    evidence=_sanitize_error(error_text),
                )

            # 402 - Payment Required
            elif status == 402:
                if not cooldown:
                    cooldown = 3600
                return FailureClassification(
                    category="payment_required",
                    action="REROUTE",
                    cooldown_seconds=cooldown,
                    scope="provider",
                    evidence=_sanitize_error(error_text),
                )

            # 403 - Permission Denied
            elif status == 403:
                if not cooldown:
                    cooldown = 3600
                return FailureClassification(
                    category="permission",
                    action="REROUTE",
                    cooldown_seconds=cooldown,
                    scope="route",
                    evidence=_sanitize_error(error_text),
                )

            # 400 - Bad Request (check for unsupported)
            elif status == 400:
                if "unsupported" in error_lower or "not supported" in error_lower:
                    if not cooldown:
                        cooldown = 86400
                    return FailureClassification(
                        category="unsupported",
                        action="REROUTE",
                        cooldown_seconds=cooldown,
                        scope="route",
                        evidence=_sanitize_error(error_text),
                    )
                # Generic 400
                if not cooldown:
                    cooldown = 120
                return FailureClassification(
                    category="bad_request",
                    action="REROUTE",
                    cooldown_seconds=cooldown,
                    scope="route",
                    evidence=_sanitize_error(error_text),
                )

            # 404 - Not Found
            elif status == 404:
                if not cooldown:
                    cooldown = 3600
                return FailureClassification(
                    category="model_unavailable",
                    action="REROUTE",
                    cooldown_seconds=cooldown,
                    scope="route",
                    evidence=_sanitize_error(error_text),
                )

            # 5xx - Server errors
            elif 500 <= status < 600:
                if not cooldown:
                    cooldown = 60
                return FailureClassification(
                    category="upstream_temporary",
                    action="REROUTE",
                    cooldown_seconds=cooldown,
                    scope="route",
                    evidence=_sanitize_error(error_text),
                )

        # Priority 2: text pattern matching (fallback)

        # Model not found / unavailable
        if any(x in error_lower for x in ("model not found", "unknown model", "does not exist")):
            if not cooldown:
                cooldown = 3600
            return FailureClassification(
                category="model_unavailable",
                action="REROUTE",
                cooldown_seconds=cooldown,
                scope="route",
                evidence=_sanitize_error(error_text),
            )

        # Timeout
        if terminal.stop_reason == "timeout" or any(
            x in error_lower for x in ("timeout", "timed out")
        ):
            if not cooldown:
                cooldown = 120
            return FailureClassification(
                category="timeout",
                action="REROUTE",
                cooldown_seconds=cooldown,
                scope="route",
                evidence=_sanitize_error(error_text),
            )

        # Connection errors
        if any(x in error_lower for x in ("connection refused", "connection reset", "transport")):
            if not cooldown:
                cooldown = 60
            return FailureClassification(
                category="transport_temporary",
                action="REROUTE",
                cooldown_seconds=cooldown,
                scope="route",
                evidence=_sanitize_error(error_text),
            )

        # No final answer
        if terminal.error == "no_final_answer" or terminal.stop_reason not in (
            "stop",
            "end_turn",
            "",
        ):
            if not cooldown:
                cooldown = 300
            return FailureClassification(
                category="no_final_answer",
                action="REROUTE",
                cooldown_seconds=cooldown,
                scope="route",
                evidence=_sanitize_error(error_text),
            )

        # Malformed response
        if "malformed" in error_lower:
            if not cooldown:
                cooldown = 300
            return FailureClassification(
                category="malformed_response",
                action="REROUTE",
                cooldown_seconds=cooldown,
                scope="route",
                evidence=_sanitize_error(error_text),
            )

        # Model mismatch
        if terminal.error == "model_mismatch":
            if not cooldown:
                cooldown = 3600
            return FailureClassification(
                category="model_mismatch",
                action="REROUTE",
                cooldown_seconds=cooldown,
                scope="route",
                evidence=_sanitize_error(error_text),
            )

        # Verification failed
        if terminal.error.startswith("verification_failed"):
            return FailureClassification(
                category="verification_failed",
                action="CORRECT_IMPLEMENTATION",
                cooldown_seconds=0,
                scope="none",
                evidence=_sanitize_error(error_text),
            )

        # Ownership violation
        if terminal.error.startswith("ownership_violation"):
            return FailureClassification(
                category="ownership_violation",
                action="REHYDRATE",
                cooldown_seconds=0,
                scope="none",
                evidence=_sanitize_error(error_text),
            )

        # Unknown / fallback
        if not cooldown:
            cooldown = 120
        return FailureClassification(
            category="unknown",
            action="REROUTE",
            cooldown_seconds=cooldown,
            scope="route",
            evidence=_sanitize_error(error_text),
        )


@dataclass
class RecoveryBudget:
    """Tracks and decides recovery actions for a node based on failure history.

    Limits:
    - max_attempts_per_node: Max total attempts before FAIL_CLOSED
    - max_same_route_retries: Max consecutive retries on same route before reassign
    - max_verification_repairs: Max CORRECT_IMPLEMENTATION repairs before FAIL_CLOSED
    """

    max_attempts_per_node: int = 4
    max_same_route_retries: int = 0
    max_verification_repairs: int = 1

    def decide(self, node_id: str, history: list[FailureClassification]) -> tuple[str, str]:
        """Decide recovery action: 'REASSIGN' | 'REPAIR' | 'FAIL_CLOSED'.

        Returns (action, reason) tuple.

        Logic:
        - If last action is BLOCK -> FAIL_CLOSED
        - If attempts exhausted -> FAIL_CLOSED
        - If verification failure category detected and repairs available -> REPAIR
        - If verification repairs exhausted -> FAIL_CLOSED
        - Default to REASSIGN for other failures
        """
        if not history:
            return "REASSIGN", "first attempt"

        # Check if last action was BLOCK (explicit blocker)
        if history[-1].action == "BLOCK":
            return "FAIL_CLOSED", "last action was BLOCK"

        # Check if we've exhausted attempts
        if len(history) >= self.max_attempts_per_node:
            return "FAIL_CLOSED", f"exhausted {self.max_attempts_per_node} attempts"

        # Count verification failures and how many repairs we've already attempted
        # Each verification_failed is a problem; if we see N failures, we've tried N-1 repairs
        verification_failures = sum(1 for c in history if c.category == "verification_failed")
        repairs_attempted = max(0, verification_failures - 1)

        # If we have a verification_failed category and haven't exhausted repairs -> REPAIR
        if history[-1].category == "verification_failed":
            if repairs_attempted >= self.max_verification_repairs:
                return (
                    "FAIL_CLOSED",
                    f"exhausted {self.max_verification_repairs} verification repairs",
                )
            # Allow repair attempt
            return "REPAIR", "allow one verification repair"

        # Default to reassign for other failures
        return "REASSIGN", f"reassign after {len(history)} attempts"
