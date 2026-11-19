#!/usr/bin/env python3
"""Paired live usage bench: does context-first plus free-first use fewer tokens?

Each task in ``benchmarks/fixtures/live_savings/tasks.json`` runs in two arms:

- **baseline**: the task prompt only, no context pack, sent to the frontier model
  named by ``--baseline-model`` (required, no default).
- **verdict**: the route Verdict chooses on the live eligibility ladder, built the
  way ``verdict serve`` builds it (``verdict.api._build_intelligence`` plus the
  ``EligibilityGate`` from ``verdict.api._build_availability_cache``). The arm
  sends the decision's context pack plus the task prompt. Only free and
  subscription capacity is used, never an opaque ``auto/*`` or ``combo/*`` route.
  A deny, or a route outside that capacity, is a Verdict-arm miss; there is no
  fallback to the baseline model.

Every call (routing confirm probes, the task call, its retry) is recorded against
the arm that caused it, with the served model and request id from the
``X-OmniRoute-*`` response headers, provider-reported usage including cached input
tokens, and the route's capacity class. Acceptance is by running the task's own
test file. Tokens are the headline metric; dollars appear only as an optional
secondary line, labelled "list-price equivalent, not an invoice".

The bench never edits tasks, tests or acceptance criteria: their SHA-256 is taken
before and after the run, and a change blocks every claim.

Opt-in only: refuses to run without ``VERDICT_LIVE_SMOKE=1``. Usage::

    VERDICT_LIVE_SMOKE=1 OMNIROUTE_BASE_URL=http://127.0.0.1:20128 \\
        uv run python scripts/live_savings_bench.py \\
        --baseline-model <frontier route> --out-dir /tmp/<run> [--repeats 1]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

REPO_ROOT = Path(__file__).resolve().parent.parent
TASKS_JSON = REPO_ROOT / "benchmarks" / "fixtures" / "live_savings" / "tasks.json"
EXECUTE_TIMEOUT = 120.0
MAX_EXECUTE_ATTEMPTS = 2
ALLOWED_CAPACITY = frozenset({"free", "subscription"})
NO_ELIGIBLE_TARGET = "no_eligible_target"
LIST_PRICE_LABEL = "list-price equivalent, not an invoice"
ARMS = ("baseline", "verdict")

# (status, headers, body) for one chat completion.
Exchange = tuple[int, Mapping[str, str], Mapping[str, Any]]
Transport = Callable[[str, list[dict[str, str]], float], Exchange]
Grader = Callable[[Mapping[str, Any], str], bool]


# ── Records ────────────────────────────────────────────────────────────
@dataclass
class Call:
    """One model call, attributed to the arm that caused it."""

    arm: str
    kind: str  # probe | execute | retry
    requested_model: str
    served_model: str
    request_id: str | None
    capacity: str
    status: int
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    error: str | None = None


@dataclass
class RouteResult:
    """What the Verdict router decided for one task."""

    model: str | None
    decision: str
    reason: str
    context_pack_prompt: str | None
    capacity: str
    probe_calls: list[Call] = field(default_factory=list)
    receipt: dict[str, Any] = field(default_factory=dict)


@dataclass
class ArmRun:
    """One arm of one (task, repeat) pair."""

    task_id: str
    task_group: str
    arm: str
    repeat: int
    requested_model: str | None
    accepted: bool
    calls: list[Call] = field(default_factory=list)
    miss_reason: str | None = None
    route: dict[str, Any] | None = None

    @property
    def served_model(self) -> str | None:
        executed = [c for c in self.calls if c.kind in ("execute", "retry") and c.status == 200]
        return executed[-1].served_model if executed else None


@dataclass
class PairsResult:
    runs: list[ArmRun]
    stopped_early: bool


class Router(Protocol):
    def route(self, prompt: str, criticality: str) -> RouteResult: ...

    def capacity_of(self, model: str) -> str: ...


# ── Helpers ────────────────────────────────────────────────────────────
def _refuse_without_opt_in() -> None:
    if os.environ.get("VERDICT_LIVE_SMOKE") != "1":
        print("refusing: set VERDICT_LIVE_SMOKE=1 (this spends real capacity)", file=sys.stderr)
        raise SystemExit(2)


def _extract_code(text: str) -> str:
    """Extract the FIRST Python code block from a model response."""
    m = re.search(r"```python\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1)
    m = re.search(r"```\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1)
    return text


def _grade_solution(task: Mapping[str, Any], code: str) -> bool:
    """Run the task's test file against the solution in a temp sandbox."""
    test_file = TASKS_JSON.parent / task["test_file"]
    if not test_file.exists():
        return False
    with tempfile.TemporaryDirectory() as sandbox:
        sandbox_path = Path(sandbox)
        (sandbox_path / "solution.py").write_text(code, encoding="utf-8")
        (sandbox_path / task["test_file"]).write_text(
            test_file.read_text(encoding="utf-8"), encoding="utf-8"
        )
        result = subprocess.run(
            [sys.executable, "-m", "pytest", str(sandbox_path / task["test_file"]), "-q"],
            capture_output=True,
            text=True,
            cwd=str(sandbox_path),
            timeout=30,
        )
        return result.returncode == 0


