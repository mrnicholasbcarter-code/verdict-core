"""OpenJev / Codiv System-One decision signal provider (BOD-235)."""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import verdict
from verdict.decision_signals.contracts import (
    DecisionQuestionV1,
    DecisionSignalSetV1,
    compute_input_digest,
)
from verdict.gateway_adapter_runtime import AdapterFailureSignal, NormalizedFailure
from verdict.gateway_adapters import NormalizedFailureClass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CODIV_SYSTEMONE_PATH = "/v1/systemone"
PINNED_MODEL = "openjev-0.1"

RETRY_AFTER_MIN_S = 1
RETRY_AFTER_MAX_S = 300
RETRY_AFTER_DEFAULT_S = 60

# Default network timeout for the Codiv API call.
# Overridable via VERDICT_DECISION_SIGNALS_TIMEOUT_MS (BOD-235/BOD-238).
# Must be low: this call runs BEFORE planning, so a slow Codiv adds to every run.
DEFAULT_TIMEOUT_MS = 1500

# Fixed question set sent in every SHADOW / ADVISORY call.
# Maps our signal names to Codiv question definitions.
_QUESTIONS: dict[str, dict[str, Any]] = {
    "complexity": {
        "type": "score",
        "instructions": "How complex is this software task overall",
        "criteria": ["trivial", "moderate", "hard", "very hard"],
    },
    "decomposability": {
        "type": "score",
        "instructions": "How easily can this task be broken into independent subtasks",
        "criteria": ["monolithic", "partially decomposable", "cleanly decomposable"],
    },
    "ambiguity": {
        "type": "score",
        "instructions": "How ambiguous or under-specified are the task requirements",
        "criteria": ["clear", "somewhat ambiguous", "highly ambiguous"],
    },
    "frontier_worthy": {
        "type": "noul",
        "instructions": "The task genuinely requires a frontier-level model (reasoning, breadth, novelty)",
    },
    "security_sensitive": {
        "type": "noul",
        "instructions": "The task touches security, authentication, secrets, privacy or compliance",
    },
    "verification_strength": {
        "type": "score",
        "instructions": "How rigorously should the output be verified",
        "criteria": ["light review", "standard review", "deep audit"],
    },
    "context_need": {
        "type": "score",
        "instructions": "How much additional context or hydration does the task require",
        "criteria": ["self-contained", "needs some context", "heavy context required"],
    },
}

