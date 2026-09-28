"""Additional bounded action implementations for BOD-275 Lane E reclassification.

Each action defers to an existing domain function; the CLI handler is wired to
call ``run_action`` for the byte-identical ``--json`` surface where required.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from verdict.actions.base import ActionResult

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


def _action_inspect(**kwargs: Any) -> ActionResult:
    """Inspect one model's catalog record and any stored passport evidence."""
    from verdict.actions.helpers import default_model_catalog

    model_id: str = kwargs["model_id"]
    catalog = kwargs.get("catalog") or default_model_catalog()

    matches = [m for m in catalog if m.id == model_id or f"{m.provider}/{m.id}" == model_id]
    if not matches:
        return ActionResult(
            data={"error": f"model not found in catalog: {model_id}"}, ok=False, exit_code=1
        )
    model = matches[0]
    payload: dict[str, Any] = {
        "id": model.id,
        "provider": model.provider,
        "tier": model.capability_tier,
        "context_window": model.context_window,
        "cost_per_1k": model.cost_per_1k,
        "capabilities": sorted(model.capabilities),
        "availability_state": model.availability_state,
    }
    return ActionResult(data=payload)


# ---------------------------------------------------------------------------
# Configuration surfaces
# ---------------------------------------------------------------------------


def _action_check(**kwargs: Any) -> ActionResult:
    """Validate the Verdict configuration file and report status."""
    import os

    import yaml

    config_dir = os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "verdict"
    )
    config_path = os.path.join(config_dir, "verdict.yaml")
    if not os.path.exists(config_path):
        return ActionResult(
            data={"status": "missing", "config_path": config_path, "issues": ["missing"]},
            ok=False,
            exit_code=1,
        )
    try:
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}
    except Exception as exc:
        return ActionResult(
            data={"status": "invalid", "config_path": config_path, "error": str(exc)},
            ok=False,
            exit_code=1,
        )
    issues: list[str] = []
    primary_model = config.get("primary_model")
    if not primary_model:
        issues.append("no_primary_model")
    providers = config.get("providers", {}) or {}
    if not isinstance(providers, dict):
        issues.append("providers_not_dict")
    else:
        seen: dict[str, str] = {}
        for name, cfg in providers.items():
            if not isinstance(cfg, dict):
                issues.append(f"provider_not_dict:{name}")
                continue
            base_url = str(cfg.get("base_url", "") or "")
            if "sk-" in base_url or "api_key" in base_url.lower():
                issues.append(f"api_key_in_url:{name}")
            key = base_url.rstrip("/")
            if key and key in seen:
                issues.append(f"duplicate_host:{name}:{seen[key]}")
            elif key:
                seen[key] = name
    return ActionResult(
        data={
            "status": "ok" if not issues else "issues_found",
            "config_path": config_path,
            "primary_model": primary_model,
            "issues": issues,
        },
        ok=not issues,
        exit_code=0 if not issues else 1,
    )


def _action_compat_manifest(**kwargs: Any) -> ActionResult:
    """Publish the cross-repo compatibility manifest."""
    from verdict.compatibility_manifest import build_compatibility_manifest

    manifest = build_compatibility_manifest()
    return ActionResult(data=manifest.to_dict())


