#!/usr/bin/env python3
r"""Capture Verdict's production renderers from a saved scenario, without running it.

Run from the repository root with its own interpreter, for example::

    PYTHONPATH=. /tmp/vrun /path/to/.venv/bin/python scripts/render_tui_gallery.py \
        --scenario /path/to/run --output-dir /tmp/verdict-gallery \
        --width 110 \
        --scenario-label "OFFLINE SCENARIO / scripted workers, injected fault"

The scenario directory must contain events.jsonl and receipt.json, with a running
worker, a recorded failure, and a COMPLETE outcome. Setup and doctor are explicitly
labelled presentation fixtures. No bootstrap, health probe, provider call, worker,
interactive loop, or input replacement runs here. Trace/claims require the merged
production trace_render and demo_render modules; there is no rendering fallback.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from dataclasses import dataclass, replace
from datetime import datetime
from io import StringIO
from itertools import pairwise
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.text import Text
from rich.theme import Theme

from verdict.design import TOKENS, PresentationMode, presentation_mode
from verdict.home import HomeState, render_home
from verdict.orchestration.claims import derive_claims
from verdict.orchestration.context_render import render_context
from verdict.orchestration.context_view import context_view
from verdict.orchestration.contracts import NodeState, RunEvent
from verdict.orchestration.routing_render import render_routing
from verdict.orchestration.routing_view import routing_view
from verdict.orchestration.trace_view import trace_view_from_events
from verdict.orchestration.tui import RunView
from verdict.orchestration.tui import render as render_cockpit
from verdict.terminal_ui import TerminalUI, clean

SCREEN_NAMES = (
    "home",
    "setup",
    "doctor",
    "cockpit-running",
    "cockpit-failure",
    "cockpit-complete",
    "routing-explorer",
    "context-view",
    "trace",
    "demo-claims",
)
SCENARIO_LABEL = "RECORDED SCENARIO / replay, not a live provider check"
FIXTURE_LABEL = "PRESENTATION FIXTURE / illustrative data; no host checks or changes"


@dataclass(frozen=True)
class Scenario:
    run_dir: Path
    raw_events: list[dict[str, Any]]
    events: list[RunEvent]
    receipt: dict[str, Any]
    # Counts, not sequence numbers: slice the unchanged recorded event stream.
    running_end: int
    failure_end: int


def snapshot_boundaries(events: list[RunEvent]) -> tuple[int, int]:
    """Find real running and failure/recovery prefixes; never invent a state."""
    first_failure = next((i for i, event in enumerate(events) if event.type == "failure"), None)
    if first_failure is None:
        raise ValueError("scenario has no recorded failure for the failure screen")
    view = RunView()
    running_end = 0
    most_running = 0
    active: set[str] = set()
    for i, event in enumerate(events[:first_failure]):
        view.apply(event)
        if event.type == "terminal":
            active.discard(event.node_id)
        elif event.type == "node_state":
            if event.data.get("state") == NodeState.RUNNING.value:
                active.add(event.node_id)
            else:
                active.discard(event.node_id)
        # A terminal event may precede the next node_state event. RunView still
        # says RUNNING during that gap. Another worker starting in the gap must
        # not create a false concurrent frame, either.
        projected_running = {
            node_id for node_id, node in view.nodes.items() if node.state is NodeState.RUNNING
        }
        if (
            event.type == "node_state"
            and event.data.get("state") == NodeState.RUNNING.value
            and len(active) >= most_running
            and projected_running == active
            and not view.final
        ):
            most_running, running_end = len(active), i + 1
    if not running_end:
        raise ValueError("scenario has no RUNNING worker before its first failure")
    failure_end = first_failure + 1
    # Include the first recorded recovery decision and its preceding cooldown.
    # Stop before another dispatch so this remains the failure/recovery frame.
    for i in range(first_failure + 1, len(events)):
        event = events[i]
        if event.type in {"dispatch", "run_finished", "terminal"}:
            break
        failure_end = i + 1
        if event.type == "reassign":
            break
    return running_end, failure_end


def load_scenario(path: Path) -> Scenario:
    run_dir = path.resolve()
    events_path = run_dir / "events.jsonl"
    receipt_path = run_dir / "receipt.json"
    # An accidental multi-GB event log is not a gallery input.
    if events_path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("events.jsonl exceeds the gallery's 32 MiB input limit")
    raw: list[dict[str, Any]] = []
    for line_number, line in enumerate(events_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"events.jsonl:{line_number}: expected an object")
        raw.append(row)
    if not raw or len(raw) > 20_000:
        raise ValueError("scenario must contain 1 to 20,000 events")
    events = [RunEvent.from_dict(row) for row in raw]
    if any(a.seq >= b.seq for a, b in pairwise(events)):
        raise ValueError("event sequence numbers must be strictly increasing")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if not isinstance(receipt, dict):
        raise ValueError("receipt.json must be an object")
    view = RunView.from_events(events)
    receipt_outcome = receipt.get("outcome") or receipt.get("claimed_outcome")
    if view.outcome != "COMPLETE" or receipt_outcome != "COMPLETE":
        raise ValueError("complete screen requires COMPLETE in both events and receipt")
    running_end, failure_end = snapshot_boundaries(events)
    return Scenario(run_dir, raw, events, receipt, running_end, failure_end)


def capture_console(width: int) -> tuple[Console, PresentationMode]:
    """Explicit presentation policy; never depend on the capture host's TTY."""
    console = Console(
        file=StringIO(),
        record=True,
        width=width,
        force_terminal=True,
        color_system="truecolor",
        no_color=False,
        theme=Theme(TOKENS),
        highlight=False,
    )
    mode = replace(
        presentation_mode(),
        width=width,
        color=True,
        unicode=True,
        color_system="truecolor",
        animate=False,
    )
    return console, mode


