"""CLI entry point for Verdict."""

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from verdict.benchmarking import format_benchmark_report, run_reproducible_benchmarks
from verdict.gate import Gate
from verdict.models import ModelInfo, ProviderConfig, TaskSpec

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

    base_url = os.getenv("OMNIROUTE_BASE_URL")
    if not base_url:
        return None
    url = base_url.rstrip("/") + "/" + path.lstrip("/")

    import json
    import urllib.request
    from urllib.error import URLError

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
    """Prompt the user to select from a list of options."""
    for i, opt in enumerate(options, 1):
        console.print(f"  [green]{i}[/]: {opt}")
    while True:
        ask_val = Prompt.ask(prompt_text, default=default)
        choice = str(ask_val).strip() if ask_val is not None else ""
        try:
            val = int(choice)
            if 1 <= val <= len(options):
                return options[val - 1]
        except ValueError:
            if choice in options:
                return choice
        console.print("[yellow]Invalid choice. Please enter the number or the exact name.[/]")


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
    *, dry_run: bool = False, output_json: bool = False, non_interactive: bool = False
) -> None:
    """Interactive setup wizard or a mutation-free setup plan."""
    if dry_run or output_json or non_interactive:
        cmd_setup_plan(output_json=output_json)
        return

    # First, run auto-detection to show user what's available
    _print_detection_banner()
    detected_result = None
    try:
        from verdict.provider_detection import detect_all_providers, format_detection_report

        detected_result = detect_all_providers()
        console.print(format_detection_report(detected_result, verbose=False))
    except Exception as e:
        console.print(f"[yellow]Detection skipped: {e}[/yellow]")

    console.print(
        Panel.fit(
            "[bold blue]Verdict Setup Wizard[/bold blue]\nLet's configure your routing engine.",
            border_style="blue",
        )
    )

    config: dict[str, Any] = {}
    use_auto = False

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
        console.print("\n[bold cyan]Auto-detection found active providers![/bold cyan]")
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
                        console.print(
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
                console.print(
                    "\n[bold cyan]Syncing detected system providers to OmniRoute/9Router:[/bold cyan]"
                )
                for name, _p_name, url, _ in to_sync:
                    console.print(f"  • Found active [green]{name}[/]: [dim]{url}[/]")

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
                            console.print(
                                f"  [green]✓[/] Successfully registered node: {node_name}"
                            )
                        else:
                            console.print(f"  [red]✗[/] Failed to register node: {node_name}")
        except (KeyboardInterrupt, EOFError):
            pass

    # Prompt user about adding free providers like gemini/antigravity for local fallback routing
    try:
        console.print("\n[bold cyan]Fallback Models Configuration:[/bold cyan]")
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
                console.print(
                    "\n[yellow]⚠️  GEMINI_API_KEY is not configured in your environment.[/yellow]"
                )
                console.print("  Get a free Gemini API key at: https://aistudio.google.com/")
                console.print('  Then select it: export GEMINI_API_KEY="your_key"')

            or_key = os.getenv("OPENROUTER_API_KEY")
            if not or_key:
                console.print(
                    "\n[yellow]⚠️  OPENROUTER_API_KEY is not configured in your environment.[/yellow]"
                )
                console.print("  Get an OpenRouter key at: https://openrouter.ai/keys")
                console.print('  Then select it: export OPENROUTER_API_KEY="your_key"')

            fallback_options = [
                "Google Gemini Free Tier (https://generativelanguage.googleapis.com)",
                "OpenRouter Free Models (https://openrouter.ai/api/v1)",
            ]

            console.print("\nAvailable free fallback endpoints:")
            selected_fallbacks = []
            for i, opt in enumerate(fallback_options, 1):
                console.print(f"  [green]{i}[/]: {opt}")

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
                        console.print("  [green]✓[/] Registered Gemini Free fallback node")
                    else:
                        console.print(
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
                        console.print("  [green]✓[/] Registered OpenRouter Free fallback node")
                    else:
                        console.print("  [red]✗[/] Failed to register OpenRouter Free node")
    except (KeyboardInterrupt, EOFError):
        pass

    if not use_auto:
        if not running_providers:
            console.print(
                "\n[bold yellow]⚠️  No active providers or routers running on this machine.[/bold yellow]"
            )
            console.print("To run OmniRoute (centralized router recommended for Verdict):")
            console.print("  [bold]npm install -g omniroute[/bold]")
            console.print("  [bold]omniroute serve[/bold]\n")

            try:
                should_manual = Prompt.ask(
                    "Would you like to manually configure Verdict right now anyway?", default="y"
                )
                if not should_manual.lower().startswith("y"):
                    console.print(
                        "\n[yellow]Setup cancelled. Please start your provider/router and try again.[/yellow]"
                    )
                    return
            except (KeyboardInterrupt, EOFError):
                console.print("\n[yellow]Setup input interrupted.[/yellow]")
                return

        console.print(
            Panel.fit("[bold blue]Verdict Manual Configuration[/bold blue]", border_style="blue")
        )
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
            console.print("\n[yellow]Manual configuration input interrupted.[/yellow]")
            return

    # Save configuration
    config_dir = os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "verdict"
    )
    os.makedirs(config_dir, exist_ok=True)
    config_path = os.path.join(config_dir, "verdict.yaml")

    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False)

    console.print(f"\n[bold green]✓ Saved configuration to {config_path}![/bold green]")
    console.print("[dim]Configuration contents:[/dim]")
    console.print(yaml.dump(config, default_flow_style=False))


