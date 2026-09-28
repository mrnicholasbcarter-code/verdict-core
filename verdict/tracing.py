"""Optional OpenTelemetry tracing for orchestration runs.

Enable by installing ``verdict[tracing]`` **and** setting
``VERDICT_TRACING=1`` (or ``on`` / ``true``).  When either condition is
missing every public function is a zero-cost no-op and the
``opentelemetry`` package is never imported.

Spans are exported via OTLP to whatever ``OTEL_EXPORTER_OTLP_ENDPOINT``
points at (e.g. a local Phoenix server).  **No prompts, outputs, file
contents, API keys, or headers are ever attached** — only the keys in
``ALLOWED_ATTRIBUTES``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from types import TracebackType

# ---------------------------------------------------------------------------
# Attribute allowlist — only these keys may appear in span attributes.
# Anything else (especially secrets or large strings) is silently dropped.
# ---------------------------------------------------------------------------

ALLOWED_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "run_id",
        "goal_id",
        "repo",
        "event.type",
        "event.node_id",
        "event.seq",
        "event.attempt",
        "event.route_id",
        "event.ok",
        "event.failure_category",
        "event.state",
        "event.detail",
        "event.usage.prompt_tokens",
        "event.usage.completion_tokens",
        "event.usage.total_tokens",
        "event.duration_seconds",
    }
)

_MAX_ATTR_VALUE_LEN = 256

# ---------------------------------------------------------------------------
# Lazy singleton — nothing is imported until actually needed.
# ---------------------------------------------------------------------------

_SECRET_FRAGMENTS: frozenset[str] = frozenset(
    {"key", "token", "secret", "password", "credential", "auth", "cookie", "header"}
)


def _looks_secret(key: str) -> bool:
    lower = key.lower()
    return any(frag in lower for frag in _SECRET_FRAGMENTS)


def _is_enabled() -> bool:
    val = os.environ.get("VERDICT_TRACING", "").strip().lower()
    return val in ("1", "on", "true", "yes")


def _safe_value(value: Any) -> str | int | float | bool:
    """Coerce a value to an OTel-safe primitive, truncating long strings."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    s = str(value)
    if len(s) > _MAX_ATTR_VALUE_LEN:
        return s[:_MAX_ATTR_VALUE_LEN] + "…"
    return s


class _NoOpSpan:
    """Stand-in when tracing is off — every method is a no-op."""

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def set_status(self, *args: Any, **kwargs: Any) -> None:
        pass

    def end(self) -> None:
        pass

    def __enter__(self) -> _NoOpSpan:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        pass


_NOOP = _NoOpSpan()


class _TracerState:
    """Lazy-initialised singleton holding the real OTel tracer."""

    def __init__(self) -> None:
        self._tracer: Any = None
        self._initialised = False

    def _ensure(self) -> Any:
        if self._initialised:
            return self._tracer
        self._initialised = True
        try:
            from opentelemetry import trace
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter  # type: ignore[import-not-found]
            from opentelemetry.sdk.resources import Resource  # type: ignore[import-not-found]
            from opentelemetry.sdk.trace import TracerProvider  # type: ignore[import-not-found]
            from opentelemetry.sdk.trace.export import BatchSpanProcessor  # type: ignore[import-not-found]

            resource = Resource.create({"service.name": "verdict-orchestration"})
            provider = TracerProvider(resource=resource)
            exporter = OTLPSpanExporter()  # reads OTEL_EXPORTER_OTLP_ENDPOINT
            provider.add_span_processor(BatchSpanProcessor(exporter))
            trace.set_tracer_provider(provider)
            self._tracer = trace.get_tracer("verdict.tracing")
        except Exception:
            self._tracer = None
        return self._tracer

    @property
    def tracer(self) -> Any:
        return self._ensure()


_state = _TracerState()


def _filter_attributes(attrs: Mapping[str, Any]) -> dict[str, str | int | float | bool]:
    """Keep only allowed, non-secret, reasonably-sized attributes."""
    out: dict[str, str | int | float | bool] = {}
    for key, value in attrs.items():
        if key not in ALLOWED_ATTRIBUTES:
            continue
        if _looks_secret(key):
            continue
        out[key] = _safe_value(value)
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def start_run_span(run_id: str, goal_id: str, repo: str) -> _NoOpSpan | Any:
    """Create and return the root span for an orchestration run.

    The caller **must** call ``.end()`` (or use as a context manager) when
    the run finishes.
    """
    if not _is_enabled():
        return _NOOP
    tracer = _state.tracer
    if tracer is None:
        return _NOOP
    span = tracer.start_span("orchestration.run")
    safe = _filter_attributes({"run_id": run_id, "goal_id": goal_id, "repo": repo})
    for k, v in safe.items():
        span.set_attribute(k, v)
    return span


def record_event(
    parent_span: _NoOpSpan | Any, event_type: str, node_id: str, data: Mapping[str, Any]
) -> None:
    """Record a child span for a single emitted event."""
    if not _is_enabled():
        return
    tracer = _state.tracer
    if tracer is None:
        return
    if isinstance(parent_span, _NoOpSpan):
        return

    try:
        from opentelemetry import context as otel_context
        from opentelemetry import trace

        ctx = trace.set_span_in_context(parent_span)
        token = otel_context.attach(ctx)
        try:
            with tracer.start_as_current_span(f"event.{event_type}") as child:
                raw_attrs: dict[str, Any] = {"event.type": event_type, "event.node_id": node_id}
                # Lift selected data keys into the flat attribute namespace.
                for data_key in (
                    "attempt",
                    "route_id",
                    "ok",
                    "failure_category",
                    "state",
                    "detail",
                    "duration_seconds",
                ):
                    if data_key in data:
                        raw_attrs[f"event.{data_key}"] = data[data_key]

                # Token usage (nested under "usage" dict or flat).
                usage = data.get("usage")
                if isinstance(usage, Mapping):
                    for ukey in ("prompt_tokens", "completion_tokens", "total_tokens"):
                        if ukey in usage:
                            raw_attrs[f"event.usage.{ukey}"] = usage[ukey]

                safe = _filter_attributes(raw_attrs)
                for k, v in safe.items():
                    child.set_attribute(k, v)
        finally:
            otel_context.detach(token)
    except Exception:
        # Tracing must never break the orchestration run.
        pass
