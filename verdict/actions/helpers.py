"""Action helpers — domain functions extracted from CLI for correct dependency direction.

These functions provide the service-layer logic that actions need. CLI imports
from here (or re-exports for backward compat), not the other way around.
"""

from __future__ import annotations

import os
import sys
from typing import Any


def default_model_catalog() -> list[Any]:
    """Build the default catalog from the configured verdict.yaml and classified tiers."""
    import yaml

    from verdict.models import ModelInfo

    models: list[ModelInfo] = []
    config_dir = os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "verdict"
    )
    config_path = os.path.join(config_dir, "verdict.yaml")
    raw: dict[str, Any] = {}
    if os.path.exists(config_path):
        with open(config_path) as f:
            loaded = yaml.safe_load(f)
            if isinstance(loaded, dict):
                raw = loaded

    from verdict.classifier import classify
    from verdict.contracts import DEFAULT_PRIMARY_MODEL

    _default_primary = DEFAULT_PRIMARY_MODEL
    primary = str(raw.get("primary_model", _default_primary))
    models.append(
        ModelInfo(
            id=primary,
            provider=primary.split("/", 1)[0] if "/" in primary else "unknown",
            capability_tier=classify(primary),
            context_window=200_000,
        )
    )
    seen = {primary}
    providers = raw.get("providers") or {}
    if isinstance(providers, dict):
        for name, provider in providers.items():
            if not isinstance(provider, dict):
                continue
            for model_id in provider.get("models") or {}:
                if model_id in seen:
                    continue
                seen.add(model_id)
                models.append(
                    ModelInfo(id=model_id, provider=name, capability_tier=classify(model_id))
                )
    return models


#: Interactive-CLI default provider set.
_CLI_DEFAULT_PROVIDERS = {"public_ollama": "http://localhost:11434/v1"}


def _cli_bootstrap(*, require_authoritative: bool = False) -> Any:
    """Resolve the CLI provider/gateway bootstrap from the shared contract."""
    from verdict.provider_bootstrap import resolve_provider_bootstrap

    return resolve_provider_bootstrap(
        allow_default_providers=True,
        default_providers=_CLI_DEFAULT_PROVIDERS,
        require_authoritative=require_authoritative,
    )


def _report_bootstrap_notes(result: Any) -> None:
    """Print non-fatal bootstrap findings so no default stays silent."""
    for note in result.notes():
        if note.code == "config_file_missing":
            continue
        print(f"verdict: {note.describe()}", file=sys.stderr)


