"""Doctor diagnostics — canonical implementation of ``verdict doctor`` checks.

Moved from ``verdict/cli.py`` so that ``verdict/actions`` can import domain
logic without a back-edge into the CLI layer.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from rich.prompt import Prompt

# ---------------------------------------------------------------------------
# _cli_bootstrap is canonical in verdict.actions.helpers; import it here so
# _doctor_gateway_lifecycle and _collect_doctor_diagnostics keep working.
# ---------------------------------------------------------------------------
from verdict.actions.helpers import _cli_bootstrap

# ---------------------------------------------------------------------------
# _DOCTOR_NETWORK_ERROR_MARKERS — substrings that indicate a transient
# third-party network problem in documentation-preflight reports.
# ---------------------------------------------------------------------------
_DOCTOR_NETWORK_ERROR_MARKERS = (
    "rate limit",
    "http error 429",
    "urlerror",
    "timed out",
    "connection refused",
    "name or service not known",
)


# ---------------------------------------------------------------------------
# OmniRoute API utilities — copied from cli.py to avoid a circular import.
# ---------------------------------------------------------------------------
def _read_omniroute_token() -> str | None:
    """Read an explicitly configured OmniRoute token without private-database access."""
    return os.getenv("OMNIROUTE_API_KEY")


def _omniroute_api_request(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    """Make an authenticated request to the explicitly configured local router."""
    token = _read_omniroute_token()
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    import urllib.request
    from urllib.error import URLError

    base_url = os.getenv("OMNIROUTE_BASE_URL")
    if not base_url:
        for candidate_url in ("http://localhost:20128", "http://localhost:20129"):
            try:
                health_req = urllib.request.Request(
                    candidate_url.rstrip("/") + "/api/health",
                    headers={"Accept": "application/json"},
                    method="GET",
                )
                with urllib.request.urlopen(health_req, timeout=2) as resp:  # nosec B310
                    payload = json.loads(resp.read().decode("utf-8"))
                    if isinstance(payload, dict) and payload.get("status") == "ok":
                        base_url = candidate_url
                        break
            except (URLError, Exception):
                continue
        if not base_url:
            return None
    url = base_url.rstrip("/") + "/" + path.lstrip("/")

    data = json.dumps(body).encode("utf-8") if body is not None else None
    if data:
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:  # nosec B310
            return json.loads(response.read().decode("utf-8"))
    except (URLError, Exception):
        return None


def _doctor_documentation_preflight_is_network_only_failure(report: Any) -> bool:
    """Return True only when a blocked documentation preflight is explained
    entirely by a transient third-party network/rate-limit condition.

    The preflight is network-only iff:

    * the local documentation set has no real gaps (``missing == 0``,
      ``stale == 0`` and ``orphaned == 0``), and
    * there is at least one error, and every error is a ``resolve`` or
      ``inventory`` fetch error that carries one of
      ``_DOCTOR_NETWORK_ERROR_MARKERS``.

    A bare ``HTTP Error 403`` (auth/permission) is NOT network-only; a 403
    counts only when the same error text also says ``rate limit``. Anything
    else is a real issue.
    """
    if getattr(report, "missing", 0) or getattr(report, "stale", 0):
        return False
    if getattr(report, "orphaned", 0):
        return False
    errors = tuple(getattr(report, "errors", ()) or ())
    if not errors:
        return False
    for error in errors:
        parts = error.split(":", 2)
        if len(parts) < 3 or parts[1] not in {"resolve", "inventory"}:
            return False
        lowered = error.lower()
        if not any(marker in lowered for marker in _DOCTOR_NETWORK_ERROR_MARKERS):
            return False
    return True


class DoctorDiagnostics:
    """Result of the single shared ``verdict doctor`` diagnostics collector.

    Both text and ``--json`` modes render this same object, so they always
    agree on ``issues`` (exit 1 iff non-empty) and ``warnings`` (non-fatal).
    ``sections`` holds ``(label, state, detail)`` rows for text rendering;
    ``state`` ``"section"`` / ``"header"`` render a heading, ``"note"`` a dim line.
    """

    def __init__(self) -> None:
        self.issues: list[str] = []
        self.warnings: list[str] = []
        self.fixed: list[str] = []
        self.sections: list[tuple[str, str, str]] = []
        self.capability_report: dict[str, Any] = {}
        self.documentation_preflight: dict[str, Any] = {}
        self.shared_memory: Any = None
        self.config_loaded: bool = False
        #: Report-only gateway lifecycle state. ``doctor`` never starts a gateway.
        self.gateway_lifecycle: dict[str, Any] = {}


DOCTOR_PREFLIGHT_TIMEOUT_DEFAULT = 120.0


def _doctor_progress(message: str) -> None:
    """Write one doctor progress line to stderr so ``--json`` stdout stays pure."""
    print(f"  {message}", file=sys.stderr, flush=True)


def _doctor_fix_gateway(
    config: dict[str, Any],
    config_path: str,
    sections: list[tuple[str, str, str]],
    fixed_issues: list[str],
) -> str | None:
    """Persist the single healthy local gateway for an old config under --fix.

    Writes ``gateway_url`` into ``verdict.yaml`` and, when the credential
    store has no ``OMNIROUTE_BASE_URL`` yet, stores the same URL there (a URL,
    not a secret). The process environment is never mutated. Returns the
    repaired URL, or ``None`` when nothing was repaired.
    """
    from verdict import provider_detection
    from verdict.credentials_store import CredentialsStore

    try:
        healthy = [g for g in provider_detection.probe_gateways() if g.health_ok]
    except Exception as exc:
        sections.append(("Gateway detection", "warn", str(exc)))
        return None
    if len(healthy) != 1:
        if len(healthy) > 1:
            sections.append(
                (
                    "Gateway detection",
                    "warn",
                    "Multiple healthy gateways found; set OMNIROUTE_BASE_URL to choose one.",
                )
            )
        return None
    url = healthy[0].url
    config["gateway_url"] = url
    try:
        with open(config_path, "w") as f:
            yaml.safe_dump(config, f, default_flow_style=False)
    except Exception as exc:
        sections.append(("Gateway repair failed", "failed", str(exc)))
        return None
    sections.append(("Gateway configured", "ok", f"gateway_url: {url}"))
    fixed_issues.append("No gateway URL configured")
    try:
        store = CredentialsStore()
        if "OMNIROUTE_BASE_URL" not in store.load():
            store.set("OMNIROUTE_BASE_URL", url)
            sections.append(("Stored credential", "ok", "OMNIROUTE_BASE_URL"))
            fixed_issues.append("Required credential OMNIROUTE_BASE_URL is not set")
    except PermissionError as exc:
        sections.append(("Credential store", "failed", str(exc)))
    return url


def _gateway_probe_for(bootstrap: Any) -> Any:
    """Readiness probe carrying the gateway credential, when one is configured.

    A gateway that requires a key answers 401 to an unauthenticated probe, which
    would be reported as unhealthy: ``doctor`` would call a healthy gateway broken,
    and ensure would fail closed against it. The key is read by the name the
    provider binding declares (``api_key_env``), from the exported environment and
    then the credential store, and is never printed.
    """
    from verdict.gateway_lifecycle import authenticated_gateway_probe
    from verdict.provider_bootstrap import load_credential_store_env

    names = [
        binding.api_key_env
        for binding in bootstrap.providers.values()
        if getattr(binding, "api_key_env", None)
        and binding.base_url.rstrip("/") == (bootstrap.gateway_url or "").rstrip("/")
    ]
    api_key: str | None = None
    store: dict[str, str] | None = None
    for name in names:
        api_key = (os.getenv(str(name)) or "").strip() or None
        if api_key is None:
            if store is None:
                store = load_credential_store_env()
            api_key = (store.get(str(name)) or "").strip() or None
        if api_key is not None:
            break
    return authenticated_gateway_probe(api_key)


def _doctor_gateway_lifecycle(diag: DoctorDiagnostics) -> None:
    """Report gateway readiness in ``verdict doctor``. Never starts anything.

    The requirement and URL come from the shared bootstrap contract, and the
    state comes from ``inspect_gateway``, which probes at most once and never
    launches, signals or locks. Exit status is unchanged: an unready gateway is
    already reported by the reachability check above, so this section adds the
    named state rather than a second failure.
    """
    from verdict.gateway_lifecycle import inspect_gateway
    from verdict.provider_bootstrap import BootstrapError

    try:
        bootstrap = _cli_bootstrap()
        outcome = inspect_gateway(bootstrap, probe=_gateway_probe_for(bootstrap))
    except BootstrapError as exc:
        diag.gateway_lifecycle = {"state": "unknown", "reason": exc.reason_code}
        diag.sections.append(("Gateway lifecycle", "warn", exc.reason_code))
        return
    diag.gateway_lifecycle = outcome.to_dict()
    state = "ok" if outcome.ready else "warn"
    diag.sections.append(("Gateway lifecycle", state, outcome.describe()))
    if outcome.diagnostic is not None:
        diag.sections.append(("Gateway remediation", "note", outcome.diagnostic.remediation))


def _collect_doctor_diagnostics(
    fix: bool,
    *,
    interactive: bool,
    preflight_timeout: float = DOCTOR_PREFLIGHT_TIMEOUT_DEFAULT,
    progress: Callable[[str], None] | None = None,
) -> DoctorDiagnostics:
    """Run every ``verdict doctor`` check once and return the shared result.

    ``interactive`` controls only whether duplicate OmniRoute nodes may be
    removed after a confirmation prompt (never in ``--json`` mode, which must
    keep stdout machine-readable).

    ``preflight_timeout`` bounds the documentation preflight in seconds
    (``0`` or less means unbounded). ``progress``, when given, receives short
    status lines before and during the (possibly slow) preflight work.
    """
    _omniroute_api_req = _omniroute_api_request

    diag = DoctorDiagnostics()
    sections = diag.sections
    issues_found = diag.issues
    warnings_found = diag.warnings
    fixed_issues = diag.fixed

    from verdict.capability_bootstrap import doctor_capability_report

    capability_report = doctor_capability_report()
    diag.capability_report = capability_report
    capabilities = capability_report.get("capabilities", [])
    if not isinstance(capabilities, list):
        capabilities = []
    covered = sum(
        1 for item in capabilities if isinstance(item, dict) and item.get("status") == "covered"
    )
    total = len(capabilities)
    sections.append(("Capability coverage", "ok", f"{covered}/{total} covered (bootstrap view)"))

    from verdict.documentation_preflight import run_documentation_preflight

    if progress is not None:
        progress("checking documentation memory (may take a while)...")
    deadline_seconds = preflight_timeout if preflight_timeout > 0 else None
    documentation_report = run_documentation_preflight(
        fix=fix, progress=progress, deadline_seconds=deadline_seconds
    )
    diag.documentation_preflight = documentation_report.to_dict()
    doc_timed_out = bool(getattr(documentation_report, "timed_out", False))
    network_only_doc_failure = (
        not doc_timed_out
        and not documentation_report.passed
        and _doctor_documentation_preflight_is_network_only_failure(documentation_report)
    )
    doc_state = (
        "ok" if documentation_report.passed else "warning" if network_only_doc_failure else "failed"
    )
    sections.append(
        (
            "Documentation preflight",
            doc_state,
            f"{documentation_report.status} ({documentation_report.inventory} documents, "
            f"{documentation_report.ingested} ingested, "
            f"{documentation_report.stale} stale, "
            f"{documentation_report.missing} missing)",
        )
    )
    if doc_timed_out:
        # An incomplete scan never counts as ready or as a transient network
        # warning: verification did not finish, so it is an unresolved issue.
        issues_found.append(
            f"Documentation preflight timed out after {preflight_timeout:g}s before every "
            "document was checked. Rerun with 'verdict doctor --preflight-timeout 0' "
            "(unbounded) or a larger value."
        )
        issues_found.extend(documentation_report.errors)
    elif not documentation_report.passed:
        if network_only_doc_failure:
            # A rate-limited/unreachable third-party GitHub source with no
            # local documentation gap is a transient network condition, not
            # a real problem with this host's routing setup.
            warnings_found.extend(
                ["authoritative documentation preflight unreachable", *documentation_report.errors]
            )
        else:
            issues_found.extend(
                ["authoritative documentation preflight did not pass", *documentation_report.errors]
            )
    elif fix and documentation_report.ingested:
        fixed_issues.append("authoritative documentation preflight repaired")

    # Memory bridge (~/.verdict/memory.db, ./.mcp.json) and shared memory.
    from verdict.memory_bridge import run_doctor_diagnostics

    memory_report = run_doctor_diagnostics(home_dir=Path.home(), cwd=Path.cwd(), fix=fix)
    memory_issues = [str(item) for item in memory_report.get("issues", [])]
    memory_repaired = [str(item) for item in memory_report.get("repaired", [])]
    fixed_issues.extend(memory_repaired)
    # Exit status reflects the state AFTER --fix: drop findings that --fix repaired.
    repaired_by = {
        "missing_memory_db": "created_verdict_dir",
        "missing_memory_db_file": "initialized_memory_db",
        "missing_mcp_config": "created_mcp_config",
    }
    issues_found.extend(
        issue for issue in memory_issues if repaired_by.get(issue) not in memory_repaired
    )
    warnings_found.extend(
        str(warning)
        for warning in memory_report.get("warnings", [])
        if repaired_by.get(str(warning)) not in memory_repaired
    )
    shared_memory = memory_report.get("shared_memory") or {}

    # BOD-80 AC 9: when a provider IS configured, run diagnose_shared_memory
    # with the real health data so unreachable / unwritable / schema_incompatible
    # surface in the doctor output with named reasons.
    if isinstance(shared_memory, dict) and shared_memory.get("configured"):
        from verdict.runtime_certification import diagnose_shared_memory

        sm_diagnosis = diagnose_shared_memory(health_fn=lambda: shared_memory)
        shared_memory["diagnosis_state"] = sm_diagnosis.state.value
        shared_memory["diagnosis_reason"] = sm_diagnosis.reason
        if sm_diagnosis.state.value == "degraded":
            warnings_found.append(f"shared memory degraded: {sm_diagnosis.reason}")

    diag.shared_memory = shared_memory
    if isinstance(shared_memory, dict):
        # Prefer the diagnosis state when available (more specific than discovery state).
        display_state = str(
            shared_memory.get("diagnosis_state", shared_memory.get("state", "unknown"))
        )
        display_detail = shared_memory.get("diagnosis_reason") or str(
            shared_memory.get("endpoint") or shared_memory.get("provider_id") or ""
        )
        sections.append(("Shared memory", display_state, str(display_detail)))

    # 1. Config Check
    config_dir = os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "verdict"
    )
    config_path = os.path.join(config_dir, "verdict.yaml")
    config = None

    if not os.path.exists(config_path):
        issues_found.append("Configuration file (verdict.yaml) is missing.")
    else:
        try:
            with open(config_path) as f:
                config = yaml.safe_load(f) or {}
        except Exception as exc:
            issues_found.append(f"Configuration file is corrupted/invalid YAML: {exc}")

    if config is not None and not isinstance(config, dict):
        issues_found.append("Configuration file verdict.yaml must be a YAML mapping.")
        config = None
    diag.config_loaded = bool(config)

    if config is not None:
        primary_model = config.get("primary_model")
        if not primary_model:
            issues_found.append("No primary model configured in verdict.yaml.")
        else:
            from verdict.classifier import classify

            tier = classify(primary_model)
            sections.append(("Configured Primary Model", "ok", f"{primary_model} (Tier-{tier})"))

        providers = config.get("providers", {})
        if not isinstance(providers, dict):
            issues_found.append("'providers' section in verdict.yaml is malformed.")
        else:
            # Check for secrets inside the config file
            for name, p_cfg in providers.items():
                if not isinstance(p_cfg, dict):
                    continue
                base_url = p_cfg.get("base_url", "")
                if "sk-" in base_url or "api_key" in base_url.lower():
                    issues_found.append(
                        f"Literal API key detected inside the host URL for provider '{name}'."
                    )

            # Check duplicate URLs in config
            urls: dict[str, str] = {}
            for name, p_cfg in providers.items():
                if isinstance(p_cfg, dict) and p_cfg.get("base_url"):
                    url = p_cfg["base_url"].rstrip("/")
                    if url in urls:
                        issues_found.append(
                            f"Duplicate host URL configured in verdict.yaml: provider '{name}' and '{urls[url]}' have identical hosts."
                        )
                    else:
                        urls[url] = name

    # 1b. Config schema version check (T023)
    if config is not None and "schema_version" not in config:
        if fix:
            config["schema_version"] = 1
            try:
                with open(config_path, "w") as f:
                    yaml.safe_dump(config, f, default_flow_style=False)
                fixed_issues.append("Config written by an older Verdict version")
            except Exception as exc:
                issues_found.append(f"Failed to migrate config schema_version: {exc}")
        else:
            issues_found.append(
                "Config written by an older Verdict version. Run 'verdict doctor --fix' to migrate."
            )

    # 1c. Config filename check (T016)
    legacy_config_path = os.path.join(config_dir, "config.yaml")
    if os.path.exists(legacy_config_path):
        if os.path.exists(config_path):
            issues_found.append(
                f"Both {legacy_config_path} and {config_path} exist. "
                "Remove the unused one to avoid confusion."
            )
        else:
            if fix:
                try:
                    os.rename(legacy_config_path, config_path)
                    sections.append(("Renamed", "ok", f"{legacy_config_path} -> {config_path}"))
                    fixed_issues.append("Config file is named 'config.yaml'")
                except Exception as exc:
                    issues_found.append(f"Failed to rename config.yaml: {exc}")
            else:
                issues_found.append(
                    "Config file is named 'config.yaml' but must be 'verdict.yaml'. "
                    f"Run: mv {legacy_config_path} {config_path}"
                )

    # 1d. Gateway reachability check (T015)
    gateway_url = os.getenv("OMNIROUTE_BASE_URL") or (config.get("gateway_url") if config else None)
    if not gateway_url and fix and config is not None:
        # Old-style configs (written before setup persisted gateway_url) have
        # no gateway at all. --fix repairs this only when exactly one healthy
        # local gateway answers the health protocol; an ambiguous or absent
        # gateway is left as an issue for the operator to choose.
        gateway_url = _doctor_fix_gateway(config, config_path, sections, fixed_issues)
    if not gateway_url:
        issues_found.append(
            "No gateway URL configured. Run 'verdict detect' or set OMNIROUTE_BASE_URL."
        )
    else:
        try:
            import urllib.request
            from urllib.error import URLError

            health_req = urllib.request.Request(
                gateway_url.rstrip("/") + "/api/health",
                headers={"Accept": "application/json"},
                method="GET",
            )
            with urllib.request.urlopen(health_req, timeout=2) as resp:  # nosec B310
                if resp.status != 200:
                    raise URLError(f"status {resp.status}")
        except Exception:
            issues_found.append(
                f"Gateway unreachable at {gateway_url}. "
                "Run 'verdict detect' to find a running gateway."
            )

    # 1d-ii. Gateway lifecycle state (report only; nothing is started here).
    _doctor_gateway_lifecycle(diag)

    # 1e. Env var format checks (T017)
    omniroute_base_url_env = os.getenv("OMNIROUTE_BASE_URL")
    if omniroute_base_url_env and not re.match(
        r"^https?://[^/]+(:[0-9]+)?$", omniroute_base_url_env
    ):
        issues_found.append(
            f"OMNIROUTE_BASE_URL has invalid format: '{omniroute_base_url_env}'. "
            "Expected http://host:port (no trailing slash)."
        )

    openai_api_key_env = os.getenv("OPENAI_API_KEY")
    if openai_api_key_env and not openai_api_key_env.startswith("sk-"):
        issues_found.append("OPENAI_API_KEY appears invalid (expected prefix 'sk-').")

    # 1f. Env var reference note (T024)
    sections.append(
        (
            "Environment reference",
            "section",
            "See .env.example in the repository root for the full environment variable reference.",
        )
    )

    # 2. OmniRoute nodes check
    existing_nodes = _omniroute_api_req("GET", "/api/provider-nodes")
    if existing_nodes is None:
        sections.append(
            (
                "OmniRoute nodes",
                "section",
                "OmniRoute server is not currently running/reachable to check nodes.",
            )
        )
    else:
        items = []
        if isinstance(existing_nodes, list):
            items = existing_nodes
        elif isinstance(existing_nodes, dict) and "items" in existing_nodes:
            items = existing_nodes["items"]

        sections.append(
            ("Connected to OmniRoute", "ok", f"Found {len(items)} configured node endpoints")
        )

        # Check duplicate nodes in OmniRoute
        node_urls: dict[str, str] = {}
        duplicates = []
        for node in items:
            if not isinstance(node, dict):
                continue
            bd_url = node.get("baseUrl")
            node_id = node.get("id")
            if bd_url and node_id:
                clean_url = bd_url.rstrip("/")
                if clean_url in node_urls:
                    duplicates.append(
                        (node_id, node.get("name") or node_id, bd_url, node_urls[clean_url])
                    )
                else:
                    node_urls[clean_url] = node_id

        if duplicates:
            sections.append(("Duplicate nodes detected", "section", ""))
            for node_id, name, _url, original_id in duplicates:
                sections.append(
                    (
                        f"Duplicate node {name}",
                        "warn",
                        f"({node_id}) is a duplicate of ({original_id})",
                    )
                )
                issues_found.append(f"Duplicate node '{name}' in OmniRoute configuration.")

            if interactive:
                try:
                    if (
                        Prompt.ask(
                            "\nWould you like to resolve and delete the duplicate provider nodes?",
                            default="y",
                        )
                        .lower()
                        .startswith("y")
                    ):
                        for node_id, name, _url, _ in duplicates:
                            res = _omniroute_api_req("DELETE", f"/api/provider-nodes/{node_id}")
                            if res is not None:
                                sections.append(
                                    ("Removed", "ok", f"Removed duplicate node: {name}")
                                )
                                fixed_issues.append(f"Removed duplicate node {node_id}")
                            else:
                                sections.append(("Removal failed", "failed", f"Node {node_id}"))
                except (KeyboardInterrupt, EOFError):
                    pass

        # Check node reachability
        for node in items:
            if not isinstance(node, dict):
                continue
            url = node.get("baseUrl")
            name = node.get("name") or node.get("id")
            if url:
                import socket
                from urllib.parse import urlparse

                try:
                    parsed = urlparse(url)
                    host = parsed.hostname or "127.0.0.1"
                    port = parsed.port or (443 if parsed.scheme == "https" else 80)
                    with socket.create_connection((host, port), timeout=1.0):
                        pass
                except Exception:
                    issues_found.append(
                        f"Configured provider node '{name}' ({url}) is unreachable/offline."
                    )

    # Credentials check
    sections.append(("Credentials", "header", ""))
    from verdict.credentials_registry import CREDENTIALS
    from verdict.credentials_store import CredentialsStore, get_credential_source

    try:
        store = CredentialsStore()
        missing_required = []
        for cred in CREDENTIALS:
            source, masked = get_credential_source(cred.env_name, store)
            if source == "missing" and not cred.optional:
                missing_required.append(cred)
                issues_found.append(
                    f"Required credential {cred.env_name} is not set. "
                    f"Set with: verdict credentials set {cred.env_name}"
                )
            elif source == "missing":
                sections.append((cred.env_name, "optional", "not set"))
            else:
                sections.append((cred.env_name, source, masked))

        if not missing_required:
            sections.append(("Required credentials", "ok", "all set"))
    except Exception as e:
        issues_found.append(f"Credential check failed: {e}")

    return diag
