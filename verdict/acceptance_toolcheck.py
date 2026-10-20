"""Independent 2-turn tool check for the live acceptance job.

This deliberately does not reuse the prober (``prove_at_rest``): it is the
yardstick the prober's eligibility list is measured against. Turn 1 must call
``read_file`` on ``notes.txt``; turn 2, given the file content, must call
``write_file`` on ``out.txt`` with that content in upper case.
"""

from __future__ import annotations

import json
import math
import random
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence, Set
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from verdict.http_safety import open_no_redirect

#: Replies that say the account or pool is blocked, not that the route is wrong.
ACCOUNT_LEVEL_STATUS = frozenset({401, 402, 403, 429})
PRECISION_MIN = 0.95
RECALL_MIN = 0.90
LISTED_SAMPLE = 8
UNLISTED_SAMPLE = 12

#: ``/v1/models`` ``type`` values of models that cannot hold a tool conversation.
_NON_CHAT_TYPES = frozenset(
    {"image", "embedding", "audio", "tts", "transcription", "rerank", "moderation"}
)
#: Unambiguous id markers, used only for routes with no ``/v1/models`` row.
_NON_CHAT_ID = re.compile(
    r"embed|rerank|tts|whisper|transcri|dall-e|stable-diffusion|sdxl|flux", re.IGNORECASE
)

_NOTE = "verdict acceptance probe 7"
_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": props, "required": list(props)},
        },
    }
    for name, description, props in (
        ("read_file", "Read a text file.", {"path": {"type": "string"}}),
        (
            "write_file",
            "Write text to a file.",
            {"path": {"type": "string"}, "content": {"type": "string"}},
        ),
    )
]
_PROMPT = (
    "Use the tools. First read notes.txt. Then write its exact content, converted "
    "to UPPER CASE, to out.txt."
)

#: ``post(path, body) -> (http_status, json_or_None)``; tests pass a fake gateway.
Post = Callable[[str, dict[str, Any]], tuple[int, Any]]


@dataclass(frozen=True)
class ToolCheck:
    route: str
    outcome: str  # pass | fail | account
    detail: str


def gateway_post(base_url: str, api_key: str | None, *, timeout: float = 90) -> Post:
    def post(path: str, body: dict[str, Any]) -> tuple[int, Any]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(
            base_url.rstrip("/") + path, data=json.dumps(body).encode(), headers=headers
        )
        try:
            with open_no_redirect(request, timeout=timeout) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, None

    return post


def _tool_call(reply: Any) -> tuple[dict[str, Any], str, dict[str, Any]] | None:
    try:
        message = reply["choices"][0]["message"]
        call = message["tool_calls"][0]
        args = json.loads(call["function"]["arguments"] or "{}")
        return message, str(call["function"]["name"]), args if isinstance(args, dict) else {}
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def check_route(route: str, post: Post) -> ToolCheck:
    """Run the 2-turn check on one route. Never raises."""
    messages: list[dict[str, Any]] = [{"role": "user", "content": _PROMPT}]
    body: dict[str, Any] = {
        "model": route,
        "messages": messages,
        "tools": _TOOLS,
        "temperature": 0,
        "max_tokens": 2048,
        "stream": False,
    }
    try:
        for turn in (1, 2):
            status, reply = post("/v1/chat/completions", {**body, "messages": messages})
            if status in ACCOUNT_LEVEL_STATUS:
                return ToolCheck(route, "account", f"turn{turn}: HTTP {status}")
            if status != 200:
                return ToolCheck(route, "fail", f"turn{turn}: HTTP {status}")
            found = _tool_call(reply)
            want = "read_file" if turn == 1 else "write_file"
            if found is None or found[1] != want:
                return ToolCheck(route, "fail", f"turn{turn}: no {want} call")
            message, _name, args = found
            if turn == 1:
                if not str(args.get("path", "")).endswith("notes.txt"):
                    return ToolCheck(route, "fail", "turn1: wrong read_file path")
                call_id = message["tool_calls"][0].get("id") or "call_0"
                messages += [
                    {
                        "role": "assistant",
                        "content": message.get("content") or "",
                        "tool_calls": message["tool_calls"],
                    },
                    {"role": "tool", "tool_call_id": call_id, "content": _NOTE},
                ]
            elif (
                not str(args.get("path", "")).endswith("out.txt")
                or str(args.get("content", "")).strip() != _NOTE.upper()
            ):
                return ToolCheck(route, "fail", "turn2: wrong write_file arguments")
    except Exception as exc:
        return ToolCheck(route, "fail", f"{type(exc).__name__}")
    return ToolCheck(route, "pass", "read_file then write_file")