def build_route_gate(allow_offline: bool = False) -> Any:
    """Build the CLI Gate from the shared bootstrap contract.

    Wraps the bootstrap/gate construction so actions can call it without
    importing the CLI module.
    """
    from verdict.gate import Gate
    from verdict.provider_bootstrap import BootstrapError, describe_bootstrap_failure

    try:
        bootstrap = _cli_bootstrap()
    except BootstrapError as exc:
        print(describe_bootstrap_failure(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    _report_bootstrap_notes(bootstrap)
    return Gate(
        primary_model=bootstrap.primary_model,
        providers=bootstrap.provider_configs(),
        log_path=bootstrap.log_path,
        allow_offline=allow_offline,
    )


def probe_result_payload(observation: Any) -> dict[str, Any]:
    """Convert a probe observation to a credential-safe result dict."""
    status = str(observation.status)
    http_status = observation.http_status
    http_success = isinstance(http_status, int) and 200 <= http_status < 300
    ok = http_success and status == "ready"
    return {
        "model": observation.model_id,
        "ok": ok,
        "status": status,
        "availability_state": observation.availability_state,
        "http_status": http_status,
        "latency_ms": observation.latency_ms,
        "usage_available": observation.usage_available,
        "prompt_tokens": observation.prompt_tokens,
        "completion_tokens": observation.completion_tokens,
        "total_tokens": observation.total_tokens,
        "error_class": observation.error_class,
        "error": observation.error,
    }


def collect_doctor_diagnostics(
    fix: bool = False,
    interactive: bool = False,
    preflight_timeout: float = 120.0,
    progress: Any = None,
) -> Any:
    """Collect doctor diagnostics using the real diagnostic pipeline.

    The canonical implementation lives in ``verdict.doctor_diagnostics``.
    """
    from verdict.doctor_diagnostics import _collect_doctor_diagnostics

    return _collect_doctor_diagnostics(
        fix, interactive=interactive, preflight_timeout=preflight_timeout, progress=progress
    )


def _gateway_url() -> str:
    """Resolve the OmniRoute gateway base URL from environment."""
    return (
        os.environ.get("OMNIROUTE_BASE_URL")
        or os.environ.get("VERDICT_GATEWAY", "http://127.0.0.1:20128")
    ).rstrip("/")


def _prov_value(field: Any) -> Any:
    """Extract .value from a ProvenancedField, or return None."""
    return field.value if field is not None else None


def inventory_model_catalog() -> tuple[list[dict[str, Any]], str | None]:
    """Build the authoritative model inventory from live gateway + metadata store.

    Returns ``(rows, inventory_error)``.  When the gateway is unreachable the
    second element carries the error message and the rows fall back to the
    config-only catalog (same as ``default_model_catalog`` but with the
    enriched row shape).
    """
    from verdict.classifier import classify

    # --- 1. Try fetching the live inventory from OmniRoute -----------------
    gateway = _gateway_url()
    inventory_error: str | None = None
    raw_rows: list[dict[str, Any]] = []
    try:
        from verdict.orchestration.run import fetch_inventory, resolve_api_key

        key = resolve_api_key()
        raw_rows = fetch_inventory(gateway, api_key=key)
    except Exception as exc:
        inventory_error = f"gateway unreachable: {type(exc).__name__}: {exc}"

    # --- 2. Load the metadata store for enrichment -------------------------
    metadata_index: dict[str, Any] = {}
    metadata_freshness: str | None = None
    try:
        from verdict.metadata.store import load_store

        snapshot = load_store()
        metadata_index = snapshot.index_omniroute()
        metadata_freshness = snapshot.refreshed_at
    except Exception:
        pass  # metadata unavailable — fields stay None/UNKNOWN

    # --- 3. Build config models (always present, marked source=config) -----
    config_catalog = default_model_catalog()

    # --- 4. If gateway failed, return config-only rows ---------------------
    if inventory_error:
        rows: list[dict[str, Any]] = []
        for m in config_catalog:
            rec = metadata_index.get(m.id)
            rows.append(
                _enrich_row(
                    m.id,
                    m.provider,
                    rec,
                    source="config",
                    classify_fn=classify,
                    freshness=metadata_freshness,
                )
            )
        return rows, inventory_error

    # --- 5. Join inventory rows with metadata ------------------------------
    rows = []
    seen: set[str] = set()
    # Config models first (mark them).
    for m in config_catalog:
        seen.add(m.id)
        rec = metadata_index.get(m.id)
        rows.append(
            _enrich_row(
                m.id,
                m.provider,
                rec,
                source="config",
                classify_fn=classify,
                freshness=metadata_freshness,
            )
        )

    for raw in raw_rows:
        model_id = str(raw.get("id", ""))
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        provider = model_id.split("/", 1)[0] if "/" in model_id else "unknown"
        rec = metadata_index.get(model_id)
        rows.append(
            _enrich_row(
                model_id,
                provider,
                rec,
                source="inventory",
                classify_fn=classify,
                freshness=metadata_freshness,
            )
        )

    return rows, None


def _enrich_row(
    model_id: str,
    provider: str,
    record: Any,
    *,
    source: str,
    classify_fn: Any,
    freshness: str | None,
) -> dict[str, Any]:
    """Build a single enriched model row from inventory + metadata."""
    tier = classify_fn(model_id)
    tier_label = {0: "frontier", 1: "strong", 2: "standard", 3: "economy"}.get(tier, f"T{tier}")

    context_window: int | None = None
    tools_support: bool | None = None
    structured_output: bool | None = None
    input_cost: float | None = None
    output_cost: float | None = None

    if record is not None:
        caps = getattr(record, "caps", None)
        if caps is not None:
            context_window = _prov_value(caps.context) if _prov_value(caps.context) else None
            tools_support = _prov_value(caps.tools)
            structured_output = _prov_value(caps.structured)
            icpm = _prov_value(caps.input_cost_per_million)
            ocpm = _prov_value(caps.output_cost_per_million)
            input_cost = float(icpm) if icpm is not None else None
            output_cost = float(ocpm) if ocpm is not None else None

    return {
        "id": model_id,
        "provider": provider,
        "capability_tier": tier_label,
        "context_window": context_window,
        "tools_support": tools_support,
        "structured_output": structured_output,
        "input_cost_per_million": input_cost,
        "output_cost_per_million": output_cost,
        "source": source,
        "freshness": freshness,
    }
