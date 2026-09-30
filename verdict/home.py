"""``verdict`` with no subcommand: the Verdict home screen.

Presentation only. Facts come from local state that is cheap to read (recent run
directories and receipts) plus an optional, bounded gateway ping. No routing,
selection or recovery decisions live here.
"""

from __future__ import annotations

import difflib
import importlib
import json
import os
import sys
import threading
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

from verdict.design import TOKENS, PresentationMode, panel, presentation_mode
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

# ---------------------------------------------------------------------------
# Grouped command palette: (group, command, one-line purpose). Kept in sync with
# the parser by tests (every entry must be a registered subcommand).
# TUI section → action name mapping. Commands with an action name invoke
# run_action() in-process; others remain CLI-only launch hints.
# ---------------------------------------------------------------------------
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

# Map bare or slash-prefixed name to (palette_cmd, action_name, kind)
# Built lazily from PALETTE on first use.
_COMMAND_INDEX: dict[str, tuple[str, str, str]] | None = None

HISTORY_DIR = Path.home() / ".verdict"
HISTORY_FILE = HISTORY_DIR / "prompt_history"
HISTORY_MAX_LINES = 500


def _build_command_index() -> dict[str, tuple[str, str, str]]:
    """Build a lookup: bare name → (palette_cmd, action_or_launch_ref, kind).

    Supports both ``/demo`` and ``demo`` as lookup keys.
    """
    from verdict.actions.registry import LAUNCH

    index: dict[str, tuple[str, str, str]] = {}
    for _section, cmd, _desc, action in PALETTE:
        if action:
            index[cmd] = (cmd, action, "action")
            index[f"/{cmd}"] = (cmd, action, "action")
        elif cmd in LAUNCH:
            index[cmd] = (cmd, cmd, "launch")
            index[f"/{cmd}"] = (cmd, cmd, "launch")
    # Additional shorthand aliases
    index["quit"] = ("quit", "", "builtin")
    index["/quit"] = ("quit", "", "builtin")
    index["exit"] = ("quit", "", "builtin")
    index["/exit"] = ("quit", "", "builtin")
    index["help"] = ("help", "", "builtin")
    index["/help"] = ("help", "", "builtin")
    index["clear"] = ("clear", "", "builtin")
    index["/clear"] = ("clear", "", "builtin")
    index["runs"] = ("watch", "watch", "action")
    index["/runs"] = ("watch", "watch", "action")
    return index


def _get_command_index() -> dict[str, tuple[str, str, str]]:
    global _COMMAND_INDEX
    if _COMMAND_INDEX is None:
        _COMMAND_INDEX = _build_command_index()
    return _COMMAND_INDEX


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
        request = urllib.request.Request(f"{url.rstrip('/')}/v1/models", method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            # Read full response (catalogs can exceed 3 MB).
            data = json.loads(resp.read())
        models = data.get("data", [])
        return True, len(models) if isinstance(models, list) else None
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
    state: HomeState, *, plain: bool = True, width: int = 100, interactive: bool = False
) -> RenderableType:
    """Build the home renderable. No motion, no I/O.

    ``interactive=True`` omits the full command list in favour of a hint line
    (the command prompt shows completions instead).
    Branding is stable; only the real gateway probe may animate, in run_home.
    """
    if not plain:
        return _styled_home(state, width=width, interactive=interactive)
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


def _styled_home(state: HomeState, *, width: int, interactive: bool = False) -> RenderableType:
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

    parts: list[RenderableType] = [
        panel(mark, mode=mode, tone="PRIMARY"),
        panel(gateway, title="01 / CONNECTION", mode=mode),
        panel(work, title="02 / RECENT WORK", mode=mode),
    ]

    if interactive:
        # Hint line instead of the full command table.
        hint = Text(
            "Type a goal, or / for commands · /demo to see it work · Ctrl-D to quit",
            style=TOKENS["MUTED"],
        )
        parts.append(hint)
    else:
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
        parts.append(controls_panel)

    return Group(*parts)


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
        return False, {"error": f"{module_path!r} has no attribute {func_name!r}"}
    try:
        result = func(**params)
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
    elif isinstance(data, dict) and "text" in data and isinstance(data["text"], str):
        # Text-bearing result (e.g. demo output): print text directly
        console.print(clean(data["text"]))
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


