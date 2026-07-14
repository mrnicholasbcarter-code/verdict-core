"""CLI entry point for llm-gate."""

import argparse
import json
import os
import sys
from typing import Any

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from llm_gate.gate import Gate
from llm_gate.models import ProviderConfig

console = Console()


def _print_detection_banner() -> None:
    """Print the detection banner."""
    console.print(
        Panel.fit(
            "[bold blue]llm-gate Provider Detection[/bold blue]\n"
            "Scanning for local servers, CLIs, API keys, and routers...",
            border_style="blue",
        )
    )


def cmd_setup() -> None:
    """Interactive setup wizard."""
    # First, run auto-detection to show user what's available
    _print_detection_banner()
    try:
        from llm_gate.provider_detection import detect_all_providers, format_detection_report

        result = detect_all_providers()
        console.print(format_detection_report(result, verbose=False))
    except Exception as e:
        console.print(f"[yellow]Detection skipped: {e}[/yellow]")

    console.print(
        Panel.fit(
            "[bold blue]llm-gate Setup Wizard[/bold blue]\nLet's configure your routing engine.",
            border_style="blue",
        )
    )

    config: dict[str, Any] = {}
    config["primary_model"] = Prompt.ask(
        "[bold]Primary model[/bold] (Tier-0, never offloaded)",
        default="anthropic/claude-3-opus-20240229",
    )

    config["providers"] = {}
    while True:
        provider_name = Prompt.ask(
            "\n[bold]Add a provider[/bold] (name, or 'done' to finish)", default="done"
        )
        if provider_name.lower() == "done":
            break
        base_url = Prompt.ask(f"  Base URL for {provider_name}")
        api_key_env = Prompt.ask(f"  API key env var for {provider_name}", default="")
        config["providers"][provider_name] = {
            "base_url": base_url,
            "api_key_env": api_key_env or None,
        }

    # Offer to use detected providers
    try:
        from llm_gate.provider_detection import detect_all_providers, generate_llm_gate_config

        result = detect_all_providers()
        suggested = generate_llm_gate_config(result)
        if suggested.get("providers"):
            console.print(
                "\n[bold cyan]Based on detection, you could use these providers:[/bold cyan]"
            )
            for name, cfg in suggested["providers"].items():
                console.print(f"  • {name}: {cfg.get('base_url', 'N/A')}")
            if (
                Prompt.ask("\nUse detected providers as starting point?", default="y")
                .lower()
                .startswith("y")
            ):
                config["providers"] = suggested["providers"]
                config["primary_model"] = suggested.get("primary_model", config["primary_model"])
    except Exception:
        pass

    config_dir = os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "llm-gate"
    )
    os.makedirs(config_dir, exist_ok=True)
    config_path = os.path.join(config_dir, "llm-gate.yaml")

    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False)

    console.print(f"\n[bold green]Saved configuration to {config_path}![/bold green]")


