"""Action registry — the single lookup table for CLI + TUI convergence.

Every action delegates to an existing domain/application service. No business
logic lives here.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

from verdict.actions.base import (
    NOOP_SINK,
    ActionEvent,
    ActionResult,
    ActionSink,
    ActionSpec,
    LaunchSpec,
)

# ---------------------------------------------------------------------------
# Registry storage
# ---------------------------------------------------------------------------

_ACTIONS: dict[str, tuple[ActionSpec, Callable[..., ActionResult]]] = {}


def register(spec: ActionSpec, fn: Callable[..., ActionResult]) -> None:
    """Register an action; overwrites silently for test-time patching."""
    _ACTIONS[spec.name] = (spec, fn)


def get_action(name: str) -> tuple[ActionSpec, Callable[..., ActionResult]] | None:
    return _ACTIONS.get(name)


def list_actions() -> list[ActionSpec]:
    return [spec for spec, _ in _ACTIONS.values()]


def run_action(
    name: str, params: dict[str, Any] | None = None, sink: ActionSink | None = None
) -> ActionResult:
    """Run a named action, emitting events to *sink*."""
    sink = sink or NOOP_SINK
    entry = _ACTIONS.get(name)
    if entry is None:
        result = ActionResult(data={"error": f"unknown action: {name}"}, ok=False, exit_code=127)
        sink.on_event(ActionEvent(kind="error", action=name, detail={"error": "not found"}))
        return result
    _spec, fn = entry
    sink.on_event(ActionEvent(kind="start", action=name))
    try:
        result = fn(**(params or {}))
        sink.on_event(ActionEvent(kind="done", action=name, detail={"ok": result.ok}))
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        result = ActionResult(
            data={"error": "action raised SystemExit", "code": code}, ok=False, exit_code=code
        )
        sink.on_event(ActionEvent(kind="error", action=name, detail={"exit_code": code}))
    except Exception as exc:
        result = ActionResult(data={"error": str(exc)}, ok=False, exit_code=1)
        sink.on_event(ActionEvent(kind="error", action=name, detail={"exception": str(exc)}))
    return result


# ---------------------------------------------------------------------------
# Per-subcommand classification (top-level + dotted leaves)
# ---------------------------------------------------------------------------
#
# Every reason string is written "<handler_name>: <what it does>; <why bucket>".
# The <handler_name> is the function argparse dispatch actually calls for that
# leaf, resolvable via verdict/commands/dispatch.py or the parser defaults.
#
# MACHINE_ONLY is restricted to three kinds only:
#   (a) server/daemon lifecycle (serve, a daemon start/stop),
#   (b) a programmatic endpoint invoked by other programs (hook handler a
#       harness calls, an MCP stdio server),
#   (c) destructive global uninstall.
#
# ALIAS entries map a compatibility command to the action it forwards to; the
# CLI alias goes through the same action, the palette lists the target once,
# and the test verifies the alias reaches the same action as its target.
#
# LaunchSpec.entry MUST NOT point into verdict.cli, verdict.commands or
# verdict.orchestration.cli (that would make the TUI depend on the CLI layer).
# Each LaunchSpec.entry points at a domain function the CLI handler calls.

MACHINE_ONLY: dict[str, str] = {
    "serve": (
        "cmd_serve (verdict.api.start_server): runs the FastAPI /v1/route "
        "and /v1/chat/completions gateway under uvicorn until stopped; "
        "server lifecycle, not an interactive action"
    ),
    "mcp.serve": (
        "cmd_mcp (mcp_server.main): stdio Model Context Protocol server "
        "consumed by editor integrations over stdin/stdout; "
        "programmatic endpoint, not an interactive action"
    ),
    "hook.claude-gate": (
        "cmd_hook: catalog-qualification gate invoked by a Claude Code "
        "PreToolUse hook script per LLM tool call; "
        "programmatic endpoint, not an interactive action"
    ),
    "prove-at-rest.daemon": (
        "cmd_prove_at_rest (build_live_daemon.run_forever): continuous "
        "prove-at-rest probe loop that runs until SIGINT; "
        "daemon lifecycle, not an interactive action"
    ),
    "uninstall": (
        "cmd_uninstall (uninstall_memory_bridge): removes memory bridge "
        "hooks, MCP registrations, and optionally purges the .verdict "
        "data directory globally; destructive global uninstall"
    ),
}

ALIAS: dict[str, str] = {
    # command -> target action name
    "run": "route",
    "plan": "setup.plan",
}

LAUNCH: dict[str, LaunchSpec] = {
    "orchestrate": LaunchSpec(
        reason=(
            "_orchestrate (run_golden_path): plans a WorkGraph, dispatches "
            "parallel workers, and writes a signed receipt over minutes to "
            "hours; long-running interactive TUI"
        ),
        entry="verdict.orchestration.run:run_golden_path",
        section="Orchestration",
    ),
    "watch": LaunchSpec(
        reason=(
            "_watch (tui.follow): live-follows a running orchestration's "
            "events.jsonl and renders the TUI until the run terminates; "
            "long-running interactive view"
        ),
        entry="verdict.orchestration.tui:follow",
        section="Orchestration",
    ),
    "supervise": LaunchSpec(
        reason=(
            "dispatch (supervise_run): spawns and restarts an orchestrate "
            "controller process against a stall and deadline budget; "
            "long-running supervisor loop"
        ),
        entry="verdict.actions.launch:supervise_run",
        section="Orchestration",
    ),
    "setup": LaunchSpec(
        reason=(
            "cmd_setup (present_bootstrap): interactive capability "
            "bootstrap wizard that prompts for consent and applies "
            "provider/gateway/harness enablement; interactive setup wizard"
        ),
        entry="verdict.setup_presentation:present_bootstrap",
        section="Setup",
    ),
    "ui": LaunchSpec(
        reason=(
            "cmd_ui (launch_dashboard): launches the Streamlit analytics "
            "dashboard as a separate long-running web server process for "
            "interactive exploration; long-running web UI"
        ),
        entry="verdict.actions.launch:launch_dashboard",
        section="Monitoring",
    ),
    "quickstart": LaunchSpec(
        reason=(
            "cmd_quickstart (flagship_demo.run_demo): renders the "
            "credential-free flagship walkthrough with staged output "
            "meant for a human to read; long-running interactive demo"
        ),
        entry="verdict.flagship_demo:run_demo",
        section="Setup",
    ),
    "benchmark": LaunchSpec(
        reason=(
            "cmd_benchmark (run_reproducible_benchmarks/run_savings_bench): "
            "runs the reproducible or paired-live routing benchmark suite "
            "over many scenarios; long-running batch benchmark"
        ),
        entry="verdict.benchmarking:run_reproducible_benchmarks",
        section="Development",
    ),
    "autodev": LaunchSpec(
        reason=(
            "cmd_autodev (autodev_run.run_autodev): decomposes an objective "
            "into work units, edits the working tree via live models, and "
            "records the outcome; long-running interactive pipeline"
        ),
        entry="verdict.autodev_run:run_autodev",
        section="Development",
    ),
    "autodev-golden-path": LaunchSpec(
        reason=(
            "cmd_autodev_golden_path (golden_path.run_golden_path): three-"
            "stage offline autodev acceptance path that edits a repo and "
            "runs verification commands; long-running interactive pipeline"
        ),
        entry="verdict.golden_path:run_golden_path",
        section="Development",
    ),
    "autodev.packet.execute": LaunchSpec(
        reason=(
            "cmd_autodev_packet_execute (run_packet_autodev): executes an "
            "admitted autodev packet against a live route, edits the "
            "working tree, and writes receipts; long-running interactive "
            "pipeline"
        ),
        entry="verdict.autodev_run:run_packet_autodev",
        section="Development",
    ),
}

# All ACTION classifications are inferred from _register_builtins() below.
# Every leaf that is not in MACHINE_ONLY, ALIAS, or LAUNCH must be a registered
# action; the exclusion test enforces that invariant across the argparse tree.

GAP: dict[str, str] = {}  # No gaps currently; every leaf is classified.

# ---------------------------------------------------------------------------
# Built-in action implementations (lazy-import to avoid import-time overhead)
# ---------------------------------------------------------------------------


def _action_models_list(**kwargs: Any) -> ActionResult:
    """List the authoritative model inventory from the live gateway + metadata store.

    Sources the qualified OmniRoute catalog joined with the metadata store (the
    same sources ``verdict catalog`` and ``verdict eligibility`` use), plus
    configured primary/config models marked as such.  Falls back to config-only
    when the gateway is unreachable.

    Accepts filter kwargs: ``provider``, ``search``, ``capability``, ``limit``,
    ``show_all``.  Filtering happens on the in-memory snapshot.
    """
    from verdict.actions.helpers import inventory_model_catalog

    # Allow tests to inject a pre-built catalog (same escape hatch as before).
    injected: list[dict[str, Any]] | None = kwargs.get("catalog_rows")
    # Legacy compat: old callers pass "catalog" as a list of ModelInfo objects.
    legacy_catalog = kwargs.get("catalog")
    if legacy_catalog is not None and injected is None:
        injected = [
            {
                "id": getattr(m, "id", ""),
                "provider": getattr(m, "provider", "unknown"),
                "capability_tier": f"T{getattr(m, 'capability_tier', '?')}",
                "context_window": getattr(m, "context_window", None),
                "tools_support": None,
                "structured_output": None,
                "input_cost_per_million": None,
                "output_cost_per_million": None,
                "source": "config",
                "freshness": None,
            }
            for m in legacy_catalog
        ]
    if injected is not None:
        rows = injected
        inventory_error: str | None = None
    else:
        rows, inventory_error = inventory_model_catalog()

    # Apply filters (all optional).
    provider_filter: str = (kwargs.get("provider") or "").strip().lower()
    search_filter: str = (kwargs.get("search") or "").strip().lower()
    capability_filter: str = (kwargs.get("capability") or "").strip().lower()
    limit: int = int(kwargs.get("limit") or 0)
    show_all: bool = bool(kwargs.get("show_all", False))

    filtered = rows
    if provider_filter:
        filtered = [r for r in filtered if provider_filter in str(r.get("provider", "")).lower()]
    if search_filter:
        filtered = [r for r in filtered if search_filter in str(r.get("id", "")).lower()]
    if capability_filter:
        filtered = [
            r
            for r in filtered
            if capability_filter in str(r.get("capability_tier", "")).lower()
            or (capability_filter == "tools" and r.get("tools_support") is True)
            or (capability_filter == "structured" and r.get("structured_output") is True)
            or (
                capability_filter == "reasoning"
                and r.get("capability_tier", "").lower() == "reasoning"
            )
        ]

    # Sort: configured models first, then by provider + id.
    def _sort_key(r: dict[str, Any]) -> tuple[int, str, str]:
        source_order = 0 if r.get("source") == "config" else 1
        return (source_order, str(r.get("provider", "")), str(r.get("id", "")))

    filtered.sort(key=_sort_key)

    # Summary stats from the FULL (unfiltered) set.
    provider_counts: dict[str, int] = {}
    for r in rows:
        p = str(r.get("provider", "unknown"))
        provider_counts[p] = provider_counts.get(p, 0) + 1

    freshness = rows[0].get("freshness") if rows else None

    # Apply limit (default 40 unless --all).
    total_filtered = len(filtered)
    if not show_all and limit <= 0:
        limit = 40
    if limit > 0 and not show_all:
        filtered = filtered[:limit]

    data: dict[str, Any] = {
        "models": filtered,
        "total": len(rows),
        "total_filtered": total_filtered,
        "shown": len(filtered),
        "provider_counts": provider_counts,
        "freshness": freshness,
        "source": "config_only" if inventory_error else "inventory",
        "inventory_error": inventory_error,
    }
    return ActionResult(data=data)


def _action_route(**kwargs: Any) -> ActionResult:
    """Route a single task through the gate (planning step).

    Callers may pass ``gate`` explicitly (the CLI does so, preserving its
    long-standing ``_build_route_gate`` monkeypatch surface); otherwise the
    action builds one via ``helpers.build_route_gate``.  Returns the raw
    ``decision`` and (when requested) the ``selection`` for the presenter;
    the CLI owns any transport execution (``_execute_cli_decision``).

    ``terse=True`` calls only ``gate.route`` to preserve the legacy escape
    surface for tests that stub a route-only gate.
    """
    from verdict.serve_path import CONTEXT_ALLOW_LEGACY

    task: str = kwargs["task"]
    criticality: str = kwargs.get("criticality", "medium")
    allow_offline: bool = kwargs.get("allow_offline", False)
    allow_legacy_selector: bool = bool(kwargs.get("allow_legacy_selector") or False)
    terse: bool = bool(kwargs.get("terse", False))

    context: dict[str, object] = {}
    if allow_legacy_selector:
        context[CONTEXT_ALLOW_LEGACY] = True

    gate = kwargs.get("gate")
    if gate is None:
        from verdict.actions.helpers import build_route_gate

        gate = build_route_gate(allow_offline=allow_offline)
    if terse:
        dec = gate.route(task, criticality, context=context or None)
        return ActionResult(data={"task": task, "gate": gate, "decision": dec, "selection": None})
    dec, selection = gate.route_with_strategy(task, criticality, context=context or None)
    return ActionResult(data={"task": task, "gate": gate, "decision": dec, "selection": selection})


def _action_doctor(**kwargs: Any) -> ActionResult:
    """Scan Verdict setup and connections."""
    from verdict.actions.helpers import collect_doctor_diagnostics

    preflight_timeout: float = kwargs.get("preflight_timeout", 120.0)
    fix: bool = kwargs.get("fix", False)
    diag = collect_doctor_diagnostics(fix, interactive=False, preflight_timeout=preflight_timeout)

    data: dict[str, Any] = {
        "status": "issues_found" if diag.issues else "ok",
        "issues": diag.issues,
        "warnings": diag.warnings,
        "repaired": diag.fixed,
        "sections": [
            {"label": label, "state": state, "detail": detail}
            for label, state, detail in diag.sections
        ],
        "documentation_preflight": diag.documentation_preflight,
        "gateway_lifecycle": diag.gateway_lifecycle,
        "shared_memory": diag.shared_memory,
        "capability_bootstrap": diag.capability_report,
    }
    return ActionResult(data=data, ok=not diag.issues, exit_code=1 if diag.issues else 0)


def _action_probe(**kwargs: Any) -> ActionResult:
    """Run bounded one-token probes."""
    from verdict.probes import ProbePolicy, ProbeRunner

    models: list[str] = kwargs["models"]
    base_url: str = kwargs.get("base_url", "http://localhost:20128/v1")
    timeout: float = kwargs.get("timeout", 20.0)
    transport = kwargs.get("transport")
    allow_live_probe: bool = kwargs.get("allow_live_probe", False)

    is_injected = transport is not None
    if not is_injected and not allow_live_probe:
        return ActionResult(
            data={
                "diagnostics": None,
                "error": "live probes require explicit consent; pass --allow-live-probe",
            },
            ok=False,
            exit_code=2,
        )

    if transport is None:
        import os

        from verdict.probes import openai_probe_transport

        transport = openai_probe_transport(base_url, api_key=os.getenv("OPENAI_API_KEY"))

    provider_name = "fixture" if is_injected else "omniroute"
    run = ProbeRunner(ProbePolicy(timeout_seconds=timeout)).run_with_diagnostics(
        models,
        transport,
        live=not is_injected,
        consented=allow_live_probe if not is_injected else False,
        provider=provider_name,
    )

    from verdict.actions.helpers import probe_result_payload

    results = [probe_result_payload(obs) for obs in run.observations]
    data = {"diagnostics": run.diagnostics.to_dict(), "results": results}
    all_ok = all(e.get("ok") for e in results)
    return ActionResult(data=data, ok=all_ok, exit_code=0 if all_ok else 1)


def _action_setup_plan(**kwargs: Any) -> ActionResult:
    """Print the mutation-free setup plan."""
    from verdict.setup_plan import build_setup_plan
    from verdict.shared_memory import discover_shared_memory_setup

    plan = build_setup_plan().to_dict()
    plan["shared_memory"] = discover_shared_memory_setup()
    return ActionResult(data=plan)


def _action_receipt_show(**kwargs: Any) -> ActionResult:
    """Routing receipt store (RoutingReceiptV1) — list, show, or export.

    Mirrors cmd_receipt's semantics: resolves the ReceiptStore path,
    runs the requested action, and returns rich data for the CLI presenter.
    ``action`` selects one of ``list`` / ``show`` / ``export``.
    """
    from pathlib import Path as _Path

    from verdict.receipt_store import ReceiptStore
    from verdict.routing_receipt import attempt_scope, human_summary, load_routing_receipt

    action: str = kwargs.get("action", "list")
    receipt_id = kwargs.get("receipt_id")
    attempt_id = kwargs.get("attempt_id")
    scope = kwargs.get("scope")
    db_path = kwargs.get("db_path")

    if db_path:
        db = _Path(db_path)
    else:
        repo_db = _Path.cwd() / ".verdict" / "receipts.db"
        db = repo_db if repo_db.exists() else (_Path.home() / ".verdict" / "receipts.db")
    store = (
        ReceiptStore(db, strict_scope=False)
        if action == "list" and scope is None
        else ReceiptStore(db, strict_scope=True)
    )

    if action == "list":
        rows = store.query_receipts(receipt_type="decision", scope=scope, limit=100)
        items = []
        for row in rows:
            if row.parent_receipt_id:
                continue
            payload = row.payload
            if payload.get("schema_version") != "routing-receipt/v1":
                continue
            latest = load_routing_receipt(
                store,
                receipt_id=row.receipt_id,
                scope=row.scope,
                attempt_id=payload.get("attempt_id"),
            )
            view = latest.to_dict() if latest is not None else payload
            items.append(
                {
                    "receipt_id": row.receipt_id,
                    "scope": row.scope,
                    "attempt_id": view.get("attempt_id"),
                    "state": view.get("state"),
                    "decision_digest": view.get("decision_digest"),
                    "created_at": view.get("created_at"),
                }
            )
        return ActionResult(data={"action": "list", "receipts": items})

    if action == "show":
        scope_value = scope
        if scope_value is None and attempt_id is not None:
            scope_value = attempt_scope(story_id=None, work_unit_id=None, attempt_id=attempt_id)
        receipt = load_routing_receipt(
            store, receipt_id=receipt_id, scope=scope_value, attempt_id=attempt_id
        )
        if receipt is None and attempt_id is not None and scope is None:
            for row in store.query_receipts(receipt_type="decision", limit=500):
                if row.parent_receipt_id:
                    continue
                if row.idempotency_key == attempt_id or row.payload.get("attempt_id") == attempt_id:
                    receipt = load_routing_receipt(
                        store, receipt_id=row.receipt_id, scope=row.scope, attempt_id=attempt_id
                    )
                    break
        if receipt is None:
            return ActionResult(data={"action": "show", "status": "missing"}, ok=False, exit_code=1)
        return ActionResult(
            data={"action": "show", "receipt": receipt.to_dict(), "summary": human_summary(receipt)}
        )

    if action == "export":
        receipt = load_routing_receipt(
            store, receipt_id=receipt_id, scope=scope, attempt_id=attempt_id
        )
        if receipt is None:
            return ActionResult(
                data={"action": "export", "status": "missing"}, ok=False, exit_code=1
            )
        return ActionResult(data={"action": "export", "receipt": receipt.to_dict()})

    return ActionResult(data={"error": f"unknown receipt action: {action}"}, ok=False, exit_code=1)


def _action_eligibility(**kwargs: Any) -> ActionResult:
    """Evaluate the DISCOVERED→SELECTED eligibility ladder."""
    import os
    from datetime import datetime, timezone

    from verdict.free_tier_admit import normalize_omniroute_origin
    from verdict.orchestration.contracts import TaskRequirements
    from verdict.orchestration.eligibility_report import (
        build_selector,
        eligibility_payload,
        parse_provider_families,
    )

    gateway = normalize_omniroute_origin(
        kwargs.get("gateway", os.environ.get("VERDICT_GATEWAY", "http://127.0.0.1:20128"))
    )
    scope: str = kwargs.get("scope", "")
    prefer: str = kwargs.get("prefer", "claude")
    provider_family: list[str] = kwargs.get("provider_family", [])
    reasoning: bool = kwargs.get("reasoning", False)
    frontier: bool = kwargs.get("frontier", False)
    do_probe: bool = kwargs.get("probe", False)

    families = parse_provider_families(provider_family)
    requirements = TaskRequirements(
        required_capabilities=frozenset({"tools"}),
        coding=True,
        reasoning=reasoning,
        frontier_worthy=frontier,
    )
    selector = build_selector(
        gateway,
        scope=scope,
        prefer=prefer,
        provider_families=families,
        required_capabilities=requirements.required_capabilities,
        min_context_tokens=requirements.min_context_tokens,
    )
    now = datetime.now(timezone.utc)
    if do_probe:
        chosen, verdicts = selector.select(requirements, now=now)
    else:
        chosen, verdicts = None, selector.evaluate(requirements, now=now)
    filters = {
        "provider_family": list(families),
        "scope": [p.strip() for p in scope.split(",") if p.strip()],
    }
    payload = eligibility_payload(verdicts, selector.summary(), chosen, filters)
    return ActionResult(data=payload)


def _action_config_show(**kwargs: Any) -> ActionResult:
    """Read-only view of current configuration."""
    import os
    from pathlib import Path

    config_file = Path.home() / ".config" / "verdict" / "verdict.yaml"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        config_file = Path(xdg) / "verdict" / "verdict.yaml"

    data: dict[str, Any] = {
        "config_file": str(config_file),
        "exists": config_file.exists(),
        "gateway": os.environ.get("VERDICT_GATEWAY", "http://127.0.0.1:20128"),
        "profile": os.environ.get("LLMGATE_AVAILABILITY_PROFILE", "default"),
        "plain": os.environ.get("VERDICT_PLAIN", ""),
        "no_animation": os.environ.get("VERDICT_NO_ANIMATION", ""),
        "no_color": "NO_COLOR" in os.environ,
        "ci": bool(os.environ.get("CI")),
    }
    if config_file.exists():
        try:
            import yaml

            from verdict.contracts import redact_contract_secrets

            # Config files can hold credentials (api_key, token, password, *_secret):
            # never return them to the CLI or the TUI.
            data["config"] = redact_contract_secrets(yaml.safe_load(config_file.read_text()) or {})
        except Exception as exc:
            data["config_error"] = type(exc).__name__
    return ActionResult(data=data)


def _action_credentials_list(**kwargs: Any) -> ActionResult:
    """List all registered credentials."""
    from verdict.credentials_registry import CREDENTIALS
    from verdict.credentials_store import CredentialsStore, get_credential_source

    store = CredentialsStore()
    results = []
    for cred in CREDENTIALS:
        source, masked = get_credential_source(cred.env_name, store)
        results.append(
            {
                "name": cred.env_name,
                "source": source,
                "value": masked,
                "purpose": cred.purpose,
                "optional": cred.optional,
            }
        )
    return ActionResult(data=results)


def _action_credentials_set(**kwargs: Any) -> ActionResult:
    """Set a credential through the real CredentialsStore authority."""
    from verdict.credentials_registry import get_credential
    from verdict.credentials_store import CredentialsStore

    name: str = kwargs["name"]
    value: str = kwargs["value"]
    force_unregistered: bool = kwargs.get("force_unregistered", False)

    cred = get_credential(name)
    if cred is None and not force_unregistered:
        return ActionResult(
            data={"error": f"{name} is not in the registry; use force_unregistered=True"},
            ok=False,
            exit_code=1,
        )
    if not value:
        return ActionResult(
            data={"error": "credential value cannot be empty"}, ok=False, exit_code=1
        )
    store = CredentialsStore()
    store.set(name, value)
    return ActionResult(data={"name": name, "status": "set"})


def _action_credentials_unset(**kwargs: Any) -> ActionResult:
    """Remove a credential through the real CredentialsStore authority."""
    from verdict.credentials_store import CredentialsStore

    name: str = kwargs["name"]
    store = CredentialsStore()
    removed = store.unset(name)
    return ActionResult(
        data={"name": name, "status": "removed" if removed else "not_found"}, ok=True
    )


# ---------------------------------------------------------------------------
# NEW actions (F3)
# ---------------------------------------------------------------------------


def _action_run_receipt(**kwargs: Any) -> ActionResult:
    """Verify an orchestration receipt — the proof surface."""
    import json as _json

    from verdict.orchestration.receipt import completion_verdict, verify_run_receipt

    run_dir_str: str = kwargs["run_dir"]
    runs_dir: str = kwargs.get("runs_dir", ".verdict/runs")

    from verdict.actions.views import _resolve_run

    run_path = _resolve_run(run_dir_str, runs_dir)
    receipt_path = run_path / "receipt.json"
    if not receipt_path.exists():
        return ActionResult(data={"error": f"no receipt at {receipt_path}"}, ok=False, exit_code=2)
    receipt = _json.loads(receipt_path.read_text())
    problems = verify_run_receipt(run_path)
    outcome, reason = completion_verdict(receipt)

    events_path = run_path / "events.jsonl"
    traces: list[dict[str, Any]] = []
    if events_path.exists():
        for line in events_path.read_text().splitlines()[:100]:
            with contextlib.suppress(ValueError):
                traces.append(_json.loads(line))

    data = {
        "outcome": outcome,
        "reason": reason,
        "problems": problems,
        "receipt": receipt,
        "trace_count": len(traces),
        "traces_preview": traces[:10],
    }
    return ActionResult(
        data=data,
        ok=(outcome == "COMPLETE" and not problems),
        exit_code=0 if (outcome == "COMPLETE" and not problems) else 1,
    )


def _action_compare(**kwargs: Any) -> ActionResult:
    """Compare a DIRECT frontier call against the Verdict route (issue #265).

    Returns ``{"comparison_report": <report.to_dict()>}`` — the CLI's --json
    surface — computed via ComparisonHarness.  The CLI may pass ``gate``
    explicitly (preserving its ``_build_route_gate`` monkeypatch surface).
    """
    from verdict.comparison import ComparisonHarness

    task: str = kwargs["task"]
    criticality: str = kwargs.get("criticality", "medium")
    allow_offline: bool = kwargs.get("allow_offline", False)

    gate = kwargs.get("gate")
    if gate is None:
        from verdict.actions.helpers import build_route_gate

        gate = build_route_gate(allow_offline=allow_offline)
    harness = ComparisonHarness(gate=gate)
    report = harness.compare(task, criticality=criticality)
    return ActionResult(data={"comparison_report": report.to_dict()})


def _action_catalog(**kwargs: Any) -> ActionResult:
    """Qualify one or both documented OmniRoute catalog projections.

    Mirrors cmd_catalog: honours the --allow-live-probe consent gate, fetches
    the requested projection(s), qualifies them, optionally reconciles and
    probes, and returns the exact ``report_payload`` shape the CLI prints as
    JSON.  Returns ``ok=False`` for refusal (exit 2) or qualification failure
    (exit 1); the CLI translates those to matching human messages.
    """
    import os as _os
    import urllib.request as _urllib

    from verdict.omniroute_catalog import (
        CATALOG_FETCH_TIMEOUT_SECONDS,
        CatalogQualificationReport,
        probe_catalog,
        qualify_catalog,
        reconcile_catalog_projections,
        store_qualification,
    )

    base_url: str = kwargs["base_url"]
    management: bool = bool(kwargs.get("management", False))
    expected_rows: int = int(kwargs.get("expected_rows", 0))
    freshness_seconds: int = int(kwargs.get("freshness_seconds", 0))
    db_path = kwargs.get("db_path")
    do_probe: bool = bool(kwargs.get("probe", False))
    probe_limit: int = int(kwargs.get("probe_limit", 0))
    probe_timeout: float = float(kwargs.get("probe_timeout", 0.0))
    allow_live_probe: bool = bool(kwargs.get("allow_live_probe", False))

    if do_probe and not allow_live_probe:
        return ActionResult(
            data={
                "status": "refused",
                "error": "catalog live probes require explicit consent; pass --allow-live-probe",
                "probes": None,
            },
            ok=False,
            exit_code=2,
        )

    paths = [
        (
            "management" if management else "public",
            "/api/models/catalog" if management else "/v1/models",
        )
    ]
    if not management:
        paths.append(("management", "/api/models/catalog"))
    reports: dict[str, CatalogQualificationReport] = {}
    payloads: dict[str, bytes] = {}
    for label, path in paths:
        source_url = base_url.rstrip("/") + path
        request = _urllib.Request(source_url, headers={"Accept": "application/json"})
        try:
            with _urllib.urlopen(  # nosec B310
                request, timeout=CATALOG_FETCH_TIMEOUT_SECONDS
            ) as response:
                payload = response.read()
        except TimeoutError:
            reports[label] = CatalogQualificationReport(
                "unknown", None, ("catalog_fetch_timeout", "TimeoutError")
            )
            continue
        except Exception as exc:
            reports[label] = CatalogQualificationReport("unknown", None, (type(exc).__name__,))
            continue
        payloads[label] = payload
        reports[label] = qualify_catalog(
            payload,
            source_url=source_url,
            expected_row_count=expected_rows,
            freshness_seconds=freshness_seconds,
        )
    report = reports["management" if management else "public"]
    reconciliation = None
    if not management and all(label in reports for label in ("public", "management")):
        reconciliation = reconcile_catalog_projections(reports["public"], reports["management"])
    report_payload: dict[str, Any] = report.to_dict()
    if not management:
        report_payload["projections"] = {label: value.to_dict() for label, value in reports.items()}
    probe_summary = None
    if do_probe and report.snapshot and report.passed:
        from verdict.probes import openai_probe_transport

        probe_summary = probe_catalog(
            payloads["management" if management else "public"],
            openai_probe_transport(
                base_url.rstrip("/") + "/v1", api_key=_os.getenv("OPENAI_API_KEY")
            ),
            limit=probe_limit,
            timeout_seconds=probe_timeout,
            live=True,
            consented=allow_live_probe,
            provider_name="omniroute",
        )
    if db_path:
        for label, projection in reports.items():
            if projection.snapshot:
                store_qualification(
                    projection,
                    memory_path=db_path,
                    probes=probe_summary
                    if label == ("management" if management else "public")
                    else None,
                )
    if probe_summary:
        report_payload["probes"] = probe_summary.to_dict()
    if reconciliation:
        report_payload["projection_reconciliation"] = reconciliation.to_dict()
    passed = report.passed and (reconciliation is None or reconciliation.passed)
    data = {
        "report_payload": report_payload,
        "passed": passed,
        "report": report,
        "reconciliation": reconciliation,
        "probe_summary": probe_summary,
    }
    return ActionResult(data=data, ok=passed, exit_code=0 if passed else 1)


def _action_detect(**kwargs: Any) -> ActionResult:
    """Detect reachable providers (offline or via HTTP-validated probes).

    Returns the CLI's --json payload shape.  On failure exits 1 with
    ``{"status": "failed", "error": ...}`` so the CLI can render the fail line.
    """
    offline: bool = kwargs.get("offline", False)

    if offline:
        data: dict[str, Any] = {
            "mode": "offline",
            "network_access": False,
            "credentials_read": False,
            "local_providers": [],
            "cli_providers": [],
            "centralized_routers": [],
            "cloud_apis": [],
            "custom_endpoints": [],
            "gateways": [],
        }
        return ActionResult(data=data)

    try:
        from verdict.provider_detection import detect_all_providers, probe_gateways

        result = detect_all_providers()
        gateways = probe_gateways()
        healthy = [g for g in gateways if g.health_ok]
        no_gateway_message = "No local gateway found on ports 20128, 20129, 20132."
        multi_gateway_message = (
            "Multiple gateways found. Set OMNIROUTE_BASE_URL to one of the above to select it."
        )
        gateway_message = None
        if not healthy:
            gateway_message = no_gateway_message
        elif len(healthy) > 1:
            gateway_message = multi_gateway_message
        data = {
            "local_servers": [p.__dict__ for p in result.local_servers],
            "cli_providers": [p.__dict__ for p in result.cli_providers],
            "centralized_routers": [p.__dict__ for p in result.centralized_routers],
            "cloud_apis": [p.__dict__ for p in result.cloud_apis],
            "custom_endpoints": [p.__dict__ for p in result.custom_endpoints],
            "gateways": [g.__dict__ for g in gateways],
            "message": gateway_message,
            "_result": result,  # domain object retained for the CLI human view
            "_healthy_gateways": healthy,
        }
        return ActionResult(data=data)
    except Exception as exc:
        return ActionResult(data={"status": "failed", "error": str(exc)}, ok=False, exit_code=1)


def _action_stats(**kwargs: Any) -> ActionResult:
    """Statistics from the routing decision log.

    Returns the aggregate the CLI needs to render its Routing stats view:
    tier distribution, top routed models, total requests, and mean latency.
    Missing log yields ``missing=True`` (the CLI reports a warning; not an error).
    """
    import json as _json
    from pathlib import Path

    log_path: str = kwargs.get("log_path", "verdict-decisions.jsonl")
    path = Path(log_path)
    if not path.exists():
        return ActionResult(data={"missing": True, "log_path": log_path})

    tiers: dict[int, int] = {}
    models: dict[str, int] = {}
    latencies: list[float] = []
    with open(path) as f:
        for line in f:
            try:
                entry = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            decision = entry.get("decision")
            if isinstance(decision, dict):
                t = decision.get("tier", 2)
                m = decision.get("model", "unknown")
                lat = decision.get("latency_ms", 0)
            else:
                t = entry.get("effective_tier", entry.get("tier", 2))
                m = entry.get("model_chosen", entry.get("model", "unknown"))
                lat = entry.get("latency_ms", 0)
            tiers[t] = tiers.get(t, 0) + 1
            models[m] = models.get(m, 0) + 1
            latencies.append(lat)

    total = sum(tiers.values())
    avg_latency = sum(latencies) / len(latencies) if latencies else 0
    data = {
        "missing": False,
        "log_path": log_path,
        "tiers": dict(sorted(tiers.items())),
        "top_models": sorted(models.items(), key=lambda kv: kv[1], reverse=True)[:5],
        "total_requests": total,
        "avg_latency_ms": avg_latency,
    }
    return ActionResult(data=data)


def _action_suggest(**kwargs: Any) -> ActionResult:
    """Suggestions from the SuggestionService.

    Returns a list of Suggestion records (dataclass shape) suitable for the
    CLI human view.  Empty list means the CLI prints the "No actionable
    suggestions" note.
    """
    from dataclasses import asdict

    from verdict.suggestions import SuggestionService

    log_path: str = kwargs.get("log_path", "verdict-decisions.jsonl")
    svc = SuggestionService(log_path=log_path)
    suggestions = svc.generate_suggestions()
    return ActionResult(data={"suggestions": [asdict(s) for s in suggestions]})


def _action_cost_report(**kwargs: Any) -> ActionResult:
    """Cost/usage report from historic routing decisions.

    Returns totals + tier breakdown (matching the CLI's Usage Summary):
    total_requests, t0_requests, offloaded_requests, estimated_savings.
    Missing log yields ``missing=True`` so the CLI can render the warning.
    """
    import json as _json
    from pathlib import Path

    log_path: str = kwargs.get("log_path", "verdict-decisions.jsonl")
    path = Path(log_path)
    if not path.exists():
        return ActionResult(data={"missing": True, "log_path": log_path})

    total_requests = 0
    t0_requests = 0
    with open(path) as f:
        for line in f:
            if not line.strip():
                continue
            try:
                entry = _json.loads(line)
            except Exception:
                continue
            decision = entry.get("decision")
            if isinstance(decision, dict):
                tier = decision.get("tier", 2)
            else:
                tier = entry.get("effective_tier", entry.get("tier", 2))
            if tier == 0:
                t0_requests += 1
            total_requests += 1
    offloaded = total_requests - t0_requests
    data = {
        "missing": False,
        "log_path": log_path,
        "total_requests": total_requests,
        "t0_requests": t0_requests,
        "offloaded_requests": offloaded,
        "estimated_savings_usd": offloaded * 0.005,
    }
    return ActionResult(data=data)


def _action_replay(**kwargs: Any) -> ActionResult:
    """Replay a recorded ExecutionSession.

    Returns one of three shapes:
      * ``{"status": "unavailable", "message": ...}`` — module missing (exit 3)
      * ``{"status": "missing", "message": ...}`` — session not in memory (exit 1)
      * ``{"status": "ok", "session_id": ..., "record": <session.to_dict()>}``
    """
    import os as _os
    from pathlib import Path as _Path

    session_id: str = kwargs["session_id"]
    try:
        from verdict.execution_session import ExecutionSession, ExecutionSessionError
        from verdict.memory_plane import MemoryPlane
    except ImportError as exc:
        message = (
            "replay is not yet available: verdict.execution_session is still in "
            f"development ({exc})"
        )
        return ActionResult(
            data={"status": "unavailable", "message": message}, ok=False, exit_code=3
        )
    db_path = _os.environ.get("VERDICT_MEMORY_DB", str(_Path.home() / ".verdict" / "memory.db"))
    try:
        session = ExecutionSession.resume(session_id, MemoryPlane(db_path))
    except ExecutionSessionError as exc:
        message = f"no recorded session found for id: {session_id} ({exc})"
        return ActionResult(
            data={"status": "missing", "message": message, "session_id": session_id},
            ok=False,
            exit_code=1,
        )
    return ActionResult(
        data={"status": "ok", "session_id": session_id, "record": session.to_dict()}
    )


# ---------------------------------------------------------------------------
# Trace + Demo actions (BOD-279)
# ---------------------------------------------------------------------------


def _action_trace_view(**kwargs: Any) -> ActionResult:
    """Human-readable trace of a run from events.jsonl."""
    import json as _json
    from pathlib import Path

    from verdict.orchestration.trace_render import render_trace_text
    from verdict.orchestration.trace_view import trace_view

    run_dir = Path(kwargs["run_dir"])
    if not (run_dir / "events.jsonl").exists():
        return ActionResult(data={"error": f"no events.jsonl in {run_dir}"}, ok=False, exit_code=2)

    tv = trace_view(run_dir)

    output_json: bool = kwargs.get("json", False)
    step: int | None = kwargs.get("step")
    kind_filter: str | None = kwargs.get("kind")

    if output_json:
        d = tv.to_dict()
        return ActionResult(data=d)

    # Drill-down: routing evidence
    if kwargs.get("routing"):
        from verdict.orchestration.routing_render import render_routing_text
        from verdict.orchestration.routing_view import routing_view as _build_routing

        events_raw = [
            _json.loads(line)
            for line in (run_dir / "events.jsonl").read_text().splitlines()
            if line.strip()
        ]
        try:
            rv = _build_routing(events_raw)
            text = render_routing_text(rv, width=kwargs.get("width", 100))
        except Exception as exc:
            text = f"routing evidence: {type(exc).__name__}: {exc}"
        return ActionResult(data={"text": text})

    # Drill-down: context evidence
    if kwargs.get("context"):
        from verdict.orchestration.context_render import render_context_text
        from verdict.orchestration.context_view import context_view as _build_context
        from verdict.orchestration.contracts import RunEvent

        events_raw = [
            _json.loads(line)
            for line in (run_dir / "events.jsonl").read_text().splitlines()
            if line.strip()
        ]
        run_events = [RunEvent.from_dict(e) for e in events_raw]
        try:
            cv = _build_context(run_events)
            text = render_context_text(cv, width=kwargs.get("width", 100))
        except Exception as exc:
            text = f"context evidence: {type(exc).__name__}: {exc}"
        return ActionResult(data={"text": text})

    # Filter by kind
    steps = list(tv.steps)
    if kind_filter:
        steps = [s for s in steps if s.kind == kind_filter]

    # Step detail
    if step is not None:
        matching = [s for s in steps if s.seq == step]
        if matching:
            s = matching[0]
            return ActionResult(
                data={
                    "seq": s.seq,
                    "kind": s.kind,
                    "node_id": s.node_id,
                    "at": s.at,
                    "evidence": dict(s.evidence),
                }
            )
        return ActionResult(data={"error": f"no step with seq={step}"}, ok=False, exit_code=2)

    text = render_trace_text(
        tv, width=kwargs.get("width", 100), run_ref=kwargs.get("run_ref") or None
    )
    return ActionResult(data={"text": text})


def _action_demo_run(**kwargs: Any) -> ActionResult:
    """Run the offline flagship scenario; with live=True, point to verdict orchestrate."""
    import json as _json
    import shutil
    import tempfile
    from pathlib import Path

    live: bool = kwargs.get("live", False)
    output_json: bool = kwargs.get("json", False)

    if live:
        # Refuse unless credentials are configured
        from verdict.credentials_store import CredentialsStore

        store = CredentialsStore()
        if not store.list_credentials():
            return ActionResult(
                data={
                    "error": (
                        "No credentials configured. "
                        "A live run (verdict orchestrate) uses the production routing path "
                        "and needs credentials. Run: verdict credentials set"
                    )
                },
                ok=False,
                exit_code=1,
            )
        return ActionResult(
            data={
                "text": (
                    "verdict demo --live does not start a run. The live path is "
                    'verdict orchestrate "<goal>" --repo . '
                    "(production routing with real providers)."
                ),
                "mode": "live",
            }
        )

    # Offline flagship scenario
    from verdict.orchestration.claims import derive_claims
    from verdict.orchestration.demo_render import render_claims_text
    from verdict.orchestration.demo_scenario import run_flagship_scenario
    from verdict.orchestration.trace_render import render_trace_text
    from verdict.orchestration.trace_view import trace_view

    workspace = Path(tempfile.mkdtemp(prefix="verdict-demo-"))
    runs_dir = workspace / "runs"
    try:
        worker_seconds = float(kwargs.get("worker_seconds", 1.5))
        result = run_flagship_scenario(
            runs_dir, workspace_root=workspace, worker_seconds=worker_seconds
        )
        events_raw = [
            _json.loads(line)
            for line in (result.run_dir / "events.jsonl").read_text().splitlines()
            if line.strip()
        ]
        receipt = _json.loads((result.run_dir / "receipt.json").read_text())
        claims = derive_claims(events_raw, receipt, run_dir=result.run_dir)
        tv = trace_view(result.run_dir)

        if output_json:
            return ActionResult(
                data={
                    "trace": tv.to_dict(),
                    "claims": [c.to_dict() for c in claims],
                    "receipt": receipt,
                    "mode": "offline",
                }
            )

        trace_text = render_trace_text(tv, width=kwargs.get("width", 100))
        claims_text = render_claims_text(
            claims,
            width=kwargs.get("width", 100),
            label=(
                f"OFFLINE SCENARIO: scripted workers ({worker_seconds:g} s each), injected faults"
            ),
        )
        return ActionResult(
            data={
                "text": trace_text + "\n" + claims_text,
                "mode": "offline",
                "run_dir": str(result.run_dir),
                "events_path": str(result.run_dir / "events.jsonl"),
            }
        )
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def _register_builtins() -> None:
    """Register the built-in action set at import time."""
    from verdict.actions.run_controls import cancel_node, cancel_run, retry_node

    for name, summary, handler in (
        ("run.cancel", "Cancel an active orchestration run", cancel_run),
        ("run.cancel-node", "Cancel an active worker without replacement", cancel_node),
        ("run.retry-node", "Retry failed work through the recovery budget", retry_node),
    ):
        register(ActionSpec(name, "orchestration", "mutation", summary, "Orchestration"), handler)

    from verdict.actions.extra import (
        _action_autodev_packet_canary,
        _action_autodev_packet_canary_rollback,
        _action_autodev_packet_compare,
        _action_autodev_packet_create,
        _action_autodev_packet_inspect,
        _action_autodev_packet_resume,
        _action_autodev_packet_shadow,
        _action_autodev_packet_validate,
        _action_certify,
        _action_check,
        _action_choose,
        _action_compat_check,
        _action_compat_manifest,
        _action_credentials_test,
        _action_failover_proof,
        _action_harness_certify,
        _action_harness_disable,
        _action_harness_discover,
        _action_harness_enable,
        _action_harness_prime_sync_models,
        _action_harness_prime_visibility,
        _action_harness_status,
        _action_hook_configure,
        _action_hook_recall,
        _action_hook_record,
        _action_hook_status,
        _action_inspect,
        _action_mcp_init,
        _action_mcp_status,
        _action_memory_docs,
        _action_memory_export,
        _action_memory_graph,
        _action_memory_import,
        _action_memory_masterdocs,
        _action_memory_put,
        _action_memory_search,
        _action_memory_setup,
        _action_metadata_lookup,
        _action_metadata_refresh,
        _action_metadata_show,
        _action_openspec_admit,
        _action_prove_at_rest_once,
        _action_prove_at_rest_status,
        _action_receipt_export,
        _action_receipt_list,
        _action_resume,
        _action_runtime_explain,
        _action_runtime_reconcile,
        _action_runtime_status,
        _action_setup_credentials,
        _action_setup_plan_scoped,
        _action_simulate,
    )
    from verdict.actions.views import _action_context_view, _action_routing_view

    _specs: list[tuple[ActionSpec, Callable[..., ActionResult]]] = [
        (
            ActionSpec(
                "models.list", "models", "read", "List the qualified model catalog", "Models"
            ),
            _action_models_list,
        ),
        (
            ActionSpec(
                "route", "routing", "read", "Route a single task through the gate", "Routing"
            ),
            _action_route,
        ),
        (
            ActionSpec(
                "doctor", "setup", "read", "Scan setup and connections for issues", "Doctor"
            ),
            _action_doctor,
        ),
        (
            ActionSpec(
                "probe", "health", "read", "Run bounded one-token liveness probes", "Health"
            ),
            _action_probe,
        ),
        (
            ActionSpec("setup.plan", "setup", "read", "Mutation-free setup plan", "Setup"),
            _action_setup_plan,
        ),
        (
            ActionSpec(
                "receipt.show", "traces", "read", "Show/verify a routing decision receipt", "Traces"
            ),
            _action_receipt_show,
        ),
        (
            ActionSpec(
                "eligibility", "health", "read", "Evaluate the eligibility ladder", "Health"
            ),
            _action_eligibility,
        ),
        (
            ActionSpec(
                "config.show",
                "configuration",
                "read",
                "Read-only configuration view",
                "Configuration",
            ),
            _action_config_show,
        ),
        (
            ActionSpec(
                "credentials.list",
                "credentials",
                "read",
                "List registered credentials",
                "Configuration",
            ),
            _action_credentials_list,
        ),
        (
            ActionSpec(
                "credentials.set", "credentials", "mutation", "Set a credential", "Configuration"
            ),
            _action_credentials_set,
        ),
        (
            ActionSpec(
                "credentials.unset",
                "credentials",
                "mutation",
                "Remove a credential",
                "Configuration",
            ),
            _action_credentials_unset,
        ),
        (
            ActionSpec(
                "credentials.test",
                "credentials",
                "read",
                "Run the live check for one credential",
                "Configuration",
            ),
            _action_credentials_test,
        ),
        # --- NEW actions (F3) ---
        (
            ActionSpec(
                "run-receipt",
                "traces",
                "read",
                "Verify an orchestration receipt (proof surface)",
                "Traces",
            ),
            _action_run_receipt,
        ),
        (
            ActionSpec(
                "trace.view",
                "traces",
                "read",
                "Human-readable trace of an orchestration run",
                "Traces",
            ),
            _action_trace_view,
        ),
        (
            ActionSpec(
                "demo.run",
                "traces",
                "read",
                "Credential-free offline demo scenario (--live points to verdict orchestrate)",
                "Traces",
            ),
            _action_demo_run,
        ),
        (
            ActionSpec(
                "compare", "routing", "read", "Compare dual-route results for a task", "Routing"
            ),
            _action_compare,
        ),
        (
            ActionSpec("catalog", "models", "read", "OmniRoute catalog dump", "Models"),
            _action_catalog,
        ),
        (
            ActionSpec("detect", "providers", "read", "Detect reachable providers", "Health"),
            _action_detect,
        ),
        (
            ActionSpec(
                "stats", "overview", "read", "Statistics from routing decision log", "Overview"
            ),
            _action_stats,
        ),
        (
            ActionSpec(
                "suggest", "overview", "read", "Suggestions from routing history", "Overview"
            ),
            _action_suggest,
        ),
        (
            ActionSpec(
                "cost-report", "overview", "read", "Cost report from routing decisions", "Overview"
            ),
            _action_cost_report,
        ),
        (
            ActionSpec("replay", "traces", "read", "Replay a routing decision session", "Traces"),
            _action_replay,
        ),
        (
            ActionSpec("inspect", "models", "read", "Inspect one model's catalog record", "Models"),
            _action_inspect,
        ),
        # --- ACTIONs reclassified from earlier MACHINE_ONLY (F5) ---
        (
            ActionSpec(
                "check",
                "configuration",
                "read",
                "Validate the verdict.yaml config file",
                "Configuration",
            ),
            _action_check,
        ),
        (
            ActionSpec(
                "choose", "routing", "read", "Select an eligible execution target", "Routing"
            ),
            _action_choose,
        ),
        (
            ActionSpec(
                "certify", "health", "read", "Emit a runtime certification report", "Health"
            ),
            _action_certify,
        ),
        (
            ActionSpec(
                "simulate",
                "routing",
                "read",
                "Forecast tokens, cost, risk, and expected model",
                "Routing",
            ),
            _action_simulate,
        ),
        (
            ActionSpec(
                "resume",
                "routing",
                "read",
                "Reconstruct durable resume state for a story",
                "Development",
            ),
            _action_resume,
        ),
        (
            ActionSpec(
                "failover-proof",
                "traces",
                "read",
                "Run the offline forced-failover proof",
                "Traces",
            ),
            _action_failover_proof,
        ),
        (
            ActionSpec(
                "setup.credentials",
                "setup",
                "mutation",
                "Prompt for and store missing required credentials",
                "Setup",
            ),
            _action_setup_credentials,
        ),
        (
            ActionSpec(
                "setup.intelligence",
                "setup",
                "read",
                "Scoped setup plan for intelligence bootstrap",
                "Setup",
            ),
            _action_setup_plan_scoped,
        ),
        (
            ActionSpec(
                "setup.gateways",
                "setup",
                "read",
                "Scoped setup plan for gateway bootstrap",
                "Setup",
            ),
            _action_setup_plan_scoped,
        ),
        (
            ActionSpec(
                "setup.harnesses",
                "setup",
                "read",
                "Scoped setup plan for harness bootstrap",
                "Setup",
            ),
            _action_setup_plan_scoped,
        ),
        # compat family
        (
            ActionSpec(
                "compat.manifest",
                "configuration",
                "read",
                "Publish the cross-repo compatibility manifest",
                "Configuration",
            ),
            _action_compat_manifest,
        ),
        (
            ActionSpec(
                "compat.check",
                "configuration",
                "read",
                "Check declared compatibility against verdict-core contracts",
                "Configuration",
            ),
            _action_compat_check,
        ),
        # memory family (per subcommand)
        (
            ActionSpec("memory.put", "memory", "mutation", "Put a record into memory", "Memory"),
            _action_memory_put,
        ),
        (
            ActionSpec("memory.search", "memory", "read", "Search memory records", "Memory"),
            _action_memory_search,
        ),
        (
            ActionSpec("memory.export", "memory", "read", "Export memory manifest", "Memory"),
            _action_memory_export,
        ),
        (
            ActionSpec("memory.import", "memory", "mutation", "Import a memory manifest", "Memory"),
            _action_memory_import,
        ),
        (
            ActionSpec(
                "memory.masterdocs",
                "memory",
                "mutation",
                "Canonicalize MasterDocs database",
                "Memory",
            ),
            _action_memory_masterdocs,
        ),
        (
            ActionSpec(
                "memory.graph", "memory", "mutation", "Ingest code review graph database", "Memory"
            ),
            _action_memory_graph,
        ),
        (
            ActionSpec(
                "memory.docs",
                "memory",
                "read",
                "Run documentation preflight over the repo",
                "Memory",
            ),
            _action_memory_docs,
        ),
        (
            ActionSpec(
                "memory.setup",
                "memory",
                "mutation",
                "Configure the memory bridge across tools",
                "Memory",
            ),
            _action_memory_setup,
        ),
        # mcp family (per subcommand)
        (
            ActionSpec(
                "mcp.init",
                "configuration",
                "mutation",
                "Configure the memory bridge for MCP-aware tools",
                "Configuration",
            ),
            _action_mcp_init,
        ),
        (
            ActionSpec(
                "mcp.status",
                "configuration",
                "read",
                "Check whether the Verdict MCP server is registered",
                "Configuration",
            ),
            _action_mcp_status,
        ),
        # hook family (per subcommand; claude-gate is MACHINE_ONLY above)
        (
            ActionSpec(
                "hook.status",
                "configuration",
                "read",
                "Show hook configuration status",
                "Configuration",
            ),
            _action_hook_status,
        ),
        (
            ActionSpec(
                "hook.configure",
                "configuration",
                "mutation",
                "Configure Verdict lifecycle hooks",
                "Configuration",
            ),
            _action_hook_configure,
        ),
        (
            ActionSpec(
                "hook.recall", "memory", "read", "Recall memory records for a hook query", "Memory"
            ),
            _action_hook_recall,
        ),
        (
            ActionSpec(
                "hook.record", "memory", "mutation", "Record a memory event from a hook", "Memory"
            ),
            _action_hook_record,
        ),
        # runtime family (per subcommand)
        (
            ActionSpec(
                "runtime.status", "runtime", "read", "Report runtime ownership status", "Runtime"
            ),
            _action_runtime_status,
        ),
        (
            ActionSpec(
                "runtime.explain", "runtime", "read", "Explain runtime health rollups", "Runtime"
            ),
            _action_runtime_explain,
        ),
        (
            ActionSpec(
                "runtime.reconcile",
                "runtime",
                "mutation",
                "Plan or apply canonical runtime reconciliation",
                "Runtime",
            ),
            _action_runtime_reconcile,
        ),
        # prove-at-rest family (daemon is MACHINE_ONLY)
        (
            ActionSpec(
                "prove-at-rest.status",
                "health",
                "read",
                "Show the latest persisted prove-at-rest state",
                "Health",
            ),
            _action_prove_at_rest_status,
        ),
        (
            ActionSpec(
                "prove-at-rest.once",
                "health",
                "read",
                "Run one prove-at-rest cycle and exit",
                "Health",
            ),
            _action_prove_at_rest_once,
        ),
        # metadata family (per subcommand)
        (
            ActionSpec(
                "metadata.show", "models", "read", "Show the current model metadata store", "Models"
            ),
            _action_metadata_show,
        ),
        (
            ActionSpec(
                "metadata.lookup",
                "models",
                "read",
                "Look up an OmniRoute id in the metadata store",
                "Models",
            ),
            _action_metadata_lookup,
        ),
        (
            ActionSpec(
                "metadata.refresh",
                "models",
                "mutation",
                "Refresh the Core metadata store from sources",
                "Models",
            ),
            _action_metadata_refresh,
        ),
        # receipt family (per subcommand)
        (
            ActionSpec("receipt.list", "traces", "read", "List routing receipts", "Traces"),
            _action_receipt_list,
        ),
        (
            ActionSpec("receipt.export", "traces", "read", "Export routing receipts", "Traces"),
            _action_receipt_export,
        ),
        # openspec family
        (
            ActionSpec(
                "openspec.admit",
                "configuration",
                "mutation",
                "Validate and admit a significant OpenSpec change",
                "Configuration",
            ),
            _action_openspec_admit,
        ),
        # autodev packet family (execute is LAUNCH)
        (
            ActionSpec(
                "autodev.packet.create",
                "development",
                "mutation",
                "Create a portable autodev packet",
                "Development",
            ),
            _action_autodev_packet_create,
        ),
        (
            ActionSpec(
                "autodev.packet.inspect",
                "development",
                "read",
                "Inspect an autodev packet",
                "Development",
            ),
            _action_autodev_packet_inspect,
        ),
        (
            ActionSpec(
                "autodev.packet.validate",
                "development",
                "read",
                "Validate an autodev packet",
                "Development",
            ),
            _action_autodev_packet_validate,
        ),
        (
            ActionSpec(
                "autodev.packet.resume",
                "development",
                "read",
                "Prepare an autodev packet resume payload",
                "Development",
            ),
            _action_autodev_packet_resume,
        ),
        (
            ActionSpec(
                "autodev.packet.compare",
                "development",
                "read",
                "Compare two family runs against a packet",
                "Development",
            ),
            _action_autodev_packet_compare,
        ),
        (
            ActionSpec(
                "autodev.packet.shadow",
                "development",
                "read",
                "Report shadow-learning statistics for an episodes fixture",
                "Development",
            ),
            _action_autodev_packet_shadow,
        ),
        (
            ActionSpec(
                "autodev.packet.canary",
                "development",
                "read",
                "Apply or roll back a shadow canary choice",
                "Development",
            ),
            _action_autodev_packet_canary,
        ),
        (
            ActionSpec(
                "autodev.packet.canary-rollback",
                "development",
                "mutation",
                "Restore the pre-canary baseline choice",
                "Development",
            ),
            _action_autodev_packet_canary_rollback,
        ),
        # harness family — per subcommand (7 harnesses * status/enable/disable, plus some certify/discover/sync-models/visibility)
        (
            ActionSpec(
                "harness.claude.status", "harness", "read", "Show Claude harness status", "Harness"
            ),
            _action_harness_status,
        ),
        (
            ActionSpec(
                "harness.claude.enable",
                "harness",
                "mutation",
                "Enable the Claude harness",
                "Harness",
            ),
            _action_harness_enable,
        ),
        (
            ActionSpec(
                "harness.claude.disable",
                "harness",
                "mutation",
                "Disable the Claude harness",
                "Harness",
            ),
            _action_harness_disable,
        ),
        (
            ActionSpec(
                "harness.claude.certify",
                "harness",
                "read",
                "Certify Claude harness readiness",
                "Harness",
            ),
            _action_harness_certify,
        ),
        (
            ActionSpec(
                "harness.claude.discover",
                "harness",
                "read",
                "Discover Claude harness installations",
                "Harness",
            ),
            _action_harness_discover,
        ),
        (
            ActionSpec(
                "harness.cline.status", "harness", "read", "Show Cline harness status", "Harness"
            ),
            _action_harness_status,
        ),
        (
            ActionSpec(
                "harness.cline.enable", "harness", "mutation", "Enable the Cline harness", "Harness"
            ),
            _action_harness_enable,
        ),
        (
            ActionSpec(
                "harness.cline.disable",
                "harness",
                "mutation",
                "Disable the Cline harness",
                "Harness",
            ),
            _action_harness_disable,
        ),
        (
            ActionSpec(
                "harness.cline.certify",
                "harness",
                "read",
                "Certify Cline harness readiness",
                "Harness",
            ),
            _action_harness_certify,
        ),
        (
            ActionSpec(
                "harness.cline.discover",
                "harness",
                "read",
                "Discover Cline harness installations",
                "Harness",
            ),
            _action_harness_discover,
        ),
        (
            ActionSpec(
                "harness.codex.status", "harness", "read", "Show Codex harness status", "Harness"
            ),
            _action_harness_status,
        ),
        (
            ActionSpec(
                "harness.codex.enable", "harness", "mutation", "Enable the Codex harness", "Harness"
            ),
            _action_harness_enable,
        ),
        (
            ActionSpec(
                "harness.codex.disable",
                "harness",
                "mutation",
                "Disable the Codex harness",
                "Harness",
            ),
            _action_harness_disable,
        ),
        (
            ActionSpec(
                "harness.cursor.status", "harness", "read", "Show Cursor harness status", "Harness"
            ),
            _action_harness_status,
        ),
        (
            ActionSpec(
                "harness.cursor.enable",
                "harness",
                "mutation",
                "Enable the Cursor harness",
                "Harness",
            ),
            _action_harness_enable,
        ),
        (
            ActionSpec(
                "harness.cursor.disable",
                "harness",
                "mutation",
                "Disable the Cursor harness",
                "Harness",
            ),
            _action_harness_disable,
        ),
        (
            ActionSpec(
                "harness.cursor.certify",
                "harness",
                "read",
                "Certify Cursor harness readiness",
                "Harness",
            ),
            _action_harness_certify,
        ),
        (
            ActionSpec(
                "harness.cursor.discover",
                "harness",
                "read",
                "Discover Cursor harness installations",
                "Harness",
            ),
            _action_harness_discover,
        ),
        (
            ActionSpec(
                "harness.hermes.status", "harness", "read", "Show Hermes harness status", "Harness"
            ),
            _action_harness_status,
        ),
        (
            ActionSpec(
                "harness.hermes.enable",
                "harness",
                "mutation",
                "Enable the Hermes harness",
                "Harness",
            ),
            _action_harness_enable,
        ),
        (
            ActionSpec(
                "harness.hermes.disable",
                "harness",
                "mutation",
                "Disable the Hermes harness",
                "Harness",
            ),
            _action_harness_disable,
        ),
        (
            ActionSpec(
                "harness.opencode.status",
                "harness",
                "read",
                "Show OpenCode harness status",
                "Harness",
            ),
            _action_harness_status,
        ),
        (
            ActionSpec(
                "harness.opencode.enable",
                "harness",
                "mutation",
                "Enable the OpenCode harness",
                "Harness",
            ),
            _action_harness_enable,
        ),
        (
            ActionSpec(
                "harness.opencode.disable",
                "harness",
                "mutation",
                "Disable the OpenCode harness",
                "Harness",
            ),
            _action_harness_disable,
        ),
        (
            ActionSpec(
                "harness.opencode.certify",
                "harness",
                "read",
                "Certify OpenCode harness readiness",
                "Harness",
            ),
            _action_harness_certify,
        ),
        (
            ActionSpec(
                "harness.opencode.discover",
                "harness",
                "read",
                "Discover OpenCode harness installations",
                "Harness",
            ),
            _action_harness_discover,
        ),
        (
            ActionSpec(
                "harness.prime.status", "harness", "read", "Show Prime harness status", "Harness"
            ),
            _action_harness_status,
        ),
        (
            ActionSpec(
                "harness.prime.enable", "harness", "mutation", "Enable the Prime harness", "Harness"
            ),
            _action_harness_enable,
        ),
        (
            ActionSpec(
                "harness.prime.disable",
                "harness",
                "mutation",
                "Disable the Prime harness",
                "Harness",
            ),
            _action_harness_disable,
        ),
        (
            ActionSpec(
                "harness.prime.certify",
                "harness",
                "read",
                "Certify Prime harness readiness",
                "Harness",
            ),
            _action_harness_certify,
        ),
        (
            ActionSpec(
                "harness.prime.discover",
                "harness",
                "read",
                "Discover Prime harness installations",
                "Harness",
            ),
            _action_harness_discover,
        ),
        (
            ActionSpec(
                "harness.prime.sync-models",
                "harness",
                "mutation",
                "Sync Prime model registry with live inventory",
                "Harness",
            ),
            _action_harness_prime_sync_models,
        ),
        (
            ActionSpec(
                "harness.prime.visibility",
                "harness",
                "read",
                "Compare Prime visibility with live inventory",
                "Harness",
            ),
            _action_harness_prime_visibility,
        ),
        (
            ActionSpec(
                "routing.view",
                "routing",
                "read",
                "Show the recorded routing explorer for a run",
                "Routing",
            ),
            _action_routing_view,
        ),
        (
            ActionSpec(
                "context.view",
                "orchestration",
                "read",
                "Show recorded context budget and provenance for a run",
                "Runs",
            ),
            _action_context_view,
        ),
    ]
    for spec, fn in _specs:
        register(spec, fn)


_register_builtins()