def _render_table(console: Console, data: list[dict[str, Any]], *, plain: bool, width: int) -> None:
    keys = list(data[0].keys())
    narrow = width < 70
    if narrow:
        keys = keys[:3]
    table = Table(
        box=None if plain else box.SIMPLE,
        pad_edge=False,
        show_edge=False,
        header_style="" if plain else TOKENS["MUTED"],
        width=min(width, 96),
    )
    for k in keys:
        table.add_column(clean(k), overflow="fold" if not narrow else "ellipsis")
    for row_dict in data[:50]:
        table.add_row(*(clean(str(row_dict.get(k, "")))[: 120 if not narrow else 30] for k in keys))
    console.print(table)


def _render_kv_panel(console: Console, data: dict[str, Any], *, plain: bool, width: int) -> None:
    kv = Table.grid(padding=(0, 2))
    kv.add_column(style="" if plain else TOKENS["MUTED"])
    kv.add_column()
    for k, v in data.items():
        if isinstance(v, dict):
            # Nested dict: render as indented lines
            kv.add_row(Text(clean(str(k))), Text(""))
            for sk, sv in v.items():
                if isinstance(sv, dict):
                    kv.add_row(Text(f"  {clean(str(sk))}"), Text(""))
                    for ssk, ssv in sv.items():
                        kv.add_row(Text(f"    {clean(str(ssk))}"), Text(clean(str(ssv))[:200]))
                else:
                    kv.add_row(Text(f"  {clean(str(sk))}"), Text(clean(str(sv))[:200]))
        else:
            kv.add_row(Text(clean(str(k))), Text(clean(str(v))[:200]))
    if plain:
        console.print(kv)
    else:
        mode = PresentationMode(True, True, False, width)
        console.print(panel(kv, mode=mode))


# ---------------------------------------------------------------------------
# Command prompt (replaces the old _interactive_palette selector)
# ---------------------------------------------------------------------------


def _suggest_command(text: str) -> str | None:
    """Return the closest /command name for unknown input, or None."""
    all_cmds = [f"/{cmd}" for _, cmd, _, _ in PALETTE]
    all_cmds.extend(["/quit", "/help", "/clear", "/exit"])
    matches = difflib.get_close_matches(
        text if text.startswith("/") else f"/{text}", all_cmds, n=1, cutoff=0.5
    )
    return matches[0] if matches else None


def _prompt_params_inline(
    action_name: str, console: Console, *, line_reader: Callable[[], str | None] | None = None
) -> dict[str, Any] | None:
    """Prompt for required/optional params inline.

    Returns params dict, or None if the user cancels (empty required param).
    ``line_reader`` is used in tests to inject lines; returns None on EOF.
    """
    param_specs = _ACTION_PARAMS.get(action_name, [])
    params: dict[str, Any] = {}
    for param_name, default in param_specs:
        required = default is _REQUIRED
        prompt_text = (
            f"  {param_name} (required): " if required else f"  {param_name} [{default!r}]: "
        )
        console.print(Text(prompt_text, style=TOKENS["MUTED"]), end="")
        try:
            if line_reader is not None:
                val = line_reader()
                if val is None:
                    return None
                val = val.strip()
                console.print(val)
            else:
                val = input().strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not val:
            if required:
                return None
            params[param_name] = default
        else:
            if param_name == "models":
                params[param_name] = [v.strip() for v in val.split(",") if v.strip()]
            else:
                params[param_name] = val
    return params


