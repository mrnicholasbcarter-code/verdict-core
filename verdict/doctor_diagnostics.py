"""Doctor diagnostics — canonical implementation of ``verdict doctor`` checks.

Moved from ``verdict/cli.py`` so that ``verdict/actions`` can import domain
logic without a back-edge into the CLI layer.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from rich.prompt import Prompt

# ---------------------------------------------------------------------------
# _cli_bootstrap is canonical in verdict.actions.helpers; import it here so
# _doctor_gateway_lifecycle and _collect_doctor_diagnostics keep working.
# ---------------------------------------------------------------------------
from verdict.actions.helpers import _cli_bootstrap
from verdict.http_safety import open_no_redirect

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
    """The OmniRoute gateway key via the one resolver (BOD-411 D29).

    ``credentials_store.resolve_api_key`` checks ``VERDICT_OMNIROUTE_API_KEY``/
    ``OMNIROUTE_API_KEY`` (env, then the credential store), the owner-only
    ``omniroute-api-key`` key file, then Prime's ``auth.json`` -- in that
    documented precedence. It never inspects anything else.
    """
    from verdict.credentials_store import resolve_api_key

    return resolve_api_key()


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
        with open_no_redirect(req, timeout=5) as response:
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
        #: The newest ``verdict acceptance run`` result (``acceptance_state.doctor_check``).
        self.acceptance: dict[str, Any] = {}


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


def census_runtime_section(status: Mapping[str, Any]) -> tuple[str, str, str]:
    """One doctor row: census runtime version and its last result."""
    version = status.get("version")
    if not version:
        return (
            "Census runtime",
            "warn",
            "runtime copy not installed (run: verdict census --install)",
        )
    last = status.get("last_result")
    if not last:
        detail = "no census result recorded yet"
    else:
        detail = ", ".join(
            f"{key}={value}"
            for key, value in sorted(last.items())
            if isinstance(value, (str, int, float, bool))
        )
    stale = _census_result_stale(last)
    if stale:
        return ("Census runtime", "warn", f"version {version} ({stale})")
    return ("Census runtime", "ok", f"version {version} ({detail})")


def _census_result_stale(last: Any) -> str | None:
    """Warn when the last census result is older than three timer intervals."""
    from datetime import datetime, timezone

    from verdict.census_passports_service import CENSUS_INTERVAL_SECONDS

    if not isinstance(last, Mapping):
        return None
    finished = last.get("finished_at")
    if not isinstance(finished, str):
        return None
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(finished)).total_seconds()
    except ValueError:
        return None
    if age > 3 * CENSUS_INTERVAL_SECONDS:
        return f"last census {int(age)}s ago is stale; check: systemctl --user status verdict-census.timer"
    return None


def _doctor_acceptance(diag: DoctorDiagnostics) -> None:
    """The newest live acceptance result: red or older than 2 h is an issue."""
    from verdict.acceptance_state import doctor_check

    row, issues, warnings = doctor_check()
    diag.acceptance = row
    age = row.get("age_seconds")
    detail = f"{row.get('state')}: " + row.get("path", "no result yet")
    if age is not None:
        detail += f", {age / 60:.0f} min old"
    state = "failed" if issues else "warning" if warnings else "ok"
    diag.sections.append(("Live acceptance", state, detail))
    diag.issues.extend(issues)
    diag.warnings.extend(warnings)


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

    _doctor_acceptance(diag)
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

    # Background census runtime (BOD-350): the systemd timer probes from a
    # runtime copy; doctor must show which version it runs and its last result.
    sections.append(("Background census", "header", ""))
    try:
        from verdict.census_passports_service import census_status

        sections.append(census_runtime_section(census_status()))
    except Exception as e:
        issues_found.append(f"Census runtime check failed: {e}")

    # Installed-state ownership (BOD-411): doctor fails when the background
    # prober/sync timers are missing, inactive or failed, or when the health
    # cache is too stale or empty to mean anything, instead of only checking
    # docs/config/gateway and leaving the actual background service unverified.
    sections.append(("Installed services", "header", ""))
    for row in (*installed_service_rows(), *service_code_path_rows(), *installed_runtime_rows()):
        sections.append((row["label"], row["status"], row["detail"]))
        if row["status"] == "failed":
            issues_found.append(row["detail"])

    return diag


def installed_service_rows(
    *, now: datetime | None = None, timer_state: Callable[[str], dict[str, Any]] | None = None
) -> list[dict[str, str]]:
    """Doctor rows for each background systemd unit and the health cache.

    A missing or inactive timer, an ``is-failed`` unit, or a health cache
    that is too stale or empty to be useful are all reported with ``status:
    "failed"`` and named by unit/file so the drill
    (``systemctl --user stop verdict-prove-at-rest.timer; verdict doctor``)
    exits non-zero and names the unit. ``now`` and ``timer_state`` are
    injectable for tests; without injection this reads the host's real
    systemd user units (never available on a CI runner).
    """
    from verdict.prove_at_rest_service import TIMER as PROVE_AT_REST_TIMER
    from verdict.prove_at_rest_service import timer_state as _real_timer_state

    injected_timer_state = timer_state
    timer_state = timer_state or _real_timer_state

    rows: list[dict[str, str]] = []
    for unit, label in (
        (PROVE_AT_REST_TIMER, "Prove-at-rest timer"),
        ("verdict-prime-sync.timer", "Prime-sync timer"),
    ):
        state = timer_state(unit)
        if state["state"] in {"missing", "failed", "inactive"}:
            rows.append(
                {
                    "label": label,
                    "status": "failed",
                    "detail": f"{unit} is {state['state']}; run 'verdict prove-at-rest install-service'"
                    if unit == PROVE_AT_REST_TIMER
                    else f"{unit} is {state['state']}",
                }
            )
        else:
            rows.append({"label": label, "status": "ok", "detail": f"{unit} is active"})

    from verdict.metadata import default_store_path, load_store
    from verdict.metadata_service import TIMER as METADATA_TIMER
    from verdict.metadata_service import timer_state as metadata_timer_state

    metadata_state = (
        injected_timer_state(METADATA_TIMER)
        if injected_timer_state is not None
        else metadata_timer_state(METADATA_TIMER)
    )
    if metadata_state.get("state") == "missing" and metadata_state.get("installed") is False:
        rows.append(
            {
                "label": "Metadata refresh timer",
                "status": "warning",
                "detail": f"{METADATA_TIMER} is not installed; explicit opt-in is available "
                "through 'verdict metadata install-service' or 'verdict setup'.",
            }
        )
        rows.append(health_cache_row(now=now))
        return rows
    checked_at = now or datetime.now(timezone.utc)
    metadata_issues: list[str] = []
    if metadata_state.get("state") in {"missing", "failed", "inactive"}:
        metadata_issues.append(f"{METADATA_TIMER} is {metadata_state.get('state')}")
    last_trigger = metadata_state.get("last_trigger")
    if not last_trigger:
        metadata_issues.append("timer has never triggered a refresh")
    else:
        try:
            triggered = _parse_systemd_time(str(last_trigger))
            if checked_at - triggered > timedelta(hours=12):
                metadata_issues.append("last timer trigger is older than 12 hours")
        except (TypeError, ValueError):
            metadata_issues.append("last timer trigger time is unreadable")
    last_completion = metadata_state.get("last_completion")
    if not last_completion:
        metadata_issues.append("service has never completed a refresh")
    else:
        try:
            completed = _parse_systemd_time(str(last_completion))
            completion_age = checked_at - completed
            if completion_age < timedelta(0):
                metadata_issues.append("last service completion time is in the future")
            elif completion_age > timedelta(hours=12):
                metadata_issues.append("last service completion is older than 12 hours")
        except (TypeError, ValueError):
            metadata_issues.append("last service completion time is unreadable")
    service_result = metadata_state.get("result")
    if metadata_state.get("failed") or service_result != "success":
        metadata_issues.append(f"last service result is {service_result or 'unknown'}")
    source_ages: list[str] = []
    try:
        snapshot = load_store(default_store_path())
        if not snapshot.sources:
            metadata_issues.append("metadata snapshot has no source status")
        for source, status in sorted(snapshot.sources.items()):
            if status.status == "skipped":
                source_ages.append(f"{source}=skipped")
                continue
            if status.status == "error":
                metadata_issues.append(f"source {source} failed")
                source_ages.append(f"{source}=error")
                continue
            stamp = status.fetched_at
            if not stamp:
                metadata_issues.append(f"source {source} has no fetch time")
                source_ages.append(f"{source}=unknown age")
                continue
            try:
                if not isinstance(stamp, str):
                    raise ValueError("timestamp is not text")
                fetched = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if fetched.tzinfo is None:
                    raise ValueError("timestamp has no timezone")
                age_seconds = (checked_at - fetched).total_seconds()
                if age_seconds < 0:
                    metadata_issues.append(f"source {source} fetch time is in the future")
                    source_ages.append(f"{source}=future")
                    continue
                age = int(age_seconds)
            except (TypeError, ValueError, OverflowError):
                metadata_issues.append(f"source {source} has an unreadable fetch time")
                source_ages.append(f"{source}=unknown age")
                continue
            source_ages.append(f"{source}={age}s")
            if age > 12 * 60 * 60:
                metadata_issues.append(f"source {source} is older than 12 hours")
    except (OSError, ValueError):
        metadata_issues.append(f"metadata snapshot missing or unreadable at {default_store_path()}")
    rows.append(
        {
            "label": "Metadata refresh timer",
            "status": "failed" if metadata_issues else "ok",
            "detail": "; ".join(metadata_issues)
            if metadata_issues
            else f"{METADATA_TIMER} active; " + ", ".join(source_ages),
        }
    )

    rows.append(health_cache_row(now=now))
    return rows


def _parse_systemd_time(value: str) -> datetime:
    from datetime import datetime, timezone

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = datetime.strptime(value, "%a %Y-%m-%d %H:%M:%S %Z").replace(tzinfo=timezone.utc)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def health_cache_row(*, now: datetime | None = None) -> dict[str, str]:
    """Doctor row for ``health-cache.json`` staleness and emptiness.

    Failed when the cache is missing, when its newest entry is older than
    2x the probe interval, or when it has no fresh and no stale route
    (``fresh + stale == 0``): either state means the background prober is
    not keeping eligibility current, whatever the timers report.

    ``now`` is injectable so tests can exercise staleness with a fake clock
    instead of racing the real one.
    """
    from datetime import datetime, timezone

    from verdict.orchestration.health_cache import (
        STATE_FRESH,
        STATE_STALE,
        HealthCache,
        default_cache_path,
    )

    path = default_cache_path()
    if not path.exists():
        return {
            "label": "Health cache",
            "status": "failed",
            "detail": f"{path} does not exist; the prober has not run yet",
        }
    try:
        cache = HealthCache(path)
    except Exception as exc:
        return {"label": "Health cache", "status": "failed", "detail": f"unreadable: {exc}"}
    now = now if now is not None else datetime.now(timezone.utc)
    routes = cache.routes()
    fresh = sum(1 for entry in routes.values() if entry.state_at(now) == STATE_FRESH)
    stale = sum(1 for entry in routes.values() if entry.state_at(now) == STATE_STALE)
    if fresh + stale == 0:
        return {
            "label": "Health cache",
            "status": "failed",
            "detail": f"{path} has no fresh or stale route; the prober is not producing usable evidence",
        }
    newest_checked = max((entry.checked_at for entry in routes.values()), default=None)
    if newest_checked is not None:
        age_seconds = (now - newest_checked).total_seconds()
        probe_interval_seconds = 300.0
        if age_seconds > 2 * probe_interval_seconds:
            return {
                "label": "Health cache",
                "status": "failed",
                "detail": f"{path} newest entry is {int(age_seconds)}s old (> 2x the {int(probe_interval_seconds)}s probe interval)",
            }
    return {
        "label": "Health cache",
        "status": "ok",
        "detail": f"{fresh} fresh, {stale} stale route(s)",
    }


# ---------------------------------------------------------------------------
# Service code paths (BOD-421): a tracked unit must import the verdict
# package its ExecStart interpreter is configured for, not one shadowed by
# its WorkingDirectory. ``python -m verdict`` puts the unit cwd first on
# sys.path, so a stale checkout as WorkingDirectory silently runs old code.
# ---------------------------------------------------------------------------

# verdict-prove-at-rest.service is deliberately NOT checked: that unit is
# currently live-misconfigured on the operator host (WorkingDirectory points
# at a stale checkout), and retargeting it is deferred until the FREE-only
# spend filter exists; it will be added to this tuple after the unit is
# reinstalled from the main repo.
_SERVICE_CODE_PATH_UNITS = (("verdict-prime-sync.service", "Prime-sync code path"),)

_EXECSTART_PATH_RE = re.compile(r"path=([^;]+)")


def _run_systemctl_show_props(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=10)


def _systemctl_show_props(
    unit: str, run: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None
) -> dict[str, str]:
    """``LoadState``/``WorkingDirectory``/``ExecStart``/``Environment`` for one unit. Never raises."""
    run = run or _run_systemctl_show_props
    try:
        result = run(
            [
                "systemctl",
                "--user",
                "show",
                "-p",
                "LoadState,WorkingDirectory,ExecStart,Environment",
                unit,
            ]
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        if key:
            values[key] = value.strip()
    return values


def _parse_execstart_interpreter(value: str) -> str | None:
    """The ExecStart binary path from ``systemctl show`` output.

    Modern systemd prints ``{ path=... ; argv[]=[...] ; ... }``; older
    versions print the plain command line. Both are handled so the check
    does not depend on the systemd version.
    """
    text = value.strip()
    if not text:
        return None
    if text.startswith("{"):
        match = _EXECSTART_PATH_RE.search(text)
        return match.group(1).strip() if match else None
    return text.split()[0]


def _resolve_verdict_package(
    interpreter: str,
    cwd: str,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str | None:
    """Directory of the ``verdict`` package ``interpreter`` imports with ``cwd``.

    Read-only: it is the same import the service performs at startup.
    ``cwd`` is first on ``sys.path`` for ``-c`` exactly as for ``-m``, so a
    ``verdict/`` package under ``cwd`` shadows the interpreter's own install.
    """
    try:
        result = run(
            [interpreter, "-c", "import verdict; print(verdict.__file__)"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=cwd,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    location = result.stdout.strip() if result.returncode == 0 else ""
    return str(Path(location).resolve().parent) if location else None


def service_code_path_rows(
    *,
    unit_props: Callable[[str], dict[str, str]] | None = None,
    resolve: Callable[[str, str], str | None] = _resolve_verdict_package,
) -> list[dict[str, str]]:
    """Doctor rows: each tracked unit must import its configured ``verdict`` package.

    The package is resolved twice with the unit's ExecStart interpreter:
    once from a neutral cwd (the checkout the interpreter's venv is
    configured to import -- the repo doctor tracks for the unit) and once
    from the unit's WorkingDirectory (what the service actually imports).
    A difference means the cwd shadows the configured checkout and the unit
    runs stale code, reported as ``status: "failed"`` with the unit name,
    the configured WorkingDirectory and the resolved package path.
    ``unit_props`` and ``resolve`` are injectable for tests; without
    injection this reads the host's real systemd user units (never
    available on a CI runner, where every unit is skipped as not loaded).
    """
    unit_props = unit_props or _systemctl_show_props
    rows: list[dict[str, str]] = []
    for unit, label in _SERVICE_CODE_PATH_UNITS:
        props = unit_props(unit)
        if props.get("LoadState", "not-found") != "loaded":
            continue
        working_directory = props.get("WorkingDirectory", "")
        interpreter = _parse_execstart_interpreter(props.get("ExecStart", ""))
        if not working_directory or not interpreter:
            rows.append(
                {
                    "label": label,
                    "status": "failed",
                    "detail": f"{unit}: missing WorkingDirectory or ExecStart interpreter",
                }
            )
            continue
        with tempfile.TemporaryDirectory() as neutral_cwd:
            configured = resolve(interpreter, neutral_cwd)
        imported = resolve(interpreter, working_directory)
        if configured is None or imported is None:
            rows.append(
                {
                    "label": label,
                    "status": "failed",
                    "detail": (
                        f"{unit}: cwd {working_directory}; could not resolve the verdict package"
                        f" the interpreter imports (configured={configured}, imported={imported})"
                    ),
                }
            )
        elif imported != configured:
            rows.append(
                {
                    "label": label,
                    "status": "failed",
                    "detail": (
                        f"{unit}: cwd {working_directory} imports package {imported},"
                        f" not the configured checkout {configured}"
                    ),
                }
            )
        else:
            rows.append(
                {
                    "label": label,
                    "status": "ok",
                    "detail": f"{unit}: cwd {working_directory}, package {imported}",
                }
            )
    return rows


# ---------------------------------------------------------------------------
# Installed runtime provenance (BOD-414 / S11, acceptance A29): every
# installed Verdict unit must import the reviewed build doctor itself runs
# from, with the same state scope. A unit on a source checkout, a stranded
# worktree or the census runtime copy is a different source copy and its
# results cannot count as proof of the reviewed build.
# ---------------------------------------------------------------------------

_RUNTIME_UNITS = (
    "verdict-prove-at-rest.service",
    "verdict-census.service",
    "verdict-acceptance.service",
    "verdict-prime-sync.service",
    "verdict-metadata-refresh.service",
)
# Units that run their own route-qualification probe loop.
_QUALIFIER_UNITS = ("verdict-prove-at-rest.service", "verdict-census.service")


def _same_build(package: Path, reviewed: Path) -> bool:
    """A copy is the reviewed build only when its recorded build and content match."""
    from verdict.census_passports_service import census_runtime_version, package_build

    if package == reviewed:
        return True
    if package_build(package) != package_build(reviewed) or package_build(package) == "unrecorded":
        return False
    return census_runtime_version(package.parent) == census_runtime_version(reviewed.parent)


def _unit_environment(value: str) -> dict[str, str]:
    try:
        words = shlex.split(value)
    except ValueError:
        return {}
    return dict(word.split("=", 1) for word in words if "=" in word)


def installed_runtime_rows(
    *,
    unit_props: Callable[[str], dict[str, str]] | None = None,
    resolve: Callable[[str, str], str | None] = _resolve_verdict_package,
    reviewed_package: Path | None = None,
    home: Path | None = None,
    state_home: Path | None = None,
) -> list[dict[str, str]]:
    """Doctor rows: build, imported package, gateway and state scope per installed unit.

    A unit fails when its ExecStart interpreter's own install or the package
    it imports from its WorkingDirectory is not the reviewed build (default:
    the package this doctor runs from), or when its state scope differs from
    ``state_home``. Qualification units share one coordinator lock and cycle
    ledger only on the reviewed build with the same state scope; otherwise
    they are independent loops and the owner row is a warning. Units that are
    not loaded are skipped, so a CI runner gets no rows.
    """
    from verdict.census_passports_service import package_build
    from verdict.paths import verdict_home

    unit_props = unit_props or _systemctl_show_props
    reviewed = (reviewed_package or Path(__file__).resolve().parent).resolve()
    user_home = home or Path.home()
    scope = state_home or verdict_home()
    reviewed_build = package_build(reviewed)
    rows: list[dict[str, str]] = []
    qualifiers: list[str] = []
    independent: list[str] = []
    for unit in _RUNTIME_UNITS:
        props = unit_props(unit)
        if props.get("LoadState", "not-found") != "loaded":
            continue
        label = f"Runtime {unit.removeprefix('verdict-').removesuffix('.service')}"
        cwd = props.get("WorkingDirectory", "")
        interpreter = _parse_execstart_interpreter(props.get("ExecStart", ""))
        imported = configured = None
        if cwd and interpreter:
            imported = resolve(interpreter, cwd)
            with tempfile.TemporaryDirectory() as neutral_cwd:
                configured = resolve(interpreter, neutral_cwd)
        if imported is None or configured is None:
            rows.append(
                {
                    "label": label,
                    "unit": unit,
                    "status": "failed",
                    "detail": f"{unit}: cannot resolve the verdict package"
                    f" (interpreter {interpreter}, cwd {cwd or 'unset'})",
                }
            )
            if unit in _QUALIFIER_UNITS:
                qualifiers.append(unit)
                independent.append(unit)
            continue
        environment = _unit_environment(props.get("Environment", ""))
        unit_scope = verdict_home({"HOME": str(user_home), **environment})
        gateway = environment.get("OMNIROUTE_BASE_URL") or "default"
        package = Path(imported).resolve()
        build = package_build(package)
        problems = []
        if Path(configured).resolve() != reviewed:
            problems.append(f"interpreter {interpreter} installs {configured}")
        if not _same_build(package, reviewed):
            difference = (
                "content differs from"
                if build == reviewed_build and build != "unrecorded"
                else "not"
            )
            problems.append(
                f"imports {package} ({build}), {difference} the reviewed build {reviewed}"
            )
        if unit_scope != scope:
            problems.append(f"state scope {unit_scope}, not {scope}")
        unit_cache = environment.get("VERDICT_HEALTH_CACHE")
        if (
            unit_cache
            and Path(unit_cache).expanduser().resolve() != (scope / "health-cache.json").resolve()
        ):
            problems.append(f"health cache {unit_cache}, not {scope / 'health-cache.json'}")
        if unit in _QUALIFIER_UNITS:
            qualifiers.append(unit)
            if problems:
                independent.append(unit)
        facts = f"package {package}, build {build}, gateway {gateway}, state {unit_scope}"
        rows.append(
            {
                "label": label,
                "unit": unit,
                "status": "failed" if problems else "ok",
                "detail": f"{unit}: "
                + (
                    "; ".join(problems) + f" (reviewed build {reviewed_build}; {facts})"
                    if problems
                    else facts
                ),
            }
        )
    if independent and len(qualifiers) > 1:
        rows.append(
            {
                "label": "Qualification owner",
                "status": "warning",
                "detail": "independent qualification loops "
                + ", ".join(qualifiers)
                + f"; off the shared coordinator: {', '.join(independent)}",
            }
        )
    elif qualifiers:
        rows.append(
            {
                "label": "Qualification owner",
                "status": "ok",
                "detail": f"reviewed build and state scope {scope}: " + ", ".join(qualifiers),
            }
        )
    return rows
