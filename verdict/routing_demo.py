"""Portfolio routing demo: 100 requests, cheaper-first savings vs expensive baseline."""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin

import httpx

from verdict.live_routing import (
    COST_RANK,
    ConcreteIdentity,
    LiveSurfaceBlocked,
    classify_identities,
    select_route,
    strip_secrets,
)
from verdict.live_routing_gateway import DEFAULT_GATEWAY, fetch_models, fetch_pricing_index
from verdict.routing_demo_capture import SCHEMA_VERSION, save_recorded_capture
from verdict.routing_demo_capture import load_recorded_capture as _load_recorded_capture
from verdict.routing_demo_mock import run_mock_comparison

REQUEST_COUNT = 100
BASELINE_DEFINITION = "costliest qualified kept identity per request"
EXECUTE_MAX_TOKENS = 96
EXECUTE_TIMEOUT_S = 12.0
MAX_EXECUTE_WORKERS = 8
MAX_UNIQUE_EXECUTES = 12
CHAT_MODALITY = "text"
_NON_CHAT_IDENTITY_MARKERS = (
    "audio",
    "clip",
    "embedding",
    "image",
    "lyria",
    "moderation",
    "rerank",
    "speech",
    "tts",
    "video",
    "vision-embedding",
    "whisper",
)

RequestClass = Literal["simple", "complex"]


@dataclass(frozen=True)
class DemoRequest:
    request_id: str
    request_class: RequestClass
    est_input_tokens: int
    est_output_tokens: int
    requires_tools: bool


def build_demo_requests(count: int = REQUEST_COUNT) -> list[DemoRequest]:
    if count != REQUEST_COUNT:
        raise ValueError(f"portfolio demo requires exactly {REQUEST_COUNT} requests")
    out: list[DemoRequest] = []
    for i in range(1, count + 1):
        complex_req = i % 5 == 0  # 20 complex, 80 simple
        out.append(
            DemoRequest(
                request_id=f"r{i:03d}",
                request_class="complex" if complex_req else "simple",
                est_input_tokens=800 if complex_req else 200,
                est_output_tokens=400 if complex_req else 80,
                requires_tools=complex_req,
            )
        )
    return out


def estimate_cost_usd(
    pricing: dict[str, dict[str, float]], identity_id: str, *, input_tokens: int, output_tokens: int
) -> float:
    quote = pricing.get(identity_id) or {}
    inp = float(quote.get("input") or quote.get("prompt") or 0.0)
    out = float(quote.get("output") or quote.get("completion") or 0.0)
    return (inp * input_tokens + out * output_tokens) / 1_000_000.0


def _is_chat_identity(identity: ConcreteIdentity) -> bool:
    if identity.modalities is None or CHAT_MODALITY not in identity.modalities:
        return False
    lowered = identity.identity_id.lower()
    return not any(marker in lowered for marker in _NON_CHAT_IDENTITY_MARKERS)


def _qualify_for_request(candidates: list[Any], request: DemoRequest) -> list[Any]:
    kept = [
        candidate
        for candidate in candidates
        if candidate.status == "kept"
        and candidate.identity is not None
        and _is_chat_identity(candidate.identity)
    ]
    if not request.requires_tools:
        return kept
    return [candidate for candidate in kept if candidate.identity.tools is True]


def _baseline_candidate(qualified: list[Any]) -> Any:
    def _key(candidate: Any) -> tuple[int, str]:
        identity = candidate.identity
        assert identity is not None and identity.cost_class is not None
        return (COST_RANK[identity.cost_class], identity.identity_id)

    return max(qualified, key=_key)


def _rationale(chosen: Any, baseline: Any, request: DemoRequest) -> str:
    cost = chosen.identity.cost_class if chosen.identity else "unknown"
    base = baseline.identity.cost_class if baseline.identity else "unknown"
    cls = request.request_class
    if chosen.ref == baseline.ref:
        return f"{cls}: only qualified class available was {cost}"
    return (
        f"{cls}: cheaper-first chose {cost} `{chosen.ref}` "
        f"instead of baseline {base} `{baseline.ref}`"
    )