def _is_opaque(model: str) -> bool:
    from verdict.admission import is_opaque

    return is_opaque(model)


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def usage_of(body: Mapping[str, Any]) -> tuple[int | None, int | None, int | None]:
    """Provider-reported (input, cached_input, output); ``None`` when not reported."""
    usage = body.get("usage") if isinstance(body, Mapping) else None
    if not isinstance(usage, Mapping):
        return None, None, None
    details = usage.get("prompt_tokens_details")
    cached = _int_or_none(details.get("cached_tokens")) if isinstance(details, Mapping) else None
    if cached is None:
        cached = _int_or_none(usage.get("cache_read_input_tokens"))
    return (
        _int_or_none(usage.get("prompt_tokens")),
        cached,
        _int_or_none(usage.get("completion_tokens")),
    )


def _header(headers: Mapping[str, str], *names: str) -> str | None:
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value:
            return value
    return None


def _content(body: Mapping[str, Any]) -> str:
    choices = body.get("choices") or []
    if not choices or not isinstance(choices[0], Mapping):
        return ""
    message = choices[0].get("message") or {}
    return str(message.get("content") or "") if isinstance(message, Mapping) else ""


def _criticality(task: Mapping[str, Any]) -> str:
    return "low" if "simple" in str(task.get("task_class", "")) else "medium"


def _execute(
    *,
    arm: str,
    model: str,
    messages: list[dict[str, str]],
    capacity: str,
    transport: Transport,
    router: Router,
) -> tuple[list[Call], Mapping[str, Any] | None]:
    """Send the task; retry once on failure. Every attempt is a recorded call."""
    calls: list[Call] = []
    for attempt in range(MAX_EXECUTE_ATTEMPTS):
        kind = "execute" if attempt == 0 else "retry"
        try:
            status, headers, body = transport(model, messages, EXECUTE_TIMEOUT)
            error = None if status == 200 else f"http {status}"
        except Exception as exc:
            status, headers, body, error = 0, {}, {}, f"{type(exc).__name__}: {exc}"
        served = (
            _header(headers, "x-omniroute-model", "x-omniroute-completed-with")
            or str(body.get("model") or "")
            or model
        )
        in_tok, cached_tok, out_tok = usage_of(body)
        calls.append(
            Call(
                arm=arm,
                kind=kind,
                requested_model=model,
                served_model=served,
                request_id=_header(headers, "x-omniroute-request-id", "x-request-id"),
                # A gateway fallback can serve another route; classify what was served.
                capacity=capacity if served == model else router.capacity_of(served),
                status=status,
                input_tokens=in_tok,
                cached_input_tokens=cached_tok,
                output_tokens=out_tok,
                error=error,
            )
        )
        if status == 200:
            return calls, body
    return calls, None


def _run_baseline(
    task: Mapping[str, Any],
    repeat: int,
    *,
    model: str,
    router: Router,
    transport: Transport,
    grader: Grader,
) -> ArmRun:
    run = ArmRun(
        str(task["id"]), str(task.get("task_group", "standalone")), "baseline", repeat, model, False
    )
    capacity = router.capacity_of(model)
    if capacity not in ALLOWED_CAPACITY:
        run.miss_reason = f"capacity {capacity} not allowed (free,subscription only)"
        return run
    calls, body = _execute(
        arm="baseline",
        model=model,
        messages=[{"role": "user", "content": str(task["prompt"])}],
        capacity=capacity,
        transport=transport,
        router=router,
    )
    run.calls = calls
    if body is None:
        run.miss_reason = "no successful response"
        return run
    run.accepted = grader(task, _extract_code(_content(body)))
    return run


