from __future__ import annotations

import asyncio
import hashlib
import inspect
import ipaddress
import json
import logging
import math
import os
import re
import sys
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from concurrent.futures import Future
from contextlib import suppress
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from typing import Any, TypeVar, cast
from urllib.parse import urlsplit, urlunsplit

try:
    from fastapi import FastAPI, HTTPException, Query, Request
    from pydantic import BaseModel, Field
    from starlette.responses import JSONResponse, Response, StreamingResponse
except ImportError as exc:
    raise ImportError(
        "FastAPI is required for the web server mode. Install with "
        '`pip install "verdict-core[server]"` (or `verdict-core[all]`)'
    ) from exc

from verdict import __version__, worker_assignment
from verdict.actions.verified_models import (
    PublicModelsReadiness,
    PublicModelsUnavailableError,
    load_public_models,
)
from verdict.admission import AdmittedSet
from verdict.availability import OmniRouteAvailabilityAdapter
from verdict.availability_cache import AvailabilityCache
from verdict.catalog import configured_catalog_filters
from verdict.context_inject import InjectionRecord, inject_context_pack
from verdict.contracts import DEFAULT_PRIMARY_MODEL, redact_contract_secrets
from verdict.cost_ledger import PriceEvidenceInput
from verdict.effective_capability import (
    AssistanceCost,
    AssistancePlan,
    DecompositionRequirement,
    ProvenanceClaim,
    TaskSlice,
    VerificationStrategy,
)
from verdict.eligibility import EligibilityGate, allow_unverified_dev_from_env
from verdict.evidence import (
    AmbiguousEvidenceSelectorError,
    DurableEvidenceStore,
    EvidenceStore,
    ExplainEvidence,
    build_outcome_event,
    build_routing_decision_contract,
    request_features,
    safe_identifier,
)
from verdict.execution_path import ExecutionPathOffer, ExecutionPathRequest
from verdict.expected_cost import build_strategy_from_assistance
from verdict.gate import Gate
from verdict.guidance import (
    GuidanceConfig,
    GuidanceConfigurationError,
    GuidanceControlPlane,
    GuidanceUnavailableError,
)
from verdict.intelligence import (
    DEFAULT_PROFILE,
    DEFAULT_TIMEOUT_MS,
    IntelligenceService,
    StateLane,
    prepared_task_profile,
)
from verdict.model_passports import ModelPassport
from verdict.models import CONTEXT_WORKER_ASSIGNMENT, ModelInfo, ProviderConfig, RoutingDecision
from verdict.omniroute import CATALOG_MAX_RESPONSE_BYTES, OmniRouteHTTPTransport
from verdict.omniroute_webhook import MAX_BODY_BYTES as MAX_OMNIROUTE_WEBHOOK_BYTES
from verdict.omniroute_webhook import RECEIVER_PATH as OMNIROUTE_WEBHOOK_PATH
from verdict.omniroute_webhook import accept_delivery as accept_omniroute_webhook
from verdict.orchestration.health_cache import (
    PERMIT_PROOF_MAX_VALIDITY_SECONDS,
    DispatchScope,
    HealthCache,
    default_cache_path,
)
from verdict.orchestration.provider_catalog import (
    is_inband_billing_refusal,
    is_truncated_empty_reply,
)
from verdict.orchestration.verified_models import public_model_entry
from verdict.outcome_log import build_outcome_record, log_outcome, omniroute_anomaly_names
from verdict.paths import default_decision_log_path
from verdict.proxy import BufferedUpstreamResponse, StreamedUpstreamResponse, UpstreamProxy
from verdict.relay import (
    build_attempts,
    dispatch_scope,
    failure_class,
    idempotency_key,
    is_opaque_alias,
    protocol_for_surface,
    relay_failure_feedback,
    response_actual_route_status,
    retry_safety,
    retryable_response_status,
    transition_edge,
)
from verdict.runtime_certification import CertificationState
from verdict.security import bearer_matches, redact_text, validate_server_security
from verdict.serve_path import CONTEXT_EP_REQUEST
from verdict.session_economics import ConcreteRoute
from verdict.stream_terminal import (
    StreamOutcome,
    StreamProtocolError,
    StreamState,
    ValidatedSSEStream,
    identity_proof_status,
    prime_stream,
)
from verdict.task_profile import TaskProfileError


class _EvidenceStreamAdapter:
    """Own stream iteration, terminalization, and upstream cleanup."""

    def __init__(
        self,
        upstream: AsyncIterator[bytes],
        *,
        on_terminal: Any,
        event_factory: Any,
        outcome_of: Callable[[], StreamOutcome | None],
        event_prefix: str = "chat_completion",
    ) -> None:
        self._upstream = upstream
        self._outcome_of = outcome_of
        self._on_terminal = on_terminal
        self._event_factory = event_factory
        self._event_prefix = event_prefix
        self._terminal = False
        self._closed = False
        self._cleanup_task: asyncio.Task[Any] | None = None
        self._cleanup_error: str | None = None

    def __aiter__(self) -> _EvidenceStreamAdapter:
        return self

    async def __anext__(self) -> bytes:
        try:
            return await self._upstream.__anext__()
        except StopAsyncIteration:
            cleanup_error = await self._cleanup()
            await self._finish_at_eof(cleanup_error)
            raise
        except asyncio.CancelledError:
            cleanup_error = await self._cleanup()
            await self._finish(
                f"{self._event_prefix}_stream_aborted", "cancelled", "aborted", cleanup_error
            )
            raise
        except StreamProtocolError as exc:
            cleanup_error = await self._cleanup()
            await self._finish(
                f"{self._event_prefix}_stream_error",
                "error",
                "error",
                cleanup_error,
                error_class=getattr(exc, "code", None) or type(exc).__name__,
            )
            raise StopAsyncIteration from None
        except Exception as exc:
            cleanup_error = await self._cleanup()
            await self._finish(
                f"{self._event_prefix}_stream_error",
                "error",
                "error",
                cleanup_error,
                error_class=(getattr(exc, "code", None) or type(exc).__name__),
            )
            raise

    async def aclose(self) -> None:
        cleanup_error = await self._cleanup()
        await self._finish(
            f"{self._event_prefix}_stream_aborted", "cancelled", "aborted", cleanup_error
        )

    async def _finish_at_eof(self, cleanup_error: str | None) -> None:
        """Map the validated stream's one outcome; only a semantic success is success."""

        outcome = self._outcome_of()
        cleanup_error = cleanup_error or (outcome.cleanup_error if outcome else None)
        prefix = self._event_prefix
        if outcome is not None and outcome.state is StreamState.SUCCESS:
            await self._finish(f"{prefix}_streamed", "success", "completed", cleanup_error)
        elif outcome is not None and outcome.state is StreamState.FAILED:
            await self._finish(f"{prefix}_stream_failed", "failure", "completed", cleanup_error)
        elif outcome is not None and outcome.state is StreamState.INCOMPLETE:
            await self._finish(f"{prefix}_stream_incomplete", "partial", "completed", cleanup_error)
        else:
            await self._finish(
                f"{prefix}_stream_error",
                "error",
                "error",
                cleanup_error,
                error_class=outcome.state.value if outcome is not None else "missing_outcome",
            )

    async def _cleanup(self) -> str | None:
        if self._closed:
            if self._cleanup_task is not None:
                with suppress(BaseException):
                    await asyncio.shield(self._cleanup_task)
            return self._cleanup_error
        self._closed = True
        close = getattr(self._upstream, "aclose", None)
        if not callable(close):
            return None
        self._cleanup_task = asyncio.create_task(close())
        try:
            await asyncio.shield(self._cleanup_task)
        except BaseException as exc:
            self._cleanup_error = type(exc).__name__
            if not self._cleanup_task.done():
                self._cleanup_task.cancel()
            with suppress(BaseException):
                await asyncio.shield(self._cleanup_task)
        return self._cleanup_error

    async def _finish(
        self,
        event_type: str,
        outcome: str,
        phase: str,
        cleanup_error: str | None,
        *,
        error_class: str | None = None,
    ) -> None:
        if self._terminal:
            return
        self._terminal = True
        result = self._on_terminal(
            self._event_factory(event_type, outcome, phase, cleanup_error, error_class)
        )
        # The relay's terminal callback is a coroutine (its state work runs on the lane); a plain
        # callback, as a test passes, is called and done.
        if inspect.isawaitable(result):
            await result


class _EvidenceStreamingResponse(StreamingResponse):
    """Close the evidence-owned iterator even when ASGI send fails."""

    def __init__(self, content: _EvidenceStreamAdapter, **kwargs: Any) -> None:
        super().__init__(content, **kwargs)
        self._evidence_stream = content

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._evidence_stream.aclose()


class ModelPassportStore:
    """Bounded in-memory isolation-cache for qualified model passports.

    Keys are ``(provider, model_id)``; entries expire after a fixed TTL so a
    fresh qualification is never served stale, and the same key is never
    re-probed within the TTL window (isolation-key caching).
    """

    def __init__(self, *, ttl_seconds: int = 300) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[tuple[str, str], tuple[datetime, ModelPassport]] = {}

    def get(
        self, provider: str, model_id: str, *, now: datetime | None = None
    ) -> ModelPassport | None:
        current = now if now is not None else datetime.now(timezone.utc)
        entry = self._entries.get((provider, model_id))
        if entry is None:
            return None
        expires_at, passport = entry
        if current > expires_at:
            self._entries.pop((provider, model_id), None)
            return None
        return passport

    def put(self, passport: ModelPassport, *, now: datetime | None = None) -> None:
        current = now if now is not None else datetime.now(timezone.utc)
        self._entries[(passport.provider, passport.model_id)] = (
            current + timedelta(seconds=self._ttl),
            passport,
        )

    def list_fresh(self, *, now: datetime | None = None) -> list[ModelPassport]:
        current = now if now is not None else datetime.now(timezone.utc)
        fresh: list[ModelPassport] = []
        for key, (expires_at, passport) in list(self._entries.items()):
            if current <= expires_at:
                fresh.append(passport)
            else:
                self._entries.pop(key, None)
        return fresh


logger = logging.getLogger("verdict.api")
_T = TypeVar("_T")

# Singleton service instances
intelligence_instance: IntelligenceService | None = None
#: The one serial worker lane for the request path's blocking state work, created and shut down
#: with the app (``lifespan``).  ``None`` only before startup and after shutdown.
state_lane_instance: StateLane | None = None
gate_instance: Gate | None = None
proxy_instance: UpstreamProxy | None = None
# Optional canonical live admission for relay attempts. When set, the selected
# model and every alternative must be inside the admitted set; a provider error
# fails the relay closed (503) instead of falling back to catalog truth.
relay_admission_provider: Callable[[], AdmittedSet] | None = None


