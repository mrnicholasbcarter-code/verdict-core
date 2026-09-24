"""``verdict orchestrate | watch | receipt | eligibility`` — the interview surface.

All four commands are thin: they wire live OmniRoute discovery into the
orchestration components and render with ``verdict.orchestration.tui``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.orchestration.contracts import TaskRequirements, WorkerExecutor
from verdict.orchestration.runtime import RuntimePolicy

DEFAULT_RUNS = Path(".verdict") / "runs"


def add_parsers(subparsers: Any) -> None:
    orch = subparsers.add_parser(
        "orchestrate", help="Goal -> frontier plan -> DAG -> parallel workers -> review -> receipt"
    )
    orch.add_argument("goal", nargs="?", help="High-level goal (omit with --resume)")
    orch.add_argument("--repo", default=".", help="Git repository to change (default: .)")
    orch.add_argument("--graph", help="Use a pre-built WorkGraph JSON instead of frontier planning")
    orch.add_argument("--resume", metavar="RUN_ID", help="Resume a run from its durable state")
    orch.add_argument("--runs-dir", default=str(DEFAULT_RUNS))
    orch.add_argument(
        "--gateway", default=os.environ.get("VERDICT_GATEWAY", "http://127.0.0.1:20128")
    )
    orch.add_argument("--max-parallel", type=int, default=3)
    orch.add_argument("--attempt-timeout", type=float, default=900)
    orch.add_argument("--run-deadline", type=float, default=3600)
    orch.add_argument(
        "--prefer",
        default="claude",
        help="Comma-separated provider preference among SUBSCRIPTION capacity "
        "(ranking policy, not a fallback chain)",
    )
    orch.add_argument(
        "--scope",
        default="",
        help="Comma-separated route prefixes allowed (e.g. cc/,cx/); empty = all",
    )
    orch.add_argument("--no-review", action="store_true", help="Skip OCR review (run ends BLOCKED)")
    orch.add_argument(
        "--inject",
        action="append",
        default=[],
        metavar="ROUTE=FAULT[,FAULT]",
        help="Chaos: inject faults for a route (quota, rate_limit, auth, payment, "
        "forbidden, server, timeout, transport, empty, no_final, malformed, "
        "hang, mismatch); ROUTE may be '*'",
    )
    orch.add_argument("--plain", action="store_true", help="ASCII narrative instead of live view")
    orch.add_argument("--json", action="store_true", help="Print the final receipt JSON")

    watch = subparsers.add_parser("watch", help="Live view of a Verdict orchestration run")
    watch.add_argument("run", help="Run id or run directory")
    watch.add_argument("--runs-dir", default=str(DEFAULT_RUNS))
    watch.add_argument("--once", action="store_true", help="Render the current state and exit")

    rec = subparsers.add_parser("run-receipt", help="Show and verify an orchestration run receipt")
    rec.add_argument("run", help="Run id or run directory")
    rec.add_argument("--runs-dir", default=str(DEFAULT_RUNS))
    rec.add_argument("--json", action="store_true")

    elig = subparsers.add_parser(
        "eligibility",
        help="Show DISCOVERED -> ENTITLED -> HEALTHY -> AVAILABLE -> TASK_ELIGIBLE -> SELECTED",
    )
    elig.add_argument(
        "--gateway", default=os.environ.get("VERDICT_GATEWAY", "http://127.0.0.1:20128")
    )
    elig.add_argument("--scope", default="")
    elig.add_argument("--prefer", default="claude")
    elig.add_argument("--probe", action="store_true", help="Probe lazily to reach SELECTED")
    elig.add_argument("--reasoning", action="store_true")
    elig.add_argument("--frontier", action="store_true")
    elig.add_argument("--json", action="store_true")


def dispatch(args: argparse.Namespace) -> int | None:
    handlers = {
        "orchestrate": _orchestrate,
        "watch": _watch,
        "run-receipt": _receipt,
        "eligibility": _eligibility,
    }
    handler = handlers.get(getattr(args, "command", ""))
    return handler(args) if handler else None


def _state_dir() -> Path:
    return Path(os.environ.get("VERDICT_HOME", Path.home() / ".verdict"))


def build_selector(gateway: str, *, scope: str, prefer: str, load: Any = None) -> Any:
    from verdict.orchestration.eligibility import EligibilityLadder
    from verdict.orchestration.run import fetch_connections, fetch_inventory, resolve_api_key
    from verdict.subagent_selection import LaunchCandidate, openai_health_probe

    key = resolve_api_key()
    rows = fetch_inventory(gateway, api_key=key)
    prefixes = tuple(p.strip() for p in scope.split(",") if p.strip())
    if prefixes:
        rows = [r for r in rows if str(r.get("id", "")).startswith(prefixes)]
    connections = fetch_connections(gateway, api_key=key)
    raw_probe = openai_health_probe(gateway.rstrip("/") + "/v1", api_key=key, timeout_seconds=30)

    def probe(route_id: str) -> Any:
        return raw_probe(
            LaunchCandidate(route_id, route_id, frozenset(), 0, 0.0, 0.0, False, False, 0, 0)
        )

    return EligibilityLadder(
        rows,
        connections,
        probe,
        _state_dir() / "orchestration-health.json",
        prefer_providers=tuple(p.strip() for p in prefer.split(",") if p.strip()),
        load=load,
        harness_visible=prime_visibility(),
    )


def prime_visibility(path: Path | None = None) -> Any:
    """Harness gate: a route is spawnable only if Prime's registry lists it.

    Reads model ids only (never credentials) from Prime's models.json. Returns
    None (no gate) when the registry is absent, e.g. for a non-Prime executor.
    """
    registry = path or Path.home() / ".prime" / "agent" / "models.json"
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
        models = data["providers"]["omniroute"]["models"]
        visible = {str(m["id"]) for m in models if isinstance(m, dict) and m.get("id")}
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return lambda route_id: route_id in visible


def _executor(args: argparse.Namespace) -> WorkerExecutor:
    from verdict.orchestration.executors import FaultInjectingExecutor, PrimeHeadlessExecutor

    executor: WorkerExecutor = PrimeHeadlessExecutor()
    faults: dict[str, list[str]] = {}
    for item in args.inject:
        route, _, kinds = item.partition("=")
        faults.setdefault(route.strip(), []).extend(
            k.strip() for k in kinds.split(",") if k.strip()
        )
    if faults:
        executor = FaultInjectingExecutor(executor, faults)
    return executor


def _resolve_run(value: str, runs_dir: str) -> Path:
    path = Path(value)
    return path if path.is_dir() else Path(runs_dir) / value


def _orchestrate(args: argparse.Namespace) -> int:
    from verdict.orchestration.contracts import WorkGraph
    from verdict.orchestration.recovery import FailureIntelligence
    from verdict.orchestration.review import OpenCodeReviewer
    from verdict.orchestration.run import resolve_api_key, run_golden_path
    from verdict.orchestration.tui import follow

    repo = Path(args.repo).resolve()
    runs_root = Path(args.runs_dir)
    if not runs_root.is_absolute():
        runs_root = repo / runs_root
    if resolve_api_key() is None:
        print("BLOCKED: set VERDICT_OMNIROUTE_API_KEY for the OmniRoute gateway", file=sys.stderr)
        return 2
    goal = args.goal or ""
    graph = None
    if args.graph:
        graph = WorkGraph.from_dict(json.loads(Path(args.graph).read_text()))
        goal = goal or graph.goal
    if args.resume:
        prior = runs_root / args.resume / "graph.json"
        if prior.exists():
            goal = goal or str(json.loads(prior.read_text()).get("goal", ""))
    if not goal:
        print("BLOCKED: a goal (or --graph/--resume) is required", file=sys.stderr)
        return 2
    inflight: dict[str, str] = {}  # node_id -> route_id, maintained by DagRuntime
    selector = build_selector(
        args.gateway,
        scope=args.scope,
        prefer=args.prefer,
        load=lambda route: sum(1 for r in inflight.values() if r == route),
    )
    run_id = args.resume or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    reviewer = (
        None
        if args.no_review
        else OpenCodeReviewer(
            selector, out_dir=run_dir / "review", gateway_url=args.gateway.rstrip("/") + "/v1"
        )
    )
    policy = RuntimePolicy(
        max_parallel=args.max_parallel,
        attempt_timeout_seconds=args.attempt_timeout,
        run_deadline_seconds=args.run_deadline,
        require_review=True,
    )
    events_path = run_dir / "events.jsonl"
    events_path.touch()
    stop = threading.Event()
    viewer = None
    if not args.json:
        viewer = threading.Thread(
            target=lambda: follow(events_path, stop_when_final=True), daemon=True
        )
        viewer.start()
    result = asyncio.run(
        run_golden_path(
            goal,
            repo=repo,
            runs_root=runs_root,
            selector=selector,
            executor=_executor(args),
            classifier=FailureIntelligence(),
            reviewer=reviewer,
            graph=graph,
            run_id=run_id,
            policy=policy,
            summary=getattr(selector, "summary", None),
            inflight=inflight,
        )
    )
    stop.set()
    if viewer is not None:
        viewer.join(timeout=5)
    receipt = json.loads(result.receipt_path.read_text()) if result.receipt_path.exists() else {}
    if args.json:
        print(json.dumps(receipt, indent=2, default=str))
    else:
        print(
            f"\nVERDICT {result.outcome}: {result.reason}\nrun: {result.run_dir}\n"
            f"receipt: {result.receipt_path}"
        )
    return 0 if result.outcome == "COMPLETE" else 1


def _watch(args: argparse.Namespace) -> int:
    from verdict.orchestration.tui import follow, render_text

    run_dir = _resolve_run(args.run, args.runs_dir)
    events = run_dir / "events.jsonl"
    if not events.exists():
        print(f"no run at {run_dir}", file=sys.stderr)
        return 2
    if args.once:
        rows = [json.loads(line) for line in events.read_text().splitlines() if line.strip()]
        plain = not sys.stdout.isatty() or "NO_COLOR" in os.environ
        print(render_text(rows, width=110, plain=plain))
        return 0
    view = follow(events, stop_when_final=True)
    return 0 if getattr(view, "outcome", "") == "COMPLETE" else 1


def _receipt(args: argparse.Namespace) -> int:
    from verdict.orchestration.receipt import completion_verdict, verify_run_receipt

    run_dir = _resolve_run(args.run, args.runs_dir)
    path = run_dir / "receipt.json"
    if not path.exists():
        print(f"no receipt at {path}", file=sys.stderr)
        return 2
    receipt = json.loads(path.read_text())
    problems = verify_run_receipt(run_dir)
    outcome, reason = completion_verdict(receipt)
    if args.json:
        print(
            json.dumps(
                {"outcome": outcome, "reason": reason, "problems": problems, "receipt": receipt},
                indent=2,
                default=str,
            )
        )
    else:
        print(f"{outcome}: {reason}")
        print(
            "integrity: " + ("OK (events digest verified)" if not problems else "; ".join(problems))
        )
        for node in receipt.get("nodes", []):
            attempts = " -> ".join(
                f"{a.get('route_id') or 'merge'}[{a.get('outcome')}"
                + (f":{a['failure_category']}" if a.get("failure_category") else "")
                + ("*" if a.get("fault_injected") else "")
                + "]"
                for a in node.get("attempts", [])
            )
            print(f"  {node['node_id']:<18} {node['final_state']:<16} {attempts}")
        review = receipt.get("review", {})
        print(
            f"  review: {review.get('status')} by {review.get('reviewer')} on {review.get('route_id')}"
        )
    return 0 if outcome == "COMPLETE" and not problems else 1


def _eligibility(args: argparse.Namespace) -> int:
    selector = build_selector(args.gateway, scope=args.scope, prefer=args.prefer)
    requirements = TaskRequirements(
        required_capabilities=frozenset({"tools"}),
        coding=True,
        reasoning=args.reasoning,
        frontier_worthy=args.frontier,
    )
    now = datetime.now(timezone.utc)
    if args.probe:
        chosen, verdicts = selector.select(requirements, now=now)
    else:
        chosen, verdicts = None, selector.evaluate(requirements, now=now)
    summary = selector.summary()
    if args.json:
        print(
            json.dumps(
                {
                    "summary": summary,
                    "selected": chosen.to_dict() if chosen else None,
                    "verdicts": [v.to_dict() for v in verdicts],
                },
                indent=2,
            )
        )
        return 0
    print("  ".join(f"{k.upper()} {v}" for k, v in summary.items()))
    ranked = sorted((v for v in verdicts if v.rank is not None), key=lambda v: v.rank or 0)
    for v in ranked[:15]:
        print(
            f"  #{v.rank:<3} {v.route_id:<40} {v.capacity_class.value:<13} {v.plan_label[:28]:<28} "
            f"{v.reached.value if v.reached else '-'}"
        )
    if chosen:
        print(f"SELECTED {chosen.route_id} ({chosen.capacity_class.value})")
    return 0