def _run_verdict(
    task: Mapping[str, Any], repeat: int, *, router: Router, transport: Transport, grader: Grader
) -> ArmRun:
    prompt = str(task["prompt"])
    route = router.route(prompt, _criticality(task))
    run = ArmRun(
        str(task["id"]),
        str(task.get("task_group", "standalone")),
        "verdict",
        repeat,
        route.model,
        False,
        calls=list(route.probe_calls),
        route={"decision": route.decision, "reason": route.reason, **route.receipt},
    )
    model = route.model
    if route.decision == "denied" or not model or model == NO_ELIGIBLE_TARGET:
        run.miss_reason = f"denied: {route.reason}"
        return run
    if _is_opaque(model):
        run.miss_reason = f"opaque route {model} is never a candidate"
        return run
    if route.capacity not in ALLOWED_CAPACITY:
        run.miss_reason = f"capacity {route.capacity} not allowed (free,subscription only)"
        return run
    messages = [{"role": "user", "content": prompt}]
    if route.context_pack_prompt:
        messages.insert(0, {"role": "system", "content": route.context_pack_prompt})
    calls, body = _execute(
        arm="verdict",
        model=model,
        messages=messages,
        capacity=route.capacity,
        transport=transport,
        router=router,
    )
    run.calls.extend(calls)
    if body is None:
        run.miss_reason = "no successful response"
        return run
    run.accepted = grader(task, _extract_code(_content(body)))
    return run


def run_pairs(
    tasks: Sequence[Mapping[str, Any]],
    *,
    router: Router,
    transport: Transport,
    baseline_model: str,
    repeats: int,
    grader: Grader = _grade_solution,
    max_wall_seconds: float,
    clock: Callable[[], float] = time.monotonic,
) -> PairsResult:
    """Run both arms for every (task, repeat); stop before a pair once over the bound."""
    runs: list[ArmRun] = []
    start = clock()
    for repeat in range(1, repeats + 1):
        for task in tasks:
            if clock() - start > max_wall_seconds:
                return PairsResult(runs, stopped_early=True)
            runs.append(
                _run_baseline(
                    task,
                    repeat,
                    model=baseline_model,
                    router=router,
                    transport=transport,
                    grader=grader,
                )
            )
            runs.append(
                _run_verdict(task, repeat, router=router, transport=transport, grader=grader)
            )
    return PairsResult(runs, stopped_early=False)


# ── Report ─────────────────────────────────────────────────────────────
def _empty_tokens() -> dict[str, int]:
    return {"input": 0, "cached_input": 0, "output": 0}


def _tokens(calls: Iterable[Call]) -> dict[str, Any]:
    total = _empty_tokens()
    by_capacity: dict[str, dict[str, int]] = {}
    missing = 0
    for call in calls:
        if call.input_tokens is None and call.output_tokens is None:
            missing += 1
        bucket = by_capacity.setdefault(call.capacity, _empty_tokens())
        for key, value in (
            ("input", call.input_tokens),
            ("cached_input", call.cached_input_tokens),
            ("output", call.output_tokens),
        ):
            total[key] += value or 0
            bucket[key] += value or 0
    return {"total": total, "by_capacity": by_capacity, "calls_without_usage": missing}


def _arm_summary(runs: Sequence[ArmRun]) -> dict[str, Any]:
    calls = [c for r in runs for c in r.calls]
    accepted = sum(1 for r in runs if r.accepted)
    misses: dict[str, int] = {}
    for r in runs:
        if r.miss_reason:
            key = r.miss_reason.split(":", 1)[0]
            misses[key] = misses.get(key, 0) + 1
    kinds: dict[str, int] = {}
    for c in calls:
        kinds[c.kind] = kinds.get(c.kind, 0) + 1
    return {
        "attempted": len(runs),
        "accepted": accepted,
        "accepted_rate": (accepted / len(runs)) if runs else 0.0,
        "misses": misses,
        "calls": kinds,
        "tokens": _tokens(calls),
    }


def _uncached(calls: Iterable[Call]) -> tuple[int, int, int]:
    calls = list(calls)
    input_total = sum(c.input_tokens or 0 for c in calls)
    cached = sum(c.cached_input_tokens or 0 for c in calls)
    output = sum(c.output_tokens or 0 for c in calls)
    return input_total - cached, cached, output


