"""``verdict`` with no subcommand: the Verdict home screen (BOD-186).

Presentation only. Facts come from local state that is cheap to read (recent run
directories and receipts) plus an optional, bounded gateway ping. No routing,
selection or recovery decisions live here.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich import box
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from verdict.terminal_ui import TOKENS, clean

WORDMARK = (
    "██╗   ██╗███████╗██████╗ ██████╗ ██╗ ██████╗████████╗",
    "██║   ██║██╔════╝██╔══██╗██╔══██╗██║██╔════╝╚══██╔══╝",
    "██║   ██║█████╗  ██████╔╝██║  ██║██║██║        ██║   ",
    "╚██╗ ██╔╝██╔══╝  ██╔══██╗██║  ██║██║██║        ██║   ",
    " ╚████╔╝ ███████╗██║  ██║██████╔╝██║╚██████╗   ██║   ",
    "  ╚═══╝  ╚══════╝╚═╝  ╚═╝╚═════╝ ╚═╝ ╚═════╝   ╚═╝   ",
)
TAGLINE = "autonomous control plane  ·  plan · select · recover · verify · prove"

# Grouped command palette: (group, command, one-line purpose). Kept in sync with
# the parser by tests (every entry must be a registered subcommand).
PALETTE: tuple[tuple[str, str, str], ...] = (
    ("Run", "orchestrate", "goal -> DAG -> parallel workers -> review -> receipt"),
    ("Run", "supervise", "run orchestrate under a stall/quota-aware supervisor"),
    ("Run", "watch", "live view of a run (or --once for a snapshot)"),
    ("Evidence", "run-receipt", "verify a run receipt and show per-node attempts"),
    ("Evidence", "receipt", "inspect routing receipts (RoutingReceiptV1)"),
    ("Models", "eligibility", "DISCOVERED > ENTITLED > HEALTHY > AVAILABLE > ELIGIBLE"),
    ("Models", "models", "local model catalog view"),
    ("Models", "route", "route one task through the gate"),
    ("Setup", "doctor", "health of gateways, harnesses, memory, docs"),
    ("Setup", "setup", "plan or apply capability bootstrap"),
    ("Setup", "quickstart", "credential-free deterministic demo"),
)


@dataclass
class HomeState:
    gateway: str = ""
    gateway_ok: bool | None = None
    gateway_models: int | None = None
    runs: list[dict[str, Any]] = field(default_factory=list)


def _plain(console: Console) -> bool:
    return (
        not console.is_terminal
        or "NO_COLOR" in os.environ
        or os.getenv("TERM") == "dumb"
        or bool(os.getenv("CI"))
        or os.getenv("VERDICT_PLAIN") == "1"
    )


def probe_gateway(url: str, *, timeout: float = 3.0) -> tuple[bool | None, int | None]:
    """Bounded, unauthenticated reachability ping; never raises."""
    if urllib.parse.urlsplit(url).scheme not in {"http", "https"}:
        return None, None
    try:
        request = urllib.request.Request(url.rstrip("/") + "/v1/models")
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
            data = json.loads(response.read(32 * 1024 * 1024)).get("data")
            return True, len(data) if isinstance(data, list) else None
    except Exception:
        return False, None


def recent_runs(roots: Sequence[Path], *, limit: int = 5) -> list[dict[str, Any]]:
    """Newest orchestration runs under the given ``.verdict/runs`` roots."""
    found: list[tuple[float, Path]] = []
    for root in roots:
        if root.is_dir():
            for run in root.iterdir():
                events = run / "events.jsonl"
                if events.is_file():
                    found.append((events.stat().st_mtime, run))
    rows: list[dict[str, Any]] = []
    for mtime, run in sorted(found, reverse=True)[:limit]:
        outcome, reason = "RUNNING", ""
        receipt = run / "receipt.json"
        if receipt.is_file():
            try:
                data = json.loads(receipt.read_text(encoding="utf-8"))
                outcome = str(data.get("outcome") or data.get("claimed_outcome") or "?")
                reason = str(data.get("reason") or "")
            except (OSError, ValueError):
                outcome = "UNREADABLE"
        rows.append(
            {
                "run": run.name,
                "path": str(run),
                "outcome": outcome,
                "reason": reason,
                "age_s": max(0, int(time.time() - mtime)),
            }
        )
    return rows


def _age(seconds: int) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def render_home(
    state: HomeState, *, plain: bool, width: int, reveal: int | None = None
) -> RenderableType:
    """Home screen. ``reveal`` animates the wordmark column-by-column (None = full)."""
    blocks: list[RenderableType] = []
    if plain:
        blocks.append(Text("VERDICT  autonomous control plane"))
    else:
        mark = Text()
        for i, line in enumerate(WORDMARK):
            shown = line if reveal is None else line[:reveal]
            mark.append(shown + "\n", style=TOKENS["PRIMARY"] if i < 3 else TOKENS["SECONDARY"])
        mark.append(TAGLINE, style=TOKENS["MUTED"])
        blocks.append(
            Panel(
                mark,
                box=box.HEAVY,
                border_style=TOKENS["PRIMARY"],
                padding=(0, 2),
                width=min(width, 96),
            )
        )
    gw = (
        "reachable"
        if state.gateway_ok
        else "unreachable"
        if state.gateway_ok is False
        else "not checked"
    )
    status = Table.grid(padding=(0, 2))
    status.add_column(style="" if plain else TOKENS["MUTED"])
    status.add_column()
    status.add_row(
        Text("gateway"),
        Text(
            f"{clean(state.gateway)}  ({gw})"
            + (f"  {state.gateway_models} models discovered" if state.gateway_models else ""),
            style="" if plain else TOKENS["SUCCESS" if state.gateway_ok else "WARNING"],
        ),
    )
    if state.runs:
        for row in state.runs:
            tone = "SUCCESS" if row["outcome"] == "COMPLETE" else "ERROR"
            status.add_row(
                Text(f"run {clean(row['run'])[:24]}"),
                Text(
                    f"{row['outcome']:<9} {_age(row['age_s'])} ago  {clean(row['reason'])[:60]}",
                    style="" if plain else TOKENS[tone],
                ),
            )
    else:
        status.add_row(Text("runs"), Text('none yet - try: verdict orchestrate "<goal>" --repo .'))
    blocks.append(
        Panel(
            status,
            title=Text("STATUS", style="" if plain else TOKENS["SECONDARY"]),
            box=box.ASCII if plain else box.ROUNDED,
            border_style="" if plain else TOKENS["BORDER"],
            width=min(width, 96),
        )
    )
    palette = Table(
        box=None if plain else box.SIMPLE,
        pad_edge=False,
        show_edge=False,
        header_style="" if plain else TOKENS["MUTED"],
    )
    for name in ("", "command", "what it does"):
        palette.add_column(name)
    last = ""
    for group, command, purpose in PALETTE:
        palette.add_row(
            Text(group if group != last else ""), Text(f"verdict {command}"), Text(purpose)
        )
        last = group
    blocks.append(
        Panel(
            palette,
            title=Text("COMMANDS", style="" if plain else TOKENS["SECONDARY"]),
            box=box.ASCII if plain else box.ROUNDED,
            border_style="" if plain else TOKENS["BORDER"],
            width=min(width, 96),
        )
    )
    blocks.append(
        Text(
            "verdict --help lists every command · VERDICT_NO_ANIMATION=1 disables motion",
            style="" if plain else TOKENS["MUTED"],
        )
    )
    return Group(*blocks)


def run_home(
    *,
    console: Console | None = None,
    gateway: str | None = None,
    runs_roots: Sequence[Path] | None = None,
    animate: bool | None = None,
    probe: bool = True,
) -> int:
    target = console or Console()
    plain = _plain(target)
    state = HomeState(
        gateway=gateway or os.environ.get("VERDICT_GATEWAY", "http://127.0.0.1:20128")
    )
    roots = list(runs_roots) if runs_roots is not None else [Path.cwd() / ".verdict" / "runs"]
    state.runs = recent_runs(roots)
    if probe:
        state.gateway_ok, state.gateway_models = probe_gateway(state.gateway)
    width = target.width or 100
    motion = (
        (
            not plain
            and target.is_terminal
            and os.getenv("VERDICT_NO_ANIMATION") != "1"
            and not os.getenv("SSH_CONNECTION")
        )
        if animate is None
        else animate
    )
    if motion:
        with Live(
            render_home(state, plain=False, width=width, reveal=0),
            console=target,
            refresh_per_second=30,
            transient=True,
        ) as live:
            for step in range(0, len(WORDMARK[0]) + 1, 3):
                live.update(render_home(state, plain=False, width=width, reveal=step))
                time.sleep(0.012)
    target.print(render_home(state, plain=plain, width=width))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    return run_home(console=Console(file=sys.stdout))
