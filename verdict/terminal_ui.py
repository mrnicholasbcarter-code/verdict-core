"""Verdict's human presentation boundary. No setup or health decisions live here."""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from time import monotonic
from typing import Any, TextIO, cast

from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.live import Live
from rich.prompt import Confirm, Prompt
from rich.segment import Segment
from rich.style import Style
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from verdict.design import PALETTE, ColorSystem, PresentationMode
from verdict.design import (
    TOKENS as TOKENS,  # re-export; keep `from verdict.terminal_ui import TOKENS` working
)
from verdict.design import panel as design_panel
from verdict.motion import MotionClock, border_highlight, pulse, synchronized_output

_CONTROLS = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))|[\x00-\x08\x0b-\x1f\x7f-\x9f]"
)
_GOOD = {"ok", "found", "healthy", "covered", "certified", "qualified", "success", "applied"}
_BAD = {"failed", "error", "blocked"}


def clean(value: object) -> str:
    """External labels are text, never Rich markup or terminal instructions."""
    return _CONTROLS.sub("", str(value)).replace("\t", " ")


def rows(value: object) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


class _Elapsed:
    """A light and border for an observed operation, never a progress estimate."""

    def __init__(self, label: str, mode: PresentationMode) -> None:
        self.label = clean(label)
        self.started = monotonic()
        self.mode = mode
        self.clock = MotionClock(mode)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        phase = self.clock.phase()
        strength = pulse(phase, mode=self.mode, state="running")
        rgb = tuple(round(channel * strength) for channel in PALETTE["PURPLE"].rgb)
        light = Style(color="rgb({},{},{})".format(*rgb))
        text = Text("● ", style=light)
        text.append("RUNNING  ", style=TOKENS["PRIMARY"])
        text.append(self.label, style=TOKENS["TEXT"])
        text.append(f"  ·  {int(monotonic() - self.started)}s", style=TOKENS["MUTED"])
        component = design_panel(text, mode=self.mode)
        lines = console.render_lines(component, options, pad=False)
        cell = border_highlight(
            phase, max(0, options.max_width - 2), mode=self.mode, state="running"
        )
        for row, line in enumerate(lines):
            position = 0
            for segment in line:
                if (
                    row == 0
                    and cell is not None
                    and position <= cell + 1 < position + segment.cell_length
                ):
                    before, rest = segment.split_cells(cell + 1 - position)
                    highlight, after = rest.split_cells(1)
                    yield before
                    yield Segment(
                        highlight.text, (highlight.style or Style()) + Style(color=TOKENS["ACCENT"])
                    )
                    yield after
                else:
                    yield segment
                position += segment.cell_length
            if row < len(lines) - 1:
                yield Segment.line()


class _SynchronizedLive(Live):
    """One short DEC-2026 frame; no input or work executes inside the marker."""

    def __init__(self, content: _Elapsed, *, console: Console, mode: PresentationMode) -> None:
        self.mode = mode
        super().__init__(content, console=console, refresh_per_second=5, transient=True)

    def refresh(self) -> None:
        with synchronized_output(cast(TextIO, self.console.file), mode=self.mode):
            super().refresh()