def _list_price(
    calls: Iterable[Call], prices: Mapping[str, Mapping[str, float]], unpriced: set[str]
) -> float:
    total = 0.0
    for c in calls:
        price = prices.get(c.served_model)
        if price is None:
            unpriced.add(c.served_model)
            continue
        total += (c.input_tokens or 0) * float(price["input_per_1m"]) / 1_000_000
        total += (c.output_tokens or 0) * float(price["output_per_1m"]) / 1_000_000
    return total


def build_report(
    runs: Sequence[ArmRun],
    *,
    config: Mapping[str, Any],
    list_prices: Mapping[str, Mapping[str, float]] | None = None,
    integrity: Mapping[str, str] | None = None,
    stopped_early: bool = False,
) -> dict[str, Any]:
    """Per-arm and per-group accepted work and tokens; refuse claims the evidence cannot carry."""
    by_arm = {arm: [r for r in runs if r.arm == arm] for arm in ARMS}
    groups: dict[str, dict[str, Any]] = {}
    for group in sorted({r.task_group for r in runs}):
        groups[group] = {
            arm: _arm_summary([r for r in by_arm[arm] if r.task_group == group]) for arm in ARMS
        }

    index = {(r.arm, r.task_id, r.repeat): r for r in runs}
    pairs: list[dict[str, Any]] = []
    comparable: list[tuple[ArmRun, ArmRun]] = []
    for task_id, repeat in sorted({(r.task_id, r.repeat) for r in runs}):
        b = index.get(("baseline", task_id, repeat))
        v = index.get(("verdict", task_id, repeat))
        entry: dict[str, Any] = {
            "pair": f"{task_id}/r{repeat}",
            "baseline_served": b.served_model if b else None,
            "verdict_served": v.served_model if v else None,
            "token_delta": None,
        }
        if b is None or v is None:
            entry["status"], entry["reason"] = "failed_pair", "missing arm run"
        elif not (b.accepted and v.accepted):
            failed = [r.arm for r in (b, v) if not r.accepted]
            entry["status"], entry["reason"] = "failed_pair", f"not accepted: {','.join(failed)}"
        elif b.served_model == v.served_model:
            # Same model in both arms: any token difference is run variance, not routing.
            entry["status"], entry["reason"] = "same_model", "both arms served by one model"
        else:
            entry["status"] = "comparable"
            bu, _, bo = _uncached(b.calls)
            vu, _, vo = _uncached(v.calls)
            entry["token_delta"] = (bu + bo) - (vu + vo)
            comparable.append((b, v))
        pairs.append(entry)

    b_calls = [c for b, _ in comparable for c in b.calls]
    v_calls = [c for _, v in comparable for c in v.calls]
    bu, bc, bo = _uncached(b_calls)
    vu, vc, vo = _uncached(v_calls)
    delta = (bu + bo) - (vu + vo)
    arms = {arm: _arm_summary(by_arm[arm]) for arm in ARMS}

    reasons: list[str] = []
    if arms["verdict"]["accepted"] < arms["baseline"]["accepted"]:
        reasons.append(
            f"Verdict arm accepted fewer tasks ({arms['verdict']['accepted']}) than the "
            f"baseline ({arms['baseline']['accepted']}): no savings claim is allowed at "
            "lower accepted work"
        )
    if not comparable:
        reasons.append("no comparable pair (both arms accepted, different served models)")
    if delta <= 0 and comparable:
        reasons.append("Verdict arm did not use fewer uncached tokens on comparable pairs")
    if integrity and integrity.get("before") != integrity.get("after"):
        reasons.append("tasks or tests changed during the run")
    if stopped_early:
        reasons.append("run stopped at the wall-clock bound before every pair ran")

    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "headline_metric": "tokens",
        "config": dict(config),
        "stopped_early": stopped_early,
        "integrity": dict(integrity) if integrity else None,
        "arms": arms,
        "groups": groups,
        "pairs": pairs,
        "savings": {
            "comparable_pairs": len(comparable),
            "claim_allowed": not reasons,
            "reasons": reasons,
            # Model choice: uncached input plus output, so a cache hit never reads as routing.
            "model_choice": {
                "baseline_uncached_input": bu,
                "verdict_uncached_input": vu,
                "baseline_output": bo,
                "verdict_output": vo,
                "delta_tokens": delta,
            },
            "cached_input": {"baseline": bc, "verdict": vc, "delta_tokens": bc - vc},
        },
        "list_price_equivalent": None,
    }
    if list_prices is not None:
        unpriced: set[str] = set()
        report["list_price_equivalent"] = {
            "label": LIST_PRICE_LABEL,
            "scope": "comparable pairs only",
            "baseline": round(_list_price(b_calls, list_prices, unpriced), 6),
            "verdict": round(_list_price(v_calls, list_prices, unpriced), 6),
            "unpriced_models": sorted(unpriced),
        }
    return report


