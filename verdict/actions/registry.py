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
# Three-bucket classification
# ---------------------------------------------------------------------------

MACHINE_ONLY: dict[str, str] = {
    "serve": "long-running HTTP API server daemon providing OpenAI-compatible routing gateway lifecycle managed by systemd or docker",
    "mcp": "Model Context Protocol server daemon lifecycle management invoked programmatically by editor integrations not end users",
    "hook": "git hook event capture endpoints for commit and push actions invoked by version control not users",
    "uninstall": "destructive global uninstall operation removing all verdict state configuration and credentials with no rollback",
    "memory": "memory-bridge daemon process lifecycle for harness integration context persistence invoked programmatically not by users",
    "runtime": "system daemon lifecycle reconciliation managing long-running background service processes and their canonical ownership",
    "certify": "batch certification evidence generator producing provider detection snapshots invoked by automation pipelines not users",
    "check": "configuration validation wrapper around probe infrastructure invoked by continuous integration pipelines not users",
    "choose": "legacy model selector command superseded by route action retained only for backward compatibility with existing scripts",
    "compat": "OpenAPI compatibility scanner batch tool analyzing gateway conformance invoked by integration test automation not users",
    "failover-proof": "batch failover proof generator producing cryptographic evidence of provider fallback invoked by test automation not users",
    "harness": "per-harness adapter daemon lifecycle configuring editor integration endpoints invoked by setup automation not users",
    "metadata": "model metadata sync daemon fetching provider catalogs on schedule invoked by background automation not users",
    "openspec": "OpenAPI specification admission daemon managing gateway spec validation lifecycle invoked by integration tools not users",
    "plan": "legacy setup plan command aliasing setup.plan action retained for backward compatibility with existing automation scripts",
    "run": "legacy routing command aliasing route action retained for backward compatibility with existing scripts and documentation",
}

LAUNCH: dict[str, LaunchSpec] = {
    "orchestrate": LaunchSpec(reason="long-running orchestration pipeline with parallel worker coordination and receipt generation", entry="verdict.orchestration.cli:_orchestrate", section="Orchestration"),
    "supervise": LaunchSpec(reason="supervisor wrapper coordinating orchestration runs with health monitoring and recovery", entry="verdict.orchestration.supervisor:dispatch", section="Orchestration"),
    "watch": LaunchSpec(reason="live terminal interface displaying real-time orchestration progress and worker status", entry="verdict.orchestration.cli:_watch", section="Orchestration"),
    "benchmark": LaunchSpec(reason="long-running benchmark suite measuring routing decision quality across multiple scenarios", entry="verdict.cli:cmd_benchmark", section="Development"),
    "autodev": LaunchSpec(reason="batch automation pipeline orchestrating multiple development tasks with shadow execution tracking", entry="verdict.cli:cmd_autodev_packet_shadow", section="Development"),
    "autodev-golden-path": LaunchSpec(reason="golden-path validation pipeline executing reference implementation scenarios end-to-end", entry="verdict.cli:cmd_autodev_golden_path", section="Development"),
    "ui": LaunchSpec(reason="launches Streamlit dashboard as separate long-running web server process for interactive exploration", entry="verdict.dashboard:main", section="Monitoring"),
    "prove-at-rest": LaunchSpec(reason="continuous background monitoring daemon executing periodic liveness probes against configured gateways", entry="verdict.cli:cmd_prove_at_rest", section="Monitoring"),
    "quickstart": LaunchSpec(reason="interactive guided setup wizard walking user through credential and gateway configuration", entry="verdict.cli:cmd_quickstart", section="Setup"),
    "simulate": LaunchSpec(reason="stateful multi-step routing simulation maintaining conversation state across decisions", entry="verdict.cli:cmd_simulate", section="Development"),
    "resume": LaunchSpec(reason="interactive story resume pipeline reconstructing and continuing interrupted work from checkpoint", entry="verdict.cli:cmd_resume", section="Development"),
}

GAP: dict[str, str] = {}  # No gaps currently; all commands are classified

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
    from verdict.actions.extra import _action_inspect

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
                "receipt.show",
                "traces",
                "read",
                "Show/verify an orchestration run receipt",
                "Traces",
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
    ]
    for spec, fn in _specs:
        register(spec, fn)


_register_builtins()
