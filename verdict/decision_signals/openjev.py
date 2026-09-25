"""OpenJev System-One decision signal provider (BOD-199)."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from verdict.gateway_adapters import NormalizedFailureClass
from verdict.gateway_adapter_runtime import AdapterFailureSignal, NormalizedFailure
from verdict.decision_signals.contracts import (
    DecisionQuestionV1,
    DecisionSignalProvider,
    DecisionSignalSetV1,
    compute_input_digest,
)


# Constants from autodev_routing
RETRY_AFTER_MIN_S = 1
RETRY_AFTER_MAX_S = 300
RETRY_AFTER_DEFAULT_S = 60


def parse_retry_after(value: str | None, *, now: datetime) -> float:
    """Parse Retry-After header value to cooldown seconds (BOD-198 compatible).

    Args:
        value: Retry-After header value (integer seconds or HTTP-date)
        now: Current datetime for relative calculation

    Returns:
        Cooldown seconds, clamped to [RETRY_AFTER_MIN_S, RETRY_AFTER_MAX_S].
        None/garbage/negative/NaN -> RETRY_AFTER_DEFAULT_S (clamped).
    """
    if not value:
        return float(RETRY_AFTER_DEFAULT_S)

    value = value.strip()

    # Try parsing as integer seconds first
    try:
        seconds = int(value)
        if seconds < 0:
            return float(RETRY_AFTER_DEFAULT_S)
        # Clamp to bounds
        return float(max(RETRY_AFTER_MIN_S, min(seconds, RETRY_AFTER_MAX_S)))
    except ValueError:
        pass

    # Try parsing as HTTP-date
    try:
        from email.utils import parsedate_to_datetime

        target_dt = parsedate_to_datetime(value)
        delta = (target_dt - now).total_seconds()
        if delta < 0:
            return float(RETRY_AFTER_DEFAULT_S)
        return float(max(RETRY_AFTER_MIN_S, min(delta, RETRY_AFTER_MAX_S)))
    except (ValueError, TypeError, OverflowError):
        return float(RETRY_AFTER_DEFAULT_S)


def normalize_failure(signal: AdapterFailureSignal, *, now: datetime) -> NormalizedFailure:
    """Normalize HTTP/runtime failure to NormalizedFailure (BOD-198 compatible)."""
    status = signal.status_code
    cooldown_seconds: float | None = None

    if signal.cancelled:
        failure_class = NormalizedFailureClass.CANCELLED
    elif signal.timed_out:
        failure_class = NormalizedFailureClass.TIMEOUT
    elif status in {401, 403}:
        failure_class = NormalizedFailureClass.AUTHENTICATION
    elif status == 402:
        failure_class = NormalizedFailureClass.QUOTA
    elif status == 429:
        # BOD-198: Distinguish quota exhaustion vs rate limiting
        code_lower = signal.code.lower() if signal.code else ""
        if code_lower in {"insufficient_quota", "quota_exceeded", "quota_exhausted"}:
            failure_class = NormalizedFailureClass.QUOTA
        else:
            failure_class = NormalizedFailureClass.RATE_LIMIT
            cooldown_seconds = parse_retry_after(signal.retry_after, now=now)
    elif status == 529:
        failure_class = NormalizedFailureClass.OVERLOADED
    elif status and 400 <= status < 500:
        failure_class = NormalizedFailureClass.INVALID_REQUEST
    elif status and 500 <= status < 600:
        failure_class = NormalizedFailureClass.UPSTREAM
    else:
        failure_class = NormalizedFailureClass.UNKNOWN

    retryable = failure_class not in {
        NormalizedFailureClass.AUTHENTICATION,
        NormalizedFailureClass.AUTHORIZATION,
        NormalizedFailureClass.QUOTA,
        NormalizedFailureClass.INVALID_REQUEST,
        NormalizedFailureClass.CAPABILITY,
    }

    return NormalizedFailure(
        failure_class=failure_class,
        retryable=retryable,
        status_code=status,
        cooldown_seconds=cooldown_seconds,
    )


class OpenJevSystemOneProvider:
    """OpenJev System-One decision signal provider (SHADOW-only, BOD-199)."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        transport: Callable[[str, dict[str, str], dict[str, Any]], tuple[int, dict[str, str], bytes]] | None = None,
    ) -> None:
        """Initialize OpenJev provider.

        Args:
            base_url: OpenJev endpoint base URL (default: from OPENJEV_BASE_URL env)
            api_key: OpenJev API key (default: from OPENJEV_API_KEY env)
            transport: Optional injectable transport for testing (url, headers, payload) -> (status, headers, body)
        """
        self.base_url = base_url or os.environ.get("OPENJEV_BASE_URL", "").strip()
        self.api_key = api_key or os.environ.get("OPENJEV_API_KEY", "").strip()
        self.transport = transport

    def signals(self, question: DecisionQuestionV1, *, now: datetime) -> DecisionSignalSetV1:
        """Return decision signals for question. NEVER raises; failures as signals with failure_class.

        Args:
            question: Typed decision question
            now: Current datetime for failure classification

        Returns:
            DecisionSignalSetV1 with signals or failure_class set
        """
        request_id = f"openjev-{now.isoformat()}"
        input_digest = compute_input_digest(question)

        # Missing credentials -> UNKNOWN
        if not self.base_url or not self.api_key:
            return DecisionSignalSetV1(
                schema_version="decision-signals/v1",
                provider="openjev",
                model="system-one",
                version="unknown",
                request_id=request_id,
                purpose=question.purpose,
                signals=None,
                confidence=0.0,
                latency_ms=0,
                usage={"input_tokens": 0, "output_tokens": 0},
                input_digest=input_digest,
                observed_at=now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                failure_class=NormalizedFailureClass.UNKNOWN,
                mode="SHADOW",
            )

        # Build request
        url = f"{self.base_url.rstrip('/')}/v1/systemone"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = question.to_dict()

        # Execute request
        start_ms = int(now.timestamp() * 1000)
        try:
            if self.transport:
                status, response_headers, body = self.transport(url, headers, payload)
            else:
                # Real HTTP call (not used in tests)
                import http.client
                import urllib.parse

                parsed = urllib.parse.urlparse(url)
                conn_class = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
                conn = conn_class(parsed.netloc, timeout=30)
                try:
                    conn.request("POST", parsed.path, json.dumps(payload).encode("utf-8"), headers)
                    response = conn.getresponse()
                    status = response.status
                    response_headers = dict(response.headers)
                    body = response.read()
                finally:
                    conn.close()

            end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            latency_ms = end_ms - start_ms

            # Success path
            if status == 200:
                try:
                    data = json.loads(body.decode("utf-8"))
                    # Validate required fields
                    required = ["provider", "model", "version", "confidence", "latency_ms", "usage", "observed_at"]
                    missing = [f for f in required if f not in data]
                    if missing:
                        raise KeyError(f"Missing required fields: {missing}")
                    
                    return DecisionSignalSetV1(
                        schema_version="decision-signals/v1",
                        provider=data["provider"],
                        model=data["model"],
                        version=data["version"],
                        request_id=data.get("request_id", request_id),
                        purpose=question.purpose,
                        signals=data.get("signals"),
                        confidence=data["confidence"],
                        latency_ms=data["latency_ms"],
                        usage=data["usage"],
                        input_digest=input_digest,
                        observed_at=data["observed_at"],
                        failure_class=None,
                        mode="SHADOW",
                    )
                except (json.JSONDecodeError, KeyError, TypeError) as exc:
                    # Malformed response
                    failure_signal = AdapterFailureSignal(
                        code="malformed_response",
                        status_code=status,
                    )
                    normalized = normalize_failure(failure_signal, now=now)
                    return DecisionSignalSetV1(
                        schema_version="decision-signals/v1",
                        provider="openjev",
                        model="system-one",
                        version="unknown",
                        request_id=request_id,
                        purpose=question.purpose,
                        signals=None,
                        confidence=0.0,
                        latency_ms=latency_ms,
                        usage={"input_tokens": 0, "output_tokens": 0},
                        input_digest=input_digest,
                        observed_at=now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                        failure_class=NormalizedFailureClass.INVALID_REQUEST,
                        mode="SHADOW",
                    )

            # Failure path - normalize with existing logic
            retry_after = response_headers.get("Retry-After") if status == 429 else None
            code = f"http_{status}"
            
            # Parse error response for 429 to detect quota vs rate-limit
            if status == 429:
                try:
                    error_data = json.loads(body.decode("utf-8"))
                    if "error" in error_data:
                        error_code = error_data["error"].get("code", "")
                        if error_code:
                            code = error_code
                except (json.JSONDecodeError, KeyError, TypeError):
                    pass
            
            failure_signal = AdapterFailureSignal(
                code=code,
                status_code=status,
                retry_after=retry_after,
            )
            normalized = normalize_failure(failure_signal, now=now)

            return DecisionSignalSetV1(
                schema_version="decision-signals/v1",
                provider="openjev",
                model="system-one",
                version="unknown",
                request_id=request_id,
                purpose=question.purpose,
                signals=None,
                confidence=0.0,
                latency_ms=latency_ms,
                usage={"input_tokens": 0, "output_tokens": 0},
                input_digest=input_digest,
                observed_at=now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                failure_class=normalized.failure_class,
                mode="SHADOW",
            )

        except TimeoutError:
            # Timeout
            end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            return DecisionSignalSetV1(
                schema_version="decision-signals/v1",
                provider="openjev",
                model="system-one",
                version="unknown",
                request_id=request_id,
                purpose=question.purpose,
                signals=None,
                confidence=0.0,
                latency_ms=end_ms - start_ms,
                usage={"input_tokens": 0, "output_tokens": 0},
                input_digest=input_digest,
                observed_at=now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                failure_class=NormalizedFailureClass.TIMEOUT,
                mode="SHADOW",
            )
        except Exception:
            # Network error or other exception
            end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            return DecisionSignalSetV1(
                schema_version="decision-signals/v1",
                provider="openjev",
                model="system-one",
                version="unknown",
                request_id=request_id,
                purpose=question.purpose,
                signals=None,
                confidence=0.0,
                latency_ms=end_ms - start_ms,
                usage={"input_tokens": 0, "output_tokens": 0},
                input_digest=input_digest,
                observed_at=now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                failure_class=NormalizedFailureClass.TRANSPORT,
                mode="SHADOW",
            )


__all__ = [
    "OpenJevSystemOneProvider",
    "parse_retry_after",
    "normalize_failure",
]