def _action_compat_check(**kwargs: Any) -> ActionResult:
    """Check a declared compatibility manifest against verdict-core contracts."""
    from verdict.compatibility_manifest import check_compatibility

    declared: str | None = kwargs.get("declared")
    if not declared or not Path(declared).exists():
        return ActionResult(
            data={"allowed": False, "reason": f"Declared manifest file not found: {declared}"},
            ok=False,
            exit_code=1,
        )
    try:
        payload = json.loads(Path(declared).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return ActionResult(
            data={"allowed": False, "reason": f"Declared manifest is invalid JSON: {exc}"},
            ok=False,
            exit_code=1,
        )
    declared_contracts = payload.get("contracts") if isinstance(payload, dict) else None
    if not isinstance(declared_contracts, dict):
        return ActionResult(
            data={"allowed": False, "reason": "Declared manifest missing 'contracts' object."},
            ok=False,
            exit_code=1,
        )
    result = check_compatibility(declared_contracts)
    data = {
        "allowed": result.allowed,
        "reason": result.reason,
        "mismatched_contracts": list(result.mismatched_contracts),
    }
    return ActionResult(data=data, ok=result.allowed, exit_code=0 if result.allowed else 1)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def _action_credentials_test(**kwargs: Any) -> ActionResult:
    """Run the live check for one credential."""
    import os

    from verdict.credentials_registry import get_credential
    from verdict.credentials_store import CredentialsStore

    name: str = kwargs["name"]
    cred = get_credential(name)
    if cred is None:
        return ActionResult(
            data={"name": name, "status": "unknown", "message": "not in registry"},
            ok=False,
            exit_code=1,
        )
    if cred.live_check is None:
        return ActionResult(
            data={"name": name, "status": "no_check", "message": "no live check defined"}, ok=True
        )
    CredentialsStore().load_into_env()
    value = os.environ.get(name)
    if not value:
        return ActionResult(
            data={"name": name, "status": "missing", "message": "credential not set"},
            ok=False,
            exit_code=1,
        )
    try:
        success, message = cred.live_check(value)
    except Exception as exc:
        return ActionResult(
            data={"name": name, "status": "error", "message": str(exc)}, ok=False, exit_code=1
        )
    return ActionResult(
        data={"name": name, "status": "ok" if success else "failed", "message": message},
        ok=bool(success),
        exit_code=0 if success else 1,
    )


# ---------------------------------------------------------------------------
# Setup (scoped plan; credentials wizard)
# ---------------------------------------------------------------------------


def _action_setup_credentials(**kwargs: Any) -> ActionResult:
    """Report registered credential coverage; does not prompt (non-interactive)."""
    from verdict.credentials_registry import CREDENTIALS, DEPENDENCIES
    from verdict.credentials_store import CredentialsStore, get_credential_source

    store = CredentialsStore()
    creds: list[dict[str, Any]] = []
    missing_required: list[str] = []
    for cred in CREDENTIALS:
        source, _ = get_credential_source(cred.env_name, store)
        creds.append(
            {
                "name": cred.env_name,
                "source": source,
                "purpose": cred.purpose,
                "optional": cred.optional,
            }
        )
        if source == "missing" and not cred.optional:
            missing_required.append(cred.env_name)
    deps: list[dict[str, Any]] = []
    for dep in DEPENDENCIES:
        present, version = dep.check()
        deps.append(
            {
                "name": dep.name,
                "installed": present,
                "version": version,
                "install": dep.install_command,
            }
        )
    return ActionResult(
        data={"credentials": creds, "dependencies": deps, "missing_required": missing_required}
    )


def _action_setup_plan_scoped(**kwargs: Any) -> ActionResult:
    """Scoped mutation-free setup plan (intelligence/gateways/harnesses)."""
    from verdict.setup_plan import build_setup_plan

    scope: str = kwargs.get("scope", "all")
    plan = build_setup_plan().to_dict()
    plan["scope"] = scope
    return ActionResult(data=plan)


# ---------------------------------------------------------------------------
# Certify / choose / simulate / resume / failover-proof
# ---------------------------------------------------------------------------


def _action_certify(**kwargs: Any) -> ActionResult:
    """Emit a runtime certification report (evidence only)."""
    from verdict.runtime_certification import ComponentKind, DetectedSnapshot, certify_runtime

    snapshot_path: str | None = kwargs.get("snapshot_path")
    snapshots: list[DetectedSnapshot] = []
    if snapshot_path:
        payload = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
        raw_items = payload.get("snapshots", payload if isinstance(payload, list) else [])
        for item in raw_items:
            snapshots.append(
                DetectedSnapshot(
                    component_id=item["component_id"],
                    kind=ComponentKind(item["kind"]),
                    identity=item["identity"],
                    source=item["source"],
                    health_claim=item.get("health_claim", "unknown"),
                    version=item.get("version"),
                    capabilities=frozenset(item.get("capabilities", [])),
                    requires_probe=bool(item.get("requires_probe", False)),
                    evidence=item.get("evidence", {}),
                    models=tuple(item.get("models", ())),
                )
            )
    report = certify_runtime(snapshots=tuple(snapshots))
    return ActionResult(data=report.to_dict())


def _action_choose(**kwargs: Any) -> ActionResult:
    """Select an eligible execution target for Prime dispatch.

    On ChooserError the payload mirrors the legacy ``cmd_choose`` contract so the
    CLI can render (and JSON callers keep) the same shape as origin/main.
    """
    from verdict.chooser import ChooserError, choose_route, load_candidates_json

    task_class: str = kwargs["task_class"]
    requires_raw: str = kwargs.get("requires", "") or ""
    requires = tuple(part.strip() for part in requires_raw.split(",") if part.strip())
    model: str | None = kwargs.get("model")
    candidates_json: str | None = kwargs.get("candidates_json")

    try:
        candidates_tuple = load_candidates_json(candidates_json) if candidates_json else ()
        decision = choose_route(
            candidates_tuple, task_class=task_class, requires=requires, explicit_model=model
        )
    except ChooserError as exc:
        payload: dict[str, Any] = {
            "task_class": task_class,
            "protected": task_class
            in {"architecture", "orchestration", "hard-debug", "final-review"},
            "selected": None,
            "reason": exc.reason,
            "error": str(exc),
            "exclusions": list(exc.exclusions),
            "policy_version": "chooser-policy/v1",
            "ranker_version": "chooser-ranker/v1",
            "explicit_model": model,
            "selected_because": f"failed because {exc.reason}",
        }
        return ActionResult(data=payload, ok=False, exit_code=1)
    return ActionResult(data=decision.to_dict())


def _action_simulate(**kwargs: Any) -> ActionResult:
    """Forecast tokens, cost, risk, and expected model before any paid call."""
    from verdict.models import TaskSpec
    from verdict.simulator import simulate

    task: str = kwargs["task"]
    criticality: str = kwargs.get("criticality", "medium")
    model_override: str | None = kwargs.get("model_override")
    catalog = kwargs.get("catalog")
    if catalog is None:
        from verdict.actions.helpers import default_model_catalog

        catalog = default_model_catalog()
    spec = TaskSpec(prompt=task, criticality=criticality)
    forecast = simulate(spec, model_catalog=catalog, model_override=model_override)
    return ActionResult(data=forecast.to_dict())


def _action_resume(**kwargs: Any) -> ActionResult:
    """Reconstruct durable resume state for a Linear story."""
    from verdict.resume import resume_story
    from verdict.worktree_registry import WorktreeRegistryError

    story: str = kwargs["story"]
    repo = Path(kwargs.get("repo") or Path.cwd())
    with_harness: str | None = kwargs.get("with_harness")
    create_if_missing: bool = kwargs.get("create_if_missing", False)
    try:
        payload = resume_story(
            repo, story, with_harness=with_harness, create_if_missing=create_if_missing
        )
    except WorktreeRegistryError as exc:
        return ActionResult(data={"error": str(exc), "story": story}, ok=False, exit_code=1)
    return ActionResult(data=payload)


def _action_failover_proof(**kwargs: Any) -> ActionResult:
    """Run the offline forced-failover and replay proof."""
    from verdict.failover_replay_proof import run_forced_failover_proof
    from verdict.memory_plane import MemoryPlane

    memory_path: str = kwargs["memory_path"]
    with MemoryPlane(memory_path) as plane:
        proof = run_forced_failover_proof(plane)
    return ActionResult(
        data={
            "session_id": proof.mission_id,
            "initial_model": "provider-a/model-a",
            "replacement_model": proof.replacement_model,
            "failure_status": 429,
            "completed_steps": list(proof.completed_stages),
            "event_sequence": [e.to_dict() for e in proof.events],
            "replay_digest": proof.digest,
        }
    )


# ---------------------------------------------------------------------------
# Memory family
# ---------------------------------------------------------------------------


def _memory_plane(kwargs: dict[str, Any]) -> Any:
    from verdict.memory_plane import MemoryPlane

    db_path = kwargs.get("db_path") or str(Path.home() / ".verdict" / "memory.db")
    return MemoryPlane(db_path)


def _action_memory_put(**kwargs: Any) -> ActionResult:
    from verdict.memory_plane import MemoryRecord

    plane = _memory_plane(kwargs)
    rec = MemoryRecord(
        record_id=f"rec_{kwargs['key']}",
        namespace=kwargs.get("namespace", "default"),
        key=kwargs["key"],
        content=kwargs["content"],
        source=kwargs.get("source", "cli"),
    )
    plane.put(rec)
    return ActionResult(data={"key": rec.key, "namespace": rec.namespace, "status": "stored"})


def _action_memory_search(**kwargs: Any) -> ActionResult:
    plane = _memory_plane(kwargs)
    results = plane.search(
        kwargs["query"], namespace=kwargs.get("namespace"), limit=kwargs.get("limit", 10)
    )
    return ActionResult(
        data={
            "query": kwargs["query"],
            "namespace": kwargs.get("namespace"),
            "count": len(results),
            "records": [r.to_dict() for r in results],
        }
    )


def _action_memory_export(**kwargs: Any) -> ActionResult:
    from verdict.memory_adapters import ImportPolicy, export_manifest

    plane = _memory_plane(kwargs)
    destination = Path(kwargs.get("output", "memory_manifest.json")).expanduser().resolve()
    policy = ImportPolicy((destination.parent,))
    report = export_manifest(
        plane.export_records(),
        destination,
        policy=policy,
        source="memory-plane",
        adapter_id="local-manifest",
    )
    return ActionResult(
        data={
            "destination": str(destination),
            "status": report.status,
            "errors": list(report.errors),
        },
        ok=report.status == "ok",
        exit_code=0 if report.status == "ok" else 1,
    )


def _action_memory_import(**kwargs: Any) -> ActionResult:
    from verdict.memory_adapters import ImportPolicy, import_manifest

    plane = _memory_plane(kwargs)
    source = Path(kwargs["manifest"]).expanduser().resolve()
    policy = ImportPolicy((source.parent,))
    records, report = import_manifest(source, policy=policy)
    count = plane.import_records(records)
    return ActionResult(
        data={
            "imported": count[0],
            "duplicates": report.duplicates,
            "manifest_hash": report.manifest_hash,
        }
    )


def _action_memory_masterdocs(**kwargs: Any) -> ActionResult:
    """Canonicalize a MasterDocs database and optionally import into memory plane."""
    from verdict.memory_masterdocs_adapter import MasterDocsAdapter

    adapter = MasterDocsAdapter()
    db = kwargs.get("db", "MasterDocsRAG.db")
    allow_legacy_sqlite = kwargs.get("allow_legacy_sqlite", False)
    limit = kwargs.get("limit", 1000)
    ingest_timestamp = kwargs.get("ingest_timestamp")
    dry_run = kwargs.get("dry_run", False)

    result = adapter.canonicalize_db_records(
        db, allow_legacy_sqlite=allow_legacy_sqlite, limit=limit, ingest_timestamp=ingest_timestamp
    )
    if result.report.status in {"unavailable", "rejected", "empty"}:
        return ActionResult(data=result.to_dict(), ok=False, exit_code=1)
    if dry_run:
        return ActionResult(data=result.to_dict())
    plane = _memory_plane(kwargs)
    imported_report = adapter.import_result(result, plane)
    payload = {
        "report": imported_report.to_dict(),
        "records": [dict(record) for record in result.records],
    }
    ok = not (imported_report.status in {"rejected", "partial"} and imported_report.ingested == 0)
    return ActionResult(data=payload, ok=ok, exit_code=0 if ok else 1)


def _action_memory_graph(**kwargs: Any) -> ActionResult:
    """Ingest a code-graph SQLite database into the memory plane."""
    from verdict.memory_graph_adapter import CodeGraphAdapter

    plane = _memory_plane(kwargs)
    db = kwargs.get("db", "code_graph.db")
    allow_legacy_sqlite = kwargs.get("allow_legacy_sqlite", False)
    graph_adapter = CodeGraphAdapter()
    graph_rep = graph_adapter.ingest_sqlite(db, plane, allow_legacy_sqlite=allow_legacy_sqlite)
    return ActionResult(data={"records_created": graph_rep.records_created})


def _action_memory_docs(**kwargs: Any) -> ActionResult:
    from verdict.documentation_preflight import run_documentation_preflight

    repo_root = Path(kwargs.get("repo_root", Path.cwd()))
    memory_path = Path(kwargs.get("db_path") or str(Path.home() / ".verdict" / "memory.db"))
    report = run_documentation_preflight(
        repo_root=repo_root, memory_path=memory_path, fix=kwargs.get("fix", False)
    )
    return ActionResult(
        data=report.to_dict(), ok=report.passed, exit_code=0 if report.passed else 1
    )


def _action_memory_setup(**kwargs: Any) -> ActionResult:
    from verdict.memory_bridge import configure_memory_bridge, detect_available_tools

    tools_raw = kwargs.get("tools")
    if tools_raw:
        tools_to_config = [t.strip() for t in tools_raw.split(",") if t.strip()]
    else:
        report = detect_available_tools()
        tools_to_config = list(report.preselected_tools)
    plane = _memory_plane(kwargs)
    res = configure_memory_bridge(tools_to_config, plane)
    return ActionResult(data={"detected_tools": tools_to_config, **res})


# ---------------------------------------------------------------------------
# MCP family (serve is MACHINE_ONLY)
# ---------------------------------------------------------------------------


def _action_mcp_init(**kwargs: Any) -> ActionResult:
    from verdict.memory_bridge import configure_memory_bridge

    res = configure_memory_bridge(selected_tools=["mcp", "codex", "claude"])
    return ActionResult(data=res)


def _action_mcp_status(**kwargs: Any) -> ActionResult:
    mcp_file = Path.cwd() / ".mcp.json"
    registered = False
    if mcp_file.exists():
        try:
            data = json.loads(mcp_file.read_text("utf-8"))
            servers = data.get("mcpServers", {})
            registered = "verdict-memory" in servers or "verdict-core" in servers
        except Exception:
            pass
    return ActionResult(data={"mcp_registered": registered, "mcp_config": str(mcp_file)})


# ---------------------------------------------------------------------------
# Hook family (claude-gate is MACHINE_ONLY)
# ---------------------------------------------------------------------------


def _action_hook_status(**kwargs: Any) -> ActionResult:
    db_path = kwargs.get("db_path") or str(Path.home() / ".verdict" / "memory.db")
    codex_agents = Path.home() / ".codex" / "AGENTS.md"
    claude_md = Path.cwd() / "CLAUDE.md"
    mcp_file = Path.cwd() / ".mcp.json"
    status: dict[str, bool] = {
        "codex_agents_md": codex_agents.exists()
        and "Verdict Unified Memory Bridge" in codex_agents.read_text(),
        "claude_md": claude_md.exists()
        and "Verdict Unified Memory Bridge" in claude_md.read_text(),
        "mcp_json": False,
        "memory_db": Path(db_path).exists(),
    }
    if mcp_file.exists():
        try:
            data = json.loads(mcp_file.read_text())
            status["mcp_json"] = "verdict-memory" in data.get(
                "mcpServers", {}
            ) or "verdict-core" in data.get("mcpServers", {})
        except Exception:
            pass
    return ActionResult(data=status)


def _action_hook_configure(**kwargs: Any) -> ActionResult:
    from verdict.memory_bridge import configure_memory_bridge

    tools_str = kwargs.get("tools")
    tools = [t.strip() for t in tools_str.split(",")] if tools_str else ["codex", "claude"]
    res = configure_memory_bridge(selected_tools=tools)
    return ActionResult(data=res)


def _action_hook_recall(**kwargs: Any) -> ActionResult:
    plane = _memory_plane(kwargs)
    results = plane.search(kwargs.get("query", ""), limit=kwargs.get("limit", 5))
    return ActionResult(data={"count": len(results), "records": [r.to_dict() for r in results]})


def _action_hook_record(**kwargs: Any) -> ActionResult:
    from verdict.memory_gate import MemoryGate, MemoryWriteRequest

    plane = _memory_plane(kwargs)
    gate = MemoryGate(plane)
    req = MemoryWriteRequest(
        namespace=kwargs.get("namespace", "sessions"),
        key=kwargs.get("key", "session"),
        value=kwargs.get("value", ""),
        source=kwargs.get("source", "cli"),
        authority="agent",
    )
    write = gate.write(req)
    return ActionResult(
        data={"allowed": write.allowed, "reason": str(write.reason) if not write.allowed else None},
        ok=bool(write.allowed),
        exit_code=0 if write.allowed else 1,
    )


# ---------------------------------------------------------------------------
# Runtime family
# ---------------------------------------------------------------------------


def _action_runtime_status(**kwargs: Any) -> ActionResult:
    from verdict.runtime_daemons import RuntimeManager

    report = RuntimeManager().status()
    return ActionResult(
        data=report.to_dict(), ok=report.passed, exit_code=0 if report.passed else 1
    )


def _action_runtime_explain(**kwargs: Any) -> ActionResult:
    from verdict.runtime_daemons import RuntimeManager
    from verdict.runtime_health import build_runtime_health_report

    report = build_runtime_health_report(RuntimeManager().status())
    return ActionResult(data=report.to_dict())


def _action_runtime_reconcile(**kwargs: Any) -> ActionResult:
    from verdict.runtime_daemons import RuntimeManager, RuntimeManagerError

    manager = RuntimeManager()
    apply_it: bool = kwargs.get("apply", False)
    consent: bool = kwargs.get("consent", False)
    service_ids = kwargs.get("service_ids") or [spec.service_id for spec in manager.specs]
    try:
        if apply_it:
            report = manager.reconcile_apply(service_ids=service_ids, consent=consent)
        else:
            report = manager.reconcile_plan()
    except RuntimeManagerError as exc:
        return ActionResult(data={"error": str(exc)}, ok=False, exit_code=2)
    return ActionResult(
        data=report.to_dict(), ok=report.passed, exit_code=0 if report.passed else 1
    )


# ---------------------------------------------------------------------------
# Prove-at-rest family (daemon is MACHINE_ONLY)
# ---------------------------------------------------------------------------


def _action_prove_at_rest_status(**kwargs: Any) -> ActionResult:
    from verdict.prove_at_rest import ProveAtRestStore, default_state_path

    state_path = kwargs.get("state_path")
    resolved = Path(state_path).expanduser() if state_path else default_state_path()
    cycle = ProveAtRestStore(path=resolved).read()
    if cycle is None:
        return ActionResult(data={"status": "empty", "state_path": str(resolved)})
    data = cycle.to_dict()
    data["state_path"] = str(resolved)
    return ActionResult(data=data)


def _action_prove_at_rest_once(**kwargs: Any) -> ActionResult:
    """Run one prove-at-rest cycle."""
    from verdict.prove_at_rest import ProveAtRestError, build_live_daemon, default_state_path

    allow_live_probe: bool = kwargs.get("allow_live_probe", False)
    if not allow_live_probe:
        return ActionResult(
            data={"error": "live prove-at-rest requires explicit consent; pass allow_live_probe"},
            ok=False,
            exit_code=2,
        )
    state_path = kwargs.get("state_path")
    resolved = Path(state_path).expanduser() if state_path else default_state_path()
    try:
        daemon = build_live_daemon(
            state_path=resolved,
            base_url=kwargs.get("base_url"),
            interval_seconds=kwargs.get("interval", 300.0),
            probe_timeout_seconds=kwargs.get("timeout", 15.0),
            allow_live_probe=True,
        )
    except ProveAtRestError as exc:
        return ActionResult(data={"error": str(exc)}, ok=False, exit_code=2)
    cycle = daemon.run_once()
    data = cycle.to_dict() if cycle is not None else {"status": "no_result"}
    ok = bool(cycle) and cycle.summary.get("failed", 0) == 0
    return ActionResult(data=data, ok=ok, exit_code=0 if ok else 1)


# ---------------------------------------------------------------------------
# Metadata family
# ---------------------------------------------------------------------------


def _action_metadata_show(**kwargs: Any) -> ActionResult:
    from verdict.metadata import ModelMetadataError, default_store_path, load_store

    store_path = kwargs.get("store_path")
    resolved = Path(store_path).expanduser() if store_path else default_store_path()
    try:
        snapshot = load_store(resolved)
    except (ModelMetadataError, OSError, json.JSONDecodeError) as exc:
        return ActionResult(data={"error": str(exc), "store": str(resolved)}, ok=False, exit_code=1)
    return ActionResult(
        data={
            "store": str(resolved),
            "schema_version": snapshot.schema_version,
            "refreshed_at": snapshot.refreshed_at,
            "record_count": len(snapshot.records),
            "sources": {name: status.to_dict() for name, status in snapshot.sources.items()},
            "drops": [item.to_dict() for item in snapshot.drops],
        }
    )


def _action_metadata_lookup(**kwargs: Any) -> ActionResult:
    from verdict.metadata import (
        ModelMetadataError,
        default_store_path,
        load_identity_map,
        load_store,
        lookup_omniroute_id,
    )

    store_path = kwargs.get("store_path")
    mapping_path = kwargs.get("mapping_path")
    required = tuple(
        item.strip() for item in str(kwargs.get("requires", "")).split(",") if item.strip()
    )
    resolved = Path(store_path).expanduser() if store_path else default_store_path()
    try:
        snapshot = load_store(resolved)
        mapping = load_identity_map(mapping_path)
        found = lookup_omniroute_id(
            snapshot, kwargs["omniroute_id"], required=required, identity_map=mapping
        )
    except (ModelMetadataError, OSError, json.JSONDecodeError) as exc:
        return ActionResult(data={"error": str(exc)}, ok=False, exit_code=1)
    return ActionResult(data=found.to_dict())


def _action_metadata_refresh(**kwargs: Any) -> ActionResult:
    from datetime import datetime, timezone

    from verdict.metadata import ModelMetadataError, file_transport, refresh_metadata

    store_path = kwargs.get("store_path")
    mapping_path = kwargs.get("mapping_path")
    models_dev_api_file = kwargs.get("models_dev_api_file")
    models_dev_models_file = kwargs.get("models_dev_models_file")
    litellm_file = kwargs.get("litellm_file")
    include_p1: bool = kwargs.get("include_p1", False)
    now = kwargs.get("now") or datetime.now(timezone.utc)

    offline = any(
        p is not None for p in (models_dev_api_file, models_dev_models_file, litellm_file)
    )
    try:
        transport = (
            file_transport(
                models_dev_api=(
                    json.loads(Path(models_dev_api_file).read_text(encoding="utf-8"))
                    if models_dev_api_file
                    else None
                ),
                models_dev_models=(
                    json.loads(Path(models_dev_models_file).read_text(encoding="utf-8"))
                    if models_dev_models_file
                    else None
                ),
                litellm=(
                    json.loads(Path(litellm_file).read_text(encoding="utf-8"))
                    if litellm_file
                    else None
                ),
                fetched_at=now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            )
            if offline
            else None
        )
        snapshot = refresh_metadata(
            transport=transport,
            mapping_path=mapping_path,
            store_path=store_path,
            include_p1=include_p1,
            now=now,
            persist=True,
        )
    except (ModelMetadataError, OSError, json.JSONDecodeError) as exc:
        return ActionResult(data={"error": str(exc)}, ok=False, exit_code=1)
    resolved_store = str(
        Path(store_path).expanduser()
        if store_path
        else Path.home() / ".verdict" / "model-metadata.json"
    )
    return ActionResult(
        data={
            "schema_version": snapshot.schema_version,
            "refreshed_at": snapshot.refreshed_at,
            "record_count": len(snapshot.records),
            "drop_count": len(snapshot.drops),
            "conflict_count": len(snapshot.conflicts),
            "sources": {name: status.to_dict() for name, status in snapshot.sources.items()},
            "mapping": snapshot.mapping,
            "store": resolved_store,
        }
    )


# ---------------------------------------------------------------------------
# Receipt family (show is already registered)
# ---------------------------------------------------------------------------


def _action_receipt_list(**kwargs: Any) -> ActionResult:
    from verdict.receipt_store import ReceiptStore
    from verdict.routing_receipt import load_routing_receipt

    db_path = kwargs.get("db_path")
    scope = kwargs.get("scope")
    if db_path:
        db = Path(db_path)
    else:
        repo_db = Path.cwd() / ".verdict" / "receipts.db"
        db = repo_db if repo_db.exists() else (Path.home() / ".verdict" / "receipts.db")
    store = ReceiptStore(db, strict_scope=(scope is not None))
    rows = store.query_receipts(receipt_type="decision", scope=scope, limit=100)
    items: list[dict[str, Any]] = []
    for row in rows:
        if row.parent_receipt_id:
            continue
        payload = row.payload
        if payload.get("schema_version") != "routing-receipt/v1":
            continue
        latest = load_routing_receipt(
            store, receipt_id=row.receipt_id, scope=row.scope, attempt_id=payload.get("attempt_id")
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
    return ActionResult(data={"receipts": items})


def _action_receipt_export(**kwargs: Any) -> ActionResult:
    """Export routing receipts (delegated to CLI handler which streams full data)."""
    return ActionResult(data={"status": "delegated", "surface": "receipt.export"})


# ---------------------------------------------------------------------------
# OpenSpec
# ---------------------------------------------------------------------------


def _action_openspec_admit(**kwargs: Any) -> ActionResult:
    """Admit a significant OpenSpec change.

    The CLI parser resolves the slug/path into an ``OpenSpecChange`` instance
    (or ``None`` if a discovery fallback is in play) before invoking this
    action, so we accept only that resolved form.
    """
    from verdict.openspec_lifecycle import OpenSpecChange, admit_significant_change

    change_ref = kwargs.get("change")
    repo = Path(kwargs.get("repo") or Path.cwd())
    if change_ref is None:
        change: OpenSpecChange | None = None
    elif isinstance(change_ref, OpenSpecChange):
        change = change_ref
    else:
        return ActionResult(
            data={
                "error": (
                    "openspec.admit requires a resolved OpenSpecChange instance; "
                    "the CLI parser is the resolver, not this action"
                )
            },
            ok=False,
            exit_code=1,
        )
    result = admit_significant_change(change, repo)
    data = {"admitted": result.admitted, "reason": result.reason, "repo": str(repo)}
    return ActionResult(data=data, ok=bool(result.admitted), exit_code=0 if result.admitted else 1)


# ---------------------------------------------------------------------------
# Autodev packet family (execute is LAUNCH)
# ---------------------------------------------------------------------------


def _action_autodev_packet_create(**kwargs: Any) -> ActionResult:
    from verdict.execution_packet import (
        ExecutionPacket,
        ExecutionPacketError,
        ExecutionPacketStore,
        UnsupportedSchemaVersionError,
        schema_refusal_receipt,
    )

    packet_path = Path(kwargs["packet_path"]).expanduser().resolve()
    source_path = kwargs.get("source_path")
    if source_path is None:
        return ActionResult(
            data={"error": "packet create requires source_path"}, ok=False, exit_code=1
        )
    try:
        payload = json.loads(Path(source_path).expanduser().read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return ActionResult(
                data={"error": "packet source JSON must be an object"}, ok=False, exit_code=1
            )
        packet = ExecutionPacket.from_dict(payload)
        ExecutionPacketStore(packet_path.parent).create(packet, packet_path)
    except UnsupportedSchemaVersionError as exc:
        return ActionResult(data=schema_refusal_receipt(exc), ok=False, exit_code=1)
    except (ExecutionPacketError, OSError, ValueError) as exc:
        return ActionResult(data={"error": str(exc)}, ok=False, exit_code=1)
    return ActionResult(data=packet.to_dict())


def _action_autodev_packet_inspect(**kwargs: Any) -> ActionResult:
    from verdict.execution_packet import (
        ExecutionPacketError,
        ExecutionPacketStore,
        UnsupportedSchemaVersionError,
        schema_refusal_receipt,
    )

    packet_path = Path(kwargs["packet_path"]).expanduser().resolve()
    try:
        packet = ExecutionPacketStore(packet_path.parent).validate(packet_path)
    except UnsupportedSchemaVersionError as exc:
        return ActionResult(data=schema_refusal_receipt(exc), ok=False, exit_code=1)
    except (ExecutionPacketError, OSError, ValueError) as exc:
        return ActionResult(data={"error": str(exc)}, ok=False, exit_code=1)
    return ActionResult(data=packet.to_dict())


def _action_autodev_packet_validate(**kwargs: Any) -> ActionResult:
    return _action_autodev_packet_inspect(**kwargs)


def _action_autodev_packet_resume(**kwargs: Any) -> ActionResult:
    from verdict.execution_packet import (
        ExecutionPacketError,
        ExecutionPacketStore,
        UnsupportedSchemaVersionError,
        schema_refusal_receipt,
    )

    packet_path = Path(kwargs["packet_path"]).expanduser().resolve()
    model = kwargs.get("model")
    if model is None:
        return ActionResult(data={"error": "packet resume requires model"}, ok=False, exit_code=1)
    try:
        packet = ExecutionPacketStore(packet_path.parent).resume(packet_path, executing_model=model)
    except UnsupportedSchemaVersionError as exc:
        return ActionResult(data=schema_refusal_receipt(exc), ok=False, exit_code=1)
    except (ExecutionPacketError, OSError, ValueError) as exc:
        return ActionResult(data={"error": str(exc)}, ok=False, exit_code=1)
    payload = packet.to_dict()
    payload["executing_model"] = model
    return ActionResult(data=payload)


def _action_autodev_packet_compare(**kwargs: Any) -> ActionResult:
    from verdict.autodev_run import AutodevError, compare_family_runs
    from verdict.execution_packet import ExecutionPacketError, ExecutionPacketStore

    packet_path = Path(kwargs["packet_path"]).expanduser().resolve()
    family_a_path = kwargs.get("family_a_path")
    family_b_path = kwargs.get("family_b_path")
    if not family_a_path or not family_b_path:
        return ActionResult(
            data={"error": "packet compare requires family_a_path and family_b_path"},
            ok=False,
            exit_code=1,
        )
    try:
        packet = ExecutionPacketStore(packet_path.parent).validate(packet_path)
        family_a = json.loads(Path(family_a_path).expanduser().read_text(encoding="utf-8"))
        family_b = json.loads(Path(family_b_path).expanduser().read_text(encoding="utf-8"))
        data = compare_family_runs(family_a, family_b, packet=packet)
    except (ExecutionPacketError, AutodevError, OSError, ValueError) as exc:
        return ActionResult(data={"error": str(exc)}, ok=False, exit_code=1)
    return ActionResult(data=data)


def _action_autodev_packet_shadow(**kwargs: Any) -> ActionResult:
    from verdict.autodev_run import shadow_learning_report

    episodes_path = kwargs["episodes_path"]
    payload = json.loads(Path(episodes_path).expanduser().resolve().read_text(encoding="utf-8"))
    episodes = (
        payload["episodes"] if isinstance(payload, dict) and "episodes" in payload else payload
    )
    if not isinstance(episodes, list):
        return ActionResult(data={"error": "episodes must be a list"}, ok=False, exit_code=1)
    return ActionResult(data=shadow_learning_report(episodes))


def _action_autodev_packet_canary(**kwargs: Any) -> ActionResult:
    from verdict.autodev_run import apply_shadow_canary, shadow_learning_report

    episodes_path = kwargs["episodes_path"]
    admitted_path = kwargs["admitted_path"]
    payload = json.loads(Path(episodes_path).expanduser().resolve().read_text(encoding="utf-8"))
    episodes = (
        payload["episodes"] if isinstance(payload, dict) and "episodes" in payload else payload
    )
    admitted = json.loads(Path(admitted_path).expanduser().resolve().read_text(encoding="utf-8"))
    if not isinstance(episodes, list) or not isinstance(admitted, list):
        return ActionResult(
            data={"error": "episodes and admitted must be lists"}, ok=False, exit_code=1
        )
    report = shadow_learning_report(episodes)
    return ActionResult(data=apply_shadow_canary(admitted, report))


# ---------------------------------------------------------------------------
# Harness family
# ---------------------------------------------------------------------------


def _harness_module(name: str) -> Any:
    import importlib

    return importlib.import_module(f"verdict.harness_{name}")


def _action_harness_status(**kwargs: Any) -> ActionResult:
    name: str = kwargs["harness"]
    module = _harness_module(name)
    report = module.status()
    return ActionResult(data=report.__dict__ if hasattr(report, "__dict__") else {"harness": name})


def _action_harness_enable(**kwargs: Any) -> ActionResult:
    name: str = kwargs["harness"]
    module = _harness_module(name)
    result = module.enable(
        base_url=kwargs.get("base_url"),
        token_env=kwargs.get("token_env"),
        force=kwargs.get("force", False),
    )
    payload = result.__dict__ if hasattr(result, "__dict__") else {"harness": name}
    return ActionResult(data=payload)


def _action_harness_disable(**kwargs: Any) -> ActionResult:
    name: str = kwargs["harness"]
    module = _harness_module(name)
    module.disable()
    return ActionResult(data={"harness": name, "status": "disabled"})


def _action_harness_certify(**kwargs: Any) -> ActionResult:
    name: str = kwargs["harness"]
    module = _harness_module(name)
    if hasattr(module, "certify"):
        report = module.certify()
        return ActionResult(
            data=report.__dict__ if hasattr(report, "__dict__") else {"harness": name}
        )
    return ActionResult(
        data={"error": f"{name} harness has no certify surface"}, ok=False, exit_code=1
    )


def _action_harness_discover(**kwargs: Any) -> ActionResult:
    name: str = kwargs["harness"]
    module = _harness_module(name)
    if hasattr(module, "discover"):
        report = module.discover()
        return ActionResult(
            data=report.__dict__ if hasattr(report, "__dict__") else {"harness": name}
        )
    return ActionResult(
        data={"error": f"{name} harness has no discover surface"}, ok=False, exit_code=1
    )


def _action_harness_prime_sync_models(**kwargs: Any) -> ActionResult:
    from verdict.harness_prime import HarnessPrimeError, sync_models
    from verdict.orchestration.run import fetch_inventory, resolve_api_key

    gateway = str(kwargs.get("gateway", "http://127.0.0.1:20128")).rstrip("/")
    if gateway.endswith("/v1"):
        gateway = gateway[: -len("/v1")]
    try:
        rows = fetch_inventory(gateway, api_key=resolve_api_key())
        result = sync_models(rows, dry_run=bool(kwargs.get("dry_run", False)))
    except HarnessPrimeError as exc:
        return ActionResult(data={"error": str(exc)}, ok=False, exit_code=1)
    except Exception as exc:
        return ActionResult(data={"error": str(exc)}, ok=False, exit_code=2)
    payload = result.__dict__ if hasattr(result, "__dict__") else {"gateway": gateway}
    return ActionResult(data=payload)


def _action_autodev_packet_canary_rollback(**kwargs: Any) -> ActionResult:
    """Restore the pre-canary baseline choice. Does not call EligibilityGate."""
    import json as _json
    from pathlib import Path as _Path

    from verdict.autodev_run import rollback_shadow_canary

    state_path: str = kwargs["state_path"]
    state = _json.loads(_Path(state_path).expanduser().resolve().read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        return ActionResult(
            data={"error": "canary rollback requires a canary state object"}, ok=False, exit_code=1
        )
    data = rollback_shadow_canary(state)
    return ActionResult(data=data)


def _action_harness_prime_visibility(**kwargs: Any) -> ActionResult:
    from verdict.orchestration.cli import prime_visibility_report
    from verdict.orchestration.run import fetch_inventory, resolve_api_key

    gateway = str(kwargs.get("gateway", "http://127.0.0.1:20128")).rstrip("/")
    if gateway.endswith("/v1"):
        gateway = gateway[: -len("/v1")]
    try:
        rows = fetch_inventory(gateway, api_key=resolve_api_key())
    except Exception as exc:
        return ActionResult(data={"error": str(exc)}, ok=False, exit_code=2)
    return ActionResult(data=prime_visibility_report(rows))