# ── Live wiring (not exercised by unit tests) ──────────────────────────
class LiveRouter:
    """The live IntelligenceService with the EligibilityGate attached as ``verdict serve`` does."""

    def __init__(self) -> None:
        from verdict import api

        # The admit ladder only runs with an explicit OmniRoute endpoint; without it the
        # service falls back to the catalog path, which is not what this bench measures.
        built = api._build_availability_cache() if os.environ.get("OMNIROUTE_BASE_URL") else None
        if built is None:
            print(
                "refusing: no live eligibility gate (set OMNIROUTE_BASE_URL); "
                "the catalog-only path is not the live ladder",
                file=sys.stderr,
            )
            raise SystemExit(2)
        _cache, gate = built
        self.service: Any = api._build_intelligence()
        self.service.eligibility_gate = gate
        self._probes: list[Call] = []
        # Wrap the resolved confirm transport rather than injecting one: an injected
        # transport switches the ladder to its offline-consent branch. Wrapping keeps
        # the live behaviour and lets every confirm probe be charged to the Verdict arm.
        resolve = self.service._resolve_confirm_transport

        def _counting_resolve(endpoint: Any) -> Any:
            inner = resolve(endpoint)
            if inner is None:
                return None

            def transport(model_id: str, payload: Mapping[str, Any], timeout: float) -> Any:
                result = inner(model_id, payload, timeout)
                body = result.get("body") if isinstance(result, Mapping) else None
                body = body if isinstance(body, Mapping) else {}
                in_tok, cached_tok, out_tok = usage_of(body)
                self._probes.append(
                    Call(
                        arm="verdict",
                        kind="probe",
                        requested_model=model_id,
                        served_model=str(body.get("model") or model_id),
                        request_id=None,
                        capacity=self.capacity_of(model_id),
                        status=int(result.get("status_code") or 0),
                        input_tokens=in_tok,
                        cached_input_tokens=cached_tok,
                        output_tokens=out_tok,
                    )
                )
                return result

            return transport

        # Instance-level override of one bound method; the service class is unchanged.
        setattr(self.service, "_resolve_confirm_transport", _counting_resolve)  # noqa: B010

    def capacity_of(self, model: str) -> str:
        snapshot = self.service._load_admit_snapshot()[0]
        if snapshot is None:
            return "unknown"
        capacity = self.service._route_capacity(snapshot, [model]).get(model)
        return str(getattr(capacity, "value", capacity) or "unknown")

    def route(self, prompt: str, criticality: str) -> RouteResult:
        self._probes = []
        decision = asyncio.run(self.service.route(prompt, criticality=criticality))
        receipt = decision.admit_receipt if isinstance(decision.admit_receipt, dict) else {}
        model = decision.model
        concrete = bool(model) and model != NO_ELIGIBLE_TARGET and not _is_opaque(model)
        return RouteResult(
            model=model,
            decision=decision.decision,
            reason=decision.reason,
            context_pack_prompt=decision.context_pack_prompt,
            capacity=self.capacity_of(model) if concrete else "unknown",
            probe_calls=list(self._probes),
            receipt={
                "alternatives": list(decision.alternatives[:8]),
                "safety_flags": list(decision.safety_flags),
                "pack_digest": receipt.get("pack_digest"),
                "prompt_digest": receipt.get("prompt_digest"),
                "pack_state": receipt.get("pack_state"),
            },
        )


def live_transport() -> Transport:
    """OpenAI-compatible chat call through OmniRoute; returns status, headers, body."""
    import httpx

    from verdict.credentials_store import resolve_api_key
    from verdict.free_tier_admit import normalize_omniroute_origin
    from verdict.http_safety import omniroute_evidence_headers

    origin = normalize_omniroute_origin(os.environ.get("OMNIROUTE_BASE_URL", ""))
    url = f"{origin}/v1/chat/completions"
    api_key = resolve_api_key()

    def transport(model: str, messages: list[dict[str, str]], timeout: float) -> Exchange:
        headers = {"Content-Type": "application/json", **omniroute_evidence_headers()}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        resp = httpx.post(
            url,
            json={"model": model, "messages": messages, "max_tokens": 4096, "temperature": 0.0},
            headers=headers,
            timeout=timeout,
        )
        try:
            body = resp.json()
        except ValueError:
            body = {}
        return resp.status_code, dict(resp.headers), body if isinstance(body, dict) else {}

    return transport


