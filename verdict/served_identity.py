"""Verify which route OmniRoute actually served, from its response headers.

OmniRoute 3.8.53 attaches the serving identity to every completion response
(``src/domain/omnirouteResponseMeta.ts``, ``src/sse/handlers/chatHelpers.ts``):

- ``X-OmniRoute-Provider``: provider alias of the provider that served it
  (``getProviderAlias(providerId)``, for example ``cc`` for ``claude``).
- ``X-OmniRoute-Model``: the provider-local model id that served it.
- ``X-OmniRoute-Decision``: ``strategy=<name>; provider=<alias>; latency_ms=<n>``,
  where ``<name>`` is ``single`` for a non-combo request.
- ``X-OmniRoute-Selected-Connection-Id``: the provider connection (account) used.

``check_served`` compares that identity with the route Verdict selected. Only an
exact provider and model match is ``verified``. A known difference is a
``mismatch``. Missing, malformed or conflicting headers, or a provider pair this
module cannot map, are ``unknown``: never a match. Opaque selections (``auto/*``,
``combo/*``) are ``opaque``: the served member is recorded but proves nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from verdict.admission import canonical_route_id, is_opaque

ServedStatus = Literal["verified", "mismatch", "unknown", "opaque"]

HEADER_PROVIDER = "x-omniroute-provider"
HEADER_MODEL = "x-omniroute-model"
HEADER_DECISION = "x-omniroute-decision"
HEADER_CONNECTION = "x-omniroute-selected-connection-id"
HEADER_REQUEST_ID = "x-omniroute-request-id"
HEADER_VERSION = "x-omniroute-version"
HEADER_FALLBACK_ATTEMPTS = "x-omniroute-fallback-attempts"
_IDENTITY_HEADERS = (HEADER_PROVIDER, HEADER_MODEL, HEADER_DECISION, HEADER_CONNECTION)

#: Provider id -> alias where they differ, from OmniRoute v3.8.53
#: ``src/shared/constants/providers/**/*.ts`` (``id`` and ``alias`` fields).
#: The response header carries the alias; a route prefix may be either form.
OMNIROUTE_PROVIDER_ALIASES: Mapping[str, str] = {
    "9router": "nr",
    "adapta-web": "adp-web",
    "adobe-firefly": "firefly",
    "agnes-cn": "agnescn",
    "aihorde": "horde",
    "aimlapi": "aiml",
    "alibaba": "ali",
    "alibaba-cn": "ali-cn",
    "amazon-q": "aq",
    "ant-ling": "ling",
    "anysearch-search": "anysearch",
    "api-airforce": "af",
    "arcee-ai": "arcee",
    "assemblyai": "aai",
    "auggie": "aug",
    "aws-polly": "polly",
    "azure-openai": "azure",
    "bailian-coding-plan": "bcp",
    "bazaarlink": "bzl",
    "black-forest-labs": "bfl",
    "blackbox": "bb",
    "blackbox-web": "bb-web",
    "bluesminds": "bm",
    "byteplus": "bpm",
    "chatgpt-web-codex": "cgpt-codex",
    "chatplayground": "cpl",
    "cheaperinference": "cinf",
    "claude": "cc",
    "claude-web": "cw",
    "cline": "cl",
    "clinepass": "cp",
    "cliproxyapi": "cpa",
    "cloudflare-ai": "cf",
    "cloudflare-playground": "cfp",
    "clova-studio": "clova",
    "codebuddy-cn": "cbcn",
    "codebuddy-intl": "cbai",
    "codex": "cx",
    "codex-app-server": "cxa",
    "command-code": "cmd",
    "conol-web": "cnl",
    "copilot-m365-web": "m365copilot",
    "copilot-web": "copilot",
    "cursor": "cu",
    "cursor-api": "cua",
    "deepgram": "dg",
    "deepseek": "ds",
    "deepseek-web": "ds-web",
    "devin-cli": "dv",
    "devin-cli-agentic": "dva",
    "dit": "dai",
    "docker-model-runner": "dmr",
    "doubao-web": "db",
    "duckduckgo-web": "ddgw",
    "elevenlabs": "el",
    "fal-ai": "fal",
    "featherless-ai": "featherless",
    "firecrawl": "fc",
    "freeaiapikey": "faik",
    "freemodel-dev": "fmd",
    "freetheai": "fta",
    "friendliai": "friendli",
    "g4f-gemini": "g4fgem",
    "g4f-groq": "g4fgroq",
    "g4f-nvidia": "g4fnv",
    "g4f-ollama": "g4foll",
    "g4f-pollinations": "g4fpol",
    "gemini-web": "gweb",
    "getgoapi": "ggo",
    "github": "gh",
    "gitlawb": "glb",
    "gitlawb-gmi": "glb-gmi",
    "glm-cn": "glmcn",
    "google-pse-search": "google-pse",
    "grok-cli": "gc",
    "grok-web": "gw",
    "haiper": "hp",
    "huggingface": "hf",
    "hyperagent": "ha",
    "hyperbolic": "hyp",
    "ideogram": "ideo",
    "inference-net": "inet",
    "inner-ai": "in-ai",
    "jina-ai": "jina",
    "jina-reader": "jr",
    "kilo-gateway": "kg",
    "kilocode": "kc",
    "kimi-coding": "kmc",
    "kimi-coding-apikey": "kmca",
    "kiro": "kr",
    "lambda-ai": "lambda",
    "laozhang": "lz",
    "leonardo": "leo",
    "linkup-search": "linkup",
    "llama-cpp": "llamacpp",
    "llm-kiwi": "llmkiwi",
    "lm-studio": "lmstudio",
    "lmarena": "lma",
    "longcat": "lc",
    "magnific": "freepik",
    "maxai": "mx",
    "meta-llama": "meta",
    "mixedbread": "mxbai",
    "modal": "mdl",
    "modelscope": "ms",
    "monsterapi": "monster",
    "muse-code": "mc",
    "muse-spark-web": "ms-web",
    "naga-ac": "naga",
    "nimble-search": "nimble",
    "nlpcloud": "nlpc",
    "notion-web": "nw",
    "notrack-web": "ntw",
    "nous-research": "nous",
    "ollama-cloud": "ollamacloud",
    "ollama-local": "ollama",
    "oneminai": "1min",
    "oobabooga": "ooba",
    "openadapter": "oad",
    "openference": "of",
    "openference-api": "ofa",
    "ovhcloud": "ovh",
    "parallel-search": "parallel",
    "perplexity": "pplx",
    "perplexity-agent": "pplx-agent",
    "perplexity-search": "pplx-search",
    "perplexity-web": "pplx-web",
    "piapi": "pi",
    "pioneer": "pn",
    "poe-web": "poe",
    "pollinations": "pol",
    "promptql": "pql",
    "qoder": "if",
    "qwen-cloud": "qwc",
    "qwen-cloud-token-plan": "qct",
    "rev-ai": "revai",
    "runwayml": "runway",
    "sambanova": "samba",
    "scaleway": "scw",
    "searchapi-search": "searchapi",
    "searxng-search": "searxng",
    "seekai": "ska",
    "soniox": "sx",
    "speechmatics": "sm",
    "stability-ai": "stability",
    "syntx": "stx",
    "t3-web": "t3chat",
    "tencent-aistudio-web": "tasw",
    "tinycms-web": "tcw",
    "tinyfish": "tf",
    "token-kiosk": "tk",
    "tokenrouter": "trk",
    "trae": "tr",
    "twinmind": "tm",
    "uc": "ucn",
    "uc-direct": "ucd",
    "uncloseai": "unc",
    "v0-vercel": "v0",
    "venice-web": "ven",
    "veoaifree-web": "veo-free",
    "vercel-ai-gateway": "vag",
    "vertex-partner": "vp",
    "volcengine-agent-plan": "veap",
    "volcengine-coding-plan": "vecp",
    "voyage-ai": "voyage",
    "workbuddy": "wb",
    "x-search": "x_search",
    "xai-oauth": "xao",
    "xiaomi-mimo": "mimo",
    "xiaomi-mimo-token-plan": "mimotp",
    "xquik-search": "xquik",
    "yuanbao-web": "ybw",
    "zai-web": "zw",
    "zcode": "zc",
    "zed": "zd",
    "zenmux": "zm",
    "zenmux-free": "zmf",
    "zylo-api": "zylo",
}
_ALIAS_TO_ID = {alias: provider for provider, alias in OMNIROUTE_PROVIDER_ALIASES.items()}

#: Catalog mirror prefixes OmniRoute strips before routing
#: (``open-sse/utils/noThinkingAlias.ts``, ``open-sse/utils/ccDiscoveryAliases.ts``).
_NO_THINK_PREFIX = "no-think/"
_CC_MIRROR_PREFIX = "claude/"


def _canonical_provider(name: str) -> str:
    lowered = name.strip().lower()
    return _ALIAS_TO_ID.get(lowered, lowered)


def _single_value(values: list[str] | None) -> str | None:
    """One clean header value, or None when absent, empty, repeated or malformed."""
    if not values:
        return None
    cleaned = {value.strip(" \t") for value in values}
    if len(cleaned) != 1:
        return None
    (value,) = cleaned
    if not value or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return None
    return value


def _parse_decision(value: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for part in value.split(";"):
        key, sep, item = part.partition("=")
        if sep:
            fields[key.strip().lower()] = item.strip()
    return fields


@dataclass(frozen=True)
class ServedIdentity:
    """What OmniRoute's response headers say served one request."""

    provider: str | None = None
    model: str | None = None
    connection_id: str | None = None
    decision_strategy: str | None = None
    decision_provider: str | None = None
    request_id: str | None = None
    version: str | None = None
    fallback_attempts: int | None = None
    conflicting: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "connection_id": self.connection_id,
            "decision_strategy": self.decision_strategy,
            "decision_provider": self.decision_provider,
            "request_id": self.request_id,
            "version": self.version,
            "fallback_attempts": self.fallback_attempts,
            "conflicting": list(self.conflicting),
        }


