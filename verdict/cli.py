"""CLI entry point for Verdict."""

import argparse
import contextlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn

import yaml
from rich.console import Console
from rich.prompt import Prompt

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
from verdict.models import ModelInfo, ProviderConfig, TaskSpec
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


def _read_omniroute_token() -> str | None:
    """Read an explicitly configured OmniRoute token without private-database access."""

    return os.getenv("OMNIROUTE_API_KEY")


def _omniroute_api_request(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    """Make an authenticated request to the explicitly configured local router."""
    token = _read_omniroute_token()
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    import json
    import urllib.request
    from urllib.error import URLError

    base_url = os.getenv("OMNIROUTE_BASE_URL")
    if not base_url:
        # OMNIROUTE_BASE_URL is not wired into the environment even when a
        # gateway is running locally. Fall back to a one-shot health probe of
        # the known local gateway ports rather than giving up immediately.
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
                    "Existing configuration preserved. Run verdict setup --recommended to review capabilities, or verdict doctor to inspect health.",
                )
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
            existing_nodes = _omniroute_api_request("GET", "/api/provider-nodes")
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
                        res = _omniroute_api_request("POST", "/api/provider-nodes", payload)
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
                    res = _omniroute_api_request("POST", "/api/provider-nodes", payload)
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
                    res = _omniroute_api_request("POST", "/api/provider-nodes", payload)
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
        from verdict.shared_memory import discover_shared_memory_setup

        plan = build_setup_plan().to_dict()
        plan["shared_memory"] = discover_shared_memory_setup()
        if output_json:
            print(json.dumps(plan, indent=2, sort_keys=True))
            return
        ui = TerminalUI(console)
        ui.header("Setup plan")
        ui.plan(plan)
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


