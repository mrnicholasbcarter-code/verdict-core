"""Aggregate entry point: collect rows from all discovery sources.

:func:`collect_sources` runs all enabled adapters and returns the combined
list of :class:`~verdict.discovery.sources._types.DiscoveryRow` objects.
Story S3's registry will consume this.

Network adapters (openrouter, kilo, models_dev, community lists) use an
injectable ``Fetcher`` so the caller can run them offline in tests.  The
OmniRoute adapter accepts an ``OmniRouteEvidence`` object (or a zero-arg
callable returning one) so it is also testable without HTTP.
"""

from __future__ import annotations

import contextlib
from typing import Any

from verdict.discovery.sources._fetcher import Fetcher, default_fetcher
from verdict.discovery.sources._types import DiscoveryRow
from verdict.discovery.sources.community import CommunityConfig, fetch_dsh, fetch_fcm
from verdict.discovery.sources.kilo import fetch_kilo
from verdict.discovery.sources.models_dev import fetch_models_dev
from verdict.discovery.sources.omniroute import parse_free_tier_ids, rows_from_evidence
from verdict.discovery.sources.openrouter import fetch_openrouter


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
    include_community: bool = True,
) -> list[DiscoveryRow]:
    """Collect rows from all enabled discovery sources.

    Returns a combined list; each row names its ``source`` and ``fetched_at``.
    Sources that fail are skipped; no exception propagates to the caller.

    Args:
        omniroute_evidence: An ``OmniRouteEvidence`` instance, a zero-arg
            callable returning one, or ``None`` to skip OmniRoute.
        omniroute_free_tier_payload: Raw JSON dict from
            ``/api/free-tier/summary``, or ``None`` if unavailable.
        omniroute_base_url: Base URL recorded in OmniRoute rows' provenance.
        fetcher: HTTP fetcher; inject
            :func:`~verdict.discovery.sources._fetcher.make_stub_fetcher`
            in tests for fully offline operation.
        community: Pin configuration for community list sources.
        include_*: Toggle individual sources.

    Returns:
        All rows from all enabled sources, in source order.
    """
    cfg = community or CommunityConfig()
    rows: list[DiscoveryRow] = []

    # 1. OmniRoute
    if include_omniroute and omniroute_evidence is not None:
        with contextlib.suppress(Exception):
            evidence = omniroute_evidence() if callable(omniroute_evidence) else omniroute_evidence
            free_ids = (
                parse_free_tier_ids(omniroute_free_tier_payload)
                if omniroute_free_tier_payload is not None
                else ()
            )
            rows.extend(
                rows_from_evidence(evidence, base_url=omniroute_base_url, free_tier_ids=free_ids)
            )

    # 2. OpenRouter
    if include_openrouter:
        with contextlib.suppress(Exception):
            rows.extend(fetch_openrouter(fetcher=fetcher))

    # 3. Kilo
    if include_kilo:
        with contextlib.suppress(Exception):
            rows.extend(fetch_kilo(fetcher=fetcher))

    # 4. models.dev
    if include_models_dev:
        with contextlib.suppress(Exception):
            rows.extend(fetch_models_dev(fetcher=fetcher))

    # 5. Community lists
    if include_community:
        with contextlib.suppress(Exception):
            rows.extend(fetch_fcm(fetcher=fetcher, commit=cfg.fcm_commit))
        with contextlib.suppress(Exception):
            rows.extend(fetch_dsh(fetcher=fetcher, commit=cfg.dsh_commit))

    return rows