# Levels per score question (len of criteria).
_SCORE_LEVELS: dict[str, int] = {
    "complexity": 4,
    "decomposability": 3,
    "ambiguity": 3,
    "verification_strength": 3,
    "context_need": 3,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user_agent() -> str:
    return f"verdict-core/{verdict.__version__}"


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

    try:
        seconds = int(value)
        if seconds < 0:
            return float(RETRY_AFTER_DEFAULT_S)
        return float(max(RETRY_AFTER_MIN_S, min(seconds, RETRY_AFTER_MAX_S)))
    except ValueError:
        pass

    try:
        from email.utils import parsedate_to_datetime

        target_dt = parsedate_to_datetime(value)
        delta = (target_dt - now).total_seconds()
        if delta < 0:
            return float(RETRY_AFTER_DEFAULT_S)
        return float(max(RETRY_AFTER_MIN_S, min(delta, RETRY_AFTER_MAX_S)))
    except (ValueError, TypeError, OverflowError):
        return float(RETRY_AFTER_DEFAULT_S)


def _entropy_confidence(noul_p: float) -> float:
    """Confidence for a noul answer using 1 - H(p)/ln 2."""
    import math

    p = max(1e-15, min(1.0 - 1e-15, noul_p))
    q = 1.0 - p
    h = -(p * math.log(p) + q * math.log(q))
    return max(0.0, min(1.0, 1.0 - h / math.log(2)))


def _score_confidence(probs: dict[str, float], k: int) -> float:
    """Confidence for a score/choice answer using 1 - H(p)/ln k."""
    import math

    if k <= 1:
        return 1.0
    h = 0.0
    for v in probs.values():
        if v > 1e-15:
            h -= v * math.log(v)
    return max(0.0, min(1.0, 1.0 - h / math.log(k)))


def _answers_to_signals(answers: dict[str, Any]) -> tuple[dict[str, float], float]:
    """Convert Codiv answers dict to (signals, confidence).

    noul  -> value = probability of yes  (already in [0,1])
    score -> value = score / (levels-1)  so it is in [0,1]
    """
    signals: dict[str, float] = {}
    confidences: list[float] = []

    for name, ans in answers.items():
        atype = ans.get("type")
        if atype == "noul":
            v = float(ans["noul"])
            signals[name] = max(0.0, min(1.0, v))
            confidences.append(_entropy_confidence(v))
        elif atype == "score":
            raw = float(ans["score"])
            levels = _SCORE_LEVELS.get(name, 2)
            signals[name] = max(0.0, min(1.0, raw / max(1, levels - 1)))
            probs = ans.get("probabilities", {})
            if probs:
                confidences.append(_score_confidence(probs, levels))
        elif atype == "choice":
            probs = ans.get("probabilities", {})
            if probs:
                k = len(probs)
                confidences.append(_score_confidence(probs, k))

    mean_conf = sum(confidences) / len(confidences) if confidences else 0.0
    return signals, max(0.0, min(1.0, mean_conf))


def normalize_failure(signal: AdapterFailureSignal, *, now: datetime) -> NormalizedFailure:
    """Normalize HTTP/runtime failure to NormalizedFailure (BOD-198 / BOD-235 compatible).

    Error shape: {"detail": {"error_type": "...", "message": "..."}}
    The code field on AdapterFailureSignal carries the error_type value.
    """
    status = signal.status_code
    cooldown_seconds: float | None = None

    if signal.cancelled:
        failure_class = NormalizedFailureClass.CANCELLED
    elif signal.timed_out:
        failure_class = NormalizedFailureClass.TIMEOUT
    elif status == 401:
        failure_class = NormalizedFailureClass.AUTHENTICATION
    elif status == 403:
        code = signal.code or ""
        if code == "permission_error":
            failure_class = NormalizedFailureClass.AUTHORIZATION
        else:
            # authentication_error (no key sent) or unknown
            failure_class = NormalizedFailureClass.AUTHENTICATION
    elif status == 429:
        code = signal.code or ""
        if code == "quota_exceeded_error":
            failure_class = NormalizedFailureClass.QUOTA  # not retryable
        else:
            # rate_limit_error -> RATE_LIMIT + retry-after
            failure_class = NormalizedFailureClass.RATE_LIMIT
            cooldown_seconds = parse_retry_after(signal.retry_after, now=now)
    elif status == 529:
        failure_class = NormalizedFailureClass.OVERLOADED
        cooldown_seconds = parse_retry_after(signal.retry_after, now=now)
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


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class OpenJevSystemOneProvider:
    """OpenJev / Codiv System-One decision signal provider (BOD-235).

    Sends the fixed question set to POST /v1/systemone and maps the answers
    back to DecisionSignalSetV1 signals in [0,1].

    Environment variables (official TypeSafe SDK names):
        TYPESAFE_API_KEY   - API key (required for live calls)
        TYPESAFE_BASE_URL  - Base URL (default: https://api.codiv.ai)

    Override the pinned model with:
        VERDICT_OPENJEV_MODEL (default: openjev-0.1)
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_ms: int | None = None,
        transport: Callable[
            [str, dict[str, str], dict[str, Any]], tuple[int, dict[str, str], bytes]
        ]
        | None = None,
    ) -> None:
        self.base_url = (
            base_url or os.environ.get("TYPESAFE_BASE_URL", "").strip() or "https://api.codiv.ai"
        )
        self.api_key = (
            api_key if api_key is not None else os.environ.get("TYPESAFE_API_KEY", "").strip()
        )
        self.model = os.environ.get("VERDICT_OPENJEV_MODEL", "").strip() or PINNED_MODEL
        self.transport = transport
        # Timeout from arg > env > default. Stored as seconds for http.client.
        if timeout_ms is not None:
            self.timeout_s = max(0.1, timeout_ms / 1000.0)
        else:
            env_ms_raw = os.environ.get("VERDICT_DECISION_SIGNALS_TIMEOUT_MS", "").strip()
            try:
                env_ms = int(env_ms_raw)
                self.timeout_s = max(0.1, env_ms / 1000.0)
            except (ValueError, TypeError):
                self.timeout_s = DEFAULT_TIMEOUT_MS / 1000.0

    def _build_state(self, question: DecisionQuestionV1) -> str:
        """Build scrubbed, minimal state from a DecisionQuestionV1."""
        from verdict.orchestration.receipt import scrub_secrets

        raw = question.task_summary[:500] if question.task_summary else ""
        scrubbed = scrub_secrets(raw)
        hints_part = ""
        if question.complexity_hints:
            with contextlib.suppress(Exception):
                hints_part = " " + json.dumps(question.complexity_hints)
        return f"{question.purpose}: {scrubbed}{hints_part}".strip()

    def _fail(
        self,
        *,
        failure_class: NormalizedFailureClass,
        request_id: str,
        purpose: str,
        input_digest: str,
        latency_ms: int,
        now: datetime,
    ) -> DecisionSignalSetV1:
        return DecisionSignalSetV1(
            schema_version="decision-signals/v1",
            provider="openjev",
            model=self.model,
            version="unknown",
            request_id=request_id,
            purpose=purpose,
            signals=None,
            confidence=0.0,
            latency_ms=latency_ms,
            usage={"input_tokens": 0, "output_tokens": 0},
            input_digest=input_digest,
            observed_at=now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            failure_class=failure_class,
            mode="SHADOW",
        )

    def signals(self, question: DecisionQuestionV1, *, now: datetime) -> DecisionSignalSetV1:
        """Return decision signals. NEVER raises; failures returned as signal set with failure_class."""
        input_digest = compute_input_digest(question)
        request_id = f"openjev-{now.isoformat()}"
        start_ts = now.timestamp()

        def elapsed_ms() -> int:
            return max(0, int((datetime.now(timezone.utc).timestamp() - start_ts) * 1000))

        if not self.api_key:
            return self._fail(
                failure_class=NormalizedFailureClass.UNKNOWN,
                request_id=request_id,
                purpose=question.purpose,
                input_digest=input_digest,
                latency_ms=0,
                now=now,
            )

        url = f"{self.base_url.rstrip('/')}{CODIV_SYSTEMONE_PATH}"
        state = self._build_state(question)
        payload: dict[str, Any] = {"model": self.model, "state": state, "questions": _QUESTIONS}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": _user_agent(),
        }

        try:
            if self.transport:
                status, response_headers, body = self.transport(url, headers, payload)
            else:
                import http.client
                import urllib.parse

                parsed = urllib.parse.urlparse(url)
                conn_class = (
                    http.client.HTTPSConnection
                    if parsed.scheme == "https"
                    else http.client.HTTPConnection
                )
                conn = conn_class(parsed.netloc, timeout=self.timeout_s)
                try:
                    conn.request(
                        "POST", parsed.path or "/", json.dumps(payload).encode("utf-8"), headers
                    )
                    resp = conn.getresponse()
                    status = resp.status
                    # Case-insensitive header dict
                    response_headers = {k.lower(): v for k, v in resp.getheaders()}
                    body = resp.read()
                finally:
                    conn.close()

            latency_ms = elapsed_ms()

            if status == 200:
                try:
                    data = json.loads(body.decode("utf-8"))
                    # Real response shape: {model, answers, usage}
                    answers = data.get("answers")
                    if not isinstance(answers, dict) or not answers:
                        raise ValueError("no answers")
                    usage_raw = data.get("usage", {})
                    usage = {
                        "input_tokens": int(usage_raw.get("input_tokens", 0)),
                        "output_tokens": int(usage_raw.get("output_tokens", 0)),
                    }
                    response_model = data.get("model", self.model)
                    # x-typesafe-request-id from response headers (case-insensitive)
                    rh_lower = (
                        {k.lower(): v for k, v in response_headers.items()}
                        if isinstance(response_headers, dict)
                        else {}
                    )
                    resp_request_id = rh_lower.get("x-typesafe-request-id", request_id)

                    signals, confidence = _answers_to_signals(answers)

                    return DecisionSignalSetV1(
                        schema_version="decision-signals/v1",
                        provider="openjev",
                        model=response_model,
                        version=response_model,  # version = exact model from response
                        request_id=resp_request_id,
                        purpose=question.purpose,
                        signals=signals if signals else None,
                        confidence=confidence,
                        latency_ms=latency_ms,
                        usage=usage,
                        input_digest=input_digest,
                        observed_at=now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                        failure_class=None,
                        mode="SHADOW",
                    )
                except Exception:
                    return self._fail(
                        failure_class=NormalizedFailureClass.INVALID_REQUEST,
                        request_id=request_id,
                        purpose=question.purpose,
                        input_digest=input_digest,
                        latency_ms=latency_ms,
                        now=now,
                    )

            # Error path: parse {"detail": {"error_type": ..., "message": ...}}
            error_type = ""
            rh_lower = (
                {k.lower(): v for k, v in response_headers.items()}
                if isinstance(response_headers, dict)
                else {}
            )
            retry_after_val = rh_lower.get("retry-after")

            try:
                err_data = json.loads(body.decode("utf-8"))
                detail = err_data.get("detail")
                if isinstance(detail, dict):
                    error_type = detail.get("error_type", "")
            except Exception:
                pass

            code = error_type or f"http_{status}"
            fail_sig = AdapterFailureSignal(
                code=code, status_code=status, retry_after=retry_after_val
            )
            normalized = normalize_failure(fail_sig, now=now)

            return self._fail(
                failure_class=normalized.failure_class,
                request_id=request_id,
                purpose=question.purpose,
                input_digest=input_digest,
                latency_ms=latency_ms,
                now=now,
            )

        except (TimeoutError, OSError) as exc:
            # http.client raises OSError("timed out") on socket timeout.
            is_timeout = isinstance(exc, TimeoutError) or "timed out" in str(exc).lower()
            return self._fail(
                failure_class=NormalizedFailureClass.TIMEOUT
                if is_timeout
                else NormalizedFailureClass.TRANSPORT,
                request_id=request_id,
                purpose=question.purpose,
                input_digest=input_digest,
                latency_ms=elapsed_ms(),
                now=now,
            )
        except Exception:
            return self._fail(
                failure_class=NormalizedFailureClass.TRANSPORT,
                request_id=request_id,
                purpose=question.purpose,
                input_digest=input_digest,
                latency_ms=elapsed_ms(),
                now=now,
            )


__all__ = [
    "DEFAULT_TIMEOUT_MS",
    "PINNED_MODEL",
    "_QUESTIONS",
    "_SCORE_LEVELS",
    "OpenJevSystemOneProvider",
    "_answers_to_signals",
    "normalize_failure",
    "parse_retry_after",
]