def _run_command(
    text: str,
    *,
    tui: TerminalUI,
    state: HomeState,
    line_reader: Callable[[], str | None] | None = None,
) -> str | None:
    """Execute one command. Returns "quit" to exit, "clear" to clear, None to continue."""
    console = tui.console
    width = console.width or 100
    stripped = text.strip()
    if not stripped:
        return None

    # Normalize: accept both /cmd and cmd
    index = _get_command_index()

    # Check for builtins
    lower = stripped.lower()
    if lower in ("quit", "/quit", "exit", "/exit", "\x04"):
        return "quit"
    if lower in ("help", "/help"):
        _show_help(console)
        return None
    if lower in ("clear", "/clear"):
        return "clear"
    if lower == "/":
        # Show completions
        _show_help(console)
        return None

    # Look up as command
    parts = stripped.split(None, 1)
    cmd_word = parts[0]
    cmd_args = parts[1] if len(parts) > 1 else ""

    entry = index.get(cmd_word) or index.get(cmd_word.lower())
    if entry is not None:
        palette_cmd, ref, kind = entry
        if kind == "builtin":
            if palette_cmd == "quit":
                return "quit"
            if palette_cmd == "help":
                _show_help(console)
                return None
            if palette_cmd == "clear":
                return "clear"
            return None

        # Resolve params: if args on the line, use them for the first required param
        params: dict[str, Any] | None = {}
        param_specs = _ACTION_PARAMS.get(ref, [])
        if cmd_args and param_specs:
            first_param = param_specs[0][0]
            params = {first_param: cmd_args}
            # Prompt remaining params
            remaining = param_specs[1:]
            if remaining:
                for pname, pdefault in remaining:
                    required = pdefault is _REQUIRED
                    prompt_text = (
                        f"  {pname} (required): " if required else f"  {pname} [{pdefault!r}]: "
                    )
                    console.print(Text(prompt_text, style=TOKENS["MUTED"]), end="")
                    try:
                        if line_reader is not None:
                            val = line_reader()
                            if val is None:
                                console.print(Text("(cancelled)", style=TOKENS["MUTED"]))
                                return None
                            val = val.strip()
                            console.print(val)
                        else:
                            val = input().strip()
                    except (EOFError, KeyboardInterrupt):
                        console.print(Text("(cancelled)", style=TOKENS["MUTED"]))
                        return None
                    if not val:
                        if required:
                            console.print(Text("(cancelled)", style=TOKENS["MUTED"]))
                            return None
                        params[pname] = pdefault
                    else:
                        params[pname] = val
        elif param_specs:
            params = _prompt_params_inline(ref, console, line_reader=line_reader)
            if params is None:
                console.print(Text("(cancelled)", style=TOKENS["MUTED"]))
                return None

        try:
            if kind == "action":
                ok, data = run_palette_action(ref, params if params else None)
            else:
                from verdict.actions.registry import LAUNCH

                launch_val = LAUNCH.get(palette_cmd)
                if launch_val is None:
                    ok, data = False, {"error": f"no LAUNCH entry for {palette_cmd!r}"}
                elif hasattr(launch_val, "entry"):
                    ok, data = _call_launch_entry(launch_val.entry, params or {})
                else:
                    ok, data = (
                        False,
                        {
                            "error": f"{palette_cmd!r} is a long-running launch ({launch_val}); run it directly in your terminal"
                        },
                    )
        except KeyboardInterrupt:
            ok, data = False, {"error": "cancelled"}

        _render_action_result(tui, ok, data, width=width)
        return None

    # Not a known command. Is it a goal (free text)?
    if not stripped.startswith("/"):
        repo = os.getcwd()
        console.print()
        console.print(
            Text(
                "Run orchestrate on this goal? This launches live workers and may spend credits.",
                style=TOKENS["WARNING"],
            )
        )
        console.print(Text(f"  repo: {clean(repo)}", style=TOKENS["MUTED"]))
        console.print(Text(f"  goal: {clean(stripped)[:120]}", style=TOKENS["MUTED"]))
        try:
            if line_reader is not None:
                console.print(Text("  [y/N] ", style=TOKENS["ACCENT"]), end="")
                answer = line_reader()
                if answer is None:
                    answer = ""
                answer = answer.strip()
                console.print(answer)
            else:
                console.print(Text("  [y/N] ", style=TOKENS["ACCENT"]), end="")
                answer = input().strip()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer.lower() == "y":
            try:
                from verdict.actions.registry import LAUNCH

                launch_val = LAUNCH.get("orchestrate")
                if launch_val is not None and hasattr(launch_val, "entry"):
                    ok, data = _call_launch_entry(
                        launch_val.entry, {"goal": stripped, "repo": repo}
                    )
                    _render_action_result(tui, ok, data, width=width)
                else:
                    console.print(
                        Text(
                            f'Run: verdict orchestrate "{clean(stripped)[:80]}" --repo {clean(repo)}',
                            style=TOKENS["ACCENT"],
                        )
                    )
            except KeyboardInterrupt:
                console.print(Text("(cancelled)", style=TOKENS["MUTED"]))
        return None

    # Unknown /command
    suggestion = _suggest_command(stripped)
    msg = f"Unknown command: {clean(stripped)[:60]}"
    if suggestion:
        msg += f"  — did you mean {suggestion}?"
    console.print(Text(msg, style=TOKENS["WARNING"]))
    return None