def assert_cheaper_first(qualified: list[Any], chosen: Any) -> None:
    if chosen.identity is None or chosen.identity.cost_class is None:
        raise RuntimeError("chosen identity missing cost class")
    cheaper = [
        c
        for c in qualified
        if c.identity is not None and c.identity.cost_class in {"local", "free", "cheaper"}
    ]
    if chosen.identity.cost_class == "paid" and cheaper:
        raise RuntimeError("paid selected while cheaper qualified kept exists")


def load_recorded_capture(
    path: Path,
) -> tuple[list[ConcreteIdentity], dict[str, dict[str, float]], datetime, str]:
    return _load_recorded_capture(path, default_gateway=DEFAULT_GATEWAY)


def _bounded_execute(base_url: str, identity_id: str) -> tuple[str, bool, float]:
    url = urljoin(base_url.rstrip("/") + "/", "chat/completions")
    started = time.perf_counter()
    try:
        response = httpx.post(
            url,
            json={
                "model": identity_id,
                "messages": [
                    {"role": "user", "content": 'Reply with only this JSON: {"routing_demo":"ok"}'}
                ],
                "max_tokens": EXECUTE_MAX_TOKENS,
            },
            timeout=EXECUTE_TIMEOUT_S,
        )
        latency_ms = (time.perf_counter() - started) * 1000.0
        if response.status_code >= 400:
            return identity_id, False, latency_ms
        payload = response.json()
        choices = payload.get("choices") or []
        if not choices:
            return identity_id, False, latency_ms
        content = str(((choices[0] or {}).get("message") or {}).get("content") or "")
        try:
            passed = json.loads(content.strip()) == {"routing_demo": "ok"}
        except json.JSONDecodeError:
            passed = False
        return identity_id, passed, latency_ms
    except (httpx.HTTPError, ValueError, json.JSONDecodeError):
        latency_ms = (time.perf_counter() - started) * 1000.0
        return identity_id, False, latency_ms


def _run_executes(base_url: str, identity_ids: list[str]) -> dict[str, tuple[bool, float]]:
    results: dict[str, tuple[bool, float]] = {}
    if not identity_ids:
        return results
    with ThreadPoolExecutor(max_workers=MAX_EXECUTE_WORKERS) as pool:
        futures = [pool.submit(_bounded_execute, base_url, mid) for mid in identity_ids]
        for fut in as_completed(futures):
            mid, ok, latency = fut.result()
            results[mid] = (ok, latency)
    return results


