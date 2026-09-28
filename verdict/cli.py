"""CLI entry point for Verdict."""

import argparse
import contextlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from rich.console import Console
from rich.prompt import Prompt

import verdict.actions.helpers as _action_helpers

# BOD-275: canonical implementations extracted from cli.py  ──────────────────
from verdict.actions.helpers import _CLI_DEFAULT_PROVIDERS as _HELPERS_CLI_DEFAULT_PROVIDERS
from verdict.benchmarking import format_benchmark_report, run_reproducible_benchmarks
from verdict.contracts import DEFAULT_PRIMARY_MODEL
from verdict.free_tier_admit import execute_offload_chat, omniroute_endpoint_from_env
from verdict.gate import Gate
from verdict.harness_claude import DEFAULT_BASE_URL as CLAUDE_HARNESS_DEFAULT_BASE_URL
from verdict.harness_claude import DEFAULT_TOKEN_ENV as CLAUDE_HARNESS_DEFAULT_TOKEN_ENV
from verdict.harness_cline import DEFAULT_BASE_URL as CLINE_HARNESS_DEFAULT_BASE_URL
from verdict.harness_cline import DEFAULT_TOKEN_ENV as CLINE_HARNESS_DEFAULT_TOKEN_ENV
from verdict.harness_codex import DEFAULT_BASE_URL as CODEX_HARNESS_DEFAULT_BASE_URL
from verdict.harness_codex import DEFAULT_TOKEN_ENV as CODEX_HARNESS_DEFAULT_TOKEN_ENV
from verdict.harness_cursor import DEFAULT_BASE_URL as CURSOR_HARNESS_DEFAULT_BASE_URL
from verdict.harness_cursor import DEFAULT_TOKEN_ENV as CURSOR_HARNESS_DEFAULT_TOKEN_ENV
from verdict.harness_hermes import DEFAULT_BASE_URL as HERMES_HARNESS_DEFAULT_BASE_URL
from verdict.harness_hermes import DEFAULT_MODEL as HERMES_HARNESS_DEFAULT_MODEL
from verdict.harness_hermes import DEFAULT_TOKEN_ENV as HERMES_HARNESS_DEFAULT_TOKEN_ENV
from verdict.harness_opencode import DEFAULT_BASE_URL as OPENCODE_HARNESS_DEFAULT_BASE_URL
from verdict.harness_opencode import DEFAULT_TOKEN_ENV as OPENCODE_HARNESS_DEFAULT_TOKEN_ENV
from verdict.harness_prime import DEFAULT_BASE_URL as PRIME_HARNESS_DEFAULT_BASE_URL
from verdict.harness_prime import DEFAULT_TOKEN_ENV as PRIME_HARNESS_DEFAULT_TOKEN_ENV
from verdict.models import ModelInfo, ProviderConfig
from verdict.patch_executor import DEFAULT_BASE_URL
from verdict.terminal_ui import TerminalUI

console = Console()


def _print_detection_banner() -> None:
    """Print the detection banner."""
    ui = TerminalUI(console)
    ui.panel(
        "Verdict Provider Detection",
        "Scanning for local servers, CLIs, API keys, and routers...",
        tone="INFO",
    )


import verdict.doctor_diagnostics as _doctor_diag  # noqa: E402


def select_from_list(prompt_text: str, options: list[str], default: str | None = None) -> str:
    """Render a consistent, accessible numbered selector."""
    return TerminalUI(console).select(prompt_text, options, default=default)


PROVIDER_MAPPING = {
    "ollama": ("ollama", "http://localhost:11434/v1", "ollama-local"),
    "lmstudio": ("lmstudio", "http://localhost:1234/v1", "lmstudio-local"),
    "vllm": ("vllm", "http://localhost:8000/v1", "vllm-local"),
    "llamacpp": ("openai", "http://localhost:8080/v1", "llamacpp-local"),
    "koboldcpp": ("openai", "http://localhost:5001/v1", "koboldcpp-local"),
    "openai": ("openai", "https://api.openai.com/v1", "openai-cloud"),
    "anthropic": ("anthropic", "https://api.anthropic.com", "anthropic-cloud"),
    "groq": ("groq", "https://api.groq.com/openai/v1", "groq-cloud"),
    "xai": ("xai", "https://api.x.ai/v1", "xai-cloud"),
    "google": ("google", "https://generativelanguage.googleapis.com", "gemini-cloud"),
    "openrouter": ("openrouter", "https://openrouter.ai/api/v1", "openrouter-cloud"),
}


def cmd_setup_credentials(*, non_interactive: bool = False) -> None:
    """Setup step for credentials and dependencies.

    Prompts for missing required credentials (unless --non-interactive).
    Shows registered credentials and dependencies status.
    """
    import getpass

    from verdict.credentials_registry import CREDENTIALS, DEPENDENCIES
    from verdict.credentials_store import CredentialsStore, get_credential_source
    from verdict.terminal_ui import TerminalUI

    ui = TerminalUI()
    store = CredentialsStore()

    ui.header("Credentials and Dependencies")

    # Check credentials
    missing_required = []
    for cred in CREDENTIALS:
        source, _masked = get_credential_source(cred.env_name, store)
        if source == "missing" and not cred.optional:
            missing_required.append(cred)

    if missing_required:
        ui.panel(
            "Missing required credentials",
            f"{len(missing_required)} required credential(s) are not set.",
            tone="WARNING",
        )

        if non_interactive:
            for cred in missing_required:
                ui.status(cred.env_name, "missing", cred.purpose)
            ui.panel(
                "Action required",
                "Set missing credentials with: verdict credentials set <NAME>",
                tone="INFO",
            )
            return

        # Interactive prompting
        for cred in missing_required:
            ui.status(cred.env_name, "missing", cred.purpose)
            try:
                prompt_text = f"Enter value for {cred.env_name} (or leave empty to skip): "
                value = getpass.getpass(prompt_text)
                if value:
                    store.set(cred.env_name, value)
                    ui.status(cred.env_name, "set", "stored securely")
                else:
                    ui.status(cred.env_name, "skipped", "")
            except (KeyboardInterrupt, EOFError):
                ui.status(cred.env_name, "skipped", "interrupted")
                continue
    else:
        ui.panel("Credentials", "All required credentials are set.", tone="INFO")

    # Show dependency status
    ui.header("Dependencies")
    missing_deps = []
    for dep in DEPENDENCIES:
        present, version = dep.check()
        if present:
            ver_str = f"v{version}" if version else "present"
            ui.status(dep.name, "installed", ver_str)
        else:
            ui.status(dep.name, "missing", dep.install_command)
            missing_deps.append(dep)

    if missing_deps:
        ui.panel(
            "Optional dependencies",
            f"{len(missing_deps)} optional dependencies are not installed.",
            tone="INFO",
        )


def cmd_setup(
    *,
    dry_run: bool = False,
    output_json: bool = False,
    non_interactive: bool = False,
    recommended: bool = False,
    plan_only: bool = False,
    scope: str = "all",
    allowlist: list[str] | None = None,
    consent: bool = False,
    apply: bool = False,
    rollback: bool = False,
    rollback_actions: list[str] | None = None,
    state_dir: str | None = None,
) -> None:
    """Interactive setup wizard, mutation-free plan, or capability bootstrap APPLY."""
    from pathlib import Path

    from verdict.capability_bootstrap import (
        BootstrapMode,
        BootstrapScope,
        rollback_bootstrap_actions,
    )
    from verdict.setup_presentation import present_bootstrap

    if rollback:
        resolved = Path(state_dir) if state_dir else Path.home() / ".verdict" / "bootstrap"
        stage = rollback_bootstrap_actions(
            state_dir=resolved, action_ids=tuple(rollback_actions) if rollback_actions else None
        )
        rollback_payload: dict[str, object] = {
            "schema_version": "capability-bootstrap/v1",
            "kind": "bootstrap_rollback",
            "stage": stage.to_dict(),
        }
        if output_json:
            print(json.dumps(rollback_payload, indent=2, sort_keys=True))
        else:
            print(f"Bootstrap rollback: {stage.status} — {stage.summary}")
            details = stage.details if isinstance(stage.details, dict) else {}
            for action_id in details.get("rolled_back", []) or []:
                print(f"  rolled_back: {action_id}")
            for item in details.get("manual_undo_required", []) or []:
                if isinstance(item, dict):
                    print(f"  manual_undo_required: {item.get('action_id')} — {item.get('undo')}")
            for item in details.get("failed", []) or []:
                if isinstance(item, dict):
                    print(f"  failed: {item.get('action_id')} — {item.get('status')}")
            if stage.status == "failed":
                raise SystemExit(1)
        return

    # Preserve the classic mutation-free setup_plan contract for dry-run / plan
    # unless the caller explicitly requested bootstrap enrichment or APPLY.
    wants_classic_plan = (dry_run or plan_only or (output_json and not apply)) and not (
        recommended or apply or scope != "all"
    )
    if wants_classic_plan:
        cmd_setup_plan(output_json=output_json)
        return

    wants_bootstrap_plan = (
        recommended or plan_only or dry_run or non_interactive or scope != "all"
    ) and not apply
    if wants_bootstrap_plan:
        # Scoped subcommands and recommended plans keep the setup_plan envelope
        # with nested bootstrap when possible; bare bootstrap report for APPLY prep.
        if scope != "all" or recommended:
            cmd_setup_plan(
                output_json=output_json, scope=scope, recommended=recommended or scope != "all"
            )
            return
        mode = BootstrapMode.RECOMMENDED if recommended else BootstrapMode.PLAN
        report = present_bootstrap(
            console=console,
            output_json=output_json,
            mode=mode,
            scope=BootstrapScope(scope),
            non_interactive=True,
            allowlist=tuple(allowlist or ()),
        )
        report_payload: dict[str, object] = report.to_dict()
        if output_json:
            print(json.dumps(report_payload, indent=2, sort_keys=True))
            return
        return

    if apply:
        report = present_bootstrap(
            console=console,
            output_json=output_json,
            mode=BootstrapMode.APPLY,
            scope=BootstrapScope(scope),
            non_interactive=non_interactive,
            allowlist=tuple(allowlist or ()),
            consent=consent,
            state_dir=Path(state_dir) if state_dir else None,
        )
        apply_payload: dict[str, object] = report.to_dict()
        stages = apply_payload["stages"]
        if not isinstance(stages, list):
            raise TypeError("bootstrap report must include stages list")
        if output_json:
            print(json.dumps(apply_payload, indent=2, sort_keys=True))
        consent_blocked = any(
            isinstance(stage, dict)
            and stage.get("stage") == "consent"
            and stage.get("status") == "blocked"
            for stage in stages
        )
        if consent_blocked:
            raise SystemExit(2)
        return

    ui = TerminalUI(console)

    # If an existing config file is present but not valid YAML, warn before
    # prompting the user for anything and let them opt out of overwriting it.
    existing_config_dir = os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "verdict"
    )
    existing_config_path = os.path.join(existing_config_dir, "verdict.yaml")
    if os.path.exists(existing_config_path):
        try:
            with open(existing_config_path) as f:
                existing = yaml.safe_load(f)
            if isinstance(existing, dict):
                ui.header("Existing installation")
                ui.status("Verdict configuration", "found", existing_config_path)
                ui.panel(
                    "Preserved",
                    "Existing configuration preserved. Run verdict setup --recommended to review capabilities, or verdict doctor to inspect health.\n"
                    "Run `verdict setup credentials` to add or update API keys.",
                )
                if not non_interactive:
                    try:
                        run_now = Prompt.ask(
                            "Run credentials setup now?", choices=["y", "n"], default="n"
                        )
                    except (KeyboardInterrupt, EOFError):
                        run_now = "n"
                    if run_now == "y":
                        cmd_setup_credentials(non_interactive=False)
                return
        except yaml.YAMLError as e:
            ui.panel(
                "Invalid YAML",
                f"Existing config at {existing_config_path} is not valid YAML: {e}",
                tone="WARNING",
            )
            try:
                overwrite = Prompt.ask("Overwrite it?", default="Y")
            except (KeyboardInterrupt, EOFError):
                overwrite = "n"
            if not overwrite.lower().startswith("y"):
                ui.status("Setup", "skipped", "Cancelled.")
                sys.exit(1)

    # First, run auto-detection to show user what's available
    ui.header("Setup")
    detected_result = None
    try:
        from verdict.provider_detection import detect_all_providers

        with ui.task("Discovering providers and gateway models"):
            detected_result = detect_all_providers()
        ui.section("Discovered providers")
        for provider in detected_result.all_providers():
            state = (
                "reachable"
                if provider.server_running
                else "configured"
                if provider.api_key_configured
                else "found"
                if provider.cli_available
                else "missing"
            )
            ui.status(provider.name, state, provider.base_url or "")
    except Exception as e:
        ui.status("Detection", "warn", str(e))

    ui.section("Configure routing")

    config: dict[str, Any] = {}
    use_auto = False

    # Auto-detect a local gateway (OmniRoute/9router) and wire it into both
    # the saved config and the current process environment so downstream
    # calls (e.g. syncing provider nodes below) can reach it immediately.
    try:
        from verdict.provider_detection import probe_gateways

        with ui.task("Discovering gateways"):
            gateways = probe_gateways()
        healthy_gateways = [g for g in gateways if g.health_ok]
        if healthy_gateways:
            selected_gateway = healthy_gateways[0]
            config["gateway_url"] = selected_gateway.url
            # Persist discovery in Verdict config only. Setup must not mutate
            # the hosting process environment; doing so leaks routing authority
            # into later in-process callers and test/application lifecycles.
            ui.status(
                "Gateway detected",
                "ok",
                f"{selected_gateway.display_name} at {selected_gateway.url}",
            )
            if len(healthy_gateways) > 1:
                ui.section("Multiple gateways found")
                ui.console.print(
                    "[dim]Set OMNIROUTE_BASE_URL to one of the above to select a different one.[/dim]"
                )
    except Exception as e:
        ui.status("Gateway detection", "warn", str(e))

    running_providers = []
    if detected_result:
        # Get all providers that are running or have configured keys
        running_providers = [
            p
            for p in detected_result.all_providers()
            if p.server_running
            or (p.type in ("cli_provider", "cloud_api") and p.api_key_configured)
        ]

    # Pre-select based on detection if running in automated test/input context where "done" or empty is passed
    if running_providers:
        ui.section("Auto-detection found active providers")
        try:
            should_auto = Prompt.ask(
                "Would you like to auto-configure Verdict using a detected provider?", default="y"
            )
            if should_auto.lower().startswith("y"):
                use_auto = True

                # Select provider
                provider_names = [
                    f"{p.name} ({p.id}) - {p.base_url or 'API Key Configuration'}"
                    for p in running_providers
                ]
                selected_option = select_from_list(
                    "Select a provider to configure", provider_names, default="1"
                )

                # Find the corresponding provider object
                selected_provider = None
                for p in running_providers:
                    if f"{p.name} ({p.id})" in selected_option:
                        selected_provider = p
                        break

                if selected_provider:
                    config["providers"] = {
                        selected_provider.id: {
                            "base_url": selected_provider.base_url,
                            "api_key_env": selected_provider.api_key_env,
                        }
                    }

                    # Retrieve models
                    models = selected_provider.models
                    if models:
                        ui.section(f"Detected models for {selected_provider.name}")
                        # Add an option for custom
                        model_options = [*list(models), "Enter a custom model ID"]
                        selected_model = select_from_list(
                            "Select the primary model (Tier-0)", model_options, default="1"
                        )
                        if selected_model == "Enter a custom model ID":
                            config["primary_model"] = Prompt.ask(
                                "Enter custom primary model ID", default=DEFAULT_PRIMARY_MODEL
                            )
                        else:
                            config["primary_model"] = selected_model
                    else:
                        config["primary_model"] = Prompt.ask(
                            "No models returned from server. Enter primary model ID (Tier-0)",
                            default=DEFAULT_PRIMARY_MODEL,
                        )
                else:
                    use_auto = False
        except (KeyboardInterrupt, EOFError):
            use_auto = False

    # Automatically add/sync detected providers to OmniRoute/9Router
    if running_providers:
        to_sync = []
        try:
            # Check existing nodes in OmniRoute
            existing_nodes = _doctor_diag._omniroute_api_request("GET", "/api/provider-nodes")
            existing_urls = set()
            if existing_nodes:
                items = []
                if isinstance(existing_nodes, list):
                    items = existing_nodes
                elif isinstance(existing_nodes, dict) and "items" in existing_nodes:
                    items = existing_nodes["items"]
                for node in items:
                    if isinstance(node, dict) and "baseUrl" in node and node["baseUrl"]:
                        existing_urls.add(node["baseUrl"].rstrip("/"))

                for p in running_providers:
                    if p.id in PROVIDER_MAPPING:
                        prov_name, base_url, node_name = PROVIDER_MAPPING[p.id]
                        url_to_check = p.base_url or base_url
                        if url_to_check.rstrip("/") not in existing_urls:
                            to_sync.append((p.name, prov_name, url_to_check, node_name))

            if to_sync:
                ui.section("Syncing detected system providers to OmniRoute/9Router")
                for name, _p_name, url, _ in to_sync:
                    ui.status("Found active", "ok", f"{name}: {url}")

                if (
                    Prompt.ask(
                        "Sync these active providers to OmniRoute as node endpoints?", default="y"
                    )
                    .lower()
                    .startswith("y")
                ):
                    for _name, p_name, url, node_name in to_sync:
                        payload = {
                            "provider": p_name,
                            "baseUrl": url,
                            "name": node_name,
                            "weight": 100,
                            "enabled": True,
                        }
                        res = _doctor_diag._omniroute_api_request(
                            "POST", "/api/provider-nodes", payload
                        )
                        if res:
                            ui.status("Node registered", "ok", node_name)
                        else:
                            ui.status("Node registration failed", "failed", node_name)
        except (KeyboardInterrupt, EOFError):
            pass

    # Prompt user about adding free providers like gemini/antigravity for local fallback routing
    try:
        ui.section("Fallback Models Configuration")
        if (
            Prompt.ask(
                "Setup free fallback endpoints (Gemini Free, OpenRouter Free) for local offloads?",
                default="n",
            )
            .lower()
            .startswith("y")
        ):
            gemini_key = os.getenv("GEMINI_API_KEY")
            if not gemini_key:
                ui.panel(
                    "GEMINI_API_KEY missing",
                    'Get a free Gemini API key at: https://aistudio.google.com/\nThen set it: export GEMINI_API_KEY="your_key"',
                    tone="WARNING",
                )

            or_key = os.getenv("OPENROUTER_API_KEY")
            if not or_key:
                ui.panel(
                    "OPENROUTER_API_KEY missing",
                    'Get an OpenRouter key at: https://openrouter.ai/keys\nThen set it: export OPENROUTER_API_KEY="your_key"',
                    tone="WARNING",
                )

            fallback_options = [
                "Google Gemini Free Tier (https://generativelanguage.googleapis.com)",
                "OpenRouter Free Models (https://openrouter.ai/api/v1)",
            ]

            ui.section("Available free fallback endpoints")
            selected_fallbacks = []
            for i, opt in enumerate(fallback_options, 1):
                ui.status(f"{i}", "info", opt)

            choices = Prompt.ask(
                "Enter endpoints to add (e.g. '1, 2' or 'all', or 'done')", default="all"
            )
            if choices.strip().lower() == "all":
                selected_fallbacks = [1, 2]
            elif choices.strip().lower() != "done":
                with contextlib.suppress(ValueError):
                    selected_fallbacks = [int(x.strip()) for x in choices.split(",") if x.strip()]

            for idx in selected_fallbacks:
                if idx == 1:
                    payload = {
                        "provider": "google",
                        "baseUrl": "https://generativelanguage.googleapis.com",
                        "name": "gemini-free",
                        "weight": 80,
                        "enabled": True,
                    }
                    res = _doctor_diag._omniroute_api_request(
                        "POST", "/api/provider-nodes", payload
                    )
                    if res:
                        ui.status("Gemini Free fallback", "ok", "Registered")
                    else:
                        ui.status("Gemini Free fallback", "failed", "OmniRoute not running")
                elif idx == 2:
                    payload = {
                        "provider": "openrouter",
                        "baseUrl": "https://openrouter.ai/api/v1",
                        "name": "openrouter-free",
                        "weight": 80,
                        "enabled": True,
                    }
                    res = _doctor_diag._omniroute_api_request(
                        "POST", "/api/provider-nodes", payload
                    )
                    if res:
                        ui.status("OpenRouter Free fallback", "ok", "Registered")
                    else:
                        ui.status("OpenRouter Free fallback", "failed", "Registration failed")
    except (KeyboardInterrupt, EOFError):
        pass

    if not use_auto:
        if not running_providers:
            ui.panel(
                "No active providers or routers",
                "To run OmniRoute (centralized router recommended for Verdict):\n  npm install -g omniroute\n  omniroute serve",
                tone="WARNING",
            )

            try:
                should_manual = Prompt.ask(
                    "Would you like to manually configure Verdict right now anyway?", default="y"
                )
                if not should_manual.lower().startswith("y"):
                    ui.status(
                        "Setup", "cancelled", "Please start your provider/router and try again."
                    )
                    return
            except (KeyboardInterrupt, EOFError):
                ui.status("Setup input", "interrupted", "")
                return

        ui.section("Manual configuration")
        try:
            config["primary_model"] = Prompt.ask(
                "[bold]Primary model[/bold] (Tier-0, never offloaded)",
                default=DEFAULT_PRIMARY_MODEL,
            )

            config["providers"] = {}
            while True:
                provider_name = Prompt.ask(
                    "\n[bold]Add a provider[/bold] (name, or 'done' to finish)", default="done"
                )
                if provider_name.lower() in ("done", ""):
                    break
                base_url = Prompt.ask(f"  Base URL for {provider_name}")
                api_key_env = Prompt.ask(f"  API key env var for {provider_name}", default="")
                config["providers"][provider_name] = {
                    "base_url": base_url,
                    "api_key_env": api_key_env or None,
                }
        except (KeyboardInterrupt, EOFError):
            ui.status("Manual configuration", "interrupted", "")
            return

    # Review the exact file write before mutating an interactive installation.
    ui.plan(
        {
            "actions": [
                {
                    "kind": "configure",
                    "description": "Save Verdict routing configuration",
                    "reason": f"Primary model: {config.get('primary_model', '')}",
                }
            ]
        }
    )
    if ui.interactive and not ui.confirm("Save this configuration?", default=True):
        ui.status("Configuration", "skipped", "No configuration file written.")
        return

    # Save configuration
    config_dir = os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "verdict"
    )
    os.makedirs(config_dir, exist_ok=True)
    config_path = os.path.join(config_dir, "verdict.yaml")

    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False)

    ui.status("Configuration", "saved", f"Written to {config_path}")
    ui.section("Configuration contents")
    print(yaml.dump(config, default_flow_style=False))


