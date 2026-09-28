import json, sys, os
from pathlib import Path
from rich.console import Console
from verdict.orchestration.tui import follow
from verdict.orchestration.cockpit_nav import ScriptedKeyReader

events = Path(sys.argv[1])
w = int(os.environ.get("COLUMNS", "100"))
console = Console(force_terminal=True, color_system="truecolor", width=w)
keys = sys.argv[2].split(",") if len(sys.argv) > 2 else ["j","ENTER","d","q"]
reader = ScriptedKeyReader(keys)
view = follow(events, console=console, interactive=True, key_reader=reader, poll_seconds=0.0, max_iterations=30, stop_when_final=False)
print("---END---")
print("outcome:", view.outcome or "-")
print("nodes:", list(view.nodes.keys()))
