"""Community list source adapters.

Two community-maintained lists are fetched at refresh time from their public
GitHub raw URLs.  Both are MIT-licensed (GitHub classifies them as "Other"
because the license name is non-standard SPDX, but the texts are MIT-based).
See license check below.

**free-coding-models** (vava-nessa/free-coding-models)
  ``sources.js`` — a JavaScript file containing provider/model arrays.
  Parsed as literal model tuples and provider references, without executing
  JavaScript. Fetches reject an invalid or empty active roster instead of
  reporting a successful empty catalog.

  License: "MIT License with Enhanced Disclaimers" — permits reuse.

**dsh-our-free-model** (Ebony-Vinyl/dsh-our-free-model)
  ``src/providers/registry.json`` — a JSON array of provider objects.
  Each provider has a ``models`` array.

  License: standard MIT License — permits reuse.

``CommunityConfig`` provides reproducible pins for both sources.
``fetch_fcm_current`` resolves the current revision of the upstream roster
before fetching it by immutable commit SHA.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
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

# A narrow grammar: strings, numbers and null in flat model tuples, and
# provider objects containing scalar metadata and a named models reference.
# Consume comments as tokens so comment-like text inside strings is preserved.
_JS_STRING = r"'(?:[^'\\\r\n]|\\[^\r\n])*'|\"(?:[^\"\\\r\n]|\\[^\r\n])*\""
_JS_COMMENT_OR_STRING = re.compile(rf"(?P<string>{_JS_STRING})|//[^\n]*|/\*[\s\S]*?\*/")
_JS_IDENT = r"[A-Za-z_$][\w$-]*"
_JS_KEY = rf"(?:{_JS_STRING}|{_JS_IDENT})"
_JS_LITERAL = rf"(?:{_JS_STRING}|null|-?\d+(?:\.\d+)?)"
_EXPORT = re.compile(rf"(?m)^\s*export\s+(?:const|let)\s+({_JS_IDENT})\s*=\s*")
_TUPLE = re.compile(rf"\[\s*({_JS_LITERAL}(?:\s*,\s*{_JS_LITERAL}){{1,6}}\s*,?)\s*\]")
_LITERAL = re.compile(_JS_LITERAL)
_MAP_FIELD = rf"{_JS_KEY}\s*:\s*(?:{_JS_LITERAL}|{_JS_IDENT})"
_PROVIDER = re.compile(
    rf"({_JS_KEY})\s*:\s*\{{\s*((?:{_MAP_FIELD}\s*,\s*)*(?:{_MAP_FIELD}\s*)?)\}}"
)
_MODELS_FIELD = re.compile(rf"({_JS_KEY})\s*:\s*({_JS_IDENT}|{_JS_LITERAL})")
_MODEL_ID = re.compile(r"[A-Za-z0-9_@][A-Za-z0-9_.@:/+-]*")
_DECLARATION_END = re.compile(r"[^\S\n]*(?:;|\n\s*(?=export\b|const\b|\Z)|\Z)")


def _fcm_members(
    text: str, start: int, pattern: re.Pattern[str], end: str
) -> list[re.Match[str]] | None:
    """Read comma-separated literals followed by the end of the declaration."""
    members: list[re.Match[str]] = []
    i = start
    ws = re.compile(r"\s*")
    while True:
        space = ws.match(text, i)
        assert space is not None  # whitespace also matches the empty string
        i = space.end()
        if text.startswith(end, i):
            return members if _DECLARATION_END.match(text, i + len(end)) else None
        match = pattern.match(text, i)
        if match is None:
            return None
        members.append(match)
        space = ws.match(text, match.end())
        assert space is not None
        i = space.end()
        if text.startswith(end, i):
            return members if _DECLARATION_END.match(text, i + len(end)) else None
        if not text.startswith(",", i):
            return None
        i += 1


def _parse_fcm_js(text: str, *, today: date) -> list[tuple[str, str]]:
    """Extract active provider/model IDs; never run upstream JavaScript."""
    text = _JS_COMMENT_OR_STRING.sub(
        lambda m: m.group("string") or "\n" * m[0].count("\n") or " ", text
    )
    arrays: dict[str, list[str]] = {}
    sources: list[tuple[str, str]] | None = None
    for export in _EXPORT.finditer(text):
        name, start = export[1], export.end()
        if name == "sources":
            sources = []
            if not text.startswith("{", start):
                return []
            providers = _fcm_members(text, start + 1, _PROVIDER, "}")
            if providers is None:
                return []
            for provider in providers:
                fields = {m[1].strip("'\""): m[2] for m in _MODELS_FIELD.finditer(provider[2])}
                if "models" not in fields:
                    return []
                sources.append((provider[1].strip("'\""), fields["models"]))
        elif name != "MODELS" and text.startswith("[", start):
            tuples = _fcm_members(text, start + 1, _TUPLE, "]")
            if tuples is None:
                continue
            ids: list[str] = []
            try:
                for row in tuples:
                    values = _LITERAL.findall(row[1])
                    model_id = ast.literal_eval(values[0])
                    if not isinstance(model_id, str) or not _MODEL_ID.fullmatch(model_id):
                        raise ValueError("Invalid model identifier")
                    if (
                        len(values) == 7
                        and values[6] != "null"
                        and today > date.fromisoformat(ast.literal_eval(values[6]))
                    ):
                        continue
                    ids.append(model_id)
            except (SyntaxError, TypeError, ValueError):
                continue
            arrays[name] = ids

    if sources is None:
        sources = [(name, name) for name in arrays]
    if any(name not in arrays for _, name in sources):
        return []
    return list(
        dict.fromkeys((provider, model) for provider, name in sources for model in arrays[name])
    )


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
    for provider_name, model_id in _parse_fcm_js(text, today=date.fromisoformat(ts[:10])):
        rows.append(
            DiscoveryRow(
                id=f"{provider_name}/{model_id}",
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
    """Fetch a pinned FCM roster; an empty or invalid roster is a source failure."""
    url = _raw_url(_FCM_REPO, _FCM_FILE, commit)
    raw = fetcher(url, timeout=timeout, max_bytes=max_bytes)
    rows = parse_fcm(raw.decode("utf-8"), commit=commit)
    if not rows:
        raise ValueError("fcm_empty_or_invalid_roster")
    return rows


def fetch_fcm_current(
    *, fetcher: Fetcher = default_fetcher, timeout: float = 20.0, max_bytes: int = 4 * 1024 * 1024
) -> list[DiscoveryRow]:
    """Resolve the latest change to the roster and fetch that immutable source."""
    url = f"https://api.github.com/repos/{_FCM_REPO}/commits?path={_FCM_FILE}&per_page=1"
    response = json.loads(fetcher(url, timeout=timeout, max_bytes=65_536))
    first = response[0] if isinstance(response, list) and response else None
    commit = first.get("sha") if isinstance(first, dict) else None
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("fcm_invalid_current_commit")
    return fetch_fcm(fetcher=fetcher, commit=commit, timeout=timeout, max_bytes=max_bytes)


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