def cmd_setup_plan(
    *, output_json: bool = False, scope: str = "all", recommended: bool = False
) -> None:
    """Print the mutation-free setup plan; optionally enrich with bootstrap."""
    from verdict.setup_plan import build_setup_plan

    if not recommended and scope == "all":
        from verdict.actions.registry import run_action

        result = run_action("setup.plan")
        if output_json:
            print(json.dumps(result.data, indent=2, sort_keys=True))
            return
        ui = TerminalUI(console)
        ui.header("Setup plan")
        ui.plan(result.data)
        ui.panel("Review complete", "No changes made. This plan does not probe services.")
        return

    from verdict.capability_bootstrap import BootstrapMode, BootstrapScope
    from verdict.setup_presentation import present_bootstrap

    bootstrap = present_bootstrap(
        console=console,
        output_json=output_json,
        mode=BootstrapMode.RECOMMENDED if recommended else BootstrapMode.PLAN,
        scope=BootstrapScope(scope),
        non_interactive=True,
    )
    from verdict.shared_memory import discover_shared_memory_setup

    base = build_setup_plan(
        bootstrap_providers=bootstrap.providers, include_bootstrap=True, bootstrap_scope=scope
    ).to_dict()
    base["shared_memory"] = discover_shared_memory_setup()
    if output_json:
        print(json.dumps({**base, "bootstrap": bootstrap.to_dict()}, indent=2, sort_keys=True))


def _omniroute_provider_from_env() -> dict[str, ProviderConfig]:
    """Surface OMNIROUTE_BASE_URL as a route provider without probing ports."""
    base = os.getenv("OMNIROUTE_BASE_URL")
    if not base or not base.strip():
        return {}
    url = base.strip().rstrip("/")
    if not url.endswith("/v1"):
        url = f"{url}/v1"
    return {"omniroute": ProviderConfig(base_url=url, api_key_env="OMNIROUTE_API_KEY")}


#: Interactive-CLI default provider set. Used only through the shared bootstrap
#: contract, which reports it as ``field_sources["providers"] == "default"`` and
#: refuses it under the production profile.
# ---------------------------------------------------------------------------
# CLI bootstrap + route gate — canonical: verdict.actions.helpers
# ---------------------------------------------------------------------------
_CLI_DEFAULT_PROVIDERS = _HELPERS_CLI_DEFAULT_PROVIDERS
_cli_bootstrap = _action_helpers._cli_bootstrap
_report_bootstrap_notes = _action_helpers._report_bootstrap_notes
_build_route_gate = _action_helpers.build_route_gate  # backward-compat alias


def _configured_completion_endpoint(
    providers: dict[str, ProviderConfig] | None,
) -> tuple[str, str | None] | None:
    """Return the OpenAI-compatible (base_url, api_key) used for CLI execution."""
    endpoint = omniroute_endpoint_from_env(providers)
    if endpoint is not None:
        return endpoint
    if not providers:
        return None
    for cfg in providers.values():
        base_url = getattr(cfg, "base_url", "") or ""
        if not str(base_url).strip():
            continue
        key = getattr(cfg, "api_key", None)
        env_name = getattr(cfg, "api_key_env", None)
        if not key and env_name:
            key = os.getenv(str(env_name))
        return str(base_url).strip(), key
    return None


