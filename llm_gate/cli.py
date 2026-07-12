"""Command line interface for llm-gate."""
import argparse
import json
import dataclasses
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

try:
    import yaml
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.prompt import Prompt, Confirm
    from rich.spinner import Spinner
    from rich.live import Live
except ImportError:
    print("Warning: 'rich' and 'pyyaml' are required for the CLI. Install with `pip install rich pyyaml`")
    exit(1)

from llm_gate.gate import Gate
from llm_gate.models import ProviderConfig

console = Console()

def cmd_setup() -> None:
    """Interactive wizard to generate llm-gate.yaml"""
    console.print(Panel.fit("[bold blue]llm-gate Setup Wizard[/bold blue]\nLet's configure your routing engine.", border_style="blue"))
    
    config = {
        "primary_model": "",
        "log_path": "llm-gate-decisions.jsonl",
        "providers": {}
    }

    config["primary_model"] = Prompt.ask(
        "Enter your [bold red]Tier 0 (Critical)[/bold red] primary model", 
        default="anthropic/claude-3-opus-20240229"
    )

    console.print("\n[bold]Let's add some offload providers (Tier 1-3).[/bold]")
    providers_added = 0
    while True:
        if providers_added > 0 and not Confirm.ask("Add another provider?"):
            break
            
        provider_name = Prompt.ask("Provider name (e.g., anthropic, groq, local_ollama)")
        base_url = Prompt.ask(f"Base URL for {provider_name}", default="https://api.anthropic.com/v1")
        api_env = Prompt.ask(f"Environment variable for API key (leave blank if none)", default="")
        
        config["providers"][provider_name] = {
            "base_url": base_url,
            "api_key_env": api_env if api_env else None,
            "priority": 10 - providers_added
        }
        providers_added += 1

    with open("llm-gate.yaml", "w") as f:
        yaml.dump(config, f, default_flow_style=False)
    
    console.print(f"\n[bold green]✔ Saved configuration to llm-gate.yaml![/bold green]")
    console.print("Run [bold cyan]llm-gate route \"hello world\"[/bold cyan] to test the router.")

def cmd_route(task: str, criticality: str, terse: bool = False) -> None:
    """Route a task and visually display the decision."""
    # Simplified default Gate for the CLI demo if yaml doesn't exist
    if os.path.exists("llm-gate.yaml"):
        with open("llm-gate.yaml", "r") as f:
            raw = yaml.safe_load(f)
            providers = {
                k: ProviderConfig(
                    base_url=v.get("base_url", ""),
                    api_key_env=v.get("api_key_env"),
                    priority=v.get("priority", 0)
                ) for k, v in raw.get("providers", {}).items()
            }
            gate = Gate(
                primary_model=raw.get("primary_model", "anthropic/claude-3-opus"),
                providers=providers,
                log_path=raw.get("log_path", "llm-gate-decisions.jsonl")
            )
    else:
        # Failsafe default
        gate = Gate() # Automatically loads config via Gate(None)

        if terse:
            dec = gate.route(task, criticality)
            print(dec.model)
            return
            
        with console.status("[bold green]Evaluating network & heuristics...", spinner="dots"):
            dec = gate.route(task, criticality)

    # Format output panel
    tier_colors = {0: "red", 1: "magenta", 2: "yellow", 3: "green"}
    t_color = tier_colors.get(dec.tier, "white")
    
    escalated_badge = "[bold red]⚠ ESCALATED[/bold red]" if dec.escalated else "[dim]Standard Routing[/dim]"
    
    output = f"""[bold]Task:[/bold] {task[:100]}{'...' if len(task) > 100 else ''}

[bold]Decision:[/bold]
• Model:     [bold {t_color}]{dec.model}[/bold {t_color}]
• Provider:  {dec.provider}
• Tier:      T{dec.tier}
• Status:    {escalated_badge}
• Latency:   [cyan]{dec.latency_ms}ms[/cyan]

[bold dim]Reason:[/bold dim] [italic]{dec.reason}[/italic]
"""
    console.print(Panel(output, title="[bold blue]Routing Decision[/bold blue]", border_style="blue", expand=False))

