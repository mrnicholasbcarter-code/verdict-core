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
    """List the qualified model catalog."""
    from verdict.actions.helpers import default_model_catalog
    from verdict.models import ModelInfo

    catalog: list[ModelInfo] = kwargs.get("catalog") or default_model_catalog()
    data = [
        {
            "id": m.id,
            "provider": m.provider,
            "tier": m.capability_tier,
            "context_window": m.context_window,
            "cost_per_1k": m.cost_per_1k,
            "availability_state": m.availability_state,
        }
        for m in catalog
    ]
    return ActionResult(data=data)


def _action_route(**kwargs: Any) -> ActionResult:
    """Route a single task through the gate."""
    task: str = kwargs["task"]
    criticality: str = kwargs.get("criticality", "medium")
    allow_offline: bool = kwargs.get("allow_offline", False)

    from verdict.actions.helpers import build_route_gate

    gate = build_route_gate(allow_offline=allow_offline)
    dec, selection = gate.route_with_strategy(task, criticality)

    data = {
        "model": dec.model,
        "provider": dec.provider,
        "tier": dec.tier,
        "decision": dec.decision,
        "transport_outcome": dec.transport_outcome,
        "quality_outcome": dec.quality_outcome,
        "managed_backend_status": dec.managed_backend_status,
        "protected": dec.protected,
        "degraded_mode": dec.degraded_mode,
        "latency_ms": dec.latency_ms,
        "reason": dec.reason,
        "strategy": selection.strategy,
    }
    return ActionResult(data=data)


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
            data={"error": "live probes require explicit consent; pass --allow-live-probe"},
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
    """Show/verify an orchestration run receipt."""
    import json as _json
    from pathlib import Path

    from verdict.orchestration.receipt import completion_verdict, verify_run_receipt

    run_dir_str: str = kwargs["run_dir"]
    runs_dir: str = kwargs.get("runs_dir", ".verdict/runs")

    # Resolve run directory
    run_path = Path(run_dir_str)
    if not run_path.is_absolute():
        run_path = Path(runs_dir) / run_dir_str
    receipt_path = run_path / "receipt.json"
    if not receipt_path.exists():
        return ActionResult(data={"error": f"no receipt at {receipt_path}"}, ok=False, exit_code=2)
    receipt = _json.loads(receipt_path.read_text())
    problems = verify_run_receipt(run_path)
    outcome, reason = completion_verdict(receipt)
    data = {"outcome": outcome, "reason": reason, "problems": problems, "receipt": receipt}
    return ActionResult(
        data=data,
        ok=(outcome == "COMPLETE" and not problems),
        exit_code=0 if (outcome == "COMPLETE" and not problems) else 1,
    )


def _action_eligibility(**kwargs: Any) -> ActionResult:
    """Evaluate the DISCOVERED→SELECTED eligibility ladder."""
    from datetime import datetime, timezone

    from verdict.orchestration.cli import (
        build_selector,
        eligibility_payload,
        parse_provider_families,
    )
    from verdict.orchestration.contracts import TaskRequirements

    gateway: str = kwargs.get("gateway", "http://localhost:20128/v1")
    scope: str = kwargs.get("scope", "all")
    prefer: str = kwargs.get("prefer", "")
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

            data["config"] = yaml.safe_load(config_file.read_text()) or {}
        except Exception as exc:
            data["config_error"] = str(exc)
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
    from pathlib import Path

    from verdict.orchestration.receipt import completion_verdict, verify_run_receipt

    run_dir_str: str = kwargs["run_dir"]
    runs_dir: str = kwargs.get("runs_dir", ".verdict/runs")

    run_path = Path(run_dir_str)
    if not run_path.is_absolute():
        run_path = Path(runs_dir) / run_dir_str
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
    """Compare dual-route results for a task."""
    from verdict.actions.helpers import build_route_gate

    task: str = kwargs["task"]
    criticality: str = kwargs.get("criticality", "medium")
    allow_offline: bool = kwargs.get("allow_offline", False)

    gate = build_route_gate(allow_offline=allow_offline)
    dec_a, sel_a = gate.route_with_strategy(task, criticality)
    dec_b, sel_b = gate.route_with_strategy(task, criticality)
    data = {
        "task": task,
        "route_a": {"model": dec_a.model, "provider": dec_a.provider, "strategy": sel_a.strategy},
        "route_b": {"model": dec_b.model, "provider": dec_b.provider, "strategy": sel_b.strategy},
        "match": dec_a.model == dec_b.model,
    }
    return ActionResult(data=data)


def _action_catalog(**kwargs: Any) -> ActionResult:
    """Dump the OmniRoute catalog."""
    import json as _json
    import urllib.request

    base_url: str = kwargs.get("base_url", "http://localhost:20128/v1")
    timeout: float = kwargs.get("timeout", 30.0)

    try:
        req = urllib.request.Request(base_url.rstrip("/") + "/models")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            body = _json.loads(resp.read(64 * 1024 * 1024))
        models = body.get("data", [])
        data = {
            "count": len(models),
            "models": [{"id": m.get("id"), "owned_by": m.get("owned_by")} for m in models],
        }
        return ActionResult(data=data)
    except Exception as exc:
        return ActionResult(data={"error": str(exc)}, ok=False, exit_code=1)


