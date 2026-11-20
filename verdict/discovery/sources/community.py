"""Community list source adapters.

Two community-maintained lists are fetched at refresh time from their public
GitHub raw URLs.  Both are MIT-licensed (GitHub classifies them as "Other"
because the license name is non-standard SPDX, but the texts are MIT-based).
See license check below.

**free-coding-models** (vava-nessa/free-coding-models)
  ``sources.js`` — a JavaScript file containing provider/model arrays.
  Parsed with a narrow, evaluated-free parser that extracts only the
  structured array literals.  Any shape we cannot parse fails closed
  (returns no rows, records a warning) rather than evaluating the JS.

  License: "MIT License with Enhanced Disclaimers" — permits reuse.

**dsh-our-free-model** (Ebony-Vinyl/dsh-our-free-model)
  ``src/providers/registry.json`` — a JSON array of provider objects.
  Each provider has a ``models`` array.

  License: standard MIT License — permits reuse.

Both sources are pinned to a commit SHA given in ``CommunityConfig``; the
default pinned SHA is the HEAD SHA at the time this module was written.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from verdict.discovery.sources._fetcher import Fetcher, default_fetcher
from verdict.discovery.sources._types import DiscoveryRow

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FCM_REPO = "vava-nessa/free-coding-models"
_FCM_FILE = "sources.js"
# To bump this pin: update the SHA to the new HEAD commit of the repo and
# update the corresponding tests in tests/test_discovery_sources.py.
# Use --fcm-commit <sha> at the CLI to override without changing source.
_FCM_DEFAULT_COMMIT = "b9269a5408d69844604b1038d5f96b6575d54b86"
_FCM_LICENSE = "MIT-with-Enhanced-Disclaimers"
_FCM_LICENSE_RESTRICTED = False  # MIT-based, permits reuse

_DSH_REPO = "Ebony-Vinyl/dsh-our-free-model"
_DSH_FILE = "src/providers/registry.json"
# To bump this pin: update the SHA to the new HEAD commit of the repo and
# update the corresponding tests in tests/test_discovery_sources.py.
# Use --dsh-commit <sha> at the CLI to override without changing source.
_DSH_DEFAULT_COMMIT = "ba266c6e268bc5fb9227fb1353149e4a99f77739"
_DSH_LICENSE = "MIT"
_DSH_LICENSE_RESTRICTED = False  # standard MIT, permits reuse

_GITHUB_RAW = "https://raw.githubusercontent.com"


def _raw_url(repo: str, file: str, commit: str) -> str:
    return f"{_GITHUB_RAW}/{repo}/{commit}/{file}"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommunityConfig:
    """Pin configuration for community list sources."""

    fcm_commit: str = _FCM_DEFAULT_COMMIT
    dsh_commit: str = _DSH_DEFAULT_COMMIT


# ---------------------------------------------------------------------------
# free-coding-models  ``sources.js`` parser
# ---------------------------------------------------------------------------

# Narrow pattern: match only the top-level ``export const <name> = [...]``
# or ``export default { ... <name>: [...] ... }`` array assignments.
# We never evaluate or execute the JS; we only extract array literals that
# match the documented schema [model_id, label, tier, swe_score, ctx].
#
# The file defines objects like:
#   export const groq = [
#     ["llama-3.3-70b-versatile", "Llama 3.3 70B", "A", 49, "128k"],
#     ...
#   ];

# Match a named array export (const or let): capture name + array body.
_NAMED_EXPORT_RE = re.compile(r"""(?:export\s+(?:const|let)\s+(\w+)\s*=\s*)(\[[\s\S]*?\]);""")

# Lightweight array-of-arrays JSON extractor: validates only array-of-array
# shape (no object nesting, no template literals, no function calls).
_ARRAY_ENTRY_RE = re.compile(
    r"""\[\s*"([^"]+)"\s*,\s*"([^"]*?)"\s*(?:,\s*"([^"]*?)"\s*)?(?:,\s*([0-9.]+|null)\s*)?(?:,\s*"([^"]*?)"\s*)?\s*\]"""
)


def _parse_fcm_js(text: str) -> list[tuple[str, str]]:
    """Extract (provider_name, model_id) pairs from ``sources.js``.

    Returns an empty list if the file cannot be parsed safely.  Never
    evaluates the JavaScript.
    """
    results: list[tuple[str, str]] = []
    for match in _NAMED_EXPORT_RE.finditer(text):
        provider_name = match.group(1)
        array_body = match.group(2)
        # Validate: array body must only contain simple array-of-arrays entries.
        # If it contains object braces or function calls, skip it.
        if "{" in array_body or "(" in array_body or "`" in array_body:
            continue
        for entry_match in _ARRAY_ENTRY_RE.finditer(array_body):
            model_id = entry_match.group(1)
            if model_id:
                results.append((provider_name, model_id))
    return results


def parse_fcm(
    text: str, *, fetched_at: str | None = None, commit: str = _FCM_DEFAULT_COMMIT
) -> list[DiscoveryRow]:
    """Parse ``sources.js`` from free-coding-models.

    Returns one :class:`~verdict.discovery.sources._types.DiscoveryRow` per
    model entry.  ``free_flag`` is not set (the list is a catalog, not a
    free-tier list).
    """
    ts = fetched_at or datetime.now(timezone.utc).isoformat()
    source_url = _raw_url(_FCM_REPO, _FCM_FILE, commit)
    rows: list[DiscoveryRow] = []
    for provider_name, model_id in _parse_fcm_js(text):
        # Use provider/model_id as the route ID.
        route_id = f"{provider_name}/{model_id}" if "/" not in model_id else model_id
        rows.append(
            DiscoveryRow(
                id=route_id,
                provider=provider_name,
                source="free_coding_models",
                source_url=source_url,
                fetched_at=ts,
                license=_FCM_LICENSE,
                license_restricted=_FCM_LICENSE_RESTRICTED,
                commit=commit,
                repo=_FCM_REPO,
            )
        )
    return rows


def fetch_fcm(
    *,
    fetcher: Fetcher = default_fetcher,
    commit: str = _FCM_DEFAULT_COMMIT,
    timeout: float = 20.0,
    max_bytes: int = 4 * 1024 * 1024,
) -> list[DiscoveryRow]:
    """Fetch and parse ``sources.js`` from free-coding-models."""
    ts = datetime.now(timezone.utc).isoformat()
    url = _raw_url(_FCM_REPO, _FCM_FILE, commit)
    raw = fetcher(url, timeout=timeout, max_bytes=max_bytes)
    text = raw.decode(errors="replace")
    return parse_fcm(text, fetched_at=ts, commit=commit)


# ---------------------------------------------------------------------------
# dsh-our-free-model  ``src/providers/registry.json`` parser
# ---------------------------------------------------------------------------


def parse_dsh(
    payload: Any, *, fetched_at: str | None = None, commit: str = _DSH_DEFAULT_COMMIT
) -> list[DiscoveryRow]:
    """Parse ``src/providers/registry.json`` from dsh-our-free-model.

    The registry is an array of provider objects; each has an ``id`` and a
    ``models`` array of model objects with an ``id`` field.
    """
    ts = fetched_at or datetime.now(timezone.utc).isoformat()
    source_url = _raw_url(_DSH_REPO, _DSH_FILE, commit)
    rows: list[DiscoveryRow] = []

    if not isinstance(payload, list):
        return rows

    for provider_obj in payload:
        if not isinstance(provider_obj, dict):
            continue
        provider_id = provider_obj.get("id")
        if not isinstance(provider_id, str) or not provider_id:
            continue
        models = provider_obj.get("models")
        if not isinstance(models, list):
            continue
        for model_obj in models:
            if not isinstance(model_obj, dict):
                continue
            model_id = model_obj.get("id")
            if not isinstance(model_id, str) or not model_id:
                continue
            route_id = f"{provider_id}/{model_id}"

            ctx_raw = model_obj.get("contextWindow")
            ctx = int(ctx_raw) if isinstance(ctx_raw, int) else None

            # Vision / reasoning as capability signals
            vision = model_obj.get("vision")
            reasoning = model_obj.get("reasoning")
            # Treat vision-capable models as likely tool-supporting (heuristic only)
            supports_tools: bool | None = None
            if isinstance(vision, bool) or isinstance(reasoning, bool):
                supports_tools = bool(vision) or bool(reasoning)

            rows.append(
                DiscoveryRow(
                    id=route_id,
                    provider=provider_id,
                    source="dsh_our_free_model",
                    source_url=source_url,
                    fetched_at=ts,
                    context_length=ctx,
                    supports_tools=supports_tools,
                    license=_DSH_LICENSE,
                    license_restricted=_DSH_LICENSE_RESTRICTED,
                    commit=commit,
                    repo=_DSH_REPO,
                )
            )
    return rows


def fetch_dsh(
    *,
    fetcher: Fetcher = default_fetcher,
    commit: str = _DSH_DEFAULT_COMMIT,
    timeout: float = 20.0,
    max_bytes: int = 2 * 1024 * 1024,
) -> list[DiscoveryRow]:
    """Fetch and parse ``src/providers/registry.json`` from dsh-our-free-model."""
    ts = datetime.now(timezone.utc).isoformat()
    url = _raw_url(_DSH_REPO, _DSH_FILE, commit)
    raw = fetcher(url, timeout=timeout, max_bytes=max_bytes)
    payload = json.loads(raw)  # JSONDecodeError (ValueError subclass) propagates as source_error
    return parse_dsh(payload, fetched_at=ts, commit=commit)