def cmd_stats(log_path: str = "llm-gate-decisions.jsonl") -> None:
    """Parse JSONL logs and build a beautiful dashboard."""
    if not os.path.exists(log_path):
        console.print(f"[bold red]Log file not found:[/bold red] {log_path}")
        return

    stats = {"0": 0, "1": 0, "2": 0, "3": 0}
    models = {}
    total = 0
    avg_latency = 0.0

    try:
        with open(log_path, "r") as f:
            for line in f:
                if not line.strip(): continue
                data = json.loads(line)
                total += 1
                t = str(data.get("effective_tier", 2))
                stats[t] = stats.get(t, 0) + 1
                
                mod = data.get("model_chosen", "unknown")
                models[mod] = models.get(mod, 0) + 1
                
                avg_latency += data.get("latency_ms", 0)
    except Exception as e:
        console.print(f"[bold red]Error parsing logs:[/bold red] {e}")
        return

    if total > 0:
        avg_latency /= total

    # Build Tier Distribution Table
    table = Table(title="[bold]Routing Distribution by Tier[/bold]")
    table.add_column("Tier", justify="center", style="cyan", no_wrap=True)
    table.add_column("Volume", justify="right", style="magenta")
    table.add_column("% of Traffic", justify="right", style="green")

    tier_colors = {"0": "red UI/Money Path", "1": "magenta High Cap", "2": "yellow Logic", "3": "green Formatting"}

    for t in sorted(stats.keys()):
        count = stats[t]
        pct = (count / total) * 100 if total > 0 else 0
        table.add_row(f"T{t} ({tier_colors.get(t, '')})", str(count), f"{pct:.1f}%")

    console.print("\n")
    console.print(table)
    console.print(f"\n[bold]Total Requests:[/bold] {total}")
    console.print(f"[bold]P50 Latency:[/bold] [cyan]{avg_latency:.2f}ms[/cyan]\n")

    # Display Top Models
    console.print("[bold]Top Routed Models:[/bold]")
    for mod, count in sorted(models.items(), key=lambda x: x[1], reverse=True)[:5]:
        console.print(f"  • {mod}: [bold yellow]{count}[/bold yellow] calls")


def main() -> None:
    parser = argparse.ArgumentParser(description="llm-gate: Tier-based LLM Router")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Command: setup
    subparsers.add_parser("setup", help="Interactive setup wizard")

    # Command: route
    route_p = subparsers.add_parser("route", help="Route a single prompt/task")
    route_p.add_argument("task", help="Task description or prompt text")
    route_p.add_argument("--terse", action="store_true", help="Output ONLY the target model string (for bash piping)")
    route_p.add_argument("--criticality", default="medium", choices=["critical", "high", "medium", "low"], help="Requested baseline criticality")

    # Command: stats
    stats_p = subparsers.add_parser("stats", help="View routing analytics and cost savings dashboard")
    stats_p.add_argument("--log_path", default="llm-gate-decisions.jsonl", help="Path to decision log")
    
    # Command: ui
    ui_p = subparsers.add_parser("ui", help="Launch the interactive Streamlit analytics dashboard")

    # Command: serve
    serve_p = subparsers.add_parser("serve", help="Launch the FastAPI microservice backend")
    serve_p.add_argument("--port", type=int, default=8000)

    args = parser.parse_args()
    
    if args.command == "setup":
        cmd_setup()
    elif args.command == "route":
        cmd_route(args.task, args.criticality, getattr(args, "terse", False))
    elif args.command == "stats":
        cmd_stats(args.log_path)
    elif args.command == "ui":
        from .dashboard import start_ui
        start_ui()
    elif args.command == "serve":
        from .api import start_server
        start_server(args.port)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
