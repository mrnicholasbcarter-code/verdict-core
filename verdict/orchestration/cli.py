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
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.orchestration.contracts import WorkerExecutor
from verdict.orchestration.eligibility_report import build_selector, prime_visibility_report
from verdict.orchestration.runtime import RuntimePolicy

DEFAULT_RUNS = Path(".verdict") / "runs"


class _ExplicitPrefer(argparse.Action):
    """Keep the legacy default while detecting an explicit eligibility preference."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        setattr(namespace, self.dest, values)
        namespace._prefer_explicit = True


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def _capacity_filter(raw: str) -> tuple[str, ...]:
    values = tuple(part.strip() for part in raw.split(","))
    if not values or any(v not in {"free", "subscription", "metered", "unknown"} for v in values):
        raise argparse.ArgumentTypeError("expected comma-separated economic classes")
    return tuple(dict.fromkeys(values))


def add_parsers(subparsers: Any) -> None:
    orch = subparsers.add_parser(
        "orchestrate", help="Goal -> frontier plan -> DAG -> parallel workers -> review -> receipt"
    )
    orch.add_argument("goal", nargs="?", help="High-level goal (omit with --resume)")
    orch.add_argument("--repo", default=".", help="Git repository to change (default: .)")
    orch.add_argument("--graph", help="Use a pre-built WorkGraph JSON instead of frontier planning")
    orch.add_argument("--resume", metavar="RUN_ID", help="Resume a run from its durable state")
    orch.add_argument(
        "--openspec-change", metavar="ID", help="OpenSpec change id under repo/openspec/changes"
    )
    orch.add_argument("--runs-dir", default=str(DEFAULT_RUNS))
    orch.add_argument(
        "--gateway", default=os.environ.get("VERDICT_GATEWAY", "http://127.0.0.1:20128")
    )
    orch.add_argument("--max-parallel", type=int, default=3)
    orch.add_argument("--max-attempts-per-node", type=_positive_int, default=4)
    orch.add_argument(
        "--capacity",
        type=_capacity_filter,
        default=(),
        help="Hard economic-class filter, e.g. free,subscription (default: unchanged)",
    )
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
        "a node '@node_id', the N-th executor call '#N' (planning is #1), "
        "the N-th worker dispatch 'worker#N' (planner calls do not count), or '*'",
    )
    orch.add_argument(
        "--state-file",
        help="Health/cooldown state file (default ~/.verdict/"
        "orchestration-health.json); chaos runs use a per-run file so injected "
        "faults never cool down real capacity for later runs",
    )
    orch.add_argument("--plain", action="store_true", help="ASCII narrative instead of live view")
    orch.add_argument("--json", action="store_true", help="Print the final receipt JSON")
    orch.add_argument(
        "--executor",
        choices=("prime", "direct-gateway"),
        default="prime",
        help="Worker executor backend (default: prime)",
    )
    orch.add_argument(
        "--executor-map",
        metavar="NODE=BACKEND[,NODE=BACKEND,...]",
        default="",
        help=(
            "Route specific nodes to a named executor backend; unmapped nodes use "
            "--executor as the default.  Format: comma-separated node_id=backend pairs "
            "where backend is one of 'prime' or 'direct-gateway'. "
            "Example: --executor-map node-1=prime,node-2=direct-gateway. "
            "When any mapping is provided the run automatically uses MixedExecutor; "
            "--executor sets the fallback for unmapped nodes."
        ),
    )

    watch = subparsers.add_parser("watch", help="Live view of a Verdict orchestration run")
    watch.add_argument("run", help="Run id or run directory")
    watch.add_argument("--runs-dir", default=str(DEFAULT_RUNS))
    watch.add_argument("--once", action="store_true", help="Render the current state and exit")
    watch.add_argument("--node", metavar="N", help="Select a recorded worker node")
    watch.add_argument(
        "--panel",
        choices=("routing", "context", "receipt", "health"),
        help="Open an inspection panel",
    )
    watch.add_argument(
        "--replay",
        action="store_true",
        help="Replay mode: drive TUI from completed run's events.jsonl with timing",
    )
    watch.add_argument(
        "--speed", type=float, default=1.0, help="Replay speed multiplier (default: 1.0)"
    )

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
    elig.set_defaults(_prefer_explicit=False)
    elig.add_argument("--prefer", default="claude", action=_ExplicitPrefer)
    elig.add_argument("--probe", action="store_true", help="Probe lazily to reach SELECTED")
    elig.add_argument(
        "--sync-visibility",
        action="store_true",
        help="Write Prime's visibility registry from this live catalog before "
        "evaluating (default: off, read-only). Prefer the explicit "
        "'verdict harness prime sync-models [--dry-run]' surface instead.",
    )
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
    elig.add_argument(
        "--verified", action="store_true", help="Show verified model evidence after bounded refresh"
    )
    elig.add_argument("--status", default=None, help="Verified model status filter")
    elig.add_argument("--provider", default=None, help="Verified model provider filter")
    elig.add_argument("--search", default=None, help="Case-insensitive verified model text filter")
    elig.add_argument("--page", type=int, default=1, help="Verified model page (one-based)")
    elig.add_argument(
        "--page-size", type=int, default=50, help="Verified model page size (max 200)"
    )
    elig.add_argument(
        "--no-refresh", action="store_true", help="Read verified evidence without automatic refresh"
    )
    elig.add_argument("--json", action="store_true")
    elig.add_argument(
        "--no-pager", action="store_true", help="Never pipe human output through a pager"
    )

    routing = subparsers.add_parser("routing", help="Show the recorded routing explorer for a run")
    routing.add_argument("run", nargs="?", help="Run id or run directory")
    routing.add_argument("--runs-dir", default=str(DEFAULT_RUNS))
    routing.add_argument("--node", default=None, help="Show one node id only")
    routing.add_argument("--json", action="store_true")
    routing.add_argument("--state", default=None, help="Candidate state filter")
    routing.add_argument("--provider", default=None, help="Provider filter")
    routing.add_argument("--search", default=None, help="Case-insensitive text filter")
    routing.add_argument("--page", type=int, default=0)
    routing.add_argument("--page-size", type=int, default=25)
    routing.add_argument(
        "--inventory", action="store_true", help="Read-only inventory view; never runs live probes"
    )

    context = subparsers.add_parser(
        "context", help="Show recorded context budget and provenance for a run"
    )
    context.add_argument("run", nargs="?", help="Run id or run directory")
    context.add_argument("--runs-dir", default=str(DEFAULT_RUNS))
    context.add_argument("--node", default=None, help="Show one node id only")
    context.add_argument("--json", action="store_true")

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
    vis = prime_sub.add_parser(
        "visibility",
        help="Read-only drift report: live gateway models vs Prime-visible models, "
        "with the last refresh timestamp/digest and last failure",
    )
    vis.add_argument(
        "--gateway",
        default=os.environ.get("OMNIROUTE_BASE_URL")
        or os.environ.get("VERDICT_GATEWAY", "http://127.0.0.1:20128"),
        help="OmniRoute gateway base URL (default: $OMNIROUTE_BASE_URL or :20128)",
    )
    vis.add_argument("--json", action="store_true", help="Print the report as JSON")


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
        "routing": _routing,
        "context": _context,
    }
    if (
        getattr(args, "command", "") == "harness"
        and getattr(args, "harness_target", "") == "prime"
        and getattr(args, "harness_prime_command", "") == "sync-models"
    ):
        return _prime_sync_models(args)
    if (
        getattr(args, "command", "") == "harness"
        and getattr(args, "harness_target", "") == "prime"
        and getattr(args, "harness_prime_command", "") == "visibility"
    ):
        return _prime_visibility(args)
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


def _prime_visibility(args: argparse.Namespace) -> int:
    from verdict.orchestration.run import fetch_inventory, resolve_api_key

    gateway = str(args.gateway).rstrip("/")
    if gateway.endswith("/v1"):
        gateway = gateway[: -len("/v1")]
    try:
        rows = fetch_inventory(gateway, api_key=resolve_api_key())
    except Exception as exc:  # report, never write
        print(
            f"BLOCKED: cannot read live inventory from {gateway}/v1/models: {exc}", file=sys.stderr
        )
        return 2
    report = prime_visibility_report(rows)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["in_sync"] else 1
    last = report["last_refresh"] or {}
    lines = [
        f"Prime visibility vs live gateway ({gateway})",
        f"  registry: {report['registry']}"
        + (f" (unreadable: {report['registry_error']})" if report["registry_error"] else ""),
        f"  live concrete models: {report['live_count']}"
        f" (+{report['live_opaque_excluded']} opaque excluded)",
        f"  Prime-visible models: {report['prime_visible_count']}",
        f"  live but not visible: {len(report['live_not_visible'])}",
        f"  visible but not live: {len(report['visible_not_live'])}",
        f"  last refresh: {last.get('refreshed_at', 'never')}"
        f" digest {last.get('digest', '-')} count {last.get('count', '-')}",
    ]
    if report["last_failure"]:
        fail = report["last_failure"]
        lines.append(f"  last refresh failure: {fail['error_type']} at {fail['failed_at']}")
    # Complete drift set: eligibility/visibility output is never truncated.
    for label, key in (("+", "live_not_visible"), ("-", "visible_not_live")):
        lines.extend(f"  {label} {rid}" for rid in report[key])
    lines.append("  status: " + ("in sync" if report["in_sync"] else "DRIFT"))
    print("\n".join(lines))
    return 0 if report["in_sync"] else 1


def _build_single_executor(name: str, *, gateway: str, api_key: str) -> WorkerExecutor:
    """Build one named executor backend (prime or direct-gateway)."""
    from verdict.orchestration.executors import DirectGatewayExecutor, PrimeHeadlessExecutor

    if name == "direct-gateway":
        return DirectGatewayExecutor(base_url=gateway, api_key=api_key)
    return PrimeHeadlessExecutor()


def _parse_executor_map(raw: str) -> dict[str, str]:
    """Parse ``node-1=prime,node-2=direct-gateway`` into a dict.

    Raises :class:`SystemExit` with a clear message for unknown backend names.
    """
    known = {"prime", "direct-gateway"}
    result: dict[str, str] = {}
    if not raw.strip():
        return result
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            sys.exit(f"error: --executor-map entry {pair!r} must be in node_id=backend format")
        node_id, _, backend = pair.partition("=")
        node_id = node_id.strip()
        backend = backend.strip()
        if backend not in known:
            sys.exit(
                f"error: --executor-map: unknown backend {backend!r} for node {node_id!r}; "
                f"choose from {sorted(known)}"
            )
        if not node_id:
            sys.exit("error: --executor-map: node_id must not be empty")
        result[node_id] = backend
    return result


def _executor(args: argparse.Namespace) -> WorkerExecutor:
    from verdict.orchestration.executors import FaultInjectingExecutor, MixedExecutor

    # Empty here lets each executor apply Verdict key precedence
    # (VERDICT_OMNIROUTE_API_KEY > OMNIROUTE_API_KEY > OPENAI_API_KEY).
    api_key = ""
    raw_map = getattr(args, "executor_map", "") or ""
    node_backend_map = _parse_executor_map(raw_map)

    executor: WorkerExecutor
    default_name = getattr(args, "executor", "prime") or "prime"
    default_executor = _build_single_executor(default_name, gateway=args.gateway, api_key=api_key)

    if node_backend_map:
        named: dict[str, WorkerExecutor] = {
            node_id: _build_single_executor(backend, gateway=args.gateway, api_key=api_key)
            for node_id, backend in node_backend_map.items()
        }
        executor = MixedExecutor(node_map=named, default=default_executor)
    else:
        executor = default_executor
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
        # Keep injected-fault health state beside, not inside, the runs dir:
        # a "chaos" folder under runs_root looked like a second run to every
        # consumer that lists runs (certification rehearsal, trace, watch).
        run_id = args.resume or "chaos"
        return runs_root.parent / f".{runs_root.name}-chaos" / run_id / "chaos-health.json"
    return None


def _resolve_run(value: str, runs_dir: str) -> Path:
    """An existing directory is used as given; otherwise join runs_dir/value once.

    A value that already starts with runs_dir is not joined again.
    """
    path = Path(value)
    if path.is_dir():
        return path
    base = Path(runs_dir)
    if base.parts and path.parts[: len(base.parts)] == base.parts:
        return path
    return base / value


def _orchestrate(args: argparse.Namespace) -> int:
    from verdict.orchestration.contracts import WorkGraph
    from verdict.orchestration.recovery import FailureIntelligence
    from verdict.orchestration.review import OpenCodeReviewer
    from verdict.orchestration.run import resolve_api_key, run_golden_path
    from verdict.orchestration.tui import follow

    repo = Path(args.repo).resolve()
    change_id = getattr(args, "openspec_change", None)
    if change_id and (Path(change_id).name != change_id or change_id in (".", "..")):
        print("BLOCKED: --openspec-change must be a change id, not a path", file=sys.stderr)
        return 2
    openspec_change_dir = repo / "openspec" / "changes" / change_id if change_id else None
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
    # Validate --executor-map node ids against the graph when one is available.
    # When no graph exists yet (frontier planning), the graph is built later;
    # emit a stderr warning so the operator can catch typos before waiting.
    raw_map = getattr(args, "executor_map", "") or ""
    if raw_map:
        node_backend_map = _parse_executor_map(raw_map)
        if node_backend_map:
            if graph is not None:
                known_ids = {n.node_id for n in graph.nodes}
                unknown = sorted(n for n in node_backend_map if n not in known_ids)
                if unknown:
                    print(
                        f"error: --executor-map: unknown node id(s) {unknown!r}; "
                        f"known ids: {sorted(known_ids)!r}",
                        file=sys.stderr,
                    )
                    return 2
            else:
                # No graph yet (frontier planning will build it); warn and continue.
                print(
                    "warning: --executor-map: graph not yet known (frontier planning); "
                    "node id(s) in map cannot be validated at start time: "
                    + ", ".join(sorted(node_backend_map)),
                    file=sys.stderr,
                )
    from verdict.actions.verified_models import selection_refresh_hook

    inflight: dict[str, str] = {}  # node_id -> route_id, maintained by DagRuntime
    state_path = _chaos_state(args, runs_root)
    refresh_hook = selection_refresh_hook(
        args.gateway, state_dir=state_path.parent if state_path else None
    )
    refresh_kwargs: dict[str, Any] = (
        {"refresh_hook": refresh_hook} if refresh_hook is not None else {}
    )
    selector = build_selector(
        args.gateway,
        scope=args.scope,
        prefer=args.prefer,
        load=lambda route: sum(1 for r in inflight.values() if r == route),
        state_file=state_path,
        **refresh_kwargs,
        **({"capacity": args.capacity} if getattr(args, "capacity", ()) else {}),
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
        max_attempts_per_node=getattr(args, "max_attempts_per_node", 4),
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
            # Background progress view: never interactive (it must not read stdin
            # or change terminal mode while the orchestration loop runs).
            target=lambda: follow(
                events_path, stop_when_final=True, start_seq=prior_seq, interactive=False
            ),
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
            openspec_change_dir=openspec_change_dir,
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


def _print_view_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _routing(args: argparse.Namespace) -> int:
    """CLI surface for the routing explorer. Rendering stays in routing_render."""
    import shutil

    from verdict.actions.registry import run_action
    from verdict.design import presentation_mode
    from verdict.orchestration.routing_render import render_routing, render_routing_text

    if not args.run and not args.inventory:
        print("routing requires a run id or --inventory", file=sys.stderr)
        return 2
    result = run_action(
        "routing.view",
        {
            "run": args.run,
            "runs_dir": args.runs_dir,
            "node": args.node,
            "state": args.state,
            "provider": args.provider,
            "search": args.search,
            "page": args.page,
            "page_size": args.page_size,
            "inventory": args.inventory,
        },
    )
    if not result.ok:
        print(result.data.get("error", "routing failed"), file=sys.stderr)
        return result.exit_code or 1
    payload = result.data["payload"]
    if args.json:
        _print_view_json(payload)
        return 0
    view = result.data["view"]
    filters = result.data["filters"]
    mode = presentation_mode(stream=sys.stdout)
    if mode.color:
        from rich.console import Console

        width = shutil.get_terminal_size(fallback=(100, 24)).columns
        console = Console(width=width, force_terminal=True, color_system=mode.color_system)
        console.print(
            render_routing(
                view,
                mode,
                page=filters["page"],
                page_size=filters["page_size"],
                state=filters["state"],
                provider=filters["provider"],
                text=filters["text"],
            )
        )
        return 0
    width = mode.width
    print(
        render_routing_text(
            view,
            width,
            page=filters["page"],
            page_size=filters["page_size"],
            state=filters["state"],
            provider=filters["provider"],
            text=filters["text"],
        ),
        end="",
    )
    return 0


def _context(args: argparse.Namespace) -> int:
    """CLI surface for the context budget view. Rendering stays in context_render."""
    import shutil

    from verdict.actions.registry import run_action
    from verdict.design import presentation_mode
    from verdict.orchestration.context_render import render_context, render_context_text

    if not args.run:
        print("context requires a run id or run directory", file=sys.stderr)
        return 2
    result = run_action(
        "context.view", {"run": args.run, "runs_dir": args.runs_dir, "node": args.node}
    )
    if not result.ok:
        print(result.data.get("error", "context failed"), file=sys.stderr)
        return result.exit_code or 1
    if args.json:
        _print_view_json(result.data["payload"])
        return 0
    view = result.data["view"]
    mode = presentation_mode(stream=sys.stdout)
    if mode.color:
        from rich.console import Console

        width = shutil.get_terminal_size(fallback=(100, 24)).columns
        console = Console(width=width, force_terminal=True, color_system=mode.color_system)
        console.print(render_context(view, mode))
        return 0
    print(render_context_text(view, mode.width), end="")
    return 0


def _watch(args: argparse.Namespace) -> int:
    from verdict.orchestration.tui import follow, follow_replay, render_text

    run_dir = _resolve_run(args.run, args.runs_dir)
    events = run_dir / "events.jsonl"
    if not events.exists():
        print(f"no run at {run_dir}", file=sys.stderr)
        return 2
    from verdict.design import presentation_mode

    mode = presentation_mode(stream=sys.stdout)
    node_id = getattr(args, "node", None)
    panel_name = getattr(args, "panel", None)
    if args.once:
        if node_id is not None or panel_name is not None:
            from verdict.orchestration.cockpit_controls import render_run_text

            try:
                output = render_run_text(run_dir, node_id=node_id, panel_name=panel_name, mode=mode)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            print(output, end="")
        else:
            rows = [json.loads(line) for line in events.read_text().splitlines() if line.strip()]
            print(render_text(rows, width=mode.width, plain=not mode.color))
        return 0
    if args.replay:
        view = follow_replay(events, speed=args.speed)
        return 0 if getattr(view, "outcome", "") == "COMPLETE" else 1
    interactive = sys.stdin.isatty() and sys.stdout.isatty() and mode.color
    if (node_id is not None or panel_name is not None) and not interactive:
        from verdict.orchestration.cockpit_controls import render_run_text

        try:
            print(
                render_run_text(run_dir, node_id=node_id, panel_name=panel_name, mode=mode), end=""
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return 0
    try:
        view = follow(
            events,
            stop_when_final=True,
            interactive=interactive,
            node_id=node_id,
            panel_name=panel_name,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0 if getattr(view, "outcome", "") == "COMPLETE" else 1


def _derive_no_change_nodes(run_dir: Path, receipt: Mapping[str, Any]) -> list[str]:
    """Prefer the stored field; rebuild from events for older receipts."""
    stored = [str(n) for n in receipt.get("no_change_nodes", []) if str(n).strip()]
    if stored:
        return stored
    from verdict.orchestration.contracts import OrchestrationError
    from verdict.orchestration.receipt import build_run_receipt

    try:
        fresh = build_run_receipt(run_dir)
    except (OrchestrationError, OSError, ValueError):
        return []
    return [str(n) for n in fresh.get("no_change_nodes", []) if str(n).strip()]


def _receipt(args: argparse.Namespace) -> int:
    from verdict.actions.registry import run_action

    run_dir = _resolve_run(args.run, args.runs_dir)
    path = run_dir / "receipt.json"
    if not path.exists():
        print(f"no receipt at {path}", file=sys.stderr)
        return 2
    result = run_action("run-receipt", {"run_dir": str(run_dir), "runs_dir": args.runs_dir})
    data = result.data
    if data.get("error"):
        # No receipt at path — already handled above; any other error path
        # from the action is a broken state.
        print(data["error"], file=sys.stderr)
        return 2
    outcome = data["outcome"]
    reason = data["reason"]
    problems = data["problems"]
    receipt = data["receipt"]
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
        no_change_nodes = _derive_no_change_nodes(run_dir, receipt)
        if no_change_nodes:
            n = len(no_change_nodes)
            noun = "node" if n == 1 else "nodes"
            print(f"  {n} implement {noun} changed no files")
        review = receipt.get("review", {})
        print(
            f"  review: {review.get('status')} by {review.get('reviewer')} on {review.get('route_id')}"
        )
    return 0 if outcome == "COMPLETE" and not problems else 1


_STAGE_ORDER = ("DISCOVERED", "ENTITLED", "HEALTHY", "AVAILABLE", "TASK_ELIGIBLE", "SELECTED")


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


def _verified_error(args: argparse.Namespace, message: str, *, exit_code: int) -> int:
    """Keep verified failures to one final document and never echo raw exceptions."""
    if args.json:
        _print_view_json({"error": message})
    else:
        print(f"error: {message}", file=sys.stderr)
    return exit_code


def _eligibility_verified(args: argparse.Namespace) -> int:
    """Validate before I/O, then wait for the shared consumer's final evidence."""
    incompatible = []
    if getattr(args, "probe", False):
        incompatible.append("--probe")
    if getattr(args, "scope", ""):
        incompatible.append("--scope")
    if getattr(args, "reasoning", False):
        incompatible.append("--reasoning")
    if getattr(args, "frontier", False):
        incompatible.append("--frontier")
    if getattr(args, "provider_family", []):
        incompatible.append("--provider-family")
    if getattr(args, "_prefer_explicit", False) or getattr(args, "prefer", "claude") != "claude":
        incompatible.append("--prefer")
    if incompatible:
        return _verified_error(
            args, "--verified cannot be combined with " + ", ".join(incompatible), exit_code=2
        )

    from verdict.orchestration.verified_models import (
        EvidenceSnapshots,
        VerifiedModelQuery,
        project_verified_models,
    )

    query = VerifiedModelQuery(
        status=getattr(args, "status", None),
        provider=getattr(args, "provider", None),
        search=getattr(args, "search", None),
        page=getattr(args, "page", 1),
        page_size=getattr(args, "page_size", 50),
    )
    try:
        # The empty projection shares canonical validation without reading any sources.
        project_verified_models(
            [], [], EvidenceSnapshots(), now=datetime.now(timezone.utc), query=query
        )
    except ValueError:
        return _verified_error(args, "invalid verified model filters or paging", exit_code=2)

    from verdict.actions.verified_models import consume_verified_models

    try:
        result = consume_verified_models(
            gateway=args.gateway,
            query=query,
            no_refresh=bool(getattr(args, "no_refresh", False)),
            read_line=input,
            write=lambda line: print(line, file=sys.stderr),
            live=True,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        return _verified_error(args, f"verified models failed ({type(exc).__name__})", exit_code=1)
    except KeyboardInterrupt:
        return _verified_error(args, "verified models cancelled", exit_code=130)

    if args.json:
        _print_view_json(result.data)
    elif not result.ok:
        print(f"error: {result.data.get('error', 'verified models failed')}", file=sys.stderr)
    else:
        from verdict.orchestration.verified_models_render import render_verified_plain

        _page(render_verified_plain(result.data), no_pager=bool(getattr(args, "no_pager", False)))
    return 0 if result.ok else result.exit_code or 1


def _eligibility(args: argparse.Namespace) -> int:
    if getattr(args, "verified", False):
        return _eligibility_verified(args)

    from verdict.actions.registry import run_action

    result = run_action(
        "eligibility",
        {
            "gateway": args.gateway,
            "scope": args.scope,
            "prefer": args.prefer,
            "provider_family": list(getattr(args, "provider_family", []) or []),
            "reasoning": bool(args.reasoning),
            "frontier": bool(args.frontier),
            "probe": bool(args.probe),
            "sync_visibility": bool(getattr(args, "sync_visibility", False)),
        },
    )
    payload = result.data
    if not result.ok:
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"error: {payload.get('error', 'eligibility failed')}", file=sys.stderr)
        return result.exit_code or 1
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    _page(render_eligibility_text(payload), no_pager=bool(getattr(args, "no_pager", False)))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="verdict orchestration")
    subparsers = parser.add_subparsers(dest="command")
    add_parsers(subparsers)
    args = parser.parse_args()
    code = dispatch(args)
    sys.exit(code or 0)
