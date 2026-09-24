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
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from verdict.benchmarking import format_benchmark_report, run_reproducible_benchmarks
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
    console.print(
        Panel.fit(
            "[bold blue]Verdict Provider Detection[/bold blue]\n"
            "Scanning for local servers, CLIs, API keys, and routers...",
            border_style="blue",
        )
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
            ui.console.print(
                f"[yellow]⚠️  Existing config at {existing_config_path} is not valid YAML: {e}[/yellow]"
            )
            try:
                overwrite = Prompt.ask("Overwrite it?", default="Y")
            except (KeyboardInterrupt, EOFError):
                overwrite = "n"
            if not overwrite.lower().startswith("y"):
                ui.console.print("[yellow]Setup cancelled.[/yellow]")
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
        ui.console.print(f"[yellow]Detection skipped: {e}[/yellow]")

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
            ui.console.print(
                f"\n[bold green]✓ Detected {selected_gateway.display_name} at "
                f"{selected_gateway.url} — gateway URL saved to config.[/bold green]"
            )
            if len(healthy_gateways) > 1:
                ui.console.print(
                    "[dim]Multiple gateways found. Set OMNIROUTE_BASE_URL to one of the "
                    "above to select a different one.[/dim]"
                )
    except Exception as e:
        ui.console.print(f"[yellow]Gateway detection skipped: {e}[/yellow]")

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
        ui.console.print("\n[bold cyan]Auto-detection found active providers![/bold cyan]")
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
                        ui.console.print(
                            f"\n[cyan]Detected models for {selected_provider.name}:[/cyan]"
                        )
                        # Add an option for custom
                        model_options = [*list(models), "Enter a custom model ID"]
                        selected_model = select_from_list(
                            "Select the primary model (Tier-0)", model_options, default="1"
                        )
                        if selected_model == "Enter a custom model ID":
                            config["primary_model"] = Prompt.ask(
                                "Enter custom primary model ID",
                                default="anthropic/claude-3-opus-20240229",
                            )
                        else:
                            config["primary_model"] = selected_model
                    else:
                        config["primary_model"] = Prompt.ask(
                            "No models returned from server. Enter primary model ID (Tier-0)",
                            default="anthropic/claude-3-opus-20240229",
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
                ui.console.print(
                    "\n[bold cyan]Syncing detected system providers to OmniRoute/9Router:[/bold cyan]"
                )
                for name, _p_name, url, _ in to_sync:
                    ui.console.print(f"  • Found active [green]{name}[/]: [dim]{url}[/]")

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
                            ui.console.print(
                                f"  [green]✓[/] Successfully registered node: {node_name}"
                            )
                        else:
                            ui.console.print(f"  [red]✗[/] Failed to register node: {node_name}")
        except (KeyboardInterrupt, EOFError):
            pass

    # Prompt user about adding free providers like gemini/antigravity for local fallback routing
    try:
        ui.console.print("\n[bold cyan]Fallback Models Configuration:[/bold cyan]")
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
                ui.console.print(
                    "\n[yellow]⚠️  GEMINI_API_KEY is not configured in your environment.[/yellow]"
                )
                ui.console.print("  Get a free Gemini API key at: https://aistudio.google.com/")
                ui.console.print('  Then select it: export GEMINI_API_KEY="your_key"')

            or_key = os.getenv("OPENROUTER_API_KEY")
            if not or_key:
                ui.console.print(
                    "\n[yellow]⚠️  OPENROUTER_API_KEY is not configured in your environment.[/yellow]"
                )
                ui.console.print("  Get an OpenRouter key at: https://openrouter.ai/keys")
                ui.console.print('  Then select it: export OPENROUTER_API_KEY="your_key"')

            fallback_options = [
                "Google Gemini Free Tier (https://generativelanguage.googleapis.com)",
                "OpenRouter Free Models (https://openrouter.ai/api/v1)",
            ]

            ui.console.print("\nAvailable free fallback endpoints:")
            selected_fallbacks = []
            for i, opt in enumerate(fallback_options, 1):
                ui.console.print(f"  [green]{i}[/]: {opt}")

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
                        ui.console.print("  [green]✓[/] Registered Gemini Free fallback node")
                    else:
                        ui.console.print(
                            "  [red]✗[/] Failed to register Gemini Free fallback node (OmniRoute not running)"
                        )
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
                        ui.console.print("  [green]✓[/] Registered OpenRouter Free fallback node")
                    else:
                        ui.console.print("  [red]✗[/] Failed to register OpenRouter Free node")
    except (KeyboardInterrupt, EOFError):
        pass

    if not use_auto:
        if not running_providers:
            ui.console.print(
                "\n[bold yellow]⚠️  No active providers or routers running on this machine.[/bold yellow]"
            )
            ui.console.print("To run OmniRoute (centralized router recommended for Verdict):")
            ui.console.print("  [bold]npm install -g omniroute[/bold]")
            ui.console.print("  [bold]omniroute serve[/bold]\n")

            try:
                should_manual = Prompt.ask(
                    "Would you like to manually configure Verdict right now anyway?", default="y"
                )
                if not should_manual.lower().startswith("y"):
                    ui.console.print(
                        "\n[yellow]Setup cancelled. Please start your provider/router and try again.[/yellow]"
                    )
                    return
            except (KeyboardInterrupt, EOFError):
                ui.console.print("\n[yellow]Setup input interrupted.[/yellow]")
                return

        ui.section("Manual configuration")
        try:
            config["primary_model"] = Prompt.ask(
                "[bold]Primary model[/bold] (Tier-0, never offloaded)",
                default="anthropic/claude-3-opus-20240229",
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
            ui.console.print("\n[yellow]Manual configuration input interrupted.[/yellow]")
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

    ui.console.print(f"\n[bold green]✓ Saved configuration to {config_path}![/bold green]")
    ui.console.print("[dim]Configuration contents:[/dim]")
    ui.console.print(yaml.dump(config, default_flow_style=False))


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
            primary_model=raw.get("primary_model", "anthropic/claude-3-opus-20240229"),
            providers=providers,
            log_path=raw.get("log_path", "verdict-decisions.jsonl"),
            allow_offline=allow_offline,
        )
    providers = {"public_ollama": ProviderConfig(base_url="http://localhost:11434/v1")}
    providers.update(omniroute)
    return Gate(
        primary_model="anthropic/claude-3-opus-20240229",
        providers=providers,
        allow_offline=allow_offline,
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

    tier_colors = {0: "red", 1: "magenta", 2: "yellow", 3: "green"}
    t_color = tier_colors.get(dec.tier, "white")

    output = f"""[bold]Task:[/bold] {task[:100]}{"..." if len(task) > 100 else ""}

[bold]Decision:[/bold]
  Model:     [bold {t_color}]{dec.model}[/bold {t_color}]
  Provider:  {dec.provider}
  Tier:      T{dec.tier}
  Outcome:   {dec.decision}
  Managed:   {dec.managed_backend_status}
  Transport: {dec.transport_outcome}
  Quality:   {dec.quality_outcome}
  Protected: {str(dec.protected).lower()}
  Degraded:  {str(dec.degraded_mode).lower()}
  Latency:   [cyan]{dec.latency_ms:.1f}ms[/cyan]
  Strategy:  [bold]{selection.strategy}[/bold]

[bold dim]Reason:[/bold dim] [italic]{dec.reason}[/italic]
"""
    console.print(
        Panel(
            output,
            title="[bold blue]Routing Decision[/bold blue]",
            border_style="blue",
            expand=False,
        )
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
    if not os.path.exists(log_path):
        console.print(f"[yellow]No log file found at {log_path}[/yellow]")
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

    table = Table(title="Tier Distribution")
    table.add_column("Tier", style="bold")
    table.add_column("Count")
    table.add_column("Pct")

    for t in sorted(tiers):
        count = tiers[t]
        pct = (count / total) * 100 if total > 0 else 0
        table.add_row(f"T{t}", str(count), f"{pct:.1f}%")

    console.print("\n")
    console.print(table)
    console.print(f"\n[bold]Total Requests:[/bold] {total}")
    console.print(f"[bold]P50 Latency:[/bold] [cyan]{avg_latency:.2f}ms[/cyan]\n")

    console.print("[bold]Top Routed Models:[/bold]")
    for mod, count in sorted(models.items(), key=lambda x: x[1], reverse=True)[:5]:
        console.print(f"  {mod}: [bold yellow]{count}[/bold yellow] calls")


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
    import json

    console.print(Panel.fit("[bold green]Verdict Cost and Usage Report[/bold green]"))

    log_path = "verdict-decisions.jsonl"
    if not os.path.exists(log_path):
        console.print("[yellow]No routing telemetry found (Verdict decision log missing).[/yellow]")
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

    table = Table(title="Usage Summary")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="magenta")
    table.add_row("Total Routing Requests", str(total_requests))
    table.add_row("T0 (Critical) Forwarded", str(t0_requests))
    table.add_row("Offloaded Tasks (T1-T3)", str(total_requests - t0_requests))

    savings = (total_requests - t0_requests) * 0.005
    table.add_row("Estimated Savings vs T0 Only", f"${savings:.2f}")

    console.print(table)


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
            console.print(format_detection_report(result, verbose=verbose))
            console.print("\n[bold]Gateways (HTTP-validated):[/bold]")
            if healthy_gateways:
                for g in healthy_gateways:
                    console.print(
                        f"  [green]✓[/green] {g.display_name} ({g.identity}) at {g.url} "
                        f"[dim](port {g.port})[/dim]"
                    )
                if len(healthy_gateways) > 1:
                    console.print(f"[yellow]{multi_gateway_message}[/yellow]")
            else:
                console.print(f"[yellow]{no_gateway_message}[/yellow]")
                console.print(
                    "[dim]To start OmniRoute: npm install -g omniroute && omniroute serve[/dim]"
                )
    except Exception as e:
        console.print(f"[bold red]Detection failed: {e}[/bold red]")
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
        console.print_json(encoded)


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
            console.print(f"[bold red]{message}[/bold red]")
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

    table = Table(title=f"Verdict probe  ({_redact(base_url)})")
    table.add_column("Model", style="cyan")
    table.add_column("Status")
    table.add_column("HTTP")
    table.add_column("Latency (ms)")
    for entry in results:
        ok = entry.get("ok")
        status = "[green]LIVE[/green]" if ok else f"[red]DOWN[/red] {entry.get('error', '')}"
        table.add_row(
            str(entry["model"]),
            status,
            str(entry.get("http_status", "-")),
            str(entry.get("latency_ms", "-")),
        )
    console.print(table)
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
            console.print(f"[bold red]{message}[/bold red]")
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
        console.print_json(json.dumps(report_payload))
    if not report.passed or (reconciliation is not None and not reconciliation.passed):
        sys.exit(1)


def cmd_suggest(log_path: str = "verdict-decisions.jsonl") -> None:
    """Run the SuggestionService to propose evidence-backed improvements."""
    from rich.console import Console
    from rich.panel import Panel

    from verdict.suggestions import SuggestionService

    console = Console()
    svc = SuggestionService(log_path=log_path)

    with console.status("[bold green]Mining telemetry for suggestions...", spinner="dots"):
        suggestions = svc.generate_suggestions()

    if not suggestions:
        console.print(
            "[yellow]No actionable suggestions found. Your routing is optimized![/yellow]"
        )
        return

    console.print(
        Panel.fit("[bold blue]Verdict Intelligence Suggestions[/bold blue]", border_style="blue")
    )

    for s in suggestions:
        category_color = {"performance": "cyan", "reliability": "red", "capacity": "yellow"}.get(
            s.category, "white"
        )
        output = f"""[bold {category_color}]{s.title} ({s.id})[/]
[dim]Category:[/] {s.category.title()}  |  [dim]Novelty:[/] {s.novelty}  |  [dim]Expires In:[/] {s.expiry}

{s.description}

[bold dim]Proposed Next Experiment:[/bold dim]
[italic]{s.proposed_next_experiment}[/italic]

[dim]Confidence:[/] {s.confidence * 100:.1f}%  |  [dim]Impact:[/] {s.expected_impact}
[dim]Evidence Events (Top 3):[/] {", ".join(s.evidence_references) if s.evidence_references else "None"}
"""
        console.print(output)
        console.print("---")


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
    ui.console.print(
        f"  • Capability coverage: [cyan]{covered}/{total}[/] covered (bootstrap view)"
    )

    issues_found = []
    fixed_issues = []

    from verdict.documentation_preflight import run_documentation_preflight

    documentation_report = run_documentation_preflight(fix=fix)
    ui.console.print(
        "  • Documentation preflight: "
        f"[{'green' if documentation_report.passed else 'red'}]"
        f"{documentation_report.status}[/] "
        f"({documentation_report.inventory} documents, "
        f"{documentation_report.ingested} ingested, "
        f"{documentation_report.stale} stale, "
        f"{documentation_report.missing} missing)"
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
            ui.console.print(
                f"  • Configured Primary Model: [cyan]{primary_model}[/] (Tier-{tier})"
            )

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
    ui.console.print(
        "  [dim]See .env.example in the repository root for the full environment "
        "variable reference.[/dim]"
    )

    # 2. OmniRoute nodes check
    existing_nodes = _omniroute_api_request("GET", "/api/provider-nodes")
    if existing_nodes is None:
        ui.console.print(
            "[dim]OmniRoute server is not currently running/reachable to check nodes.[/dim]"
        )
    else:
        items = []
        if isinstance(existing_nodes, list):
            items = existing_nodes
        elif isinstance(existing_nodes, dict) and "items" in existing_nodes:
            items = existing_nodes["items"]

        ui.console.print(
            f"  • Connected to OmniRoute: [green]OK[/] (Found {len(items)} configured node endpoints)"
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
            ui.console.print(
                "\n[yellow]⚠️  Duplicate provider nodes detected in local OmniRoute database:[/yellow]"
            )
            for node_id, name, url, original_id in duplicates:
                ui.console.print(
                    f"  • Node [red]{name}[/] ({node_id}) is a duplicate of node ({original_id}) on URL: {url}"
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
                            ui.console.print(f"  [green]✓[/] Removed duplicate node: {name}")
                            fixed_issues.append(f"Removed duplicate node {node_id}")
                        else:
                            ui.console.print(f"  [red]✗[/] Failed to remove node {node_id}")
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
            print(payload["selected_because"])
            if exc.exclusions:
                print(
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
    print(human_summary(receipt))


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
        if not items:
            print("no routing receipts found")
            return
        for item in items:
            print(
                f"{item['receipt_id']} scope={item['scope']} "
                f"attempt={item.get('attempt_id')} state={item.get('state')}"
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
        print(human_summary(receipt))
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
            console.print(f"[yellow]{message}[/yellow]")
        raise SystemExit(3) from exc
    db_path = os.environ.get("VERDICT_MEMORY_DB", str(Path.home() / ".verdict" / "memory.db"))
    try:
        session = ExecutionSession.resume(session_id, MemoryPlane(db_path))
    except ExecutionSessionError as exc:
        message = f"no recorded session found for id: {session_id} ({exc})"
        if output_json:
            print(json.dumps({"status": "missing", "message": message}, sort_keys=True))
        else:
            console.print(f"[bold red]{message}[/bold red]")
        raise SystemExit(1) from exc
    record = session.to_dict()
    if output_json:
        print(json.dumps(record, indent=2, sort_keys=True))
        return
    console.print(f"[bold cyan]Execution session {session_id}[/bold cyan]")
    console.print(
        f"  State: {record['state']}  |  Model: {record['model_id']}  |  "
        f"Steps: {len(record['steps'])} completed: {len(record['completed_steps'])}"
    )
    console.print(f"  Task: {record['task_spec']}")


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
    table = Table(title="Verdict pre-execution simulation")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="magenta")
    table.add_row("Model", f"{forecast.model} ({forecast.provider}, T{forecast.tier})")
    table.add_row("Prompt tokens", str(forecast.prompt_tokens))
    table.add_row("Completion tokens", str(forecast.completion_tokens))
    table.add_row("Total tokens", str(forecast.total_tokens))
    table.add_row("Est. cost", f"${forecast.cost_usd:.6f}")
    table.add_row("Risk score", f"{forecast.risk_score} / 100")
    table.add_row("Capacity confidence", f"{forecast.capacity_confidence:.2f}")
    console.print(table)
    console.print(f"[dim]{forecast.rationale}[/dim]")


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

    primary = str(raw.get("primary_model", "anthropic/claude-3-opus-20240229"))
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
            console.print(json.dumps(docs_report.to_dict(), indent=2, sort_keys=True))
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
        console.print(
            f"[bold green]✓ Memory record put: {rec.key} (ns: {rec.namespace})[/bold green]"
        )
    elif sub == "search":
        results = plane.search(
            args.query, namespace=getattr(args, "namespace", None), limit=getattr(args, "limit", 10)
        )
        console.print(f"[bold cyan]Found {len(results)} memory record(s):[/bold cyan]")
        for r in results:
            console.print(f"- [{r.namespace}:{r.key}] ({r.source}): {r.content[:100]}")
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
        console.print(f"[bold green]✓ Exported memory manifest to {destination}[/bold green]")
    elif sub == "import":
        from verdict.memory_adapters import ImportPolicy, import_manifest

        man = args.manifest
        source = Path(man).expanduser().resolve()
        policy = ImportPolicy((source.parent,))
        manifest_records, import_report = import_manifest(source, policy=policy)
        count = plane.import_records(manifest_records)
        console.print(
            f"[bold green]✓ Imported {count[0]} record(s) ({import_report.duplicates} duplicates; "
            f"manifest {import_report.manifest_hash})[/bold green]"
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
                console.print(json.dumps(payload["report"], indent=2, sort_keys=True))
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
            console.print(json.dumps(payload["report"], indent=2, sort_keys=True))
        return
    elif sub == "graph":
        db = getattr(args, "db", "code_graph.db")
        graph_adapter = CodeGraphAdapter()
        graph_rep = graph_adapter.ingest_sqlite(
            db, plane, allow_legacy_sqlite=args.allow_legacy_sqlite
        )
        console.print(
            f"[bold green]✓ Code graph ingested {graph_rep.records_created} node(s)[/bold green]"
        )
    elif sub == "setup":
        report = detect_available_tools()
        tools_to_config = getattr(args, "tools", None)
        if not tools_to_config:
            tools_to_config = list(report.preselected_tools)
        else:
            tools_to_config = [t.strip() for t in tools_to_config.split(",") if t.strip()]

        console.print(
            f"[bold cyan]Detected available AI tools:[/bold cyan] {list(report.preselected_tools)}"
        )
        console.print(f"[bold cyan]Configuring memory bridge for:[/bold cyan] {tools_to_config}")

        res = configure_memory_bridge(tools_to_config, plane)
        console.print(f"[bold green]✓ Configured tools: {res['configured_tools']}[/bold green]")
        console.print(f"[bold green]✓ Memory database ready: {res['memory_db_path']}[/bold green]")

    else:
        console.print("[bold yellow]Use --help to view memory subcommands.[/bold yellow]")


def cmd_uninstall(purge_data: bool = False) -> None:
    """Reversibly uninstall memory bridge hooks and MCP registrations."""
    from verdict.memory_bridge import uninstall_memory_bridge

    res = uninstall_memory_bridge(home_dir=Path.home(), cwd=Path.cwd(), purge_data=purge_data)
    console.print(f"[bold green]✓ Uninstalled targets: {res['uninstalled_targets']}[/bold green]")
    if purge_data:
        console.print("[bold yellow]⚠ Purged .verdict memory data directory.[/bold yellow]")


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
            console.print(f"[bold red]Runtime operation blocked:[/] {exc}")
        raise SystemExit(2) from exc

    if output_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        console.print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
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

    if not os.path.exists(config_path):
        console.print(
            f"[bold red]❌ Configuration file (verdict.yaml) is missing at {config_path}.[/bold red]"
        )
        sys.exit(1)

    try:
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}
    except Exception as exc:
        console.print(
            f"[bold red]❌ Configuration file is corrupted/invalid YAML: {exc}[/bold red]"
        )
        sys.exit(1)

    has_issue = False

    primary_model = config.get("primary_model")
    if not primary_model:
        console.print("[bold red]❌ No primary model configured in verdict.yaml.[/bold red]")
        has_issue = True
    else:
        from verdict.classifier import classify

        tier = classify(primary_model)
        console.print(f"✓ Configured Primary Model: [cyan]{primary_model}[/] (Tier-{tier})")

    providers = config.get("providers", {})
    if not isinstance(providers, dict):
        console.print("[bold red]❌ 'providers' section in verdict.yaml is malformed.[/bold red]")
        has_issue = True
    else:
        urls: dict[str, str] = {}
        for name, p_cfg in providers.items():
            if not isinstance(p_cfg, dict):
                console.print(
                    f"[bold red]❌ Provider '{name}' config is not a dictionary.[/bold red]"
                )
                has_issue = True
                continue
            base_url = p_cfg.get("base_url", "")
            if "sk-" in base_url or "api_key" in base_url.lower():
                console.print(
                    f"[bold red]❌ Literal API key detected inside host URL for provider '{name}'.[/bold red]"
                )
                has_issue = True

            if base_url:
                url = base_url.rstrip("/")
                if url in urls:
                    console.print(
                        f"[bold red]❌ Duplicate host URL configured in verdict.yaml: provider '{name}' and '{urls[url]}' have identical hosts: {url}[/bold red]"
                    )
                    has_issue = True
                else:
                    urls[url] = name

    if has_issue:
        console.print("[bold red]❌ Config validation failed with issues.[/bold red]")
        sys.exit(1)

    console.print("[bold green]✓ Configuration file is valid.[/bold green]")


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
            console.print(f"[bold cyan]Recall: {len(results)} record(s)[/bold cyan]")
            for r in results:
                console.print(f"- [{r.namespace}:{r.key}] ({r.source}): {r.content[:120]}")

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
            console.print(f"[bold green]✓ Recorded [{namespace}:{key}][/bold green]")
        else:
            console.print(
                f"[bold red]✗ Rejected [{namespace}:{key}]: {write_res.reason}[/bold red]"
            )

    elif hook_cmd == "configure":
        tools_str = getattr(args, "tools", None)
        tools = [t.strip() for t in tools_str.split(",")] if tools_str else ["codex", "claude"]
        res = configure_memory_bridge(selected_tools=tools)
        if getattr(args, "json", False):
            print(json.dumps(res, indent=2))
        else:
            console.print("[bold green]✓ Memory bridge configured.[/bold green]")
            console.print(f"  DB: {res['memory_db_path']}")
            console.print(f"  Targets: {', '.join(res['configured_tools'])}")

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
                icon = "✅" if v else "⚠️"
                console.print(f"{icon} {k}: {v}")


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
            console.print(
                "[bold green]✅ Verdict MCP server initialized across tool environments.[/bold green]"
            )
            console.print(f"Memory DB: {res['memory_db_path']}")
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
            if registered:
                console.print(
                    "[bold green]✅ Verdict MCP server is registered in .mcp.json[/bold green]"
                )
            else:
                console.print(
                    "[yellow]⚠️ Verdict MCP server is not registered in .mcp.json[/yellow]"
                )


def _stdout_is_tty() -> bool:
    return sys.stdout.isatty()


def main() -> None:
    parser = argparse.ArgumentParser(description="Verdict: policy-gated LLM Router")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    setup_cli_p = subparsers.add_parser("setup", help="Plan or apply the interactive setup wizard")
    setup_cli_p.add_argument(
        "setup_action",
        nargs="?",
        choices=["plan", "intelligence", "gateways", "harnesses"],
        help="Read-only setup operation or capability scope",
    )
    setup_cli_p.add_argument(
        "--dry-run", action="store_true", help="Build a mutation-free setup plan"
    )
    setup_cli_p.add_argument(
        "--plan", action="store_true", help="Mutation-free capability bootstrap plan"
    )
    setup_cli_p.add_argument(
        "--recommended",
        action="store_true",
        help="Show recommended enrichment set (still mutation-free without --apply)",
    )
    setup_cli_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    setup_cli_p.add_argument(
        "--non-interactive",
        action="store_true",
        help="Headless/CI mode: no prompts; APPLY only via --allow ( --yes is not enough)",
    )
    setup_cli_p.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Apply authorized bootstrap actions "
            "(interactive: --yes; non-interactive: --allow provider ids)"
        ),
    )
    setup_cli_p.add_argument(
        "--yes",
        action="store_true",
        help=(
            "Interactive final consent for the shown APPLY plan; "
            "ignored as blanket auth under --non-interactive (use --allow)"
        ),
    )
    setup_cli_p.add_argument(
        "--allow",
        dest="allowlist",
        action="append",
        default=[],
        help=(
            "Explicit allowlist provider id (repeatable), e.g. gateway.omniroute; "
            "required for non-interactive APPLY"
        ),
    )
    setup_cli_p.add_argument(
        "--rollback",
        action="store_true",
        help="Roll back Verdict-owned bootstrap APPLY records (ownership/backups; no TUI)",
    )
    setup_cli_p.add_argument(
        "--rollback-action",
        dest="rollback_actions",
        action="append",
        default=[],
        help="Limit rollback to a managed action_id (repeatable)",
    )
    setup_cli_p.add_argument(
        "--state-dir",
        default=None,
        help="Bootstrap ownership state directory (default: ~/.verdict/bootstrap)",
    )

    route_p = subparsers.add_parser("route", help="Route a single prompt/task")
    route_p.add_argument("task", help="Task description or prompt text")
    route_p.add_argument("--terse", action="store_true", help="Output ONLY the target model string")
    route_p.add_argument(
        "--criticality", default="medium", choices=["critical", "high", "medium", "low"]
    )
    route_p.add_argument(
        "--allow-offline",
        action="store_true",
        help=(
            "Decide from the static catalog only — no network discovery or probes. "
            "Does not enable the BOD-127 legacy selector escape (use --allow-legacy-selector)."
        ),
    )
    route_p.add_argument(
        "--allow-legacy-selector",
        action="store_true",
        help=(
            "BOD-127 migration escape: allow pre-BOD-104 selectors. "
            "Required explicitly; --allow-offline alone never enables this. "
            "Production serve must omit this and supply execution_path_decision."
        ),
    )

    compare_p = subparsers.add_parser(
        "compare", help="Compare a DIRECT frontier call vs the Verdict route for one task"
    )
    compare_p.add_argument("task", help="Task description or prompt text")
    compare_p.add_argument(
        "--criticality", default="medium", choices=["critical", "high", "medium", "low"]
    )
    compare_p.add_argument(
        "--allow-offline",
        action="store_true",
        help="Decide from the static catalog only — no network discovery or probes",
    )

    autodev_p = subparsers.add_parser(
        "autodev", help="Decompose an objective, execute each unit on a cheap route, and verify"
    )
    autodev_p.add_argument("--objective", help="What the run must accomplish")
    autodev_p.add_argument("--repo", default=".", help="Repository to work in (default: .)")
    autodev_p.add_argument(
        "--orchestrator-model",
        default=None,
        help="Optional model assertion; route is supplied by the BOD-104 ExecutionPathDecision",
    )
    autodev_p.add_argument(
        "--executor-model",
        default=None,
        help="Optional exact model assertion; the BOD-104 decision supplies the worker route",
    )
    autodev_p.add_argument(
        "--base-url",
        default=None,
        help="Optional gateway assertion; BOD-104 decision supplies the route",
    )
    autodev_p.add_argument("--json", action="store_true", help="Emit the machine-readable report")
    autodev_p.add_argument(
        "--allow-live",
        action="store_true",
        help="Consent to live model calls and working-tree edits",
    )
    autodev_p.add_argument(
        "--no-mechanical",
        action="store_true",
        help="Disable the zero-token deterministic tier and send every unit to a model",
    )
    autodev_p.add_argument(
        "--dry-run", action="store_true", help="Show the plan and its cost without executing"
    )
    autodev_p.add_argument(
        "--execution-path-request",
        default=None,
        help=(
            "public execution-path request JSON; the in-process optimizer "
            "decision is the launch authority (BOD-104)"
        ),
    )
    packet_p = autodev_p.add_subparsers(dest="autodev_action")
    packet_root = packet_p.add_parser("packet", help="Portable packet operations")
    packet_actions = packet_root.add_subparsers(dest="packet_action", required=True)
    for action in ("create", "inspect", "validate", "resume", "execute"):
        action_p = packet_actions.add_parser(action)
        action_p.add_argument("--packet", required=True)
        action_p.add_argument("--json", action="store_true")
        if action == "create":
            action_p.add_argument("--from", dest="source_path", required=True)
        if action == "resume":
            action_p.add_argument("--model", required=True)
        if action == "execute":
            action_p.add_argument(
                "--execution-path-request",
                default=None,
                help=(
                    "public execution-path request JSON; the in-process optimizer "
                    "decision is the launch authority (BOD-104)"
                ),
            )
            action_p.add_argument("--repo", required=True)
            action_p.add_argument(
                "--allow-live",
                action="store_true",
                help="consent: executes through the gateway and edits the working tree",
            )
            action_p.add_argument(
                "--prefer-non-primary",
                action="store_true",
                help="first attempt must use a concrete non-primary admitted route",
            )
            action_p.add_argument(
                "--primary-fallback",
                default=None,
                help="concrete route that currently occupies the primary-subscription role",
            )
            action_p.add_argument(
                "--base-url",
                dest="packet_base_url",
                default=None,
                help="override gateway family base URL for this packet run",
            )
            action_p.add_argument(
                "--canary",
                dest="canary_path",
                default=None,
                help="JSON canary state; chosen applies only among admitted_ids",
            )
            action_p.add_argument(
                "--delegation",
                choices=["legwork", "decision"],
                default=None,
                help=(
                    "required classification for this unit; a decision must also "
                    "carry --undelegable-reason"
                ),
            )
            action_p.add_argument(
                "--undelegable-reason",
                dest="undelegable_reason",
                default=None,
                help="capability that makes a --delegation decision unable to run non-primary",
            )
    shadow_p = packet_actions.add_parser(
        "shadow", help="Dump advisory shadow-learning JSON without calling eligibility"
    )
    shadow_p.add_argument("--episodes", required=True, help="JSON list of trusted episodes")
    shadow_p.add_argument("--json", action="store_true")
    canary_p = packet_actions.add_parser(
        "canary", help="Dump explicit bounded canary choice or rollback without eligibility"
    )
    canary_p.add_argument("--episodes", help="JSON list of trusted episodes")
    canary_p.add_argument("--admitted", help="JSON list of already-admitted identities")
    canary_p.add_argument("--rollback", help="JSON canary state to restore baseline")
    canary_p.add_argument("--json", action="store_true")
    compare_p = packet_actions.add_parser("compare", help="Compare two family-run JSON objects")
    compare_p.add_argument("--packet", required=True)
    compare_p.add_argument("--json", action="store_true")
    compare_p.add_argument("--a", dest="family_a_path", required=True)
    compare_p.add_argument("--b", dest="family_b_path", required=True)

    golden_p = subparsers.add_parser(
        "autodev-golden-path",
        help="Run offline discovery, durable memory, and bounded verification",
    )
    golden_p.add_argument("--objective", required=True, help="Bounded mission objective")
    golden_p.add_argument("--repo", required=True, help="Real Git repository to inspect")
    golden_p.add_argument("--memory-path", default=".verdict-golden-memory.db")
    golden_p.add_argument("--verify", nargs="+", default=["git", "status", "--short"])
    golden_p.add_argument("--owned-path", action="append", default=[])
    golden_p.add_argument("--timeout", type=float, default=10.0)
    golden_p.add_argument("--json", action="store_true")

    stats_p = subparsers.add_parser("stats", help="View routing analytics")
    stats_p.add_argument("--log_path", default="verdict-decisions.jsonl")

    benchmark_p = subparsers.add_parser(
        "benchmark", help="Run the reproducible local benchmark harness"
    )
    benchmark_p.add_argument("--fixture", default="benchmarks/fixtures/reproducible.json")
    benchmark_p.add_argument("--output-json", default=None)
    benchmark_p.add_argument("--allow-live-provider", action="store_true")
    benchmark_p.add_argument("--live-provider", default=None)
    benchmark_p.add_argument(
        "--savings",
        action="store_true",
        help=(
            "Run the paired legit-task savings bench (we measure). Without --live-paired "
            "this is a labeled simulation that cannot claim savings."
        ),
    )
    benchmark_p.add_argument(
        "--live-paired",
        action="store_true",
        help=(
            "Execute both arms of every --savings task against OMNIROUTE_BASE_URL "
            "(OMNIROUTE_API_KEY) and bind cost/identity/quality to the execution receipts"
        ),
    )

    quickstart_p = subparsers.add_parser(
        "quickstart", help="Run the credential-free deterministic flagship quickstart"
    )
    quickstart_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    quickstart_p.add_argument(
        "--non-interactive", action="store_true", help="Do not prompt for input"
    )
    quickstart_p.add_argument(
        "--dry-run", action="store_true", help="Run the read-only quickstart fixture"
    )

    subparsers.add_parser("ui", help="Launch the Streamlit analytics dashboard")

    serve_p = subparsers.add_parser("serve", help="Launch the FastAPI microservice")
    serve_p.add_argument("--port", type=int, default=8000)
    serve_p.add_argument(
        "--host", default=None, help="Bind address (anonymous mode must be loopback)"
    )
    serve_p.add_argument("--dev", action="store_true", help="Enable hot-reload development mode")

    # New: detect command
    detect_p = subparsers.add_parser("detect", help="Detect available LLM providers")
    detect_p.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    detect_p.add_argument("--json", action="store_true", help="Output JSON")
    detect_p.add_argument(
        "--offline",
        action="store_true",
        help="Deterministic offline mode: no network, no credentials",
    )
    detect_p.add_argument("--config", action="store_true", help="Generate suggested Verdict config")

    certify_p = subparsers.add_parser(
        "certify", help="Emit runtime certification passport JSON (BOD-92 evidence only)"
    )
    certify_p.add_argument(
        "--from",
        dest="certify_from",
        default=None,
        help="Path to DetectedSnapshot JSON fixtures (offline; no live probes)",
    )
    certify_p.add_argument(
        "--json",
        action="store_true",
        default=True,
        help="Output JSON (default; certification is machine-readable)",
    )

    # New: probe command (1-token liveness test before assigning work)
    probe_p = subparsers.add_parser("probe", help="Run a 1-token liveness probe against models")
    probe_p.add_argument(
        "models", nargs="+", help="Model IDs to probe (e.g. openrouter/tencent/hy3:free)"
    )
    probe_p.add_argument(
        "--base-url",
        default="http://localhost:20128/v1",
        help="OpenAI-compatible base URL (default: local OmniRoute)",
    )
    probe_p.add_argument("--timeout", type=float, default=20.0, help="Per-probe timeout seconds")
    probe_p.add_argument(
        "--allow-live-probe",
        action="store_true",
        help="Explicitly consent to network liveness probes",
    )
    probe_p.add_argument("--json", action="store_true", help="Output JSON")

    catalog_p = subparsers.add_parser(
        "catalog", help="Qualify and optionally store a sanitized OmniRoute catalog snapshot"
    )
    catalog_p.add_argument(
        "--base-url", default="http://127.0.0.1:20128", help="OmniRoute base URL"
    )
    catalog_p.add_argument(
        "--management",
        action="store_true",
        help="Use only the documented management endpoint (default fetches both projections)",
    )
    catalog_p.add_argument(
        "--expected-rows",
        type=int,
        default=0,
        help="Exact row count required to qualify (0 = any well-formed non-empty catalog)",
    )
    catalog_p.add_argument("--freshness-seconds", type=int, default=3600)
    catalog_p.add_argument("--db-path", default=None, help="Store qualification in a memory DB")
    catalog_p.add_argument(
        "--probe",
        action="store_true",
        help="Run a bounded liveness sample after catalog qualification",
    )
    catalog_p.add_argument("--probe-limit", type=int, default=16)
    catalog_p.add_argument("--probe-timeout", type=float, default=20.0)
    catalog_p.add_argument(
        "--allow-live-probe",
        action="store_true",
        help="Explicitly consent to network liveness probes",
    )
    catalog_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    metadata_p = subparsers.add_parser(
        "metadata", help="Refresh and inspect Core's independent model metadata store (BOD-108)"
    )
    metadata_sub = metadata_p.add_subparsers(dest="metadata_command", required=True)
    metadata_refresh_p = metadata_sub.add_parser(
        "refresh", help="Fetch models.dev + LiteLLM into the on-disk Core store"
    )
    metadata_refresh_p.add_argument(
        "--store",
        dest="store_path",
        default=None,
        help="Store path (default ~/.verdict/model-metadata.json)",
    )
    metadata_refresh_p.add_argument("--mapping", dest="mapping_path", default=None)
    metadata_refresh_p.add_argument(
        "--models-dev-api-file", default=None, help="Offline models.dev api.json fixture"
    )
    metadata_refresh_p.add_argument(
        "--models-dev-models-file", default=None, help="Offline models.dev models.json fixture"
    )
    metadata_refresh_p.add_argument(
        "--litellm-file", default=None, help="Offline LiteLLM JSON fixture"
    )
    metadata_refresh_p.add_argument(
        "--include-p1",
        action="store_true",
        help="Record P1 skip reasons (AA/Arena/OpenLLM/BFCL); scores stored only from fixtures",
    )
    metadata_refresh_p.add_argument(
        "--json", action="store_true", help="Output machine-readable JSON"
    )
    metadata_show_p = metadata_sub.add_parser(
        "show", help="Summarize the on-disk Core metadata store"
    )
    metadata_show_p.add_argument("--store", dest="store_path", default=None)
    metadata_show_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    metadata_lookup_p = metadata_sub.add_parser(
        "lookup", help="Look up one OmniRoute id in the Core store (named drop if unmapped)"
    )
    metadata_lookup_p.add_argument("omniroute_id")
    metadata_lookup_p.add_argument("--store", dest="store_path", default=None)
    metadata_lookup_p.add_argument("--mapping", dest="mapping_path", default=None)
    metadata_lookup_p.add_argument(
        "--requires", default="", help="Comma-separated required caps (unknown → named drop)"
    )
    metadata_lookup_p.add_argument(
        "--json", action="store_true", help="Output machine-readable JSON"
    )

    suggest_p = subparsers.add_parser(
        "suggest", help="Review intelligence suggestions from past outcomes"
    )
    suggest_p.add_argument("--log_path", default="verdict-decisions.jsonl")

    doctor_p = subparsers.add_parser(
        "doctor", help="Scan and repair system configuration and connectivity issues"
    )
    doctor_p.add_argument(
        "--fix", action="store_true", help="Automatically repair detected configuration issues"
    )
    doctor_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    harness_p = subparsers.add_parser(
        "harness", help="Point a coding-agent harness at Verdict without hand-editing its config"
    )
    harness_sub = harness_p.add_subparsers(dest="harness_target", required=True)
    harness_codex_p = harness_sub.add_parser(
        "codex", help="Enable, disable, or inspect Codex as a Verdict OpenAI-compatible client"
    )
    harness_codex_sub = harness_codex_p.add_subparsers(dest="harness_codex_command", required=True)
    harness_enable_p = harness_codex_sub.add_parser(
        "enable", help="Backup ~/.codex/config.toml and set model_provider = verdict"
    )
    harness_enable_p.add_argument(
        "--base-url",
        default=CODEX_HARNESS_DEFAULT_BASE_URL,
        help="Verdict OpenAI-compatible base URL (default: http://127.0.0.1:8000/v1)",
    )
    harness_enable_p.add_argument(
        "--token-env",
        default=CODEX_HARNESS_DEFAULT_TOKEN_ENV,
        help="Env var Codex should read for the bearer token (default: LLMGATE_AUTH_TOKEN; never printed)",
    )
    harness_enable_p.add_argument(
        "--force",
        action="store_true",
        help="Write Codex config even if the Verdict health check fails",
    )
    harness_codex_sub.add_parser(
        "disable", help="Restore the pre-enable ~/.codex/config.toml backup"
    )
    harness_codex_sub.add_parser(
        "status", help="Show active Codex provider, base URL, and whether the token env is set"
    )
    harness_hermes_p = harness_sub.add_parser(
        "hermes", help="Enable, disable, or inspect Hermes as a Verdict OpenAI-compatible client"
    )
    harness_hermes_sub = harness_hermes_p.add_subparsers(
        dest="harness_hermes_command", required=True
    )
    hermes_enable_p = harness_hermes_sub.add_parser(
        "enable", help="Backup ~/.hermes/config.yaml and point model.provider at Verdict"
    )
    hermes_enable_p.add_argument(
        "--base-url",
        default=HERMES_HARNESS_DEFAULT_BASE_URL,
        help="Verdict OpenAI-compatible base URL (default: http://127.0.0.1:8000/v1)",
    )
    hermes_enable_p.add_argument(
        "--token-env",
        default=HERMES_HARNESS_DEFAULT_TOKEN_ENV,
        help="Env var Hermes should read for the bearer token (default: LLMGATE_AUTH_TOKEN)",
    )
    hermes_enable_p.add_argument(
        "--model",
        default=HERMES_HARNESS_DEFAULT_MODEL,
        help="Default model id to set under model.default",
    )
    hermes_enable_p.add_argument(
        "--force",
        action="store_true",
        help="Write Hermes config even if the Verdict health check fails",
    )
    harness_hermes_sub.add_parser(
        "disable", help="Restore the pre-enable ~/.hermes/config.yaml backup"
    )
    harness_hermes_sub.add_parser(
        "status", help="Show active Hermes provider, base URL, and whether the token env is set"
    )
    harness_claude_p = harness_sub.add_parser(
        "claude", help="Enable, disable, discover, or certify Claude Code as a Verdict client"
    )
    harness_claude_sub = harness_claude_p.add_subparsers(
        dest="harness_claude_command", required=True
    )
    harness_claude_sub.add_parser(
        "discover", help="Observe Claude Code install/config without mutating it"
    )
    claude_enable_p = harness_claude_sub.add_parser(
        "enable",
        help="Backup ~/.claude/settings.json and point OpenAI-compatible traffic at Verdict",
    )
    claude_enable_p.add_argument(
        "--base-url",
        default=CLAUDE_HARNESS_DEFAULT_BASE_URL,
        help="Verdict OpenAI-compatible base URL (default: http://127.0.0.1:8000/v1)",
    )
    claude_enable_p.add_argument(
        "--token-env",
        default=CLAUDE_HARNESS_DEFAULT_TOKEN_ENV,
        help="Env var Claude/OpenAI tooling should read for the bearer token (never printed)",
    )
    claude_enable_p.add_argument(
        "--force",
        action="store_true",
        help="Write Claude settings even if the Verdict health check fails",
    )
    harness_claude_sub.add_parser(
        "disable", help="Restore the pre-enable ~/.claude/settings.json backup"
    )
    harness_claude_sub.add_parser(
        "status",
        help="Show Claude Code Verdict base URL, gate hook, and whether the token env is set",
    )
    claude_certify_p = harness_claude_sub.add_parser(
        "certify",
        help="Evidence-only Claude Code certification (partial until BOD-102 Messages proxy)",
    )
    claude_certify_p.add_argument(
        "--force",
        action="store_true",
        help="Treat Verdict health as ok when probing fails (local proof only)",
    )
    harness_cursor_p = harness_sub.add_parser(
        "cursor",
        help="Enable, disable, discover, or certify Cursor as a Verdict OpenAI-compatible client",
    )
    harness_cursor_sub = harness_cursor_p.add_subparsers(
        dest="harness_cursor_command", required=True
    )
    harness_cursor_sub.add_parser(
        "discover", help="Observe Cursor install/config without mutating it"
    )
    cursor_enable_p = harness_cursor_sub.add_parser(
        "enable", help="Write Verdict-managed Cursor provider config aimed at Verdict :8000"
    )
    cursor_enable_p.add_argument(
        "--base-url",
        default=CURSOR_HARNESS_DEFAULT_BASE_URL,
        help="Verdict OpenAI-compatible base URL (default: http://127.0.0.1:8000/v1)",
    )
    cursor_enable_p.add_argument(
        "--token-env",
        default=CURSOR_HARNESS_DEFAULT_TOKEN_ENV,
        help="Env var Cursor/OpenAI tooling should read for the bearer token (never printed)",
    )
    cursor_enable_p.add_argument(
        "--force",
        action="store_true",
        help="Write Cursor config even if the Verdict health check fails",
    )
    cursor_enable_p.add_argument(
        "--wrapper",
        action="store_true",
        help="Also install ~/.cursor/bin/cursor-verdict wrapper (last-resort integration)",
    )
    harness_cursor_sub.add_parser(
        "disable", help="Restore pre-enable Cursor provider/settings/wrapper backups"
    )
    harness_cursor_sub.add_parser(
        "status", help="Show Cursor Verdict provider, base URL, and whether the token env is set"
    )
    cursor_certify_p = harness_cursor_sub.add_parser(
        "certify", help="Evidence-only Cursor certification (partial; IDE UI toggle is NEEDS_OWNER)"
    )
    cursor_certify_p.add_argument(
        "--force",
        action="store_true",
        help="Treat Verdict health as ok when probing fails (local proof only)",
    )
    harness_prime_p = harness_sub.add_parser(
        "prime", help="Enable, disable, discover, or certify Prime Agent as a Verdict client"
    )
    harness_prime_sub = harness_prime_p.add_subparsers(dest="harness_prime_command", required=True)
    harness_prime_sub.add_parser(
        "discover", help="Observe Prime Agent install/config without mutating it"
    )
    prime_enable_p = harness_prime_sub.add_parser(
        "enable",
        help="Backup ~/.prime/agent/models.json and upsert Verdict OpenAI-compatible provider",
    )
    prime_enable_p.add_argument(
        "--base-url",
        default=PRIME_HARNESS_DEFAULT_BASE_URL,
        help="Verdict OpenAI-compatible base URL (default: http://127.0.0.1:8000/v1)",
    )
    prime_enable_p.add_argument(
        "--token-env",
        default=PRIME_HARNESS_DEFAULT_TOKEN_ENV,
        help="Env var name stored as apiKey (never prints the value)",
    )
    prime_enable_p.add_argument(
        "--force",
        action="store_true",
        help="Write Prime models.json even if the Verdict health check fails",
    )
    harness_prime_sub.add_parser(
        "disable", help="Restore the pre-enable ~/.prime/agent/models.json backup"
    )
    harness_prime_sub.add_parser(
        "status",
        help="Show Prime Agent Verdict provider, base URL, and whether the token env is set",
    )
    prime_certify_p = harness_prime_sub.add_parser(
        "certify",
        help="Evidence-only Prime Agent certification (partial; not-installed when binary missing)",
    )
    prime_certify_p.add_argument(
        "--force",
        action="store_true",
        help="Treat Verdict health as ok when probing fails (local proof only)",
    )
    harness_opencode_p = harness_sub.add_parser(
        "opencode",
        help="Enable, disable, discover, or certify OpenCode as a Verdict OpenAI-compatible client",
    )
    harness_opencode_sub = harness_opencode_p.add_subparsers(
        dest="harness_opencode_command", required=True
    )
    harness_opencode_sub.add_parser(
        "discover", help="Observe OpenCode install/config without mutating it"
    )
    opencode_enable_p = harness_opencode_sub.add_parser(
        "enable",
        help="Backup ~/.config/opencode/opencode.json and upsert Verdict OpenAI-compatible provider",
    )
    opencode_enable_p.add_argument(
        "--base-url",
        default=OPENCODE_HARNESS_DEFAULT_BASE_URL,
        help="Verdict OpenAI-compatible base URL (default: http://127.0.0.1:8000/v1)",
    )
    opencode_enable_p.add_argument(
        "--token-env",
        default=OPENCODE_HARNESS_DEFAULT_TOKEN_ENV,
        help="Env var name recorded for OpenCode auth (never prints the value)",
    )
    opencode_enable_p.add_argument(
        "--force",
        action="store_true",
        help="Write OpenCode config even if the Verdict health check fails",
    )
    harness_opencode_sub.add_parser(
        "disable", help="Restore the pre-enable ~/.config/opencode/opencode.json backup"
    )
    harness_opencode_sub.add_parser(
        "status", help="Show OpenCode Verdict provider, base URL, and whether the token env is set"
    )
    opencode_certify_p = harness_opencode_sub.add_parser(
        "certify",
        help="Evidence-only OpenCode certification (partial; not-installed when binary missing)",
    )
    opencode_certify_p.add_argument(
        "--force",
        action="store_true",
        help="Treat Verdict health as ok when probing fails (local proof only)",
    )
    harness_cline_p = harness_sub.add_parser(
        "cline",
        help="Discover, enable, disable, status, or certify Cline as a Verdict OpenAI-compatible client",
    )
    harness_cline_sub = harness_cline_p.add_subparsers(dest="harness_cline_command", required=True)
    harness_cline_sub.add_parser(
        "discover", help="Report whether Cline CLI/IDE config is present and where it points"
    )
    cline_enable_p = harness_cline_sub.add_parser(
        "enable", help="Backup Cline config and point OpenAI-compatible base URL at Verdict :8000"
    )
    cline_enable_p.add_argument(
        "--base-url",
        default=CLINE_HARNESS_DEFAULT_BASE_URL,
        help="Verdict OpenAI-compatible base URL (default: http://127.0.0.1:8000/v1)",
    )
    cline_enable_p.add_argument(
        "--token-env",
        default=CLINE_HARNESS_DEFAULT_TOKEN_ENV,
        help="Env var Cline should read for the bearer token (default: LLMGATE_AUTH_TOKEN; never printed)",
    )
    cline_enable_p.add_argument(
        "--force",
        action="store_true",
        help="Write Cline config even if the Verdict health check fails",
    )
    harness_cline_sub.add_parser(
        "disable", help="Restore pre-enable Cline provider/settings/providers.json backups"
    )
    harness_cline_sub.add_parser(
        "status", help="Show Cline install state, base URL, and whether the token env is set"
    )
    cline_certify_p = harness_cline_sub.add_parser(
        "certify", help="Emit Cline harness parity facets (partial while IDE secrets need owner)"
    )
    cline_certify_p.add_argument(
        "--force", action="store_true", help="Treat Verdict health as ok for local certify proof"
    )

    runtime_p = subparsers.add_parser(
        "runtime", help="Inspect and safely reconcile optional global runtime ownership records"
    )
    runtime_sub = runtime_p.add_subparsers(dest="runtime_command", required=True)
    runtime_status_p = runtime_sub.add_parser("status", help="Report runtime ownership status")
    runtime_status_p.add_argument("--json", action="store_true", help="Output JSON")
    runtime_explain_p = runtime_sub.add_parser(
        "explain", help="Report observed runtime capability and health evidence"
    )
    runtime_explain_p.add_argument("--json", action="store_true", help="Output JSON")
    runtime_reconcile_p = runtime_sub.add_parser(
        "reconcile", help="Plan or explicitly apply duplicate-service reconciliation"
    )
    runtime_reconcile_p.add_argument(
        "--plan",
        action="store_true",
        help="Perform a read-only deterministic plan (the default when --apply is absent)",
    )
    runtime_reconcile_p.add_argument("--apply", action="store_true", help="Apply planned stops")
    runtime_reconcile_p.add_argument(
        "--yes", action="store_true", help="Explicit consent required with --apply"
    )
    runtime_reconcile_p.add_argument(
        "--service",
        dest="service_ids",
        action="append",
        help="Limit apply to this exact service id; repeat for multiple services",
    )
    runtime_reconcile_p.add_argument("--json", action="store_true", help="Output JSON")

    prove_p = subparsers.add_parser(
        "prove-at-rest", help="Prove free-tier ∩ active OmniRoute models at rest (daemon or once)"
    )
    prove_sub = prove_p.add_subparsers(dest="prove_command", required=True)
    prove_once_p = prove_sub.add_parser("once", help="Run one prove-at-rest cycle and exit")
    prove_daemon_p = prove_sub.add_parser(
        "daemon", help="Continuously prove free∩active identities at rest"
    )
    prove_status_p = prove_sub.add_parser("status", help="Show the latest persisted proof state")
    for _prove_p in (prove_once_p, prove_daemon_p, prove_status_p):
        _prove_p.add_argument(
            "--state-path",
            default=None,
            help="Proof state JSON path (default: ~/.verdict/prove-at-rest/state.json)",
        )
        _prove_p.add_argument("--json", action="store_true", help="Output JSON")
    for _prove_live_p in (prove_once_p, prove_daemon_p):
        _prove_live_p.add_argument(
            "--base-url", default=None, help="OmniRoute origin (default: OMNIROUTE_BASE_URL)"
        )
        _prove_live_p.add_argument(
            "--interval",
            type=float,
            default=300.0,
            help="Daemon interval seconds between cycles (daemon only; default 300)",
        )
        _prove_live_p.add_argument(
            "--timeout", type=float, default=15.0, help="Per-identity probe timeout seconds"
        )
        _prove_live_p.add_argument(
            "--allow-live-probe",
            action="store_true",
            help="Explicit consent to network prove-at-rest probes",
        )
    uninst_p = subparsers.add_parser(
        "uninstall", help="Reversibly uninstall Verdict memory bridge hooks and MCP registrations"
    )
    uninst_p.add_argument(
        "--purge-data", action="store_true", help="Purge .verdict memory database directory"
    )
    subparsers.add_parser("check", help="Validate system configuration file syntax and sanity")

    compat_p = subparsers.add_parser(
        "compat", help="Cross-repo contract compatibility manifest and gate (ADR-024)"
    )
    compat_sub = compat_p.add_subparsers(dest="compat_command")
    compat_manifest_p = compat_sub.add_parser(
        "manifest", help="Print the current verdict-core contract compatibility manifest"
    )
    compat_manifest_p.add_argument("--json", action="store_true", help="Output JSON")
    compat_check_p = compat_sub.add_parser(
        "check",
        help="Check a downstream repo's declared manifest against the current one, failing closed",
    )
    compat_check_p.add_argument(
        "--declared", required=True, help="Path to the downstream repo's declared manifest JSON"
    )
    compat_check_p.add_argument("--json", action="store_true", help="Output JSON")

    memory_p = subparsers.add_parser("memory", help="Local-first unified memory management")
    memory_sub = memory_p.add_subparsers(dest="memory_command")

    put_p = memory_sub.add_parser("put", help="Put a record into memory")
    put_p.add_argument("key", help="Key for memory record")
    put_p.add_argument("content", help="Content of memory record")
    put_p.add_argument("--namespace", default="default", help="Namespace")
    put_p.add_argument("--source", default="cli", help="Source provenance")

    srch_p = memory_sub.add_parser("search", help="Search memory records")
    srch_p.add_argument("query", help="Query text")
    srch_p.add_argument("--namespace", default=None, help="Namespace filter")
    srch_p.add_argument("--limit", type=int, default=10, help="Max results")

    exp_p = memory_sub.add_parser("export", help="Export memory manifest")
    exp_p.add_argument("--output", default="memory_manifest.json", help="Output file")

    imp_p = memory_sub.add_parser("import", help="Import memory manifest")
    imp_p.add_argument("manifest", help="Manifest JSON file")

    md_p = memory_sub.add_parser("masterdocs", help="Canonicalize MasterDocs database")
    md_p.add_argument("--db", default="MasterDocsRAG.db", help="Database path")
    md_p.add_argument(
        "--allow-legacy-sqlite",
        action="store_true",
        help="Explicitly allow an exported local SQLite artifact (prefer manifests)",
    )
    md_p.add_argument("--dry-run", action="store_true", help="Canonicalize without writing memory")
    md_p.add_argument("--limit", type=int, default=1000, help="Maximum source rows to inspect")
    md_p.add_argument(
        "--ingest-timestamp",
        type=float,
        default=None,
        help="Stable provenance timestamp (defaults to deterministic zero)",
    )
    md_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    cg_p = memory_sub.add_parser("graph", help="Ingest code review graph database")
    cg_p.add_argument("--db", default="code_graph.db", help="Database path")
    cg_p.add_argument(
        "--allow-legacy-sqlite",
        action="store_true",
        help="Explicitly allow an exported local SQLite artifact (prefer manifests)",
    )

    docs_p = memory_sub.add_parser(
        "docs",
        help="Verify or ingest authoritative project and optional external runtime documentation",
    )
    docs_p.add_argument(
        "--fix", action="store_true", help="Fetch and ingest missing/stale documents"
    )
    docs_p.add_argument("--repo-root", default=str(Path.cwd()), help="Repository root")
    docs_p.add_argument("--db-path", default=None, help="Shared memory database path")
    docs_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    setup_p = memory_sub.add_parser(
        "setup", help="Autopilot wizard to connect tools (Codex, Claude, Pi) to Unified Memory"
    )
    setup_p.add_argument(
        "--tools", default=None, help="Comma-separated tools to configure (default: auto-detected)"
    )

    mcp_p = subparsers.add_parser(
        "mcp", help="Manage and run Model Context Protocol (MCP) stdio server"
    )
    mcp_sub = mcp_p.add_subparsers(dest="mcp_command", required=True)
    mcp_sub.add_parser("serve", help="Launch the stdio MCP JSON-RPC server")
    mcp_init_p = mcp_sub.add_parser(
        "init", help="Configure Verdict MCP server across host tool environments"
    )
    mcp_init_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    mcp_status_p = mcp_sub.add_parser("status", help="Report active MCP registrations")
    mcp_status_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    hook_p = subparsers.add_parser(
        "hook", help="Manage Verdict lifecycle hooks for Codex and Claude"
    )
    hook_sub = hook_p.add_subparsers(dest="hook_command", required=True)
    hook_recall_p = hook_sub.add_parser("recall", help="Search memory for prior context")
    hook_recall_p.add_argument("query", help="Search query")
    hook_recall_p.add_argument("--limit", type=int, default=5, help="Max results")
    hook_recall_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    hook_record_p = hook_sub.add_parser("record", help="Record a session/event memory entry")
    hook_record_p.add_argument("key", help="Memory key")
    hook_record_p.add_argument("value", help="Memory value/content")
    hook_record_p.add_argument("--namespace", default="sessions", help="Namespace")
    hook_record_p.add_argument("--source", default="cli", help="Source provenance")
    hook_configure_p = hook_sub.add_parser(
        "configure", help="Configure memory bridge for Codex and Claude"
    )
    hook_configure_p.add_argument(
        "--tools", default=None, help="Comma-separated tools (default: codex,claude)"
    )
    hook_configure_p.add_argument(
        "--json", action="store_true", help="Output machine-readable JSON"
    )
    hook_status_p = hook_sub.add_parser("status", help="Show hook and MCP registration status")
    hook_status_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    hook_status_p.add_argument("--db-path", default=None, help="Shared memory database path")
    hook_gate_p = hook_sub.add_parser(
        "claude-gate",
        help="Fail-closed catalog check for Claude Code / Codex SessionStart hooks (exit 2 if blocked)",
    )
    hook_gate_p.add_argument(
        "--base-url",
        default="http://127.0.0.1:20128",
        help="OmniRoute or OpenAI-compatible gateway base URL",
    )

    run_p = subparsers.add_parser("run", help="Route a single prompt/task (alias of route)")
    run_p.add_argument("task", help="Task description or prompt text")
    run_p.add_argument("--terse", action="store_true", help="Output ONLY the target model string")
    run_p.add_argument(
        "--criticality", default="medium", choices=["critical", "high", "medium", "low"]
    )

    plan_p = subparsers.add_parser("plan", help="Print a mutation-free setup plan")
    plan_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    choose_p = subparsers.add_parser(
        "choose", help="Choose an eligible execution target for Prime dispatch"
    )
    choose_p.add_argument(
        "--task-class",
        required=True,
        dest="task_class",
        help="Task class (implementation, architecture, ...)",
    )
    choose_p.add_argument(
        "--requires", default="", help="Comma-separated required capabilities (example: tools,code)"
    )
    choose_p.add_argument(
        "--model",
        default=None,
        help="Explicit model identity; eligible selection wins, ineligible fails closed",
    )
    choose_p.add_argument(
        "--candidates-json",
        default=None,
        help="JSON file of CandidateEvidence fixtures (required for P0)",
    )
    choose_p.add_argument(
        "--json", action="store_true", help="Output machine-readable JSON receipt"
    )

    models_p = subparsers.add_parser(
        "models", help="List the qualified model catalog used for routing and simulation"
    )
    models_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    inspect_p = subparsers.add_parser("inspect", help="Inspect one model's catalog record")
    inspect_p.add_argument("model_id", help="Model ID to inspect")
    inspect_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    receipt_p = subparsers.add_parser(
        "receipt", help="Inspect durable RoutingReceiptV1 records (BOD-144)"
    )
    receipt_sub = receipt_p.add_subparsers(dest="receipt_action", required=True)
    receipt_list_p = receipt_sub.add_parser("list", help="List routing receipts")
    receipt_list_p.add_argument("--scope", default=None, help="Optional receipt scope filter")
    receipt_list_p.add_argument(
        "--db",
        dest="db_path",
        default=None,
        help="ReceiptStore sqlite path (default: .verdict/receipts.db)",
    )
    receipt_list_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    receipt_show_p = receipt_sub.add_parser("show", help="Show one routing receipt")
    receipt_show_p.add_argument("receipt_id", nargs="?", default=None, help="Receipt id")
    receipt_show_p.add_argument("--attempt", dest="attempt_id", default=None, help="Attempt id")
    receipt_show_p.add_argument("--scope", default=None, help="Receipt scope")
    receipt_show_p.add_argument(
        "--db", dest="db_path", default=None, help="ReceiptStore sqlite path"
    )
    receipt_show_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    receipt_export_p = receipt_sub.add_parser("export", help="Export one routing receipt as JSON")
    receipt_export_p.add_argument("receipt_id", nargs="?", default=None, help="Receipt id")
    receipt_export_p.add_argument("--attempt", dest="attempt_id", default=None, help="Attempt id")
    receipt_export_p.add_argument("--scope", default=None, help="Receipt scope")
    receipt_export_p.add_argument(
        "--db", dest="db_path", default=None, help="ReceiptStore sqlite path"
    )

    replay_p = subparsers.add_parser("replay", help="Replay a recorded execution session")
    replay_p.add_argument("session_id", help="Session ID to replay")
    replay_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    failover_p = subparsers.add_parser(
        "failover-proof", help="Run the offline forced-failover and replay proof"
    )
    failover_p.add_argument(
        "--memory-path",
        default=".verdict-failover-memory.db",
        help="MemoryPlane database path for the replayable session",
    )
    failover_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    simulate_p = subparsers.add_parser(
        "simulate", help="Forecast tokens, cost, risk, and model before any paid call"
    )
    simulate_p.add_argument("task", help="Task description or prompt text")
    simulate_p.add_argument(
        "--criticality", default="medium", choices=["critical", "high", "medium", "low"]
    )
    simulate_p.add_argument("--model", dest="model_override", default=None, help="Model override")
    simulate_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    subparsers.add_parser("cost-report", help="Estimate token cost from routing decision history")

    resume_p = subparsers.add_parser(
        "resume", help="Reconstruct durable story resume state (worktree + handoff + prompt)"
    )
    resume_p.add_argument("story", help="Linear story id (e.g. BOD-65)")
    resume_p.add_argument(
        "--with",
        dest="with_harness",
        choices=["claude", "codex", "cursor", "prime"],
        default=None,
        help="Optional harness launcher stub (records intent; does not exec yet)",
    )
    resume_p.add_argument(
        "--repo", default=".", help="Repository path used for worktree discovery (default: cwd)"
    )
    resume_p.add_argument(
        "--create",
        action="store_true",
        help="Create a story worktree when none exists (reattach-before-create still applies)",
    )
    resume_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    from verdict.orchestration import cli as _orchestration_cli

    _orchestration_cli.add_parsers(subparsers)
    args = parser.parse_args()
    _orchestration_rc = _orchestration_cli.dispatch(args)
    if _orchestration_rc is not None:
        raise SystemExit(_orchestration_rc)

    if args.command == "setup":
        scope = "all"
        if args.setup_action in {"intelligence", "gateways", "harnesses"}:
            scope = args.setup_action
        if getattr(args, "rollback", False):
            cmd_setup(
                rollback=True,
                rollback_actions=list(getattr(args, "rollback_actions", None) or []),
                output_json=args.json,
                state_dir=getattr(args, "state_dir", None),
            )
        elif args.setup_action == "plan" or args.plan:
            cmd_setup_plan(output_json=args.json, scope=scope, recommended=args.recommended)
        elif args.setup_action in {"intelligence", "gateways", "harnesses"} and not args.apply:
            # Bare scoped subcommands plan bootstrap for that scope (not the legacy wizard).
            cmd_setup_plan(output_json=args.json, scope=scope, recommended=True)
        else:
            cmd_setup(
                dry_run=args.dry_run,
                output_json=args.json,
                non_interactive=args.non_interactive,
                recommended=args.recommended,
                plan_only=False,
                scope=scope,
                allowlist=list(args.allowlist or []),
                consent=bool(args.yes),
                apply=bool(args.apply),
                state_dir=getattr(args, "state_dir", None),
            )
    elif args.command == "route":
        cmd_route(
            args.task,
            args.criticality,
            args.terse,
            allow_offline=getattr(args, "allow_offline", False),
            allow_legacy_selector=getattr(args, "allow_legacy_selector", None),
        )
    elif args.command == "autodev":
        if args.autodev_action == "packet":
            if args.packet_action == "shadow":
                cmd_autodev_packet_shadow(args.episodes, output_json=args.json)
            elif args.packet_action == "canary":
                if args.rollback:
                    cmd_autodev_packet_canary_rollback(args.rollback, output_json=args.json)
                else:
                    cmd_autodev_packet_canary(args.episodes, args.admitted, output_json=args.json)
            elif args.packet_action == "execute":
                decision = _cli_execution_path_decision(
                    parser, getattr(args, "execution_path_request", None), packet_path=args.packet
                )
                cmd_autodev_packet_execute(
                    args.packet,
                    getattr(args, "repo", "."),
                    output_json=args.json,
                    allow_live=getattr(args, "allow_live", False),
                    resume=True,
                    primary_fallback=getattr(args, "primary_fallback", None),
                    prefer_non_primary=getattr(args, "prefer_non_primary", False),
                    base_url=getattr(args, "packet_base_url", None),
                    canary_path=getattr(args, "canary_path", None),
                    delegation=getattr(args, "delegation", None),
                    undelegable_reason=getattr(args, "undelegable_reason", None),
                    execution_path_decision=decision,
                )
            else:
                cmd_autodev_packet(
                    args.packet_action,
                    args.packet,
                    source_path=getattr(args, "source_path", None),
                    model=getattr(args, "model", None),
                    output_json=args.json,
                    family_a_path=getattr(args, "family_a_path", None),
                    family_b_path=getattr(args, "family_b_path", None),
                )
        else:
            if not args.objective:
                parser.error("verdict autodev requires --objective unless using packet operations")
            cmd_autodev(
                args.objective,
                args.repo,
                orchestrator_model=args.orchestrator_model,
                executor_model=args.executor_model,
                base_url=args.base_url,
                output_json=args.json,
                allow_live=args.allow_live,
                no_mechanical=args.no_mechanical,
                dry_run=args.dry_run,
                execution_path_decision=_cli_execution_path_decision(
                    parser, getattr(args, "execution_path_request", None), task=args.objective
                ),
            )
    elif args.command == "autodev-golden-path":
        cmd_autodev_golden_path(
            args.objective,
            args.repo,
            args.memory_path,
            args.verify,
            args.timeout,
            args.owned_path,
            args.json,
        )
    elif args.command == "stats":
        cmd_stats(args.log_path)
    elif args.command == "benchmark":
        cmd_benchmark(
            args.fixture,
            args.output_json,
            allow_live_provider=args.allow_live_provider,
            live_provider=args.live_provider,
            savings=args.savings,
            live_paired=args.live_paired,
        )
    elif args.command == "quickstart":
        cmd_quickstart(
            output_json=args.json, non_interactive=args.non_interactive, dry_run=args.dry_run
        )
    elif args.command == "ui":
        try:
            # Resolve the path dynamically without executing the file
            import importlib.util
            import subprocess
            import sys

            spec = importlib.util.find_spec("verdict.dashboard")
            if not spec or not spec.origin:
                console.print("[bold red]❌ Dashboard module missing.[/bold red]")
                sys.exit(1)
            subprocess.run([sys.executable, "-m", "streamlit", "run", spec.origin])

        except ImportError:
            console.print("[bold red]❌ UI dependencies not found.[/bold red]")
            console.print("Please install the UI package suite:")
            console.print('  [bold cyan]pipx install "verdict-core[all]" --force[/bold cyan]')
            sys.exit(1)
    elif args.command == "serve":
        try:
            from verdict.api import start_server

            if args.dev:
                os.environ["LLMGATE_AVAILABILITY_PROFILE"] = "development"
                console.print(
                    "[bold cyan]🔥 Dev mode: hot-reload enabled "
                    "(LLMGATE_AVAILABILITY_PROFILE=development)[/bold cyan]"
                )
            start_server(args.port, args.host, reload=args.dev)
        except ImportError:
            console.print("[bold red]❌ Server dependencies not found.[/bold red]")
            console.print("Please install the FastAPI server suite:")
            console.print('  [bold cyan]pipx install "verdict-core[all]" --force[/bold cyan]')
            sys.exit(1)
    elif args.command == "probe":
        cmd_probe(
            args.models,
            base_url=args.base_url,
            timeout=args.timeout,
            output_json=args.json,
            allow_live_probe=args.allow_live_probe,
        )
    elif args.command == "catalog":
        cmd_catalog(
            base_url=args.base_url,
            management=args.management,
            expected_rows=args.expected_rows,
            freshness_seconds=args.freshness_seconds,
            db_path=args.db_path,
            probe=args.probe,
            probe_limit=args.probe_limit,
            probe_timeout=args.probe_timeout,
            output_json=args.json,
            allow_live_probe=args.allow_live_probe,
        )
    elif args.command == "detect":
        cmd_detect(
            verbose=args.verbose,
            output_json=args.json,
            output_config=args.config,
            offline=args.offline,
        )
    elif args.command == "certify":
        cmd_certify(
            snapshot_path=getattr(args, "certify_from", None),
            output_json=getattr(args, "json", True),
        )
    elif args.command == "suggest":
        cmd_suggest(args.log_path)
    elif args.command == "doctor":
        cmd_doctor(fix=getattr(args, "fix", False), output_json=getattr(args, "json", False))
    elif args.command == "harness":
        if args.harness_target == "codex":
            cmd_harness_codex(
                args.harness_codex_command,
                base_url=getattr(args, "base_url", CODEX_HARNESS_DEFAULT_BASE_URL),
                token_env=getattr(args, "token_env", CODEX_HARNESS_DEFAULT_TOKEN_ENV),
                force=getattr(args, "force", False),
            )
        elif args.harness_target == "hermes":
            cmd_harness_hermes(
                args.harness_hermes_command,
                base_url=getattr(args, "base_url", HERMES_HARNESS_DEFAULT_BASE_URL),
                token_env=getattr(args, "token_env", HERMES_HARNESS_DEFAULT_TOKEN_ENV),
                model=getattr(args, "model", HERMES_HARNESS_DEFAULT_MODEL),
                force=getattr(args, "force", False),
            )
        elif args.harness_target == "claude":
            cmd_harness_claude(
                args.harness_claude_command,
                base_url=getattr(args, "base_url", CLAUDE_HARNESS_DEFAULT_BASE_URL),
                token_env=getattr(args, "token_env", CLAUDE_HARNESS_DEFAULT_TOKEN_ENV),
                force=getattr(args, "force", False),
            )
        elif args.harness_target == "cursor":
            cmd_harness_cursor(
                args.harness_cursor_command,
                base_url=getattr(args, "base_url", CURSOR_HARNESS_DEFAULT_BASE_URL),
                token_env=getattr(args, "token_env", CURSOR_HARNESS_DEFAULT_TOKEN_ENV),
                force=getattr(args, "force", False),
                wrapper=getattr(args, "wrapper", False),
            )
        elif args.harness_target == "prime":
            cmd_harness_prime(
                args.harness_prime_command,
                base_url=getattr(args, "base_url", PRIME_HARNESS_DEFAULT_BASE_URL),
                token_env=getattr(args, "token_env", PRIME_HARNESS_DEFAULT_TOKEN_ENV),
                force=getattr(args, "force", False),
            )
        elif args.harness_target == "opencode":
            cmd_harness_opencode(
                args.harness_opencode_command,
                base_url=getattr(args, "base_url", OPENCODE_HARNESS_DEFAULT_BASE_URL),
                token_env=getattr(args, "token_env", OPENCODE_HARNESS_DEFAULT_TOKEN_ENV),
                force=getattr(args, "force", False),
            )
        elif args.harness_target == "cline":
            cmd_harness_cline(
                args.harness_cline_command,
                base_url=getattr(args, "base_url", CLINE_HARNESS_DEFAULT_BASE_URL),
                token_env=getattr(args, "token_env", CLINE_HARNESS_DEFAULT_TOKEN_ENV),
                force=getattr(args, "force", False),
            )
        else:
            raise SystemExit(f"unknown harness: {args.harness_target}")
    elif args.command == "runtime":
        cmd_runtime(
            args.runtime_command,
            apply=getattr(args, "apply", False),
            consent=getattr(args, "yes", False),
            service_ids=getattr(args, "service_ids", None),
            output_json=getattr(args, "json", False),
        )
    elif args.command == "prove-at-rest":
        cmd_prove_at_rest(
            args.prove_command,
            base_url=getattr(args, "base_url", None),
            state_path=getattr(args, "state_path", None),
            interval=getattr(args, "interval", 300.0),
            timeout=getattr(args, "timeout", 15.0),
            allow_live_probe=getattr(args, "allow_live_probe", False),
            output_json=getattr(args, "json", False),
        )
    elif args.command == "uninstall":
        cmd_uninstall(purge_data=getattr(args, "purge_data", False))
    elif args.command == "check":
        cmd_check()
    elif args.command == "compat":
        cmd_compat(
            getattr(args, "compat_command", None),
            getattr(args, "declared", None),
            getattr(args, "json", False),
        )
    elif args.command == "memory":
        cmd_memory(args)
    elif args.command == "mcp":
        cmd_mcp(args)
    elif args.command == "hook":
        cmd_hook(args)
    elif args.command == "run":
        cmd_run(args.task, args.criticality, args.terse)
    elif args.command == "plan":
        cmd_plan(output_json=args.json)
    elif args.command == "choose":
        cmd_choose(
            task_class=args.task_class,
            requires=args.requires,
            model=args.model,
            candidates_json=args.candidates_json,
            output_json=args.json,
        )
    elif args.command == "models":
        cmd_models(output_json=args.json)
    elif args.command == "inspect":
        cmd_inspect(args.model_id, output_json=args.json)
    elif args.command == "receipt":
        cmd_receipt(
            args.receipt_action,
            receipt_id=getattr(args, "receipt_id", None),
            attempt_id=getattr(args, "attempt_id", None),
            scope=getattr(args, "scope", None),
            db_path=getattr(args, "db_path", None),
            output_json=bool(getattr(args, "json", False)),
        )
    elif args.command == "replay":
        cmd_replay(args.session_id, output_json=args.json)
    elif args.command == "simulate":
        cmd_simulate(
            args.task, args.criticality, model_override=args.model_override, output_json=args.json
        )
    elif args.command == "failover-proof":
        cmd_failover_proof(memory_path=args.memory_path, output_json=args.json)
    elif args.command == "metadata":
        cmd_metadata(args)
    elif args.command == "cost-report":
        cmd_cost_report()
    elif args.command == "resume":
        cmd_resume(
            args.story,
            with_harness=getattr(args, "with_harness", None),
            output_json=args.json,
            repo=Path(args.repo),
            create_if_missing=bool(getattr(args, "create", False)),
        )
    elif args.command is None and _stdout_is_tty() and os.getenv("VERDICT_PLAIN") != "1":
        # Interactive terminals get the Verdict home screen; pipes, CI and tests
        # keep the historical argparse help contract.
        from verdict.home import run_home

        raise SystemExit(run_home())
    else:
        parser.print_help()


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
    from verdict.harness_codex import HarnessCodexError, disable, enable, format_status, status

    try:
        if command == "enable":
            result = enable(base_url=base_url, token_env=token_env, force=force)
            console.print("[bold green]Codex harness enabled[/bold green]")
            console.print("  provider: verdict")
            console.print(f"  base_url: {result.base_url}")
            console.print(f"  token_env: {result.token_env}")
            if result.created_backup:
                console.print(f"  backup: {result.backup_path}")
            else:
                console.print(f"  config: {result.config_path}")
            return
        if command == "disable":
            disable()
            console.print("[bold green]Codex harness disabled[/bold green]")
            console.print("  restored pre-enable ~/.codex/config.toml backup")
            return
        if command == "status":
            console.print(format_status(status()), end="")
            return
    except HarnessCodexError as exc:
        console.print(f"[bold red]{exc}[/bold red]")
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
    from verdict.harness_hermes import HarnessHermesError, disable, enable, format_status, status

    try:
        if command == "enable":
            result = enable(base_url=base_url, token_env=token_env, model=model, force=force)
            console.print("[bold green]Hermes harness enabled[/bold green]")
            console.print("  provider: Verdict")
            console.print(f"  base_url: {result.base_url}")
            console.print(f"  model: {result.model}")
            console.print(f"  token_env: {result.token_env}")
            if result.created_backup:
                console.print(f"  backup: {result.backup_path}")
            else:
                console.print(f"  config: {result.config_path}")
            return
        if command == "disable":
            disable()
            console.print("[bold green]Hermes harness disabled[/bold green]")
            console.print("  restored pre-enable ~/.hermes/config.yaml backup")
            return
        if command == "status":
            console.print(format_status(status()), end="")
            return
    except HarnessHermesError as exc:
        console.print(f"[bold red]{exc}[/bold red]")
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
        format_certify,
        format_discover,
        format_status,
        status,
    )

    try:
        if command == "discover":
            console.print(format_discover(discover()), end="")
            return
        if command == "enable":
            result = enable(base_url=base_url, token_env=token_env, force=force)
            console.print("[bold green]Claude Code harness enabled[/bold green]")
            console.print(f"  integration: {result.integration}")
            console.print(f"  base_url: {result.base_url}")
            console.print(f"  token_env: {result.token_env}")
            if result.created_backup:
                console.print(f"  backup: {result.backup_path}")
            else:
                console.print(f"  config: {result.config_path}")
            return
        if command == "disable":
            disable()
            console.print("[bold green]Claude Code harness disabled[/bold green]")
            console.print("  restored pre-enable ~/.claude/settings.json backup")
            return
        if command == "status":
            console.print(format_status(status()), end="")
            return
        if command == "certify":
            console.print(format_certify(certify(force=force)), end="")
            return
    except HarnessClaudeError as exc:
        console.print(f"[bold red]{exc}[/bold red]")
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
        format_certify,
        format_discover,
        format_status,
        status,
    )

    try:
        if command == "discover":
            console.print(format_discover(discover()), end="")
            return
        if command == "enable":
            result = enable(base_url=base_url, token_env=token_env, force=force, wrapper=wrapper)
            console.print("[bold green]Cursor harness enabled[/bold green]")
            console.print(f"  integration: {result.integration}")
            console.print(f"  base_url: {result.base_url}")
            console.print(f"  token_env: {result.token_env}")
            if result.created_backup:
                console.print(f"  backup: {result.backup_path}")
            else:
                console.print(f"  config: {result.config_path}")
            if result.wrapper_path is not None:
                console.print(f"  wrapper: {result.wrapper_path}")
            return
        if command == "disable":
            disable()
            console.print("[bold green]Cursor harness disabled[/bold green]")
            console.print("  restored pre-enable Cursor provider/settings/wrapper backups")
            return
        if command == "status":
            console.print(format_status(status()), end="")
            return
        if command == "certify":
            console.print(format_certify(certify(force=force)), end="")
            return
    except HarnessCursorError as exc:
        console.print(f"[bold red]{exc}[/bold red]")
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
    from verdict.harness_prime import (
        HarnessPrimeError,
        certify,
        disable,
        discover,
        enable,
        format_certify,
        format_discover,
        format_status,
        status,
    )

    try:
        if command == "discover":
            console.print(format_discover(discover()), end="")
            return
        if command == "enable":
            result = enable(base_url=base_url, token_env=token_env, force=force)
            console.print("[bold green]Prime Agent harness enabled[/bold green]")
            console.print(f"  integration: {result.integration}")
            console.print(f"  base_url: {result.base_url}")
            console.print(f"  token_env: {result.token_env}")
            if result.created_backup:
                console.print(f"  backup: {result.backup_path}")
            else:
                console.print(f"  config: {result.config_path}")
            return
        if command == "disable":
            disable()
            console.print("[bold green]Prime Agent harness disabled[/bold green]")
            console.print("  restored pre-enable ~/.prime/agent/models.json backup")
            return
        if command == "status":
            console.print(format_status(status()), end="")
            return
        if command == "certify":
            console.print(format_certify(certify(force=force)), end="")
            return
    except HarnessPrimeError as exc:
        console.print(f"[bold red]{exc}[/bold red]")
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
        format_certify,
        format_discover,
        format_status,
        status,
    )

    try:
        if command == "discover":
            console.print(format_discover(discover()), end="")
            return
        if command == "enable":
            result = enable(base_url=base_url, token_env=token_env, force=force)
            console.print("[bold green]OpenCode harness enabled[/bold green]")
            console.print(f"  integration: {result.integration}")
            console.print(f"  base_url: {result.base_url}")
            console.print(f"  model: {result.model}")
            console.print(f"  token_env: {result.token_env}")
            if result.created_backup:
                console.print(f"  backup: {result.backup_path}")
            else:
                console.print(f"  config: {result.config_path}")
            return
        if command == "disable":
            disable()
            console.print("[bold green]OpenCode harness disabled[/bold green]")
            console.print("  restored pre-enable ~/.config/opencode/opencode.json backup")
            return
        if command == "status":
            console.print(format_status(status()), end="")
            return
        if command == "certify":
            console.print(format_certify(certify(force=force)), end="")
            return
    except HarnessOpenCodeError as exc:
        console.print(f"[bold red]{exc}[/bold red]")
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
    from verdict.harness_cline import (
        HarnessClineError,
        certify,
        disable,
        discover,
        enable,
        format_certify,
        format_discover,
        format_status,
        status,
    )

    try:
        if command == "discover":
            console.print(format_discover(discover()), end="")
            return
        if command == "enable":
            result = enable(base_url=base_url, token_env=token_env, force=force)
            console.print("[bold green]Cline harness enabled[/bold green]")
            console.print(f"  integration: {result.integration}")
            console.print(f"  base_url: {result.base_url}")
            console.print(f"  token_env: {result.token_env}")
            if result.created_backup:
                console.print(f"  backup: {result.backup_path}")
            else:
                console.print(f"  config: {result.config_path}")
            if result.providers_json_path is not None:
                console.print(f"  providers_json: {result.providers_json_path}")
            if result.settings_path is not None:
                console.print(f"  settings: {result.settings_path}")
            for step in result.ui_steps:
                console.print(f"  ui: {step}")
            return
        if command == "disable":
            disable()
            console.print("[bold green]Cline harness disabled[/bold green]")
            console.print("  restored pre-enable Cline provider/settings/providers.json backups")
            return
        if command == "status":
            console.print(format_status(status()), end="")
            return
        if command == "certify":
            console.print(format_certify(certify(force=force)), end="")
            return
    except HarnessClineError as exc:
        console.print(f"[bold red]{exc}[/bold red]")
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
                console.print(f"[yellow]No prove-at-rest state at {resolved_state}[/yellow]")
            return
        payload = cycle.to_dict()
        payload["state_path"] = str(resolved_state)
        if output_json:
            print(json.dumps(payload, indent=2, sort_keys=True))
            return
        summary = cycle.summary
        console.print(
            f"[bold cyan]prove-at-rest[/bold cyan] cycle={cycle.cycle_id} "
            f"healthy={summary.get('healthy', 0)} failed={summary.get('failed', 0)} "
            f"skipped={summary.get('skipped', 0)}"
        )
        console.print(f"  state: {resolved_state}")
        for item in cycle.results:
            if item.status == "healthy":
                style = "green"
            elif item.status == "failed":
                style = "red"
            else:
                style = "yellow"
            detail = f" ({item.reason})" if item.reason else ""
            console.print(f"  [{style}]{item.status}[/{style}] {item.identity_id}{detail}")
        return

    if prove_command in {"once", "daemon"} and not allow_live_probe:
        message = "live prove-at-rest requires explicit consent; pass --allow-live-probe"
        if output_json:
            print(json.dumps({"error": message}, sort_keys=True))
        else:
            console.print(f"[bold red]{message}[/bold red]")
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
            console.print(f"[bold red]{message}[/bold red]")
        raise SystemExit(2) from exc

    if prove_command == "once":
        cycle = daemon.run_once()
        payload = cycle.to_dict()
        payload["state_path"] = str(resolved_state)
        if output_json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            summary = cycle.summary
            console.print(
                f"[bold green]prove-at-rest once[/bold green] "
                f"healthy={summary.get('healthy', 0)} failed={summary.get('failed', 0)} "
                f"skipped={summary.get('skipped', 0)}"
            )
            console.print(f"  wrote {resolved_state}")
        if cycle.summary.get("failed", 0):
            raise SystemExit(1)
        return

    if prove_command == "daemon":
        console.print(
            f"[bold cyan]prove-at-rest daemon[/bold cyan] interval={interval}s "
            f"state={resolved_state}"
        )

        def report_cycle_error(exc: Exception) -> None:
            code = getattr(exc, "code", exc.__class__.__name__)
            console.print(
                f"[bold red]prove-at-rest cycle failed[/bold red] code={code}: {exc}; "
                f"retaining last complete state and retrying in {interval}s"
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
                console.print("[yellow]prove-at-rest daemon stopped[/yellow]")
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
        from rich.console import Console

        console = Console()
        console.print("[bold green]Failover proof completed[/bold green]")
        console.print(f"  Session ID: {proof.mission_id}")
        console.print("  Initial model: provider-a/model-a")
        console.print(f"  Replacement model: {proof.replacement_model}")
        console.print(f"  Completed steps: {list(proof.completed_stages)}")
        console.print(f"  Digest: {proof.digest}")


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
        if offline:
            transport = file_transport(
                models_dev_api=_metadata_json_file(models_dev_api_file),
                models_dev_models=_metadata_json_file(models_dev_models_file),
                litellm=_metadata_json_file(litellm_file),
                fetched_at=clock.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            )
        else:
            transport = None
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
            console.print(f"[bold red]{exc}[/bold red]")
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
    console.print("[bold green]Core metadata store refreshed[/bold green]")
    console.print(f"  records: {report['record_count']}")
    console.print(f"  mapping drops: {report['drop_count']}")
    console.print(f"  store: {report['store']}")
    for name, status in snapshot.sources.items():
        console.print(
            f"  {name}: {status.status}" + (f" ({status.reason})" if status.reason else "")
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
            console.print(f"[bold red]{exc}[/bold red]")
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
    console.print(f"[bold cyan]Core metadata[/bold cyan] {snapshot.refreshed_at}")
    console.print(f"  records: {len(snapshot.records)}  store: {resolved}")


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
            console.print(f"[bold red]{exc}[/bold red]")
        raise SystemExit(1) from exc
    payload = found.to_dict()
    if output_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    if found.drop is not None:
        console.print(f"[yellow]named drop[/yellow] {found.drop.reason}: {omniroute_id}")
        if found.drop.detail:
            console.print(f"  {found.drop.detail}")
        return
    assert found.record is not None
    console.print(f"[green]mapped[/green] {omniroute_id} → {found.record.id}")
    for name, cited in found.provenance_for_receipt().items():
        console.print(
            f"  {name}: {cited.get('source')} {cited.get('version') or cited.get('fetched_at')}"
        )


if __name__ == "__main__":
    main()
