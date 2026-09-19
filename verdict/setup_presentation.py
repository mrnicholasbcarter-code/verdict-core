"""CLI adapter for the canonical bootstrap; no parallel planning or mutation."""

from typing import Any

from rich.console import Console

from verdict.capability_bootstrap import BootstrapReport, run_bootstrap
from verdict.terminal_ui import TerminalUI


def present_bootstrap(
    *, console: Console, output_json: bool = False, **options: Any
) -> BootstrapReport:
    ui = TerminalUI(console, machine=output_json)
    ui.header("Setup")
    if output_json:
        # The machine boundary must never request input, even with terminal stdin.
        options["non_interactive"] = True
        return run_bootstrap(**options)
    with ui.session():
        report = run_bootstrap(
            **options, observer=ui.event, confirm_plan=ui.confirm_plan if ui.interactive else None
        )
        ui.bootstrap(report.to_dict(), rendered_events=True)
    return report
