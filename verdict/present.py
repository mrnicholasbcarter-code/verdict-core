"""Shared human-output helpers for `verdict` commands (BOD-187).

Every command renders its human (non-``--json``) output through these helpers so
the whole CLI shares one identity: the Verdict header, labelled sections, status
glyphs, tables and key/value blocks from ``verdict.terminal_ui`` tokens, with the
same plain / NO_COLOR / non-TTY fallback. Machine output never passes through here.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from verdict.terminal_ui import TOKENS, TerminalUI, clean

_ui: TerminalUI | None = None


def ui() -> TerminalUI:
    """Presenter bound to the *current* ``sys.stdout``.

    A cached console would keep writing to a stream that was replaced (pytest
    capture, redirected output), so a fresh presenter is built unless a test
    installed one explicitly with :func:`reset`.
    """
    if _ui is not None:
        return _ui
    return TerminalUI(Console(file=sys.stdout))


def reset(presenter: TerminalUI | None = None) -> None:
    """Testing hook: install a presenter bound to a captured console."""
    global _ui
    _ui = presenter


def header(title: str) -> None:
    ui().header(title)


def section(title: str) -> None:
    ui().section(title)


def status(label: str, state: str, detail: str = "") -> None:
    ui().status(label, state, detail)


def ok(label: str, detail: str = "") -> None:
    ui().status(label, "ok", detail)


def fail(label: str, detail: str = "") -> None:
    ui().status(label, "failed", detail)


def warn(label: str, detail: str = "") -> None:
    ui().status(label, "warning", detail)


def kv(rows: Mapping[str, Any] | Iterable[tuple[str, Any]], *, title: str | None = None) -> None:
    """Aligned key/value block (labels muted, values plain text)."""
    presenter = ui()
    if presenter.machine:
        return
    if title:
        presenter.section(title)
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="" if presenter.plain else TOKENS["MUTED"])
    grid.add_column()
    items = rows.items() if isinstance(rows, Mapping) else rows
    for key, value in items:
        grid.add_row(Text(f"  {clean(key)}"), Text(clean("-" if value is None else value)))
    presenter.console.print(grid)


def table(
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
    *,
    title: str | None = None,
    empty: str = "nothing to show",
) -> None:
    """Token-styled table; plain mode drops box drawing and colour."""
    presenter = ui()
    if presenter.machine:
        return
    if title:
        presenter.section(title)
    grid = Table(
        box=None if presenter.plain else box.SIMPLE_HEAD,
        pad_edge=False,
        show_edge=False,
        header_style="" if presenter.plain else TOKENS["SECONDARY"],
    )
    for name in columns:
        grid.add_column(clean(name), overflow="fold")
    count = 0
    for row in rows:
        grid.add_row(*(Text(clean("-" if cell is None else cell)) for cell in row))
        count += 1
    if count:
        presenter.console.print(grid)
    else:
        presenter.console.print(
            Text(f"  {empty}", style="" if presenter.plain else TOKENS["MUTED"])
        )


def note(text: str) -> None:
    presenter = ui()
    if not presenter.machine:
        presenter.console.print(
            Text(f"  {clean(text)}", style="" if presenter.plain else TOKENS["MUTED"])
        )