def _action_detect(**kwargs: Any) -> ActionResult:
    """Detect reachable providers."""
    _verbose: bool = kwargs.get("verbose", False)  # reserved for future use
    offline: bool = kwargs.get("offline", False)

    if offline:
        data: dict[str, Any] = {
            "mode": "offline",
            "network_access": False,
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
        data = {
            "local_servers": [p.__dict__ for p in result.local_servers],
            "cli_providers": [p.__dict__ for p in result.cli_providers],
            "centralized_routers": [p.__dict__ for p in result.centralized_routers],
            "cloud_apis": [p.__dict__ for p in result.cloud_apis],
            "custom_endpoints": [p.__dict__ for p in result.custom_endpoints],
            "gateways": [g.__dict__ for g in gateways],
        }
        return ActionResult(data=data)
    except Exception as exc:
        return ActionResult(data={"error": str(exc)}, ok=False, exit_code=1)


def _action_stats(**kwargs: Any) -> ActionResult:
    """Statistics from the routing decision log."""
    import json as _json
    from pathlib import Path

    log_path: str = kwargs.get("log_path", "verdict-decisions.jsonl")
    path = Path(log_path)
    if not path.exists():
        return ActionResult(data={"error": f"log not found: {log_path}"}, ok=False, exit_code=1)

    entries: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        try:
            entries.append(_json.loads(line))
        except ValueError:
            continue
    models: dict[str, int] = {}
    for e in entries:
        m = e.get("model", "unknown")
        models[m] = models.get(m, 0) + 1
    data = {
        "total_decisions": len(entries),
        "unique_models": len(models),
        "model_counts": dict(sorted(models.items(), key=lambda x: -x[1])),
    }
    return ActionResult(data=data)


def _action_suggest(**kwargs: Any) -> ActionResult:
    """Suggestions based on routing decision history."""
    import json as _json
    from pathlib import Path

    log_path: str = kwargs.get("log_path", "verdict-decisions.jsonl")
    path = Path(log_path)
    if not path.exists():
        return ActionResult(data={"error": f"log not found: {log_path}"}, ok=False, exit_code=1)

    entries: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        try:
            entries.append(_json.loads(line))
        except ValueError:
            continue
    suggestions: list[str] = []
    models: dict[str, int] = {}
    for e in entries:
        m = e.get("model", "unknown")
        models[m] = models.get(m, 0) + 1
    if len(models) == 1:
        suggestions.append("Only one model in use — consider adding alternatives for resilience")
    if len(entries) > 100:
        suggestions.append(f"{len(entries)} decisions logged — review for cost optimization")
    data = {
        "total_decisions": len(entries),
        "suggestions": suggestions,
        "model_distribution": dict(sorted(models.items(), key=lambda x: -x[1])),
    }
    return ActionResult(data=data)


def _action_cost_report(**kwargs: Any) -> ActionResult:
    """Generate a cost report from routing decisions."""
    import json as _json
    from pathlib import Path

    log_path: str = kwargs.get("log_path", "verdict-decisions.jsonl")
    path = Path(log_path)
    if not path.exists():
        return ActionResult(data={"error": f"log not found: {log_path}"}, ok=False, exit_code=1)

    entries: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        try:
            entries.append(_json.loads(line))
        except ValueError:
            continue
    total_cost = sum(e.get("cost", 0.0) for e in entries)
    by_model: dict[str, float] = {}
    for e in entries:
        m = e.get("model", "unknown")
        by_model[m] = by_model.get(m, 0.0) + e.get("cost", 0.0)
    data = {
        "total_cost": total_cost,
        "decisions": len(entries),
        "cost_by_model": dict(sorted(by_model.items(), key=lambda x: -x[1])),
    }
    return ActionResult(data=data)


def _action_replay(**kwargs: Any) -> ActionResult:
    """Replay a routing decision session."""
    import json as _json
    from pathlib import Path

    session_id: str = kwargs["session_id"]
    log_path: str = kwargs.get("log_path", "verdict-decisions.jsonl")
    path = Path(log_path)
    if not path.exists():
        return ActionResult(data={"error": f"log not found: {log_path}"}, ok=False, exit_code=1)

    matching: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        try:
            entry = _json.loads(line)
            if entry.get("session_id") == session_id or entry.get("request_id") == session_id:
                matching.append(entry)
        except ValueError:
            continue
    data = {"session_id": session_id, "entries": matching, "count": len(matching)}
    return ActionResult(data=data, ok=bool(matching), exit_code=0 if matching else 1)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def _register_builtins() -> None:
    """Register the built-in action set at import time."""
    from verdict.actions.extra import (
        _action_autodev_packet_canary,
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
    ]
    for spec, fn in _specs:
        register(spec, fn)


_register_builtins()
