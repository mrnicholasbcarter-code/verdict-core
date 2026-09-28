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
    orch.add_argument(
        "--executor",
        choices=("prime", "direct-gateway"),
        default="prime",
        help="Worker executor backend (default: prime)",
    )

    watch = subparsers.add_parser("watch", help="Live view of a Verdict orchestration run")
    watch.add_argument("run", help="Run id or run directory")
    watch.add_argument("--runs-dir", default=str(DEFAULT_RUNS))
    watch.add_argument("--once", action="store_true", help="Render the current state and exit")
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


def prime_visibility_report(live_rows: Any, *, prime_home: Path | None = None) -> dict[str, Any]:
    """Compare live concrete gateway models with Prime's registry. Never writes."""
    from verdict.harness_prime import resolve_paths
    from verdict.prime_inventory import _read, concrete_rows, sidecar_path

    paths = resolve_paths(prime_home=prime_home)
    live_concrete, excluded = concrete_rows(live_rows)
    live = {str(row["id"]) for row in live_concrete}
    visible: set[str] = set()
    registry_error = None
    try:
        data = json.loads(paths.models.read_text(encoding="utf-8"))
        models = data["providers"]["omniroute"]["models"]
        visible = {str(m["id"]) for m in models if isinstance(m, dict) and m.get("id")}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        registry_error = type(exc).__name__
    snapshot, failure = _read(sidecar_path(paths.models))
    return {
        "schema": "verdict.prime-visibility/v1",
        "registry": str(paths.models),
        "registry_error": registry_error,
        "live_count": len(live),
        "live_opaque_excluded": len(excluded),
        "prime_visible_count": len(visible),
        "live_not_visible": sorted(live - visible),
        "visible_not_live": sorted(visible - live),
        "in_sync": registry_error is None and live == visible,
        "last_refresh": snapshot.to_dict() | {"route_ids": None} if snapshot else None,
        "last_failure": failure.to_dict() if failure else None,
    }


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
    required_capabilities: frozenset[str] = frozenset(),
    min_context_tokens: int = 0,
) -> Any:
    """Admitted eligibility ladder over the live gateway.

    ``required_capabilities`` / ``min_context_tokens`` are the floor every
    selection from this ladder needs. They go into canonical admission so
    CAPABILITY drops appear in the receipt. The ladder still applies each
    request's own requirements later.
    """
    from verdict.admission import active_controller_route, admit, default_runtime_evidence
    from verdict.orchestration.eligibility import EligibilityLadder
    from verdict.orchestration.run import fetch_connections, fetch_inventory, resolve_api_key
    from verdict.subagent_selection import LaunchCandidate, openai_health_probe

    key = resolve_api_key()
    rows = fetch_inventory(gateway, api_key=key)
    connections = fetch_connections(gateway, api_key=key)
    state_path = state_file or (_state_dir() / "orchestration-health.json")
    now = datetime.now(timezone.utc)
    # Canonical admission over the full live inventory first. Scope, provider
    # family and the active controller are extra narrowing on that set.
    admitted = admit(
        rows,
        connections,
        # Evidence comes from the ladder state file this ladder reads, so the
        # admitted set and the ladder cannot disagree about cached health.
        default_runtime_evidence(now=now, state_dir=state_path.parent, ladder_state=state_path),
        now=now,
        required_capabilities=required_capabilities,
        min_context_tokens=min_context_tokens,
    )
    prefixes = tuple(p.strip() for p in scope.split(",") if p.strip())
    admitted = admitted.restrict_prefixes(prefixes).restrict_families(provider_families)
    admitted = admitted.exclude_controller(active_controller_route())
    receipt_path = state_path.parent / "admission-latest.json"
    admitted.write_receipt(receipt_path)
    # Prime visibility is refreshed from the FULL live catalog. Scope and
    # provider-family filters only narrow what this ladder evaluates; they
    # must never shrink the operator's Prime registry to the scoped subset.
    catalog_rows = list(rows)
    if prefixes:
        rows = [r for r in rows if str(r.get("id", "")).startswith(prefixes)]
    if provider_families:
        rows = [r for r in rows if route_prefix(str(r.get("id", ""))) in provider_families]
    raw_probe = openai_health_probe(gateway.rstrip("/") + "/v1", api_key=key, timeout_seconds=30)

    def probe(route_id: str) -> Any:
        return raw_probe(
            LaunchCandidate(route_id, route_id, frozenset(), 0, 0.0, 0.0, False, False, 0, 0)
        )

    return EligibilityLadder(
        rows,
        connections,
        probe,
        state_path,
        prefer_providers=tuple(p.strip() for p in prefer.split(",") if p.strip()),
        load=load,
        harness_visible=prime_visibility(live_rows=catalog_rows),
        admitted=admitted,
        admission_receipt=receipt_path,
    )