def _show_help(console: Console) -> None:
    """Print a compact command list, each group shown once."""
    from collections import OrderedDict

    groups: OrderedDict[str, list[tuple[str, str]]] = OrderedDict()
    for group, cmd, purpose, _action in PALETTE:
        groups.setdefault(group, []).append((cmd, purpose))

    table = Table.grid(padding=(0, 2))
    table.add_column(style=TOKENS["ACCENT"], no_wrap=True)
    table.add_column(style=TOKENS["SECONDARY"])
    for group, entries in groups.items():
        table.add_row(Text(group.upper(), style=TOKENS["PRIMARY"]), Text(""))
        for cmd, purpose in entries:
            table.add_row(Text(f"/{cmd}"), Text(purpose))
    table.add_row(Text("SESSION", style=TOKENS["PRIMARY"]), Text(""))
    table.add_row(Text("/clear"), Text("clear screen"))
    table.add_row(Text("/help"), Text("show this list"))
    table.add_row(Text("/quit"), Text("exit (or Ctrl-D)"))
    console.print(table)


def _load_history(path: Path) -> list[str]:
    """Load history from file; returns empty list on failure."""
    try:
        if path.exists():
            return path.read_text().splitlines()[-HISTORY_MAX_LINES:]
    except Exception:
        pass
    return []