def fixture_ui(console: Console, mode: PresentationMode) -> TerminalUI:
    """Configure the public presentation fields; do not replace render methods."""
    ui = TerminalUI(console)
    # TerminalUI constructs its own themed Console. Enable its public recorder;
    # keep every rendering method unchanged and export that same Console.
    ui.console.record = True
    ui.mode = mode
    ui.plain = False
    ui.animate = False
    if ui.console.color_system != "truecolor" or ui.console.no_color:
        raise ValueError(
            "TerminalUI capture needs color enabled: unset CI, NO_COLOR and VERDICT_PLAIN; "
            "use TERM=xterm-256color"
        )
    return ui


def render_setup_fixture(console: Console, mode: PresentationMode) -> Console:
    ui = fixture_ui(console, mode)
    console = ui.console
    ui.header("Setup / presentation fixture")
    console.print(Text(FIXTURE_LABEL, style=TOKENS["WARNING"]))
    ui.bootstrap(
        {
            "mutation_free": True,
            "stages": [
                {
                    "stage": "discover",
                    "status": "ok",
                    "summary": "Fixture: existing local tools found",
                },
                {
                    "stage": "certify",
                    "status": "partial",
                    "summary": "Fixture: one capability needs configuration",
                },
                {
                    "stage": "plan",
                    "status": "ok",
                    "summary": "Fixture: review proposed actions before consent",
                },
            ],
            "providers": [
                {
                    "provider_kind": "runtime",
                    "provider_id": "prime-agent",
                    "lifecycle": "qualified",
                },
                {
                    "provider_kind": "review",
                    "provider_id": "open-code-review",
                    "lifecycle": "not_installed",
                },
            ],
            "recommendations": [
                {
                    "capability_id": "worker_execution",
                    "status": "covered",
                    "selected_provider_id": "prime-agent",
                    "reason": "Fixture: execution runtime present",
                },
                {
                    "capability_id": "independent_review",
                    "status": "recommended",
                    "candidate_providers": ["open-code-review"],
                    "reason": "Fixture: independent reviewer not configured",
                },
            ],
            "plan": {
                "plan_id": "presentation-fixture-plan",
                "actions": [
                    {
                        "kind": "preserve_configuration",
                        "description": "Preserve existing runtime settings",
                    },
                    {
                        "kind": "install_provider",
                        "description": "Install independent review capability",
                        "reason": "Requires explicit consent; nothing has been installed",
                    },
                ],
            },
            "certification": {
                "results": [
                    {
                        "provider_id": "prime-agent",
                        "certified": True,
                        "reason": "Fixture certification result",
                    },
                    {
                        "provider_id": "open-code-review",
                        "certified": False,
                        "reason": "Fixture: installation not observed",
                    },
                ]
            },
            "apply": {"actions": []},
        }
    )
    return console


