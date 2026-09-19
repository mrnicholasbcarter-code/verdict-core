"""Deterministic presentation examples; fixtures only, never install software.

Run: uv run python -m scripts.terminal_preview --width 80
Use --tty for styled output and --active to exercise the real elapsed spinner.
"""

from __future__ import annotations

import argparse
import tempfile
import time
from pathlib import Path

from rich.console import Console

from verdict.capability_bootstrap import (
    DiscoveredProvider,
    ProviderKind,
    ProviderLifecycle,
    run_bootstrap,
)
from verdict.terminal_ui import TerminalUI


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, default=80)
    parser.add_argument("--tty", action="store_true")
    parser.add_argument("--active", action="store_true")
    args = parser.parse_args()
    ui = TerminalUI(Console(width=args.width, force_terminal=args.tty))
    ui.header("Fixture preview")
    ui.panel(
        "Example data",
        "These deterministic observations demonstrate presentation only. No software is installed.",
    )
    with tempfile.TemporaryDirectory(prefix="verdict-ui-preview-") as directory:
        ui.section("First setup / discovery / recommendations / plan")
        with ui.session():
            report = run_bootstrap(providers=[], observer=ui.event)
            ui.bootstrap(report.to_dict(), rendered_events=True)
        if args.active:
            with ui.task("Rendering fixture installation feedback"):
                time.sleep(1)
        healthy = DiscoveredProvider(
            provider_id="example.native",
            provider_kind=ProviderKind.INTELLIGENCE,
            capabilities=frozenset({"code.symbols"}),
            lifecycle=ProviderLifecycle.HEALTHY,
            health_state="healthy",
            qualification_state="qualified",
        )
        ui.section("Successful action / certification")
        report = run_bootstrap(
            providers=[healthy],
            mode="apply",
            consent=True,
            state_dir=Path(directory) / "success",
            install_runner=lambda action: {"status": "success"},
        )
        ui.bootstrap(report.to_dict())
        ui.section("Partial failure")
        report = run_bootstrap(
            providers=[],
            mode="apply",
            consent=True,
            state_dir=Path(directory) / "failure",
            install_runner=lambda action: {
                "status": "failed",
                "error": "Example download unavailable",
            },
        )
        ui.bootstrap(report.to_dict())
    ui.header("Doctor")
    ui.doctor(
        {
            "capabilities": [
                {
                    "capability_id": "code.symbols",
                    "status": "covered",
                    "selected_provider_id": "example.native",
                    "health": "healthy",
                },
                {
                    "capability_id": "memory.search",
                    "status": "missing",
                    "selected_provider_id": None,
                },
            ]
        }
    )
    ui.doctor_summary(["Example gateway is unreachable"], [])


if __name__ == "__main__":
    main()