def _save_history(path: Path, entries: list[str]) -> None:
    """Append and cap the history file."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = _load_history(path) if path.exists() else []
        combined = (existing + entries)[-HISTORY_MAX_LINES:]
        path.write_text("\n".join(combined) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Startup motion: wordmark sweep while probing in a background thread
# ---------------------------------------------------------------------------


def _startup_with_motion(
    target: Console,
    state: HomeState,
    *,
    probe_fn: Callable[[], tuple[bool | None, int | None]] | None = None,
    max_sweep_s: float = 1.2,
) -> None:
    """Show wordmark line-by-line while probing gateway in a background thread.

    The sweep ends when probe finishes OR max_sweep_s is reached, whichever
    comes first. No ``time.sleep`` — uses threading.Event with timeout.
    The wordmark reveal spreads across ``max_sweep_s`` so the user sees motion.
    """
    if probe_fn is not None:
        done = threading.Event()
        result: list[tuple[bool | None, int | None]] = []

        def _probe_worker() -> None:
            result.append(probe_fn())
            done.set()

        t = threading.Thread(target=_probe_worker, daemon=True)
        t.start()
    else:
        done = threading.Event()
        done.set()
        result = [(state.gateway_ok, state.gateway_models)]

    # Sweep: reveal wordmark lines while waiting for probe.
    # Each line waits up to line_delay for the probe; if the probe is still
    # running the delay produces the visual sweep effect.
    line_delay = max_sweep_s / max(len(WORDMARK) + 2, 1)
    for line in WORDMARK:
        target.print(Text(line.rstrip(), style=TOKENS["PRIMARY"]))
        if hasattr(target, "file"):
            target.file.flush()
        done.wait(timeout=line_delay)

    # Show tagline
    target.print(Text("AUTONOMOUS CONTROL PLANE", style=TOKENS["TEXT"]))
    target.print(Text("plan · select · recover · verify · prove", style=TOKENS["SECONDARY"]))
    target.print()

    # Show "checking…" while probe may still be running
    if probe_fn is not None and not done.is_set():
        checking = Text("gateway  ", style=TOKENS["SECONDARY"])
        checking.append("checking…", style=TOKENS["MUTED"])
        checking.append(f"  {clean(state.gateway)}", style=TOKENS["MUTED"])
        target.print(checking)
        if hasattr(target, "file"):
            target.file.flush()
        # Wait for probe to finish (remaining time)
        done.wait(timeout=max(0, max_sweep_s))
        # Move cursor up to overwrite the "checking" line
        if hasattr(target, "file"):
            target.file.write("\033[1A\033[2K")
            target.file.flush()

    # Resolve probe result
    if result:
        state.gateway_ok, state.gateway_models = result[0]
    gw_label = (
        "REACHABLE"
        if state.gateway_ok
        else "UNREACHABLE"
        if state.gateway_ok is False
        else "NOT CHECKED"
    )
    gw_style = TOKENS[
        "SUCCESS" if state.gateway_ok else "ERROR" if state.gateway_ok is False else "MUTED"
    ]
    gw_line = Text("gateway  ", style=TOKENS["SECONDARY"])
    gw_line.append(gw_label, style=gw_style)
    gw_line.append(f"  {clean(state.gateway)}", style=TOKENS["MUTED"])
    if state.gateway_models:
        gw_line.append(f"  ({state.gateway_models} models)", style=TOKENS["ACCENT"])
    target.print(gw_line)

    # Show recent runs
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
            run_line = Text(f"  {clean(row['run'])[:24]}  ", style=TOKENS["TEXT"])
            run_line.append(f"{outcome}  {_age(row['age_s'])} ago", style=TOKENS[tone])
            target.print(run_line)
    target.print()


# ---------------------------------------------------------------------------
# Interactive command prompt (prompt_toolkit-based with fallback)
# ---------------------------------------------------------------------------


def _command_prompt(
    target: Console, state: HomeState, *, line_reader: Callable[[], str | None] | None = None
) -> int:
    """Command prompt loop. Returns exit code.

    ``line_reader`` is injected in tests. Returns the typed line (without
    newline) or None on EOF. In production, prompt_toolkit handles the input.
    """
    tui = TerminalUI(target)

    if line_reader is not None:
        # Test mode: use injected reader
        _iter_count = 0
        _max_iters = 10000
        while _iter_count < _max_iters:
            _iter_count += 1
            try:
                line = line_reader()
            except (EOFError, KeyboardInterrupt):
                return 0
            if line is None:
                return 0
            result = _run_command(line, tui=tui, state=state, line_reader=line_reader)
            if result == "quit":
                return 0
            if result == "clear":
                target.clear()
        return 0

    # Production: try prompt_toolkit, fall back to plain input()
    try:
        return _prompt_toolkit_loop(target, tui, state)
    except Exception:
        return _fallback_input_loop(target, tui, state)


def _prompt_toolkit_loop(target: Console, tui: TerminalUI, state: HomeState) -> int:
    """Interactive loop using prompt_toolkit with completions and history."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.history import FileHistory, InMemoryHistory

    class VerdictCompleter(Completer):
        def get_completions(self, document: Any, complete_event: Any) -> Any:
            text = document.text_before_cursor.lstrip()
            # Slash-prefix completions
            if text.startswith("/") or not text:
                prefix = text.lstrip("/")
                for _section, cmd, desc, _action in PALETTE:
                    if cmd.startswith(prefix):
                        yield Completion(
                            f"/{cmd}",
                            start_position=-len(text),
                            display=f"/{cmd}",
                            display_meta=desc[:50],
                        )
                for builtin_cmd, builtin_desc in [
                    ("quit", "exit"),
                    ("help", "show commands"),
                    ("clear", "clear screen"),
                ]:
                    if builtin_cmd.startswith(prefix):
                        yield Completion(
                            f"/{builtin_cmd}",
                            start_position=-len(text),
                            display=f"/{builtin_cmd}",
                            display_meta=builtin_desc,
                        )
                return

            # Complete run IDs for commands that take a run arg
            parts = text.split(None, 1)
            if len(parts) >= 1:
                cmd_part = parts[0].lstrip("/")
                # Commands that accept run IDs
                run_cmds = {"trace", "routing", "context", "watch", "run-receipt", "receipt"}
                if cmd_part in run_cmds and state.runs:
                    partial = parts[1] if len(parts) > 1 else ""
                    for row in state.runs:
                        run_id = row["run"]
                        if run_id.startswith(partial):
                            yield Completion(
                                run_id,
                                start_position=-len(partial),
                                display=run_id[:30],
                                display_meta=f"{row['outcome']} {_age(row['age_s'])} ago",
                            )

    # History
    try:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        history: Any = FileHistory(str(HISTORY_FILE))
    except Exception:
        history = InMemoryHistory()

    # Suppress CPR (cursor position request) warning in terminals that
    # do not support it. Verdict does not need cursor position info.
    os.environ.setdefault("PROMPT_TOOLKIT_NO_CPR", "1")

    session: PromptSession[str] = PromptSession(
        message="verdict › ",  # noqa: RUF001
        completer=VerdictCompleter(),
        history=history,
        complete_while_typing=False,
    )

    while True:
        try:
            line = session.prompt()
        except KeyboardInterrupt:
            continue
        except EOFError:
            return 0
        result = _run_command(line, tui=tui, state=state)
        if result == "quit":
            return 0
        if result == "clear":
            target.clear()