def render_doctor_fixture(console: Console, mode: PresentationMode) -> Console:
    ui = fixture_ui(console, mode)
    console = ui.console
    ui.header("Doctor / presentation fixture")
    console.print(Text(FIXTURE_LABEL, style=TOKENS["WARNING"]))
    ui.doctor(
        {
            "capabilities": [
                {
                    "capability_id": "worker_execution",
                    "status": "healthy",
                    "selected_provider_id": "prime-agent",
                    "health": "fixture result",
                    "authority": "fixture",
                },
                {
                    "capability_id": "independent_review",
                    "status": "missing",
                    "health": "not configured in fixture",
                    "authority": "fixture",
                },
                {
                    "capability_id": "shared_memory",
                    "status": "degraded",
                    "health": "fixture configuration incomplete",
                    "authority": "fixture",
                },
            ]
        }
    )
    ui.doctor_summary(
        [
            "Fixture: independent review capability missing",
            "Fixture: shared memory configuration incomplete",
        ],
        [],
    )
    return console


def save_capture(console: Console, output_dir: Path, name: str, width: int) -> dict[str, str]:
    stem = f"{name}-{width}"
    txt = output_dir / f"{stem}.txt"
    svg = output_dir / f"{stem}.svg"
    # Export text BEFORE SVG clears the recording. Both formats contain one frame.
    txt.write_text(console.export_text(clear=False, styles=False), encoding="utf-8")
    console.save_svg(str(svg), title=f"Verdict / {name}", unique_id=stem, clear=True)
    return {"txt": txt.name, "svg": svg.name}