def served_identity_from_headers(headers: Any) -> ServedIdentity | None:
    """Read the serving identity from response headers.

    ``headers`` is any object with ``items()`` (``dict``, ``httpx.Headers``,
    ``http.client.HTTPMessage``); for ``httpx.Headers`` use ``multi_items`` so a
    repeated header is seen. Returns None when no identity header is present.
    """
    items = getattr(headers, "multi_items", None) or getattr(headers, "items", None)
    if not callable(items):
        return None
    values: dict[str, list[str]] = {}
    for key, value in items():
        values.setdefault(str(key).lower(), []).append(str(value))
    if not any(name in values for name in _IDENTITY_HEADERS):
        return None
    conflicting = tuple(
        name for name in _IDENTITY_HEADERS if name in values and _single_value(values[name]) is None
    )
    decision_raw = _single_value(values.get(HEADER_DECISION))
    decision = _parse_decision(decision_raw) if decision_raw else {}
    attempts_raw = _single_value(values.get(HEADER_FALLBACK_ATTEMPTS))
    return ServedIdentity(
        provider=_single_value(values.get(HEADER_PROVIDER)),
        model=_single_value(values.get(HEADER_MODEL)),
        connection_id=_single_value(values.get(HEADER_CONNECTION)),
        decision_strategy=decision.get("strategy") or None,
        decision_provider=decision.get("provider") or None,
        request_id=_single_value(values.get(HEADER_REQUEST_ID)),
        version=_single_value(values.get(HEADER_VERSION)),
        fallback_attempts=int(attempts_raw) if attempts_raw and attempts_raw.isdigit() else None,
        conflicting=conflicting,
    )