async def _on_state_lane(fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
    """Run synchronous request-path state work on the app's one serial lane and await it.

    The lane keeps the event loop free for ``/health`` and the cheap refusals while a decision or
    an evidence write is in progress, and runs jobs one at a time in the order they were
    submitted.  Errors surface exactly as if ``fn`` had been called here.  An app whose lifespan
    has not started has no lane (a test that sets the instances by hand): the work runs inline,
    as it always did.  Never wrap an upstream call in this.
    """
    lane = state_lane_instance
    if lane is None:
        return fn(*args, **kwargs)
    return await lane.run(fn, *args, **kwargs)


class CachedRelayAdmission:
    """Bounded cache for relay admission; ``get`` is strictly network-free.

    Refresh is an explicit lifecycle action. A stale or failed cache therefore
    fails closed rather than causing a request-time gateway GET or accepting an
    old admitted set as indefinite authority. The TTL never outlives the source
    evidence deadline handed to ``refresh``: copying a value renews nothing.
    """

    def __init__(
        self,
        refresh: Callable[[], AdmittedSet],
        *,
        ttl_seconds: float = 60.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("relay admission TTL must be positive")
        self._refresh = refresh
        self._ttl = timedelta(seconds=ttl_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._value: AdmittedSet | None = None
        self._expires_at: datetime | None = None
        self.last_refresh_failure: str | None = None

    def refresh(
        self, *, now: datetime | None = None, source_expires_at: datetime | None = None
    ) -> AdmittedSet:
        current = now or self._clock()
        try:
            value = self._refresh()
        except Exception as exc:
            self.last_refresh_failure = type(exc).__name__
            raise
        expires_at = current + self._ttl
        if source_expires_at is not None:
            expires_at = min(expires_at, source_expires_at)
        self._value = value
        self._expires_at = expires_at
        self.last_refresh_failure = None
        return value

    def invalidate(self) -> None:
        """Drop the cached value so a classified authority loss fails closed at once."""
        self._value = None
        self._expires_at = None

    def get(self, *, now: datetime | None = None) -> AdmittedSet:
        current = now or self._clock()
        if self._value is None:
            raise RuntimeError("relay_admission_cache_empty")
        if self._expires_at is None or current >= self._expires_at:
            raise RuntimeError("relay_admission_cache_stale")
        return self._value


#: Public-catalog readiness (BOD-435). The lifespan owns one refresh task that
#: reloads the local verified projection; request handlers only read it.
PUBLIC_MODELS_REFRESH_SECONDS = 20.0
PUBLIC_MODELS_LOAD_DEADLINE_SECONDS = 5.0
public_models_readiness: PublicModelsReadiness | None = None
public_models_task: asyncio.Task[None] | None = None


def _public_models_clock() -> datetime:
    return datetime.now(timezone.utc)


availability_cache_instance: AvailabilityCache | None = None
eligibility_gate_instance: EligibilityGate | None = None
evidence_store_instance: EvidenceStore | DurableEvidenceStore | None = None
guidance_plane_instance: GuidanceControlPlane | None = None
model_passport_store_instance: ModelPassportStore | None = None
#: Startup bootstrap classification recorded by ``lifespan``.
server_bootstrap_report: dict[str, Any] | None = None

DEFAULT_AVAILABILITY_TTL_SECONDS = 60
DEFAULT_AVAILABILITY_STALE_WINDOW_SECONDS = 30


def _normalize_omniroute_loopback(base_url: str) -> str:
    """Rewrite an ``http://localhost[:port]`` OmniRoute URL to a loopback IP literal.

    ``localhost`` is semantically identical to ``127.0.0.1`` for a local
    OmniRoute deployment, but :class:`OmniRouteHTTPTransport` requires an IP
    literal for plain HTTP destinations (verdict/omniroute.py).  Only the
    ``localhost`` hostname over plain HTTP is rewritten; every other rule the
    transport enforces (allow-listed https hosts, IPv6 ``[::1]``, path shape)
    is left untouched.
    """
    stripped = base_url.strip()
    parsed = urlsplit(stripped)
    if parsed.scheme.lower() != "http":
        return stripped
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if hostname != "localhost" or parsed.username or parsed.password:
        return stripped
    netloc = "127.0.0.1" if parsed.port is None else f"127.0.0.1:{parsed.port}"
    return urlunsplit(parsed._replace(netloc=netloc))


def _build_availability_cache() -> tuple[AvailabilityCache, EligibilityGate] | None:
    """Build the bounded availability cache backed by the native OmniRoute transport.

    Returns ``None`` when no OmniRoute endpoint is configured, so the server
    still boots without availability explainability.  The transport is
    loopback-only and credential-safe.

    A configured but invalid ``OMNIROUTE_BASE_URL`` raises ``RuntimeError`` so
    startup fails closed.  The ``LLMGATE_UPSTREAM_BASE_URL`` fallback is
    different: it is a direct-upstream proxy setting, not an OmniRoute
    endpoint, so a value the transport rejects (e.g. a public https host with
    no OmniRoute semantics) degrades to no availability cache with a logged
    warning instead of failing startup.
    """
    omniroute_url = os.getenv("OMNIROUTE_BASE_URL")
    fallback_url = os.getenv("LLMGATE_UPSTREAM_BASE_URL")
    base_url = omniroute_url or fallback_url
    if not base_url or base_url.strip().lower() in {"", "none"}:
        return None
    is_omniroute_configured = bool(
        omniroute_url and omniroute_url.strip().lower() not in {"", "none"}
    )
    normalized_url = (
        _normalize_omniroute_loopback(base_url) if is_omniroute_configured else base_url
    )
    from verdict.credentials_store import resolve_api_key, resolve_management_token

    api_key = resolve_api_key()
    # Environment first (blank ignored), then the same-named credential-store
    # field, so an unattended unit with no exported token still reads
    # connections (BOD-219, the BOD-345 resolver).
    management_token = resolve_management_token()
    usage_api_key_id = os.getenv("OMNIROUTE_USAGE_API_KEY_ID")
    allow_private = _allowed_private_hosts() | {
        item.strip().lower()
        for item in os.getenv("OMNIROUTE_ALLOW_PRIVATE_HOSTS", "").split(",")
        if item.strip()
    }
    try:
        transport = OmniRouteHTTPTransport(
            normalized_url,
            api_key=api_key,
            management_token=management_token,
            usage_api_key_id=usage_api_key_id,
            # providerHealth lists only the providers OmniRoute is monitoring right
            # now (it can shrink to a handful); connections covers every active one.
            runtime_sources={"health", "connections"} if management_token else {"health"},
            allow_private_hosts=allow_private,
            max_response_bytes=CATALOG_MAX_RESPONSE_BYTES,
        )
    except Exception as exc:
        if not is_omniroute_configured:
            # LLMGATE_UPSTREAM_BASE_URL is a direct-upstream proxy setting, not
            # an OmniRoute endpoint. A value OmniRoute's transport rejects is
            # not a misconfigured OmniRoute; degrade to no availability cache
            # rather than failing startup closed.
            logger.warning(
                "LLMGATE_UPSTREAM_BASE_URL is not usable as an OmniRoute "
                "availability endpoint (%s: %s); continuing without the "
                "availability cache and eligibility gate.",
                type(exc).__name__,
                exc,
            )
            return None
        # OMNIROUTE_BASE_URL is set but invalid: fail startup closed rather
        # than silently serving routing without the eligibility gate.
        hint = ""
        if isinstance(exc, ValueError) and "IP literal" in str(exc):
            hint = " (use a loopback IP literal such as http://127.0.0.1:20128)"
        raise RuntimeError(
            f"invalid OmniRoute configuration: {type(exc).__name__}: {exc}{hint}"
        ) from exc
    adapter: OmniRouteAvailabilityAdapter = OmniRouteAvailabilityAdapter(transport)
    # Issue #57 root cause: enrich the adapter with bounded live probes when the
    # production availability profile is enabled.  Reuses ProbeRunner + the
    # documented openai_probe_transport; disabled by default (development).
    probe_base_url = os.getenv("LLMGATE_PROBE_BASE_URL")
    probe_enabled = os.getenv("LLMGATE_AVAILABILITY_PROFILE", "development").lower() == "production"
    if probe_enabled and probe_base_url:
        from verdict.availability import ProbeEnrichedAdapter
        from verdict.probes import openai_probe_transport

        probe_consented = os.getenv("LLMGATE_ALLOW_LIVE_PROBES", "").lower() in {"1", "true", "yes"}
        probe_transport = openai_probe_transport(
            probe_base_url, api_key=os.getenv("LLMGATE_PROBE_API_KEY") or api_key
        )
        enriched: Any = ProbeEnrichedAdapter(
            adapter,
            probe_transport=probe_transport,
            enabled=True,
            live=True,
            consented=probe_consented,
            provider="omniroute",
        )
    else:
        enriched = adapter
    cache = AvailabilityCache(
        source=enriched.evaluate,
        ttl_seconds=DEFAULT_AVAILABILITY_TTL_SECONDS,
        stale_window_seconds=DEFAULT_AVAILABILITY_STALE_WINDOW_SECONDS,
    )
    from verdict.eligibility import EligibilityGate

    gate = EligibilityGate(
        cache.get,
        protected_fail_closed=True,
        allow_unverified_in_dev=allow_unverified_dev_from_env(),
    )
    return cache, gate


DEFAULT_UPSTREAM_BASE_URL = "http://127.0.0.1:20128/v1"
DEFAULT_MAX_REQUEST_BYTES = 2 * 1024 * 1024
DEFAULT_ALLOWED_PRIVATE_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _allowed_private_hosts() -> set[str]:
    configured = os.getenv("LLMGATE_UPSTREAM_ALLOW_PRIVATE_HOSTS", "")
    return DEFAULT_ALLOWED_PRIVATE_HOSTS | {
        item.strip().lower() for item in configured.split(",") if item.strip()
    }


def _resolve_upstream_base_url() -> str:
    """Prefer explicit llmgate upstream, else OmniRoute via ``OMNIROUTE_BASE_URL``.

    The built-in default is a last resort and is reported by
    :func:`server_bootstrap_diagnostics` as a ``default`` source, never applied
    silently.
    """
    from verdict.credentials_store import resolve_gateway_binding

    explicit = (os.getenv("LLMGATE_UPSTREAM_BASE_URL") or "").strip() or None
    return resolve_gateway_binding(base_url=explicit)[0]


def _upstream_base_url_source() -> str:
    """Name the bootstrap source that supplied the upstream base URL."""
    if (os.getenv("LLMGATE_UPSTREAM_BASE_URL") or "").strip():
        return "environment"
    if (os.getenv("OMNIROUTE_BASE_URL") or "").strip():
        return "environment"
    return "default"


def server_bootstrap_diagnostics(
    *, gateway_probe: Callable[[str], Any] | None = None
) -> dict[str, Any]:
    """Classify serve-path startup configuration via the shared contract.

    Separates configuration failure from gateway health and from model
    eligibility, so an operator reading startup output can tell which of the
    three went wrong. Never fatal: the serve path admits candidates at request
    time (an empty provider map is the documented serve posture), so this
    records and reports rather than refusing to boot. No gateway is started.

    ``gateway_probe`` is the readiness seam. It is called at most once, and only
    when the readiness report is actually requested: with both lifecycle opt-ins
    off there is no gateway I/O at all, so this function stays exactly as
    offline as it was before the lifecycle landed. Tests pass a fake.
    """
    from verdict.credentials_store import resolve_management_token
    from verdict.provider_bootstrap import (
        BootstrapError,
        load_credential_store_env,
        redact_url,
        resolve_provider_bootstrap,
    )

    upstream_source = _upstream_base_url_source()
    report: dict[str, Any] = {
        "upstream_base_url": redact_url(_resolve_upstream_base_url()),
        "upstream_base_url_source": upstream_source,
        "diagnostics": [],
    }
    if upstream_source == "default":
        report["diagnostics"].append(
            {
                "code": "default_upstream_base_url",
                "class": "configuration",
                "field": "LLMGATE_UPSTREAM_BASE_URL",
                "source": "default",
                "detail": (
                    f"no upstream configured; using the built-in default "
                    f"{DEFAULT_UPSTREAM_BASE_URL}"
                ),
                "remediation": "set LLMGATE_UPSTREAM_BASE_URL or OMNIROUTE_BASE_URL",
                "fatal": False,
            }
        )
    # The same resolver _build_availability_cache uses, so this names what serve reads.
    if resolve_management_token() is None:
        report["diagnostics"].append(
            {
                "code": "runtime_sources_health_only",
                "class": "configuration",
                "field": "OMNIROUTE_MANAGEMENT_TOKEN",
                "source": "missing",
                "detail": (
                    "runtime sources: health only (no management token); providers outside "
                    "OmniRoute's monitored set read as unknown"
                ),
                "remediation": "verdict credentials set OMNIROUTE_MANAGEMENT_TOKEN --stdin",
                "fatal": False,
            }
        )
    try:
        bootstrap = resolve_provider_bootstrap(credential_store_env=load_credential_store_env())
    except BootstrapError as exc:
        report["status"] = "configuration_incomplete"
        report["gateway_required"] = False
        report["gateway_url"] = None
        report["diagnostics"].extend(exc.to_dict()["diagnostics"])
        return report
    report["status"] = "ok"
    report["gateway_required"] = bootstrap.gateway_required
    report["gateway_url"] = redact_url(bootstrap.gateway_url) if bootstrap.gateway_url else None
    report["field_sources"] = dict(bootstrap.field_sources)
    report["diagnostics"].extend(d.to_dict() for d in bootstrap.diagnostics)
    report["gateway_lifecycle"] = _serve_gateway_lifecycle(bootstrap, probe=gateway_probe)
    return report


#: Reported instead of a state when nothing probed the gateway. Startup does no
#: gateway I/O of its own, so the absence of a probe is named rather than being
#: rendered as an unhealthy gateway nobody looked at.
GATEWAY_NOT_PROBED = "not_probed"


def _serve_gateway_lifecycle(
    bootstrap: Any, *, probe: Callable[[str], Any] | None = None
) -> dict[str, Any]:
    """Name the gateway lifecycle state for the serve startup line.

    Startup performs no gateway I/O unless it is asked to. With
    ``VERDICT_SERVE_ENSURE_GATEWAY`` off and no injected ``probe``, this reports
    ``not_probed`` without opening a socket, so importing or booting the server
    never depends on whether a gateway happens to be listening.

    ``VERDICT_SERVE_ENSURE_GATEWAY`` opts into the active path, which reuses a
    healthy gateway and starts one only when the bootstrap contract supplies a
    start command. An injected ``probe`` opts into the report-only path: the
    caller supplied the transport, so the caller decides whether I/O happens.
    """
    from verdict.credentials_store import GATEWAY_KEY_ENV_NAMES, resolve_gateway_binding
    from verdict.gateway_lifecycle import (
        authenticated_gateway_probe,
        ensure_gateway_ready,
        inspect_gateway,
    )
    from verdict.provider_bootstrap import load_credential_store_env

    ensure = os.getenv("VERDICT_SERVE_ENSURE_GATEWAY", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not ensure and probe is None:
        return {
            "state": GATEWAY_NOT_PROBED,
            "ready": False,
            "probed": False,
            "ensure_requested": False,
            "detail": (
                "gateway readiness was not probed at startup; run 'verdict doctor', or set "
                "VERDICT_SERVE_ENSURE_GATEWAY=true to ensure it at boot"
            ),
        }
    if probe is not None:
        probe_fn = probe
    else:
        # Match transport: bind the canonical URL/key resolver to lifecycle
        # discovery and never call the raw unauthenticated probe here.
        gateway_url, api_key = resolve_gateway_binding(base_url=bootstrap.gateway_url)
        custom_key_names = [
            binding.api_key_env
            for binding in bootstrap.providers.values()
            if binding.api_key_env
            and binding.api_key_env not in GATEWAY_KEY_ENV_NAMES
            and binding.base_url.rstrip("/") == gateway_url.rstrip("/")
        ]
        if custom_key_names:
            stored = load_credential_store_env()
            api_key = None
            for name in custom_key_names:
                api_key = (
                    (os.getenv(name) or "").strip() or (stored.get(name) or "").strip() or None
                )
                if api_key:
                    break
        bootstrap = replace(bootstrap, gateway_url=gateway_url)
        probe_fn = authenticated_gateway_probe(api_key)
    try:
        outcome = (
            ensure_gateway_ready(bootstrap, probe=probe_fn)
            if ensure
            else inspect_gateway(bootstrap, probe=probe_fn)
        )
    except Exception as exc:  # pragma: no cover - defensive: startup must not crash here
        return {"state": "unknown", "probed": True, "detail": f"{type(exc).__name__}: {exc}"}
    payload = outcome.to_dict()
    payload["probed"] = True
    payload["ensure_requested"] = ensure
    return payload


def _build_proxy() -> UpstreamProxy:
    """Build the configured upstream transport without reading client fields.

    Coding-agent harnesses point at ``verdict serve``; this transport points
    Verdict at OmniRoute (``OMNIROUTE_BASE_URL``) unless
    ``LLMGATE_UPSTREAM_BASE_URL`` is set explicitly.
    """
    from verdict.credentials_store import resolve_gateway_binding

    explicit_url = (os.getenv("LLMGATE_UPSTREAM_BASE_URL") or "").strip() or None
    base_url, api_key = resolve_gateway_binding(base_url=explicit_url)
    # Preserve the legacy per-proxy credential override.
    api_key = (os.getenv("LLMGATE_UPSTREAM_API_KEY") or "").strip() or api_key
    timeout_ms = int(os.getenv("LLMGATE_UPSTREAM_TIMEOUT_MS", "30000"))
    if timeout_ms <= 0:
        raise ValueError("LLMGATE_UPSTREAM_TIMEOUT_MS must be positive")
    return UpstreamProxy(
        base_url,
        api_key=api_key,
        timeout=timeout_ms / 1000,
        allow_private_hosts=_allowed_private_hosts(),
    )


#: Serve-path precedence for the three identity/telemetry fields: environment,
#: then the built-in default. The serve path deliberately does not adopt the
#: routing YAML for these three, because doing so would silently change an
#: already-deployed server's profile, identity and decision log.
_SERVE_ENV_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("primary_model", "LLMGATE_PRIMARY", DEFAULT_PRIMARY_MODEL),
    ("profile", "LLMGATE_INTELLIGENCE_PROFILE", DEFAULT_PROFILE),
    ("log_path", "LLMGATE_LOG_PATH", ""),
)


def _serve_field_resolution(bootstrap: Any | None) -> tuple[dict[str, str], list[Any]]:
    """Resolve the serve-path identity fields and report config-file disagreement.

    Returns the resolved values plus one non-fatal ``precedence_conflict``
    diagnostic per field where the routing YAML asks for a different value than
    the serve path applies. The conflict names both sources and both values, so a
    config the serve path does not honour is never silently ignored.
    """
    from verdict.provider_bootstrap import BootstrapDiagnostic, BootstrapSource

    values: dict[str, str] = {}
    conflicts: list[Any] = []
    for field_name, env_name, default in _SERVE_ENV_FIELDS:
        exported = (os.getenv(env_name) or "").strip()
        # The decision log default is resolved per call so it follows VERDICT_HOME.
        applied = exported or default or default_decision_log_path()
        applied_source: BootstrapSource = "environment" if exported else "default"
        values[field_name] = applied
        if bootstrap is None or bootstrap.source_of(field_name) != "config_file":
            continue
        configured = str(getattr(bootstrap, field_name))
        if configured == applied:
            continue
        conflicts.append(
            BootstrapDiagnostic(
                code="precedence_conflict",
                diagnostic_class="configuration",
                field=field_name,
                source=applied_source,
                detail=(
                    f"config file {bootstrap.config_path} sets {field_name}="
                    f"{configured!r} while the serve path applies {applied!r} from "
                    f"{applied_source} ({env_name}); the serve path resolves "
                    f"{field_name} from the environment, then the built-in default, "
                    f"and does not read the routing config for it"
                ),
                remediation=(
                    f"set {env_name}={configured} to apply the config value to "
                    f"'verdict serve', or remove {field_name} from "
                    f"{bootstrap.config_path}"
                ),
                fatal=False,
            )
        )
    return values, conflicts


def _build_intelligence() -> IntelligenceService:
    """Build the public IntelligenceService boundary from environment settings.

    ``primary_model``, ``profile`` and ``log_path`` keep the serve path's own
    precedence: environment, then the built-in default. The routing YAML is read
    only to detect and report a disagreement, never to override a running
    server's identity. The provider map stays empty on purpose: the serve path
    admits a concrete candidate per request rather than binding providers at
    startup.
    """
    from verdict.provider_bootstrap import (
        BootstrapError,
        load_credential_store_env,
        resolve_provider_bootstrap,
    )

    try:
        bootstrap = resolve_provider_bootstrap(credential_store_env=load_credential_store_env())
    except BootstrapError:
        bootstrap = None
    resolved, conflicts = _serve_field_resolution(bootstrap)
    for conflict in conflicts:
        print(f"verdict serve: {conflict.describe()}", file=sys.stderr)
    profile = resolved["profile"]
    timeout_ms = int(os.getenv("LLMGATE_INTELLIGENCE_TIMEOUT_MS", str(DEFAULT_TIMEOUT_MS)))
    allow_client_model_override = os.getenv(
        "LLMGATE_ALLOW_CLIENT_MODEL_OVERRIDE", "false"
    ).lower() in {"1", "true", "yes", "on"}
    frontier_allowlist_raw = os.getenv("LLMGATE_FRONTIER_ALLOWLIST")
    frontier_allowlist = (
        tuple(item.strip() for item in frontier_allowlist_raw.split(",") if item.strip())
        if frontier_allowlist_raw
        else None
    )
    primary_model = resolved["primary_model"]
    log_path = resolved["log_path"]
    providers: dict[str, ProviderConfig] = {}
    from verdict.outcome_shadow import resolve_outcome_shadow_roots

    return IntelligenceService(
        primary_model=primary_model,
        providers=providers,
        profile=profile,
        log_path=log_path,
        log_full_task=False,
        discovery_ttl=int(os.getenv("LLMGATE_DISCOVERY_TTL_SECONDS", "60")),
        timeout_ms=timeout_ms,
        frontier_allowlist=frontier_allowlist,
        allow_client_model_override=allow_client_model_override,
        # Serve path: free∩active admit selects; UpstreamProxy executes once.
        # CLI ``verdict route`` keeps its own execute_offload default.
        execute_offload=False,
        workspace_root=os.getenv("VERDICT_WORKSPACE_ROOT") or os.getcwd(),
        # BOD-304: default empty unless VERDICT_OUTCOME_SHADOW_ROOTS is set.
        outcome_shadow_roots=resolve_outcome_shadow_roots(None),
    )


def _build_model_passport_store() -> ModelPassportStore:
    """Build the in-memory model-passport isolation cache.

    Qualification requires a probe endpoint.  When none is configured the store
    is still built (empty) so ``/v1/models/qualify`` fails closed with a clear
    503 instead of crashing startup — same posture as the availability cache.
    """
    probe_base_url = os.getenv("LLMGATE_PROBE_BASE_URL")
    if not probe_base_url or probe_base_url.strip().lower() in {"", "none"}:
        return ModelPassportStore()
    ttl = max(1, int(os.getenv("LLMGATE_MODEL_PASSPORT_TTL", "300")))
    return ModelPassportStore(ttl_seconds=ttl)


async def lifespan(app: FastAPI) -> Any:
    global intelligence_instance, gate_instance, proxy_instance, evidence_store_instance
    global guidance_plane_instance, state_lane_instance
    validate_server_security(host=os.getenv("LLMGATE_HOST", "127.0.0.1"))
    global server_bootstrap_report
    server_bootstrap_report = server_bootstrap_diagnostics()
    lifecycle = server_bootstrap_report.get("gateway_lifecycle") or {}
    # Nothing probed the gateway, so there is no readiness fact to print. Startup
    # output with the opt-in off is what it was before the lifecycle landed.
    if lifecycle and lifecycle.get("probed"):
        print(
            f"verdict serve: gateway {lifecycle.get('state')} "
            f"url={lifecycle.get('gateway_url')} ready={lifecycle.get('ready')}",
            file=sys.stderr,
        )
    from verdict.provider_bootstrap import BootstrapDiagnostic

    for entry in server_bootstrap_report.get("diagnostics", []):
        # Rendered by the one diagnostic formatter, so a credential name in the
        # field position is never followed by ": ". The serve line has never
        # carried the source, so it is left out.
        line = BootstrapDiagnostic(
            code=entry["code"],
            diagnostic_class=entry["class"],
            field=entry["field"],
            detail=entry["detail"],
            remediation=entry["remediation"],
        ).describe()
        print(f"verdict serve: {line}", file=sys.stderr)
    intelligence_instance = _build_intelligence()
    gate_instance = Gate(
        primary_model=intelligence_instance.primary_model,
        providers=intelligence_instance.providers,
        intelligence_service=intelligence_instance,
    )
    proxy_instance = _build_proxy()
    global availability_cache_instance, eligibility_gate_instance
    global model_passport_store_instance
    model_passport_store_instance = _build_model_passport_store()
    built = _build_availability_cache()
    availability_cache_instance, eligibility_gate_instance = (
        built if built is not None else (None, None)
    )
    evidence_db = os.getenv("VERDICT_RECEIPTS_DB") or os.getenv("VERDICT_EVIDENCE_DB")
    max_entries = max(1, int(os.getenv("VERDICT_EVIDENCE_MAX_ENTRIES", "256")))
    if evidence_db:
        evidence_store_instance = DurableEvidenceStore(evidence_db, max_entries=max_entries)
    elif os.getenv("LLMGATE_ALLOW_ANONYMOUS", "false").lower() in {"1", "true", "yes", "on"}:
        # Anonymous development/test mode has an explicit in-memory backend.
        # Authenticated deployments must configure a durable DB path.
        evidence_store_instance = DurableEvidenceStore(":memory:", max_entries=max_entries)
    else:
        raise RuntimeError("VERDICT_RECEIPTS_DB must be configured for authenticated API mode")
    # Guidance is opt-in and host-neutral. Keep normal routing startup
    # independent of project instruction files and optional agent tooling.
    try:
        guidance_config = GuidanceConfig.from_environment()
    except GuidanceConfigurationError as exc:
        # Invalid opt-in configuration must be visible as degraded guidance,
        # while leaving the normal routing service available.
        guidance_config = GuidanceConfig(
            enabled=True, repo_root=Path.cwd(), guidance_path=Path.cwd() / "GUIDANCE.md"
        )
        guidance_plane_instance = GuidanceControlPlane.degraded(guidance_config, str(exc))
    else:
        if not guidance_config.enabled:
            guidance_plane_instance = GuidanceControlPlane.disabled(guidance_config)
        else:
            guidance_plane_instance = await GuidanceControlPlane.initialize(guidance_config)
    # Issue #57: feed the eligibility gate into the IntelligenceService so the
    # live routing path filters before ranking (single source of truth).
    if eligibility_gate_instance is not None:
        intelligence_instance.eligibility_gate = eligibility_gate_instance
    # The public catalog and the optional relay admission cache share one local
    # snapshot. The cache is refreshed by the single lifespan task below and
    # every request reads only bounded in-memory values; no request path
    # performs a gateway GET or a model call. Persisted ladder state is
    # evidence, never cache authority.
    global relay_admission_provider, public_models_readiness, public_models_task
    from verdict.free_tier_admit import omniroute_endpoint_from_env

    endpoint = omniroute_endpoint_from_env(getattr(intelligence_instance, "providers", None))
    relay_cache: CachedRelayAdmission | None = None
    pending: dict[str, AdmittedSet] = {}
    if os.getenv("VERDICT_RELAY_ADMISSION_CACHE", "false").lower() in {"1", "true", "yes", "on"}:
        relay_cache = CachedRelayAdmission(
            lambda: pending["admitted"],
            ttl_seconds=float(os.getenv("VERDICT_RELAY_ADMISSION_TTL_SECONDS", "60")),
            clock=lambda: _public_models_clock(),
        )
        relay_admission_provider = relay_cache.get
    public_models_readiness = PublicModelsReadiness()
    readiness = public_models_readiness

    async def refresh_public_models() -> None:
        if endpoint is None:
            readiness.fail("inventory_absent")
            return
        now = _public_models_clock()
        try:
            state = await asyncio.wait_for(
                asyncio.to_thread(
                    load_public_models,
                    gateway=endpoint[0],
                    api_key=endpoint[1],
                    now=now,
                    with_admission=relay_cache is not None,
                ),
                PUBLIC_MODELS_LOAD_DEADLINE_SECONDS,
            )
        except PublicModelsUnavailableError as exc:
            readiness.fail(exc.code)
            if relay_cache is not None:
                relay_cache.invalidate()
            return
        except Exception as exc:
            # A load that failed or timed out keeps the last-good snapshot only
            # until its own original deadline; nothing here extends it.
            readiness.fail(type(exc).__name__, keep_last_good=True)
            logger.warning("public models refresh failed: %s", type(exc).__name__)
            return
        readiness.publish(state)
        if relay_cache is not None and state.admitted is not None:
            pending["admitted"] = state.admitted
            relay_cache.refresh(now=now, source_expires_at=state.source_expires_at)

    # Cancel alone cannot stop this loop: before Python 3.12, asyncio.wait_for
    # returns the inner result and drops a cancel that lands as the load
    # finishes (bpo-42130), so the loop would run on and shutdown would hang
    # (issue #1149). The flag ends the loop at its next check either way.
    stopping = False

    async def public_models_loop() -> None:
        while not stopping:
            await asyncio.sleep(PUBLIC_MODELS_REFRESH_SECONDS)
            if stopping:
                return
            await refresh_public_models()

    await refresh_public_models()
    public_models_task = asyncio.create_task(public_models_loop(), name="public-models-refresh")
    # Last, so a startup failure above never leaves a lane behind.  The service decides on it and
    # the handlers record on it: one worker, one order.
    lane = StateLane()
    state_lane_instance = lane
    service = intelligence_instance
    service.state_lane = lane
    try:
        yield
        task, public_models_task = public_models_task, None
        if task is not None:
            stopping = True
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        public_models_readiness = None
    finally:
        # However the app ends (an exception thrown at the yield, a failed refresh task), the
        # lane is released: queued jobs (a terminal evidence write may still be waiting) get a
        # bounded wait, then the worker stops.
        try:
            await lane.aclose()
        finally:
            state_lane_instance = None
            service.state_lane = None
    intelligence_instance = None
    gate_instance = None
    proxy_instance = None
    availability_cache_instance = None
    eligibility_gate_instance = None
    evidence_store_instance = None
    guidance_plane_instance = None
    model_passport_store_instance = None
    relay_admission_provider = None


app = FastAPI(
    title="verdict API",
    description="Microservice for Tier-based LLM Routing",
    version=__version__,
    lifespan=lifespan,
)


#: Every worker assignment starts with this (the format version and a dot).
_ASSIGNMENT_PREFIX = f"{worker_assignment.VERSION}."
#: The only paths that accept an assignment as the bearer, and only by ``POST``: the OpenAI
#: relay surfaces a worker calls.  Everything else (guidance, route, runs, models, the Anthropic
#: surface) takes the serve token alone.
_ASSIGNMENT_RELAY_PATHS = frozenset({"/v1/chat/completions", "/v1/responses"})


def _presents_assignment(request: Request) -> bool:
    """True when the bearer or ``x-api-key`` value carries the assignment prefix.

    Only the prefix is read: this never decodes, verifies or loads a key, so it is safe to run
    where no assignment may be honoured.
    """
    scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and supplied.startswith(_ASSIGNMENT_PREFIX):
        return True
    return request.headers.get("x-api-key", "").startswith(_ASSIGNMENT_PREFIX)


async def _authenticate_assignment(request: Request, supplied: str, call_next: Any) -> Response:
    """Verify a worker assignment presented as the bearer on a relay POST.

    The key is read from its file on every request (a rotation needs no restart), in a worker
    thread so a slow home filesystem never stalls the event loop.  A key that cannot be loaded
    fails closed with 503; any verification failure is the same generic 403 as a wrong bearer,
    and the log carries the reason code only, never the token.  On success the verified
    assignment travels in ``request.state``, which no header or body can write.
    """
    try:
        key = await asyncio.to_thread(worker_assignment.load_assignment_key)
    except worker_assignment.AssignmentError as exc:
        logger.warning("worker assignment refused: reason=%s", exc.reason)
        return _proxy_error(503, "worker assignments are not configured")
    try:
        verified = worker_assignment.verify_assignment(
            supplied, key, now=datetime.now(timezone.utc)
        )
    except worker_assignment.AssignmentError as exc:
        logger.warning("worker assignment refused: reason=%s", exc.reason)
        return _proxy_error(403, "authentication failed")
    request.state.worker_assignment = verified
    return cast(Response, await call_next(request))


@app.middleware("http")
async def caller_authentication(request: Request, call_next: Any) -> Response:
    """Require server-owned bearer auth for every non-health API route.

    The OmniRoute webhook receiver authenticates each delivery by its HMAC
    signature instead, because OmniRoute cannot send Verdict's bearer token.
    ``POST /v1/messages`` also accepts the same server token as ``x-api-key``.
    A signed worker assignment (``vwa1.`` bearer) is accepted only on the two OpenAI relay
    POST paths, and never in anonymous mode.
    """
    if request.url.path in {"/health", OMNIROUTE_WEBHOOK_PATH}:
        return cast(Response, await call_next(request))
    token = os.getenv("LLMGATE_AUTH_TOKEN")
    anonymous = os.getenv("LLMGATE_ALLOW_ANONYMOUS", "false").lower() in {"1", "true", "yes", "on"}
    if anonymous and not token:
        # Defense-in-depth: anonymous mode must only serve loopback clients.
        # Only real loopback IP addresses qualify; hostnames are never trusted.
        host = request.client.host if request.client is not None else ""
        try:
            _is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            _is_loopback = False
        if not _is_loopback:
            return _proxy_error(
                403, "anonymous mode is loopback-only", surface=_auth_surface(request)
            )
        if _presents_assignment(request):
            # Anonymous mode has no serve token to stand behind and grants no assigned lane, so
            # an assignment is refused outright on every path and method: nothing is verified
            # and the key file is never read.
            return _proxy_error(403, "authentication failed", surface=_auth_surface(request))
        return cast(Response, await call_next(request))
    if not token:
        return _proxy_error(
            503, "server authentication is not configured", surface=_auth_surface(request)
        )
    authorization = request.headers.get("authorization", "")
    scheme, _, supplied = authorization.partition(" ")
    api_key = request.headers.get("x-api-key", "") if request.url.path == "/v1/messages" else ""
    bearer_present = scheme.lower() == "bearer" and bool(supplied)
    if any(
        value.startswith(_ASSIGNMENT_PREFIX) and not value.isascii()
        for value in (api_key, supplied if bearer_present else "")
    ):
        # The constant-time compares below raise on non-ASCII text.  An assignment is ASCII, so a
        # non-ASCII one in either header is a refusal, never a server error.
        return _proxy_error(403, "authentication failed", surface=_auth_surface(request))
    if api_key and bearer_present and not bearer_matches(api_key, supplied):
        return _proxy_error(403, "authentication failed", surface="messages")
    if api_key:
        if bearer_matches(api_key, token):
            return cast(Response, await call_next(request))
        return _proxy_error(403, "authentication failed", surface="messages")
    if not bearer_present:
        return JSONResponse(
            status_code=401,
            content=_auth_required_body(request),
            headers={"www-authenticate": "Bearer"},
        )
    is_assignment = supplied.startswith(_ASSIGNMENT_PREFIX)
    if not bearer_matches(supplied, token):
        if (
            is_assignment
            and request.method == "POST"
            and request.url.path in _ASSIGNMENT_RELAY_PATHS
        ):
            return await _authenticate_assignment(request, supplied, call_next)
        return _proxy_error(403, "authentication failed", surface=_auth_surface(request))
    return cast(Response, await call_next(request))


def _auth_surface(request: Request) -> str | None:
    return "messages" if request.url.path == "/v1/messages" else None


def _auth_required_body(request: Request) -> dict[str, Any]:
    error = {"message": "authentication required", "type": "authentication_error"}
    if request.url.path == "/v1/messages":
        return {"type": "error", "error": error}
    return {"error": error}


@app.middleware("http")
async def no_store_cache_headers(request: Request, call_next: Any) -> Response:
    """Mark every response non-cacheable; this is a routing control-plane API, never a static asset host."""
    response = cast(Response, await call_next(request))
    response.headers["Cache-Control"] = "no-store"
    return response


class RouteRequest(BaseModel):
    task: str
    criticality: str = "medium"
    model: str | None = None
    allow_client_model_override: bool = False
    protected: bool = False
    privacy_class: str = "any"
    tools_required: bool = False
    structured_output_required: bool = False
    vision_required: bool = False
    streaming_required: bool = False
    request_id: str | None = None
    correlation_id: str | None = None
    execution_path_request: dict[str, Any] | None = None
    execution_path_decision: dict[str, Any] | None = None
    task_family: str | None = None
    min_context_tokens: int | None = Field(default=None, gt=0)
    reasoning_required: bool = False


def _public_execution_path_request(
    raw: Any, *, task: str, context: Mapping[str, Any] | None = None
) -> ExecutionPathRequest:
    """Validate the public JSON optimizer contract and construct trusted types."""

    from verdict.execution_path import EXECUTION_PATH_SCHEMA_VERSION, ExecutionPathError

    if not isinstance(raw, dict):
        raise ExecutionPathError("execution_path_request must be a JSON object")
    if raw.get("schema_version") != EXECUTION_PATH_SCHEMA_VERSION:
        raise ExecutionPathError(
            f"execution_path_request.schema_version must be {EXECUTION_PATH_SCHEMA_VERSION!r}"
        )
    hard_spend_budget = _public_hard_spend_budget(raw)
    trajectory_id = raw.get("trajectory_id")
    slice_id = raw.get("slice_id")
    candidates = raw.get("candidates")
    if not isinstance(trajectory_id, str) or not trajectory_id.strip():
        raise ExecutionPathError("execution_path_request.trajectory_id must be non-empty")
    if not isinstance(slice_id, str) or not slice_id.strip():
        raise ExecutionPathError("execution_path_request.slice_id must be non-empty")
    if not isinstance(candidates, list) or not candidates:
        raise ExecutionPathError("execution_path_request.candidates must be a non-empty array")
    acceptance = raw.get("acceptance_criteria", [])
    proof = raw.get("proof_criteria", [])
    if not isinstance(acceptance, list) or not all(isinstance(item, str) for item in acceptance):
        raise ExecutionPathError("execution_path_request.acceptance_criteria must be strings")
    if not isinstance(proof, list) or not proof or not all(isinstance(item, str) for item in proof):
        raise ExecutionPathError("execution_path_request.proof_criteria must be non-empty strings")
    task_slice = TaskSlice(
        slice_id=slice_id.strip(),
        objective=task,
        acceptance_criteria=tuple(acceptance),
        proof_criteria=tuple(proof),
    )
    now = datetime.now(timezone.utc)
    offers: list[ExecutionPathOffer] = []
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise ExecutionPathError(
                f"execution_path_request.candidates[{index}] must be an object"
            )
        required_strings = ("route_id", "gateway", "provider", "model")
        for field_name in required_strings:
            if not isinstance(candidate.get(field_name), str) or not candidate[field_name].strip():
                raise ExecutionPathError(
                    f"execution_path_request.candidates[{index}].{field_name} must be non-empty"
                )
        strategy = candidate.get("strategy", "direct_cheap")
        if not isinstance(strategy, str):
            raise ExecutionPathError(
                f"execution_path_request.candidates[{index}].strategy must be a string"
            )
        try:
            capability_tier = int(candidate.get("capability_tier", 2))
            execution_tokens = int(candidate.get("execution_tokens", 1))
            verification_tokens = int(candidate.get("verification_tokens", 1))
        except (TypeError, ValueError) as exc:
            raise ExecutionPathError(
                f"execution_path_request.candidates[{index}] token/tier fields must be integers"
            ) from exc
        if execution_tokens < 1 or verification_tokens < 1:
            raise ExecutionPathError(
                f"execution_path_request.candidates[{index}] execution and verification tokens must be positive"
            )
        certification = candidate.get("certification_state")
        try:
            certification_state = CertificationState(str(certification))
        except ValueError as exc:
            raise ExecutionPathError(
                f"execution_path_request.candidates[{index}].certification_state is invalid"
            ) from exc
        route_id = candidate["route_id"].strip()
        evidence_payload = json.dumps(candidate, sort_keys=True, separators=(",", ":"))
        evidence_digest = "sha256:" + hashlib.sha256(evidence_payload.encode()).hexdigest()
        assistance = AssistanceCost(verification_tokens=verification_tokens)
        plan = AssistancePlan(
            plan_id=f"api:{trajectory_id}:{index}",
            candidate_id=route_id,
            task_slice=task_slice,
            required_intrinsic_capabilities=(),
            required_context_slots=(),
            required_context_evidence=(),
            required_tool_capabilities=(),
            selected_tool_surface=(),
            decomposition=DecompositionRequirement(required=False),
            verification=VerificationStrategy(
                kind="caller_proof_contract", proof_criteria=tuple(proof)
            ),
            assistance_cost=assistance,
            result="sufficient" if candidate.get("eligible") is True else "insufficient",
            reasons=("public_optimizer_contract",),
            intrinsic_sufficient=candidate.get("eligible") is True,
            assisted_sufficient=candidate.get("eligible") is True,
            assistance_delta=(),
            provenance=(
                ProvenanceClaim(
                    kind="optimizer_contract",
                    claim=route_id,
                    source="api:execution_path_request",
                    digest=evidence_digest,
                    observed_at=now.isoformat(),
                    freshness=str(candidate.get("certification_freshness", "unknown")),
                    fresh=str(candidate.get("certification_freshness", "unknown")) == "fresh",
                ),
            ),
            evidence_digest=evidence_digest,
        )
        is_free = candidate.get("is_free") is True
        raw_price = candidate.get("price")
        price = cast(PriceEvidenceInput, raw_price) if isinstance(raw_price, dict) else None
        if is_free and price is None:
            price = PriceEvidenceInput(
                input_usd_per_mtok="0",
                output_usd_per_mtok="0",
                observed_at=now.isoformat(),
                evidence_id=evidence_digest,
            )
        # Missing price alone is not a gate without an explicit hard spend budget
        route = ConcreteRoute(
            route_id=route_id,
            gateway=candidate["gateway"].strip(),
            provider=candidate["provider"].strip(),
            model=candidate["model"].strip(),
            credential_pool=candidate.get("credential_pool"),
            capability_tier=capability_tier,
            eligible=candidate.get("eligible") is True,
            excluded=candidate.get("excluded") is True,
            exclusion_reason=candidate.get("exclusion_reason"),
        )
        expected = build_strategy_from_assistance(
            strategy_id=f"{strategy}:{route_id}",
            trajectory_id=trajectory_id.strip(),
            assistance=assistance,
            execution_tokens=execution_tokens,
            price=price,
            is_free=is_free,
            qualified=candidate.get("eligible") is True,
            free_first_preferred=True,
            now=now,
        )
        offers.append(
            ExecutionPathOffer(
                strategy=strategy,  # type: ignore[arg-type]
                route=route,
                assistance_plan=plan,
                expected_cost=expected,
                certification_state=certification_state,
                certification_freshness=str(candidate.get("certification_freshness", "unknown")),
                hard_excluded=candidate.get("excluded") is True,
                is_cheap=is_free or candidate.get("is_cheap") is True,
                is_paid=candidate.get("is_paid") is True,
                is_frontier=candidate.get("is_frontier") is True,
            )
        )
    ctx_map = context or {}
    task_family = raw.get("task_family") or ctx_map.get("task_family")
    if not isinstance(task_family, str) or not task_family.strip():
        task_family = None
    else:
        task_family = task_family.strip().lower()

    raw_min_ctx = raw.get("min_context_tokens") or ctx_map.get("min_context_tokens")
    min_context_tokens = None
    if isinstance(raw_min_ctx, int) and not isinstance(raw_min_ctx, bool):
        if raw_min_ctx <= 0:
            raise ExecutionPathError("min_context_tokens must be positive")
        min_context_tokens = raw_min_ctx

    reasoning_required = bool(raw.get("reasoning_required") or ctx_map.get("reasoning_required"))
    tools_required = bool(raw.get("tools_required") or ctx_map.get("tools_required"))
    structured_output_required = bool(
        raw.get("structured_output_required") or ctx_map.get("structured_output_required")
    )
    streaming_required = bool(raw.get("streaming_required") or ctx_map.get("streaming_required"))
    vision_required = bool(raw.get("vision_required") or ctx_map.get("vision_required"))

    return ExecutionPathRequest(
        task_slice=task_slice,
        trajectory_id=trajectory_id.strip(),
        offers=tuple(offers),
        assumptions=("public_json_validated_at_api_boundary",),
        now=now,
        task_family=task_family,
        min_context_tokens=min_context_tokens,
        reasoning_required=reasoning_required,
        tools_required=tools_required,
        structured_output_required=structured_output_required,
        streaming_required=streaming_required,
        vision_required=vision_required,
        hard_spend_budget=hard_spend_budget,
    )


def _public_hard_spend_budget(raw: Mapping[str, Any]) -> float | None:
    """Validate the optional USD cap; an absent key means no budget."""

    from verdict.execution_path import ExecutionPathError

    if "hard_spend_budget" not in raw:
        return None
    value = raw["hard_spend_budget"]
    error = ExecutionPathError(
        "execution_path_request.hard_spend_budget must be a positive finite number"
    )
    # bool is an int subclass, so it must be refused before the numeric check.
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise error
    try:
        budget = float(value)
    except OverflowError as exc:  # an integer too large for a float
        raise error from exc
    if not math.isfinite(budget) or budget <= 0:
        raise error
    return budget


RESERVED_WEAKENING_FLAGS = frozenset(
    {
        "allow_legacy_selector",
        "require_execution_path_authority",
        "execution_path_decision",
        "legacy_non_authority_selector",
        "bod104_execution_path_authority",
        "strategy_authority",
        "client_certificate",
        "client_cert",
        "certificate",
        "raw_decision",
        "routing_decision",
    }
)

#: Verdict-local fields stripped from relayed bodies before any upstream send.
#: Derived from the reserved weakening flags plus the typed-requirement keys so
#: the two lists cannot drift apart.
_RELAY_LOCAL_FIELDS: frozenset[str] = RESERVED_WEAKENING_FLAGS | frozenset(
    {
        "request_id",
        "correlation_id",
        "criticality",
        "idempotency_key",
        "execution_path_request",
        "task_family",
        "min_context_tokens",
        "reasoning_required",
        "tools_required",
        "structured_output_required",
        "vision_required",
        "streaming_required",
    }
)


def _authority_context(payload: dict[str, Any], *, task: str) -> dict[str, Any]:
    """Reject raw decisions and replace public JSON with a trusted request."""

    from verdict.execution_path import ExecutionPathError

    for key in RESERVED_WEAKENING_FLAGS:
        if payload.get(key) is not None:
            if key == "execution_path_decision":
                raise ExecutionPathError("client-supplied execution_path_decision is not accepted")
            raise ExecutionPathError(f"reserved control field {key!r} is not accepted")

    # Underscore keys are server-internal (e.g. the trusted dispatch origin set
    # after this call); a client payload never supplies them.
    context = {key: value for key, value in payload.items() if not str(key).startswith("_")}

    # Server policy: critical tasks are protected and cannot be lowered by client fields
    if (
        str(payload.get("criticality", "")).strip().lower() == "critical"
        or payload.get("protected") is True
    ):
        context["protected"] = True

    raw = context.get(CONTEXT_EP_REQUEST)
    if raw is not None:
        context[CONTEXT_EP_REQUEST] = _public_execution_path_request(
            raw, task=task, context=context
        )
    return context


async def _route_with_intelligence(
    task: str, criticality: str, context: dict[str, Any] | None = None
) -> Any:
    if intelligence_instance is None:
        raise HTTPException(status_code=503, detail="Intelligence service not initialized")
    # legacy selector demotion: default API serve path requires execution-path authority (no silent invent).
    from verdict.serve_path import CONTEXT_REQUIRE_AUTHORITY

    merged = dict(context or {})
    merged.setdefault(CONTEXT_REQUIRE_AUTHORITY, True)
    try:
        return await intelligence_instance.route(task, criticality=criticality, context=merged)
    except Exception as exc:
        from verdict.execution_path import ExecutionPathError

        if isinstance(exc, ExecutionPathError):
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        raise


@app.post("/v1/route")
async def route_task(
    request: Request, req: RouteRequest, include_routing_trace: bool = False
) -> Response:
    raw_context = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    try:
        context = _authority_context(raw_context, task=req.task)
    except Exception as exc:
        from verdict.execution_path import ExecutionPathError

        if isinstance(exc, ExecutionPathError):
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        raise
    try:
        decision = await _route_with_intelligence(req.task, req.criticality, context=context)
    except HTTPException as exc:
        from verdict.serve_path import TracedExecutionPathError

        cause = exc.__cause__
        if include_routing_trace and isinstance(cause, TracedExecutionPathError):
            # Same status and detail as the legacy no-selection error, plus the trace.
            return JSONResponse(
                content={"detail": exc.detail, "routing_trace": cause.routing_trace},
                status_code=exc.status_code,
            )
        raise
    route_evidence, evidence_key = await _start_evidence_on_lane(
        decision,
        cancelled_event_prefix="route_decision",
        task=req.task,
        criticality=req.criticality,
        features={
            "stream": req.streaming_required,
            "tools": req.tools_required,
            "response_format": "structured" if req.structured_output_required else None,
            "vision": req.vision_required,
            "tool_count": 0,
            "tool_names": [],
        },
        request_id=req.request_id,
        correlation_id=req.correlation_id,
        scope=_evidence_scope(request),
    )
    outcome = build_outcome_event(
        route_evidence.routing_decision,
        event_type="route_decision_recorded",
        outcome="denied" if decision.decision == "denied" else "success",
        features={"route_only": True},
    )
    route_evidence = await _on_state_lane(_finish_evidence, route_evidence, outcome, evidence_key)
    status_code = 200 if decision.decision != "denied" else 503
    headers: dict[str, str] = {}
    if route_evidence.evidence_id:
        headers["x-verdict-evidence-id"] = route_evidence.evidence_id
    headers["x-verdict-evidence-request-id"] = route_evidence.routing_decision.request_id or ""
    headers["x-verdict-correlation-id"] = route_evidence.routing_decision.correlation_id or ""
    content = _safe_decision_dict(decision)
    if include_routing_trace and decision.routing_trace is not None:
        content["routing_trace"] = decision.routing_trace
    return JSONResponse(content=content, status_code=status_code, headers=headers)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy", "engine": "verdict"}


@app.get("/v1/guidance/status")
async def guidance_status() -> dict[str, Any]:
    """Return opt-in guidance status without exposing policy contents."""

    if guidance_plane_instance is None:
        return {"status": "disabled", "enabled": False, "engine": "verdict"}
    return {
        "status": guidance_plane_instance.status.state,
        "enabled": guidance_plane_instance.status.enabled,
        "reason": guidance_plane_instance.status.reason,
        "policy_version": guidance_plane_instance.status.policy_version,
        "initialization_ms": guidance_plane_instance.status.initialization_ms,
        "engine": "verdict",
    }


@app.get("/ready")
async def ready() -> Response:
    """Report process readiness and verify that the configured upstream responds."""
    if intelligence_instance is None or proxy_instance is None:
        raise HTTPException(status_code=503, detail="Gate engine not initialized")

    intel = intelligence_instance.readiness()
    try:
        upstream = await proxy_instance.models()
        upstream_ok = upstream.status_code < 400
    except Exception as exc:
        upstream_ok = False
        upstream = None
        upstream_error = str(exc)
    else:
        upstream_error = ""

    overall_status = intel.status if upstream_ok else "not_ready"
    status_code = 200 if overall_status in {"ready", "degraded"} else 503
    content: dict[str, Any] = {
        "status": overall_status,
        "engine": "verdict",
        "intelligence": asdict(intel),
        "upstream": "[configured]",
    }
    if upstream is not None:
        content["upstream_status_code"] = upstream.status_code
    if upstream_error:
        content["reason"] = redact_text(upstream_error)
    elif intel.reason:
        content["reason"] = intel.reason
    return JSONResponse(status_code=status_code, content=content)


class GuidanceTaskRequest(BaseModel):
    schema_version: str
    task: dict[str, Any]


@app.post("/v1/guidance/execute")
async def execute_guidance(request: Request, payload: GuidanceTaskRequest) -> Response:
    """Evaluate one versioned, platform-neutral guidance request."""

    del request
    if guidance_plane_instance is None or not guidance_plane_instance.status.enabled:
        return _proxy_error(404, "guidance is disabled")
    if payload.schema_version != "1":
        return _proxy_error(400, "unsupported guidance schema_version")
    if guidance_plane_instance.status.state != "ready":
        return _proxy_error(
            503,
            "guidance is degraded",
            extra={"guidance_status": guidance_plane_instance.status.reason},
        )
    goal = payload.task.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        return _proxy_error(422, "task.goal must be a non-empty string")
    try:
        result = guidance_plane_instance.evaluate(payload.task)
    except GuidanceUnavailableError as exc:
        return _proxy_error(503, str(exc))
    return JSONResponse(content=result)


def _proxy_error(
    status_code: int,
    message: str,
    *,
    extra: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    surface: str | None = None,
) -> JSONResponse:
    error_type = "invalid_request_error"
    if status_code in {401, 403}:
        error_type = "authentication_error"
    elif status_code == 413:
        error_type = "request_too_large"
    elif status_code >= 500:
        error_type = "api_error"
    error: dict[str, Any] = {"message": message, "type": error_type}
    payload: dict[str, Any] = (
        {"type": "error", "error": error} if surface == "messages" else {"error": error}
    )
    if extra:
        payload.update(extra)
    return JSONResponse(status_code=status_code, content=payload, headers=headers)


def _safe_decision_dict(decision: Any) -> dict[str, Any]:
    """Serialize legacy compatibility data without exposing diagnostic secrets."""

    data = asdict(decision)
    # The compiled pack is workspace content bound for the upstream model, not
    # a client-facing decision field (cheap-path pack). Its digest lives in the receipt.
    data.pop("context_pack_prompt", None)
    data.pop("admission_proofs", None)
    # Opt-in diagnostic; only route_task projects it, on request.
    data.pop("routing_trace", None)
    return cast(dict[str, Any], redact_contract_secrets(data))


def _inject_decision_pack(
    payload: dict[str, Any], decision: RoutingDecision, *, surface: str
) -> tuple[dict[str, Any], InjectionRecord]:
    """Inject the decision's compiled cheap-path pack into the forwarded payload."""
    receipt = decision.admit_receipt if isinstance(decision.admit_receipt, dict) else {}
    if "cheap_path_context_pack" not in decision.safety_flags:
        return payload, InjectionRecord(
            injected=False,
            pack_state=None,
            pack_digest=None,
            prompt_digest=None,
            envelope_digest=None,
            reason="not_cheap_path",
        )
    pack_state = receipt.get("pack_state")
    pack_digest = receipt.get("pack_digest")
    prompt_digest = receipt.get("prompt_digest")
    task_complete = receipt.get("task_complete")
    return inject_context_pack(
        payload,
        surface=surface,
        compiled_prompt=decision.context_pack_prompt,
        pack_state=pack_state if isinstance(pack_state, str) else None,
        pack_digest=pack_digest if isinstance(pack_digest, str) else None,
        prompt_digest=prompt_digest if isinstance(prompt_digest, str) else None,
        task_complete=task_complete is not False,
    )


def _task_text(payload: dict[str, Any]) -> str:
    messages = payload.get("messages", payload.get("input", []))
    if isinstance(messages, str):
        return messages
    if not isinstance(messages, list):
        return ""
    parts: list[str] = []
    for message in messages:
        if isinstance(message, str):
            parts.append(message)
            continue
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                parts.extend(
                    item["text"]
                    for item in content
                    if isinstance(item, dict) and isinstance(item.get("text"), str)
                )
            elif isinstance(message.get("text"), str):
                parts.append(message["text"])
    return "\n".join(parts)


def _inband_refusal_body(surface: str) -> bytes:
    """The error body Verdict returns in place of an in-band credit refusal."""
    error = {
        "message": "upstream answered HTTP 200 with a credit refusal and no output",
        "type": "invalid_request_error",
        "code": "payment_required",
    }
    payload: dict[str, Any] = (
        {"type": "error", "error": error} if surface == "messages" else {"error": error}
    )
    return json.dumps(payload).encode("utf-8")


def _truncated_empty_body(surface: str) -> bytes:
    """The error body Verdict returns when the upstream produced no content.

    ``finish_reason: "length"`` with empty content means the model's output
    budget was exhausted before any reply text was written.  The request is
    retried on the next admitted candidate; this body is forwarded only when
    all candidates are exhausted.
    """
    error = {
        "message": (
            "upstream answered HTTP 200 with finish_reason 'length' and no content; "
            "all output tokens were consumed before any reply was written"
        ),
        "type": "invalid_request_error",
        "code": "truncated_empty_reply",
    }
    payload: dict[str, Any] = (
        {"type": "error", "error": error} if surface == "messages" else {"error": error}
    )
    return json.dumps(payload).encode("utf-8")


def _as_response(result: BufferedUpstreamResponse) -> Response:
    return Response(
        content=result.body, status_code=result.status_code, headers=dict(result.headers)
    )


def _headers_for_body(result: BufferedUpstreamResponse) -> dict[str, str]:
    headers = dict(result.headers)
    headers.pop("content-length", None)
    return headers


def _record_execution_outcome(
    *,
    request_id: str,
    model: str,
    attempt: int,
    surface: str,
    result: BufferedUpstreamResponse | StreamedUpstreamResponse,
) -> None:
    """Persist the gateway's post-execution receipt beside the decision log (outcome receipts).

    Streamed responses expose headers only; their body is not buffered here, so
    token counts fall back to headers and cost remains header-only either way.
    """
    log_path = getattr(intelligence_instance, "log_path", None)
    if not log_path:
        return
    body = result.body if isinstance(result, BufferedUpstreamResponse) else None
    log_outcome(
        log_path,
        build_outcome_record(
            request_id=request_id,
            model=model,
            status_code=result.status_code,
            attempt=attempt,
            surface=surface,
            headers=result.headers,
            body=body,
        ),
    )


# BOD-437: relay failure classes mapped onto the fixed BOD-353 attempt taxonomy.
_RELAY_LEDGER_CLASS: dict[str, str] = {
    "transport_timeout": "timeout",
    "transport_connection": "stream_drop",
    "auth_failure": "auth_error",
    "transport_upstream": "provider_5xx",
}
_STREAM_LEDGER: dict[StreamState, tuple[str, str]] = {
    StreamState.SUCCESS: ("accepted", "success"),
    StreamState.FAILED: ("failed", "other"),
    StreamState.INCOMPLETE: ("partial", "stream_drop"),
    StreamState.ABORTED: ("abandoned", "other"),
    StreamState.PROTOCOL_ERROR: ("failed", "stream_drop"),
    StreamState.ABRUPT_EOF: ("failed", "stream_drop"),
    StreamState.TRANSPORT_ERROR: ("failed", "stream_drop"),
}
_EFFECTIVE_PARAMETER_KEYS = (
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "max_output_tokens",
    "reasoning_effort",
    "reasoning",
)


def _ledger_failure_class(
    status_code: int | None = None, error: BaseException | None = None
) -> str:
    if isinstance(error, StreamProtocolError):
        return "stream_drop"
    if status_code == 429:
        return "quota_429"
    if status_code == 408:
        return "timeout"
    return _RELAY_LEDGER_CLASS.get(failure_class(status_code, error), "other")


def _stream_ledger_outcome(outcome: StreamOutcome | None) -> tuple[str, str]:
    """Map the stream's one terminal state; only a semantic success is accepted.

    A success here is transport and protocol completion, not checked quality:
    the row is a ``terminal`` record and never carries quality credit.
    """
    if outcome is None:
        return "failed", "stream_drop"
    if outcome.state is StreamState.TRANSPORT_ERROR and "Timeout" in (outcome.detail or ""):
        return "failed", "timeout"
    return _STREAM_LEDGER[outcome.state]


def _record_attempt_ledger(
    *,
    request_id: str,
    decision_id: str | None,
    attempt_id: str,
    model: str,
    surface: str,
    profile_digest: str | None,
    task_family: str | None,
    outcome: str,
    failure_class: str,
    forwarded: Mapping[str, Any],
    headers: Any = (),
    body: bytes | None = None,
) -> None:
    """Append one relay attempt's terminal row to the shared attempt ledger.

    Usage and cost come only from observed headers or body; missing values stay
    ``None``. A ledger failure is logged and never disturbs serving.
    """
    from verdict.outcome_log import observe_execution
    from verdict.outcome_records import build_attempt_record, write_attempt_record
    from verdict.paths import verdict_home

    try:
        observed = observe_execution(headers=headers, body=body)
        parameters = {k: forwarded[k] for k in _EFFECTIVE_PARAMETER_KEYS if k in forwarded}
        record = build_attempt_record(
            task_kind=surface,
            harness="verdict-relay",
            model=model,
            outcome=outcome,
            failure_class=failure_class,
            source="verdict.api",
            tokens_in=observed["observed_tokens_in"],
            tokens_out=observed["observed_tokens_out"],
            cost_usd=observed["observed_cost_usd"],
            request_id=request_id,
            decision_id=decision_id,
            attempt_id=attempt_id,
            task_family=task_family,
            profile_digest=profile_digest,
            record_kind="terminal",
            effective_parameters=parameters or None,
        )
        write_attempt_record(record, store_dir=verdict_home())
    except Exception:
        logger.warning("attempt ledger write failed for request %s", request_id, exc_info=True)


def _relay_response_headers(
    result: BufferedUpstreamResponse | StreamedUpstreamResponse,
) -> dict[str, str]:
    """Return safe upstream metadata without stale framing or forged Verdict headers."""

    headers = dict(result.headers)
    headers.pop("content-length", None)
    headers.pop("content-encoding", None)
    return {
        name: value for name, value in headers.items() if not name.lower().startswith("x-verdict-")
    }


def _evidence_scope(request: Request) -> str:
    """Bind evidence lookup to the authenticated deployment principal."""

    if os.getenv("LLMGATE_AUTH_TOKEN"):
        return "server-auth"
    # Anonymous mode intentionally has one explicit, non-authoritative scope;
    # a caller-controlled header must never create an authorization boundary.
    return "anonymous"


def _evidence_headers(evidence: ExplainEvidence) -> dict[str, str]:
    """Expose correlation metadata without putting evidence on legacy bodies."""

    headers = {
        "x-verdict-evidence-request-id": evidence.routing_decision.request_id or "",
        "x-verdict-correlation-id": evidence.routing_decision.correlation_id or "",
    }
    if evidence.evidence_id:
        headers["x-verdict-evidence-id"] = evidence.evidence_id
    return headers


def _router_dev_mode() -> bool:
    """Return the dev_mode the live router uses (``profile == "development"``)."""
    if intelligence_instance is not None:
        return intelligence_instance.profile == "development"
    return os.getenv("LLMGATE_INTELLIGENCE_PROFILE", DEFAULT_PROFILE) == "development"


@app.get("/v1/route/explain")
async def route_explain(
    request: Request,
    model_id: str | None = None,
    evidence_id: str | None = None,
    request_id: str | None = None,
    correlation_id: str | None = None,
) -> Response:
    """Explain availability freshness and eligibility for one or all cached models.

    Implements the issue #56 / #73 explain contract: surfaces observed_at,
    expires_at, age, source, confidence, candidate/eligible counts, per-candidate
    exclusion reasons (#73), and cache refresh/error state (``cache_state``,
    ``stale``, ``refreshing``, ``refresh_error``).

    Without ``model_id`` the response reports the cache scope (policy version and
    configured model keys) plus the gate's pre-ranking eligible/exclusion sets
    when available. With ``model_id`` it returns the per-model freshness explain
    record, refreshing on first access.
    """
    # Evidence lookup is independent of live availability. This lets an
    # operator inspect the immutable decision-time record even after a cache
    # expires or OmniRoute is temporarily unavailable.
    if sum(value is not None for value in (model_id, evidence_id, request_id, correlation_id)) > 1:
        return _proxy_error(400, "provide exactly one explain query selector")
    if evidence_id or request_id or correlation_id:
        try:
            evidence = (
                evidence_store_instance.find(
                    evidence_id=evidence_id,
                    request_id=request_id,
                    correlation_id=correlation_id,
                    scope=_evidence_scope(request),
                )
                if evidence_store_instance is not None
                else None
            )
        except AmbiguousEvidenceSelectorError:
            return _proxy_error(409, "evidence selector is ambiguous; use evidence_id")
        if evidence_store_instance is None:
            return _proxy_error(503, "execution evidence is unavailable")
        if evidence is None:
            return _proxy_error(404, "routing evidence not found")
        return JSONResponse(content=evidence.to_dict())

    if availability_cache_instance is None:
        return _proxy_error(
            503, "availability cache not configured (set OMNIROUTE_BASE_URL to enable)"
        )
    if model_id is None or model_id == "":
        base: dict[str, Any] = {
            "kind": "availability_explain",
            "policy_version": availability_cache_instance.policy_version,
            "cached_models": sorted(availability_cache_instance.keys()),
            "cache_state": "configured",
        }
        # Issue #73: surface the gate's complete pre-ranking eligible set and
        # exclusions from the same authority the router uses.
        if eligibility_gate_instance is not None:
            gate_eval = eligibility_gate_instance.evaluate(
                [
                    ModelInfo(
                        id=mid,
                        provider=mid.split("/", 1)[0] if "/" in mid else "unknown",
                        capability_tier=2,
                    )
                    for mid in base["cached_models"]
                ],
                dev_mode=_router_dev_mode(),
            )
            base["eligible_set"] = [m.id for m in gate_eval.eligible]
            base["exclusions"] = [r.to_dict() for r in gate_eval.exclusions]
        return JSONResponse(content=base)
    if model_id is None:
        return _proxy_error(400, "model_id must not be null")
    record = availability_cache_instance.explain(model_id)
    record["kind"] = "availability_explain"
    if eligibility_gate_instance is not None:
        gate_eval = eligibility_gate_instance.evaluate(
            [
                ModelInfo(
                    id=model_id,
                    provider=model_id.split("/", 1)[0] if "/" in model_id else "unknown",
                    capability_tier=2,
                )
            ],
            dev_mode=_router_dev_mode(),
        )
        if gate_eval.records:
            record["eligibility"] = gate_eval.records[0].to_dict()
            record["eligible"] = gate_eval.records[0].admitted
    return JSONResponse(content=record)


@app.get("/v1/models")
async def list_models() -> Response:
    """Publish the locally verified model set.

    Membership needs fresh positive basic-chat proof and a valid, complete and
    current inventory authority. The handler reads the lifespan snapshot only:
    no gateway read, provider call or qualification happens here.
    """
    if public_models_readiness is None:
        raise HTTPException(status_code=503, detail="Proxy not initialized")
    now = _public_models_clock()
    try:
        state = public_models_readiness.current(now)
    except PublicModelsUnavailableError as exc:
        return _proxy_error(503, f"verified model catalog unavailable: {exc.code}")
    allowlist, denylist = configured_catalog_filters(
        os.getenv("LLMGATE_MODEL_ALLOWLIST"), os.getenv("LLMGATE_MODEL_DENYLIST")
    )
    data = [
        public_model_entry(row)
        for row in state.rows_at(now)
        if (not allowlist or row.route_id in allowlist) and row.route_id not in denylist
    ]
    return JSONResponse(content={"object": "list", "data": data})


class QualifyRequest(BaseModel):
    """Body for ``POST /v1/models/qualify``."""

    provider: str
    model_id: str
    estimated_tokens: int | None = None
    require_tools: bool = False
    require_structured_output: bool = False


@app.post("/v1/models/qualify")
async def qualify_model(request: Request, req: QualifyRequest) -> Response:
    """Qualify one concrete provider model with a bounded probe cascade.

    Returns a fresh ``ModelPassport`` (isolation-key cached for the store TTL),
    or a fail-closed error when qualification is not configured.
    """
    if not req.provider.strip() or not req.model_id.strip():
        return _proxy_error(422, "provider and model_id must be non-empty")
    if "/" in req.provider or "/" in req.model_id:
        return _proxy_error(422, "provider and model_id must not contain '/'")
    if req.estimated_tokens is not None and req.estimated_tokens < 0:
        return _proxy_error(422, "estimated_tokens must be non-negative")
    if model_passport_store_instance is None:
        return _proxy_error(503, "model qualification is not configured")
    fresh = model_passport_store_instance.get(req.provider, req.model_id)
    if fresh is not None:
        return JSONResponse(content=fresh.to_dict())
    probe_base_url = os.getenv("LLMGATE_PROBE_BASE_URL")
    if not probe_base_url or probe_base_url.strip().lower() in {"", "none"}:
        return _proxy_error(503, "model qualification requires LLMGATE_PROBE_BASE_URL")
    try:
        from verdict.model_passports import run_qualification
        from verdict.probes import openai_probe_transport

        api_key = os.getenv("LLMGATE_PROBE_API_KEY") or os.getenv("LLMGATE_UPSTREAM_API_KEY")
        transport = openai_probe_transport(probe_base_url, api_key=api_key)
        live = os.getenv("LLMGATE_ALLOW_LIVE_PROBES", "").lower() in {"1", "true", "yes"}
        consented = os.getenv("LLMGATE_PROBE_CONSENTED", "").lower() in {"1", "true", "yes"}
        passport = await asyncio.to_thread(
            run_qualification,
            provider=req.provider,
            model_id=req.model_id,
            transport=transport,
            live=live,
            consented=consented,
            require_tools=req.require_tools,
            require_structured_output=req.require_structured_output,
            estimated_tokens=req.estimated_tokens,
        )
    except Exception:
        return _proxy_error(502, "model qualification failed")
    model_passport_store_instance.put(passport)
    return JSONResponse(content=passport.to_dict())


@app.get("/v1/models/passports")
async def list_passports(request: Request, model_ids: str | None = None) -> Response:
    """List fresh qualified model passports, optionally filtered by ids."""
    if model_passport_store_instance is None:
        return _proxy_error(503, "model qualification is not configured")
    if model_ids:
        wanted = [item.strip() for item in model_ids.split(",") if item.strip()]
        if not wanted:
            return _proxy_error(422, "model_ids must contain at least one id")
        found = []
        for item in wanted:
            if "/" not in item:
                return _proxy_error(422, f"model_id '{item}' must be provider/model")
            provider, model_id = item.split("/", 1)
            passport = model_passport_store_instance.get(provider, model_id)
            if passport is not None:
                found.append(passport.to_dict())
        return JSONResponse(content={"passports": found})
    passports = [p.to_dict() for p in model_passport_store_instance.list_fresh()]
    return JSONResponse(content={"passports": passports})


def _dispatch_attempt_id() -> str:
    return uuid.uuid4().hex


_MODEL_NOT_FOUND_CODES = frozenset({"model_not_found", "model_not_available"})


def _gateway_model_not_found(body: str) -> bool:
    """True only for the gateway's structured model-not-found error code."""
    try:
        parsed = json.loads(body)
    except ValueError:
        return False
    error = parsed.get("error") if isinstance(parsed, dict) else None
    return isinstance(error, dict) and error.get("code") in _MODEL_NOT_FOUND_CODES


def _verified_assignment(request: Request) -> worker_assignment.VerifiedWorkerAssignment | None:
    """The assignment the middleware verified for this request, or ``None``.

    Read only from ``request.state``, which no header or body can write, and only when it has
    the verified type, which only the verifier can build.
    """
    value = getattr(getattr(request, "state", None), "worker_assignment", None)
    return value if isinstance(value, worker_assignment.VerifiedWorkerAssignment) else None


def _worker_marked(request: Request) -> bool:
    """True for a request that presents itself as a worker's: the header or an assignment bearer."""
    if request.headers.get("x-verdict-worker") is not None:
        return True
    scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
    return scheme.lower() == "bearer" and supplied.startswith(_ASSIGNMENT_PREFIX)


async def _relay_completion(request: Request, *, surface: str) -> Response:
    """Route and transparently forward one OpenAI protocol surface."""
    if intelligence_instance is None or proxy_instance is None:
        raise HTTPException(status_code=503, detail="Proxy not initialized")
    assignment = _verified_assignment(request)
    if assignment is None and _worker_marked(request):
        # The bare worker header, or an assignment bearer that was not verified, selects nothing:
        # no ladder, no proof, no upstream.  The middleware already refuses an unverified
        # assignment bearer; this is the second line, and the only guard in anonymous mode.
        return _proxy_error(403, "authentication failed", surface=surface)

    max_bytes = int(os.getenv("LLMGATE_MAX_REQUEST_BYTES", str(DEFAULT_MAX_REQUEST_BYTES)))
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes:
                return _proxy_error(
                    413, "request body exceeds configured size limit", surface=surface
                )
        except ValueError:
            return _proxy_error(400, "content-length must be an integer", surface=surface)
    body_parts: list[bytes] = []
    body_size = 0
    async for chunk in request.stream():
        body_size += len(chunk)
        if body_size > max_bytes:
            return _proxy_error(413, "request body exceeds configured size limit", surface=surface)
        body_parts.append(chunk)
    body = b"".join(body_parts)
    if len(body) > max_bytes:
        return _proxy_error(413, "request body exceeds configured size limit", surface=surface)
    try:
        payload = json.loads(body)
    except (TypeError, ValueError, json.JSONDecodeError):
        return _proxy_error(400, "request body must be valid JSON", surface=surface)
    if not isinstance(payload, dict):
        return _proxy_error(400, "request body must be a JSON object", surface=surface)
    if assignment is not None and "execution_path_request" in payload:
        # A worker is pinned to the route it was assigned.  The execution-path flow selects its
        # own offer and would bypass that pin, so a worker request may not carry one: this
        # rejection, before any routing, is the contract (operator decision, 2026-11-24);
        # pinned execution-path support for workers is not built.
        return _proxy_error(
            400,
            "worker requests must not carry execution_path_request: the assigned route is pinned",
            surface=surface,
        )
    if assignment is not None and payload.get("model") != assignment.route_id:
        # The assignment names the route; the request's ``model`` must say the same, exactly.
        # A different, missing or malformed model is refused, never ignored and never rewritten,
        # before the ladder runs: the request contradicts its own credential.  Only the signed
        # ids are logged (the token was verified, so they can be trusted), never the token.
        logger.warning(
            "worker assignment refused: reason=model_mismatch route_id=%s run_id=%s node_id=%s",
            assignment.route_id,
            assignment.run_id,
            assignment.node_id,
        )
        return _proxy_error(
            400,
            "the request's model must equal the route in the worker assignment",
            surface=surface,
        )

    task = _task_text(payload)
    protocol = protocol_for_surface(surface)
    event_prefix = (
        "chat_completion"
        if surface == "chat"
        else "responses"
        if surface == "responses"
        else "messages"
    )
    features = request_features(payload)
    correlation_id = request.headers.get("x-verdict-correlation-id")
    if not correlation_id and isinstance(payload.get("correlation_id"), str):
        correlation_id = cast(str, payload["correlation_id"])
    # Resolve the request id *before* routing so the pre-execution decision row
    # and the post-execution outcome receipt share one key.
    client_request_id = request.headers.get("x-verdict-request-id") or (
        payload.get("request_id") if isinstance(payload.get("request_id"), str) else None
    )
    request_id = safe_identifier(client_request_id, prefix="req")
    try:
        authority_context = _authority_context(payload, task=task)
        authority_context["_dispatch_gateway_origin"] = proxy_instance.base_url
        authority_context["_relay_protocol"] = protocol
        if assignment is not None:
            # Typed, server-set input (ADR-053 decision 1): the verified assignment.  Admission
            # selects its route within the admitted set and mints the route-bound proof for it.
            # A request without an assignment never carries it, and a client cannot forge it
            # (underscore keys are dropped from the public payload and JSON cannot build the
            # typed value, which only the verifier constructs).
            authority_context[CONTEXT_WORKER_ASSIGNMENT] = assignment
        decision = await intelligence_instance.route(
            task,
            criticality=payload.get("criticality", "medium"),
            context=authority_context,
            request_id=request_id,
        )
    except Exception as exc:
        from verdict.execution_path import ExecutionPathError

        if isinstance(exc, ExecutionPathError):
            return _proxy_error(400, str(exc), surface=surface)
        raise
    if client_request_id is None and decision.request_id:
        # An intelligence that mints its own ids keeps them; precedence stays
        # client id → decision id → generated.
        request_id = decision.request_id
    criticality = payload.get("criticality", "medium")
    if not isinstance(criticality, str):
        criticality = "unknown"
    request_identity = (
        payload.get("model") if isinstance(payload.get("model"), str) else "unspecified"
    )
    request_key = idempotency_key(request, payload)
    safety = retry_safety(request, payload, request_key)
    relay_admitted: AdmittedSet | None = None
    if relay_admission_provider is not None:
        try:
            relay_admitted = relay_admission_provider()
        except Exception as exc:
            return _proxy_error(
                503, f"live admission unavailable: {type(exc).__name__}", surface=surface
            )
    # --- Per-request route pinning for worker calls (ADR-053 Decision 1, as amended) ---
    # A worker's route is the one in its verified assignment, and its ``model`` was checked equal
    # to that route above.  The relay passes the assignment to admission as a typed input;
    # admission selects that route within the set it already admitted and mints its route-bound
    # proof, or denies.  Here the relay only *verifies* that outcome: it never rewrites a
    # worker's decision, so a route can never be dispatched without the proof admission minted
    # for it.  Requests without an assignment are untouched: their ``model`` field does not select
    # the route (``LLMGATE_ALLOW_CLIENT_MODEL_OVERRIDE`` is only stored on the service, as before;
    # the relay never honoured it).
    # Any denial sets ``decision="denied"`` so the existing evidence-recording path handles it.
    if assignment is not None and decision.decision != "denied":
        if decision.model != assignment.route_id:
            # Admission did not select the assigned route.  Dispatching its own pick would run a
            # route the worker was not assigned, and rewriting the model here would bypass the
            # route-bound proof.
            _admitted_ids = {
                str(r.get("model_id"))
                for r in decision.candidate_states
                if isinstance(r, dict) and r.get("admitted") is True
            }
            decision = replace(
                decision,
                decision="denied",
                reason=(
                    f"route {assignment.route_id!r} was not selected by admission for this request"
                    if assignment.route_id in _admitted_ids
                    else f"route {assignment.route_id!r} was not in the admitted set for this request"
                ),
            )
        else:
            # Accepted: bound the request to exactly the assigned route.  No alternatives, so a
            # worker request never falls back to a route it was not assigned.
            decision = replace(decision, alternatives=[])
    attempts = build_attempts(proxy_instance, decision, protocol=protocol, admitted=relay_admitted)
    if not attempts:
        if "free_supply_unknown" in decision.safety_flags:
            return _proxy_error(
                503, "free supply unknown; higher-tier fallback denied", surface=surface
            )
        return _proxy_error(503, "no executable route was selected", surface=surface)
    attempted_routes = [item.route.to_dict() for item in attempts]
    route_evidence, evidence_key = await _start_evidence_on_lane(
        decision,
        cancelled_event_prefix=event_prefix,
        task=task,
        criticality=criticality,
        features=features,
        request_id=request_id,
        correlation_id=correlation_id,
        scope=_evidence_scope(request),
        requested_identity=request_identity,
        resolved_route=attempts[0].route.to_dict(),
        attempted_routes=attempted_routes,
    )
    decision = replace(
        decision, request_id=route_evidence.routing_decision.request_id or decision.request_id
    )
    if decision.decision == "denied":
        outcome = build_outcome_event(
            route_evidence.routing_decision,
            event_type=f"{event_prefix}_denied",
            outcome="denied",
            features=features,
        )
        evidence = await _on_state_lane(_finish_evidence, route_evidence, outcome, evidence_key)
        return _proxy_error(
            503,
            decision.reason,
            extra={"decision": _safe_decision_dict(decision)},
            headers=_evidence_headers(evidence),
            surface=surface,
        )

    if decision.protected and is_opaque_alias(attempts[0].model):
        evidence = await _on_state_lane(
            _finish_evidence,
            route_evidence,
            build_outcome_event(
                route_evidence.routing_decision,
                event_type=f"{event_prefix}_denied",
                outcome="denied",
                features=features,
                details={"failure_class": "opaque_route_unattested"},
            ),
            evidence_key,
        )
        return _proxy_error(
            503,
            "protected execution requires an attested actual route",
            headers=_evidence_headers(evidence),
            surface=surface,
        )
    started_at = monotonic()
    result: BufferedUpstreamResponse | StreamedUpstreamResponse | None = None
    attempts_used: list[dict[str, Any]] = []
    validated_stream: ValidatedSSEStream | None = None
    last_error: BaseException | None = None
    last_status: int | None = None
    _last_error_body: str = ""  # bounded upstream body for failure classification

    async def on_lane(fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
        """``_on_state_lane`` for the steps after the decision row exists and before an attempt's
        own cancellation handler: a task cancelled while it waits still ends its row."""
        try:
            return await _on_state_lane(fn, *args, **kwargs)
        except asyncio.CancelledError:
            _queue_cancelled_terminal(
                route_evidence, evidence_key, event_prefix=event_prefix, features=features
            )
            raise

    async def record_attempt_event(
        *, attempt: Any, event_type: str, details: dict[str, Any]
    ) -> None:
        nonlocal route_evidence
        route_evidence = await on_lane(
            _finish_evidence,
            route_evidence,
            build_outcome_event(
                route_evidence.routing_decision,
                event_type=event_type,
                outcome="unknown",
                features=features,
                details={
                    "attempt": len(attempts_used) - 1,
                    "model": attempt.model,
                    "route": attempt.route.to_dict(),
                    "policy_version": decision.policy_version,
                    **details,
                },
            ),
            evidence_key,
        )

    # Proofs are bound to the task profile of the request actually being sent,
    # recomputed with the same helper preparation used to mint them.
    relay_profile_digest: str | None
    relay_task_family: str | None = None
    try:
        # The lane and the relay compute one profile with one helper, in the same mode: the
        # proof the lane minted scopes the digest recomputed here.
        relay_profile = prepared_task_profile(
            task,
            authority_context,
            planner=getattr(intelligence_instance, "planner", None),
            assigned=assignment is not None,
        )[1]
        relay_profile_digest = relay_profile.digest
        relay_task_family = relay_profile.task_family
    except TaskProfileError:
        relay_profile_digest = None
    stream_ledger: dict[str, Any] = {}

    def ledger(attempt_id: str, model: str, sent: Mapping[str, Any], **row: Any) -> None:
        _record_attempt_ledger(
            request_id=decision.request_id,
            decision_id=route_evidence.routing_decision.decision_id,
            attempt_id=attempt_id,
            model=model,
            surface=surface,
            profile_digest=relay_profile_digest,
            task_family=relay_task_family,
            forwarded=sent,
            **row,
        )

    # Real-request availability failures narrow the shared health cache the same
    # way orchestrate session feedback does. A failed cache write never breaks
    # the response path; it is reported on the outcome evidence event instead.
    health_cache_feedback: list[dict[str, Any]] = []

    def record_failure_feedback(
        *,
        route_id: str,
        attempt_id: str,
        status_code: int | None = None,
        error: BaseException | None = None,
        body: str = "",
    ) -> None:
        try:
            feedback = relay_failure_feedback(
                HealthCache(default_cache_path()),
                route_id=route_id,
                status_code=status_code,
                error=error,
                body=body,
                now=datetime.now(timezone.utc),
                provenance=f"{decision.request_id}:{attempt_id}",
            )
        except Exception as exc:
            health_cache_feedback.append(
                {
                    "route_id": route_id,
                    "recorded": False,
                    "reason": f"persistence_error:{type(exc).__name__}",
                }
            )
            return
        health_cache_feedback.append(
            {"route_id": route_id, "recorded": feedback.recorded, "reason": feedback.reason}
        )

    selected_route_id = next(
        (
            flag.split(":", 1)[1]
            for flag in decision.safety_flags
            if flag.startswith("bod104_route_id:")
        ),
        None,
    )
    dispatch_denial: str | None = None
    previous_refused = False  # last sent attempt was an in-band refusal with no output
    for index, attempt in enumerate(attempts):
        if index:
            edge = transition_edge(
                attempts,
                index - 1,
                request_id=route_evidence.routing_decision.request_id or decision.request_id,
                key=request_key,
                safety=safety,
                protocol=protocol,
                protected=decision.protected,
                rejected_without_output=previous_refused,
            )
            if edge is None or not edge.legal:
                break
        else:
            edge = None
        capacity = next(
            (
                row.get("capacity_order")
                for row in decision.candidate_states
                if row.get("model_id") == attempt.model
            ),
            None,
        )
        forwarded = dict(payload)
        # Verdict-local controls must never be forwarded to an upstream provider.
        for local_field in _RELAY_LOCAL_FIELDS:
            forwarded.pop(local_field, None)
        forwarded["model"] = attempt.model
        # cheap-path pack: the hydrated pack the receipt describes is what the upstream
        # receives. Injection is recorded per attempt and never claimed when
        # the pack is empty/failed or the task did not survive compilation.
        forwarded, injection = _inject_decision_pack(forwarded, decision, surface=surface)
        # Exactly one proof may name this attempt's model; the primary must also
        # carry the provider route() selected. Ambiguity is a denial, not a choice.
        matches = [
            proof
            for route_id, proof in decision.admission_proofs.items()
            if proof.model == attempt.model and proof.route_id == route_id
        ]
        proof = matches[0] if len(matches) == 1 else None
        if proof is None or (
            attempt.model == decision.model
            and (
                proof.provider != decision.provider
                or (selected_route_id is not None and proof.route_id != selected_route_id)
            )
        ):
            dispatch_denial = "dispatch denied: missing route-bound admission proof"
            if attempts_used:
                break
            continue
        if relay_profile_digest is None:
            dispatch_denial = "dispatch denied: task profile unavailable at relay"
            if attempts_used:
                break
            continue
        expected_scope = dispatch_scope(
            gateway_origin=proxy_instance.base_url,
            provider=proof.provider,
            model=attempt.model,
            route_id=proof.route_id,
            profile_digest=relay_profile_digest,
            protocol=protocol if surface == "messages" else None,
        )
        now = datetime.now(timezone.utc)
        attempt_id = _dispatch_attempt_id()
        if proof.scope != expected_scope or proof.qualified_at > now:
            dispatch_denial = "dispatch denied: proof scope or observation mismatch"
            ledger(attempt_id, attempt.model, forwarded, outcome="abandoned", failure_class="other")
            if attempts_used:
                break
            continue
        permit = await on_lane(
            _commit_dispatch_permit,
            expected_scope,
            route_id=proof.route_id,
            attempt_id=attempt_id,
            authorization_digest=proof.authorization_digest,
            expected_epoch=proof.epoch,
            qualified_at=proof.qualified_at,
            proof_deadline=min(
                proof.expires_at,
                proof.qualified_at + timedelta(seconds=PERMIT_PROOF_MAX_VALIDITY_SECONDS),
            ),
            now=now,
        )
        if permit is None:
            dispatch_denial = "dispatch denied: permit commit rejected proof"
            ledger(attempt_id, attempt.model, forwarded, outcome="abandoned", failure_class="other")
            if attempts_used:
                break
            continue
        if "subscription_fallback_policy" in decision.safety_flags and capacity == 1:
            free_routes = [
                str(row["route_id"])
                for row in decision.candidate_states
                if row.get("capacity_order") == 0 and isinstance(row.get("route_id"), str)
            ]
            try:
                HealthCache(default_cache_path()).request_rescreen(
                    free_routes, now=datetime.now(timezone.utc)
                )
                rescreen = "queued"
            except Exception as exc:
                rescreen = f"persistence_error:{type(exc).__name__}"
            await record_attempt_event(
                attempt=attempt,
                event_type=f"{event_prefix}_subscription_fallback",
                details={
                    "attempt": index,
                    "fallback": "subscription",
                    "reason": "free attempts failed" if index else "no free route qualifies",
                    "rescreen": rescreen,
                    "free_routes": free_routes,
                },
            )
        if "subscription_fallback_policy" in decision.safety_flags and capacity == 2:
            await record_attempt_event(
                attempt=attempt,
                event_type=f"{event_prefix}_metered_fallback",
                details={
                    "attempt": index,
                    "fallback": "metered",
                    "reason": "free and capable subscription attempts unavailable or failed",
                },
            )
        try:
            if surface == "responses":
                result = await proxy_instance.responses(forwarded, idempotency_key=request_key)
            elif surface == "messages":
                result = await proxy_instance.messages(forwarded, idempotency_key=request_key)
            else:
                result = await proxy_instance.chat(forwarded, idempotency_key=request_key)
            validated_stream = None
            if isinstance(result, StreamedUpstreamResponse):
                validated_stream = ValidatedSSEStream(result.body, surface=surface)
                result = replace(result, body=await prime_stream(validated_stream))
            if HealthCache(default_cache_path()).revocation_epoch(proof.scope) != permit.epoch:
                if isinstance(result, StreamedUpstreamResponse):
                    assert validated_stream is not None
                    await validated_stream.aclose()
                raise RuntimeError("dispatch permit revoked after commit; possibly sent")
            inband_refusal = False
            if (
                isinstance(result, BufferedUpstreamResponse)
                and result.status_code == 200
                and is_inband_billing_refusal(result.body)
            ):
                # The provider answered 200 but the body is a credit refusal with
                # no completion tokens. Project it as a 402 so it is a failed
                # attempt everywhere below; the provider text is not forwarded.
                inband_refusal = True
                result = replace(result, status_code=402, body=_inband_refusal_body(surface))
            truncated_empty = False
            if (
                not inband_refusal
                and isinstance(result, BufferedUpstreamResponse)
                and result.status_code == 200
                and is_truncated_empty_reply(result.body)
            ):
                # The provider answered 200 with finish_reason "length" but
                # produced no content — the output budget was consumed entirely
                # by reasoning overhead.  Project it as 422 so it is recorded
                # as a failed attempt and the loop continues to the next
                # admitted candidate.  422 falls outside the feedback-write
                # gate (402 or >=500), so no route health negative is written;
                # this is a prompt-budget event, not a route availability event.
                truncated_empty = True
                result = replace(result, status_code=422, body=_truncated_empty_body(surface))
            previous_refused = inband_refusal or truncated_empty
            last_status = result.status_code
            # Extract bounded body text for failure classification (context-length
            # detection).  Error responses are always BufferedUpstreamResponse, so
            # body is bytes.  Read at most 8 KB; never log or persist the raw body.
            _error_body = ""
            if result.status_code >= 400 and isinstance(result, BufferedUpstreamResponse):
                _error_body = result.body[:8192].decode("utf-8", errors="replace")
            _last_error_body = _error_body
            if result.status_code in {401, 403} or (
                result.status_code == 404 and _gateway_model_not_found(_error_body)
            ):
                HealthCache(default_cache_path()).record_revocation(
                    proof.scope,
                    route_id=proof.route_id,
                    event_id=f"{request_id}:{permit.permit_id}:denied",
                    source_revision=None,
                    observed_at=datetime.now(timezone.utc),
                )
            if result.status_code == 402 or result.status_code >= 500:
                # A general upstream 5xx is route-scoped availability evidence;
                # it must also narrow the shared health cache, not just the ledger.
                record_failure_feedback(
                    route_id=proof.route_id,
                    attempt_id=attempt_id,
                    status_code=result.status_code,
                    body=_error_body,
                )
            # outcome receipts: the only place execution cost is observable is *after* the
            # upstream answers. Persist it per attempt, keyed to the decision.
            _record_execution_outcome(
                request_id=decision.request_id,
                model=attempt.model,
                attempt=index,
                surface=surface,
                result=result,
            )
            served_model = result.actual_route.model_id if result.actual_route is not None else None
            identity_status = identity_proof_status(
                attempt.model, served_model, attested=result.actual_route is not None
            )
            if identity_status == "identity_mismatch":
                ledger(
                    attempt_id, attempt.model, forwarded, outcome="failed", failure_class="other"
                )
            elif isinstance(result, BufferedUpstreamResponse):
                ok = result.status_code < 400
                ledger(
                    attempt_id,
                    attempt.model,
                    forwarded,
                    outcome="accepted" if ok else "failed",
                    failure_class="success" if ok else _ledger_failure_class(result.status_code),
                    headers=result.headers,
                    body=result.body,
                )
            else:
                stream_ledger.update(
                    attempt_id=attempt_id,
                    model=attempt.model,
                    sent=forwarded,
                    headers=result.headers,
                )
            if identity_status == "identity_mismatch":
                HealthCache(default_cache_path()).record_revocation(
                    proof.scope,
                    route_id=proof.route_id,
                    event_id=f"{request_id}:{permit.permit_id}:identity_mismatch",
                    source_revision=None,
                    observed_at=datetime.now(timezone.utc),
                )
                attempts_used.append(
                    {
                        "model": attempt.model,
                        "served_model": served_model,
                        "route_key": attempt.route.key,
                        "outcome": "error",
                        "failure_class": "identity_mismatch",
                        "transition_legal": True if index == 0 else bool(edge and edge.legal),
                        "compatibility_rule_version": result.compatibility_rule_version,
                        "context_pack": injection.to_dict(),
                    }
                )
                await record_attempt_event(
                    attempt=attempt,
                    event_type=f"{event_prefix}_attempt_failed",
                    details={
                        "status_code": result.status_code,
                        "failure_class": "identity_mismatch",
                        "selected_model": attempt.model,
                        "served_model": served_model,
                    },
                )
                if validated_stream is not None:
                    await validated_stream.aclose()
                result = None
                last_error = RuntimeError(
                    f"identity mismatch: selected {attempt.model!r}, served {served_model!r}"
                )
                break
            _attempt_failure_class = (
                "truncated_empty_reply"
                if truncated_empty
                else (
                    None
                    if result.status_code < 400
                    else failure_class(result.status_code, body=_error_body)
                )
            )
            _attempt_outcome = (
                "error" if truncated_empty else ("success" if result.status_code < 400 else "error")
            )
            attempts_used.append(
                {
                    "model": attempt.model,
                    "route_key": attempt.route.key,
                    "outcome": _attempt_outcome,
                    "failure_class": _attempt_failure_class,
                    "transition_legal": True if index == 0 else bool(edge and edge.legal),
                    "compatibility_rule_version": result.compatibility_rule_version,
                    "context_pack": injection.to_dict(),
                }
            )
            await record_attempt_event(
                attempt=attempt,
                event_type=(
                    f"{event_prefix}_attempt_completed"
                    if _attempt_outcome == "success"
                    else f"{event_prefix}_attempt_failed"
                ),
                details={
                    "status_code": result.status_code,
                    "failure_class": _attempt_failure_class,
                    "transition_edge": edge.to_dict() if edge is not None else None,
                    "compatibility_rule_version": result.compatibility_rule_version,
                    "context_pack": injection.to_dict(),
                },
            )
            # An in-band refusal or truncated empty reply produced no output,
            # so another candidate is safe to try.
            if result.status_code < 400 or not (
                inband_refusal
                or truncated_empty
                or retryable_response_status(
                    result.status_code,
                    compatibility_applied=result.compatibility_rule_version is not None,
                    body=_error_body,
                )
            ):
                break
            # A later denial fences this request even when its rejection or
            # idempotency key would otherwise make an alternative safe.
            if HealthCache(default_cache_path()).revocation_epoch(proof.scope) != permit.epoch:
                break
            last_error = None
        except asyncio.CancelledError:
            ledger(attempt_id, attempt.model, forwarded, outcome="abandoned", failure_class="other")
            outcome = build_outcome_event(
                route_evidence.routing_decision,
                event_type=f"{event_prefix}_cancelled",
                outcome="cancelled",
                features=features,
                abort_observed=True,
                latency_ms=(monotonic() - started_at) * 1000,
                details={"attempted_routes": attempts_used},
            )
            # The job is queued before the wait, and the lane shields it from this cancellation:
            # the terminal event lands even though this task is going away.
            await _on_state_lane(_finish_evidence, route_evidence, outcome, evidence_key)
            raise
        except Exception as exc:
            result = None
            last_error = exc
            ledger(
                attempt_id,
                attempt.model,
                forwarded,
                outcome="failed",
                failure_class=_ledger_failure_class(error=exc),
            )
            attempts_used.append(
                {
                    "model": attempt.model,
                    "route_key": attempt.route.key,
                    "outcome": "error",
                    "failure_class": failure_class(error=exc),
                    "transition_legal": True if index == 0 else bool(edge and edge.legal),
                }
            )
            await record_attempt_event(
                attempt=attempt,
                event_type=f"{event_prefix}_attempt_failed",
                details={
                    "failure_class": failure_class(error=exc),
                    "transition_edge": edge.to_dict() if edge is not None else None,
                },
            )
            record_failure_feedback(route_id=proof.route_id, attempt_id=attempt_id, error=exc)
            # Once committed, a transport failure may have executed upstream.
            break

    if result is None:
        if dispatch_denial is not None and not attempts_used:
            return _proxy_error(503, dispatch_denial, surface=surface)
        evidence = await _on_state_lane(
            _finish_evidence,
            route_evidence,
            build_outcome_event(
                route_evidence.routing_decision,
                event_type=f"{event_prefix}_error",
                outcome="error",
                features=features,
                error_class=failure_class(last_status, last_error, body=_last_error_body),
                retries=max(0, len(attempts_used) - 1),
                fallbacks=attempts_used[1:],
                latency_ms=(monotonic() - started_at) * 1000,
                details={
                    "attempted_routes": attempts_used,
                    **(
                        {"health_cache_feedback": health_cache_feedback}
                        if health_cache_feedback
                        else {}
                    ),
                },
            ),
            evidence_key,
        )
        return _proxy_error(
            502,
            "upstream request failed",
            extra={
                "decision": _safe_decision_dict(
                    replace(decision, transport_outcome="upstream_error")
                )
            },
            headers=_evidence_headers(evidence),
            surface=surface,
        )

    budget_exhausted = (
        "subscription_fallback_policy" in decision.safety_flags
        and len(attempts_used) == 3
        and result.status_code >= 400
        and retryable_response_status(
            result.status_code,
            compatibility_applied=result.compatibility_rule_version is not None,
            body=_last_error_body,
        )
    )
    transport_outcome = "success" if result.status_code < 400 else "upstream_error"
    decision_record = replace(decision, transport_outcome=transport_outcome)
    response_outcome = "success" if result.status_code < 400 else "error"
    response_headers = _relay_response_headers(result)
    response_headers["x-verdict-model"] = attempts_used[-1]["model"]
    response_headers["x-verdict-tier"] = str(decision_record.tier)
    response_headers["x-verdict-request-id"] = decision_record.request_id
    response_headers["x-verdict-decision"] = decision_record.decision
    response_headers["x-verdict-transport-outcome"] = decision_record.transport_outcome
    response_headers["x-verdict-quality-outcome"] = decision_record.quality_outcome
    response_headers["x-verdict-degraded-mode"] = str(decision_record.degraded_mode).lower()
    last_injection = attempts_used[-1].get("context_pack") if attempts_used else None
    if isinstance(last_injection, dict):
        response_headers["x-verdict-pack-injected"] = str(
            bool(last_injection.get("injected"))
        ).lower()
        if last_injection.get("pack_state"):
            response_headers["x-verdict-pack-state"] = str(last_injection["pack_state"])
        if last_injection.get("injected") and last_injection.get("envelope_digest"):
            response_headers["x-verdict-pack-digest"] = str(last_injection["envelope_digest"])
    response_headers.update(_evidence_headers(route_evidence))

    if isinstance(result, BufferedUpstreamResponse):
        from verdict.orchestration.eligibility import selection_accounting

        observed_model = (
            attempts_used[-1]["model"]
            if result.status_code < 400 and response_actual_route_status(result) != "mismatch"
            else None
        )
        evidence = await _on_state_lane(
            _finish_with_observation,
            route_evidence,
            build_outcome_event(
                route_evidence.routing_decision,
                event_type=f"{event_prefix}_buffered",
                outcome=response_outcome,
                status_code=result.status_code,
                features=features,
                latency_ms=(monotonic() - started_at) * 1000,
                retries=max(0, len(attempts_used) - 1),
                fallbacks=attempts_used[1:],
                details={
                    "attempted_routes": attempts_used,
                    **(
                        {"termination_reason": "budget_exhausted"}
                        if budget_exhausted
                        else {"termination_reason": "free_supply_unknown"}
                        if result.status_code >= 400
                        and "free_supply_unknown" in decision.safety_flags
                        else {}
                    ),
                    "selection_accounting": selection_accounting(
                        decision.candidate_states,
                        attempted=[row["model"] for row in attempts_used],
                        authority_expired=budget_exhausted,
                    ),
                    "compatibility_rule_version": result.compatibility_rule_version,
                    # Recorded, not enforced: names only, never header values.
                    "omniroute_anomalies": omniroute_anomaly_names(result.headers),
                    **(
                        {"health_cache_feedback": health_cache_feedback}
                        if health_cache_feedback
                        else {}
                    ),
                },
            ),
            evidence_key,
            observed_model,
            observe_first=True,
        )
        if result.status_code >= 400:
            return Response(
                content=result.body, status_code=result.status_code, headers=response_headers
            )
        return Response(
            content=result.body, status_code=result.status_code, headers=response_headers
        )
    if isinstance(result, StreamedUpstreamResponse):
        assert validated_stream is not None

        async def finalize_stream(event: Any) -> None:
            label, cls = _stream_ledger_outcome(validated_stream.outcome)
            usage = validated_stream.outcome.usage if validated_stream.outcome else None

            def write_ledger() -> None:
                ledger(
                    stream_ledger["attempt_id"],
                    stream_ledger["model"],
                    stream_ledger["sent"],
                    outcome=label,
                    failure_class=cls,
                    headers=stream_ledger["headers"],
                    body=json.dumps({"usage": usage}).encode() if usage else None,
                )

            try:
                # One lane job, so a cancelled closer cannot land the terminal event and lose
                # the observation (or the reverse).
                await _on_state_lane(
                    _finish_with_observation,
                    route_evidence,
                    event,
                    evidence_key,
                    stream_ledger["model"] if label == "accepted" else None,
                )
            except asyncio.CancelledError:
                write_ledger()  # the attempt's ledger row is written even when the closer is cancelled
                raise
            write_ledger()

        def stream_event(
            event_type: str,
            outcome: str,
            phase: str,
            cleanup_error: str | None,
            error_class: str | None,
        ) -> Any:
            stream_outcome = validated_stream.outcome
            details: dict[str, Any] = {
                "cleanup_status": "error" if cleanup_error else "closed",
                "cleanup_attempted": True,
            }
            if cleanup_error:
                details["cleanup_error_class"] = cleanup_error
            return build_outcome_event(
                route_evidence.routing_decision,
                event_type=event_type,
                outcome=outcome,
                status_code=result.status_code,
                features=features,
                streaming_phase=phase,
                abort_observed=phase != "completed",
                error_class=error_class,
                latency_ms=(monotonic() - started_at) * 1000,
                retries=max(0, len(attempts_used) - 1),
                fallbacks=attempts_used[1:],
                details={
                    **details,
                    "attempted_routes": attempts_used,
                    "compatibility_rule_version": result.compatibility_rule_version,
                    "omniroute_anomalies": omniroute_anomaly_names(result.headers),
                    **(
                        {"health_cache_feedback": health_cache_feedback}
                        if health_cache_feedback
                        else {}
                    ),
                    "actual_route": result.actual_route.to_dict()
                    if result.actual_route is not None
                    else None,
                    "actual_route_status": response_actual_route_status(result),
                    "identity_proof_status": identity_proof_status(
                        attempts_used[-1]["model"],
                        result.actual_route.model_id if result.actual_route else None,
                        attested=result.actual_route is not None,
                    ),
                    "stream_state": stream_outcome.state.value
                    if stream_outcome is not None
                    else None,
                },
            )

        evidence_stream = _EvidenceStreamAdapter(
            result.body,
            on_terminal=finalize_stream,
            event_factory=stream_event,
            outcome_of=lambda: validated_stream.outcome,
            event_prefix=event_prefix,
        )

        return _EvidenceStreamingResponse(
            evidence_stream,
            status_code=result.status_code,
            headers=response_headers,
            media_type=None,
        )
    raise TypeError(f"unsupported upstream response: {type(result)!r}")


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    """Route and transparently forward an OpenAI chat completion request."""
    return await _relay_completion(request, surface="chat")


@app.post("/v1/responses")
async def responses(request: Request) -> Response:
    """Route and transparently forward an OpenAI Responses request."""
    return await _relay_completion(request, surface="responses")


@app.post("/v1/messages")
async def messages(request: Request) -> Response:
    """Route and transparently forward an Anthropic Messages request."""
    return await _relay_completion(request, surface="messages")


def _start_evidence(
    decision: Any,
    *,
    task: str,
    criticality: str,
    features: dict[str, Any],
    request_id: str | None = None,
    correlation_id: str | None = None,
    scope: str,
    requested_identity: str | None = None,
    resolved_route: dict[str, Any] | None = None,
    attempted_routes: list[dict[str, Any]] | None = None,
) -> tuple[ExplainEvidence, str | None]:
    """Create and retain immutable decision evidence before upstream I/O."""

    routing = build_routing_decision_contract(
        decision,
        task=task,
        criticality=criticality,
        features=features,
        request_id=request_id,
        correlation_id=correlation_id,
        requested_identity=requested_identity,
        resolved_route=resolved_route,
        attempted_routes=attempted_routes,
    )
    started = build_outcome_event(
        routing,
        event_type="execution_started",
        outcome="unknown",
        features=features,
        streaming_phase="started" if features.get("stream") else None,
    )
    evidence = ExplainEvidence(routing, started)
    evidence_key = None
    if evidence_store_instance is not None:
        evidence_key = evidence_store_instance.put(evidence, scope=scope)
        stored = evidence_store_instance.find(evidence_id=evidence_key, scope=scope)
        if stored is not None:
            evidence = stored
    return evidence, evidence_key


def _finish_evidence(
    evidence: ExplainEvidence, event: Any, evidence_key: str | None = None
) -> ExplainEvidence:
    """Append a lifecycle event while retaining the decision-time snapshot."""

    events = evidence.events or (evidence.outcome_event,)
    updated = ExplainEvidence(evidence.routing_decision, event, events=(*events, event))
    if evidence_store_instance is not None and evidence_key is not None:
        stored = evidence_store_instance.update_outcome(evidence_key, event, scope=evidence.scope)
        if stored is not None:
            return stored
    return updated


def _finish_with_observation(
    evidence: ExplainEvidence,
    event: Any,
    evidence_key: str | None,
    observed_model: str | None,
    *,
    observe_first: bool = False,
) -> ExplainEvidence:
    """A terminal evidence event and the dispatch observation beside it, as one lane job.

    One job, so a waiter cancelled between the two cannot land one and lose the other.
    ``observed_model`` is the model whose dispatch to record as healthy, or ``None`` for none.
    The observation comes first on the buffered path and second on the stream path, as each
    always did.
    """
    from verdict.admit_prove_confirm import record_dispatch_observation

    if observed_model is not None and observe_first:
        record_dispatch_observation(
            observed_model, healthy=True, observed_at=datetime.now(timezone.utc)
        )
    finished = _finish_evidence(evidence, event, evidence_key)
    if observed_model is not None and not observe_first:
        record_dispatch_observation(
            observed_model, healthy=True, observed_at=datetime.now(timezone.utc)
        )
    return finished


def _commit_dispatch_permit(expected_scope: DispatchScope, **kwargs: Any) -> Any:
    """Commit one dispatch permit (state work: lock, reload, rewrite), for the lane."""
    return HealthCache(default_cache_path()).commit_dispatch(expected_scope, **kwargs)


def _cancelled_event(
    evidence: ExplainEvidence, *, event_prefix: str, features: dict[str, Any]
) -> Any:
    return build_outcome_event(
        evidence.routing_decision,
        event_type=f"{event_prefix}_cancelled",
        outcome="cancelled",
        features=features,
        abort_observed=True,
    )


def _queue_cancelled_terminal(
    evidence: ExplainEvidence,
    evidence_key: str | None,
    *,
    event_prefix: str,
    features: dict[str, Any],
) -> None:
    """Queue, without waiting, the cancelled terminal event of a request whose task was cancelled.

    Used when the task is cancelled while it waits on a lane job after its decision row exists
    and before any attempt could record the cancellation.  The event is queued behind the job
    the task was waiting on, so it lands after it; a request that already has a terminal event
    keeps it (an append of a different terminal outcome is refused and ignored).  A lane that
    has already closed (the app is shutting down) takes no more jobs: nothing is recorded then.
    """
    lane = state_lane_instance
    if lane is None or evidence_key is None:
        return
    event = _cancelled_event(evidence, event_prefix=event_prefix, features=features)
    with suppress(RuntimeError):
        lane.submit(_finish_evidence, evidence, event, evidence_key)


def _end_started_row(
    started: Future[tuple[ExplainEvidence, str | None]],
    *,
    event_prefix: str,
    features: dict[str, Any],
) -> None:
    """A lane job, queued behind ``started``'s: give the row it wrote its cancelled event."""
    if started.cancelled() or started.exception() is not None:
        return
    evidence, evidence_key = started.result()
    if evidence_key is not None:
        _finish_evidence(
            evidence,
            _cancelled_event(evidence, event_prefix=event_prefix, features=features),
            evidence_key,
        )


async def _start_evidence_on_lane(
    decision: Any, *, cancelled_event_prefix: str, **kwargs: Any
) -> tuple[ExplainEvidence, str | None]:
    """``_start_evidence`` on the lane; a cancelled waiter still gives the new row a terminal event.

    The job is queued at the call and runs whatever happens to this task, so a task cancelled
    while it waits would leave a decision row nobody ends.  On cancellation a second job is
    queued behind the first, in the same lane, to end the row it wrote.
    """
    lane = state_lane_instance
    if lane is None:
        return _start_evidence(decision, **kwargs)
    future = lane.submit(_start_evidence, decision, **kwargs)
    try:
        return await asyncio.shield(asyncio.wrap_future(future))
    except asyncio.CancelledError:
        # Queued now, behind the start job, so it is accepted while the lane is open, drained if
        # the lane is closing, and runs after the row exists.
        with suppress(RuntimeError):
            lane.submit(
                _end_started_row,
                future,
                event_prefix=cancelled_event_prefix,
                features=kwargs["features"],
            )
        raise


# ---------------------------------------------------------------------------
# Run-trace API (read-only; BOD plan 20261120-orchestration-adoption item 11)
# ---------------------------------------------------------------------------

# Maximum bytes of serialised events returned by a single /v1/runs/{id}/events
# response.  Protects against huge runs that would exceed response limits.
_RUNS_EVENTS_BYTE_BUDGET = 1 * 1024 * 1024  # 1 MiB

# Prefix read by _run_summary to find the run_started event.  run_started is
# always the first event, so 64 KiB is a generous bound.  Reading the whole
# file would cost hundreds of MB across _RUNS_SCAN_LIMIT runs per request.
_RUNS_SUMMARY_PREFIX_BYTES = 64 * 1024  # 64 KiB

# Maximum number of run directories scanned per /v1/runs request.  Prevents
# O(n) work when the runs root contains thousands of old directories.
_RUNS_SCAN_LIMIT = 500

# Maximum page sizes (also the FastAPI Query ``le=`` values; named here so
# tests can reference them without magic numbers).
_RUNS_PAGE_LIMIT_MAX = 100
_RUNS_EVENTS_LIMIT_MAX = 500

# Strict allowlist: alphanumeric start, then alphanumeric / dot / hyphen /
# underscore.  Explicitly excludes every path-separator character.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# Event-data credential redaction.  Keys whose names match this pattern have
# their values replaced with "[REDACTED]" in /v1/runs/{id}/events responses.
# This is defence-in-depth: events are written by internal code, but a future
# change might accidentally log a secret-shaped value.
_CREDENTIAL_KEY_RE = re.compile(
    r"^(api[_-]?key|password|secret|api[_-]?token|auth[_-]?token|credential)$", re.IGNORECASE
)


def _redact_event_data(data: Any) -> Any:
    """Recursively redact values in *data* whose key names match credential patterns."""
    if isinstance(data, dict):
        return {
            k: "[REDACTED]" if _CREDENTIAL_KEY_RE.match(k) else _redact_event_data(v)
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [_redact_event_data(item) for item in data]
    return data


def _runs_root() -> Path:
    """Configured runs directory; default ``<verdict_home()>/runs``.

    Returns the absolute path; never creates the directory.
    """
    raw = os.getenv("VERDICT_RUNS_DIR", "").strip()
    if raw:
        return Path(raw).resolve()
    from verdict.paths import verdict_home

    return verdict_home() / "runs"


def _resolve_run_dir(run_id: str) -> Path | None:
    """Validate *run_id* and return its absolute path, or ``None`` on failure.

    Returns ``None`` when:
    * *run_id* does not match ``_RUN_ID_RE``,
    * *run_id* is ``.`` or ``..``,
    * no directory with that exact name exists as a direct child of the runs root.

    The returned ``Path`` object is sourced from ``root.iterdir()`` (the
    filesystem), not constructed from the user-supplied *run_id*.  Static
    analysis taint tracking therefore cannot reach a file-system sink via
    *run_id*: user input is used only in the ``entry.name == run_id``
    comparison, which is a data-flow dead end, not a path expression.
    """
    if not _RUN_ID_RE.match(run_id) or run_id in {".", ".."}:
        return None
    root = _runs_root().resolve()
    try:
        for entry in root.iterdir():
            if entry.name != run_id:
                continue
            # Reject symlinks: is_dir() follows symlinks, so a symlink to a
            # directory outside the runs root would otherwise pass.
            if entry.is_symlink():
                return None
            if not entry.is_dir():
                return None
            # Defence-in-depth: confirm the resolved path stays inside root.
            if not entry.resolve().is_relative_to(root):
                return None
            return entry
    except OSError:
        pass
    return None


def _run_summary(run_id: str, run_dir: Path) -> dict[str, Any]:
    """Build a summary dict for one run directory; never raises.

    On read errors, logs a warning (exception type and run_id only — never file
    contents) and includes ``summary_error`` in the result so API callers can
    see that the entry is degraded.
    """
    started_at: str | None = None
    outcome: str | None = None
    receipt_status: str = "missing"
    summary_error: str | None = None

    # Both reads below use O_NOFOLLOW so a symlinked file inside a valid run
    # dir cannot leak outside content into the summary.
    from verdict.orchestration.receipt import _read_bytes_nofollow

    receipt_path = run_dir / "receipt.json"
    if receipt_path.exists():
        try:
            rdata: Any = json.loads(_read_bytes_nofollow(receipt_path))
            if isinstance(rdata, dict):
                started_at = rdata.get("started_at") or None
                outcome = rdata.get("outcome") or rdata.get("claimed_outcome") or None
                receipt_status = "present"
        except Exception as exc:
            receipt_status = "unreadable"
            summary_error = type(exc).__name__
            logger.warning(
                "_run_summary: %s reading receipt for run %s", type(exc).__name__, run_id
            )

    if started_at is None:
        events_path = run_dir / "events.jsonl"
        if events_path.exists():
            try:
                # Open with O_NOFOLLOW (atomic symlink rejection) and read
                # only a bounded prefix — run_started is the first event so
                # 64 KiB is always enough; reading the whole file would cost
                # hundreds of MB across _RUNS_SCAN_LIMIT runs per request.
                fd = os.open(str(events_path), os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as fh:
                    prefix = fh.read(_RUNS_SUMMARY_PREFIX_BYTES)
                for raw_line in prefix.splitlines():
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except Exception:
                        break
                    # Only the run_started event carries the authoritative
                    # start timestamp; ignore any earlier non-typed events.
                    if isinstance(evt, dict) and evt.get("type") == "run_started":
                        started_at = evt.get("at") or None
                        break
            except Exception as exc:
                if summary_error is None:
                    summary_error = type(exc).__name__
                logger.warning(
                    "_run_summary: %s reading events for run %s", type(exc).__name__, run_id
                )

    result: dict[str, Any] = {
        "run_id": run_id,
        "started_at": started_at,
        "outcome": outcome,
        "receipt_status": receipt_status,
    }
    if summary_error is not None:
        result["summary_error"] = summary_error
    return result


@app.get("/v1/runs")
async def list_runs(
    limit: int = Query(default=20, ge=1, le=_RUNS_PAGE_LIMIT_MAX),
    offset: int = Query(default=0, ge=0),
) -> Response:
    """List recent orchestration runs.

    Returns run_id, started_at, outcome and receipt_status for each run,
    sorted newest-first.  Reads ``VERDICT_RUNS_DIR`` (default
    ``<verdict_home()>/runs``).  Never writes to any file.
    """
    root = _runs_root()
    if not root.is_dir():
        return JSONResponse({"runs": [], "total": 0})

    def _collect() -> tuple[list[dict[str, Any]], int, bool]:
        # Store (started_at, name, summary) so each run is summarised exactly
        # once during the scan; the page slice reuses the cached summary dict.
        entries: list[tuple[str | None, str, dict[str, Any]]] = []
        scanned = 0
        scan_truncated = False
        for child in root.iterdir():
            if scanned >= _RUNS_SCAN_LIMIT:
                # More entries exist beyond the scan limit; total will be a
                # lower bound.  Pick the bounded path: add total_scan_truncated
                # to the response rather than scanning all directories.
                scan_truncated = True
                break
            scanned += 1
            if child.is_symlink() or not child.is_dir():
                continue
            name = child.name
            if not _RUN_ID_RE.match(name) or name in {".", ".."}:
                continue
            summary = _run_summary(name, child)
            entries.append((summary.get("started_at"), name, summary))

        entries.sort(key=lambda t: t[0] or "", reverse=True)
        total = len(entries)
        page = entries[offset : offset + limit]
        rows = [summary for _, _, summary in page]
        return rows, total, scan_truncated

    rows, total, scan_truncated = await asyncio.to_thread(_collect)
    return JSONResponse({"runs": rows, "total": total, "total_scan_truncated": scan_truncated})


@app.get("/v1/runs/{run_id}/events")
async def get_run_events(
    run_id: str,
    node_id: list[str] = Query(default=[]),
    type: list[str] = Query(default=[]),
    after_seq: int | None = Query(default=None, ge=1),
    seq_to: int | None = Query(default=None, ge=1),
    limit: int = Query(default=100, ge=1, le=_RUNS_EVENTS_LIMIT_MAX),
) -> Response:
    """Return a paginated window of events from one run's ``events.jsonl``.

    Filters: ``node_id`` (repeatable), ``type`` (repeatable),
    ``after_seq`` (exclusive lower bound), ``seq_to`` (inclusive upper bound).
    Response is bounded to 1 MiB; ``truncated`` is true when the byte budget
    was exhausted before the page limit was reached.  ``next_seq`` carries the
    seq of the first event not returned, or null when the log is exhausted.
    Never instantiates ``EventLog``; reads the file directly.
    """
    run_dir = _resolve_run_dir(run_id)
    if run_dir is None:
        raise HTTPException(status_code=404, detail={"error": {"message": "run not found"}})

    node_id_set: frozenset[str] = frozenset(node_id)
    type_set: frozenset[str] = frozenset(type)

    def _read() -> dict[str, Any]:
        events_path = run_dir / "events.jsonl"
        # Open with O_NOFOLLOW so the check and the read are one atomic
        # operation: a symlink swapped in after any earlier check is still
        # rejected.  OSError (ELOOP, ENOENT, …) → empty events, no 500.
        try:
            fd = os.open(str(events_path), os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return {"events": [], "next_seq": None, "truncated": False, "oversized_seqs": []}

        collected: list[dict[str, Any]] = []
        oversized_seqs: list[int] = []
        byte_total = 0
        truncated = False
        next_seq: int | None = None
        last_processed_seq: int | None = None  # tracks last seq emitted or skipped

        with os.fdopen(fd, "rb") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except Exception:
                    # Torn trailing line — treat as end of log, never repair.
                    break
                if not isinstance(evt, dict):
                    continue
                seq = evt.get("seq")
                if not isinstance(seq, int):
                    continue
                if after_seq is not None and seq <= after_seq:
                    continue
                if seq_to is not None and seq > seq_to:
                    break
                if node_id_set and evt.get("node_id", "") not in node_id_set:
                    continue
                if type_set and evt.get("type", "") not in type_set:
                    continue
                # Redact credential-like keys across the whole event dict
                # (top-level and all nested dicts/lists) before serving.
                safe_evt = _redact_event_data(evt)
                serialised = json.dumps(safe_evt, separators=(",", ":"))
                event_size = len(serialised.encode())
                if event_size > _RUNS_EVENTS_BYTE_BUDGET:
                    # This single event is larger than the whole byte budget.
                    # Omit it and continue scanning — a following event may fit.
                    # Report the seq so the caller knows a seq was skipped.
                    oversized_seqs.append(seq)
                    truncated = True
                    last_processed_seq = seq
                    continue
                if byte_total + event_size > _RUNS_EVENTS_BYTE_BUDGET:
                    # Adding this event would exceed the byte budget; stop here.
                    next_seq = seq
                    truncated = True
                    break
                byte_total += event_size
                collected.append(safe_evt)
                last_processed_seq = seq
                if len(collected) >= limit:
                    # Peek at the next matching event to populate next_seq.
                    # We read one more line from the file (already open).
                    for peek_line in fh:
                        peek = peek_line.strip()
                        if not peek:
                            continue
                        try:
                            peek_evt = json.loads(peek)
                        except Exception:
                            break
                        if not isinstance(peek_evt, dict):
                            continue
                        pseq = peek_evt.get("seq")
                        if not isinstance(pseq, int):
                            continue
                        # Only count this as next_seq if it passes the same filters.
                        if after_seq is not None and pseq <= after_seq:
                            continue
                        if seq_to is not None and pseq > seq_to:
                            break
                        if node_id_set and peek_evt.get("node_id", "") not in node_id_set:
                            continue
                        if type_set and peek_evt.get("type", "") not in type_set:
                            continue
                        next_seq = pseq
                        break
                    break

        # When truncated only because of oversized events (the byte-budget
        # and limit-peek paths set next_seq themselves), set next_seq to
        # one past the last emitted-or-skipped seq so the client always
        # has a valid cursor.  Convention matches the other two paths:
        # next_seq = first seq NOT in this response.
        if truncated and next_seq is None and last_processed_seq is not None:
            next_seq = last_processed_seq + 1
        return {
            "events": collected,
            "next_seq": next_seq,
            "truncated": truncated,
            "oversized_seqs": oversized_seqs,
        }

    result = await asyncio.to_thread(_read)
    return JSONResponse(result)


@app.get("/v1/runs/{run_id}/receipt")
async def get_run_receipt(run_id: str) -> Response:
    """Return the stored receipt and its verification result.

    Calls ``verify_run_receipt(repair=False)`` so EventLog never truncates
    events.jsonl, even if a live run appends a partial line between any
    intermediate check and the actual disk reads.  Never writes to any file.
    Returns ``verification.ok`` (bool) and ``verification.problems`` (list).
    """
    run_dir = _resolve_run_dir(run_id)
    if run_dir is None:
        raise HTTPException(status_code=404, detail={"error": {"message": "run not found"}})

    receipt_path = run_dir / "receipt.json"

    class _ReceiptNotReadableError(Exception):
        """Raised from _load_and_verify when the file is missing or is a symlink."""

    def _load_and_verify() -> dict[str, Any]:
        from verdict.orchestration.receipt import verify_run_receipt_from_bytes

        # Open receipt.json with O_NOFOLLOW so the check and the read are one
        # atomic operation: a symlink swapped in after any earlier check is
        # still rejected.  OSError covers ELOOP (symlink), ENOENT (missing),
        # and other access failures.
        try:
            fd = os.open(str(receipt_path), os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as exc:
            raise _ReceiptNotReadableError from exc

        try:
            with os.fdopen(fd, "rb") as fh:
                raw = fh.read()
        except Exception as exc:
            return {
                "run_id": run_id,
                "receipt": None,
                "verification": {"ok": False, "problems": [f"cannot read receipt: {exc}"]},
            }

        try:
            receipt_data: Any = json.loads(raw)
        except Exception as exc:
            return {
                "run_id": run_id,
                "receipt": None,
                "verification": {"ok": False, "problems": [f"cannot read receipt: {exc}"]},
            }

        # Read events.jsonl once with O_NOFOLLOW.  Both files are read exactly
        # once; verify_run_receipt_from_bytes uses these same bytes so the
        # verification verdict and the returned receipt always describe the
        # same content — no second read of either file.  OSError (ELOOP for
        # a symlink, ENOENT for a missing file, …) → events_raw=None →
        # verify_run_receipt_from_bytes reports a missing-events problem.
        events_path = run_dir / "events.jsonl"
        events_raw: bytes | None = None
        try:
            efd = os.open(str(events_path), os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(efd, "rb") as fh:
                events_raw = fh.read()
        except OSError:
            pass

        # Verification runs on raw file bytes; redaction happens after.
        # A torn tail or digest mismatch appears as problems in the response.
        problems = verify_run_receipt_from_bytes(raw, events_raw, run_dir)
        return {
            "run_id": run_id,
            "receipt": _redact_event_data(receipt_data),
            "verification": {"ok": not problems, "problems": problems},
        }

    try:
        result = await asyncio.to_thread(_load_and_verify)
    except _ReceiptNotReadableError:
        raise HTTPException(
            status_code=404, detail={"error": {"message": "receipt not found for run"}}
        ) from None
    return JSONResponse(result)


@app.post(OMNIROUTE_WEBHOOK_PATH)
async def omniroute_webhook(request: Request) -> Response:
    """Receive one HMAC-signed OmniRoute webhook delivery."""
    content_length = request.headers.get("content-length")
    if content_length is not None and (
        not content_length.isdigit() or int(content_length) > MAX_OMNIROUTE_WEBHOOK_BYTES
    ):
        return JSONResponse({"accepted": False, "reason": "body_too_large"}, status_code=413)
    body_parts: list[bytes] = []
    body_size = 0
    async for chunk in request.stream():
        body_size += len(chunk)
        if body_size > MAX_OMNIROUTE_WEBHOOK_BYTES:
            return JSONResponse({"accepted": False, "reason": "body_too_large"}, status_code=413)
        body_parts.append(chunk)
    raw_body = b"".join(body_parts)
    status_code, body = await asyncio.to_thread(
        accept_omniroute_webhook, raw_body, dict(request.headers)
    )
    return JSONResponse(content=body, status_code=status_code)


@app.post("/route")
async def route_task_alias(request: Request, req: RouteRequest) -> Response:
    """Convenience alias matching the integration test client path."""
    response = await route_task(request, req)
    return response


def start_server(port: int = 8000, host: str | None = None, reload: bool = False) -> None:
    """Boot the uvicorn server with explicit production security defaults."""
    import uvicorn

    configured_host: str = (
        host if host is not None else cast(str, os.getenv("LLMGATE_HOST", "127.0.0.1"))
    )
    unix_socket = os.getenv("LLMGATE_UNIX_SOCKET") or None
    allow_anonymous = os.getenv("LLMGATE_ALLOW_ANONYMOUS", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    validate_server_security(
        host=configured_host,
        token=os.getenv("LLMGATE_AUTH_TOKEN") or None,
        allow_anonymous=allow_anonymous,
        unix_socket=unix_socket,
    )
    # Same rule as ``lifespan`` (ADR-017), checked before uvicorn starts so a
    # missing path is one clear error instead of a lifespan traceback.
    if not allow_anonymous and not (
        os.getenv("VERDICT_RECEIPTS_DB") or os.getenv("VERDICT_EVIDENCE_DB")
    ):
        raise ValueError("VERDICT_RECEIPTS_DB must be configured for authenticated API mode")
    kwargs: dict[str, Any] = {"port": port}
    if unix_socket:
        kwargs["uds"] = unix_socket
    else:
        kwargs["host"] = configured_host
    if reload:
        # uvicorn requires an import string (not an app object) for reload
        # to work, since it needs to re-import the module on file changes.
        uvicorn.run("verdict.api:app", reload=True, **kwargs)
    else:
        uvicorn.run(app, **kwargs)