def render_gallery(
    scenario: Scenario,
    output_dir: Path,
    widths: list[int],
    *,
    scenario_label: str = SCENARIO_LABEL,
    overwrite: bool = False,
) -> dict[str, Any]:
    # Deliberately import real modules only, after CLI parsing/input validation.
    # This fails clearly before outputs are written if the redesign is not merged.
    try:
        from verdict.orchestration.demo_render import render_claims
        from verdict.orchestration.trace_render import render_trace
    except ModuleNotFoundError as exc:
        raise ValueError(
            "merge the production trace_render/demo_render modules before capture"
        ) from exc
    widths = list(dict.fromkeys(widths))
    if not widths or any(width < 40 or width > 240 for width in widths):
        raise ValueError("each width must be between 40 and 240 columns")
    output_dir = output_dir.resolve()
    targets = [output_dir / "manifest.json"] + [
        output_dir / f"{name}-{width}.{extension}"
        for width in widths
        for name in SCREEN_NAMES
        for extension in ("txt", "svg")
    ]
    if not overwrite and any(target.exists() for target in targets):
        raise ValueError("gallery files already exist; use a new directory or --overwrite")

    events = scenario.events
    runs = {
        "cockpit-running": RunView.from_events(events[: scenario.running_end]),
        "cockpit-failure": RunView.from_events(events[: scenario.failure_end]),
        "cockpit-complete": RunView.from_events(events),
    }
    routing = routing_view(scenario.raw_events)
    context = context_view(events)
    trace = trace_view_from_events(events)
    claims = derive_claims(scenario.raw_events, scenario.receipt, run_dir=scenario.run_dir)
    # Prefer an actual recorded cooldown evaluation. No rows, eligibility, or
    # selection reasons are synthesized to make this screen look busy.
    routing_index = (
        next(
            (
                i
                for i, evaluation in enumerate(routing.evaluations)
                if any(candidate.cooldown_until for candidate in evaluation.candidates or [])
            ),
            0,
        )
        if routing.evaluations
        else None
    )
    routing_at = routing.evaluations[routing_index].at if routing_index is not None else ""
    try:
        routing_now = (
            datetime.fromisoformat(routing_at.replace("Z", "+00:00")) if routing_at else None
        )
    except ValueError:
        routing_now = None
    home = HomeState(
        gateway="Not queried: artifact-only gallery capture",
        gateway_ok=None,
        runs=[
            {
                "run": scenario.run_dir.name,
                "outcome": runs["cockpit-complete"].outcome,
                "reason": runs["cockpit-complete"].reason,
                "age_s": max(
                    0, int(time.time() - (scenario.run_dir / "events.jsonl").stat().st_mtime)
                ),
            }
        ],
    )
    manifest: dict[str, Any] = {
        "scenario": str(scenario.run_dir),
        "scenario_label": clean(scenario_label),
        "fixture_screens": ["setup", "doctor"],
        "inputs": {
            name: hashlib.sha256((scenario.run_dir / name).read_bytes()).hexdigest()
            for name in ("events.jsonl", "receipt.json")
        },
        "event_count": len(events),
        "snapshot_last_seq": {
            "cockpit-running": events[scenario.running_end - 1].seq,
            "cockpit-failure": events[scenario.failure_end - 1].seq,
            "cockpit-complete": events[-1].seq,
        },
        "running_workers": sum(
            node.state is NodeState.RUNNING for node in runs["cockpit-running"].nodes.values()
        ),
        "routing_evaluation_index": routing_index,
        "routing_as_of": routing_at,
        "claim_statuses": {claim.id: claim.status for claim in claims},
        "screens": [],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    for width in widths:
        for name in SCREEN_NAMES:
            console, mode = capture_console(width)
            if name == "setup":
                console = render_setup_fixture(console, mode)
            elif name == "doctor":
                console = render_doctor_fixture(console, mode)
            else:
                console.print(Text(clean(scenario_label), style=TOKENS["WARNING"]))
                if name == "home":
                    console.print(render_home(home, plain=False, width=width, interactive=True))
                    console.print(Text("verdict \u203a", style=TOKENS["TEXT"]))
                elif name in runs:
                    view = runs[name]
                    console.print(
                        Text(
                            f"Recorded snapshot through event #{view.last_seq}",
                            style=TOKENS["MUTED"],
                        )
                    )
                    console.print(render_cockpit(view, width=width, plain=False))
                elif name == "routing-explorer":
                    console.print(
                        Text(
                            f"Recorded evaluation as of {routing_at or 'unknown'}",
                            style=TOKENS["MUTED"],
                        )
                    )
                    console.print(
                        render_routing(
                            routing, mode, evaluation_index=routing_index, now=routing_now
                        )
                    )
                elif name == "context-view":
                    console.print(render_context(context, mode))
                elif name == "trace":
                    console.print(render_trace(trace, mode))
                elif name == "demo-claims":
                    console.print(render_claims(claims, mode))
            text_lines = console.export_text(clear=False, styles=False).splitlines()
            files = save_capture(console, output_dir, name, width)
            manifest["screens"].append(
                {
                    "name": name,
                    "width": width,
                    "rows": len(text_lines),
                    "max_columns": max((Text(line).cell_len for line in text_lines), default=0),
                    **files,
                }
            )
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--scenario",
        type=Path,
        required=True,
        help="saved run directory containing events.jsonl and receipt.json",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--width", type=int, action="append", help="repeat for multiple widths; default: 110"
    )
    parser.add_argument(
        "--scenario-label",
        default=SCENARIO_LABEL,
        help="visible provenance label; identify scripted/offline boundaries here",
    )
    parser.add_argument("--overwrite", action="store_true", help="replace existing gallery files")
    args = parser.parse_args(argv)
    try:
        scenario = load_scenario(args.scenario)
        manifest = render_gallery(
            scenario,
            args.output_dir,
            args.width or [110],
            scenario_label=args.scenario_label,
            overwrite=args.overwrite,
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    print(
        f"Saved {len(manifest['screens'])} SVG/text pairs and manifest.json to {args.output_dir.resolve()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