def cmd_route(task: str, criticality: str, terse: bool = False) -> None:
    """Route a single task."""
    config_dir = os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "llm-gate"
    )
    config_path = os.path.join(config_dir, "llm-gate.yaml")

    if os.path.exists(config_path):
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}
        providers = {
            k: ProviderConfig(
                base_url=v.get("base_url", ""),
                api_key_env=v.get("api_key_env"),
            )
            for k, v in (raw.get("providers") or {}).items()
        }
        gate = Gate(
            primary_model=raw.get("primary_model", "anthropic/claude-3-opus-20240229"),
            providers=providers,
            log_path=raw.get("log_path", "llm-gate-decisions.jsonl"),
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

    escalated_badge = (
        "[bold red]ESCALATED[/bold red]" if dec.escalated else "[dim]Standard Routing[/dim]"
    )

    output = f"""[bold]Task:[/bold] {task[:100]}{"..." if len(task) > 100 else ""}

[bold]Decision:[/bold]
  Model:     [bold {t_color}]{dec.model}[/bold {t_color}]
  Provider:  {dec.provider}
  Tier:      T{dec.tier}
  Status:    {escalated_badge}
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


def cmd_stats(log_path: str = "llm-gate-decisions.jsonl") -> None:
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
                d = entry.get("decision", {})
                t = d.get("tier", 2)
                tiers[t] = tiers.get(t, 0) + 1
                m = d.get("model", "unknown")
                models[m] = models.get(m, 0) + 1
                latencies.append(d.get("latency_ms", 0))
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


def cmd_cost_report() -> None:
    """Calculates and prints the estimated token usage execution cost from historic routing decisions."""
    import json

    console.print(Panel.fit("[bold green]llm-gate Cost and Usage Report[/bold green]"))

    log_path = "llm-gate-decisions.jsonl"
    if not os.path.exists(log_path):
        console.print(
            "[yellow]No routing telemetry found (llm-gate-decisions.jsonl missing).[/yellow]"
        )
        return

    total_requests = 0
    t0_requests = 0

    with open(log_path) as f:
        for line in f:
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                total_requests += 1
                if data.get("tier", 2) == 0:
                    t0_requests += 1
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
        from llm_gate.provider_detection import (
            detect_all_providers,
            format_detection_report,
            generate_llm_gate_config,
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
            config = generate_llm_gate_config(result)
            print(yaml.dump(config, default_flow_style=False))
        else:
            console.print(format_detection_report(result, verbose=verbose))
    except Exception as e:
        console.print(f"[bold red]Detection failed: {e}[/bold red]")
        import traceback

        traceback.print_exc()
        sys.exit(1)


def cmd_suggest(log_path: str = "llm-gate-decisions.jsonl") -> None:
    """Run the SuggestionService to propose evidence-backed improvements."""
    from rich.console import Console
    from rich.panel import Panel

    from llm_gate.suggestions import SuggestionService

    console = Console()
    svc = SuggestionService(log_path=log_path)

    with console.status("[bold green]Mining telemetry for suggestions...", spinner="dots"):
        suggestions = svc.generate_suggestions()

    if not suggestions:
        console.print("[yellow]No actionable suggestions found. Your routing is optimized![/yellow]")
        return

    console.print(Panel.fit("[bold blue]llm-gate Intelligence Suggestions[/bold blue]", border_style="blue"))

    for s in suggestions:
        category_color = {"performance": "cyan", "reliability": "red", "capacity": "yellow"}.get(s.category, "white")
        output = f"""[bold {category_color}]{s.title} ({s.id})[/] 
[dim]Category:[/] {s.category.title()}  |  [dim]Novelty:[/] {s.novelty}  |  [dim]Expires In:[/] {s.expiry}

{s.description}

[bold dim]Proposed Next Experiment:[/bold dim]
[italic]{s.proposed_next_experiment}[/italic]

[dim]Confidence:[/] {s.confidence * 100:.1f}%  |  [dim]Impact:[/] {s.expected_impact}
[dim]Evidence Events (Top 3):[/] {', '.join(s.evidence_references) if s.evidence_references else 'None'}
"""
        console.print(output)
        console.print("---")

def main() -> None:
    parser = argparse.ArgumentParser(description="llm-gate: Tier-based LLM Router")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    subparsers.add_parser("setup", help="Interactive setup wizard")

    route_p = subparsers.add_parser("route", help="Route a single prompt/task")
    route_p.add_argument("task", help="Task description or prompt text")
    route_p.add_argument("--terse", action="store_true", help="Output ONLY the target model string")
    route_p.add_argument(
        "--criticality", default="medium", choices=["critical", "high", "medium", "low"]
    )

    stats_p = subparsers.add_parser("stats", help="View routing analytics")
    stats_p.add_argument("--log_path", default="llm-gate-decisions.jsonl")

    subparsers.add_parser("ui", help="Launch the Streamlit analytics dashboard")

    serve_p = subparsers.add_parser("serve", help="Launch the FastAPI microservice")
    serve_p.add_argument("--port", type=int, default=8000)

    # New: detect command
    detect_p = subparsers.add_parser("detect", help="Detect available LLM providers")
    detect_p.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    detect_p.add_argument("--json", action="store_true", help="Output JSON")
    detect_p.add_argument("--config", action="store_true", help="Generate suggested llm-gate.yaml")

    suggest_p = subparsers.add_parser("suggest", help="Review intelligence suggestions from past outcomes")
    suggest_p.add_argument("--log_path", default="llm-gate-decisions.jsonl")

    args = parser.parse_args()

    if args.command == "setup":
        cmd_setup()
    elif args.command == "route":
        cmd_route(args.task, args.criticality, args.terse)
    elif args.command == "stats":
        cmd_stats(args.log_path)
    elif args.command == "ui":
        try:
            # Resolve the path dynamically without executing the file
            import importlib.util
            import subprocess
            import sys

            spec = importlib.util.find_spec("llm_gate.dashboard")
            if not spec or not spec.origin:
                console.print("[bold red]❌ Dashboard module missing.[/bold red]")
                sys.exit(1)
            subprocess.run([sys.executable, "-m", "streamlit", "run", spec.origin])

        except ImportError:
            console.print("[bold red]❌ UI dependencies not found.[/bold red]")
            console.print("Please install the UI package suite:")
            console.print(
                '  [bold cyan]pipx install "llm-gate[all] @ git+https://github.com/mrnicholasbcarter-code/llm-gate.git" --force[/bold cyan]'
            )
            sys.exit(1)
    elif args.command == "serve":
        try:
            from llm_gate.api import start_server

            start_server(args.port)
        except ImportError:
            console.print("[bold red]❌ Server dependencies not found.[/bold red]")
            console.print("Please install the FastAPI server suite:")
            console.print(
                '  [bold cyan]pipx install "llm-gate[all] @ git+https://github.com/mrnicholasbcarter-code/llm-gate.git" --force[/bold cyan]'
            )
            sys.exit(1)
    elif args.command == "detect":
        cmd_detect(verbose=args.verbose, output_json=args.json, output_config=args.config)
    elif args.command == "suggest":
        cmd_suggest(args.log_path)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
