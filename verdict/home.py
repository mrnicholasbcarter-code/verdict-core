"""``verdict`` with no subcommand: the Verdict home screen.

Presentation only. Facts come from local state that is cheap to read (recent run
directories and receipts) plus an optional, bounded gateway ping. No routing,
selection or recovery decisions live here.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich import box
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from verdict.design import TOKENS, PresentationMode, panel
from verdict.terminal_ui import TerminalUI, clean

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
# TUI section → action name mapping. Commands with an action name invoke
# run_action() in-process; others remain CLI-only launch hints.
PALETTE: tuple[tuple[str, str, str, str], ...] = (
    ("Runs", "orchestrate", "goal -> DAG -> parallel workers -> review -> receipt", ""),
    ("Runs", "supervise", "run orchestrate under a stall/quota-aware supervisor", ""),
    ("Runs", "watch", "live view of a run (or --once for a snapshot)", ""),
    ("Traces", "trace", "human-readable trace of an orchestration run", "trace.view"),
    ("Traces", "demo", "credential-free deterministic demo (offline scenario)", "demo.run"),
    ("Traces", "run-receipt", "verify an orchestration receipt (proof surface)", "run-receipt"),
    ("Traces", "receipt", "inspect routing receipts (RoutingReceiptV1)", "receipt.show"),
    ("Traces", "replay", "replay a routing decision session", "replay"),
    (
        "Health",
        "eligibility",
        "DISCOVERED > ENTITLED > HEALTHY > AVAILABLE > ELIGIBLE",
        "eligibility",
    ),
    ("Health", "probe", "one-token liveness probes", "probe"),
    ("Health", "detect", "detect reachable providers", "detect"),
    ("Models", "models", "local model catalog view", "models.list"),
    ("Models", "catalog", "OmniRoute catalog dump", "catalog"),
    ("Routing", "route", "route one task through the gate", "route"),
    ("Routing", "routing", "recorded routing explorer for a run", "routing.view"),
    ("Context", "context", "recorded context budget and provenance", "context.view"),
    ("Routing", "compare", "compare dual-route results", "compare"),
    ("Overview", "stats", "statistics from routing decision log", "stats"),
    ("Overview", "suggest", "suggestions from routing history", "suggest"),
    ("Overview", "cost-report", "cost report from routing decisions", "cost-report"),
    ("Config", "config", "show resolved configuration (secrets redacted)", "config.show"),
    ("Config", "credentials", "manage stored credentials", "credentials.list"),
    ("Setup", "doctor", "health of gateways, harnesses, memory, docs", "doctor"),
    ("Setup", "setup", "plan or apply capability bootstrap", "setup.plan"),
    ("Setup", "quickstart", "credential-free deterministic demo", ""),
)

# ---------------------------------------------------------------------------
# Required-param metadata for in-process prompt.
# Maps action name → list of (param_name, default | _REQUIRED).
# Only parameters that the action function retrieves via kwargs["name"] (no
# default) are listed here; optional ones are omitted so the palette works
# zero-friction for read-only actions.
# ---------------------------------------------------------------------------
_REQUIRED = object()  # sentinel — no default

_ACTION_PARAMS: dict[str, list[tuple[str, object]]] = {
    "route": [("task", _REQUIRED), ("criticality", "medium")],
    "compare": [("task", _REQUIRED), ("criticality", "medium")],
    "receipt.show": [("run_dir", _REQUIRED)],
    "run-receipt": [("run_dir", _REQUIRED)],
    "trace.view": [("run_dir", _REQUIRED)],
    "routing.view": [("run", _REQUIRED)],
    "context.view": [("run", _REQUIRED)],
    "replay": [("session_id", _REQUIRED)],
    "probe": [("models", _REQUIRED)],
    "credentials.set": [("name", _REQUIRED), ("value", _REQUIRED)],
    "credentials.unset": [("name", _REQUIRED)],
    # Actions with no required params have no entry here.
}


@dataclass
class HomeState:
    gateway: str = ""
    gateway_ok: bool | None = None
    gateway_models: int | None = None
    runs: list[dict[str, Any]] = field(default_factory=list)


def _plain(console: Console) -> bool:
    return TerminalUI(console).plain


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
        # Event-log existence is not evidence that a worker is still running.
        outcome, reason = "NO RECEIPT", "Execution state not observed"
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
    """Render observed facts. ``reveal`` is retained for API compatibility only.

    Branding is stable; only the real gateway probe may animate, in run_home.
    """
    if not plain:
        return _styled_home(state, width=width)
    blocks: list[RenderableType] = []
    blocks.append(Text("VERDICT  autonomous control plane"))
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
    for group, command, purpose, _action in PALETTE:
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


def _styled_home(state: HomeState, *, width: int) -> RenderableType:
    """One stable hierarchy: identity, observed health, recent work, controls."""
    mode = PresentationMode(True, True, False, width)
    mark = Text()
    if width >= 66:
        for line in WORDMARK:
            mark.append(line.rstrip() + "\n", style=TOKENS["PRIMARY"])
    else:
        mark.append("VERDICT\n", style=TOKENS["PRIMARY"])
    mark.append("AUTONOMOUS CONTROL PLANE\n", style=TOKENS["TEXT"])
    mark.append("plan · select · recover · verify · prove", style=TOKENS["SECONDARY"])
    gateway = Text("GATEWAY  ", style=TOKENS["SECONDARY"])
    gateway.append(
        "REACHABLE"
        if state.gateway_ok
        else "UNREACHABLE"
        if state.gateway_ok is False
        else "NOT CHECKED",
        style=TOKENS[
            "SUCCESS" if state.gateway_ok else "ERROR" if state.gateway_ok is False else "MUTED"
        ],
    )
    gateway.append("\n" + clean(state.gateway), style=TOKENS["TEXT"])
    if state.gateway_models is not None:
        gateway.append(f"\n{state.gateway_models} models discovered", style=TOKENS["ACCENT"])
    gateway.append("\nInventory is not task eligibility.", style=TOKENS["MUTED"])
    if state.gateway_ok is False:
        gateway.append("\nRepair: verdict doctor", style=TOKENS["WARNING"])
    work = Text()
    if state.runs:
        for row in state.runs:
            outcome = str(row["outcome"])
            tone = (
                "SUCCESS"
                if outcome == "COMPLETE"
                else "MUTED"
                if outcome in {"RUNNING", "NO RECEIPT"}
                else "ERROR"
            )
            work.append(clean(row["run"]) + "  ", style=TOKENS["TEXT"])
            work.append(clean(outcome) + f"  ·  {_age(row['age_s'])} ago\n", style=TOKENS[tone])
            if row.get("reason"):
                work.append(clean(row["reason"]) + "\n", style=TOKENS["SECONDARY"])
    else:
        work.append("No runs yet.\n", style=TOKENS["SECONDARY"])
        work.append('Start: verdict orchestrate "<goal>" --repo .', style=TOKENS["ACCENT"])
    controls = Table.grid(padding=(0, 2), expand=True)
    controls.add_column(style=TOKENS["ACCENT"], no_wrap=True)
    controls.add_column(style=TOKENS["SECONDARY"], overflow="fold")
    last = ""
    for group, command, purpose, _action in PALETTE:
        if group != last:
            controls.add_row(Text(group.upper(), style=TOKENS["PRIMARY"]), "")
        controls.add_row(Text(f"verdict {command}", style=TOKENS["TEXT"]), Text(purpose))
        last = group
    footer = Text("↑/↓ select  ·  Enter run  ·  q quit", style=TOKENS["ACCENT"])
    footer.append("\nverdict --help  ·  VERDICT_NO_ANIMATION=1", style=TOKENS["MUTED"])
    controls_panel = panel(Group(controls, footer), title="03 / COMMANDS", mode=mode)
    return Group(
        panel(mark, mode=mode, tone="PRIMARY"),
        panel(gateway, title="01 / CONNECTION", mode=mode),
        panel(work, title="02 / RECENT WORK", mode=mode),
        controls_panel,
    )


def palette_actions() -> list[tuple[str, str, str, str]]:
    """Return palette entries that have a mapped action name (non-empty 4th field)."""
    return [(section, cmd, desc, action) for section, cmd, desc, action in PALETTE if action]


def palette_launches() -> list[tuple[str, str, str, str]]:
    """Return palette entries that map to a LAUNCH entry (no action name, cmd in LAUNCH).

    Returns list of (section, cmd, desc, launch_key) where launch_key == cmd.
    Uses the LAUNCH registry which lane E will populate as dict[str, LaunchSpec].
    Falls back gracefully when LAUNCH values are plain strings (pre-lane-E shape).
    """
    from verdict.actions.registry import LAUNCH

    result = []
    for section, cmd, desc, action in PALETTE:
        if action:
            continue  # already handled by palette_actions
        if cmd in LAUNCH:
            result.append((section, cmd, desc, cmd))
    return result


def run_palette_action(action_name: str, params: dict[str, Any] | None = None) -> tuple[bool, Any]:
    """Run a palette action in-process via the shared action layer.

    Returns ``(ok, data)`` — the caller decides presentation.
    """
    from verdict.actions import run_action

    result = run_action(action_name, params)
    return result.ok, result.data


def _call_launch_entry(entry: str, params: dict[str, Any]) -> tuple[bool, Any]:
    """Invoke a launch entry-point function in-process via importlib.

    ``entry`` has the form ``"pkg.module:function"``.  The function is called
    with **params and its return value is normalised to ``(ok, data)``.
    Never calls subprocess.
    """
    if ":" not in entry:
        return False, {"error": f"invalid entry format (expected 'pkg.module:function'): {entry}"}
    module_path, func_name = entry.rsplit(":", 1)
    try:
        mod = importlib.import_module(module_path)
    except ImportError as exc:
        return False, {"error": f"cannot import {module_path!r}: {exc}"}
    func: Callable[..., Any] | None = getattr(mod, func_name, None)
    if func is None:
        return False, {"error": f"{func_name!r} not found in {module_path!r}"}
    try:
        result = func(**params)
    except (KeyboardInterrupt, SystemExit):
        raise  # re-raise to outer handler
    except Exception as exc:
        return False, {"error": str(exc)}
    # Normalise result to (ok, data)
    if hasattr(result, "ok") and hasattr(result, "data"):
        return bool(result.ok), result.data
    if isinstance(result, int):
        return result == 0, {"exit_code": result}
    return True, result


def _render_action_result(tui: TerminalUI, ok: bool, data: Any, *, width: int = 100) -> None:
    """Render an ActionResult payload through TerminalUI.

    - list[dict]: table
    - dict: key/value panel
    - ok=False: error panel
    - narrow (width<70): truncate table columns to fit
    """
    console = tui.console
    effective_width = max(40, min(width, console.width or width))

    if not ok:
        # Error panel
        err_msg = ""
        if isinstance(data, dict):
            err_msg = str(data.get("error") or json.dumps(data, default=str))
        elif data is not None:
            err_msg = clean(str(data))[:500]
        else:
            err_msg = "action failed with no error detail"
        if tui.plain:
            console.print(f"ERROR: {err_msg}")
        else:
            console.print(
                panel(
                    Text(clean(err_msg), style=TOKENS["ERROR"]),
                    title="Error",
                    tone="ERROR",
                    mode=tui.mode,
                )
            )
        return

    if isinstance(data, list) and data and isinstance(data[0], dict):
        _render_table(console, data, plain=tui.plain, width=effective_width)
    elif isinstance(data, dict):
        _render_kv_panel(console, data, plain=tui.plain, width=effective_width)
    elif isinstance(data, list):
        # list of non-dicts
        if tui.plain:
            for item in data[:50]:
                console.print(f"  {clean(str(item))}")
        else:
            lines = "\n".join(clean(str(item)) for item in data[:50])
            console.print(panel(Text(lines), mode=tui.mode))
    elif data is None:
        if tui.plain:
            console.print("ok (no data)")
        else:
            console.print(Text("✓ ok", style=TOKENS["SUCCESS"]))
    else:
        console.print(clean(str(data))[:2000])


def _render_table(
    console: Console, rows_data: list[dict[str, Any]], *, plain: bool, width: int
) -> None:
    """Render list[dict] as a Rich table, handling narrow terminals."""
    if not rows_data:
        console.print(Text("(empty)", style=TOKENS["MUTED"] if not plain else ""))
        return
    all_keys = list(dict.fromkeys(k for row in rows_data for k in row))
    # Narrow: show at most 3 columns
    narrow = width < 70
    keys = all_keys[:3] if narrow else all_keys

    table = Table(
        box=None if plain else box.SIMPLE,
        pad_edge=False,
        show_edge=False,
        header_style="" if plain else TOKENS["MUTED"],
    )
    col_width = max(8, (width - 4) // max(1, len(keys)))
    for k in keys:
        table.add_column(str(k), max_width=col_width, overflow="fold")
    for row in rows_data[:100]:
        table.add_row(*(clean(str(row.get(k, "")))[:col_width] for k in keys))
    console.print(table)
    if len(rows_data) > 100:
        console.print(
            Text(
                f"  … {len(rows_data) - 100} more rows", style=TOKENS["MUTED"] if not plain else ""
            )
        )


def _render_kv_panel(console: Console, data: dict[str, Any], *, plain: bool, width: int) -> None:
    """Render a dict as a key/value panel."""
    effective_width = min(width, 96)
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="" if plain else TOKENS["MUTED"], no_wrap=True)
    grid.add_column(overflow="fold", max_width=max(20, effective_width - 24))
    for k, v in data.items():
        val_str = (
            json.dumps(v, default=str, ensure_ascii=False)
            if isinstance(v, (dict, list))
            else clean(str(v))
        )
        grid.add_row(clean(str(k)), val_str[:500])
    if plain:
        console.print(grid)
    else:
        console.print(panel(grid, mode=PresentationMode(True, True, False, width)))


# ---------------------------------------------------------------------------
# Param prompting helpers
# ---------------------------------------------------------------------------


def _prompt_params(
    action_name: str, console: Console, *, key_reader: Callable[[], str] | None = None
) -> dict[str, Any] | None:
    """Prompt for required/optional params for an action.

    Uses a single-line prompt per param (defaults shown).  Returns None if the
    user cancels (enters blank for a required param or types 'q').
    ``key_reader`` is the raw key source — injected in tests to avoid real
    terminal reads; when None, falls back to ``input()``.
    """
    param_specs = _ACTION_PARAMS.get(action_name, [])
    params: dict[str, Any] = {}
    for param_name, default in param_specs:
        required = default is _REQUIRED
        prompt = f"  {param_name} (required): " if required else f"  {param_name} [{default!r}]: "
        console.print(Text(prompt, style=TOKENS["MUTED"]), end="")
        if key_reader is not None:
            # Test injection: read chars until newline.
            # Hard cap at 512 chars; EOFError from exhausted reader → cancel.
            buf: list[str] = []
            try:
                while len(buf) < 512:
                    ch = key_reader()
                    if ch in ("\r", "\n"):
                        break
                    if ch in ("\x03", "\x04"):  # Ctrl-C / Ctrl-D → cancel
                        return None
                    buf.append(ch)
            except (EOFError, KeyboardInterrupt):
                return None
            val = "".join(buf).strip()
        else:
            try:
                val = input()
            except (EOFError, KeyboardInterrupt):
                return None
        if not val:
            if required:
                console.print(
                    Text("  (cancelled — required param not provided)", style=TOKENS["MUTED"])
                )
                return None
            if default is not _REQUIRED:
                # Use default; coerce list-type defaults
                if param_name == "models" and isinstance(default, str):
                    params[param_name] = [default]
                else:
                    params[param_name] = default
        else:
            # Coerce known list params
            if param_name == "models":
                params[param_name] = [v.strip() for v in val.split(",") if v.strip()]
            else:
                params[param_name] = val
    return params


def _interactive_palette(
    target: Console, state: HomeState, *, key_reader: Callable[[], str] | None = None
) -> int:
    """Keyboard selector: arrow/number selection + enter runs the action.

    TTY only. Non-TTY, NO_COLOR, CI environments skip this entirely.

    ``key_reader`` replaces raw stdin reads in tests (inject a function that
    returns one character at a time).
    """
    tui = TerminalUI(target)
    action_entries = palette_actions()
    launch_entries = palette_launches()

    # Build flat entry list: (section, cmd, desc, kind, ref)
    # kind = "action" | "launch"
    entries: list[tuple[str, str, str, str, str]] = []
    for section, cmd, desc, action_name in action_entries:
        entries.append((section, cmd, desc, "action", action_name))
    for section, cmd, desc, launch_key in launch_entries:
        entries.append((section, cmd, desc, "launch", launch_key))

    if not entries:
        return 0

    selected = 0
    width = target.width or 100

    # termios/tty only needed for real TTY mode (not test injection)
    fd: int | None = None
    old_settings: list[Any] | None = None
    if key_reader is None:
        import termios
        import tty

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)

    def _render_selector() -> None:
        nonlocal width
        width = target.width
        target.print()
        target.print(Text("ACTION PALETTE", style=TOKENS["SECONDARY"]))
        target.print(
            Text("↑/↓ or number to select, Enter to run, q to quit", style=TOKENS["MUTED"])
        )
        target.print()
        for i, (section, cmd, desc, kind, _ref) in enumerate(entries):
            marker = "▸ " if i == selected else "  "
            style = TOKENS["PRIMARY"] if i == selected else ""
            tag = " [launch]" if kind == "launch" else ""
            line_width = max(40, (width or 80) - 4)
            label = f"{marker}{i + 1:2d}. [{section}] {cmd}{tag}"
            # Truncate desc to fit in narrow terminals
            remaining = line_width - len(label) - 4
            shown_desc = desc[: max(0, remaining)] if remaining < len(desc) else desc
            line = f"{label} — {shown_desc}"
            target.print(Text(line[:line_width], style=style))
        target.print()

    def _read_key() -> str:
        if key_reader is not None:
            return key_reader()
        return sys.stdin.read(1)

    try:
        if key_reader is None:
            import tty

            assert fd is not None  # set above in the same key_reader is None branch
            tty.setcbreak(fd)

        running = True
        _iter_count = 0
        _max_iters = 10000  # safety cap — prevents runaway loops in tests
        while running and _iter_count < _max_iters:
            _iter_count += 1
            _render_selector()

            try:
                ch = _read_key()
            except (EOFError, KeyboardInterrupt):
                return 0
            if ch in ("", "q", "Q"):
                target.print(Text("quit", style=TOKENS["MUTED"]))
                return 0
            elif ch in ("\r", "\n"):
                section, cmd, desc, kind, ref = entries[selected]
                target.print(Text(f"\nRunning: verdict {cmd}", style=TOKENS["SUCCESS"]))
                target.print()

                # Prompt for params
                if key_reader is not None:
                    params = _prompt_params(
                        ref if kind == "action" else cmd, target, key_reader=key_reader
                    )
                else:
                    params = _prompt_params(ref if kind == "action" else cmd, target)

                if params is None:
                    target.print(Text("(cancelled)", style=TOKENS["MUTED"]))
                    target.print()
                    target.print(
                        Text("Press any key to continue, q to quit", style=TOKENS["MUTED"])
                    )
                    try:
                        ch2 = _read_key()
                    except (EOFError, KeyboardInterrupt):
                        return 0
                    if ch2 in ("q", "Q"):
                        return 0
                    continue

                # Restore terminal to cooked mode for action execution
                if key_reader is None and fd is not None and old_settings is not None:
                    import contextlib
                    import termios

                    with contextlib.suppress(Exception):
                        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

                try:
                    if kind == "action":
                        ok, data = run_palette_action(ref, params if params else None)
                    else:
                        # LAUNCH: call entry in-process via importlib
                        from verdict.actions.registry import LAUNCH

                        launch_val = LAUNCH.get(cmd)
                        if launch_val is None:
                            ok, data = False, {"error": f"no LAUNCH entry for {cmd!r}"}
                        elif hasattr(launch_val, "entry"):
                            # LaunchSpec shape (lane E)
                            ok, data = _call_launch_entry(launch_val.entry, params or {})
                        else:
                            # Pre-lane-E: LAUNCH value is a plain string (reason)
                            ok, data = (
                                False,
                                {
                                    "error": f"{cmd!r} is a long-running launch ({launch_val}); run it directly in your terminal"
                                },
                            )
                except KeyboardInterrupt:
                    ok, data = False, {"error": "cancelled"}
                finally:
                    # Re-enter cbreak mode
                    if key_reader is None and fd is not None:
                        import tty

                        with contextlib.suppress(Exception):
                            tty.setcbreak(fd)

                _render_action_result(tui, ok, data, width=width)
                target.print()
                target.print(Text("Press any key to continue, q to quit", style=TOKENS["MUTED"]))
                try:
                    ch2 = _read_key()
                except (EOFError, KeyboardInterrupt):
                    return 0
                if ch2 in ("q", "Q"):
                    return 0

            elif ch == "\x1b":
                # Escape sequence (arrow keys)
                try:
                    seq1 = _read_key()
                    seq2 = _read_key()
                except (EOFError, KeyboardInterrupt):
                    return 0
                seq = seq1 + seq2
                if seq == "[A":  # Up
                    selected = max(0, selected - 1)
                elif seq == "[B":  # Down
                    selected = min(len(entries) - 1, selected + 1)
            elif ch.isdigit():
                num = int(ch)
                if 1 <= num <= len(entries):
                    selected = num - 1
            elif ch in ("\x03", "\x04"):  # Ctrl-C / Ctrl-D
                return 0

    except (KeyboardInterrupt, EOFError):
        return 0
    finally:
        if _iter_count >= _max_iters:
            target.print(
                Text("Safety iteration cap reached; exiting palette.", style=TOKENS["WARNING"])
            )
        if key_reader is None and fd is not None and old_settings is not None:
            import contextlib
            import termios

            with contextlib.suppress(Exception):
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    return 0


def run_home(
    *,
    console: Console | None = None,
    gateway: str | None = None,
    runs_roots: Sequence[Path] | None = None,
    animate: bool | None = None,
    probe: bool = True,
    interactive: bool | None = None,
    _key_reader: Callable[[], str] | None = None,
) -> int:
    target = console or Console()
    plain = _plain(target)
    state = HomeState(
        gateway=gateway or os.environ.get("VERDICT_GATEWAY", "http://127.0.0.1:20128")
    )
    roots = list(runs_roots) if runs_roots is not None else [Path.cwd() / ".verdict" / "runs"]
    state.runs = recent_runs(roots)
    ui = TerminalUI(target)
    # An explicit True never overrides accessibility or terminal policy.
    ui.animate = ui.animate and animate is not False
    if probe:
        if plain:
            state.gateway_ok, state.gateway_models = probe_gateway(state.gateway)
        else:
            with ui.task("Checking gateway reachability"):
                state.gateway_ok, state.gateway_models = probe_gateway(state.gateway)
    width = target.width or 100
    target.print(render_home(state, plain=plain, width=width))
    # F4: interactive palette — TTY only; non-TTY/NO_COLOR/CI unchanged.
    want_interactive = (
        interactive
        if interactive is not None
        else (
            target.is_terminal
            and not plain
            and not os.getenv("CI")
            and "NO_COLOR" not in os.environ
        )
    )
    if want_interactive:
        return _interactive_palette(target, state, key_reader=_key_reader)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    return run_home(console=Console(file=sys.stdout))