def run_routing_demo(
    *,
    gateway_base_url: str = DEFAULT_GATEWAY,
    recorded_path: Path | None = None,
    save_capture_path: Path | None = None,
    execute: bool = True,
    mock: bool = False,
) -> dict[str, Any]:
    started = time.perf_counter()
    if mock:
        summary = run_mock_comparison(build_demo_requests())
        summary["schema_version"] = SCHEMA_VERSION
        summary["wall_clock_ms"] = int((time.perf_counter() - started) * 1000)
        return strip_secrets(summary)
    mode: Literal["live", "recorded"] = "recorded" if recorded_path else "live"
    try:
        if recorded_path is not None:
            identities, pricing, captured_at, gateway_base_url = load_recorded_capture(
                recorded_path
            )
        else:
            identities, captured_at = fetch_models(gateway_base_url)
            pricing = fetch_pricing_index(gateway_base_url)
            # Attach pricing onto missing rows for cost_class completeness where needed
            for identity in identities:
                if identity.identity_id not in pricing and identity.cost_class == "free":
                    pricing[identity.identity_id] = {"input": 0.0, "output": 0.0}
            if save_capture_path is not None:
                kept_ids = {
                    c.identity.identity_id
                    for c in classify_identities(identities)
                    if c.status == "kept" and c.identity is not None
                }
                kept_identities = [i for i in identities if i.identity_id in kept_ids]
                kept_pricing = {
                    i.identity_id: pricing[i.identity_id]
                    for i in kept_identities
                    if i.identity_id in pricing
                }
                save_recorded_capture(
                    save_capture_path,
                    gateway_base_url=gateway_base_url,
                    captured_at=captured_at,
                    identities=kept_identities or identities[:50],
                    pricing=kept_pricing or pricing,
                )
    except LiveSurfaceBlocked as exc:
        return strip_secrets(
            {
                "schema_version": SCHEMA_VERSION,
                "mode": mode,
                "status": "blocked",
                "reason": getattr(exc, "code", "live_surface_blocked"),
                "request_count": 0,
                "wall_clock_ms": int((time.perf_counter() - started) * 1000),
            }
        )
    except (httpx.HTTPError, OSError, ValueError, json.JSONDecodeError):
        return strip_secrets(
            {
                "schema_version": SCHEMA_VERSION,
                "mode": mode,
                "status": "blocked",
                "reason": "live_surface_blocked",
                "request_count": 0,
                "wall_clock_ms": int((time.perf_counter() - started) * 1000),
            }
        )

    candidates = classify_identities(identities)
    requests = build_demo_requests()
    decisions: list[dict[str, Any]] = []
    chosen_ids: list[str] = []

    for request in requests:
        qualified = _qualify_for_request(candidates, request)
        if not qualified:
            return strip_secrets(
                {
                    "schema_version": SCHEMA_VERSION,
                    "mode": mode,
                    "status": "blocked",
                    "reason": "no_qualified_candidate",
                    "message": f"no qualified candidates for {request.request_id}",
                    "request_count": 0,
                    "wall_clock_ms": int((time.perf_counter() - started) * 1000),
                }
            )
        selection = select_route(qualified)
        chosen = selection.chosen
        baseline = _baseline_candidate(qualified)
        assert_cheaper_first(qualified, chosen)
        assert chosen.identity is not None and baseline.identity is not None
        routed_cost = estimate_cost_usd(
            pricing,
            chosen.identity.identity_id,
            input_tokens=request.est_input_tokens,
            output_tokens=request.est_output_tokens,
        )
        baseline_cost = estimate_cost_usd(
            pricing,
            baseline.identity.identity_id,
            input_tokens=request.est_input_tokens,
            output_tokens=request.est_output_tokens,
        )
        decisions.append(
            {
                "request_id": request.request_id,
                "class": request.request_class,
                "chosen_id": chosen.identity.identity_id,
                "baseline_id": baseline.identity.identity_id,
                "rationale": _rationale(chosen, baseline, request),
                "routed_cost_usd": round(routed_cost, 8),
                "baseline_cost_usd": round(baseline_cost, 8),
                "latency_ms": None,
                "success": None,
            }
        )
        if chosen.identity.identity_id not in chosen_ids:
            chosen_ids.append(chosen.identity.identity_id)

    execute_results: dict[str, tuple[bool, float]] = {}
    if execute and mode == "live":
        execute_results = _run_executes(gateway_base_url, chosen_ids[:MAX_UNIQUE_EXECUTES])
    elif execute and mode == "recorded":
        # Recorded mode may include prior execute metrics; default none.
        execute_results = {}

    successes = 0
    attempts = 0
    latency_sum = 0.0
    for decision in decisions:
        mid = str(decision["chosen_id"])
        if mid in execute_results:
            ok, latency = execute_results[mid]
            decision["success"] = ok
            decision["latency_ms"] = round(latency, 2)
            attempts += 1
            latency_sum += latency
            if ok:
                successes += 1

    if execute and mode == "live" and (attempts == 0 or successes != attempts):
        return strip_secrets(
            {
                "schema_version": SCHEMA_VERSION,
                "mode": mode,
                "status": "blocked",
                "reason": "quality_check_failed",
                "message": (
                    f"named chat check passed for {successes}/{attempts} "
                    "selected identities; savings are not claimed"
                ),
                "request_count": 0,
                "execute_attempts": attempts,
                "wall_clock_ms": int((time.perf_counter() - started) * 1000),
            }
        )

    routed_total = sum(float(d["routed_cost_usd"]) for d in decisions)
    baseline_total = sum(float(d["baseline_cost_usd"]) for d in decisions)
    savings = baseline_total - routed_total
    savings_pct = (savings / baseline_total) if baseline_total > 0 else 0.0
    wall_ms = int((time.perf_counter() - started) * 1000)

    summary = {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "status": "completed",
        "request_count": len(decisions),
        "routed_cost_usd": round(routed_total, 6),
        "baseline_cost_usd": round(baseline_total, 6),
        "savings_usd": round(savings, 6),
        "savings_pct": round(savings_pct, 4),
        "success_rate": round((successes / attempts), 4) if attempts else 0.0,
        "avg_latency_ms": round(latency_sum / attempts, 2) if attempts else 0.0,
        "execute_attempts": attempts,
        "wall_clock_ms": wall_ms,
        "catalog_captured_at": captured_at.isoformat(),
        "gateway_base_url": gateway_base_url,
        "baseline_definition": BASELINE_DEFINITION,
        "decisions": decisions,
    }
    return strip_secrets(summary)