def _live_ids(synced: Mapping[str, Any], catalog: Mapping[str, Any] | None = None) -> set[str]:
    """Union of the synced catalog and ``/v1/models`` served ids.

    ``/api/synced-available-models`` omits some active providers (observed:
    ``ollama-cloud``, which ``/v1/models`` and ``/api/providers`` both confirm
    is live and serving). A route is unserved only when it is in neither.
    """
    ids = {
        f"{provider}/{row.get('id')}"
        for provider, rows in synced.items()
        if isinstance(rows, list)
        for row in rows
        if isinstance(row, Mapping)
    }
    if isinstance(catalog, Mapping):
        rows = catalog.get("data", [])
        if isinstance(rows, list):
            ids |= {str(row["id"]) for row in rows if isinstance(row, Mapping) and row.get("id")}
    return ids


def _catalog_rows(catalog: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    rows = catalog.get("data", []) if isinstance(catalog, Mapping) else []
    if not isinstance(rows, list):
        return {}
    return {str(r["id"]): r for r in rows if isinstance(r, Mapping) and r.get("id")}


def _non_chat_reason(route: str, row: Mapping[str, Any] | None) -> str | None:
    """Why ``route`` is not a chat candidate, from its catalog row, else its id."""
    if row is None:
        return "id_non_chat" if _NON_CHAT_ID.search(route.split("/", 1)[-1]) else None
    outputs = row.get("output_modalities")
    caps = row.get("capabilities")
    if (
        (isinstance(outputs, list) and "text" not in outputs)
        or str(row.get("type", "")).lower() in _NON_CHAT_TYPES
        or (isinstance(caps, Mapping) and caps.get("tool_calling") is False)
    ):
        return "catalog_non_chat"
    return None


def cooling_providers(connections: Sequence[Mapping[str, Any]], now: datetime) -> set[str]:
    """Providers whose every connection has an active ``rateLimitedUntil``."""
    by_provider: dict[str, list[bool]] = {}
    for row in connections:
        until = row.get("rateLimitedUntil")
        try:
            cooling = (
                bool(until) and datetime.fromisoformat(str(until).replace("Z", "+00:00")) > now
            )
        except ValueError:
            cooling = False
        by_provider.setdefault(str(row.get("provider", "")), []).append(cooling)
    return {name for name, flags in by_provider.items() if flags and all(flags)}


def inactive_providers(connections: Sequence[Mapping[str, Any]]) -> set[str]:
    """Providers whose every connection is switched off or failed its gateway test."""
    by_provider: dict[str, list[bool]] = {}
    for row in connections:
        off = row.get("isActive") is False or row.get("testStatus", "active") != "active"
        by_provider.setdefault(str(row.get("provider", "")), []).append(off)
    return {name for name, flags in by_provider.items() if flags and all(flags)}


def wilson_interval(passes: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson score interval for ``passes`` out of ``n``; ``None`` when ``n`` is 0."""
    if n <= 0:
        return None
    p = passes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def measure(
    listed: Sequence[Mapping[str, Any]],
    verdicts: Sequence[Mapping[str, Any]],
    *,
    synced: Mapping[str, Any],
    catalog: Mapping[str, Any] | None = None,
    cooling: set[str],
    inactive: Set[str] = frozenset(),
    post: Post,
    seed: str,
    concurrency: int = 6,
) -> dict[str, Any]:
    """Precision of the eligible FREE list and estimated recall of the rest.

    ``listed`` are Verdict's eligible FREE tool routes. Listed routes that are
    not served or sit in a cooling pool fail without a probe (A3 says they must
    not be listed). "Served" is the union of the synced catalog and
    ``/v1/models`` (``catalog``): the synced endpoint can omit an active
    provider that ``/v1/models`` still lists as live (see ``_live_ids``).
    Routes answering 401/402/403/429 are removed from both ratios.
    The unlisted population leaves out routes of ``inactive`` providers and
    non-chat models: by the ``/v1/models`` row when there is one (output
    modalities, type, ``tool_calling``), else by unambiguous id markers. So the
    recall sample measures real candidates. Unmeasurable ratios are ``None``.
    """
    live = _live_ids(synced, catalog)
    listed_ids = {str(row["route_id"]) for row in listed}
    cooling_hits = {
        str(row["route_id"]) for row in listed if str(row.get("provider", "")) in cooling
    }
    unserved = listed_ids - live
    probe_listed = sorted(listed_ids - unserved - cooling_hits)
    candidates = [
        v
        for v in verdicts
        if v.get("capacity_class") == "free"
        and str(v["route_id"]) in live
        and str(v["route_id"]) not in listed_ids
        and str(v.get("provider", "")) not in cooling
        and v.get("reason") not in {"effort_duplicate", "admission:missing_capability:tools"}
    ]
    rows = _catalog_rows(catalog)
    excluded = {"inactive_provider": 0, "catalog_non_chat": 0, "id_non_chat": 0}
    kept: list[str] = []
    for v in candidates:
        route = str(v["route_id"])
        reason = (
            "inactive_provider"
            if str(v.get("provider", "")) in inactive
            else _non_chat_reason(route, rows.get(route))
        )
        if reason:
            excluded[reason] += 1
        else:
            kept.append(route)
    population = sorted(kept)
    rng = random.Random(seed)
    sample_listed = rng.sample(probe_listed, min(LISTED_SAMPLE, len(probe_listed)))
    sample_unlisted = rng.sample(population, min(UNLISTED_SAMPLE, len(population)))
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        results = list(pool.map(lambda r: check_route(r, post), sample_listed + sample_unlisted))
    on_listed = results[: len(sample_listed)]
    on_unlisted = results[len(sample_listed) :]
    listed_measured = [r for r in on_listed if r.outcome != "account"]
    listed_pass = sum(r.outcome == "pass" for r in listed_measured)
    # Listed routes that are unserved or cooling are false without a probe.
    blocked = len(unserved | cooling_hits)
    precision: float | None = None
    if listed_ids and (listed_measured or blocked):
        rate = listed_pass / len(listed_measured) if listed_measured else 0.0
        precision = rate * (len(listed_ids) - blocked) / len(listed_ids)
    # A3 removes routes in account-blocked pools from the recall population:
    # missed = pass rate among measured samples x the unblocked share of the
    # population. A sample that is all account-blocked (for example a 429 burst)
    # measures nothing, so recall stays unmeasured (a FAIL), never a free 100%.
    measured_unlisted = [r for r in on_unlisted if r.outcome != "account"]
    unlisted_pass = sum(r.outcome == "pass" for r in measured_unlisted)
    missed: float | None = 0.0 if not population else None
    if measured_unlisted:
        unblocked = len(population) * len(measured_unlisted) / len(on_unlisted)
        missed = unlisted_pass / len(measured_unlisted) * unblocked
    recall: float | None = None
    if missed is not None:
        # Nothing listed finds nothing: recall is 0 when the sample shows passes.
        found = precision * len(listed_ids) if precision is not None else 0.0
        recall = found / (found + missed) if found + missed else None
    return {
        "listed": len(listed_ids),
        "listed_unserved": sorted(unserved),
        "listed_in_cooling_pool": sorted(cooling_hits),
        "unlisted_population": len(population),
        "population_before": len(candidates),
        "population_after": len(population),
        "population_excluded": excluded,
        "unlisted_measured": len(measured_unlisted),
        "unlisted_passes": unlisted_pass,
        "unlisted_wilson": wilson_interval(unlisted_pass, len(measured_unlisted)),
        "precision": precision,
        "recall_estimate": recall,
        "estimated_missed": missed,
        "precision_min": PRECISION_MIN,
        "recall_min": RECALL_MIN,
        "checks": [asdict(r) | {"listed": i < len(sample_listed)} for i, r in enumerate(results)],
    }
