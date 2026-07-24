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
from verdict.models import ProviderConfig

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
    """Read the OmniRoute management token from local filesystem or env."""
    db_path = os.path.expanduser("~/.omniroute/storage.sqlite")
    if not os.path.exists(db_path):
        return os.getenv("OMNIROUTE_API_KEY")
    import sqlite3

    try:
        con = sqlite3.connect(db_path)
        try:
            # Check for active key named 'Jcode' first, then 'ok', or any active management key
            row = con.execute(
                "select key from api_keys where is_active=1 order by case when name='Jcode' then 0 when name='ok' then 1 else 2 end, id limit 1"
            ).fetchone()
            if row:
                return str(row[0])
        finally:
            con.close()
    except Exception:
        pass
    return os.getenv("OMNIROUTE_API_KEY")


def _omniroute_api_request(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    """Make an authenticated request to the local OmniRoute server."""
    token = _read_omniroute_token()
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    import socket

    port = 20128

    def check_port(p: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", p), timeout=0.1):
                return True
        except Exception:
            return False

    if not check_port(20128) and check_port(20132):
        port = 20132

    url = f"http://127.0.0.1:{port}{path}"

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


def cmd_setup() -> None:
    """Interactive setup wizard."""
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
) -> None:
    """Run a real one-token liveness probe against each model through a router.

    Sends the fixed, no-user-data probe payload (max_tokens=1) so a model can be
    confirmed live before it is assigned real work (e.g. a subagent).
    """
    import time

    from verdict.probes import ProbePolicy, openai_probe_transport

    api_key = os.getenv("OPENAI_API_KEY")
    policy = ProbePolicy(timeout_seconds=timeout)
    transport = openai_probe_transport(base_url, api_key=api_key)

    results: list[dict[str, Any]] = []
    for model_id in models:
        payload = policy.payload(model_id)
        started = time.monotonic()
        entry: dict[str, Any] = {"model": model_id}
        try:
            response = transport(model_id, payload, timeout)
            latency_ms = (time.monotonic() - started) * 1000.0
            status_code = response.get("status_code")
            body = response.get("body") or {}
            usage = body.get("usage") if isinstance(body, dict) else None
            entry.update(
                {
                    "ok": bool(status_code and 200 <= int(status_code) < 300),
                    "http_status": status_code,
                    "latency_ms": round(latency_ms, 1),
                    "usage": usage,
                }
            )
        except Exception as exc:
            entry.update({"ok": False, "error": type(exc).__name__, "detail": str(exc)[:200]})
        results.append(entry)

    if output_json:
        print(json.dumps(results, indent=2))
        return

    table = Table(title=f"Verdict probe  ({base_url})")
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


def cmd_doctor() -> None:
    """Scan the Verdict setup and OmniRoute connections for issues and repair them."""
    console.print(
        Panel.fit("[bold green]🩺 Verdict System Doctor[/bold green]", border_style="green")
    )

    issues_found = []
    fixed_issues = []

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


def main() -> None:
    parser = argparse.ArgumentParser(description="Verdict: policy-gated LLM Router")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    subparsers.add_parser("setup", help="Interactive setup wizard")

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
    probe_p.add_argument("--json", action="store_true", help="Output JSON")

    suggest_p = subparsers.add_parser(
        "suggest", help="Review intelligence suggestions from past outcomes"
    )
    suggest_p.add_argument("--log_path", default="verdict-decisions.jsonl")

    subparsers.add_parser(
        "doctor", help="Scan and repair system configuration and connectivity issues"
    )

    subparsers.add_parser("check", help="Validate system configuration file syntax and sanity")

    args = parser.parse_args()

    if args.command == "setup":
        cmd_setup()
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
        cmd_probe(args.models, base_url=args.base_url, timeout=args.timeout, output_json=args.json)
    elif args.command == "detect":
        cmd_detect(verbose=args.verbose, output_json=args.json, output_config=args.config)
    elif args.command == "suggest":
        cmd_suggest(args.log_path)
    elif args.command == "doctor":
        cmd_doctor()
    elif args.command == "check":
        cmd_check()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