def route_prefix(route_id: str) -> str:
    """Provider family of a gateway route id: the prefix before '/' (``cc/x`` -> ``cc``)."""
    return route_id.split("/", 1)[0].lower() if "/" in route_id else ""


def parse_provider_families(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """``["cc,kr", "gc"]`` -> ``("cc", "gc", "kr")`` (deduplicated, sorted, lowercase)."""
    found = {part.strip().lower() for value in values for part in value.split(",")}
    return tuple(sorted(f for f in found if f))


def prime_visibility(path: Path | None = None, *, live_rows: Any = None) -> Any:
    """Return the concrete ids Prime can actually spawn, never gateway authority.

    When the caller has just refreshed ``/v1/models``, it supplies those rows
    here so the local Prime registry is synchronized before selection. A failed
    sync leaves an LKG sidecar for audit but does not invent visibility: only
    the concrete ids read from Prime's registry are returned. Health, capacity,
    entitlement and launch authority stay in their separate gates.
    """
    from verdict.harness_prime import refresh_omniroute_visibility
    from verdict.orchestration.eligibility import HarnessVisibility

    registry = path or Path.home() / ".prime" / "agent" / "models.json"
    if live_rows is not None:
        rows = tuple(row for row in live_rows if isinstance(row, Mapping))
        # A successful refresh changes Prime's local registry before selection.
        # On failure its LKG remains auditable, but only registry ids below can
        # be claimed as actually spawn-visible.
        refresh_omniroute_visibility(
            prime_home=registry.parent,
            source="omniroute:/v1/models",
            fetch_rows=lambda: rows,
            force=True,
        )
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
        models = data["providers"]["omniroute"]["models"]
        visible = frozenset(str(m["id"]) for m in models if isinstance(m, dict) and m.get("id"))
    except (OSError, ValueError, KeyError, TypeError):
        return HarnessVisibility(None, source="none")
    return HarnessVisibility(visible, source="models.json")


def _executor(args: argparse.Namespace) -> WorkerExecutor:
    from verdict.orchestration.executors import (
        DirectGatewayExecutor,
        FaultInjectingExecutor,
        PrimeHeadlessExecutor,
    )

    executor: WorkerExecutor
    if getattr(args, "executor", "prime") == "direct-gateway":
        executor = DirectGatewayExecutor(
            base_url=args.gateway,
            api_key=os.environ.get("OPENAI_API_KEY", ""),
        )
    else:
        executor = PrimeHeadlessExecutor()
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
    from verdict.orchestration.tui import follow, follow_replay, render_text

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
    if args.replay:
        view = follow_replay(events, speed=args.speed)
        return 0 if getattr(view, "outcome", "") == "COMPLETE" else 1
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
    requirements = TaskRequirements(
        required_capabilities=frozenset({"tools"}),
        coding=True,
        reasoning=args.reasoning,
        frontier_worthy=args.frontier,
    )
    selector = build_selector(
        args.gateway,
        scope=args.scope,
        prefer=args.prefer,
        provider_families=families,
        required_capabilities=requirements.required_capabilities,
        min_context_tokens=requirements.min_context_tokens,
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="verdict orchestration")
    subparsers = parser.add_subparsers(dest="command")
    add_parsers(subparsers)
    args = parser.parse_args()
    code = dispatch(args)
    sys.exit(code or 0)