def _routed_target(selected_route: str) -> tuple[str, str] | ServedStatus:
    """The ``(provider, model)`` OmniRoute routes a selected id to.

    Returns ``"opaque"`` for an opaque alias and ``"unknown"`` for an id with no
    provider prefix (a custom alias whose target this module cannot see).
    """
    route = canonical_route_id(selected_route)
    if route.lower().startswith(_NO_THINK_PREFIX):
        route = route[len(_NO_THINK_PREFIX) :]
    if route.lower().startswith(_CC_MIRROR_PREFIX) and "/" in route[len(_CC_MIRROR_PREFIX) :]:
        route = route[len(_CC_MIRROR_PREFIX) :]
    if is_opaque(route):
        return "opaque"
    provider, sep, model = route.partition("/")
    if not sep or not provider or not model:
        return "unknown"
    return provider, model


@dataclass(frozen=True)
class ServedCheck:
    """Selected route compared with the served identity for one attempt."""

    status: ServedStatus
    selected_route: str
    served: ServedIdentity | None
    reasons: tuple[str, ...] = ()

    @property
    def served_route(self) -> str | None:
        """``<provider alias>/<model>`` as served, or None when either is unknown."""
        if self.served is None or not self.served.provider or not self.served.model:
            return None
        return f"{self.served.provider}/{self.served.model}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "selected_route": self.selected_route,
            "served_route": self.served_route,
            "served": None if self.served is None else self.served.to_dict(),
            "reasons": list(self.reasons),
        }


def check_served(selected_route: str, served: ServedIdentity | None) -> ServedCheck:
    """Classify one attempt: ``verified``, ``mismatch``, ``unknown`` or ``opaque``."""
    target = _routed_target(selected_route)
    if target == "opaque":
        return ServedCheck("opaque", selected_route, served, ("opaque_selection",))
    if target == "unknown":
        return ServedCheck("unknown", selected_route, served, ("unqualified_selection",))
    assert isinstance(target, tuple)
    if served is None:
        return ServedCheck("unknown", selected_route, None, ("no_served_identity_headers",))
    if served.conflicting:
        reasons = tuple(f"conflicting_{name}" for name in served.conflicting)
        return ServedCheck("unknown", selected_route, served, reasons)
    missing = tuple(
        f"missing_{name}"
        for name, value in (("provider", served.provider), ("model", served.model))
        if not value
    )
    if missing:
        return ServedCheck("unknown", selected_route, served, missing)
    assert served.provider is not None and served.model is not None
    want_provider, want_model = target
    mismatches: list[str] = []
    if _canonical_provider(served.provider) != _canonical_provider(want_provider):
        mismatches.append("provider_differs")
    if served.model not in (want_model, f"{want_provider}/{want_model}"):
        mismatches.append("model_differs")
    if served.decision_provider and _canonical_provider(
        served.decision_provider
    ) != _canonical_provider(served.provider):
        mismatches.append("decision_provider_differs")
    if served.decision_strategy and served.decision_strategy != "single":
        mismatches.append("combo_strategy_on_concrete_route")
    if mismatches:
        return ServedCheck("mismatch", selected_route, served, tuple(mismatches))
    return ServedCheck("verified", selected_route, served)


__all__ = [
    "OMNIROUTE_PROVIDER_ALIASES",
    "ServedCheck",
    "ServedIdentity",
    "ServedStatus",
    "check_served",
    "served_identity_from_headers",
]
