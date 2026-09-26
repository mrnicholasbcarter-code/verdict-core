"""``verdict orchestrate | watch | receipt | eligibility`` — the orchestration CLI surface.

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
        "hang, mismatch); ROUTE may be an exact route, a provider 'cc/*', "
        "a node '@node_id', the N-th executor call '#N' (planning is #1), or '*'",
    )
    orch.add_argument(
        "--state-file",
        help="Health/cooldown state file (default ~/.verdict/"
        "orchestration-health.json); chaos runs use a per-run file so injected "
        "faults never cool down real capacity for later runs",
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

    from verdict.orchestration import supervisor

    supervisor.add_parser(subparsers)

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
    elig.add_argument(
        "--provider-family",
        action="append",
        default=[],
        metavar="FAMILY[,FAMILY]",
        help="Only evaluate routes whose id prefix (before '/') is one of these "
        "families, e.g. cc,kr; repeatable. Listed in output metadata.",
    )
    elig.add_argument("--json", action="store_true")
    elig.add_argument(
        "--no-pager", action="store_true", help="Never pipe human output through a pager"
    )

    _add_prime_sync_models(subparsers)


def _add_prime_sync_models(subparsers: Any) -> None:
    """Attach ``verdict harness prime sync-models`` to the existing harness parser.

    ``harness prime`` is registered by ``verdict.commands.parsers_harness``
    before this module runs; this only adds one leaf subcommand to it.
    """
    harness = getattr(subparsers, "choices", {}).get("harness")
    prime = _subcommand(harness, "prime")
    prime_sub = _subparsers_action(prime)
    if prime_sub is None or "sync-models" in prime_sub.choices:
        return
    sync = prime_sub.add_parser(
        "sync-models",
        help="Rewrite providers.omniroute.models in ~/.prime/agent/models.json "
        "from the live gateway /v1/models (backup first)",
    )
    sync.add_argument(
        "--gateway",
        default=os.environ.get("OMNIROUTE_BASE_URL")
        or os.environ.get("VERDICT_GATEWAY", "http://127.0.0.1:20128"),
        help="OmniRoute gateway base URL (default: $OMNIROUTE_BASE_URL or :20128)",
    )
    sync.add_argument(
        "--dry-run", action="store_true", help="Print added/removed ids; write nothing"
    )


def _subparsers_action(parser: Any) -> Any:
    for action in getattr(parser, "_actions", []):
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _subcommand(parser: Any, name: str) -> Any:
    action = _subparsers_action(parser)
    return action.choices.get(name) if action is not None else None


def dispatch(args: argparse.Namespace) -> int | None:
    from verdict.orchestration import supervisor

    handlers = {
        "supervise": supervisor.dispatch,
        "orchestrate": _orchestrate,
        "watch": _watch,
        "run-receipt": _receipt,
        "eligibility": _eligibility,
    }
    if (
        getattr(args, "command", "") == "harness"
        and getattr(args, "harness_target", "") == "prime"
        and getattr(args, "harness_prime_command", "") == "sync-models"
    ):
        return _prime_sync_models(args)
    handler = handlers.get(getattr(args, "command", ""))
    return handler(args) if handler else None


def _prime_sync_models(args: argparse.Namespace) -> int:
    from verdict.harness_prime import HarnessPrimeError, format_sync_models, sync_models
    from verdict.orchestration.run import fetch_inventory, resolve_api_key

    gateway = str(args.gateway).rstrip("/")
    if gateway.endswith("/v1"):
        gateway = gateway[: -len("/v1")]
    try:
        rows = fetch_inventory(gateway, api_key=resolve_api_key())
    except Exception as exc:  # network/shape failure: report, never write
        print(
            f"BLOCKED: cannot read live inventory from {gateway}/v1/models: {exc}", file=sys.stderr
        )
        return 2
    try:
        result = sync_models(rows, dry_run=bool(args.dry_run))
    except HarnessPrimeError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 1
    print(format_sync_models(result), end="")
    return 0


def _state_dir() -> Path:
    return Path(os.environ.get("VERDICT_HOME", Path.home() / ".verdict"))


def build_selector(
    gateway: str,
    *,
    scope: str,
    prefer: str,
    load: Any = None,
    state_file: Path | None = None,
    provider_families: tuple[str, ...] = (),
) -> Any:
    from verdict.orchestration.eligibility import EligibilityLadder
    from verdict.orchestration.run import fetch_connections, fetch_inventory, resolve_api_key
    from verdict.subagent_selection import LaunchCandidate, openai_health_probe

    key = resolve_api_key()
    rows = fetch_inventory(gateway, api_key=key)
    live_ids = [str(r.get("id")) for r in rows if r.get("id")]
    prefixes = tuple(p.strip() for p in scope.split(",") if p.strip())
    if prefixes:
        rows = [r for r in rows if str(r.get("id", "")).startswith(prefixes)]
    if provider_families:
        rows = [r for r in rows if route_prefix(str(r.get("id", ""))) in provider_families]
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
        state_file or (_state_dir() / "orchestration-health.json"),
        prefer_providers=tuple(p.strip() for p in prefer.split(",") if p.strip()),
        load=load,
        harness_visible=prime_visibility(live_ids=live_ids),
    )


def route_prefix(route_id: str) -> str:
    """Provider family of a gateway route id: the prefix before '/' (``cc/x`` -> ``cc``)."""
    return route_id.split("/", 1)[0].lower() if "/" in route_id else ""


def parse_provider_families(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """``["cc,kr", "gc"]`` -> ``("cc", "gc", "kr")`` (deduplicated, sorted, lowercase)."""
    found = {part.strip().lower() for value in values for part in value.split(",")}
    return tuple(sorted(f for f in found if f))


def prime_visibility(path: Path | None = None, *, live_ids: Any = None) -> Any:
    """Harness gate: which route ids a Prime worker can be spawned on.

    The live gateway inventory (``live_ids``, the ids from ``GET /v1/models``)
    is the source of truth. Prime's ``models.json`` is only a fallback signal
    when no live inventory is supplied: it is a static snapshot and goes stale.
    Reads model ids only (never credentials). With neither source available the
    gate fails closed and every route is denied as
    ``harness_inventory_unavailable``.
    """
    from verdict.orchestration.eligibility import HarnessVisibility

    if live_ids is not None:
        ids = frozenset(str(i) for i in live_ids if i)
        if ids:
            return HarnessVisibility(ids, source="live")
    registry = path or Path.home() / ".prime" / "agent" / "models.json"
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
        models = data["providers"]["omniroute"]["models"]
        visible = frozenset(str(m["id"]) for m in models if isinstance(m, dict) and m.get("id"))
    except (OSError, ValueError, KeyError, TypeError):
        return HarnessVisibility(None, source="none")
    return HarnessVisibility(visible, source="models.json")


def _executor(args: argparse.Namespace) -> WorkerExecutor:
    from verdict.orchestration.executors import FaultInjectingExecutor, PrimeHeadlessExecutor

    executor: WorkerExecutor = PrimeHeadlessExecutor()
    faults: dict[str, list[str]] = {}
    injected = list(args.inject)
    # Supervisor-generation-scoped chaos: VERDICT_CHAOS_G0 applies only to the
    # first controller life (lets a demo stall generation 0, then resume cleanly).
    generation = os.environ.get("VERDICT_CONTROLLER_GENERATION", "0")
    injected.extend(x for x in os.environ.get(f"VERDICT_CHAOS_G{generation}", "").split(";") if x)
    for item in injected:
        route, _, kinds = item.partition("=")
        faults.setdefault(route.strip(), []).extend(
            k.strip() for k in kinds.split(",") if k.strip()
        )
    if faults:
        executor = FaultInjectingExecutor(executor, faults)
    return executor


def _chaos_state(args: argparse.Namespace, runs_root: Path) -> Path | None:
    if args.state_file:
        return Path(args.state_file)
    if args.inject:
        run_id = args.resume or "chaos"
        return runs_root / run_id / "chaos-health.json"
    return None


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
        state_file=_chaos_state(args, runs_root),
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
    prior_seq = sum(1 for line in events_path.read_text().splitlines() if line.strip())
    stop = threading.Event()
    viewer = None
    if not args.json:
        viewer = threading.Thread(
            target=lambda: follow(events_path, stop_when_final=True, start_seq=prior_seq),
            daemon=True,
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


_STAGE_ORDER = ("DISCOVERED", "ENTITLED", "HEALTHY", "AVAILABLE", "TASK_ELIGIBLE", "SELECTED")


def eligibility_payload(
    verdicts: Any, summary: dict[str, int], chosen: Any, filters: dict[str, list[str]]
) -> dict[str, Any]:
    """Complete, self-reconciling eligibility record set (never truncated).

    ``summary`` keeps the ladder's cumulative stage counts and adds
    ``selected`` (the chosen route id or None, equal to ``selected.route_id``)
    and ``by_reached_stage``: exclusive buckets whose sum is ``evaluated_count``.
    """
    records = [v.to_dict() for v in verdicts]
    buckets = dict.fromkeys(_STAGE_ORDER, 0)
    buckets["NONE"] = 0
    for record in records:
        buckets[record["reached"] or "NONE"] += 1
    full_summary: dict[str, Any] = dict(summary)
    full_summary["selected"] = chosen.route_id if chosen else None
    full_summary["by_reached_stage"] = buckets
    return {
        "filters": filters,
        "evaluated_count": len(records),
        "summary": full_summary,
        "selected": chosen.to_dict() if chosen else None,
        "verdicts": records,
    }


def render_eligibility_text(payload: dict[str, Any]) -> str:
    """Human view listing every evaluated route: selected, ranked, then rejected by stage."""
    filters = payload["filters"]
    active = ", ".join(f"{k}={','.join(v)}" for k, v in sorted(filters.items()) if v)
    summary = payload["summary"]
    counts = "  ".join(f"{k.upper()} {v}" for k, v in summary.items() if isinstance(v, int))
    lines = [
        f"filters: {active or 'none'}",
        f"evaluated: {payload['evaluated_count']}  {counts}  SELECTED {summary['selected'] or '-'}",
    ]
    records = payload["verdicts"]
    selected_id = summary["selected"]

    def fmt(r: dict[str, Any]) -> str:
        rank = "-" if r["rank"] is None else str(r["rank"])
        return (
            f"  #{rank:<5} {r['route_id']:<48} {r['provider']:<14} "
            f"{r['capacity_class']:<13} reached={r['reached'] or '-':<13} "
            f"failed={r['failed_stage'] or '-':<13} reason={r['reason']}"
            + (f" cooldown_until={r['cooldown_until']}" if r["cooldown_until"] else "")
        )

    chosen = [r for r in records if r["route_id"] == selected_id]
    ranked = sorted(
        (r for r in records if r["route_id"] != selected_id and r["failed_stage"] is None),
        key=lambda r: (r["rank"] is None, r["rank"] or 0, r["route_id"]),
    )
    rejected = [r for r in records if r["route_id"] != selected_id and r["failed_stage"]]
    lines.append(f"SELECTED ({len(chosen)})")
    lines.extend(fmt(r) for r in chosen)
    lines.append(f"RANKED / ELIGIBLE ({len(ranked)})")
    lines.extend(fmt(r) for r in ranked)
    for stage in _STAGE_ORDER:
        group = [r for r in rejected if r["failed_stage"] == stage]
        if group:
            lines.append(f"REJECTED AT {stage} ({len(group)})")
            lines.extend(fmt(r) for r in sorted(group, key=lambda r: (r["reason"], r["route_id"])))
    return "\n".join(lines) + "\n"


def _page(text: str, *, no_pager: bool) -> None:
    """Pipe through $PAGER (else ``less -R``) on a TTY; never drops output."""
    if no_pager or not sys.stdout.isatty():
        sys.stdout.write(text)
        return
    import shlex
    import subprocess  # nosec B404 - operator-configured pager only

    command = shlex.split(os.environ.get("PAGER") or "less -R")
    try:
        subprocess.run(command, input=text, text=True, check=False)  # nosec B603
    except OSError:
        sys.stdout.write(text)


def _eligibility(args: argparse.Namespace) -> int:
    families = parse_provider_families(getattr(args, "provider_family", []) or [])
    selector = build_selector(
        args.gateway, scope=args.scope, prefer=args.prefer, provider_families=families
    )
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
    filters = {
        "provider_family": list(families),
        "scope": [p.strip() for p in args.scope.split(",") if p.strip()],
    }
    payload = eligibility_payload(verdicts, selector.summary(), chosen, filters)
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    _page(render_eligibility_text(payload), no_pager=bool(getattr(args, "no_pager", False)))
    return 0
