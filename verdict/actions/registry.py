"""Action registry — the single lookup table for CLI + TUI convergence.

Every action delegates to an existing domain/application service. No business
logic lives here.
"""

from __future__ import annotations

import contextlib
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
# Three-bucket classification
# ---------------------------------------------------------------------------

MACHINE_ONLY: dict[str, str] = {
    "serve": "server/daemon lifecycle; long-running HTTP process",
    "mcp": "MCP server lifecycle; invoked by other programs, not users",
    "hook": "hook management endpoint; invoked by harness integrations",
    "uninstall": "destructive global uninstall; not an interactive action",
    "memory": "memory-bridge lifecycle; invoked by harness integrations",
    "runtime": "daemon lifecycle management; long-running system process",
    "certify": "certification snapshot; batch dev-tool process",
    "check": "inline probe wrapper; dev-tool plumbing",
    "choose": "legacy model chooser; superseded by route action",
    "compat": "compatibility scanner; dev-tool",
    "failover-proof": "failover proof generation; batch dev-tool",
    "harness": "per-harness lifecycle commands; dev-tool plumbing",
    "metadata": "metadata sync; batch process invoked by scripts",
    "openspec": "spec lifecycle; external tool integration",
    "plan": "legacy alias for setup plan",
    "run": "legacy alias for route",
}

LAUNCH: dict[str, str] = {
    "orchestrate": "long-running orchestration pipeline; launches parallel workers",
    "supervise": "supervisor wrapper around orchestrate; long-running",
    "watch": "live TUI viewer for running orchestrations; interactive",
    "benchmark": "benchmark suite; long-running measurement",
    "autodev": "batch automation pipeline; long-running",
    "autodev-golden-path": "golden-path batch pipeline; long-running",
    "ui": "launches Streamlit dashboard; separate process",
    "prove-at-rest": "long-running monitoring probe; continuous",
    "quickstart": "guided quickstart wizard; interactive session",
    "simulate": "simulation run; stateful multi-step session",
    "resume": "story resume pipeline; interactive session",
}

GAP: dict[str, str] = {"inspect": "model detail view (BOD-278: context inspect service needed)"}

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
    ]
    for spec, fn in _specs:
        register(spec, fn)


_register_builtins()