def _build_route_gate(allow_offline: bool = False) -> Gate:
    """Build the CLI Gate from the user config (shared by route/compare)."""
    config_dir = os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "verdict"
    )
    config_path = os.path.join(config_dir, "verdict.yaml")
    omniroute = _omniroute_provider_from_env()

    if os.path.exists(config_path):
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}
        providers = {
            k: ProviderConfig(base_url=v.get("base_url", ""), api_key_env=v.get("api_key_env"))
            for k, v in (raw.get("providers") or {}).items()
        }
        for name, cfg in omniroute.items():
            providers.setdefault(name, cfg)
        return Gate(
            primary_model=raw.get("primary_model", DEFAULT_PRIMARY_MODEL),
            providers=providers,
            log_path=raw.get("log_path", "verdict-decisions.jsonl"),
            allow_offline=allow_offline,
        )
    providers = {"public_ollama": ProviderConfig(base_url="http://localhost:11434/v1")}
    providers.update(omniroute)
    return Gate(
        primary_model=DEFAULT_PRIMARY_MODEL, providers=providers, allow_offline=allow_offline
    )


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

    BOD-127: authority is derived from profile / ``VERDICT_REQUIRE_EXECUTION_PATH``
    (and production profile). Pass ``allow_legacy_selector=True`` only as an
    explicit migration escape — never silent. ``allow_offline`` only switches the
    catalog/network surface; it must not imply legacy selector escape. API serve
    still forces authority.
    """
    from verdict.serve_path import CONTEXT_ALLOW_LEGACY

    gate = _build_route_gate(allow_offline=allow_offline)
    # Never couple offline catalog mode to the BOD-127 legacy escape.
    if allow_legacy_selector is None:
        allow_legacy_selector = False
    context: dict[str, object] = {}
    if allow_legacy_selector:
        context[CONTEXT_ALLOW_LEGACY] = True
    # Do not force CONTEXT_REQUIRE_AUTHORITY here — development/smoke CLI must
    # still route via the legacy feed path; production/env opt-in fail-closed.

    if terse:
        dec = gate.route(task, criticality, context=context or None)
        dec = _execute_cli_decision(gate, task, dec, allow_offline=allow_offline)
        if getattr(dec, "transport_outcome", "not_sent") not in {"sent", "success"}:
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

    status_label = (
        "[bold green]Evaluating static catalog (offline)..."
        if allow_offline
        else "[bold green]Evaluating network & heuristics..."
    )
    with console.status(status_label, spinner="dots"):
        dec, selection = gate.route_with_strategy(task, criticality, context=context or None)
        dec = _execute_cli_decision(gate, task, dec, allow_offline=allow_offline)
        selection = type(selection)(
            strategy="DIRECT",
            model=dec.model,
            reasoning="CLI route/run dispatched one provider completion directly",
            timestamp=selection.timestamp,
        )

    from verdict import present

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
    # Machine-readable StrategySelection record (issue #265).
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
    if dec.transport_outcome not in {"sent", "success"}:
        raise SystemExit(1)


def cmd_compare(task: str, criticality: str = "medium", allow_offline: bool = False) -> None:
    """Compare a DIRECT frontier call against the Verdict route (issue #265)."""
    from verdict.comparison import ComparisonHarness

    gate = _build_route_gate(allow_offline=allow_offline)
    harness = ComparisonHarness(gate=gate)
    report = harness.compare(task, criticality=criticality)
    print(json.dumps({"comparison_report": report.to_dict()}, sort_keys=True, indent=2))


def cmd_stats(log_path: str = "verdict-decisions.jsonl") -> None:
    """Parse JSONL logs and build analytics."""
    from verdict import present

    if not os.path.exists(log_path):
        present.header("Routing stats")
        present.warn("log", f"No log file found at {log_path}")
        return

    tiers: dict[int, int] = {}
    models: dict[str, int] = {}
    latencies: list[float] = []

    with open(log_path) as f:
        for line in f:
            try:
                entry = json.loads(line)
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
            except json.JSONDecodeError:
                continue

    total = sum(tiers.values())
    avg_latency = sum(latencies) / len(latencies) if latencies else 0

    present.header("Routing stats")
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
    present.table(
        ["Model", "Calls"],
        [
            (mod, str(count))
            for mod, count in sorted(models.items(), key=lambda x: x[1], reverse=True)[:5]
        ],
    )


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
            # BOD-114: the only claim-capable mode. Both arms execute against the
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

    present.header("Cost and Usage Report")

    log_path = "verdict-decisions.jsonl"
    if not os.path.exists(log_path):
        present.warn("log", "No routing telemetry found (Verdict decision log missing).")
        return

    total_requests = 0
    t0_requests = 0

    with open(log_path) as f:
        for line in f:
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                decision = data.get("decision")
                if isinstance(decision, dict):
                    tier = decision.get("tier", 2)
                else:
                    tier = data.get("effective_tier", data.get("tier", 2))
                if tier == 0:
                    t0_requests += 1
                total_requests += 1
            except Exception:
                pass

    savings = (total_requests - t0_requests) * 0.005
    present.table(
        ["Metric", "Value"],
        [
            ("Total Routing Requests", str(total_requests)),
            ("T0 (Critical) Forwarded", str(t0_requests)),
            ("Offloaded Tasks (T1-T3)", str(total_requests - t0_requests)),
            ("Estimated Savings vs T0 Only", f"${savings:.2f}"),
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
    if offline:
        payload: dict[str, Any] = {
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
        if output_json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        elif output_config:
            print(yaml.dump({"providers": {}}, default_flow_style=False))
        else:
            from verdict import present

            present.header("Provider detection (offline)")
            present.kv({"network access": "no", "credentials read": "no"})
            present.note("Offline mode reports nothing by design. Run `verdict detect` to probe.")
        return

    try:
        from verdict.provider_detection import (
            detect_all_providers,
            format_detection_report,
            generate_verdict_config,
            probe_gateways,
        )

        result = detect_all_providers()

        # T018/T019/T020: HTTP-validated gateway detection (TCP + /api/health),
        # replacing reliance on the TCP-only centralized-router heuristic for
        # gateway selection purposes.
        gateways = probe_gateways()
        healthy_gateways = [g for g in gateways if g.health_ok]
        no_gateway_message = "No local gateway found on ports 20128, 20129, 20132."
        multi_gateway_message = (
            "Multiple gateways found. Set OMNIROUTE_BASE_URL to one of the above to select it."
        )
        gateway_message = None
        if not healthy_gateways:
            gateway_message = no_gateway_message
        elif len(healthy_gateways) > 1:
            gateway_message = multi_gateway_message

        if output_json:
            print(
                json.dumps(
                    {
                        "local_servers": [p.__dict__ for p in result.local_servers],
                        "cli_providers": [p.__dict__ for p in result.cli_providers],
                        "centralized_routers": [p.__dict__ for p in result.centralized_routers],
                        "cloud_apis": [p.__dict__ for p in result.cloud_apis],
                        "custom_endpoints": [p.__dict__ for p in result.custom_endpoints],
                        "gateways": [g.__dict__ for g in gateways],
                        "message": gateway_message,
                    },
                    indent=2,
                )
            )
        elif output_config:
            config = generate_verdict_config(result)
            print(yaml.dump(config, default_flow_style=False))
        else:
            from verdict import present

            present.header("Provider detection")
            # Keep the established provider report as a note so its detailed
            # wording remains available while presentation owns the framing.
            present.note(format_detection_report(result, verbose=verbose))
            present.section("Gateways (HTTP-validated)")
            if healthy_gateways:
                present.table(
                    ["Gateway", "Identity", "URL", "Port"],
                    [(g.display_name, g.identity, g.url, g.port) for g in healthy_gateways],
                )
                if len(healthy_gateways) > 1:
                    present.warn("gateway selection", multi_gateway_message)
            else:
                present.warn("gateway", no_gateway_message)
                present.note("To start OmniRoute: npm install -g omniroute && omniroute serve")
    except Exception as e:
        from verdict import present

        present.header("Provider detection")
        present.fail("Detection failed", str(e))
        import traceback

        traceback.print_exc()
        sys.exit(1)


def cmd_certify(*, snapshot_path: str | None = None, output_json: bool = True) -> None:
    """Emit a BOD-92 runtime certification report (evidence only, JSON).

    Reads DetectedSnapshot fixtures from ``--from`` when provided. Does not
    perform live network probes or mutate setup/doctor state (BOD-124).
    """
    from verdict.runtime_certification import ComponentKind, DetectedSnapshot, certify_runtime

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
    encoded = json.dumps(report.to_dict(), indent=2, sort_keys=True)
    if output_json:
        print(encoded)
    else:
        from verdict import present

        present.header("Runtime certification")
        present.note(encoded)


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
    from verdict.probes import ProbePolicy, ProbeRunner, _redact

    is_injected = transport is not None
    if not is_injected and not allow_live_probe:
        message = "live probes require explicit consent; pass --allow-live-probe"
        if output_json:
            print(json.dumps({"error": message, "diagnostics": None}, sort_keys=True))
        else:
            from verdict import present

            present.header("Probe")
            present.fail("probe", message)
        raise SystemExit(2)
    if transport is None:
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
    results = [_probe_result_payload(observation) for observation in run.observations]

    if output_json:
        print(json.dumps({"diagnostics": run.diagnostics.to_dict(), "results": results}, indent=2))
        return

    from verdict import present

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
    if not all(e.get("ok") for e in results):
        sys.exit(1)


def _execution_path_decision_from_request_file(path: str, *, task: str) -> Any:
    """Build the mandatory BOD-104 decision in-process from the public contract."""

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

            probe_transport = openai_probe_transport(family_url, api_key=_read_omniroute_token())
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
    from verdict.autodev_run import shadow_learning_report

    payload = json.loads(Path(episodes_path).expanduser().resolve().read_text(encoding="utf-8"))
    if isinstance(payload, dict) and "episodes" in payload:
        episodes = payload["episodes"]
    else:
        episodes = payload
    if not isinstance(episodes, list):
        message = "shadow episodes JSON must be a list or an object with episodes"
        if output_json:
            print(json.dumps({"error": message}, sort_keys=True))
        else:
            from verdict import present

            present.header("Autodev packet  /  shadow")
            present.fail("episodes", message)
        raise SystemExit(1)
    report = shadow_learning_report(episodes)
    print(json.dumps(report, indent=2, sort_keys=True))


def cmd_autodev_packet_canary(
    episodes_path: str, admitted_path: str, *, output_json: bool = False
) -> None:
    """Dump an explicit bounded canary choice. Does not call EligibilityGate."""
    from verdict.autodev_run import apply_shadow_canary, shadow_learning_report

    if not episodes_path or not admitted_path:
        message = "canary apply requires --episodes and --admitted"
        if output_json:
            print(json.dumps({"error": message}, sort_keys=True))
        else:
            from verdict import present

            present.header("Autodev packet  /  canary")
            present.fail("inputs", message)
        raise SystemExit(1)
    payload = json.loads(Path(episodes_path).expanduser().resolve().read_text(encoding="utf-8"))
    if isinstance(payload, dict) and "episodes" in payload:
        episodes = payload["episodes"]
    else:
        episodes = payload
    admitted = json.loads(Path(admitted_path).expanduser().resolve().read_text(encoding="utf-8"))
    if not isinstance(episodes, list) or not isinstance(admitted, list):
        message = "canary requires an episodes list and an admitted identity list"
        if output_json:
            print(json.dumps({"error": message}, sort_keys=True))
        else:
            from verdict import present

            present.header("Autodev packet  /  canary")
            present.fail("inputs", message)
        raise SystemExit(1)
    report = shadow_learning_report(episodes)
    print(
        json.dumps(
            apply_shadow_canary([str(item) for item in admitted], report), indent=2, sort_keys=True
        )
    )


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


def _probe_result_payload(observation: Any) -> dict[str, Any]:
    """Convert a probe observation to a credential-safe CLI result."""

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
    import urllib.request

    from verdict.omniroute_catalog import (
        CATALOG_FETCH_TIMEOUT_SECONDS,
        CatalogQualificationReport,
        probe_catalog,
        qualify_catalog,
        reconcile_catalog_projections,
        store_qualification,
    )

    if probe and not allow_live_probe:
        message = "catalog live probes require explicit consent; pass --allow-live-probe"
        if output_json:
            print(json.dumps({"error": message, "probes": None}, sort_keys=True))
        else:
            from verdict import present

            present.header("Catalog qualification")
            present.fail("catalog", message)
        raise SystemExit(2)

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
        request = urllib.request.Request(source_url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(  # nosec B310
                request, timeout=CATALOG_FETCH_TIMEOUT_SECONDS
            ) as response:
                payload = response.read()
        except TimeoutError as exc:
            del exc
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
    if probe and report.snapshot and report.passed:
        from verdict.probes import openai_probe_transport

        probe_summary = probe_catalog(
            payloads["management" if management else "public"],
            openai_probe_transport(
                base_url.rstrip("/") + "/v1", api_key=os.getenv("OPENAI_API_KEY")
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
    if not report.passed or (reconciliation is not None and not reconciliation.passed):
        sys.exit(1)


def cmd_suggest(log_path: str = "verdict-decisions.jsonl") -> None:
    """Run the SuggestionService to propose evidence-backed improvements."""
    from verdict import present
    from verdict.suggestions import SuggestionService

    svc = SuggestionService(log_path=log_path)
    suggestions = svc.generate_suggestions()

    present.header("Verdict Intelligence Suggestions")
    if not suggestions:
        present.note("No actionable suggestions found. Your routing is optimized!")
        return

    for s in suggestions:
        present.section(f"{s.title} ({s.id})")
        present.kv(
            {
                "Category": s.category.title(),
                "Novelty": s.novelty,
                "Expires In": s.expiry,
                "Description": s.description,
                "Proposed Experiment": s.proposed_next_experiment,
                "Confidence": f"{s.confidence * 100:.1f}%",
                "Impact": s.expected_impact,
                "Evidence (top 3)": (
                    ", ".join(s.evidence_references) if s.evidence_references else "None"
                ),
            }
        )


def cmd_doctor(fix: bool = False, output_json: bool = False) -> None:
    """Scan the Verdict setup and OmniRoute connections for issues and repair them."""
    if output_json:
        from pathlib import Path

        from verdict.capability_bootstrap import doctor_capability_report
        from verdict.memory_bridge import run_doctor_diagnostics
        from verdict.runtime_daemons import RuntimeManager
        from verdict.runtime_health import build_runtime_health_report

        report = run_doctor_diagnostics(home_dir=Path.home(), cwd=Path.cwd(), fix=fix)
        report["runtime_health"] = build_runtime_health_report(RuntimeManager().status()).to_dict()
        report["capability_bootstrap"] = doctor_capability_report()
        print(json.dumps(report, indent=2, sort_keys=True))
        if report["status"] != "healthy":
            raise SystemExit(1)
        return

    ui = TerminalUI(console)
    ui.header("Doctor")

    from verdict.capability_bootstrap import doctor_capability_report

    with ui.task("Inspecting capabilities"):
        capability_report = doctor_capability_report()
    ui.doctor(capability_report)
    capabilities = capability_report.get("capabilities", [])
    if not isinstance(capabilities, list):
        capabilities = []
    covered = sum(
        1 for item in capabilities if isinstance(item, dict) and item.get("status") == "covered"
    )
    total = len(capabilities)
    ui.status("Capability coverage", "ok", f"{covered}/{total} covered (bootstrap view)")

    issues_found = []
    fixed_issues = []

    from verdict.documentation_preflight import run_documentation_preflight

    documentation_report = run_documentation_preflight(fix=fix)
    doc_state = "ok" if documentation_report.passed else "failed"
    ui.status(
        "Documentation preflight",
        doc_state,
        f"{documentation_report.status} ({documentation_report.inventory} documents, "
        f"{documentation_report.ingested} ingested, "
        f"{documentation_report.stale} stale, "
        f"{documentation_report.missing} missing)",
    )
    if not documentation_report.passed:
        issues_found.extend(
            ["authoritative documentation preflight did not pass", *documentation_report.errors]
        )
    elif fix and documentation_report.ingested:
        fixed_issues.append("authoritative documentation preflight repaired")

    from verdict.shared_memory import doctor_shared_memory_report

    shared_memory = doctor_shared_memory_report()
    ui.status(
        "Shared memory",
        str(shared_memory.get("state", "unknown")),
        str(shared_memory.get("endpoint") or shared_memory.get("provider_id") or ""),
    )

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

    if config is not None:
        primary_model = config.get("primary_model")
        if not primary_model:
            issues_found.append("No primary model configured in verdict.yaml.")
        else:
            from verdict.classifier import classify

            tier = classify(primary_model)
            ui.status("Configured Primary Model", "ok", f"{primary_model} (Tier-{tier})")

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
                    ui.console.print(f"  [green]✓[/] Renamed {legacy_config_path} -> {config_path}")
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
    ui.section("Environment reference")
    ui.console.print(
        "  [dim]See .env.example in the repository root for the full environment "
        "variable reference.[/dim]"
    )

    # 2. OmniRoute nodes check
    existing_nodes = _omniroute_api_request("GET", "/api/provider-nodes")
    if existing_nodes is None:
        ui.section("OmniRoute nodes")
        ui.console.print(
            "[dim]OmniRoute server is not currently running/reachable to check nodes.[/dim]"
        )
    else:
        items = []
        if isinstance(existing_nodes, list):
            items = existing_nodes
        elif isinstance(existing_nodes, dict) and "items" in existing_nodes:
            items = existing_nodes["items"]

        ui.status("Connected to OmniRoute", "ok", f"Found {len(items)} configured node endpoints")

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
            ui.section("Duplicate nodes detected")
            for node_id, name, _url, original_id in duplicates:
                ui.status(
                    f"Duplicate node {name}",
                    "warn",
                    f"({node_id}) is a duplicate of ({original_id})",
                )
                issues_found.append(f"Duplicate node '{name}' in OmniRoute configuration.")

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
                        res = _omniroute_api_request("DELETE", f"/api/provider-nodes/{node_id}")
                        if res is not None:
                            ui.status("Removed", "ok", f"Removed duplicate node: {name}")
                            fixed_issues.append(f"Removed duplicate node {node_id}")
                        else:
                            ui.status("Removal failed", "failed", f"Node {node_id}")
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

    # One shared presentation for the existing diagnostic results.
    ui.doctor_summary(issues_found, fixed_issues)
    if issues_found and not config:
        ui.panel(
            "Next step", "Run verdict setup to initialize your configuration file.", tone="WARNING"
        )


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


def cmd_choose(
    *,
    task_class: str,
    requires: str = "",
    model: str | None = None,
    candidates_json: str | None = None,
    output_json: bool = False,
) -> None:
    """Select an eligible execution target for Prime dispatch."""
    from verdict.chooser import ChooserError, choose_route, human_summary, load_candidates_json

    if not candidates_json:
        print(
            "verdict choose requires --candidates-json until live catalog probing is in a later story",
            file=sys.stderr,
        )
        sys.exit(2)
    required = tuple(part.strip() for part in requires.split(",") if part.strip())
    try:
        candidates = load_candidates_json(candidates_json)
        receipt = choose_route(
            candidates, task_class=task_class, requires=required, explicit_model=model
        )
    except ChooserError as exc:
        payload = {
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
        if output_json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            from verdict import present

            present.header("Choose route")
            present.fail(task_class, exc.reason)
            if exc.exclusions:
                present.note(
                    "excluded: "
                    + ", ".join(
                        f"{item.get('model', '?')} ({item.get('reason', 'excluded')})"
                        for item in exc.exclusions[:3]
                    )
                )
        sys.exit(1)
    if output_json:
        print(json.dumps(receipt.to_dict(), indent=2, sort_keys=True))
        return
    from verdict import present

    present.header("Choose route")
    present.note(human_summary(receipt))


def cmd_models(catalog: list[ModelInfo] | None = None, output_json: bool = False) -> None:
    """List the qualified model catalog used for routing and simulation."""
    if catalog is None:
        catalog = default_model_catalog()
    if output_json:
        print(
            json.dumps(
                [
                    {
                        "id": m.id,
                        "provider": m.provider,
                        "tier": m.capability_tier,
                        "context_window": m.context_window,
                        "cost_per_1k": m.cost_per_1k,
                        "availability_state": m.availability_state,
                    }
                    for m in catalog
                ],
                indent=2,
                sort_keys=True,
            )
        )
        return
    from verdict import present

    present.header("Model catalog")
    present.table(
        ["ID", "Provider", "Tier", "Context", "Cost/1k", "State"],
        [
            (
                m.id,
                m.provider,
                f"T{m.capability_tier}",
                str(m.context_window) if m.context_window > 0 else "-",
                f"${m.cost_per_1k:.4f}" if m.cost_per_1k else "-",
                m.availability_state,
            )
            for m in catalog
        ],
        empty="catalog is empty",
    )
    present.note(f"{len(catalog)} model(s). Live eligibility: verdict eligibility --probe")


def cmd_inspect(
    model_id: str, catalog: list[ModelInfo] | None = None, output_json: bool = False
) -> None:
    """Inspect one model's catalog record and any stored passport evidence."""
    if catalog is None:
        catalog = default_model_catalog()
    matches = [m for m in catalog if m.id == model_id or f"{m.provider}/{m.id}" == model_id]
    if not matches:
        message = f"model not found in catalog: {model_id}"
        if output_json:
            print(json.dumps({"error": message}, sort_keys=True))
        else:
            from verdict import present

            present.header("Model inspect")
            present.fail(model_id, "not found in catalog")
        raise SystemExit(1)
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
    if output_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    from verdict import present

    present.header(f"Model inspect  /  {model.id}")
    present.kv(
        {
            "provider": model.provider,
            "tier": f"T{model.capability_tier}",
            "context window": model.context_window or "-",
            "cost per 1k": f"${model.cost_per_1k:.4f}" if model.cost_per_1k else "-",
            "capabilities": ", ".join(sorted(model.capabilities)) or "-",
            "availability": model.availability_state,
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
    """Inspect durable RoutingReceiptV1 records from ReceiptStore (BOD-144)."""
    from pathlib import Path

    from verdict.receipt_store import ReceiptStore
    from verdict.routing_receipt import attempt_scope, human_summary, load_routing_receipt

    if db_path:
        db = Path(db_path)
    else:
        repo_db = Path.cwd() / ".verdict" / "receipts.db"
        db = repo_db if repo_db.exists() else (Path.home() / ".verdict" / "receipts.db")
    # List-all must scan scopes; show/export keep strict scope when a scope is known.
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
        scope_value = scope
        if scope_value is None and attempt_id is not None:
            scope_value = attempt_scope(story_id=None, work_unit_id=None, attempt_id=attempt_id)
        receipt = load_routing_receipt(
            store, receipt_id=receipt_id, scope=scope_value, attempt_id=attempt_id
        )
        if receipt is None and attempt_id is not None and scope is None:
            # Scan scopes for the attempt.
            for row in store.query_receipts(receipt_type="decision", limit=500):
                if row.parent_receipt_id:
                    continue
                if row.idempotency_key == attempt_id or row.payload.get("attempt_id") == attempt_id:
                    receipt = load_routing_receipt(
                        store, receipt_id=row.receipt_id, scope=row.scope, attempt_id=attempt_id
                    )
                    break
        if receipt is None:
            raise SystemExit("routing receipt not found")
        if output_json:
            print(json.dumps(receipt.to_dict(), indent=2, sort_keys=True))
            return
        from verdict import present

        present.header("Routing receipt")
        present.note(human_summary(receipt))
        print(json.dumps(receipt.to_dict(), indent=2, sort_keys=True))
        return

    if action == "export":
        receipt = load_routing_receipt(
            store, receipt_id=receipt_id, scope=scope, attempt_id=attempt_id
        )
        if receipt is None:
            raise SystemExit("routing receipt not found")
        print(json.dumps(receipt.to_dict(), indent=2, sort_keys=True))
        return

    raise SystemExit(f"unknown receipt action: {action}")


def cmd_replay(session_id: str, output_json: bool = False) -> None:
    """Replay a recorded execution session from the shared MemoryPlane."""
    try:
        from verdict.execution_session import ExecutionSession, ExecutionSessionError
        from verdict.memory_plane import MemoryPlane
    except ImportError as exc:
        message = (
            "replay is not yet available: verdict.execution_session is still in "
            f"development ({exc})"
        )
        if output_json:
            print(json.dumps({"status": "unavailable", "message": message}, sort_keys=True))
        else:
            from verdict import present

            present.header("Replay session")
            present.warn("replay", message)
        raise SystemExit(3) from exc
    db_path = os.environ.get("VERDICT_MEMORY_DB", str(Path.home() / ".verdict" / "memory.db"))
    try:
        session = ExecutionSession.resume(session_id, MemoryPlane(db_path))
    except ExecutionSessionError as exc:
        message = f"no recorded session found for id: {session_id} ({exc})"
        if output_json:
            print(json.dumps({"status": "missing", "message": message}, sort_keys=True))
        else:
            from verdict import present

            present.header("Replay session")
            present.fail(session_id, "not found")
        raise SystemExit(1) from exc
    record = session.to_dict()
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
    from verdict.simulator import simulate

    spec = TaskSpec(prompt=task, criticality=criticality)
    forecast = simulate(
        spec,
        model_catalog=catalog if catalog is not None else default_model_catalog(),
        model_override=model_override,
    )
    if output_json:
        print(json.dumps(forecast.to_dict(), indent=2, sort_keys=True))
        return
    from verdict import present

    present.header("Verdict pre-execution simulation")
    present.table(
        ["Metric", "Value"],
        [
            ("Model", f"{forecast.model} ({forecast.provider}, T{forecast.tier})"),
            ("Prompt tokens", str(forecast.prompt_tokens)),
            ("Completion tokens", str(forecast.completion_tokens)),
            ("Total tokens", str(forecast.total_tokens)),
            ("Est. cost", f"${forecast.cost_usd:.6f}"),
            ("Risk score", f"{forecast.risk_score} / 100"),
            ("Capacity confidence", f"{forecast.capacity_confidence:.2f}"),
        ],
    )
    present.note(forecast.rationale)


def default_model_catalog() -> list[ModelInfo]:
    """Build the default catalog from the configured verdict.yaml and classified tiers."""
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

    primary = str(raw.get("primary_model", DEFAULT_PRIMARY_MODEL))
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
    """Validate the Verdict configuration file and print status."""
    config_dir = os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "verdict"
    )
    config_path = os.path.join(config_dir, "verdict.yaml")
    from verdict import present

    present.header("Configuration check")

    if not os.path.exists(config_path):
        present.fail("Configuration file (verdict.yaml) is missing", f"at {config_path}.")
        sys.exit(1)

    try:
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}
    except Exception as exc:
        present.fail("Configuration file is corrupted/invalid YAML", str(exc))
        sys.exit(1)

    has_issue = False

    primary_model = config.get("primary_model")
    if not primary_model:
        present.fail("No primary model configured in verdict.yaml.")
        has_issue = True
    else:
        from verdict.classifier import classify

        tier = classify(primary_model)
        present.ok("Configured Primary Model", f"{primary_model} (Tier-{tier})")

    providers = config.get("providers", {})
    if not isinstance(providers, dict):
        present.fail("'providers' section in verdict.yaml is malformed.")
        has_issue = True
    else:
        urls: dict[str, str] = {}
        for name, p_cfg in providers.items():
            if not isinstance(p_cfg, dict):
                present.fail(f"Provider '{name}' config is not a dictionary.")
                has_issue = True
                continue
            base_url = p_cfg.get("base_url", "")
            if "sk-" in base_url or "api_key" in base_url.lower():
                present.fail(f"Literal API key detected inside host URL for provider '{name}'.")
                has_issue = True

            if base_url:
                url = base_url.rstrip("/")
                if url in urls:
                    present.fail(
                        "Duplicate host URL configured in verdict.yaml",
                        f"provider '{name}' and '{urls[url]}' have identical hosts: {url}",
                    )
                    has_issue = True
                else:
                    urls[url] = name

    if has_issue:
        present.fail("Config validation failed with issues.")
        sys.exit(1)

    present.ok("Configuration file is valid.")


def cmd_compat(compat_command: str | None, declared: str | None, output_json: bool) -> None:
    """Publish or check the cross-repo contract compatibility manifest (ADR-024, CON-001).

    Fails closed: a missing declaration or a hash mismatch against the current
    verdict-core contracts blocks (exit 1) rather than assuming compatibility.
    """
    from verdict.compatibility_manifest import build_compatibility_manifest, check_compatibility

    if compat_command == "manifest":
        manifest = build_compatibility_manifest()
        if output_json:
            print(json.dumps(manifest.to_dict(), indent=2, sort_keys=True))
        else:
            from verdict import present

            present.header("Compatibility manifest")
            present.kv({"schema": manifest.schema_version, "manifest hash": manifest.manifest_hash})
            present.table(["Contract", "Digest"], sorted(manifest.contracts.items()))
        return

    if compat_command == "check":

        def _fail(reason: str) -> NoReturn:
            if output_json:
                print(json.dumps({"allowed": False, "reason": reason}, indent=2))
            else:
                from verdict import present

                present.header("Compatibility check")
                present.fail("compatibility", f"{reason} (failing closed)")
            sys.exit(1)

        if not os.path.exists(declared or ""):
            _fail(f"Declared manifest file not found: {declared}")

        try:
            with open(declared) as f:  # type: ignore[arg-type]
                declared_raw = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            _fail(f"Declared manifest is invalid JSON: {exc}")

        declared_contracts = (
            declared_raw.get("contracts") if isinstance(declared_raw, dict) else None
        )
        if not isinstance(declared_contracts, dict):
            _fail("Declared manifest missing 'contracts' object.")

        result = check_compatibility(declared_contracts)
        if result.allowed:
            if output_json:
                print(json.dumps({"allowed": True, "reason": None}, indent=2))
            else:
                from verdict import present

                present.header("Compatibility check")
                present.ok("compatibility", "matches the current verdict-core contracts")
            return

        if output_json:
            print(
                json.dumps(
                    {
                        "allowed": False,
                        "reason": result.reason,
                        "mismatched_contracts": list(result.mismatched_contracts),
                    },
                    indent=2,
                )
            )
        else:
            from verdict import present

            present.header("Compatibility check")
            present.fail("compatibility", str(result.reason))
            present.table(["Mismatched contract"], [(n,) for n in result.mismatched_contracts])
        sys.exit(1)

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


def main() -> None:
    parser = argparse.ArgumentParser(description="Verdict: policy-gated LLM Router")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    from verdict.commands import (
        parsers_autodev,
        parsers_harness,
        parsers_models,
        parsers_routing,
        parsers_runtime,
        parsers_setup,
    )

    for registrar in (
        parsers_setup,
        parsers_routing,
        parsers_autodev,
        parsers_harness,
        parsers_runtime,
        parsers_models,
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
    """Reconstruct durable resume state for a Linear story (BOD-65/66 foundations).

    Canonical sources: Git worktree/branch/SHA, ``.verdict/handoff.md``, optional
    ``gh`` PR discovery. Does not read proprietary chat history. ``--with`` records
    a launcher stub only.
    """
    from verdict.resume import resume_story
    from verdict.worktree_registry import WorktreeRegistryError

    target = Path(repo) if repo is not None else Path.cwd()
    try:
        payload = resume_story(
            target, story, with_harness=with_harness, create_if_missing=create_if_missing
        )
    except WorktreeRegistryError as exc:
        if output_json:
            print(json.dumps({"error": str(exc), "story": story}, sort_keys=True))
        else:
            from verdict import present

            present.header("Resume")
            present.fail("resume", str(exc))
        raise SystemExit(1) from exc

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
    from verdict.failover_replay_proof import run_forced_failover_proof
    from verdict.memory_plane import MemoryPlane

    with MemoryPlane(memory_path) as plane:
        proof = run_forced_failover_proof(plane)
    payload = {
        "session_id": proof.mission_id,
        "initial_model": "provider-a/model-a",
        "replacement_model": proof.replacement_model,
        "failure_status": 429,
        "completed_steps": list(proof.completed_stages),
        "event_sequence": [e.to_dict() for e in proof.events],
        "replay_digest": proof.digest,
    }
    if output_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        from verdict import present

        present.header("Failover proof")
        present.ok("proof", "completed")
        present.kv(
            {
                "Session ID": proof.mission_id,
                "Initial model": "provider-a/model-a",
                "Replacement model": proof.replacement_model,
                "Completed steps": str(list(proof.completed_stages)),
                "Digest": proof.digest,
            }
        )


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
    from verdict.metadata import ModelMetadataError, file_transport, refresh_metadata

    offline = any(
        path is not None for path in (models_dev_api_file, models_dev_models_file, litellm_file)
    )
    clock = now or datetime.now(timezone.utc)
    try:
        transport = (
            file_transport(
                models_dev_api=_metadata_json_file(models_dev_api_file),
                models_dev_models=_metadata_json_file(models_dev_models_file),
                litellm=_metadata_json_file(litellm_file),
                fetched_at=clock.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            )
            if offline
            else None
        )
        snapshot = refresh_metadata(
            transport=transport,
            mapping_path=mapping_path,
            store_path=store_path,
            include_p1=include_p1,
            now=clock,
            persist=True,
        )
    except (ModelMetadataError, OSError, json.JSONDecodeError) as exc:
        if output_json:
            print(json.dumps({"error": str(exc)}, sort_keys=True))
        else:
            from verdict import present

            present.header("Metadata  /  refresh")
            present.fail("metadata refresh", str(exc))
        raise SystemExit(1) from exc
    report = {
        "schema_version": snapshot.schema_version,
        "refreshed_at": snapshot.refreshed_at,
        "record_count": len(snapshot.records),
        "drop_count": len(snapshot.drops),
        "conflict_count": len(snapshot.conflicts),
        "sources": {name: status.to_dict() for name, status in snapshot.sources.items()},
        "mapping": snapshot.mapping,
        "store": str(
            Path(store_path).expanduser()
            if store_path
            else Path.home() / ".verdict" / "model-metadata.json"
        ),
    }
    if output_json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return
    from verdict import present

    present.header("Metadata  /  refresh")
    present.ok("Core metadata store refreshed")
    present.kv(
        {
            "records": report["record_count"],
            "mapping drops": report["drop_count"],
            "store": report["store"],
        }
    )
    present.table(
        ["Source", "Status", "Reason"],
        [(name, status.status, status.reason or "-") for name, status in snapshot.sources.items()],
    )


def cmd_metadata_show(*, store_path: str | Path | None = None, output_json: bool = False) -> None:
    from verdict.metadata import ModelMetadataError, default_store_path, load_store

    resolved = Path(store_path).expanduser() if store_path else default_store_path()
    try:
        snapshot = load_store(resolved)
    except (ModelMetadataError, OSError, json.JSONDecodeError) as exc:
        if output_json:
            print(json.dumps({"error": str(exc), "store": str(resolved)}, sort_keys=True))
        else:
            from verdict import present

            present.header("Metadata  /  show")
            present.fail("metadata", str(exc))
        raise SystemExit(1) from exc
    payload = {
        "store": str(resolved),
        "schema_version": snapshot.schema_version,
        "refreshed_at": snapshot.refreshed_at,
        "record_count": len(snapshot.records),
        "sources": {name: status.to_dict() for name, status in snapshot.sources.items()},
        "drops": [item.to_dict() for item in snapshot.drops],
    }
    if output_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    from verdict import present

    present.header("Metadata  /  show")
    present.kv(
        {"refreshed": snapshot.refreshed_at, "records": len(snapshot.records), "store": resolved}
    )


def cmd_metadata_lookup(
    omniroute_id: str,
    *,
    store_path: str | Path | None = None,
    mapping_path: str | Path | None = None,
    required: tuple[str, ...] = (),
    output_json: bool = False,
) -> None:
    from verdict.metadata import (
        ModelMetadataError,
        default_store_path,
        load_identity_map,
        load_store,
        lookup_omniroute_id,
    )

    resolved = Path(store_path).expanduser() if store_path else default_store_path()
    try:
        snapshot = load_store(resolved)
        mapping = load_identity_map(mapping_path)
        found = lookup_omniroute_id(snapshot, omniroute_id, required=required, identity_map=mapping)
    except (ModelMetadataError, OSError, json.JSONDecodeError) as exc:
        if output_json:
            print(json.dumps({"error": str(exc)}, sort_keys=True))
        else:
            from verdict import present

            present.header("Metadata  /  lookup")
            present.fail("metadata lookup", str(exc))
        raise SystemExit(1) from exc
    payload = found.to_dict()
    if output_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    from verdict import present

    present.header("Metadata  /  lookup")
    if found.drop is not None:
        present.warn("named drop", f"{found.drop.reason}: {omniroute_id}")
        if found.drop.detail:
            present.note(found.drop.detail)
        return
    assert found.record is not None
    present.ok("mapped", f"{omniroute_id} → {found.record.id}")
    present.table(
        ["Source", "Version"],
        [
            (
                name,
                cited.get("source", "")
                + " "
                + str(cited.get("version") or cited.get("fetched_at") or ""),
            )
            for name, cited in found.provenance_for_receipt().items()
        ],
    )


if __name__ == "__main__":
    main()