def cmd_setup_plan(*, output_json: bool = False) -> None:
    """Print the mutation-free setup plan without discovery or side effects."""

    from verdict.setup_plan import build_setup_plan

    plan: dict[str, Any] = build_setup_plan().to_dict()
    if output_json:
        print(json.dumps(plan, indent=2, sort_keys=True))
        return
    print("Verdict setup plan (dry-run; no changes made)")
    print(f"Plan: {plan['plan_id']}")
    config = plan["config"]
    actions = plan["actions"]
    assert isinstance(config, dict)
    assert isinstance(actions, list)
    print(f"Config: {config['path']}")
    for action in actions:
        assert isinstance(action, dict)
        print(f"- {action['description']}")


def cmd_route(task: str, criticality: str, terse: bool = False) -> None:
    """Route a single task."""
    config_dir = os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "verdict"
    )
    config_path = os.path.join(config_dir, "verdict.yaml")

    if os.path.exists(config_path):
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}
        providers = {
            k: ProviderConfig(base_url=v.get("base_url", ""), api_key_env=v.get("api_key_env"))
            for k, v in (raw.get("providers") or {}).items()
        }
        gate = Gate(
            primary_model=raw.get("primary_model", "anthropic/claude-3-opus-20240229"),
            providers=providers,
            log_path=raw.get("log_path", "verdict-decisions.jsonl"),
        )
    else:
        gate = Gate(
            primary_model="anthropic/claude-3-opus-20240229",
            providers={"public_ollama": ProviderConfig(base_url="http://localhost:11434/v1")},
        )

    if terse:
        dec = gate.route(task, criticality)
        print(dec.model)
        return

    with console.status("[bold green]Evaluating network & heuristics...", spinner="dots"):
        dec = gate.route(task, criticality)

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
) -> None:
    """Run the reproducible local benchmark harness and optionally persist JSON."""
    report = run_reproducible_benchmarks(
        fixture, allow_live_provider=allow_live_provider, live_provider=live_provider
    )
    console.print(format_benchmark_report(report), end="")

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
    verbose: bool = False, output_json: bool = False, output_config: bool = False
) -> None:
    """Detect available LLM providers."""
    try:
        from verdict.provider_detection import (
            detect_all_providers,
            format_detection_report,
            generate_verdict_config,
        )

        result = detect_all_providers()
        if output_json:
            print(
                json.dumps(
                    {
                        "local_servers": [p.__dict__ for p in result.local_servers],
                        "cli_providers": [p.__dict__ for p in result.cli_providers],
                        "centralized_routers": [p.__dict__ for p in result.centralized_routers],
                        "cloud_apis": [p.__dict__ for p in result.cloud_apis],
                        "custom_endpoints": [p.__dict__ for p in result.custom_endpoints],
                    },
                    indent=2,
                )
            )
        elif output_config:
            config = generate_verdict_config(result)
            print(yaml.dump(config, default_flow_style=False))
        else:
            console.print(format_detection_report(result, verbose=verbose))
    except Exception as e:
        console.print(f"[bold red]Detection failed: {e}[/bold red]")
        import traceback

        traceback.print_exc()
        sys.exit(1)


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
            with urllib.request.urlopen(request, timeout=10) as response:  # nosec B310
                payload = response.read()
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

        from verdict.memory_bridge import run_doctor_diagnostics
        from verdict.runtime_daemons import RuntimeManager
        from verdict.runtime_health import build_runtime_health_report

        report = run_doctor_diagnostics(home_dir=Path.home(), cwd=Path.cwd(), fix=fix)
        report["runtime_health"] = build_runtime_health_report(RuntimeManager().status()).to_dict()
        print(json.dumps(report, indent=2, sort_keys=True))
        if report["status"] != "healthy":
            raise SystemExit(1)
        return

    console.print(
        Panel.fit("[bold green]🩺 Verdict System Doctor[/bold green]", border_style="green")
    )

    issues_found = []
    fixed_issues = []

    from verdict.documentation_preflight import run_documentation_preflight

    documentation_report = run_documentation_preflight(fix=fix)
    console.print(
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
            console.print(f"  • Configured Primary Model: [cyan]{primary_model}[/] (Tier-{tier})")

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

    # 2. OmniRoute nodes check
    existing_nodes = _omniroute_api_request("GET", "/api/provider-nodes")
    if existing_nodes is None:
        console.print(
            "[dim]OmniRoute server is not currently running/reachable to check nodes.[/dim]"
        )
    else:
        items = []
        if isinstance(existing_nodes, list):
            items = existing_nodes
        elif isinstance(existing_nodes, dict) and "items" in existing_nodes:
            items = existing_nodes["items"]

        console.print(
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
            console.print(
                "\n[yellow]⚠️  Duplicate provider nodes detected in local OmniRoute database:[/yellow]"
            )
            for node_id, name, url, original_id in duplicates:
                console.print(
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
                            console.print(f"  [green]✓[/] Removed duplicate node: {name}")
                            fixed_issues.append(f"Removed duplicate node {node_id}")
                        else:
                            console.print(f"  [red]✗[/] Failed to remove node {node_id}")
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

    # 4. Summary report
    console.print("\n" + "═" * 45)
    console.print(
        f"🩺 Doctor Report: {len(issues_found)} issues identified. {len(fixed_issues)} resolved."
    )
    console.print("═" * 45)

    if issues_found:
        for iss in issues_found:
            is_fixed = False
            for fixed_issue in fixed_issues:
                if fixed_issue.lower() in iss.lower():
                    is_fixed = True
                    break
            if is_fixed:
                console.print(f"  [green]✓ FIXED:[/] {iss}")
            else:
                console.print(f"  [red]✗ ISSUE:[/] {iss}")

        if not config:
            console.print(
                "\n[yellow]💡 Suggestion: Run 'verdict setup' to initialize your configuration file.[/yellow]"
            )
    else:
        console.print("  [green]✓ System is healthy! All checks passed.[/green]")


def cmd_run(task: str, criticality: str, terse: bool = False) -> None:
    """Run a single task through the routing gate (alias of route)."""
    cmd_route(task, criticality, terse)


def cmd_plan(output_json: bool = False) -> None:
    """Print a mutation-free setup plan without probing or writing state."""
    cmd_setup_plan(output_json=output_json)


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
    table = Table(title="Verdict model catalog")
    table.add_column("ID", style="cyan")
    table.add_column("Provider")
    table.add_column("Tier")
    table.add_column("Context")
    table.add_column("Cost/1k", justify="right")
    table.add_column("State")
    for m in catalog:
        table.add_row(
            m.id,
            m.provider,
            f"T{m.capability_tier}",
            str(m.context_window) if m.context_window > 0 else "-",
            f"${m.cost_per_1k:.4f}" if m.cost_per_1k else "-",
            m.availability_state,
        )
    console.print(table)


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
            console.print(f"[bold red]{message}[/bold red]")
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
    console.print(Panel(f"[bold cyan]{model.id}[/bold cyan]", title="Model inspect"))
    console.print(json.dumps(payload, indent=2, sort_keys=True))


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


def cmd_hook(args: Any) -> None:
    """Manage Verdict lifecycle hooks for Codex and Claude Code."""
    import time as _time

    from verdict.memory_bridge import configure_memory_bridge
    from verdict.memory_plane import MemoryPlane, MemoryRecord

    hook_cmd = getattr(args, "hook_command", None)
    db_path = getattr(args, "db_path", None) or str(Path.home() / ".verdict" / "memory.db")
    plane = MemoryPlane(db_path)

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
        rec = MemoryRecord(
            record_id=f"rec_{key}_{int(_time.time())}",
            namespace=namespace,
            key=key,
            content=value,
            source=source,
            trust="gated-local-observation",
        )
        plane.put(rec)
        console.print(f"[bold green]✓ Recorded [{namespace}:{key}][/bold green]")

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


def main() -> None:
    parser = argparse.ArgumentParser(description="Verdict: policy-gated LLM Router")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    setup_cli_p = subparsers.add_parser("setup", help="Plan or apply the interactive setup wizard")
    setup_cli_p.add_argument(
        "setup_action",
        nargs="?",
        choices=["plan"],
        help="Read-only setup operation (currently: plan)",
    )
    setup_cli_p.add_argument(
        "--dry-run", action="store_true", help="Build a mutation-free setup plan"
    )
    setup_cli_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    setup_cli_p.add_argument(
        "--non-interactive", action="store_true", help="Do not prompt or mutate state"
    )

    route_p = subparsers.add_parser("route", help="Route a single prompt/task")
    route_p.add_argument("task", help="Task description or prompt text")
    route_p.add_argument("--terse", action="store_true", help="Output ONLY the target model string")
    route_p.add_argument(
        "--criticality", default="medium", choices=["critical", "high", "medium", "low"]
    )

    stats_p = subparsers.add_parser("stats", help="View routing analytics")
    stats_p.add_argument("--log_path", default="verdict-decisions.jsonl")

    benchmark_p = subparsers.add_parser(
        "benchmark", help="Run the reproducible local benchmark harness"
    )
    benchmark_p.add_argument("--fixture", default="benchmarks/fixtures/reproducible.json")
    benchmark_p.add_argument("--output-json", default=None)
    benchmark_p.add_argument("--allow-live-provider", action="store_true")
    benchmark_p.add_argument("--live-provider", default=None)

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

    # New: detect command
    detect_p = subparsers.add_parser("detect", help="Detect available LLM providers")
    detect_p.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    detect_p.add_argument("--json", action="store_true", help="Output JSON")
    detect_p.add_argument("--config", action="store_true", help="Generate suggested Verdict config")

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
    catalog_p.add_argument("--expected-rows", type=int, default=3977)
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

    runtime_p = subparsers.add_parser(
        "runtime", help="Inspect and safely reconcile global Ruflo/RuVector ownership"
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
    uninst_p = subparsers.add_parser(
        "uninstall", help="Reversibly uninstall Verdict memory bridge hooks and MCP registrations"
    )
    uninst_p.add_argument(
        "--purge-data", action="store_true", help="Purge .verdict memory database directory"
    )
    subparsers.add_parser("check", help="Validate system configuration file syntax and sanity")

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
        "docs", help="Verify or ingest authoritative project, Ruflo, and RuVector documentation"
    )
    docs_p.add_argument(
        "--fix", action="store_true", help="Fetch and ingest missing/stale documents"
    )
    docs_p.add_argument("--repo-root", default=str(Path.cwd()), help="Repository root")
    docs_p.add_argument("--db-path", default=None, help="Shared memory database path")
    docs_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    setup_p = memory_sub.add_parser(
        "setup",
        help="Autopilot wizard to connect tools (Codex, Claude, Pi, Ruflo) to Unified Memory",
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

    run_p = subparsers.add_parser("run", help="Route a single prompt/task (alias of route)")
    run_p.add_argument("task", help="Task description or prompt text")
    run_p.add_argument("--terse", action="store_true", help="Output ONLY the target model string")
    run_p.add_argument(
        "--criticality", default="medium", choices=["critical", "high", "medium", "low"]
    )

    plan_p = subparsers.add_parser("plan", help="Print a mutation-free setup plan")
    plan_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    models_p = subparsers.add_parser(
        "models", help="List the qualified model catalog used for routing and simulation"
    )
    models_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    inspect_p = subparsers.add_parser("inspect", help="Inspect one model's catalog record")
    inspect_p.add_argument("model_id", help="Model ID to inspect")
    inspect_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    replay_p = subparsers.add_parser("replay", help="Replay a recorded execution session")
    replay_p.add_argument("session_id", help="Session ID to replay")
    replay_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    simulate_p = subparsers.add_parser(
        "simulate", help="Forecast tokens, cost, risk, and model before any paid call"
    )
    simulate_p.add_argument("task", help="Task description or prompt text")
    simulate_p.add_argument(
        "--criticality", default="medium", choices=["critical", "high", "medium", "low"]
    )
    simulate_p.add_argument("--model", dest="model_override", default=None, help="Model override")
    simulate_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    args = parser.parse_args()

    if args.command == "setup":
        if args.setup_action == "plan":
            cmd_setup_plan(output_json=args.json)
        else:
            cmd_setup(
                dry_run=args.dry_run, output_json=args.json, non_interactive=args.non_interactive
            )
    elif args.command == "route":
        cmd_route(args.task, args.criticality, args.terse)
    elif args.command == "stats":
        cmd_stats(args.log_path)
    elif args.command == "benchmark":
        cmd_benchmark(
            args.fixture,
            args.output_json,
            allow_live_provider=args.allow_live_provider,
            live_provider=args.live_provider,
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

            start_server(args.port, args.host)
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
        cmd_detect(verbose=args.verbose, output_json=args.json, output_config=args.config)
    elif args.command == "suggest":
        cmd_suggest(args.log_path)
    elif args.command == "doctor":
        cmd_doctor(fix=getattr(args, "fix", False), output_json=getattr(args, "json", False))
    elif args.command == "runtime":
        cmd_runtime(
            args.runtime_command,
            apply=getattr(args, "apply", False),
            consent=getattr(args, "yes", False),
            service_ids=getattr(args, "service_ids", None),
            output_json=getattr(args, "json", False),
        )
    elif args.command == "uninstall":
        cmd_uninstall(purge_data=getattr(args, "purge_data", False))
    elif args.command == "check":
        cmd_check()
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
    elif args.command == "models":
        cmd_models(output_json=args.json)
    elif args.command == "inspect":
        cmd_inspect(args.model_id, output_json=args.json)
    elif args.command == "replay":
        cmd_replay(args.session_id, output_json=args.json)
    elif args.command == "simulate":
        cmd_simulate(
            args.task, args.criticality, model_override=args.model_override, output_json=args.json
        )
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
