"""Verdict's human presentation boundary. No setup or health decisions live here."""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from time import monotonic
from typing import Any

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

TOKENS = {
    "PRIMARY": "bold #54c7b0",
    "SECONDARY": "#8db8d8",
    "ACCENT": "#e5b567",
    "MUTED": "#929ca6",
    "SUCCESS": "#83c995",
    "WARNING": "#e5b567",
    "ERROR": "bold #ee8585",
    "INFO": "#8db8d8",
    "BORDER": "#647580",
}
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
    def __init__(self, label: str) -> None:
        self.label = clean(label)
        self.started = monotonic()
        self.spinner = Spinner("dots", style=TOKENS["PRIMARY"])

    def __rich__(self) -> Table:
        table = Table.grid(padding=(0, 1))
        table.add_column(width=2)
        table.add_column(overflow="fold")
        table.add_row(self.spinner, Text(f"{self.label} · {int(monotonic() - self.started)}s"))
        return table


class TerminalUI:
    """A scoped console; fallback decisions cannot leak into other CLI commands."""

    def __init__(self, console: Console | None = None, *, machine: bool = False) -> None:
        source = console or Console()
        self.machine = machine
        self._can_prompt = source.is_terminal and not bool(os.getenv("CI"))
        self.plain = (
            not source.is_terminal
            or "NO_COLOR" in os.environ
            or os.getenv("TERM") == "dumb"
            or bool(os.getenv("CI"))
            or os.getenv("VERDICT_PLAIN") == "1"
        )
        self.console = Console(
            file=source.file,
            width=source.width,
            force_terminal=not self.plain,
            color_system=None if self.plain else "auto",
            no_color=self.plain,
            theme=Theme(TOKENS),
            markup=True,
            highlight=False,
        )
        self.animate = not (machine or self.plain or os.getenv("VERDICT_NO_ANIMATION") == "1")
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
        subtitle = Text("autonomous control plane  ·  plan · select · recover · verify · prove", style="MUTED")
        if self.plain or self.console.width < 40:
            self.console.print(name)
            self.console.print(subtitle)
        else:
            self.console.print(
                Panel(
                    Group(name, subtitle),
                    box=box.ROUNDED,
                    border_style="BORDER",
                    padding=(1, 2),
                    width=min(88, self.console.width),
                )
            )
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
        self.console.print(text)
        if detail:
            self.console.print(Text(f"    {clean(detail)}", style="MUTED"))

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
                Panel(
                    Text(clean(content)),
                    title=Text(clean(title), style=tone),
                    border_style=tone,
                    box=box.ROUNDED,
                    padding=(1, 2),
                    width=min(100, self.console.width),
                )
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
            self._live = Live(
                _Elapsed(label), console=self.console, refresh_per_second=8, transient=True
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

    def event(self, stage: str, state: str, summary: str, data: Mapping[str, Any]) -> None:
        """Consume semantic notifications from the real bootstrap operation."""
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

    def doctor(self, report: Mapping[str, Any]) -> None:
        self.section("Capabilities")
        for item in rows(report.get("capabilities")):
            detail = " · ".join(
                str(item[key])
                for key in ("selected_provider_id", "health", "authority")
                if item.get(key) is not None
            )
            self.status(
                str(item.get("capability_id", "Capability")),
                str(item.get("status", "unknown")),
                detail,
            )

    def doctor_summary(self, issues: Sequence[str], fixed: Sequence[str]) -> None:
        self.panel(
            "Doctor Report",
            f"Doctor Report: {len(issues)} issues identified. {len(fixed)} resolved.",
        )
        for issue in issues:
            resolved = any(item.lower() in issue.lower() for item in fixed)
            self.status("FIXED" if resolved else "ISSUE", "ok" if resolved else "failed", issue)
        if not issues:
            self.status(
                "System is healthy! All checks passed.",
                "healthy",
                "Configuration checks passed; capability coverage is reported separately above.",
            )