def format_human(summary: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("Verdict routing demo — cost vs quality")
    lines.append(f"status: {summary.get('status')}  mode: {summary.get('mode')}")
    if summary.get("status") == "blocked":
        lines.append(f"reason: {summary.get('reason')} — {summary.get('message')}")
        lines.append("Live savings not claimed.")
        return "\n".join(lines)
    lines.append(f"requests: {summary.get('request_count')}")
    lines.append(
        f"routed: ${summary.get('routed_cost_usd'):.6f}  "
        f"baseline: ${summary.get('baseline_cost_usd'):.6f}  "
        f"savings: ${summary.get('savings_usd'):.6f} "
        f"({100 * float(summary.get('savings_pct') or 0):.1f}%)"
    )
    lines.append(
        f"success_rate: {100 * float(summary.get('success_rate') or 0):.1f}%  "
        f"avg_latency_ms: {summary.get('avg_latency_ms')}  "
        f"wall_clock_ms: {summary.get('wall_clock_ms')}"
    )
    if summary.get("mode") == "mock":
        lines.append("cost comparison (USD):")
        for model_id, cost in dict(summary.get("cost_comparison_usd") or {}).items():
            lines.append(f"  {model_id:<20} ${float(cost):.6f}")
        ranker = dict(summary.get("adaptive_ranker") or {})
        lines.append(
            f"adaptive_ranker: {ranker.get('mode')} shadow={ranker.get('shadow')} "
            f"eligible={','.join(ranker.get('ranked_ids') or [])}"
        )
        lines.append(f"pricing_source: {summary.get('pricing_source')}")
    else:
        lines.append(f"baseline: {summary.get('baseline_definition')}")
        lines.append(f"catalog_captured_at: {summary.get('catalog_captured_at')}")
    lines.append("")
    lines.append("sample decisions:")
    for decision in list(summary.get("decisions") or [])[:10]:
        lines.append(
            f"  {decision['request_id']} [{decision['class']}] "
            f"{decision['chosen_id']} — {decision['rationale']}"
        )
    if int(summary.get("request_count") or 0) > 10:
        lines.append(f"  … ({int(summary['request_count']) - 10} more)")
    return "\n".join(lines)


def _public_summary(summary: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "adaptive_ranker",
        "avg_latency_ms",
        "baseline_cost_usd",
        "baseline_definition",
        "catalog_captured_at",
        "cost_comparison_usd",
        "decisions",
        "execute_attempts",
        "mode",
        "pricing_observed_at",
        "pricing_source",
        "quality_metric",
        "reason",
        "request_count",
        "routed_cost_usd",
        "savings_pct",
        "savings_usd",
        "schema_version",
        "status",
        "success_rate",
        "wall_clock_ms",
    }
    return {key: summary[key] for key in sorted(allowed & summary.keys())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verdict 100-request routing demo")
    parser.add_argument("--gateway", default=DEFAULT_GATEWAY)
    parser.add_argument("--recorded", type=Path, default=None)
    parser.add_argument(
        "--save-capture",
        type=Path,
        default=None,
        help="When running live, write a labeled recorded capture JSON",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Run deterministic no-spend Opus/Sonnet/Haiku/auto comparison",
    )
    parser.add_argument("--no-execute", action="store_true")
    args = parser.parse_args(argv)
    summary = run_routing_demo(
        gateway_base_url=args.gateway,
        recorded_path=args.recorded,
        save_capture_path=args.save_capture,
        execute=not args.no_execute,
        mock=args.mock,
    )
    if args.json:
        print(json.dumps(_public_summary(summary), indent=2, sort_keys=True))
    else:
        print(format_human(_public_summary(summary)))
    return 0 if summary.get("status") == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
