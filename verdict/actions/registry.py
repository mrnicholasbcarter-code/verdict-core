"""Action registry — the single lookup table for CLI + TUI convergence.

Every action delegates to an existing domain/application service. No business
logic lives here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from verdict.actions.base import NOOP_SINK, ActionEvent, ActionResult, ActionSink, ActionSpec

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
# MACHINE_ONLY — defensible exceptions with reasons
# ---------------------------------------------------------------------------

MACHINE_ONLY: dict[str, str] = {
    "serve": "long-running server process; not an action",
    "mcp": "MCP server lifecycle; not an interactive action",
    "hook": "hook management; dev-tool plumbing",
    "ui": "launches Streamlit subprocess; not an in-process action",
    "harness": "per-harness lifecycle commands; multiple subcommands, dev-tool",
    "autodev": "batch automation pipeline; subprocess-heavy",
    "autodev-golden-path": "golden-path batch pipeline; not interactive",
    "runtime": "daemon lifecycle management; long-running",
    "uninstall": "destructive global uninstall; not a TUI action",
    "memory": "memory-bridge lifecycle; harness-specific",
    "compat": "compatibility scanner; dev-tool",
    "openspec": "spec lifecycle; external tool integration",
    "resume": "story resume pipeline; subprocess-heavy",
    "prove-at-rest": "long-running monitoring probe; not interactive",
    "failover-proof": "failover proof generation; batch process",
    "cost-report": "cost report; computation-heavy",
    "metadata": "metadata sync; batch process",
    "benchmark": "benchmark suite; long-running",
    "certify": "certification snapshot; batch process",
    "suggest": "suggestion engine; analysis-heavy (620L inline)",
    "compare": "dual-route comparison; could be future action",
    "simulate": "simulation run; stateful",
    "replay": "replay viewer; needs full TUI",
    "stats": "statistics computation from log file",
    "plan": "planner invocation; stateful",
    "run": "legacy run command; use orchestrate",
    "choose": "model chooser; legacy",
    "inspect": "model inspect; legacy",
    "catalog": "OmniRoute catalog dump; network-heavy",
    "detect": "provider detection; network scan",
    "quickstart": "guided quickstart wizard; interactive subprocess",
    "check": "inline check; quick probe wrapper",
    "orchestrate": "long-running orchestration pipeline; launches parallel workers",
    "supervise": "supervisor wrapper around orchestrate; long-running",
    "watch": "live TUI viewer for running orchestrations; interactive",
    "run-receipt": "orchestration receipt; use receipt.show action for routing receipts",
    "sync-models": "metadata sync for orchestration; batch process",
    "visibility": "supervisor visibility report; dev-tool",
}


# ---------------------------------------------------------------------------
# Built-in action implementations (lazy-import to avoid import-time overhead)
# ---------------------------------------------------------------------------


def _action_models_list(**kwargs: Any) -> ActionResult:
    """List the qualified model catalog."""
    from verdict.cli import default_model_catalog
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

    from verdict.cli import _build_route_gate

    gate = _build_route_gate(allow_offline=allow_offline)
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
    from verdict.cli import _collect_doctor_diagnostics

    preflight_timeout: float = kwargs.get("preflight_timeout", 120.0)
    fix: bool = kwargs.get("fix", False)
    diag = _collect_doctor_diagnostics(fix, interactive=False, preflight_timeout=preflight_timeout)

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
            data={"error": "live probes require explicit consent; pass allow_live_probe=True"},
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

    from verdict.cli import _probe_result_payload

    results = [_probe_result_payload(obs) for obs in run.observations]
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
# Registration
# ---------------------------------------------------------------------------


def _register_builtins() -> None:
    """Register the built-in action set at import time."""
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
    ]
    for spec, fn in _specs:
        register(spec, fn)


_register_builtins()