def fixture_digest() -> str:
    """SHA-256 over tasks.json and every task test file, to detect edits during a run."""
    digest = hashlib.sha256()
    for path in sorted(TASKS_JSON.parent.iterdir()):
        if path.is_file():
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


# ── CLI ────────────────────────────────────────────────────────────────
def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--baseline-model",
        required=True,
        help=(
            "Concrete frontier route for the baseline arm, on free or subscription "
            "capacity (for example a cc/* subscription model). Required; there is no default."
        ),
    )
    parser.add_argument(
        "--out-dir",
        required=True,
        type=Path,
        help="Directory for report.json and calls.jsonl. Must be outside the repository.",
    )
    parser.add_argument("--repeats", type=int, default=1, help="Runs per arm per task.")
    parser.add_argument(
        "--max-wall-seconds",
        type=float,
        default=3600.0,
        help="Wall-clock bound, checked before each pair; the report marks an early stop.",
    )
    parser.add_argument(
        "--list-prices",
        type=Path,
        default=None,
        help=(
            "Optional JSON {model: {input_per_1m, output_per_1m}} for the secondary "
            "list-price-equivalent line. Never an invoice."
        ),
    )
    return parser.parse_args(argv)


def validate_args(args: argparse.Namespace) -> None:
    out_dir = Path(args.out_dir).expanduser().resolve()
    if out_dir == REPO_ROOT or out_dir.is_relative_to(REPO_ROOT):
        raise SystemExit(f"refusing: --out-dir {out_dir} is inside the repository")
    if (out_dir / "report.json").exists():
        raise SystemExit(f"refusing: {out_dir / 'report.json'} already exists")
    if _is_opaque(args.baseline_model):
        raise SystemExit(f"refusing: --baseline-model {args.baseline_model} is an opaque route")
    if args.repeats < 1:
        raise SystemExit("refusing: --repeats must be at least 1")
    if args.max_wall_seconds <= 0:
        raise SystemExit("refusing: --max-wall-seconds must be positive")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    validate_args(args)
    _refuse_without_opt_in()
    out_dir = Path(args.out_dir).expanduser().resolve()
    tasks = json.loads(TASKS_JSON.read_text(encoding="utf-8"))["tasks"]
    list_prices = (
        json.loads(args.list_prices.read_text(encoding="utf-8")) if args.list_prices else None
    )

    router = LiveRouter()
    baseline_capacity = router.capacity_of(args.baseline_model)
    if baseline_capacity not in ALLOWED_CAPACITY:
        raise SystemExit(
            f"refusing: baseline {args.baseline_model} capacity is {baseline_capacity}"
        )
    before = fixture_digest()
    result = run_pairs(
        tasks,
        router=router,
        transport=live_transport(),
        baseline_model=args.baseline_model,
        repeats=args.repeats,
        max_wall_seconds=args.max_wall_seconds,
    )
    report = build_report(
        result.runs,
        config={
            "baseline_model": args.baseline_model,
            "baseline_capacity": baseline_capacity,
            "repeats": args.repeats,
            "max_wall_seconds": args.max_wall_seconds,
            "tasks": len(tasks),
            "allowed_capacity": sorted(ALLOWED_CAPACITY),
            "unattributed": (
                "availability probes made inside the EligibilityGate adapter "
                "(production profile only) are not visible to the bench"
            ),
        },
        list_prices=list_prices,
        integrity={"before": before, "after": fixture_digest()},
        stopped_early=result.stopped_early,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    with (out_dir / "calls.jsonl").open("w", encoding="utf-8") as fh:
        for run in result.runs:
            row = {k: v for k, v in asdict(run).items() if k != "calls"}
            for call in run.calls:
                fh.write(json.dumps({**row, "call": asdict(call)}) + "\n")
    savings = report["savings"]
    for arm in ARMS:
        summary = report["arms"][arm]
        print(f"{arm}: accepted {summary['accepted']}/{summary['attempted']}")
        print(f"  tokens {summary['tokens']['total']}")
    print(f"comparable pairs: {savings['comparable_pairs']}")
    print(f"claim allowed: {savings['claim_allowed']}")
    for reason in savings["reasons"]:
        print(f"  - {reason}")
    print(f"report: {out_dir / 'report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