def _ensure_cli_gateway_ready() -> None:
    """Make the gateway ready before the CLI executes through it, or fail closed.

    Opt-in: ``VERDICT_ENSURE_GATEWAY`` turns the active path on, so an ordinary
    ``verdict route`` keeps its current behaviour and never starts a process the
    operator did not ask for. When it is on and the gateway cannot be made ready,
    the command exits instead of quietly executing somewhere else.
    """
    if os.getenv("VERDICT_ENSURE_GATEWAY", "false").lower() not in {"1", "true", "yes", "on"}:
        return
    from verdict.gateway_lifecycle import require_gateway_ready
    from verdict.provider_bootstrap import BootstrapError, describe_bootstrap_failure

    try:
        bootstrap = _action_helpers._cli_bootstrap()
        outcome = require_gateway_ready(bootstrap, probe=_gateway_probe_for(bootstrap))
    except BootstrapError as exc:
        print(describe_bootstrap_failure(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    if outcome.state != "not_required":
        print(f"verdict: {outcome.describe()}", file=sys.stderr)


def _execute_cli_decision(gate: Gate, task: str, dec: Any, *, allow_offline: bool) -> Any:
    """Send the selected route through the configured provider, or fail closed."""
    from dataclasses import replace

    from verdict.models import RoutingDecision

    if not isinstance(dec, RoutingDecision):
        return dec
    if allow_offline:
        return replace(
            dec,
            transport_outcome="error",
            reason="offline routing does not execute a provider completion",
            execute_preview="offline routing does not execute a provider completion",
        )
    if dec.decision == "denied":
        return dec
    if dec.transport_outcome in {"sent", "success"}:
        return dec
    endpoint = _configured_completion_endpoint(getattr(gate, "providers", None))
    if endpoint is None:
        return replace(
            dec,
            transport_outcome="error",
            reason="no configured provider endpoint",
            execute_preview="no configured provider endpoint",
        )
    _ensure_cli_gateway_ready()
    packed = dec.context_pack_prompt or task
    outcome, preview = execute_offload_chat(endpoint[0], dec.model, packed, api_key=endpoint[1])
    reason = dec.reason
    if outcome != "sent":
        reason = preview or reason
    return replace(dec, transport_outcome=outcome, execute_preview=preview, reason=reason)


def cmd_route(
    task: str,
    criticality: str,
    terse: bool = False,
    allow_offline: bool = False,
    allow_legacy_selector: bool | None = None,
) -> None:
    """Route a single task.

    Authority is derived from profile / ``VERDICT_REQUIRE_EXECUTION_PATH``
    (and production profile). Pass ``allow_legacy_selector=True`` only as an
    explicit migration escape — never silent. ``allow_offline`` only switches the
    catalog/network surface; it must not imply legacy selector escape. API serve
    still forces authority.
    """
    from verdict.actions.registry import run_action

    if allow_legacy_selector is None:
        allow_legacy_selector = False

    gate = _action_helpers.build_route_gate(allow_offline=allow_offline)
    params = {
        "task": task,
        "criticality": criticality,
        "allow_offline": allow_offline,
        "allow_legacy_selector": allow_legacy_selector,
        "gate": gate,
        "terse": terse,
    }
    if terse:
        result = run_action("route", params)
    else:
        status_label = (
            "[bold green]Evaluating static catalog (offline)..."
            if allow_offline
            else "[bold green]Evaluating network & heuristics..."
        )
        with console.status(status_label, spinner="dots"):
            result = run_action("route", params)

    data = result.data
    gate = data["gate"]
    dec = _execute_cli_decision(gate, task, data["decision"], allow_offline=allow_offline)
    base_selection = data["selection"]
    selection = None
    if base_selection is not None:
        selection = type(base_selection)(
            strategy="DIRECT",
            model=dec.model,
            reasoning="CLI route/run dispatched one provider completion directly",
            timestamp=base_selection.timestamp,
        )
    transport_ok = dec.transport_outcome in {"sent", "success"}

    if terse:
        if not transport_ok:
            error_payload = {
                "model": getattr(dec, "model", None),
                "provider": getattr(dec, "provider", None),
                "transport_outcome": dec.transport_outcome,
                "reason": getattr(dec, "reason", ""),
                "request_id": getattr(dec, "request_id", ""),
                "execute_preview": (dec.execute_preview or "")[:500],
            }
            print(json.dumps(error_payload, sort_keys=True))
            raise SystemExit(1)
        print(dec.model)
        return

    from verdict import present

    assert selection is not None  # non-terse always requests a strategy
    present.header("Routing Decision")
    present.kv(
        {
            "Task": task[:100] + ("..." if len(task) > 100 else ""),
            "Model": f"{dec.model}  (T{dec.tier})",
            "Provider": dec.provider,
            "Outcome": dec.decision,
            "Managed": dec.managed_backend_status,
            "Transport": dec.transport_outcome,
            "Quality": dec.quality_outcome,
            "Protected": str(dec.protected).lower(),
            "Degraded": str(dec.degraded_mode).lower(),
            "Latency": f"{dec.latency_ms:.1f}ms",
            "Strategy": selection.strategy,
            "Reason": dec.reason,
        }
    )
    payload: dict[str, Any] = {
        "strategy_selection": selection.to_dict(),
        "transport_outcome": dec.transport_outcome,
        "model": dec.model,
        "provider": dec.provider,
        "request_id": dec.request_id,
    }
    if dec.admit_receipt:
        payload["admit_receipt"] = dec.admit_receipt
    if dec.execute_preview:
        payload["execute_preview"] = dec.execute_preview[:500]
    print(json.dumps(payload, sort_keys=True))
    if not transport_ok:
        raise SystemExit(1)


def cmd_compare(task: str, criticality: str = "medium", allow_offline: bool = False) -> None:
    """Compare a DIRECT frontier call against the Verdict route (issue #265)."""
    from verdict.actions.registry import run_action

    gate = _action_helpers.build_route_gate(allow_offline=allow_offline)
    result = run_action(
        "compare",
        {"task": task, "criticality": criticality, "allow_offline": allow_offline, "gate": gate},
    )
    print(json.dumps(result.data, sort_keys=True, indent=2))


def cmd_stats(log_path: str = "verdict-decisions.jsonl") -> None:
    """Parse JSONL logs and build analytics."""
    from verdict import present
    from verdict.actions.registry import run_action

    result = run_action("stats", {"log_path": log_path})
    data = result.data
    present.header("Routing stats")
    if data.get("missing"):
        present.warn("log", f"No log file found at {data['log_path']}")
        return
    tiers: dict[int, int] = data["tiers"]
    total = data["total_requests"]
    avg_latency = data["avg_latency_ms"]
    top_models = data["top_models"]
    present.table(
        ["Tier", "Count", "Pct"],
        [
            (f"T{t}", str(tiers[t]), f"{(tiers[t] / total) * 100 if total > 0 else 0:.1f}%")
            for t in sorted(tiers)
        ],
        title="Tier Distribution",
    )
    present.kv({"Total Requests": str(total), "P50 Latency": f"{avg_latency:.2f}ms"})
    present.section("Top Routed Models")
    present.table(["Model", "Calls"], [(mod, str(count)) for mod, count in top_models])


def cmd_benchmark(
    fixture: str,
    output_json: str | None = None,
    *,
    allow_live_provider: bool = False,
    live_provider: str | None = None,
    savings: bool = False,
    live_paired: bool = False,
) -> None:
    """Run the reproducible local benchmark harness and optionally persist JSON."""
    if savings:
        from verdict.savings_bench import (
            DEFAULT_SAVINGS_FIXTURE_PATH,
            format_savings_report,
            run_savings_bench,
        )

        path = fixture
        if fixture == "benchmarks/fixtures/reproducible.json":
            path = str(DEFAULT_SAVINGS_FIXTURE_PATH)
        execute_arm = None
        if live_paired:
            # The only claim-capable mode. Both arms execute against the
            # configured OmniRoute gateway; without one we refuse rather than
            # silently degrade to the labeled simulation.
            from verdict.savings_live import LiveExecutorUnavailableError, executor_from_env

            try:
                execute_arm = executor_from_env()
            except LiveExecutorUnavailableError as exc:
                from verdict import present

                present.header("Benchmark  /  savings")
                present.fail("live executor", str(exc))
                raise SystemExit(2) from exc
        report = run_savings_bench(path, execute_arm=execute_arm, live_admit=live_paired)
        from verdict import present

        present.header("Benchmark  /  savings")
        # The report body is a stable text artifact meant for piping; keep it raw.
        print(format_savings_report(report), end="")
        if output_json:
            output_path = Path(output_json)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        return

    report = run_reproducible_benchmarks(
        fixture, allow_live_provider=allow_live_provider, live_provider=live_provider
    )
    from verdict import present

    present.header("Benchmark  /  reproducible")
    # The report body is a stable text artifact meant for piping; keep it raw.
    print(format_benchmark_report(report), end="")

    if output_json:
        output_path = Path(output_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def cmd_quickstart(
    *, output_json: bool = False, non_interactive: bool = False, dry_run: bool = False
) -> None:
    """Run the deterministic, credential-free flagship quickstart."""

    # The flags are explicit operational contracts even though this fixture is
    # already non-interactive and read-only. Keep the values visible for future
    # extensions without changing the deterministic output.
    _ = non_interactive, dry_run
    try:
        from verdict.flagship_demo import render_report, run_demo

        result = run_demo()
    except Exception as exc:
        if output_json:
            print(json.dumps({"status": "fail", "error": type(exc).__name__}, sort_keys=True))
        else:
            print(f"Verdict credential-free quickstart: FAIL ({type(exc).__name__})")
        raise SystemExit(1) from exc

    if output_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(render_report(result), end="")


def cmd_cost_report() -> None:
    """Calculates and prints the estimated token usage execution cost from historic routing decisions."""
    from verdict import present
    from verdict.actions.registry import run_action

    present.header("Cost and Usage Report")
    result = run_action("cost-report", {"log_path": "verdict-decisions.jsonl"})
    data = result.data
    if data.get("missing"):
        present.warn("log", "No routing telemetry found (Verdict decision log missing).")
        return
    present.table(
        ["Metric", "Value"],
        [
            ("Total Routing Requests", str(data["total_requests"])),
            ("T0 (Critical) Forwarded", str(data["t0_requests"])),
            ("Offloaded Tasks (T1-T3)", str(data["offloaded_requests"])),
            ("Estimated Savings vs T0 Only", f"${data['estimated_savings_usd']:.2f}"),
        ],
        title="Usage Summary",
    )


def cmd_detect(
    verbose: bool = False,
    output_json: bool = False,
    output_config: bool = False,
    offline: bool = False,
) -> None:
    """Detect available LLM providers."""
    from verdict.actions.registry import run_action

    result = run_action("detect", {"offline": offline})
    data = result.data
    if data.get("status") == "failed":
        from verdict import present

        present.header("Provider detection")
        present.fail("Detection failed", data["error"])
        import traceback

        traceback.print_exc()
        sys.exit(1)

    if offline:
        # For --json / --config, print the offline payload verbatim from data.
        if output_json:
            print(json.dumps(data, indent=2, sort_keys=True))
        elif output_config:
            print(yaml.dump({"providers": {}}, default_flow_style=False))
        else:
            from verdict import present

            present.header("Provider detection (offline)")
            present.kv({"network access": "no", "credentials read": "no"})
            present.note("Offline mode reports nothing by design. Run `verdict detect` to probe.")
        return

    if output_json:
        payload = {k: v for k, v in data.items() if not k.startswith("_")}
        print(json.dumps(payload, indent=2))
        return
    if output_config:
        from verdict.provider_detection import generate_verdict_config

        config = generate_verdict_config(data["_result"])
        print(yaml.dump(config, default_flow_style=False))
        return

    from verdict import present
    from verdict.provider_detection import format_detection_report

    healthy_gateways = data["_healthy_gateways"]
    present.header("Provider detection")
    present.note(format_detection_report(data["_result"], verbose=verbose))
    present.section("Gateways (HTTP-validated)")
    if healthy_gateways:
        present.table(
            ["Gateway", "Identity", "URL", "Port"],
            [(g.display_name, g.identity, g.url, g.port) for g in healthy_gateways],
        )
        if len(healthy_gateways) > 1:
            present.warn(
                "gateway selection",
                "Multiple gateways found. Set OMNIROUTE_BASE_URL to one of the above to select it.",
            )
    else:
        present.warn("gateway", "No local gateway found on ports 20128, 20129, 20132.")
        present.note("To start OmniRoute: npm install -g omniroute && omniroute serve")


def cmd_certify(*, snapshot_path: str | None = None, output_json: bool = True) -> None:
    """Emit a runtime certification report (evidence only, JSON).

    Delegates to the ``certify`` action; the CLI only formats/prints.
    """
    from verdict.actions.registry import run_action

    result = run_action("certify", {"snapshot_path": snapshot_path})
    encoded = json.dumps(result.data, indent=2, sort_keys=True)
    if output_json:
        print(encoded)
    else:
        from verdict import present

        present.header("Runtime certification")
        present.note(encoded)
    if result.exit_code:
        sys.exit(result.exit_code)


def cmd_probe(
    models: list[str],
    base_url: str = "http://localhost:20128/v1",
    timeout: float = 20.0,
    output_json: bool = False,
    allow_live_probe: bool = False,
    transport: Any | None = None,
) -> None:
    """Run a bounded one-token probe, requiring consent for network transport.

    Sends the fixed, no-user-data probe payload (max_tokens=1) so a model can be
    confirmed live before it is assigned real work (e.g. a subagent).
    """
    from verdict.actions.registry import run_action

    params: dict[str, Any] = {
        "models": models,
        "base_url": base_url,
        "timeout": timeout,
        "allow_live_probe": allow_live_probe,
    }
    if transport is not None:
        params["transport"] = transport
    result = run_action("probe", params)
    if not result.ok and result.exit_code == 2:
        message = result.data.get("error", "live probes require consent")
        if output_json:
            print(json.dumps({"error": message, "diagnostics": None}, sort_keys=True))
        else:
            from verdict import present

            present.header("Probe")
            present.fail("probe", message)
        raise SystemExit(2)

    if output_json:
        print(json.dumps(result.data, indent=2))
        if not result.ok:
            raise SystemExit(1)
        return

    from verdict import present
    from verdict.probes import _redact

    results = result.data.get("results", [])
    present.header(f"Probe  /  {_redact(base_url)}")
    present.table(
        ["Model", "Status", "HTTP", "Latency (ms)"],
        [
            (
                str(entry["model"]),
                "LIVE" if entry.get("ok") else f"DOWN {entry.get('error', '')}",
                str(entry.get("http_status", "-")),
                str(entry.get("latency_ms", "-")),
            )
            for entry in results
        ],
    )
    if not result.ok:
        sys.exit(1)


def _execution_path_decision_from_request_file(path: str, *, task: str) -> Any:
    """Build the mandatory execution-path decision in-process from the public contract."""

    from verdict.execution_path import optimize_execution_path
    from verdict.subagent_resolver import public_execution_path_request

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return optimize_execution_path(public_execution_path_request(raw, task=task))


def _cli_execution_path_decision(
    parser: argparse.ArgumentParser,
    request_path: str | None,
    *,
    packet_path: str | None = None,
    task: str | None = None,
) -> Any:
    """Load a public request and bind it to the CLI task or packet objective."""

    if request_path is None:
        return None
    if task is None:
        if packet_path is None:
            parser.error("--execution-path-request requires a task objective")
        try:
            packet_raw = json.loads(Path(packet_path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            parser.error(f"cannot read packet for execution-path request binding: {exc}")
        task = str(packet_raw.get("objective") or "") if isinstance(packet_raw, dict) else ""
        if not task.strip():
            parser.error("packet must carry a non-empty objective for --execution-path-request")
    try:
        return _execution_path_decision_from_request_file(request_path, task=task)
    except Exception as exc:
        parser.error(f"execution-path request is invalid: {exc}")


def cmd_autodev(
    objective: str,
    repo: str,
    *,
    orchestrator_model: str | None,
    executor_model: str | None,
    base_url: str | None,
    output_json: bool = False,
    allow_live: bool = False,
    no_mechanical: bool = False,
    dry_run: bool = False,
    execution_path_decision: Any = None,
) -> None:
    """Decompose an objective, execute each unit, verify it, and record the outcome.

    This edits the working tree and calls live models, so it requires explicit
    consent via ``--allow-live``.  ``--dry-run`` shows the plan and its measured
    cost without executing any unit.
    """
    from verdict.autodev_run import (
        AutodevError,
        _assert_exact_model,
        _require_launch_decision,
        collect_ruff_evidence,
        run_autodev,
    )
    from verdict.decomposer import Decomposer, DecompositionConfig, DecompositionError

    decision = _require_launch_decision(execution_path_decision, surface="autodev CLI")
    selected_route = decision.selected_route
    assert selected_route is not None
    _assert_exact_model(executor_model, selected_route.model, surface="autodev CLI executor")
    _assert_exact_model(
        orchestrator_model, selected_route.model, surface="autodev CLI orchestrator"
    )
    executor_model = selected_route.model
    orchestrator_model = selected_route.model
    if base_url is not None and base_url.rstrip("/") != selected_route.gateway.rstrip("/"):
        raise AutodevError(
            "autodev CLI: --base-url must match the BOD-104 selected gateway "
            f"{selected_route.gateway!r}"
        )
    base_url = selected_route.gateway
    repo_path = Path(repo).resolve()
    if not allow_live:
        message = (
            "autodev calls live models and edits the working tree; pass --allow-live to consent"
        )
        if output_json:
            print(json.dumps({"error": message}, sort_keys=True))
        else:
            from verdict import present

            present.header("Autodev")
            present.fail("consent", message)
        raise SystemExit(2)

    api_key = os.getenv("OMNIROUTE_API_KEY")
    evidence = collect_ruff_evidence(repo_path)

    if dry_run:
        decomposer = Decomposer(
            DecompositionConfig(model=orchestrator_model, base_url=base_url, api_key=api_key)
        )
        try:
            plan = decomposer.decompose(objective, repo_root=repo_path, evidence=evidence)
        except DecompositionError as exc:
            _report_autodev_failure(str(exc), output_json=output_json)
            raise SystemExit(1) from exc
        if output_json:
            print(json.dumps(plan.to_dict(), indent=2, sort_keys=True))
        else:
            from verdict import present

            present.header("Autodev  /  dry run")
            present.ok("plan", f"{len(plan.units)} unit(s) planned by {plan.model}")
            present.table(
                ["Unit", "Owned files", "Verify"],
                [
                    (unit.unit_id, ", ".join(unit.owned_files), " ".join(unit.verification_command))
                    for unit in plan.units
                ],
                empty="no units planned",
            )
            present.note(
                f"orchestrator tokens: {plan.usage.total_tokens}"
                f"{'' if plan.usage.reported else ' (not reported by provider)'}"
            )
        return

    try:
        report = run_autodev(
            objective,
            repo_path,
            orchestrator_model=orchestrator_model,
            executor_model=executor_model,
            execution_path_decision=decision,
            base_url=base_url,
            api_key=api_key,
            evidence=evidence,
            mechanical=not no_mechanical,
        )
    except DecompositionError as exc:
        # A decomposition that cannot state its own checks is a reportable
        # negative result about decomposition, not a crash.
        _report_autodev_failure(str(exc), output_json=output_json)
        raise SystemExit(1) from exc

    if output_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        from verdict import present

        present.header("Autodev")
        units_line = (
            f"{len(report.verified)} verified, {len(report.failed)} failed, "
            f"of {report.units_planned} planned"
        )
        if report.failed:
            present.fail("units", units_line)
        else:
            present.ok("units", units_line)
        tokens = report.to_dict()["tokens"]
        rows: dict[str, Any] = {
            "objective": report.objective,
            "mechanical (zero tokens)": len(report.mechanical),
            f"model ({report.executor_model})": len(report.outcomes) - len(report.mechanical),
            "orchestrator tokens": (
                f"{tokens['orchestrator']['total_tokens']} ({report.orchestrator_model})"
            ),
            "executor tokens": tokens["executor"]["total_tokens"],
        }
        share = tokens["expensive_share"]
        if share is not None:
            rows["expensive share"] = f"{share:.1%} of {tokens['total']} measured tokens"
        present.kv(rows)
        if report.unreported_units:
            present.warn(
                "usage",
                f"{len(report.unreported_units)} unit(s) had no provider usage block; "
                "their tokens are unknown, not estimated",
            )
        for outcome in report.failed:
            present.fail(outcome.unit_id, outcome.reason)
    if report.failed:
        sys.exit(1)


def cmd_autodev_packet_execute(
    packet_path: str,
    repo: str,
    *,
    output_json: bool = False,
    allow_live: bool = False,
    resume: bool = False,
    primary_fallback: str | None = None,
    prefer_non_primary: bool = False,
    base_url: str | None = None,
    catalog_rows: Any = None,
    probe_transport: Any = None,
    canary_path: str | None = None,
    delegation: str | None = None,
    undelegable_reason: str | None = None,
    execution_path_decision: Any = None,
) -> None:
    """Execute or resume one bounded packet work unit through an admitted route.

    A dry-run, mock, decomposition-only, or unverified response can never
    produce a completed proof class here; only independent trusted verification
    decides success. ``--primary-fallback`` is the operator designation path
    when no CandidateEvidence object is supplied; ``run_packet_autodev``
    composes ``primary`` from ``to_admission_record`` when refresh returns
    evidence.
    """
    from verdict.autodev_run import (
        AutodevError,
        designated_primary_fallback,
        packet_family_run_payload,
        refuse_opaque_family_route,
        run_packet_autodev,
    )
    from verdict.execution_packet import (
        ExecutionPacketStore,
        UnsupportedSchemaVersionError,
        schema_refusal_receipt,
    )

    def _human_refusal(label: str, message: str) -> None:
        from verdict import present

        present.header("Autodev packet  /  execute")
        present.fail(label, message)

    if not allow_live:
        message = "packet execute calls live routes and edits the working tree; pass --allow-live"
        if output_json:
            print(json.dumps({"error": message}, sort_keys=True))
        else:
            _human_refusal("consent", message)
        raise SystemExit(2)

    path = Path(packet_path).expanduser().resolve()
    store = ExecutionPacketStore(path.parent)
    try:
        packet = store.validate(path)
    except UnsupportedSchemaVersionError as exc:
        receipt = schema_refusal_receipt(exc)
        if output_json:
            print(json.dumps(receipt, sort_keys=True))
        else:
            _human_refusal("schema", f"refused before any gateway request: {exc}")
        raise SystemExit(1) from exc

    from verdict.autodev_run import _require_launch_decision

    decision = _require_launch_decision(execution_path_decision, surface="packet execute CLI")
    selected_route = decision.selected_route
    assert selected_route is not None
    route = dict(packet.route_attempts[-1]) if packet.route_attempts else {}
    if base_url and base_url.rstrip("/") != selected_route.gateway.rstrip("/"):
        message = "--base-url does not match the BOD-104 selected gateway"
        if output_json:
            print(json.dumps({"error": message}, sort_keys=True))
        else:
            _human_refusal("gateway", message)
        raise SystemExit(1)
    packet_provider = str(route.get("provider") or "")
    packet_gateway = str(route.get("gateway") or "")
    packet_endpoint = str(route.get("base_url") or "")
    if packet_provider and packet_provider != selected_route.provider:
        message = "packet route provider does not match the BOD-104 ExecutionPathDecision"
        if output_json:
            print(json.dumps({"error": message}, sort_keys=True))
        else:
            _human_refusal("provider", message)
        raise SystemExit(1)
    if packet_gateway and packet_gateway != selected_route.gateway:
        message = "packet route gateway does not match the BOD-104 ExecutionPathDecision"
        if output_json:
            print(json.dumps({"error": message}, sort_keys=True))
        else:
            _human_refusal("gateway", message)
        raise SystemExit(1)
    if packet_endpoint and packet_endpoint.rstrip("/") != selected_route.gateway.rstrip("/"):
        message = "packet route endpoint does not match the BOD-104 selected gateway"
        if output_json:
            print(json.dumps({"error": message}, sort_keys=True))
        else:
            _human_refusal("endpoint", message)
        raise SystemExit(1)
    requested = str(route.get("requested_identity") or route.get("model") or "")
    route_identity = str(route.get("model") or route.get("actual_identity") or requested)
    route_was_unbound = not requested
    if route_identity and route_identity != selected_route.model:
        message = "packet route model does not exactly match the BOD-104 ExecutionPathDecision"
        if output_json:
            print(json.dumps({"error": message}, sort_keys=True))
        else:
            _human_refusal("model", message)
        raise SystemExit(1)
    route["provider"] = selected_route.provider
    route["gateway"] = selected_route.gateway
    route["base_url"] = selected_route.gateway
    if route_was_unbound:
        # A trusted decision is itself the admission source when a packet has
        # no historical route attempt. Do not let a later catalog harvest
        # select a different worker.
        route["requested_identity"] = selected_route.model
        route["model"] = selected_route.model
        route["actual_identity"] = selected_route.model
        route["admitted"] = True
        route["evidence_digest"] = next(
            (
                digest
                for digest in decision.evidence_digests.values()
                if str(digest).startswith("sha256:")
            ),
            decision.decision_digest,
        )
    try:
        refuse_opaque_family_route(route)
    except AutodevError as exc:
        if output_json:
            print(json.dumps({"error": str(exc)}, sort_keys=True))
        else:
            _human_refusal("route", str(exc))
        raise SystemExit(1) from exc
    if delegation is None:
        # FR-031: the production entry point refuses an unclassified unit by
        # default rather than silently skipping the delegation floor.
        message = (
            "a delegation classification ('legwork' or 'decision') is required "
            "before dispatch; pass --delegation to classify this unit"
        )
        if output_json:
            print(json.dumps({"error": message, "missing": "delegation"}, sort_keys=True))
        else:
            _human_refusal("delegation", message)
        raise SystemExit(1)
    if prefer_non_primary and route.get("primary") is True:
        message = (
            "first attempt must use a concrete non-primary route; "
            "the supplied admitted route occupies the primary-subscription role"
        )
        if output_json:
            print(
                json.dumps(
                    {"error": message, "missing": "non-primary admitted route"}, sort_keys=True
                )
            )
        else:
            _human_refusal("route", message)
        raise SystemExit(1)
    fallback_route = None
    if primary_fallback:
        digest = str(route.get("evidence_digest") or "")
        actual = primary_fallback
        for attempt in packet.route_attempts:
            requested = str(attempt.get("requested_identity") or "")
            served = str(attempt.get("actual_identity") or "")
            if primary_fallback in {requested, served}:
                digest = str(attempt.get("evidence_digest") or digest)
                actual = served or primary_fallback
                break
        fallback_route = designated_primary_fallback(
            primary_fallback, evidence_digest=digest, actual_identity=actual
        )
    if catalog_rows is None and not str(route.get("requested_identity") or "").strip():
        import urllib.request

        from verdict.free_route_harvest import catalog_rows_from_payload

        family_url = str(route.get("base_url") or base_url or DEFAULT_BASE_URL).rstrip("/")
        request = urllib.request.Request(
            family_url + "/models", headers={"Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:  # nosec B310
                catalog_rows = catalog_rows_from_payload(
                    json.loads(response.read().decode("utf-8"))
                )
        except Exception:
            catalog_rows = []
        if probe_transport is None:
            from verdict.probes import openai_probe_transport

            probe_transport = openai_probe_transport(
                family_url, api_key=_doctor_diag._read_omniroute_token()
            )
    canary_state = None
    if canary_path:
        loaded = json.loads(Path(canary_path).expanduser().resolve().read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            message = "canary JSON must be an object"
            if output_json:
                print(json.dumps({"error": message}, sort_keys=True))
            else:
                _human_refusal("canary", message)
            raise SystemExit(1)
        canary_state = loaded
    report = run_packet_autodev(
        packet,
        Path(repo).expanduser().resolve(),
        admitted_route=route,
        execution_path_decision=decision,
        fallback_route=fallback_route,
        resume=resume,
        catalog_rows=catalog_rows,
        probe_transport=probe_transport,
        canary_state=canary_state,
        # FR-037: the CLI is the real production entry, so red-green is required
        # here by default. A resume legitimately re-verifies prior work and may
        # find it already green, so the requirement is scoped to fresh attempts.
        require_red_green=not resume,
        delegation=delegation,
        undelegable_reason=undelegable_reason,
    )
    family_url = str(route.get("base_url") or base_url or DEFAULT_BASE_URL)
    payload = packet_family_run_payload(packet, report, family_url)
    if output_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        from verdict import present

        present.header("Autodev packet  /  execute")
        state = str(report.terminal_state)
        present.status("terminal state", "ok" if state == "completed" else "failed", state)
        present.kv(
            {
                "proof level": payload["proof_level"],
                "fallbacks": report.fallback_count,
                "checkpoints": len(report.checkpoints),
            }
        )
    if report.terminal_state != "completed":
        raise SystemExit(1)


def cmd_autodev_packet_shadow(episodes_path: str, *, output_json: bool = False) -> None:
    """Dump an advisory shadow-learning JSON report. Does not call EligibilityGate."""
    from verdict.actions.registry import run_action

    result = run_action("autodev.packet.shadow", {"episodes_path": episodes_path})
    data = result.data
    if not result.ok:
        message = str(data.get("error", "autodev.packet.shadow failed"))
        if output_json:
            print(json.dumps({"error": message}, sort_keys=True))
        else:
            from verdict import present

            present.header("Autodev packet  /  shadow")
            present.fail("episodes", message)
        raise SystemExit(result.exit_code or 1)
    print(json.dumps(data, indent=2, sort_keys=True))


def cmd_autodev_packet_canary(
    episodes_path: str, admitted_path: str, *, output_json: bool = False
) -> None:
    """Dump an explicit bounded canary choice. Does not call EligibilityGate."""
    from verdict.actions.registry import run_action

    if not episodes_path or not admitted_path:
        message = "canary apply requires --episodes and --admitted"
        if output_json:
            print(json.dumps({"error": message}, sort_keys=True))
        else:
            from verdict import present

            present.header("Autodev packet  /  canary")
            present.fail("inputs", message)
        raise SystemExit(1)
    result = run_action(
        "autodev.packet.canary", {"episodes_path": episodes_path, "admitted_path": admitted_path}
    )
    data = result.data
    if not result.ok:
        message = str(data.get("error", "autodev.packet.canary failed"))
        if output_json:
            print(json.dumps({"error": message}, sort_keys=True))
        else:
            from verdict import present

            present.header("Autodev packet  /  canary")
            present.fail("inputs", message)
        raise SystemExit(result.exit_code or 1)
    print(json.dumps(data, indent=2, sort_keys=True))


def cmd_autodev_packet_canary_rollback(state_path: str, *, output_json: bool = False) -> None:
    """Restore the pre-canary baseline choice. Does not call EligibilityGate."""
    from verdict.autodev_run import rollback_shadow_canary

    state = json.loads(Path(state_path).expanduser().resolve().read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        message = "canary rollback requires a canary state object"
        if output_json:
            print(json.dumps({"error": message}, sort_keys=True))
        else:
            from verdict import present

            present.header("Autodev packet  /  canary rollback")
            present.fail("state", message)
        raise SystemExit(1)
    print(json.dumps(rollback_shadow_canary(state), indent=2, sort_keys=True))


def cmd_autodev_packet(
    action: str,
    packet_path: str,
    *,
    source_path: str | None = None,
    model: str | None = None,
    output_json: bool = False,
    family_a_path: str | None = None,
    family_b_path: str | None = None,
) -> None:
    """Create or inspect a portable packet without granting execution authority."""

    from verdict.execution_packet import (
        ExecutionPacket,
        ExecutionPacketError,
        ExecutionPacketStore,
        UnsupportedSchemaVersionError,
        schema_refusal_receipt,
    )

    path = Path(packet_path).expanduser().resolve()
    store = ExecutionPacketStore(path.parent)
    try:
        if action == "create":
            if source_path is None:
                raise ExecutionPacketError("packet create requires --from")
            source = Path(source_path).expanduser().resolve()
            payload = json.loads(source.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ExecutionPacketError("packet source JSON must be an object")
            created = ExecutionPacket.from_dict(payload)
            store.create(created, path)
            packet = created
        elif action in {"inspect", "validate"}:
            packet = store.validate(path)
        elif action == "resume":
            if model is None:
                raise ExecutionPacketError("packet resume requires --model")
            packet = store.resume(path, executing_model=model)
        elif action == "compare":
            from verdict.autodev_run import AutodevError, compare_family_runs

            if family_a_path is None or family_b_path is None:
                raise ExecutionPacketError("packet compare requires --a and --b")
            packet = store.validate(path)
            family_a = json.loads(Path(family_a_path).expanduser().read_text(encoding="utf-8"))
            family_b = json.loads(Path(family_b_path).expanduser().read_text(encoding="utf-8"))
            if not isinstance(family_a, dict) or not isinstance(family_b, dict):
                raise ExecutionPacketError("family run JSON must be an object")
            try:
                data = compare_family_runs(family_a, family_b, packet=packet)
            except AutodevError as exc:
                raise ExecutionPacketError(str(exc)) from exc
            if output_json:
                print(json.dumps(data, indent=2, sort_keys=True))
            else:
                from verdict import present

                present.header("Autodev packet  /  compare")
                present.kv(
                    {
                        "pair": data["pair_id"],
                        "parity claimed": data["parity_claimed"],
                        "unknown facets": len(data["unknown_facets"]),
                    }
                )
            return
        else:
            raise ExecutionPacketError(f"unsupported packet action: {action}")
    except UnsupportedSchemaVersionError as exc:
        receipt = schema_refusal_receipt(exc)
        if output_json:
            print(json.dumps(receipt, sort_keys=True))
        else:
            from verdict import present

            present.header(f"Autodev packet  /  {action}")
            present.fail(
                "schema",
                f"refused {receipt['encountered_schema_version']!r} — "
                f"supported: {', '.join(receipt['supported_schema_versions'])} "
                f"(no gateway request issued)",
            )
        raise SystemExit(1) from exc
    except (ExecutionPacketError, OSError, ValueError) as exc:
        if output_json:
            print(json.dumps({"error": str(exc)}, sort_keys=True))
        else:
            from verdict import present

            present.header(f"Autodev packet  /  {action}")
            present.fail("packet", str(exc))
        raise SystemExit(1) from exc

    data = packet.to_dict()
    if model is not None and action == "resume":
        data["executing_model"] = model
    if output_json:
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        from verdict import present

        present.header(f"Autodev packet  /  {action}")
        present.kv(
            {
                "packet": packet.packet_id,
                "version": f"v{packet.packet_version}",
                "proof level": packet.proof_level.value,
                "next safe action": packet.next_safe_action,
            }
        )


def cmd_autodev_golden_path(
    objective: str,
    repo: str,
    memory_path: str,
    verification_command: list[str],
    timeout_seconds: float,
    owned_paths: list[str],
    output_json: bool,
) -> None:
    """Run the offline, real-repository three-stage acceptance path."""
    from verdict.golden_path import run_golden_path

    report = run_golden_path(
        objective,
        repo,
        memory_path=memory_path,
        verification_command=verification_command,
        timeout_seconds=timeout_seconds,
        owned_paths=owned_paths,
    )
    if output_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        from verdict import present

        present.header("Autodev golden path")
        accepted = report.decision == "accepted"
        present.status("decision", "ok" if accepted else "failed", report.decision)
        for receipt in report.stages:
            status_value = receipt.status.value
            stage_state = "ok" if status_value == "passed" else "failed"
            present.status(receipt.stage.value, stage_state, status_value)
        present.note(f"report digest: {report.report_digest}")
    if report.decision != "accepted":
        raise SystemExit(1)


def _report_autodev_failure(reason: str, *, output_json: bool) -> None:
    if output_json:
        print(json.dumps({"error": "decomposition failed", "reason": reason}, sort_keys=True))
    else:
        from verdict import present

        present.fail("decomposition failed", reason)


_probe_result_payload = _action_helpers.probe_result_payload


def cmd_catalog(
    *,
    base_url: str,
    management: bool,
    expected_rows: int,
    freshness_seconds: int,
    db_path: str | None,
    probe: bool,
    probe_limit: int,
    probe_timeout: float,
    output_json: bool,
    allow_live_probe: bool = False,
) -> None:
    """Qualify one or both documented OmniRoute catalog projections."""
    from verdict.actions.registry import run_action

    result = run_action(
        "catalog",
        {
            "base_url": base_url,
            "management": management,
            "expected_rows": expected_rows,
            "freshness_seconds": freshness_seconds,
            "db_path": db_path,
            "probe": probe,
            "probe_limit": probe_limit,
            "probe_timeout": probe_timeout,
            "allow_live_probe": allow_live_probe,
        },
    )
    data = result.data
    if data.get("status") == "refused":
        if output_json:
            print(json.dumps({"error": data["error"], "probes": None}, sort_keys=True))
        else:
            from verdict import present

            present.header("Catalog qualification")
            present.fail("catalog", data["error"])
        raise SystemExit(2)
    report_payload = data["report_payload"]
    report = data["report"]
    reconciliation = data["reconciliation"]
    probe_summary = data["probe_summary"]
    if output_json:
        print(json.dumps(report_payload, sort_keys=True))
    else:
        from verdict import present

        present.header("Catalog qualification")
        present.status("catalog", "ok" if report.passed else "failed")
        snapshot = report.snapshot
        if snapshot:
            present.kv(
                {
                    "source": snapshot.source_url,
                    "rows": snapshot.row_count,
                    "fresh until": snapshot.fresh_until,
                }
            )
        if report.errors:
            present.table(["Issue"], [(error,) for error in report.errors])
        if reconciliation:
            present.status("projection reconciliation", "ok" if reconciliation.passed else "failed")
        if probe_summary:
            present.note(f"probes: {json.dumps(probe_summary.to_dict(), sort_keys=True)}")
    if not data["passed"]:
        sys.exit(1)


def cmd_suggest(log_path: str = "verdict-decisions.jsonl") -> None:
    """Run the SuggestionService to propose evidence-backed improvements."""
    from verdict import present
    from verdict.actions.registry import run_action

    result = run_action("suggest", {"log_path": log_path})
    suggestions = result.data["suggestions"]

    present.header("Verdict Intelligence Suggestions")
    if not suggestions:
        present.note("No actionable suggestions found. Your routing is optimized!")
        return

    for s in suggestions:
        present.section(f"{s['title']} ({s['id']})")
        present.kv(
            {
                "Category": s["category"].title(),
                "Novelty": s["novelty"],
                "Expires In": s["expiry"],
                "Description": s["description"],
                "Proposed Experiment": s["proposed_next_experiment"],
                "Confidence": f"{s['confidence'] * 100:.1f}%",
                "Impact": s["expected_impact"],
                "Evidence (top 3)": (
                    ", ".join(s["evidence_references"]) if s["evidence_references"] else "None"
                ),
            }
        )


# ---------------------------------------------------------------------------
# Doctor diagnostics — canonical: verdict.doctor_diagnostics
# ---------------------------------------------------------------------------
from verdict.doctor_diagnostics import (  # noqa: E402
    DOCTOR_PREFLIGHT_TIMEOUT_DEFAULT,
    DoctorDiagnostics,  # noqa: F401 — backward-compat alias
    _collect_doctor_diagnostics,  # noqa: F401 — backward-compat alias
    _doctor_documentation_preflight_is_network_only_failure,  # noqa: F401
    _doctor_fix_gateway,  # noqa: F401
    _doctor_gateway_lifecycle,  # noqa: F401
    _doctor_progress,
    _gateway_probe_for,
    _omniroute_api_request,  # noqa: F401 — backward-compat alias
    _read_omniroute_token,  # noqa: F401 — backward-compat alias
)


def cmd_doctor(
    fix: bool = False,
    output_json: bool = False,
    preflight_timeout: float = DOCTOR_PREFLIGHT_TIMEOUT_DEFAULT,
) -> None:
    """Scan the Verdict setup and OmniRoute connections for issues and repair them.

    Text and ``--json`` modes share one collector (``_collect_doctor_diagnostics``),
    so both exit 1 iff unresolved issues remain after ``--fix``. Text mode
    prints preflight progress lines on stderr; ``--json`` prints none, so
    stdout stays pure JSON.
    """
    if output_json:
        from verdict.actions.registry import run_action
        from verdict.runtime_daemons import RuntimeManager
        from verdict.runtime_health import build_runtime_health_report

        result = run_action("doctor", {"fix": fix, "preflight_timeout": preflight_timeout})
        report: dict[str, Any] = dict(result.data)
        report["runtime_health"] = build_runtime_health_report(RuntimeManager().status()).to_dict()
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        if not result.ok:
            raise SystemExit(1)
        return

    ui = TerminalUI(console)
    ui.header("Doctor")
    # Interactive only for the optional duplicate-node removal prompt; the
    # set of issues/warnings is identical to --json mode.
    diag = _doctor_diag._collect_doctor_diagnostics(
        fix, interactive=True, preflight_timeout=preflight_timeout, progress=_doctor_progress
    )
    ui.doctor(diag.capability_report)
    for label, state, detail in diag.sections:
        if state == "header":
            ui.header(label)
        elif state == "section":
            ui.section(label)
            if detail:
                ui.console.print(f"  [dim]{detail}[/dim]")
        else:
            ui.status(label, state, detail)
    for warning in diag.warnings:
        ui.status("WARNING", "warning", warning)
    ui.doctor_summary(diag.issues, diag.fixed)
    if diag.issues and not diag.config_loaded:
        ui.panel(
            "Next step", "Run verdict setup to initialize your configuration file.", tone="WARNING"
        )
    if diag.issues:
        raise SystemExit(1)


def cmd_run(
    task: str,
    criticality: str,
    terse: bool = False,
    allow_offline: bool = False,
    allow_legacy_selector: bool | None = None,
) -> None:
    """Run a single task through the routing gate (alias of route)."""
    cmd_route(
        task,
        criticality,
        terse,
        allow_offline=allow_offline,
        allow_legacy_selector=allow_legacy_selector,
    )


def cmd_plan(output_json: bool = False) -> None:
    """Print a mutation-free setup plan without probing or writing state."""
    cmd_setup_plan(output_json=output_json)


def _render_choose_summary(data: dict[str, Any]) -> str:
    """Mirror ``verdict.chooser.human_summary`` for a decision returned as a dict."""
    lines: list[str] = []
    selected = data.get("selected")
    if selected is None:
        lines.append(f"no eligible target ({data.get('reason', 'unknown')})")
    else:
        lines.append(
            f"selected {selected['gateway']}/{selected['provider']}/"
            f"{selected['resource_pool']}/{selected['model']}"
        )
        if data.get("selected_because"):
            lines.append(str(data["selected_because"]))
    fallbacks = data.get("ranked_fallbacks") or []
    if fallbacks:
        top = fallbacks[0]
        lines.append(
            "top fallback: "
            f"{top['gateway']}/{top['provider']}/{top['resource_pool']}/{top['model']}"
        )
    exclusions = data.get("exclusions") or []
    if exclusions:
        reasons = ", ".join(
            f"{item.get('model', '?')} ({item.get('reason', 'excluded')})"
            for item in exclusions[:3]
        )
        lines.append(f"excluded: {reasons}")
    return "\n".join(lines)


def cmd_choose(
    *,
    task_class: str,
    requires: str = "",
    model: str | None = None,
    candidates_json: str | None = None,
    output_json: bool = False,
) -> None:
    """Select an eligible execution target for Prime dispatch."""
    from verdict.actions.registry import run_action

    if not candidates_json:
        print(
            "verdict choose requires --candidates-json until live catalog probing is in a later story",
            file=sys.stderr,
        )
        sys.exit(2)
    result = run_action(
        "choose",
        {
            "task_class": task_class,
            "requires": requires,
            "model": model,
            "candidates_json": candidates_json,
        },
    )
    if not result.ok:
        payload = result.data
        if output_json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            from verdict import present

            present.header("Choose route")
            present.fail(task_class, payload.get("reason", "unknown"))
            exclusions = payload.get("exclusions") or []
            if exclusions:
                present.note(
                    "excluded: "
                    + ", ".join(
                        f"{item.get('model', '?')} ({item.get('reason', 'excluded')})"
                        for item in exclusions[:3]
                    )
                )
        sys.exit(result.exit_code or 1)
    if output_json:
        print(json.dumps(result.data, indent=2, sort_keys=True))
        return
    from verdict import present

    present.header("Choose route")
    present.note(_render_choose_summary(result.data))


def cmd_models(
    catalog: list[ModelInfo] | None = None,
    output_json: bool = False,
    provider: str | None = None,
    search: str | None = None,
    capability: str | None = None,
    limit: int = 0,
    show_all: bool = False,
) -> None:
    """List the authoritative model inventory from the live gateway."""
    from verdict.actions.registry import run_action

    params: dict[str, Any] = {}
    if catalog is not None:
        # Legacy injection: convert ModelInfo list to enriched row dicts.
        params["catalog_rows"] = [
            {
                "id": m.id,
                "provider": m.provider,
                "capability_tier": f"T{m.capability_tier}",
                "context_window": m.context_window if m.context_window > 0 else None,
                "tools_support": None,
                "structured_output": None,
                "input_cost_per_million": None,
                "output_cost_per_million": None,
                "source": "config",
                "freshness": None,
            }
            for m in catalog
        ]
    if provider:
        params["provider"] = provider
    if search:
        params["search"] = search
    if capability:
        params["capability"] = capability
    if limit:
        params["limit"] = limit
    if show_all:
        params["show_all"] = True

    result = run_action("models.list", params or None)
    if output_json:
        print(json.dumps(result.data, indent=2, sort_keys=True))
        return

    from verdict import present

    data = result.data if isinstance(result.data, dict) else {}
    total = data.get("total", 0)
    shown = data.get("shown", 0)
    total_filtered = data.get("total_filtered", 0)
    source = data.get("source", "unknown")
    provider_counts = data.get("provider_counts", {})
    freshness = data.get("freshness")
    entries = data.get("models", [])

    if source == "config_only":
        present.header("Model catalog (gateway unreachable: showing configured models only)")
    else:
        present.header("Model inventory")

    # Summary line.
    top_providers = sorted(provider_counts.items(), key=lambda kv: kv[1], reverse=True)[:8]
    provider_summary = ", ".join(f"{p}: {c}" for p, c in top_providers)
    present.note(f"{total} models total across {len(provider_counts)} providers")
    if provider_summary:
        present.note(f"Top providers: {provider_summary}")
    if freshness:
        present.note(f"Metadata freshness: {freshness}")

    # Table of shown rows.
    def _fmt_ctx(v: Any) -> str:
        if v is None:
            return "-"
        try:
            n = int(v)
            return f"{n:,}" if n > 0 else "-"
        except (TypeError, ValueError):
            return "-"

    def _fmt_cost(v: Any) -> str:
        if v is None:
            return "-"
        try:
            return f"${float(v):.2f}"
        except (TypeError, ValueError):
            return "-"

    def _fmt_bool(v: Any) -> str:
        if v is True:
            return "yes"
        if v is False:
            return "no"
        return "-"

    present.table(
        ["ID", "Provider", "Tier", "Context", "Tools", "$/M in", "Source"],
        [
            (
                str(m.get("id", "")),
                str(m.get("provider", "")),
                str(m.get("capability_tier", "-")),
                _fmt_ctx(m.get("context_window")),
                _fmt_bool(m.get("tools_support")),
                _fmt_cost(m.get("input_cost_per_million")),
                str(m.get("source", "")),
            )
            for m in entries
        ],
        empty="no models found",
    )
    if total_filtered > shown:
        present.note(
            f"Showing {shown} of {total_filtered} filtered models "
            f"(use --all or --limit N to see more)"
        )
    else:
        present.note(f"{shown} model(s) shown. Live eligibility: verdict eligibility --probe")


def cmd_inspect(
    model_id: str, catalog: list[ModelInfo] | None = None, output_json: bool = False
) -> None:
    """Inspect one model's catalog record and any stored passport evidence."""
    from verdict.actions.registry import run_action

    params: dict[str, Any] = {"model_id": model_id}
    if catalog is not None:
        params["catalog"] = catalog
    result = run_action("inspect", params)

    if not result.ok:
        if output_json:
            print(json.dumps(result.data, sort_keys=True))
        else:
            from verdict import present

            present.header("Model inspect")
            present.fail(model_id, result.data.get("error", "unknown error"))
        raise SystemExit(result.exit_code)

    payload = result.data
    if output_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return

    from verdict import present

    present.header(f"Model inspect  /  {model_id}")
    present.kv(
        {
            "provider": payload["provider"],
            "tier": f"T{payload['tier']}",
            "context window": payload["context_window"] or "-",
            "cost per 1k": f"${payload['cost_per_1k']:.4f}" if payload["cost_per_1k"] else "-",
            "capabilities": ", ".join(payload["capabilities"]) or "-",
            "availability": payload["availability_state"],
        }
    )


def cmd_receipt(
    action: str,
    *,
    receipt_id: str | None = None,
    attempt_id: str | None = None,
    scope: str | None = None,
    db_path: str | None = None,
    output_json: bool = False,
) -> None:
    """Inspect durable RoutingReceiptV1 records from ReceiptStore."""
    from verdict.actions.registry import run_action

    result = run_action(
        "receipt.show",
        {
            "action": action,
            "receipt_id": receipt_id,
            "attempt_id": attempt_id,
            "scope": scope,
            "db_path": db_path,
        },
    )
    data = result.data
    if data.get("error"):
        raise SystemExit(data["error"])

    if action == "list":
        items = data["receipts"]
        if output_json:
            print(json.dumps({"receipts": items}, indent=2, sort_keys=True))
            return
        from verdict import present

        present.header("Routing receipts")
        if not items:
            present.note("no routing receipts found")
            return
        present.table(
            ["Receipt ID", "Scope", "Attempt", "State"],
            [
                (
                    item["receipt_id"],
                    item["scope"],
                    item.get("attempt_id") or "-",
                    item.get("state") or "-",
                )
                for item in items
            ],
        )
        return

    if action == "show":
        if data.get("status") == "missing":
            raise SystemExit("routing receipt not found")
        receipt = data["receipt"]
        summary = data["summary"]
        if output_json:
            print(json.dumps(receipt, indent=2, sort_keys=True))
            return
        from verdict import present

        present.header("Routing receipt")
        present.note(summary)
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return

    if action == "export":
        if data.get("status") == "missing":
            raise SystemExit("routing receipt not found")
        print(json.dumps(data["receipt"], indent=2, sort_keys=True))
        return

    raise SystemExit(f"unknown receipt action: {action}")


def cmd_replay(session_id: str, output_json: bool = False) -> None:
    """Replay a recorded execution session from the shared MemoryPlane."""
    from verdict.actions.registry import run_action

    result = run_action("replay", {"session_id": session_id})
    data = result.data
    status = data.get("status")
    if status == "unavailable":
        if output_json:
            print(json.dumps({"status": "unavailable", "message": data["message"]}, sort_keys=True))
        else:
            from verdict import present

            present.header("Replay session")
            present.warn("replay", data["message"])
        raise SystemExit(3)
    if status == "missing":
        if output_json:
            print(json.dumps({"status": "missing", "message": data["message"]}, sort_keys=True))
        else:
            from verdict import present

            present.header("Replay session")
            present.fail(session_id, "not found")
        raise SystemExit(1)
    record = data["record"]
    if output_json:
        print(json.dumps(record, indent=2, sort_keys=True))
        return
    from verdict import present

    present.header("Replay session")
    present.kv(
        {
            "Session ID": session_id,
            "State": record["state"],
            "Model": record["model_id"],
            "Steps": str(len(record["steps"])),
            "Completed": str(len(record["completed_steps"])),
            "Task": str(record["task_spec"]),
        }
    )


def cmd_simulate(
    task: str,
    criticality: str = "medium",
    *,
    model_override: str | None = None,
    output_json: bool = False,
    catalog: list[ModelInfo] | None = None,
    passports: dict[str, Any] | None = None,
) -> None:
    """Forecast tokens, cost, risk, and the expected model before any paid call."""
    from verdict.actions.registry import run_action

    params: dict[str, Any] = {
        "task": task,
        "criticality": criticality,
        "model_override": model_override,
    }
    if catalog is not None:
        params["catalog"] = catalog
    result = run_action("simulate", params)
    data = result.data
    if output_json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return
    from verdict import present

    present.header("Verdict pre-execution simulation")
    present.table(
        ["Metric", "Value"],
        [
            ("Model", f"{data['model']} ({data['provider']}, T{data['tier']})"),
            ("Prompt tokens", str(data["prompt_tokens"])),
            ("Completion tokens", str(data["completion_tokens"])),
            ("Total tokens", str(data["total_tokens"])),
            ("Est. cost", f"${data['cost_usd']:.6f}"),
            ("Risk score", f"{data['risk_score']} / 100"),
            ("Capacity confidence", f"{data['capacity_confidence']:.2f}"),
        ],
    )
    present.note(data["rationale"])


default_model_catalog = _action_helpers.default_model_catalog


def cmd_memory(args: Any) -> None:
    """Handle memory subcommands: put, search, export, import, masterdocs, graph."""
    from verdict.memory_bridge import configure_memory_bridge, detect_available_tools
    from verdict.memory_graph_adapter import CodeGraphAdapter
    from verdict.memory_masterdocs_adapter import MasterDocsAdapter
    from verdict.memory_plane import MemoryPlane, MemoryRecord

    db_path = getattr(args, "db_path", None) or str(Path.home() / ".verdict" / "memory.db")
    sub = getattr(args, "memory_command", None)
    if not (sub in {"docs", "masterdocs"} and getattr(args, "json", False)):
        from verdict import present

        present.header(f"Memory / {sub or 'help'}")

    if sub == "docs":
        from verdict.documentation_preflight import run_documentation_preflight

        docs_report = run_documentation_preflight(
            repo_root=Path(getattr(args, "repo_root", Path.cwd())),
            memory_path=Path(db_path),
            fix=getattr(args, "fix", False),
        )
        if getattr(args, "json", False):
            print(json.dumps(docs_report.to_dict(), indent=2, sort_keys=True))
        else:
            present.kv(docs_report.to_dict(), title="Documentation preflight")
            if not docs_report.passed:
                present.fail("documentation preflight", "failed")
        if not docs_report.passed:
            raise SystemExit(1)
        return

    plane = MemoryPlane(db_path)

    if sub == "put":
        rec = MemoryRecord(
            record_id=f"rec_{args.key}",
            namespace=getattr(args, "namespace", "default"),
            key=args.key,
            content=args.content,
            source=getattr(args, "source", "cli"),
        )
        plane.put(rec)
        present.ok("Memory record put", f"{rec.key} (ns: {rec.namespace})")
    elif sub == "search":
        results = plane.search(
            args.query, namespace=getattr(args, "namespace", None), limit=getattr(args, "limit", 10)
        )
        present.section(f"Found {len(results)} memory record(s):")
        present.table(
            ["Namespace", "Key", "Source", "Content"],
            [(r.namespace, r.key, r.source, r.content[:100]) for r in results],
        )
    elif sub == "export":
        from verdict.memory_adapters import ImportPolicy, export_manifest

        out = getattr(args, "output", "memory_manifest.json")
        destination = Path(out).expanduser().resolve()
        policy = ImportPolicy((destination.parent,))
        export_report = export_manifest(
            plane.export_records(),
            destination,
            policy=policy,
            source="memory-plane",
            adapter_id="local-manifest",
        )
        if export_report.status != "ok":
            raise SystemExit("memory manifest export failed: " + "; ".join(export_report.errors))
        present.ok("Exported memory manifest", f"to {destination}")
    elif sub == "import":
        from verdict.memory_adapters import ImportPolicy, import_manifest

        man = args.manifest
        source = Path(man).expanduser().resolve()
        policy = ImportPolicy((source.parent,))
        manifest_records, import_report = import_manifest(source, policy=policy)
        count = plane.import_records(manifest_records)
        present.ok(
            "Imported memory records",
            f"{count[0]} record(s) ({import_report.duplicates} duplicates; "
            f"manifest {import_report.manifest_hash})",
        )
    elif sub == "masterdocs":
        db = getattr(args, "db", "MasterDocsRAG.db")
        adapter = MasterDocsAdapter()
        result = adapter.canonicalize_db_records(
            db,
            allow_legacy_sqlite=args.allow_legacy_sqlite,
            limit=getattr(args, "limit", 1000),
            ingest_timestamp=getattr(args, "ingest_timestamp", None),
        )
        if result.report.status in {"unavailable", "rejected", "empty"}:
            payload = result.to_dict()
            if getattr(args, "json", False):
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                present.fail("MasterDocs import", str(result.report.status))
                present.kv(payload["report"])
            raise SystemExit(1)
        if getattr(args, "dry_run", False):
            payload = result.to_dict()
        else:
            imported_report = adapter.import_result(result, plane)
            payload = {
                "report": imported_report.to_dict(),
                "records": [dict(record) for record in result.records],
            }
            if imported_report.status in {"rejected", "partial"} and imported_report.ingested == 0:
                raise SystemExit(1)
        if getattr(args, "json", False):
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            present.ok("MasterDocs import", str(payload["report"].get("status", "ok")))
            present.kv(payload["report"])
        return
    elif sub == "graph":
        db = getattr(args, "db", "code_graph.db")
        graph_adapter = CodeGraphAdapter()
        graph_rep = graph_adapter.ingest_sqlite(
            db, plane, allow_legacy_sqlite=args.allow_legacy_sqlite
        )
        present.ok("Code graph ingested", f"{graph_rep.records_created} node(s)")
    elif sub == "setup":
        report = detect_available_tools()
        tools_to_config = getattr(args, "tools", None)
        if not tools_to_config:
            tools_to_config = list(report.preselected_tools)
        else:
            tools_to_config = [t.strip() for t in tools_to_config.split(",") if t.strip()]

        present.kv(
            {
                "Detected available AI tools": list(report.preselected_tools),
                "Configuring memory bridge for": tools_to_config,
            }
        )

        res = configure_memory_bridge(tools_to_config, plane)
        present.ok("Configured tools", str(res["configured_tools"]))
        present.ok("Memory database ready", str(res["memory_db_path"]))

    else:
        present.warn("Memory", "Use --help to view memory subcommands.")


def cmd_uninstall(purge_data: bool = False) -> None:
    """Reversibly uninstall memory bridge hooks and MCP registrations."""
    from verdict.memory_bridge import uninstall_memory_bridge

    res = uninstall_memory_bridge(home_dir=Path.home(), cwd=Path.cwd(), purge_data=purge_data)
    from verdict import present

    present.header("Uninstall memory bridge")
    targets = res["uninstalled_targets"]
    present.ok(
        "Uninstalled targets", ", ".join(map(str, targets)) if targets else "none were installed"
    )
    if purge_data:
        present.warn("Purged .verdict memory data directory.")


def cmd_runtime(
    operation: str,
    *,
    apply: bool = False,
    consent: bool = False,
    service_ids: list[str] | None = None,
    output_json: bool = False,
    manager: Any | None = None,
) -> None:
    """Inspect or explicitly reconcile canonical global runtime ownership."""
    from verdict.runtime_daemons import RuntimeManager, RuntimeManagerError

    manager = manager or RuntimeManager()
    try:
        if operation == "status":
            report = manager.status()
        elif operation == "reconcile":
            if apply:
                report = manager.reconcile_apply(
                    service_ids=service_ids or [spec.service_id for spec in manager.specs],
                    consent=consent,
                )
            else:
                report = manager.reconcile_plan()
        elif operation == "explain":
            from verdict.runtime_health import build_runtime_health_report

            report = build_runtime_health_report(manager.status())
        else:
            raise RuntimeManagerError(f"unsupported runtime operation: {operation}")
    except RuntimeManagerError as exc:
        payload = {"operation": "runtime", "status": "blocked", "errors": [str(exc)]}
        if output_json:
            print(json.dumps(payload, sort_keys=True))
        else:
            from verdict import present

            present.header("Runtime")
            present.fail("Runtime operation blocked", str(exc))
        raise SystemExit(2) from exc

    if output_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        from verdict import present

        data = report.to_dict()
        present.header(f"Runtime  /  {operation}")
        present.kv(
            {key: value for key, value in data.items() if not isinstance(value, (dict, list))}
        )
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                present.note(f"{key}: {json.dumps(value, sort_keys=True)}")
        present.status("runtime", "ok" if report.passed else "failed")
    if operation == "explain":
        return
    if not report.passed:
        raise SystemExit(1)


def cmd_check() -> None:
    """Validate the Verdict configuration file and print status.

    Delegates the validation to the ``check`` action; the CLI renders the
    result via ``present.*`` (behaviour on ``origin/main`` is the contract).
    """
    from verdict import present
    from verdict.actions.registry import run_action

    result = run_action("check")
    data = result.data
    config_path = str(data.get("config_path", ""))
    status = str(data.get("status", ""))
    issues = list(data.get("issues", []))

    present.header("Configuration check")

    if status == "missing":
        present.fail("Configuration file (verdict.yaml) is missing", f"at {config_path}.")
        sys.exit(1)
    if status == "invalid":
        present.fail("Configuration file is corrupted/invalid YAML", str(data.get("error", "")))
        sys.exit(1)

    primary_model = data.get("primary_model")
    if not primary_model:
        present.fail("No primary model configured in verdict.yaml.")
    else:
        from verdict.classifier import classify

        tier = classify(str(primary_model))
        present.ok("Configured Primary Model", f"{primary_model} (Tier-{tier})")

    for issue in issues:
        if issue == "no_primary_model":
            continue  # already emitted above
        if issue == "providers_not_dict":
            present.fail("'providers' section in verdict.yaml is malformed.")
        elif issue.startswith("provider_not_dict:"):
            _, provider = issue.split(":", 1)
            present.fail(f"Provider '{provider}' config is not a dictionary.")
        elif issue.startswith("api_key_in_url:"):
            _, provider = issue.split(":", 1)
            present.fail(f"Literal API key detected inside host URL for provider '{provider}'.")
        elif issue.startswith("duplicate_host:"):
            _, provider, other = issue.split(":", 2)
            present.fail(
                "Duplicate host URL configured in verdict.yaml",
                f"provider '{provider}' and '{other}' have identical hosts.",
            )

    if issues:
        present.fail("Config validation failed with issues.")
        sys.exit(1)

    present.ok("Configuration file is valid.")


def cmd_compat(compat_command: str | None, declared: str | None, output_json: bool) -> None:
    """Publish or check the cross-repo contract compatibility manifest (ADR-024, CON-001).

    Fails closed: a missing declaration or a hash mismatch against the current
    verdict-core contracts blocks (exit 1) rather than assuming compatibility.
    """
    from verdict.actions.registry import run_action

    if compat_command == "manifest":
        result = run_action("compat.manifest")
        data = result.data
        if output_json:
            print(json.dumps(data, indent=2, sort_keys=True))
        else:
            from verdict import present

            present.header("Compatibility manifest")
            present.kv({"schema": data["schema_version"], "manifest hash": data["manifest_hash"]})
            present.table(["Contract", "Digest"], sorted(data.get("contracts", {}).items()))
        if result.exit_code:
            sys.exit(result.exit_code)
        return

    if compat_command == "check":
        result = run_action("compat.check", {"declared": declared})
        data = result.data
        if result.ok:
            if output_json:
                print(json.dumps({"allowed": True, "reason": None}, indent=2))
            else:
                from verdict import present

                present.header("Compatibility check")
                present.ok("compatibility", "matches the current verdict-core contracts")
            return

        reason = str(data.get("reason", "unknown"))
        mismatched = list(data.get("mismatched_contracts") or [])
        if output_json:
            payload: dict[str, Any] = {"allowed": False, "reason": reason}
            if mismatched:
                payload["mismatched_contracts"] = mismatched
            print(json.dumps(payload, indent=2))
        else:
            from verdict import present

            present.header("Compatibility check")
            if not mismatched:
                # File / JSON errors follow the historical "failing closed" prefix.
                present.fail("compatibility", f"{reason} (failing closed)")
            else:
                present.fail("compatibility", reason)
                present.table(["Mismatched contract"], [(n,) for n in mismatched])
        sys.exit(result.exit_code or 1)

    from verdict import present

    present.fail("compat", "unknown subcommand; use 'manifest' or 'check'")
    sys.exit(1)


def cmd_hook(args: Any) -> None:
    """Manage Verdict lifecycle hooks for Codex and Claude Code."""

    from verdict.memory_bridge import configure_memory_bridge
    from verdict.memory_gate import MemoryGate, MemoryWriteRequest
    from verdict.memory_plane import MemoryPlane

    hook_cmd = getattr(args, "hook_command", None)
    if hook_cmd != "claude-gate" and not (
        hook_cmd in {"recall", "configure", "status"} and getattr(args, "json", False)
    ):
        from verdict import present

        present.header(f"Hook / {hook_cmd or 'help'}")
    if hook_cmd == "claude-gate":
        base_url = getattr(args, "base_url", "http://127.0.0.1:20128")
        try:
            cmd_catalog(
                base_url=base_url,
                management=True,
                expected_rows=0,
                freshness_seconds=3600,
                db_path=None,
                probe=False,
                probe_limit=1,
                probe_timeout=1.0,
                output_json=True,
            )
        except SystemExit as exc:
            if exc.code not in (0, None):
                print(
                    "Verdict blocked: catalog not qualified. Unknown is not healthy.",
                    file=sys.stderr,
                )
                raise SystemExit(2) from exc
        return

    db_path = getattr(args, "db_path", None) or str(Path.home() / ".verdict" / "memory.db")
    plane = MemoryPlane(db_path)
    gate = MemoryGate(plane)

    if hook_cmd == "recall":
        query = getattr(args, "query", "")
        limit = getattr(args, "limit", 5)
        results = plane.search(query, limit=limit)
        if getattr(args, "json", False):
            print(json.dumps([r.to_dict() for r in results], indent=2))
        else:
            present.section(f"Recall: {len(results)} record(s)")
            present.table(
                ["Namespace", "Key", "Source", "Content"],
                [(r.namespace, r.key, r.source, r.content[:120]) for r in results],
            )

    elif hook_cmd == "record":
        key = getattr(args, "key", "session")
        value = getattr(args, "value", "")
        namespace = getattr(args, "namespace", "sessions")
        source = getattr(args, "source", "cli")
        req = MemoryWriteRequest(
            namespace=namespace, key=key, value=value, source=source, authority="agent"
        )
        write_res = gate.write(req)
        if write_res.allowed:
            present.ok("Recorded", f"[{namespace}:{key}]")
        else:
            present.fail(f"Rejected [{namespace}:{key}]", str(write_res.reason))

    elif hook_cmd == "configure":
        tools_str = getattr(args, "tools", None)
        tools = [t.strip() for t in tools_str.split(",")] if tools_str else ["codex", "claude"]
        res = configure_memory_bridge(selected_tools=tools)
        if getattr(args, "json", False):
            print(json.dumps(res, indent=2))
        else:
            present.ok("Memory bridge configured.")
            present.kv({"DB": res["memory_db_path"], "Targets": ", ".join(res["configured_tools"])})

    elif hook_cmd == "status":
        codex_agents = Path.home() / ".codex" / "AGENTS.md"
        claude_md = Path.cwd() / "CLAUDE.md"
        mcp_file = Path.cwd() / ".mcp.json"
        status = {
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
        if getattr(args, "json", False):
            print(json.dumps(status, indent=2))
        else:
            for k, v in status.items():
                present.status(k.replace("_", " "), "ok" if v else "missing")


def cmd_mcp(args: Any) -> None:
    """Manage and run Model Context Protocol (MCP) server."""
    mcp_cmd = getattr(args, "mcp_command", None)
    if mcp_cmd == "serve":
        from verdict.mcp_server import main as run_mcp

        run_mcp()
    elif mcp_cmd == "init":
        from verdict.memory_bridge import configure_memory_bridge

        res = configure_memory_bridge(selected_tools=["mcp", "codex", "claude"])
        if getattr(args, "json", False):
            print(json.dumps(res, indent=2))
        else:
            from verdict import present

            present.header("MCP / init")
            present.ok("Verdict MCP server initialized across tool environments.")
            present.kv({"Memory DB": res["memory_db_path"]})
    elif mcp_cmd == "status":
        mcp_file = Path.cwd() / ".mcp.json"
        registered = False
        if mcp_file.exists():
            try:
                data = json.loads(mcp_file.read_text("utf-8"))
                servers = data.get("mcpServers", {})
                registered = "verdict-memory" in servers or "verdict-core" in servers
            except Exception:
                pass
        status_info = {"mcp_registered": registered, "mcp_config": str(mcp_file)}
        if getattr(args, "json", False):
            print(json.dumps(status_info, indent=2))
        else:
            from verdict import present

            present.header("MCP / status")
            if registered:
                present.ok("Verdict MCP server", "is registered in .mcp.json")
            else:
                present.warn("Verdict MCP server", "is not registered in .mcp.json")


def _stdout_is_tty() -> bool:
    return sys.stdout.isatty()


def cmd_credentials_list(*, output_json: bool = False) -> None:
    """List all registered credentials with their source and masked value."""
    from verdict.actions.registry import run_action

    result = run_action("credentials.list")
    if output_json:
        print(json.dumps(result.data, indent=2))
        return

    # Terminal output
    from verdict.terminal_ui import TerminalUI

    ui = TerminalUI()

    ui.header("Credentials")
    for item in result.data:
        ui.status(str(item["name"]), str(item["source"]), str(item["value"]))


def cmd_credentials_set(
    *, name: str, force_unregistered: bool = False, from_stdin: bool = False
) -> None:
    """Set a credential in the store."""
    import getpass

    from verdict.actions.registry import run_action

    # Read value (I/O must happen before the action call)
    if from_stdin:
        value = sys.stdin.read().strip()
    else:
        prompt_text = f"Enter value for {name}: "
        value = getpass.getpass(prompt_text)

    result = run_action(
        "credentials.set", {"name": name, "value": value, "force_unregistered": force_unregistered}
    )
    from verdict.terminal_ui import TerminalUI

    ui = TerminalUI()
    if not result.ok:
        ui.panel("Credential error", str(result.data.get("error", "")), tone="WARNING")
        raise SystemExit(result.exit_code)
    ui.status(name, "set", "in credential store")


def cmd_credentials_unset(*, name: str) -> None:
    """Remove a credential from the store."""
    from verdict.actions.registry import run_action
    from verdict.terminal_ui import TerminalUI

    result = run_action("credentials.unset", {"name": name})
    ui = TerminalUI()
    if result.data.get("status") == "removed":
        ui.status(name, "removed", "from credential store")
    else:
        ui.panel("Not found", f"{name} was not in the credential store.", tone="WARNING")


def cmd_credentials_test(*, name: str) -> None:
    """Test a credential with its live check.

    Delegates the live check to the ``credentials.test`` action; the CLI only
    renders the ``ActionResult`` via ``TerminalUI`` (parity with ``origin/main``).
    """
    from verdict.actions.registry import run_action
    from verdict.terminal_ui import TerminalUI

    ui = TerminalUI()
    result = run_action("credentials.test", {"name": name})
    data = result.data
    status = str(data.get("status", ""))
    message = str(data.get("message", ""))

    if status == "unknown":
        ui.panel("Unknown credential", f"{name} is not in the registry.", tone="WARNING")
        raise SystemExit(result.exit_code or 1)
    if status == "no_check":
        ui.panel("No live check", f"{name} has no live check defined.", tone="INFO")
        return
    if status == "missing":
        ui.panel("Missing", f"{name} is not set in env or store.", tone="WARNING")
        raise SystemExit(result.exit_code or 1)
    if status == "error":
        ui.panel("Check error", message, tone="ERROR")
        raise SystemExit(result.exit_code or 1)

    # ok / failed
    if result.ok:
        # Preserve origin/main sequencing: a "testing" line then the result.
        ui.status(name, "testing", "...")
        ui.status(name, "ok", message)
        return
    ui.status(name, "testing", "...")
    ui.status(name, "failed", message)
    raise SystemExit(result.exit_code or 1)


def main() -> None:
    # Load credentials from store early (exported env vars win)
    from verdict.credentials_store import CredentialsStore

    try:
        CredentialsStore().load_into_env()
    except PermissionError as e:
        import sys

        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Verdict: policy-gated LLM Router")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    from verdict.commands import (
        parsers_autodev,
        parsers_credentials,
        parsers_harness,
        parsers_models,
        parsers_openspec,
        parsers_routing,
        parsers_runtime,
        parsers_setup,
    )

    for registrar in (
        parsers_setup,
        parsers_credentials,
        parsers_routing,
        parsers_autodev,
        parsers_harness,
        parsers_runtime,
        parsers_models,
        parsers_openspec,
    ):
        registrar.register(subparsers)

    from verdict.commands.dispatch import dispatch

    dispatch(parser, parser.parse_args())


def cmd_resume(
    story: str,
    *,
    with_harness: str | None = None,
    output_json: bool = False,
    repo: Path | str | None = None,
    create_if_missing: bool = False,
) -> dict[str, Any]:
    """Reconstruct durable resume state for a Linear story (cross-harness resume context).

    Canonical sources: Git worktree/branch/SHA, ``.verdict/handoff.md``, optional
    ``gh`` PR discovery. Does not read proprietary chat history. ``--with`` records
    a launcher stub only.
    """
    from verdict.actions.registry import run_action

    target = Path(repo) if repo is not None else Path.cwd()
    result = run_action(
        "resume",
        {
            "story": story,
            "repo": target,
            "with_harness": with_harness,
            "create_if_missing": create_if_missing,
        },
    )
    if not result.ok:
        err = result.data.get("error", "resume failed")
        if output_json:
            print(json.dumps({"error": err, "story": story}, sort_keys=True))
        else:
            from verdict import present

            present.header("Resume")
            present.fail("resume", err)
        raise SystemExit(result.exit_code or 1)
    payload: dict[str, Any] = dict(result.data)

    if output_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return payload

    from verdict import present

    present.header(f"Resume  /  {payload['story_id']}")
    rows: dict[str, Any] = {
        "worktree": payload["worktree"],
        "branch": payload.get("branch"),
        "HEAD": payload.get("head_sha"),
        "dirty": payload.get("dirty"),
        "reattach": payload.get("reattach"),
        "previous": payload.get("previous_worker"),
    }
    if payload.get("pr_url"):
        rows["pr"] = f"{payload['pr_url']} ({payload.get('pr_state')})"
    present.kv(rows)
    if with_harness:
        present.warn("launcher", f"{with_harness} (stub — not executed)")
    present.section("Resume prompt")
    # The prompt body is meant for copy/paste into a harness; keep it raw.
    print(payload["resume_prompt"])
    return payload


def cmd_harness_codex(
    command: str,
    *,
    base_url: str = CODEX_HARNESS_DEFAULT_BASE_URL,
    token_env: str = CODEX_HARNESS_DEFAULT_TOKEN_ENV,
    force: bool = False,
) -> None:
    """Enable, disable, or inspect Codex as a Verdict OpenAI-compatible client."""
    from verdict.harness_codex import HarnessCodexError, disable, enable, status

    try:
        if command == "enable":
            result = enable(base_url=base_url, token_env=token_env, force=force)
            from verdict import present

            present.header("Codex harness")
            present.ok("Codex harness enabled")
            present.kv(
                {
                    "provider": "verdict",
                    "base URL": result.base_url,
                    "token environment": result.token_env,
                    "backup" if result.created_backup else "config": (
                        result.backup_path if result.created_backup else result.config_path
                    ),
                }
            )
            return
        if command == "disable":
            disable()
            from verdict import present

            present.header("Codex harness")
            present.ok("Codex harness disabled")
            present.note("restored pre-enable ~/.codex/config.toml backup")
            return
        if command == "status":
            report = status()
            state = (
                "enabled"
                if report.enabled
                else "configured"
                if report.config_exists
                else "not configured"
            )
            from verdict import present

            present.header("Codex harness")
            present.status("Codex harness", "ok" if report.enabled else "warning", state)
            present.kv(
                {
                    "provider": report.provider or "(none)",
                    "base URL": report.base_url or "(none)",
                    "token environment": f"{report.token_env} (set: {'yes' if report.token_env_set else 'no'})",
                    "config": report.config_path,
                }
            )
            return
    except HarnessCodexError as exc:
        from verdict import present

        present.header("Codex harness")
        present.fail("Codex harness", str(exc))
        raise SystemExit(1) from exc
    raise SystemExit(f"unknown harness codex command: {command}")


def cmd_harness_hermes(
    command: str,
    *,
    base_url: str = HERMES_HARNESS_DEFAULT_BASE_URL,
    token_env: str = HERMES_HARNESS_DEFAULT_TOKEN_ENV,
    model: str = HERMES_HARNESS_DEFAULT_MODEL,
    force: bool = False,
) -> None:
    """Enable, disable, or inspect Hermes as a Verdict OpenAI-compatible client."""
    from verdict.harness_hermes import HarnessHermesError, disable, enable, status

    try:
        if command == "enable":
            result = enable(base_url=base_url, token_env=token_env, model=model, force=force)
            from verdict import present

            present.header("Hermes harness")
            present.ok("Hermes harness enabled")
            present.kv(
                {
                    "provider": "Verdict",
                    "base URL": result.base_url,
                    "model": result.model,
                    "token environment": result.token_env,
                    "backup" if result.created_backup else "config": (
                        result.backup_path if result.created_backup else result.config_path
                    ),
                }
            )
            return
        if command == "disable":
            disable()
            from verdict import present

            present.header("Hermes harness")
            present.ok("Hermes harness disabled")
            present.note("restored pre-enable ~/.hermes/config.yaml backup")
            return
        if command == "status":
            report = status()
            state = (
                "enabled"
                if report.enabled
                else "configured"
                if report.config_exists
                else "not configured"
            )
            from verdict import present

            present.header("Hermes harness")
            present.status("Hermes harness", "ok" if report.enabled else "warning", state)
            present.kv(
                {
                    "provider": report.provider or "(none)",
                    "base URL": report.base_url or "(none)",
                    "model": report.model or "(none)",
                    "token environment": f"{report.token_env} (set: {'yes' if report.token_env_set else 'no'})",
                    "config": report.config_path,
                }
            )
            return
    except HarnessHermesError as exc:
        from verdict import present

        present.header("Hermes harness")
        present.fail("Hermes harness", str(exc))
        raise SystemExit(1) from exc
    raise SystemExit(f"unknown harness hermes command: {command}")


def cmd_harness_claude(
    command: str,
    *,
    base_url: str = CLAUDE_HARNESS_DEFAULT_BASE_URL,
    token_env: str = CLAUDE_HARNESS_DEFAULT_TOKEN_ENV,
    force: bool = False,
) -> None:
    """Discover, enable, disable, status, or certify Claude Code → Verdict."""
    from verdict.harness_claude import (
        HarnessClaudeError,
        certify,
        disable,
        discover,
        enable,
        status,
    )

    try:
        if command == "discover":
            discovery = discover()
            from verdict import present

            present.header("Claude Code harness")
            present.status(
                "Claude Code installation",
                "found" if discovery.installed else "missing",
                discovery.binary_path or "(none)",
            )
            present.kv(
                {
                    "config": f"{discovery.config_path} (exists: {'yes' if discovery.config_exists else 'no'})",
                    "managed by Verdict": "yes" if discovery.managed_by_verdict else "no",
                    "base URL": discovery.base_url or "(none)",
                    "pointing at Verdict": "yes" if discovery.pointing_at_verdict else "no",
                    "pointing at OmniRoute": "yes" if discovery.pointing_at_omniroute else "no",
                    "gate hook": "yes" if discovery.gate_hook_present else "no",
                }
            )
            return
        if command == "enable":
            result = enable(base_url=base_url, token_env=token_env, force=force)
            from verdict import present

            present.header("Claude Code harness")
            present.ok("Claude Code harness enabled")
            present.kv(
                {
                    "integration": result.integration,
                    "base URL": result.base_url,
                    "token environment": result.token_env,
                    "backup" if result.created_backup else "config": (
                        result.backup_path if result.created_backup else result.config_path
                    ),
                }
            )
            return
        if command == "disable":
            disable()
            from verdict import present

            present.header("Claude Code harness")
            present.ok("Claude Code harness disabled")
            present.note("restored pre-enable ~/.claude/settings.json backup")
            return
        if command == "status":
            status_report = status()
            state = (
                "enabled"
                if status_report.enabled
                else "configured"
                if status_report.config_exists
                else "not configured"
            )
            from verdict import present

            present.header("Claude Code harness")
            present.status(
                "Claude Code harness", "ok" if status_report.enabled else "warning", state
            )
            present.kv(
                {
                    "provider": status_report.provider or "(none)",
                    "base URL": status_report.base_url or "(none)",
                    "integration": status_report.integration or "(none)",
                    "token environment": f"{status_report.token_env} (set: {'yes' if status_report.token_env_set else 'no'})",
                    "config": status_report.config_path,
                    "gate hook": "yes" if status_report.gate_hook_present else "no",
                }
            )
            return
        if command == "certify":
            certification = certify(force=force)
            from verdict import present

            present.header("Claude Code harness")
            present.status(
                "Claude Code certification",
                certification.overall,
                "healthy" if certification.healthy else "not healthy",
            )
            present.kv(
                {
                    "base URL": certification.base_url or "(none)",
                    "token environment set": "yes" if certification.token_env_set else "no",
                }
            )
            present.table(["Facet", "Level"], sorted(certification.facets.items()), title="Facets")
            if certification.notes:
                present.section("Notes")
                for item in certification.notes:
                    present.note(item)
            if certification.needs_owner:
                present.section("Needs owner")
                for item in certification.needs_owner:
                    present.note(item)
            return
    except HarnessClaudeError as exc:
        from verdict import present

        present.header("Claude Code harness")
        present.fail("Claude Code harness", str(exc))
        raise SystemExit(1) from exc
    raise SystemExit(f"unknown harness claude command: {command}")


def cmd_harness_cursor(
    command: str,
    *,
    base_url: str = CURSOR_HARNESS_DEFAULT_BASE_URL,
    token_env: str = CURSOR_HARNESS_DEFAULT_TOKEN_ENV,
    force: bool = False,
    wrapper: bool = False,
) -> None:
    """Discover, enable, disable, status, or certify Cursor → Verdict."""
    from verdict.harness_cursor import (
        HarnessCursorError,
        certify,
        disable,
        discover,
        enable,
        status,
    )

    try:
        if command == "discover":
            discovery = discover()
            from verdict import present

            present.header("Cursor harness")
            present.status(
                "Cursor installation",
                "found" if discovery.installed else "missing",
                discovery.binary_path or "(none)",
            )
            present.kv(
                {
                    "config": f"{discovery.config_path} (exists: {'yes' if discovery.config_exists else 'no'})",
                    "managed by Verdict": "yes" if discovery.managed_by_verdict else "no",
                    "base URL": discovery.base_url or "(none)",
                    "pointing at Verdict": "yes" if discovery.pointing_at_verdict else "no",
                    "pointing at OmniRoute": "yes" if discovery.pointing_at_omniroute else "no",
                    "settings": discovery.settings_path or "(none)",
                    "wrapper": discovery.wrapper_path or "(none)",
                }
            )
            return
        if command == "enable":
            result = enable(base_url=base_url, token_env=token_env, force=force, wrapper=wrapper)
            from verdict import present

            present.header("Cursor harness")
            present.ok("Cursor harness enabled")
            present.kv(
                {
                    "integration": result.integration,
                    "base URL": result.base_url,
                    "token environment": result.token_env,
                    "wrapper": result.wrapper_path or "(none)",
                    "backup" if result.created_backup else "config": (
                        result.backup_path if result.created_backup else result.config_path
                    ),
                }
            )
            return
        if command == "disable":
            disable()
            from verdict import present

            present.header("Cursor harness")
            present.ok("Cursor harness disabled")
            present.note("restored pre-enable Cursor provider/settings/wrapper backups")
            return
        if command == "status":
            status_report = status()
            state = (
                "enabled"
                if status_report.enabled
                else "configured"
                if status_report.config_exists
                else "not configured"
            )
            from verdict import present

            present.header("Cursor harness")
            present.status("Cursor harness", "ok" if status_report.enabled else "warning", state)
            present.kv(
                {
                    "provider": status_report.provider or "(none)",
                    "base URL": status_report.base_url or "(none)",
                    "integration": status_report.integration or "(none)",
                    "token environment": f"{status_report.token_env} (set: {'yes' if status_report.token_env_set else 'no'})",
                    "config": status_report.config_path,
                    "wrapper": "yes" if status_report.wrapper_present else "no",
                }
            )
            return
        if command == "certify":
            certification = certify(force=force)
            from verdict import present

            present.header("Cursor harness")
            present.status(
                "Cursor certification",
                certification.overall,
                "healthy" if certification.healthy else "not healthy",
            )
            present.kv(
                {
                    "base URL": certification.base_url or "(none)",
                    "token environment set": "yes" if certification.token_env_set else "no",
                }
            )
            present.table(["Facet", "Level"], sorted(certification.facets.items()), title="Facets")
            if certification.notes:
                present.section("Notes")
                for item in certification.notes:
                    present.note(item)
            if certification.needs_owner:
                present.section("Needs owner")
                for item in certification.needs_owner:
                    present.note(item)
            return
    except HarnessCursorError as exc:
        from verdict import present

        present.header("Cursor harness")
        present.fail("Cursor harness", str(exc))
        raise SystemExit(1) from exc
    raise SystemExit(f"unknown harness cursor command: {command}")


def cmd_harness_prime(
    command: str,
    *,
    base_url: str = PRIME_HARNESS_DEFAULT_BASE_URL,
    token_env: str = PRIME_HARNESS_DEFAULT_TOKEN_ENV,
    force: bool = False,
) -> None:
    """Discover, enable, disable, status, or certify Prime Agent → Verdict."""
    from verdict.harness_prime import HarnessPrimeError, certify, disable, discover, enable, status

    try:
        if command == "discover":
            discovery = discover()
            from verdict import present

            present.header("Prime Agent harness")
            present.status(
                "Prime Agent installation",
                "found" if discovery.installed else "missing",
                discovery.binary_path or "(none)",
            )
            present.kv(
                {
                    "config": f"{discovery.config_path} (exists: {'yes' if discovery.config_exists else 'no'})",
                    "managed by Verdict": "yes" if discovery.managed_by_verdict else "no",
                    "base URL": discovery.base_url or "(none)",
                    "pointing at Verdict": "yes" if discovery.pointing_at_verdict else "no",
                    "pointing at OmniRoute": "yes" if discovery.pointing_at_omniroute else "no",
                }
            )
            return
        if command == "enable":
            result = enable(base_url=base_url, token_env=token_env, force=force)
            from verdict import present

            present.header("Prime Agent harness")
            present.ok("Prime Agent harness enabled")
            present.kv(
                {
                    "integration": result.integration,
                    "base URL": result.base_url,
                    "token environment": result.token_env,
                    "backup" if result.created_backup else "config": (
                        result.backup_path if result.created_backup else result.config_path
                    ),
                }
            )
            return
        if command == "disable":
            disable()
            from verdict import present

            present.header("Prime Agent harness")
            present.ok("Prime Agent harness disabled")
            present.note("restored pre-enable ~/.prime/agent/models.json backup")
            return
        if command == "status":
            status_report = status()
            state = (
                "enabled"
                if status_report.enabled
                else "configured"
                if status_report.config_exists
                else "not configured"
            )
            from verdict import present

            present.header("Prime Agent harness")
            present.status(
                "Prime Agent harness", "ok" if status_report.enabled else "warning", state
            )
            present.kv(
                {
                    "provider": status_report.provider or "(none)",
                    "base URL": status_report.base_url or "(none)",
                    "integration": status_report.integration or "(none)",
                    "token environment": f"{status_report.token_env} (set: {'yes' if status_report.token_env_set else 'no'})",
                    "config": status_report.config_path,
                }
            )
            return
        if command == "certify":
            certification = certify(force=force)
            from verdict import present

            present.header("Prime Agent harness")
            present.status(
                "Prime Agent certification",
                certification.overall,
                "healthy" if certification.healthy else "not healthy",
            )
            present.kv(
                {
                    "base URL": certification.base_url or "(none)",
                    "token environment set": "yes" if certification.token_env_set else "no",
                }
            )
            present.table(["Facet", "Level"], sorted(certification.facets.items()), title="Facets")
            if certification.notes:
                present.section("Notes")
                for item in certification.notes:
                    present.note(item)
            if certification.needs_owner:
                present.section("Needs owner")
                for item in certification.needs_owner:
                    present.note(item)
            return
    except HarnessPrimeError as exc:
        from verdict import present

        present.header("Prime Agent harness")
        present.fail("Prime Agent harness", str(exc))
        raise SystemExit(1) from exc
    raise SystemExit(f"unknown harness prime command: {command}")


def cmd_harness_opencode(
    command: str,
    *,
    base_url: str = OPENCODE_HARNESS_DEFAULT_BASE_URL,
    token_env: str = OPENCODE_HARNESS_DEFAULT_TOKEN_ENV,
    force: bool = False,
) -> None:
    """Discover, enable, disable, status, or certify OpenCode → Verdict."""
    from verdict.harness_opencode import (
        HarnessOpenCodeError,
        certify,
        disable,
        discover,
        enable,
        status,
    )

    try:
        if command == "discover":
            discovery = discover()
            from verdict import present

            present.header("OpenCode harness")
            present.status(
                "OpenCode installation",
                "found" if discovery.installed else "missing",
                discovery.binary_path or "(none)",
            )
            present.kv(
                {
                    "config": f"{discovery.config_path} (exists: {'yes' if discovery.config_exists else 'no'})",
                    "managed by Verdict": "yes" if discovery.managed_by_verdict else "no",
                    "base URL": discovery.base_url or "(none)",
                    "pointing at Verdict": "yes" if discovery.pointing_at_verdict else "no",
                    "pointing at OmniRoute": "yes" if discovery.pointing_at_omniroute else "no",
                }
            )
            return
        if command == "enable":
            result = enable(base_url=base_url, token_env=token_env, force=force)
            from verdict import present

            present.header("OpenCode harness")
            present.ok("OpenCode harness enabled")
            present.kv(
                {
                    "integration": result.integration,
                    "base URL": result.base_url,
                    "token environment": result.token_env,
                    "model": result.model,
                    "backup" if result.created_backup else "config": (
                        result.backup_path if result.created_backup else result.config_path
                    ),
                }
            )
            return
        if command == "disable":
            disable()
            from verdict import present

            present.header("OpenCode harness")
            present.ok("OpenCode harness disabled")
            present.note("restored pre-enable ~/.config/opencode/opencode.json backup")
            return
        if command == "status":
            status_report = status()
            state = (
                "enabled"
                if status_report.enabled
                else "configured"
                if status_report.config_exists
                else "not configured"
            )
            from verdict import present

            present.header("OpenCode harness")
            present.status("OpenCode harness", "ok" if status_report.enabled else "warning", state)
            present.kv(
                {
                    "provider": status_report.provider or "(none)",
                    "base URL": status_report.base_url or "(none)",
                    "integration": status_report.integration or "(none)",
                    "token environment": f"{status_report.token_env} (set: {'yes' if status_report.token_env_set else 'no'})",
                    "config": status_report.config_path,
                    "model": status_report.model or "(none)",
                }
            )
            return
        if command == "certify":
            certification = certify(force=force)
            from verdict import present

            present.header("OpenCode harness")
            present.status(
                "OpenCode certification",
                certification.overall,
                "healthy" if certification.healthy else "not healthy",
            )
            present.kv(
                {
                    "base URL": certification.base_url or "(none)",
                    "token environment set": "yes" if certification.token_env_set else "no",
                }
            )
            present.table(["Facet", "Level"], sorted(certification.facets.items()), title="Facets")
            if certification.notes:
                present.section("Notes")
                for item in certification.notes:
                    present.note(item)
            if certification.needs_owner:
                present.section("Needs owner")
                for item in certification.needs_owner:
                    present.note(item)
            return
    except HarnessOpenCodeError as exc:
        from verdict import present

        present.header("OpenCode harness")
        present.fail("OpenCode harness", str(exc))
        raise SystemExit(1) from exc
    raise SystemExit(f"unknown harness opencode command: {command}")


def cmd_harness_cline(
    command: str,
    *,
    base_url: str = CLINE_HARNESS_DEFAULT_BASE_URL,
    token_env: str = CLINE_HARNESS_DEFAULT_TOKEN_ENV,
    force: bool = False,
) -> None:
    """Discover, enable, disable, status, or certify Cline → Verdict."""
    from verdict.harness_cline import HarnessClineError, certify, disable, discover, enable, status

    try:
        if command == "discover":
            discovery = discover()
            from verdict import present

            present.header("Cline harness")
            present.status(
                "Cline installation",
                "found" if discovery.installed else "missing",
                discovery.binary_path or "(none)",
            )
            present.kv(
                {
                    "config": f"{discovery.config_path} (exists: {'yes' if discovery.config_exists else 'no'})",
                    "managed by Verdict": "yes" if discovery.managed_by_verdict else "no",
                    "base URL": discovery.base_url or "(none)",
                    "pointing at Verdict": "yes" if discovery.pointing_at_verdict else "no",
                    "pointing at OmniRoute": "yes" if discovery.pointing_at_omniroute else "no",
                    "CLI home": "yes" if discovery.cli_home_present else "no",
                    "settings": discovery.settings_path or "(none)",
                    "providers JSON": discovery.providers_json_path or "(none)",
                }
            )
            return
        if command == "enable":
            result = enable(base_url=base_url, token_env=token_env, force=force)
            from verdict import present

            present.header("Cline harness")
            present.ok("Cline harness enabled")
            present.kv(
                {
                    "integration": result.integration,
                    "base URL": result.base_url,
                    "token environment": result.token_env,
                    "providers JSON": result.providers_json_path or "(none)",
                    "settings": result.settings_path or "(none)",
                    "backup" if result.created_backup else "config": (
                        result.backup_path if result.created_backup else result.config_path
                    ),
                }
            )
            if result.ui_steps:
                present.section("Manual UI steps")
                for step in result.ui_steps:
                    present.note(step)
            return
        if command == "disable":
            disable()
            from verdict import present

            present.header("Cline harness")
            present.ok("Cline harness disabled")
            present.note("restored pre-enable Cline provider/settings/providers.json backups")
            return
        if command == "status":
            status_report = status()
            state = (
                "enabled"
                if status_report.enabled
                else "configured"
                if status_report.config_exists
                else "not configured"
            )
            from verdict import present

            present.header("Cline harness")
            present.status("Cline harness", "ok" if status_report.enabled else "warning", state)
            present.kv(
                {
                    "provider": status_report.provider or "(none)",
                    "base URL": status_report.base_url or "(none)",
                    "integration": status_report.integration or "(none)",
                    "token environment": f"{status_report.token_env} (set: {'yes' if status_report.token_env_set else 'no'})",
                    "config": status_report.config_path,
                    "installed": "yes" if status_report.installed else "no",
                    "binary": status_report.binary_path or "(none)",
                }
            )
            return
        if command == "certify":
            certification = certify(force=force)
            from verdict import present

            present.header("Cline harness")
            present.status(
                "Cline certification",
                certification.overall,
                "healthy" if certification.healthy else "not healthy",
            )
            present.kv(
                {
                    "base URL": certification.base_url or "(none)",
                    "token environment set": "yes" if certification.token_env_set else "no",
                }
            )
            present.table(["Facet", "Level"], sorted(certification.facets.items()), title="Facets")
            if certification.notes:
                present.section("Notes")
                for item in certification.notes:
                    present.note(item)
            if certification.needs_owner:
                present.section("Needs owner")
                for item in certification.needs_owner:
                    present.note(item)
            return
    except HarnessClineError as exc:
        from verdict import present

        present.header("Cline harness")
        present.fail("Cline harness", str(exc))
        raise SystemExit(1) from exc
    raise SystemExit(f"unknown harness cline command: {command}")


def cmd_prove_at_rest(
    prove_command: str,
    *,
    base_url: str | None = None,
    state_path: str | None = None,
    interval: float = 300.0,
    timeout: float = 15.0,
    allow_live_probe: bool = False,
    output_json: bool = False,
) -> None:
    """Run or inspect the free∩active prove-at-rest daemon."""
    from verdict.prove_at_rest import (
        ProveAtRestError,
        ProveAtRestStore,
        build_live_daemon,
        default_state_path,
    )

    resolved_state = Path(state_path).expanduser() if state_path else default_state_path()
    if prove_command == "status":
        cycle = ProveAtRestStore(path=resolved_state).read()
        if cycle is None:
            payload = {"status": "empty", "state_path": str(resolved_state)}
            if output_json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                from verdict import present

                present.header("Prove at rest")
                present.warn("prove-at-rest", f"No prove-at-rest state at {resolved_state}")
            return
        payload = cycle.to_dict()
        payload["state_path"] = str(resolved_state)
        if output_json:
            print(json.dumps(payload, indent=2, sort_keys=True))
            return
        from verdict import present

        summary = cycle.summary
        present.header("Prove at rest  /  status")
        present.kv({"cycle": cycle.cycle_id, "state": resolved_state, **summary})
        present.table(
            ["Status", "Identity", "Reason"],
            [(item.status, item.identity_id, item.reason or "-") for item in cycle.results],
        )
        return

    if prove_command in {"once", "daemon"} and not allow_live_probe:
        message = "live prove-at-rest requires explicit consent; pass --allow-live-probe"
        if output_json:
            print(json.dumps({"error": message}, sort_keys=True))
        else:
            from verdict import present

            present.header("Prove at rest")
            present.fail("prove-at-rest", message)
        raise SystemExit(2)

    try:
        daemon = build_live_daemon(
            state_path=resolved_state,
            base_url=base_url,
            interval_seconds=interval,
            probe_timeout_seconds=timeout,
            allow_live_probe=allow_live_probe,
        )
    except ProveAtRestError as exc:
        message = str(exc)
        if output_json:
            print(json.dumps({"error": message}, sort_keys=True))
        else:
            from verdict import present

            present.header("Prove at rest")
            present.fail("prove-at-rest", message)
        raise SystemExit(2) from exc

    if prove_command == "once":
        cycle = daemon.run_once()
        payload = cycle.to_dict()
        payload["state_path"] = str(resolved_state)
        if output_json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            from verdict import present

            summary = cycle.summary
            present.header("Prove at rest  /  once")
            present.kv({"state": resolved_state, **summary})
            present.status("prove-at-rest", "failed" if summary.get("failed", 0) else "ok")
        if cycle.summary.get("failed", 0):
            raise SystemExit(1)
        return

    if prove_command == "daemon":
        from verdict import present

        if not output_json:
            present.header("Prove at rest  /  daemon")
            present.kv({"interval": f"{interval}s", "state": resolved_state})

        def report_cycle_error(exc: Exception) -> None:
            code = getattr(exc, "code", exc.__class__.__name__)
            if output_json:
                return
            present.fail(
                "prove-at-rest cycle failed",
                f"code={code}: {exc}; retaining last complete state and retrying in {interval}s",
            )

        daemon.on_cycle_error = report_cycle_error
        try:
            daemon.run_forever()
        except KeyboardInterrupt:
            daemon.stop()
            if output_json:
                cycle = daemon.status()
                print(
                    json.dumps(
                        {
                            "status": "interrupted",
                            "state_path": str(resolved_state),
                            "last": None if cycle is None else cycle.to_dict(),
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                present.warn("prove-at-rest daemon", "prove-at-rest daemon stopped")
        return

    raise SystemExit(f"unknown prove-at-rest command: {prove_command}")


def cmd_failover_proof(memory_path: str, output_json: bool = False) -> None:
    """Run the offline forced-failover and replay proof, persisting the session."""
    from verdict.actions.registry import run_action

    result = run_action("failover-proof", {"memory_path": memory_path})
    payload = result.data
    if output_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        from verdict import present

        present.header("Failover proof")
        present.ok("proof", "completed")
        present.kv(
            {
                "Session ID": payload["session_id"],
                "Initial model": payload["initial_model"],
                "Replacement model": payload["replacement_model"],
                "Completed steps": str(payload["completed_steps"]),
                "Digest": payload["replay_digest"],
            }
        )
    if result.exit_code:
        sys.exit(result.exit_code)


def _metadata_json_file(path: str | Path | None) -> Any | None:
    if path is None:
        return None
    return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))


def cmd_metadata(args: Any) -> None:
    command = getattr(args, "metadata_command", None)
    if command == "refresh":
        cmd_metadata_refresh(
            store_path=getattr(args, "store_path", None),
            mapping_path=getattr(args, "mapping_path", None),
            models_dev_api_file=getattr(args, "models_dev_api_file", None),
            models_dev_models_file=getattr(args, "models_dev_models_file", None),
            litellm_file=getattr(args, "litellm_file", None),
            include_p1=getattr(args, "include_p1", False),
            output_json=getattr(args, "json", False),
        )
        return
    if command == "show":
        cmd_metadata_show(
            store_path=getattr(args, "store_path", None), output_json=getattr(args, "json", False)
        )
        return
    if command == "lookup":
        required = tuple(
            item.strip() for item in str(getattr(args, "requires", "")).split(",") if item.strip()
        )
        cmd_metadata_lookup(
            args.omniroute_id,
            store_path=getattr(args, "store_path", None),
            mapping_path=getattr(args, "mapping_path", None),
            required=required,
            output_json=getattr(args, "json", False),
        )
        return
    raise SystemExit(f"unknown metadata command: {command}")


def cmd_metadata_refresh(
    *,
    store_path: str | Path | None = None,
    mapping_path: str | Path | None = None,
    models_dev_api_file: str | Path | None = None,
    models_dev_models_file: str | Path | None = None,
    litellm_file: str | Path | None = None,
    include_p1: bool = False,
    output_json: bool = False,
    now: datetime | None = None,
) -> None:
    """Refresh the Core metadata store from files (offline) or public HTTPS."""
    from verdict.actions.registry import run_action

    params: dict[str, Any] = {
        "store_path": store_path,
        "mapping_path": mapping_path,
        "models_dev_api_file": models_dev_api_file,
        "models_dev_models_file": models_dev_models_file,
        "litellm_file": litellm_file,
        "include_p1": include_p1,
    }
    if now is not None:
        params["now"] = now
    result = run_action("metadata.refresh", params)
    data = result.data
    if not result.ok:
        err = str(data.get("error", "metadata refresh failed"))
        if output_json:
            print(json.dumps({"error": err}, sort_keys=True))
        else:
            from verdict import present

            present.header("Metadata  /  refresh")
            present.fail("metadata refresh", err)
        raise SystemExit(result.exit_code or 1)
    if output_json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return
    from verdict import present

    present.header("Metadata  /  refresh")
    present.ok("Core metadata store refreshed")
    present.kv(
        {
            "records": data["record_count"],
            "mapping drops": data["drop_count"],
            "store": data["store"],
        }
    )
    sources = data.get("sources") or {}
    present.table(
        ["Source", "Status", "Reason"],
        [
            (name, status.get("status", "?"), status.get("reason") or "-")
            for name, status in sources.items()
        ],
    )


def cmd_metadata_show(*, store_path: str | Path | None = None, output_json: bool = False) -> None:
    from verdict.actions.registry import run_action

    result = run_action("metadata.show", {"store_path": store_path})
    data = result.data
    if not result.ok:
        err = str(data.get("error", "metadata show failed"))
        store_ref = str(data.get("store", ""))
        if output_json:
            print(json.dumps({"error": err, "store": store_ref}, sort_keys=True))
        else:
            from verdict import present

            present.header("Metadata  /  show")
            present.fail("metadata", err)
        raise SystemExit(result.exit_code or 1)
    if output_json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return
    from verdict import present

    present.header("Metadata  /  show")
    present.kv(
        {"refreshed": data["refreshed_at"], "records": data["record_count"], "store": data["store"]}
    )


def cmd_metadata_lookup(
    omniroute_id: str,
    *,
    store_path: str | Path | None = None,
    mapping_path: str | Path | None = None,
    required: tuple[str, ...] = (),
    output_json: bool = False,
) -> None:
    from verdict.actions.registry import run_action

    params: dict[str, Any] = {
        "omniroute_id": omniroute_id,
        "store_path": store_path,
        "mapping_path": mapping_path,
        "requires": ",".join(required) if required else "",
    }
    result = run_action("metadata.lookup", params)
    data = result.data
    if not result.ok:
        err = str(data.get("error", "metadata lookup failed"))
        if output_json:
            print(json.dumps({"error": err}, sort_keys=True))
        else:
            from verdict import present

            present.header("Metadata  /  lookup")
            present.fail("metadata lookup", err)
        raise SystemExit(result.exit_code or 1)
    if output_json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return
    from verdict import present

    present.header("Metadata  /  lookup")
    drop = data.get("drop")
    if drop is not None:
        present.warn("named drop", f"{drop.get('reason', '?')}: {omniroute_id}")
        if drop.get("detail"):
            present.note(str(drop["detail"]))
        return
    record = data.get("record") or {}
    present.ok("mapped", f"{omniroute_id} → {record.get('id', '?')}")
    provenance = data.get("provenance") or {}
    present.table(
        ["Source", "Version"],
        [
            (
                name,
                str(cited.get("source", ""))
                + " "
                + str(cited.get("version") or cited.get("fetched_at") or ""),
            )
            for name, cited in provenance.items()
        ],
    )


if __name__ == "__main__":
    main()
