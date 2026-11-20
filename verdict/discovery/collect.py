"""Aggregate entry point: collect rows from all discovery sources.

:func:`collect_sources` runs all enabled adapters and returns a
:class:`CollectResult` with both the combined
:class:`~verdict.discovery.sources._types.DiscoveryRow` list and any
per-source :class:`SourceError` records.  Story S3's registry will consume
this.

Network adapters (openrouter, kilo, models_dev, community lists) use an
injectable ``Fetcher`` so the caller can run them offline in tests.  The
OmniRoute adapter accepts an ``OmniRouteEvidence`` object (or a zero-arg
callable returning one) so it is also testable without HTTP.

A failed source is always recorded in :attr:`CollectResult.errors`.  An
empty ``rows`` list for a source means the source returned no models;
a non-empty ``errors`` list means at least one source *failed* and must
not be treated as a successful-but-empty result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from verdict.discovery.sources._fetcher import Fetcher, default_fetcher
from verdict.discovery.sources._types import DiscoveryRow
from verdict.discovery.sources.community import CommunityConfig, fetch_dsh, fetch_fcm
from verdict.discovery.sources.kilo import fetch_kilo
from verdict.discovery.sources.models_dev import fetch_models_dev
from verdict.discovery.sources.omniroute import parse_free_tier_ids, rows_from_evidence
from verdict.discovery.sources.openrouter import fetch_openrouter

# Exceptions that indicate a transient source outage (network/data) rather than
# a bug in the adapter code.
_OUTAGE_EXC = (OSError, ValueError, UnicodeDecodeError)

# Top-up patterns applied after verdict.security.redact_text to cover gaps:
#
# 1. URL query-string credential params (e.g. ``?key=…``, ``&api_key=…``).
#    redact_text covers user:pass@ URLs but not query params with bare key names.
_URL_QUERY_CRED_RE = re.compile(
    r"([?&](?:api[_-]?key|token|key|secret|password|auth)[^=&]*=)[^\s&,;#]+", re.IGNORECASE
)
# 2. ``Authorization: Basic <token>``.  redact_text's CREDENTIAL_SHAPES covers
#    Bearer but not Basic.
_AUTH_BASIC_RE = re.compile(r"(?i)(authorization\s*:\s*basic\s+)[^\s,;]+")


def _sanitize_exc_msg(exc: BaseException, *, limit: int = 200) -> str:
    """Return a sanitized, truncated exception message.

    Uses :func:`~verdict.security.redact_text` as the primary credential
    redactor, then applies two targeted top-up patterns that it does not cover:
    URL query-string credential parameters and ``Authorization: Basic`` headers.
    """
    from verdict.security import redact_text

    sanitized = redact_text(str(exc))
    sanitized = _URL_QUERY_CRED_RE.sub(r"\1[redacted]", sanitized)
    sanitized = _AUTH_BASIC_RE.sub(r"\1[redacted]", sanitized)
    return sanitized[:limit]


@dataclass(frozen=True)
class SourceError:
    """A single per-source failure recorded by :func:`collect_sources`.

    Attributes:
        source: The adapter name (e.g. ``"kilo"``).
        error_type: The exception class name (e.g. ``"OSError"``).
        message: A short, human-readable description; no secrets.
        error_kind: ``"source_error"`` for transient network / data failures;
            ``"adapter_bug"`` for unexpected exceptions in the adapter code.
        location: For ``adapter_bug`` only — the innermost traceback frame as
            ``"file:line:function"``.  ``None`` for ``source_error`` entries.
            Never contains the exception message, which may carry secrets.
    """

    source: str
    error_type: str
    message: str
    error_kind: str = "source_error"
    location: str | None = None


@dataclass
class CollectResult:
    """Return value of :func:`collect_sources`.

    Attributes:
        rows: All :class:`~verdict.discovery.sources._types.DiscoveryRow`
            objects from every source that succeeded.
        errors: One :class:`SourceError` per adapter that raised.  An empty
            list means every enabled adapter ran without error.  A source
            that errored must *not* appear as an empty-success entry in
            ``rows``; it only appears here.
    """

    rows: list[DiscoveryRow] = field(default_factory=list)
    errors: list[SourceError] = field(default_factory=list)


def collect_sources(
    *,
    omniroute_evidence: Any = None,
    omniroute_free_tier_payload: Any = None,
    omniroute_base_url: str = "http://localhost:20128",
    fetcher: Fetcher = default_fetcher,
    community: CommunityConfig | None = None,
    include_omniroute: bool = True,
    include_openrouter: bool = True,
    include_kilo: bool = True,
    include_models_dev: bool = True,
    include_fcm: bool = True,
    include_dsh: bool = True,
) -> CollectResult:
    """Collect rows from all enabled discovery sources.

    Returns a :class:`CollectResult` with ``rows`` from every source that
    succeeded and ``errors`` for every source that raised.  A failed source
    never appears as an empty success.

    Args:
        omniroute_evidence: An ``OmniRouteEvidence`` instance, a zero-arg
            callable returning one, or ``None`` to skip OmniRoute.
        omniroute_free_tier_payload: Raw JSON dict from
            ``/api/free-tier/summary``, or ``None`` if unavailable.
        omniroute_base_url: Base URL recorded in OmniRoute rows' provenance.
        fetcher: HTTP fetcher; inject
            :func:`~tests.helpers_discovery.make_stub_fetcher`
            in tests for fully offline operation.
        community: Pin configuration for community list sources.
        include_*: Toggle individual sources.  ``include_fcm`` controls the
            free-coding-models list; ``include_dsh`` controls the
            dsh-our-free-model list.

    Returns:
        :class:`CollectResult` with combined rows and any per-source errors.
    """
    cfg = community or CommunityConfig()
    result = CollectResult()

    def _run(source: str, fn: Any, *args: Any, **kwargs: Any) -> None:
        try:
            result.rows.extend(fn(*args, **kwargs))
        except _OUTAGE_EXC as exc:
            result.errors.append(
                SourceError(
                    source=source,
                    error_type=type(exc).__name__,
                    message=_sanitize_exc_msg(exc),
                    error_kind="source_error",
                )
            )
        except Exception as exc:
            import traceback as _tb

            loc: str | None = None
            if exc.__traceback__ is not None:
                frame = _tb.extract_tb(exc.__traceback__)[-1]
                loc = f"{frame.filename}:{frame.lineno}:{frame.name}"
            result.errors.append(
                SourceError(
                    source=source,
                    error_type=type(exc).__name__,
                    message=_sanitize_exc_msg(exc),
                    error_kind="adapter_bug",
                    location=loc,
                )
            )

    # 1. OmniRoute
    if include_omniroute and omniroute_evidence is not None:
        try:
            evidence = omniroute_evidence() if callable(omniroute_evidence) else omniroute_evidence
            free_ids = (
                parse_free_tier_ids(omniroute_free_tier_payload)
                if omniroute_free_tier_payload is not None
                else ()
            )
            result.rows.extend(
                rows_from_evidence(evidence, base_url=omniroute_base_url, free_tier_ids=free_ids)
            )
        except _OUTAGE_EXC as exc:
            result.errors.append(
                SourceError(
                    source="omniroute",
                    error_type=type(exc).__name__,
                    message=_sanitize_exc_msg(exc),
                    error_kind="source_error",
                )
            )
        except Exception as exc:
            import traceback as _tb

            loc_omni: str | None = None
            if exc.__traceback__ is not None:
                frame = _tb.extract_tb(exc.__traceback__)[-1]
                loc_omni = f"{frame.filename}:{frame.lineno}:{frame.name}"
            result.errors.append(
                SourceError(
                    source="omniroute",
                    error_type=type(exc).__name__,
                    message=_sanitize_exc_msg(exc),
                    error_kind="adapter_bug",
                    location=loc_omni,
                )
            )

    # 2. OpenRouter
    if include_openrouter:
        _run("openrouter", fetch_openrouter, fetcher=fetcher)

    # 3. Kilo
    if include_kilo:
        _run("kilo", fetch_kilo, fetcher=fetcher)

    # 4. models.dev
    if include_models_dev:
        _run("models_dev", fetch_models_dev, fetcher=fetcher)

    # 5. Community lists
    if include_fcm:
        _run("free_coding_models", fetch_fcm, fetcher=fetcher, commit=cfg.fcm_commit)
    if include_dsh:
        _run("dsh_our_free_model", fetch_dsh, fetcher=fetcher, commit=cfg.dsh_commit)

    return result