class TerminalUI:
    """A scoped console; fallback decisions cannot leak into other CLI commands."""

    def __init__(self, console: Console | None = None, *, machine: bool = False) -> None:
        source = console or Console()
        self.machine = machine
        self._can_prompt = source.is_terminal and not bool(os.getenv("CI"))
        from verdict.design import presentation_mode as _pm

        class _ConsoleStream:
            """Shim so presentation_mode sees Console.is_terminal, not file.isatty()."""

            def __init__(self, c: Console) -> None:
                self._c = c

            def isatty(self) -> bool:
                return bool(self._c.is_terminal)

            @property
            def width(self) -> int:
                return self._c.width

        _mode = _pm(_ConsoleStream(source))
        self.mode = _mode
        self.plain = not _mode.color
        self.console = Console(
            file=source.file,
            # Preserve explicitly sized captures; real terminals measure each frame.
            width=source._width,
            height=source._height,
            force_terminal=not self.plain,
            color_system=None if self.plain else cast(ColorSystem, source.color_system),
            no_color=self.plain,
            theme=Theme(TOKENS),
            markup=True,
            highlight=False,
        )
        self.animate = not machine and _mode.animate
        self._stages: dict[str, str] = {}
        self._live: Live | None = None

    @property
    def interactive(self) -> bool:
        return not self.machine and self._can_prompt and sys.stdin.isatty()

    def header(self, title: str) -> None:
        if self.machine:
            return
        self.console.print()
        name = Text("VERDICT", style="PRIMARY")
        name.append(f"  /  {clean(title)}", style="SECONDARY")
        subtitle = Text(
            "autonomous control plane  ·  plan · select · recover · verify · prove", style="MUTED"
        )
        if self.plain or self.console.width < 40:
            self.console.print(name)
            self.console.print(subtitle)
        else:
            self.console.print(design_panel(Group(name, subtitle), mode=self.mode, tone="PRIMARY"))
        self.console.print()

    def status(self, label: str, state: str, detail: str = "") -> None:
        if self.machine:
            return
        state = clean(state).lower()
        tone = (
            "SUCCESS"
            if state in _GOOD
            else "ERROR"
            if state in _BAD
            else "MUTED"
            if state == "skipped"
            else "WARNING"
            if state in {"warning", "missing", "partial", "degraded"}
            else "INFO"
        )
        marker = "+" if state in _GOOD else "!" if state in _BAD else "-"
        if not self.plain:
            marker = (
                "✓"
                if state in _GOOD
                else "✗"
                if state in _BAD
                else "○"
                if state in {"missing", "skipped"}
                else "◆"
            )
        text = Text(f"  {marker} ", style=tone)
        text.append(clean(label))
        text.append(f"  {state.upper()}", style=tone)
        if self.plain:
            self.console.print(text)
            if detail:
                self.console.print(Text(f"    {clean(detail)}", style="MUTED"))
        else:
            status = Table.grid(padding=(0, 2), expand=True)
            status.add_column(ratio=1, style="TEXT", overflow="fold")
            status.add_column(justify="right", style=tone, no_wrap=True)
            status.add_row(Text(clean(label)), Text(f"{marker} {state.upper()}", style=tone))
            self.console.print(status)
            if detail:
                self.console.print(Text(clean(detail), style="SECONDARY"))

    def section(self, title: str) -> None:
        if not self.machine:
            self.console.print(Text(f"\n  {clean(title)}", style="PRIMARY"))

    def panel(self, title: str, content: str, *, tone: str = "BORDER") -> None:
        if self.machine:
            return
        if self.plain or self.console.width < 40:
            self.section(title)
            self.console.print(Text(clean(content)))
        else:
            self.console.print(
                design_panel(Text(clean(content)), title=clean(title), mode=self.mode, tone=tone)
            )

    def stop(self) -> None:
        if self._live is not None:
            live, self._live = self._live, None
            live.stop()

    def start(self, label: str) -> None:
        self.stop()
        if self.machine:
            return
        if self.animate:
            self._live = _SynchronizedLive(
                _Elapsed(label, self.mode), console=self.console, mode=self.mode
            )
            self._live.start(refresh=True)
        else:
            self.status(label, "running")

    @contextmanager
    def session(self) -> Iterator[None]:
        try:
            yield
        except (KeyboardInterrupt, EOFError):
            self.stop()
            self.panel(
                "Cancelled", "Setup interrupted. Re-run to inspect current state.", tone="WARNING"
            )
            raise
        except Exception as exc:
            self.stop()
            self.panel("Operation failed", str(exc), tone="ERROR")
            raise
        finally:
            self.stop()

    @contextmanager
    def task(self, label: str) -> Iterator[None]:
        with self.session():
            self.start(label)
            yield
            self.stop()

    def setup_journey(self) -> None:
        """Show only observed bootstrap stages; external checks remain not run."""
        if self.machine:
            return
        stages = (
            ("discover", "DISCOVER"),
            ("certify", "CERTIFY"),
            ("plan", "PLAN"),
            ("apply", "APPLY"),
            ("doctor", "DOCTOR"),
            ("proof", "FIRST RUN / PROOF"),
        )
        journey = Text()
        for index, (key, label) in enumerate(stages):
            state = self._stages.get(key, "not run")
            tone = (
                "SUCCESS"
                if state in _GOOD
                else "ERROR"
                if state in _BAD
                else "PRIMARY"
                if state == "running"
                else "MUTED"
            )
            if index:
                journey.append("  /  ", style=TOKENS["BORDER"] if not self.plain else "")
            journey.append(
                f"{label} [{state.upper()}]", style=TOKENS[tone] if not self.plain else ""
            )
        if self.plain:
            self.console.print(journey)
        else:
            self.console.print(
                design_panel(journey, title="SETUP / OBSERVED STAGES", mode=self.mode)
            )

    def event(self, stage: str, state: str, summary: str, data: Mapping[str, Any]) -> None:
        """Consume semantic notifications from the real bootstrap operation."""
        self._stages[stage] = state
        if state == "running":
            self.start(summary)
            return
        self.stop()
        if stage == "plan":
            self.plan(data)
        elif stage == "providers":
            self.capabilities(rows(data.get("providers")))
        elif stage == "recommendations":
            self.recommendations(rows(data.get("recommendations")))
        else:
            self.status(stage.title(), state, summary)
        if not self.plain and stage in {"discover", "certify", "plan", "apply"}:
            self.setup_journey()

    def capabilities(self, providers: Sequence[Mapping[str, Any]]) -> None:
        groups = sorted({str(p.get("provider_kind", "other")) for p in providers})
        for group in groups:
            self.section(group.title())
            for provider in providers:
                if provider.get("provider_kind", "other") != group:
                    continue
                lifecycle = str(provider.get("lifecycle", "unknown"))
                self.status(
                    str(provider.get("provider_id", "Provider")),
                    "missing" if lifecycle == "not_installed" else lifecycle,
                    str(provider.get("failure_reason") or ""),
                )

    def recommendations(self, recommendations: Sequence[Mapping[str, Any]]) -> None:
        self.section("Recommendations")
        for item in recommendations:
            candidates = item.get("candidate_providers") or []
            selected = item.get("selected_provider_id")
            detail = str(item.get("reason") or "")
            if selected:
                detail += f" · {selected}"
            elif candidates:
                detail += " · " + ", ".join(str(c) for c in candidates)
            self.status(
                str(item.get("capability_id", "Capability")),
                "covered" if item.get("status") == "covered" else "recommended",
                detail,
            )

    def plan(self, plan: Mapping[str, Any], *, details: bool = False) -> None:
        lines: list[str] = []
        for action in rows(plan.get("actions")):
            kind = str(action.get("kind", "configure"))
            label = (
                "Install"
                if kind == "install_provider"
                else "Preserve"
                if "preserve" in kind
                else "Configure"
            )
            lines.append(f"{label} · {action.get('description', action.get('target', ''))}")
            if action.get("reason"):
                lines.append(f"  {action['reason']}")
            if action.get("trust_warning"):
                lines.append(f"  Warning: {action['trust_warning']}")
            if details:
                for field in ("install_command", "security_impact", "postcondition", "undo"):
                    if action.get(field):
                        lines.append(f"  {field.replace('_', ' ').title()}: {action[field]}")
        if not lines:
            lines.append("No actions proposed.")
        if plan.get("plan_id"):
            lines.append(f"\nPlan: {plan['plan_id']}")
        self.panel("Plan", "\n".join(lines))

    def confirm_plan(self, plan: Any) -> bool:
        self.stop()
        if not self.interactive:
            return False
        payload = plan.to_dict()
        while True:
            choice = Prompt.ask(
                "Continue with this plan? (y / n / details)",
                choices=["y", "n", "details"],
                default="n",
                console=self.console,
            )
            if choice == "details":
                self.plan(payload, details=True)
            else:
                return choice == "y"

    def confirm(self, prompt: str, *, default: bool = False) -> bool:
        self.stop()
        return Confirm.ask(clean(prompt), default=default, console=self.console)

    def select(self, prompt: str, options: Sequence[str], *, default: str | None = None) -> str:
        self.section(prompt)
        for index, option in enumerate(options, 1):
            self.status(str(index), "option", option)
        choice = Prompt.ask(
            clean(prompt),
            choices=[str(i) for i in range(1, len(options) + 1)],
            default=default,
            console=self.console,
        )
        return options[int(choice or "1") - 1]

    def bootstrap(self, report: Mapping[str, Any], *, rendered_events: bool = False) -> None:
        self.stop()
        for stage in rows(report.get("stages")):
            self._stages[str(stage["stage"])] = str(stage["status"])
        if report.get("plan"):
            self._stages.setdefault("plan", "available")
        self.setup_journey()
        if not rendered_events:
            for stage in rows(report.get("stages")):
                self.status(
                    str(stage["stage"]).title(), str(stage["status"]), str(stage["summary"])
                )
            self.capabilities(rows(report.get("providers")))
            self.recommendations(rows(report.get("recommendations")))
            self.plan(report.get("plan", {}))
        actions = rows(report.get("apply", {}).get("actions"))
        if actions:
            self.section("Apply results")
        for action in actions:
            result = action.get("result", {})
            state = str(result.get("status", "unknown"))
            self.status(
                str(action.get("action_id", "Action")),
                "applied"
                if state in {"success", "ok", "installed", "configured", "applied"}
                else state,
                str(result.get("note") or result.get("error") or ""),
            )
        self.section("Certification")
        for result in rows(report.get("certification", {}).get("results")):
            self.status(
                str(result.get("provider_id", "Provider")),
                "certified" if result.get("certified") is True else "partial",
                str(result.get("reason") or ""),
            )
        failed = any(str(a.get("result", {}).get("status")) in _BAD for a in actions) or any(
            s.get("status") in _BAD | {"partial"} for s in rows(report.get("stages"))
        )
        if failed:
            self.panel(
                "Setup needs attention",
                "Some actions failed or were blocked. Review the results above; completed actions remain recorded.",
                tone="WARNING",
            )
        elif report.get("apply", {}).get("idempotent"):
            self.panel(
                "Existing installation", "Previously applied actions preserved. No new mutations."
            )
        elif report.get("mutation_free"):
            self.panel(
                "Review complete",
                "No changes made. APPLY requires consent or an explicit allowlist.",
            )
        else:
            self.panel(
                "Apply complete",
                "Review certification above. Run verdict doctor to check the full installation.",
            )

    def doctor_finding(self, label: str, state: str, detail: str = "") -> None:
        """Keep the observed problem and its next action adjacent, in every mode."""
        from verdict.doctor_presentation import PROBLEM_STATES, repair_command

        self.status(label, state, detail)
        if not self.machine and state.lower() in PROBLEM_STATES:
            command = repair_command(f"{label} {state} {detail}")
            self.console.print(Text(f"    Repair: {command}", style="ACCENT"))

    def doctor(self, report: Mapping[str, Any]) -> None:
        from verdict.design import PresentationMode
        from verdict.design import panel as design_panel
        from verdict.doctor_presentation import PROBLEM_STATES, repair_command

        mode = PresentationMode(True, True, False, self.console.width)
        cap_items: list[Text] = []
        for item in rows(report.get("capabilities")):
            detail = " · ".join(
                str(item[key])
                for key in ("selected_provider_id", "health", "authority")
                if item.get(key) is not None
            )
            cap_id = str(item.get("capability_id", "Capability"))
            status = str(item.get("status", "unknown"))
            line = Text(f"{cap_id}: {status.upper()}", style="TEXT")
            if detail:
                line.append(f"  {detail}", style="MUTED")
            cap_items.append(line)
            if not self.machine and status.lower() in PROBLEM_STATES:
                cmd = repair_command(f"{cap_id} {status} {detail}")
                cap_items.append(Text(f"    Repair: {cmd}", style="ACCENT"))
        if cap_items:
            content: Group | Text = Group(*cap_items)
        else:
            content = Text("No capabilities discovered.", style="MUTED")
        self.console.print(design_panel(content, title="CAPABILITIES", mode=mode))

    def doctor_summary(self, issues: Sequence[str], fixed: Sequence[str]) -> None:
        from verdict.design import PresentationMode
        from verdict.design import panel as design_panel
        from verdict.doctor_presentation import repair_command

        mode = PresentationMode(True, True, False, self.console.width)
        issue_items: list[Text] = []
        shown_count = 0
        for issue in issues:
            resolved = any(item.lower() in issue.lower() for item in fixed)
            label = "FIXED" if resolved else "ISSUE"
            state = "ok" if resolved else "failed"
            line = Text(f"{label}: ", style="ACCENT" if resolved else "ERROR")
            line.append(issue, style="TEXT")
            issue_items.append(line)
            shown_count += 1
            if not self.machine and not resolved:
                cmd = repair_command(f"{label} {state} {issue}")
                issue_items.append(Text(f"    Repair: {cmd}", style="ACCENT"))
        if issue_items:
            issue_content: Group | Text = Group(*issue_items)
            title = f"ISSUES ({shown_count})"
        else:
            issue_content = Text("System is healthy! All checks passed.", style="SUCCESS")
            title = "ISSUES (0)"
        self.console.print(design_panel(issue_content, title=title, mode=mode))
