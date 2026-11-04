"""Optional OmniRoute free-provider rankings advisory, parsed fail-closed.

Rankings are ADVISORY only: they reorder already-admitted routes and never
re-admit, entitle, or prove capability. The rankings endpoint is an OmniRoute
management API, not part of the provider-neutral OpenAI-compatible contract
(ADR-020), so generic gateways are skipped with a named omission.
"""

from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from verdict.free_tier_admit import normalize_omniroute_origin
from verdict.http_safety import open_no_redirect


@dataclass(frozen=True)
class RankingRow:
    """One provider's best-scored model. Provider-level, not whole-catalog."""

    id: str
    top_model_id: str
    score: float
    reliability: float | None = None
    reliability_context: dict[str, Any] | None = None
    usage_context: dict[str, Any] | None = None


@dataclass(frozen=True)
class RankingsResult:
    rows: tuple[RankingRow, ...]
    omission: str | None = None
    source: str | None = None
    received_at: str | None = None
    source_observed_at: str | None = None

    def is_fresh(self, now: datetime, *, max_age_seconds: float = 86400.0) -> bool:
        """Bound local observation age; the gateway's source age remains unknown."""
        if self.received_at is None:
            return False
        try:
            received = datetime.fromisoformat(self.received_at.replace("Z", "+00:00"))
            if received.tzinfo is None or now.tzinfo is None:
                return False
            return 0 <= (now - received).total_seconds() < max_age_seconds
        except (ValueError, TypeError, OverflowError):
            return False


# A fetcher returns the decoded JSON payload for a URL, or raises on transport error.
JsonFetcher = Callable[[str, str | None, float], Any]


def _default_fetch_json(url: str, api_key: str | None, timeout: float) -> Any:
    """Bounded GET through Verdict's redirect-refusing transport (same shape as run._get_json)."""
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if urllib.parse.urlsplit(url).scheme not in {"http", "https"}:
        raise ValueError(f"gateway URL must be http(s): {url!r}")
    request = urllib.request.Request(url, headers=headers)
    with open_no_redirect(request, timeout=timeout) as response:
        return json.loads(response.read(64 * 1024 * 1024))


def _reliability(rel: Any) -> float | None:
    """Numeric reliability only when the source sends a number. Unknown stays None, never zero.

    A categorical state such as ``healthy`` is kept in ``reliability_context``; it is
    never converted into a hand-authored score.
    """
    if isinstance(rel, (int, float)) and not isinstance(rel, bool):
        return float(rel)
    return None


class OmniRouteRankings:
    """Fetch OmniRoute free-provider rankings for one task category."""

    def __init__(
        self,
        gateway: str,
        api_key: str | None,
        *,
        fetch_json: JsonFetcher | None = None,
        timeout: float = 10.0,
        gateway_kind: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.gateway = normalize_omniroute_origin(gateway).rstrip("/")
        self.api_key = api_key
        self._fetch_json = fetch_json or _default_fetch_json
        self.timeout = timeout
        self.gateway_kind = (
            (gateway_kind if gateway_kind is not None else os.getenv("VERDICT_GATEWAY_KIND", ""))
            .strip()
            .casefold()
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def fetch(self, category: str) -> RankingsResult:
        """Fetch rankings for a category, or a named omission on any failure."""
        if not self._is_omniroute_gateway():
            return RankingsResult((), "generic_gateway")
        url = (
            self.gateway
            + "/api/free-provider-rankings?"
            + urllib.parse.urlencode(
                {
                    "category": category,
                    "configuredOnly": "1",
                    "availableOnly": "1",
                    "withUsage": "1",
                    "usageRange": "24h",
                }
            )
        )
        try:
            payload = self._fetch_json(url, self.api_key, self.timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return RankingsResult((), "unsupported_or_missing")
            return RankingsResult((), "unavailable")
        except (urllib.error.URLError, OSError, ValueError):
            return RankingsResult((), "unavailable")
        return replace(
            self._parse(payload),
            source="omniroute:/api/free-provider-rankings",
            received_at=self._clock().isoformat(),
        )

    def _parse(self, payload: Any) -> RankingsResult:
        raw = payload.get("rankings") if isinstance(payload, Mapping) else None
        if not isinstance(raw, list):
            return RankingsResult((), "malformed")
        rows: list[RankingRow] = []
        # Any invalid row rejects the whole response: a partial subset never reorders.
        for item in raw:
            if not isinstance(item, Mapping):
                return RankingsResult((), "malformed")
            top = item.get("topModel")
            if not isinstance(top, Mapping):
                return RankingsResult((), "malformed")
            provider_id = item.get("id")
            model = top.get("modelId")
            # Score lives inside topModel in the real API.
            score = top.get("score")
            if not (
                isinstance(provider_id, str)
                and provider_id
                and isinstance(model, str)
                and model
                and isinstance(score, (int, float))
                and not isinstance(score, bool)
                and math.isfinite(float(score))
            ):
                return RankingsResult((), "malformed")
            reliability = item.get("reliability")
            context = dict(reliability) if isinstance(reliability, Mapping) else None
            usage = context.get("usage") if context is not None else None
            rows.append(
                RankingRow(
                    provider_id,
                    model,
                    float(score),
                    _reliability(reliability),
                    context,
                    dict(usage) if isinstance(usage, Mapping) else None,
                )
            )
        return RankingsResult(tuple(rows))

    def get_rankings(self, category: str) -> list[RankingRow]:
        """Return valid rows only; advisory failures produce an empty list."""
        return list(self.fetch(category).rows)

    def _is_omniroute_gateway(self) -> bool:
        """True only for an OmniRoute gateway; generic OpenAI-compatible gateways are skipped."""
        return self.gateway_kind == "omniroute"