def _fallback_input_loop(target: Console, tui: TerminalUI, state: HomeState) -> int:
    """Plain input() loop when prompt_toolkit fails."""
    while True:
        try:
            line = input("verdict › ")  # noqa: RUF001
        except KeyboardInterrupt:
            continue
        except EOFError:
            return 0
        result = _run_command(line, tui=tui, state=state)
        if result == "quit":
            return 0
        if result == "clear":
            target.clear()


# ---------------------------------------------------------------------------
# Param prompting helpers (legacy, kept for backward compatibility in tests)
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
                val = input().strip()
            except (EOFError, KeyboardInterrupt):
                return None
        if not val:
            if required:
                return None
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
    """Command prompt interface.

    TTY only. Non-TTY, NO_COLOR, CI environments skip this entirely.

    ``key_reader`` replaces the prompt session in tests. When provided, it
    returns one line at a time (the full typed input). Raising EOFError means
    quit. This replaces the old arrow-key selector.
    """
    # Wrap key_reader (char-at-a-time) into a line reader if needed
    if key_reader is not None:

        def line_reader() -> str | None:
            buf: list[str] = []
            try:
                while len(buf) < 512:
                    ch = key_reader()
                    if ch in ("\r", "\n"):
                        return "".join(buf)
                    if ch in ("\x03",):  # Ctrl-C
                        raise KeyboardInterrupt
                    if ch in ("\x04",):  # Ctrl-D
                        return None
                    buf.append(ch)
            except EOFError:
                if buf:
                    return "".join(buf)
                return None
            return "".join(buf)

        return _command_prompt(target, state, line_reader=line_reader)
    return _command_prompt(target, state)


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
    mode = presentation_mode(target.file if hasattr(target, "file") else None)

    # F4: interactive prompt — TTY only; non-TTY/NO_COLOR/CI unchanged.
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

    if want_interactive and mode.animate and animate is not False:
        # Animated startup: sweep wordmark while probing gateway
        probe_fn = (lambda: probe_gateway(state.gateway)) if probe else None
        _startup_with_motion(target, state, probe_fn=probe_fn)
        # Show the hint line
        target.print(
            Text(
                "Type a goal, or / for commands · /demo to see it work · Ctrl-D to quit",
                style=TOKENS["MUTED"],
            )
        )
        target.print()
    else:
        # No animation: probe synchronously, render full home
        if probe:
            if plain:
                state.gateway_ok, state.gateway_models = probe_gateway(state.gateway)
            else:
                with ui.task("Checking gateway reachability"):
                    state.gateway_ok, state.gateway_models = probe_gateway(state.gateway)
        width = target.width or 100
        target.print(render_home(state, plain=plain, width=width, interactive=want_interactive))

    if want_interactive:
        return _interactive_palette(target, state, key_reader=_key_reader)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    return run_home(console=Console(file=sys.stdout))
