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
from verdict.tui_completion import (
    ArgumentSpec,
    CommandSpec,
    CompletionSnapshot,
    VerdictCompleter,
    default_command_specs,
    suggest_command,
    syntax_help,
)

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
        "unscoped verified evidence; bounded refresh, progress and consent",
        "models.verified",
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
    (
        "Setup",
        "bootstrap",
        "Prime interactive picker; read-only Claude compatibility",
        "harness.prime.select.preview",
    ),
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

_PROBE_MAX_BYTES = 32 * 1024 * 1024
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
    completion_snapshot: CompletionSnapshot | None = None
    completion_view: dict[str, Any] | None = None


def _plain(console: Console) -> bool:
    return TerminalUI(console).plain


def probe_gateway(url: str, *, timeout: float = 3.0) -> tuple[bool | None, int | None]:
    """Bounded, unauthenticated reachability ping; never raises."""
    try:
        scheme = urllib.parse.urlsplit(url).scheme
    except Exception:
        return None, None
    if scheme not in {"http", "https"}:
        return None, None
    try:
        request = urllib.request.Request(f"{url.rstrip('/')}/v1/models", method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # nosec B310 — scheme validated above
            # Bounded read: large catalogs are several MB; anything past the
            # cap is treated as a failed probe rather than read into memory.
            raw = resp.read(_PROBE_MAX_BYTES + 1)
            if len(raw) > _PROBE_MAX_BYTES:
                return False, None
            data = json.loads(raw)
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
        footer = Text("verdict --help  ·  VERDICT_NO_ANIMATION=1", style=TOKENS["MUTED"])
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

    values = dict(params or {})
    if action_name == "models.verified" and values.pop("_consumer", False):
        from verdict.actions.verified_models import consume_verified_models

        result = consume_verified_models(**values)
    else:
        result = run_action(action_name, values or None)
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
    if ok and isinstance(data, dict) and data.get("schema") == "verdict.verified-models/v1":
        from verdict.orchestration.verified_models_render import (
            render_verified_plain,
            render_verified_table,
        )

        console.print(
            Text(render_verified_plain(data)) if tui.plain else render_verified_table(data)
        )
        return

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
            console.print(Text(f"ERROR: {err_msg}"))
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


def command_specs() -> tuple[CommandSpec, ...]:
    """One immutable palette grammar for dispatch help and offline completion."""
    defaults = {spec.name: spec for spec in default_command_specs()}
    specs = []
    for _section, name, description, action in PALETTE:
        spec = defaults.get(name, CommandSpec(name, description, "/" + name, ()))
        arguments = spec.arguments
        if not arguments and action in _ACTION_PARAMS:
            arguments = tuple(
                ArgumentSpec(pname, "field", True, secret=pname == "value")
                for pname, _default in _ACTION_PARAMS[action]
            )
        specs.append(CommandSpec(name, description, spec.syntax, arguments))
    specs.extend(spec for name, spec in defaults.items() if name in {"help", "quit", "clear"})
    return tuple(specs)


def _suggest_command(text: str) -> str | None:
    hints = suggest_command(text, commands=command_specs(), limit=1)
    return hints[0] if hints else None


def _reload_completion(state: HomeState) -> None:
    from verdict.actions.verified_models import utc_now
    from verdict.tui_completion_snapshot import load_snapshot

    state.completion_snapshot = load_snapshot(now=utc_now(), runs=state.runs)


def _publish_completion(state: HomeState, console: Console) -> None:
    from verdict.actions.verified_models import StorePaths, VerifiedSnapshotAdapter, utc_now
    from verdict.tui_completion_snapshot import local_projection, publish_snapshot

    adapter = VerifiedSnapshotAdapter(state.gateway, StorePaths.defaults(), local_only=True)
    try:
        view = state.completion_view or local_projection(adapter, now=utc_now())
        state.completion_view = None
        warning = publish_snapshot(view)
    except (ValueError, OSError, TypeError):
        warning = "completion snapshot publication failed; evidence unchanged"
    if warning:
        console.print(Text(warning, style=TOKENS["MUTED"]))
    _reload_completion(state)


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
                try:
                    params[param_name] = _probe_model_ids(val)
                except ValueError:
                    console.print(Text("/probe expects a nonempty exact model list, not options"))
                    return None
            else:
                params[param_name] = val
    return params


def _probe_model_ids(text: str) -> list[str]:
    from verdict.tui_verified_controls import ControlsError, parse_probe_model_list

    ids = parse_probe_model_list(text)
    if any(route_id.startswith("-") for route_id in ids):
        raise ControlsError("/probe expects exact model ids, not options such as --probe")
    return ids


def _run_verified_command(
    text: str, *, tui: TerminalUI, state: HomeState, line_reader: Callable[[], str | None] | None
) -> tuple[bool, Any]:
    from verdict.orchestration.verified_models import VerifiedModelQuery
    from verdict.tui_verified_controls import parse_eligibility_args

    parsed = parse_eligibility_args(text)
    query = VerifiedModelQuery(
        status=parsed.status, provider=parsed.provider, search=parsed.search, page=parsed.page
    )

    def read_line(_prompt: str) -> str | None:
        return line_reader() if line_reader is not None else input()

    from verdict.actions.verified_models import StorePaths, VerifiedSnapshotAdapter, utc_now
    from verdict.tui_completion_snapshot import local_projection

    adapter = VerifiedSnapshotAdapter(state.gateway, StorePaths.defaults())
    result = run_palette_action(
        "models.verified",
        {
            "_consumer": True,
            "gateway": state.gateway,
            "adapter": adapter,
            "query": query,
            "manual": parsed.refresh,
            "read_line": read_line,
            "write": lambda line: tui.console.print(Text(line), end="\n"),
            "live": True,
        },
    )
    if result[0] and adapter._metadata_loaded:
        state.completion_view = local_projection(adapter, now=utc_now())
    return result


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
                _show_help(console, cmd_args, state.completion_snapshot)
                return None
            if palette_cmd == "clear":
                return "clear"
            return None

        if cmd_args == "--help":
            _show_help(console, palette_cmd, state.completion_snapshot)
            return None
        if palette_cmd == "probe" and any(
            token.startswith("-") for token in cmd_args.replace(",", " ").split()
        ):
            console.print(Text("Invalid /probe model field: options are not model ids."))
            _show_help(console, "probe", state.completion_snapshot)
            return None
        if palette_cmd == "bootstrap":
            from verdict.actions.registry import run_action
            from verdict.tui_bootstrap_controls import consume_prime, parse_bootstrap_args

            try:
                parsed = parse_bootstrap_args(cmd_args)
                if parsed.target == "claude":
                    result = run_action(
                        "harness.claude.compat", {"selected_ids": parsed.ids, "mode": parsed.mode}
                    )
                else:
                    result = consume_prime(
                        parsed,
                        gateway=state.gateway,
                        read_line=lambda _prompt: (
                            line_reader() if line_reader is not None else input()
                        ),
                        write=lambda line: console.print(Text(line)),
                        on_projection=lambda view: setattr(state, "completion_view", dict(view)),
                        live=True,
                    )
                _render_action_result(tui, result.ok, result.data, width=width)
                if parsed.target == "prime" and not parsed.restore:
                    _publish_completion(state, console)
            except (ValueError, KeyboardInterrupt):
                console.print(Text("Invalid bootstrap input or cancelled; no apply."))
                _show_help(console, "bootstrap", state.completion_snapshot)
            return None
        if ref == "models.verified":
            try:
                ok, data = _run_verified_command(
                    cmd_args, tui=tui, state=state, line_reader=line_reader
                )
            except (ValueError, KeyboardInterrupt):
                ok, data = (
                    False,
                    {
                        "error": "invalid /eligibility arguments or cancelled; use [status] [provider=..] [search=..] [page=..] [refresh]"
                    },
                )
            _render_action_result(tui, ok, data, width=width)
            if ok:
                _publish_completion(state, console)
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
                if ref == "probe":
                    from verdict.actions.model_refresh import SPEND_NOTE
                    from verdict.tui_verified_controls import VerifiedControlsController

                    raw_models = (params or {}).get("models", "")
                    models = _probe_model_ids(
                        " ".join(raw_models) if isinstance(raw_models, list) else str(raw_models)
                    )
                    controller = VerifiedControlsController(
                        read_line=lambda _prompt: (
                            line_reader() if line_reader is not None else input()
                        ),
                        write=lambda line: console.print(Text(line)),
                        run_refresh=lambda *_a, **_k: None,
                    )
                    console.print(
                        Text(f"Probe {len(models)} exact model ids: " + ", ".join(models))
                    )
                    console.print(Text(SPEND_NOTE))
                    if not controller.confirm("Run bounded liveness probes?").granted:
                        console.print(Text("(cancelled; no probes)"))
                        return None
                    params = {
                        **(params or {}),
                        "models": models,
                        "allow_live_probe": True,
                        "base_url": state.gateway.rstrip("/").removesuffix("/v1") + "/v1",
                    }
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
        except Exception as exc:
            # A failing command reports its error and returns to the prompt.
            ok, data = False, {"error": f"{type(exc).__name__}: {clean(str(exc))[:200]}"}

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


def _show_help(
    console: Console, command: str = "", snapshot: CompletionSnapshot | None = None
) -> None:
    """Same local syntax and typo help for both prompt paths."""
    if command:
        from verdict.actions.verified_models import utc_now
        from verdict.tui_completion_snapshot import load_snapshot

        help_result = syntax_help(
            command.strip().lstrip("/"),
            commands=command_specs(),
            snapshot=snapshot or load_snapshot(now=utc_now()),
            now=utc_now(),
        )
        if help_result is None:
            console.print(
                Text("Unknown help command. " + (_suggest_command(command) or "Use /help"))
            )
            return
        console.print(Text(help_result.syntax))
        console.print(Text(help_result.description))
        for argument in help_result.arguments:
            console.print(Text("; ".join(f"{key}: {value}" for key, value in argument.items())))
        return
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
    """Load history (one command per line); returns empty list on failure.

    Lines in prompt_toolkit's FileHistory format (``# <timestamp>`` headers and
    ``+``-prefixed entries, written by an earlier build) are converted, so an
    old file never shows its markers as commands.
    """
    try:
        if not path.exists():
            return []
        entries: list[str] = []
        for line in path.read_text().splitlines():
            if not line.strip() or line.startswith("# "):
                continue
            entry = line[1:] if line.startswith("+") else line
            if entry.strip():
                entries.append(entry.strip())
        return entries[-HISTORY_MAX_LINES:]
    except Exception:
        return []


def _save_history(path: Path, entries: list[str]) -> None:
    """Append and cap the history file; owner-only permissions."""
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # mkdir's mode does not apply to an existing directory: tighten it.
        os.chmod(path.parent, 0o700)
        existing = _load_history(path) if path.exists() else []
        combined = (existing + entries)[-HISTORY_MAX_LINES:]
        # Write a new owner-only file and atomically replace the old one, so
        # the history is never readable by others, even for a moment.
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(combined) + "\n")
        os.replace(tmp, path)
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

    Single deadline: the sweep and the checking state share ``max_sweep_s``.
    After the budget the "checking…" line stays until the probe returns
    (bounded by the probe's own timeout). No ``time.sleep``; timing comes
    from ``threading.Event.wait`` and the existing ``TerminalUI.task`` Live
    region with pulse/synchronized_output.
    """
    if probe_fn is not None:
        done = threading.Event()
        result: list[tuple[bool | None, int | None]] = []

        def _probe_worker() -> None:
            try:
                result.append(probe_fn())
            except Exception:
                # A probe that raises is a failed probe, never a hang.
                result.append((False, None))
            finally:
                done.set()

        t = threading.Thread(target=_probe_worker, daemon=True)
        t.start()
    else:
        done = threading.Event()
        done.set()
        result = [(state.gateway_ok, state.gateway_models)]

    # Sweep: reveal wordmark lines, sharing a single budget with the
    # checking state.  Each line waits up to line_delay for the probe;
    # when the probe is already done the wait returns immediately.
    line_delay = max_sweep_s / max(len(WORDMARK) + 2, 1)
    for line in WORDMARK:
        target.print(Text(line.rstrip(), style=TOKENS["PRIMARY"]))
        if hasattr(target, "file"):
            target.file.flush()
        # Always print every line; only the pacing stops once the probe is
        # done (the wait returns at once), so the sweep never delays for effect.
        if not done.is_set():
            done.wait(timeout=line_delay)

    # Show tagline
    target.print(Text("AUTONOMOUS CONTROL PLANE", style=TOKENS["TEXT"]))
    target.print(Text("plan · select · recover · verify · prove", style=TOKENS["SECONDARY"]))
    target.print()

    # Wait for probe if still running: use TerminalUI.task for a pulsing
    # "Checking gateway reachability" Live region (with synchronized_output
    # and border_highlight from verdict/motion.py), instead of raw ANSI.
    if probe_fn is not None and not done.is_set():
        ui = TerminalUI(target)
        with ui.task("Checking gateway reachability"):
            # Block until the probe finishes. The probe's own timeout
            # bounds this; we do not impose our own deadline.
            done.wait()

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
    if isinstance(state, HomeState):
        _reload_completion(state)

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
            if isinstance(state, HomeState):
                _reload_completion(state)
            if result == "quit":
                return 0
            if result == "clear":
                target.clear()
        return 0

    # Production: use prompt_toolkit when it can start; fall back to plain
    # input() only when it cannot (no real terminal, missing library). Errors
    # raised later, while a command runs, are not a reason to switch loops.
    try:
        _prompt_toolkit_ready()
    except Exception:
        return _fallback_input_loop(target, tui, state)
    return _prompt_toolkit_loop(target, tui, state)


def _prompt_toolkit_ready() -> None:
    """Raise if prompt_toolkit cannot drive this terminal."""
    from prompt_toolkit.output import create_output

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError("prompt_toolkit needs a real terminal")
    create_output()


def completion_key_bindings() -> Any:
    """Tab cycles; Enter inserts only; Esc restores the pre-menu line."""
    from prompt_toolkit.filters import has_completions
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.key_binding.key_processor import KeyPressEvent

    bindings = KeyBindings()

    @bindings.add("tab")
    def tab(event: KeyPressEvent) -> None:
        buffer = event.current_buffer
        if buffer.complete_state:
            buffer.complete_next()
        else:
            buffer.start_completion(select_first=True)

    @bindings.add("enter", filter=has_completions)
    def enter(event: KeyPressEvent) -> None:
        buffer = event.current_buffer
        if buffer.complete_state and buffer.complete_state.current_completion:
            buffer.apply_completion(buffer.complete_state.current_completion)
        else:
            buffer.complete_state = None

    @bindings.add("escape", filter=has_completions)
    def escape(event: KeyPressEvent) -> None:
        event.current_buffer.cancel_completion()

    return bindings


def _prompt_toolkit_loop(target: Console, tui: TerminalUI, state: HomeState) -> int:
    """Interactive loop using prompt_toolkit with completions and history."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import InMemoryHistory

    # History: capped at HISTORY_MAX_LINES and owner-only (0700 dir, 0600 file).
    # Loaded once into memory; new entries are written back through the capped
    # _save_history helper, so the file never grows past the cap.
    history: Any = InMemoryHistory()
    for entry in _load_history(HISTORY_FILE):
        history.append_string(entry)

    # Suppress CPR (cursor position request) warning in terminals that
    # do not support it. Verdict does not need cursor position info.
    os.environ.setdefault("PROMPT_TOOLKIT_NO_CPR", "1")

    from verdict.actions.verified_models import utc_now
    from verdict.tui_completion_snapshot import load_snapshot

    completer = VerdictCompleter(
        command_specs(), state.completion_snapshot or load_snapshot(now=utc_now()), utc_now()
    )
    session: PromptSession[str] = PromptSession(
        message="verdict › ",  # noqa: RUF001
        completer=completer,
        key_bindings=completion_key_bindings(),
        history=history,
        complete_while_typing=False,
    )

    while True:
        # Time changes labels only; no keystroke I/O or live reads.
        completer.now = utc_now()
        try:
            line = session.prompt()
        except KeyboardInterrupt:
            continue
        except EOFError:
            return 0
        if line.strip():
            _save_history(HISTORY_FILE, [line.strip()])
        result = _run_command(line, tui=tui, state=state)
        _reload_completion(state)
        completer.snapshot = state.completion_snapshot or completer.snapshot
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
        if line.strip():
            _save_history(HISTORY_FILE, [line.strip()])
        result = _run_command(line, tui=tui, state=state)
        _reload_completion(state)
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
                try:
                    params[param_name] = _probe_model_ids(val)
                except ValueError:
                    console.print(Text("/probe expects a nonempty exact model list, not options"))
                    return None
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
