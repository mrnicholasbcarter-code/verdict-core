with open('llm_gate/cli.py', 'r') as f:
    text = f.read()

import re

# We will just replace everything from "def main() -> None:" to the end
main_fixed = """def main() -> None:
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
        run_setup()
    elif args.command == "route":
        handle_route(args.task, args.criticality, getattr(args, "terse", False))
    elif args.command == "stats":
        handle_stats(args.log_path)
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
"""

# Strip out old main
text = re.sub(r"def main\(\) -> None:.*", main_fixed, text, flags=re.DOTALL)

with open('llm_gate/cli.py', 'w') as f:
    f.write(text)
