"""Guided home setup: choices feed the existing bootstrap consent/apply gate."""

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from rich.text import Text

from verdict.capability_bootstrap import apply_bootstrap_actions, run_bootstrap
from verdict.home_action_render import render_setup_plan
from verdict.orchestration.run import resolve_api_key
from verdict.shared_memory import discover_shared_memory_setup
from verdict.terminal_ui import TerminalUI, clean


def _ask(tui: TerminalUI, reader: Callable[[], str | None] | None, prompt: str) -> str:
    tui.console.print(Text(prompt), end=" ")
    if reader is None and not tui.interactive:
        tui.console.print(Text("(default; no input)"))
        return ""
    try:
        return ((reader() if reader else input()) or "").strip()
    except EOFError:
        return ""


def _choice(
    tui: TerminalUI,
    reader: Callable[[], str | None] | None,
    title: str,
    current: str,
    recommended: str,
    options: list[str],
) -> str | None:
    tui.section(title)
    tui.console.print(Text(f"Current: {clean(current)}"))
    tui.console.print(Text(f"Recommended: {clean(recommended)}"))
    tui.console.print(Text("Choices: " + ", ".join(options)))
    choice = _ask(tui, reader, f"Choice [{recommended}]:") or recommended
    if choice not in options:
        tui.status(title, "skipped", "Unknown choice; unchanged.")
        return None
    if _ask(tui, reader, "Confirm this step? [y/N]").lower() not in {"y", "yes"}:
        tui.status(title, "skipped", "No changes confirmed.")
        return None
    return choice


def run_setup_wizard(
    tui: TerminalUI,
    *,
    gateway: str,
    plan: dict[str, Any],
    state_dir: Path,
    reader: Callable[[], str | None] | None = None,
) -> dict[str, Any]:
    """Never mutate on defaults/EOF. Each confirmed choice remains narrowly scoped."""
    render_setup_plan(tui, plan)
    # Discovery is local-only. Gateway checks are opt-in; no model probes or installs.
    report = run_bootstrap(
        mode="recommended", non_interactive=True, probe_gateway=lambda _id: {}, state_dir=state_dir
    )
    selected: list[str] = []
    notes: list[str] = []
    values: dict[str, Any] = {}
    tui.section("1 / Gateway URL and key")
    tui.console.print(Text(f"Current: {clean(gateway)}"))
    tui.console.print(Text(f"Recommended: {clean(gateway)} (keep existing URL)"))
    tui.console.print(
        Text(
            "Key: "
            + (
                "configured (hidden)"
                if resolve_api_key() or resolve_api_key("OMNIROUTE_API_KEY")
                else "not configured; use verdict credentials set OMNIROUTE_API_KEY"
            )
        )
    )
    url = _ask(tui, reader, f"Gateway URL [{gateway}]:") or gateway
    if _ask(tui, reader, "Confirm gateway/key reachability check? [y/N]").lower() in {"y", "yes"}:
        from verdict.home import probe_gateway

        reachable, _count, auth = probe_gateway(url.rstrip("/").removesuffix("/v1"))
        tui.status(
            "Gateway check",
            "ok" if reachable and not auth else "failed",
            auth or "reachable" if reachable else "unreachable",
        )
        if reachable and not auth:
            values["gateway_url"] = url
        notes.append("Gateway checked; confirmed healthy URL can be saved at final confirmation.")
    else:
        notes.append("Gateway unchanged; no network check.")
    groups = [
        ("2 / Harness", "harness"),
        ("3 / Memory provider", "intelligence"),
        ("4 / Documentation sources", "docs"),
    ]
    memory = discover_shared_memory_setup()
    for title, kind in groups:
        providers = [p for p in report.providers if p.provider_kind.value == kind]
        if kind == "intelligence":
            providers = [p for p in providers if "memory.search" in p.capabilities]
        options = [p.provider_id for p in providers]
        current = (
            ", ".join(
                p.provider_id + " (" + p.lifecycle.value + ")"
                for p in providers
                if p.lifecycle.value != "not_installed"
            )
            or "not configured"
        )
        if kind == "intelligence":
            current += f"; shared memory: {memory['state']}"
        recommended = next(
            (p.provider_id for p in providers if p.lifecycle.value != "not_installed"), "keep"
        )
        choice = _choice(tui, reader, title, current, recommended, ["keep", *options])
        if choice and choice != "keep":
            selected.append(choice)
            notes.append(title + ": " + choice + " confirmed; apply result below.")
            values.setdefault("setup_preferences", {})[kind] = choice
        else:
            notes.append(title + ": unchanged.")
    _dependencies(tui, reader, report, notes, selected)
    _warm_cache(tui, reader, gateway, notes)
    actions = tuple(a for a in report.plan.actions if a.provider_id in selected)
    if actions:
        tui.section("Apply confirmed provider choices (with bootstrap backups)")
        for action in actions:
            tui.console.print(Text(action.description))
        _consent, stage, payload = apply_bootstrap_actions(
            replace(report.plan, actions=actions),
            state_dir=state_dir,
            consent=False,
            allowlist=selected,
            non_interactive=True,
        )
        tui.status("Bootstrap apply", stage.status, stage.summary)
        raw_actions = payload.get("actions", [])
        for item in raw_actions if isinstance(raw_actions, list) else []:
            result = item.get("result", {})
            notes.append(
                str(item["action_id"])
                + ": "
                + str(result.get("status"))
                + " — "
                + str(result.get("note", ""))
            )
    elif selected:
        notes.append("Selected providers already present; no installation needed.")
    tui.section("Setup Summary")
    for note in notes:
        tui.console.print(Text(clean(note)))
    if not selected:
        tui.console.print(Text("No changes to provider configuration."))
    tui.console.print(
        Text(
            "Next step: run verdict doctor. Unsupported installs stay blocked; never reported as success."
        )
    )
    tui.console.print(
        Text(
            "Harness/memory/docs preferences record review choices only; existing integrations remain unchanged."
        )
    )
    if _ask(tui, reader, "Confirm save configuration and finish setup review? [y/N]").lower() in {
        "y",
        "yes",
    }:
        from verdict.setup_config import save_setup_config

        path = save_setup_config(values)
        state_dir.parent.mkdir(parents=True, exist_ok=True)
        marker = state_dir.parent / "setup-complete"
        marker.write_text("reviewed; dependency skips remain in doctor findings\n")
        marker.chmod(0o600)
        tui.status("Configuration", "saved", str(path))
    else:
        tui.console.print(Text("No changes saved; setup remains incomplete."))
    return {"status": "reviewed", "selected": selected, "summary": notes}


def _dependencies(
    tui: TerminalUI,
    reader: Callable[[], str | None] | None,
    report: Any,
    notes: list[str],
    selected: list[str],
) -> None:
    import shutil

    tui.section("Dependency review — optional installs are never silent")
    for provider in report.providers:
        if provider.provider_id not in {
            "gateway.omniroute",
            "harness.prime",
            "adapter.codebase_memory",
            "adapter.serena_lsp",
            "adapter.context7",
        }:
            continue
        tui.status(
            provider.provider_id,
            provider.lifecycle.value,
            "Enables: " + ", ".join(sorted(provider.capabilities)),
        )
        tui.console.print(
            Text("Version: " + (provider.version or "not observed; outdated status unknown"))
        )
        tui.console.print(Text("Install: " + provider.install_command))
        answer = _ask(tui, reader, "Confirm dependency recommendation? [y/N]")
        if answer.lower() in {"y", "yes"} and provider.provider_id == "gateway.omniroute":
            selected.append(provider.provider_id)
            notes.append("OmniRoute install confirmed; canonical apply verifies binary presence.")
        else:
            notes.append(
                provider.provider_id
                + (
                    ": manual — use upstream command above."
                    if answer.lower() in {"y", "yes"}
                    else ": skipped; unchanged."
                )
            )
    for name, binary, capability, command in (
        (
            "context-mode",
            "context-mode",
            "context compression",
            "See https://context-mode.com installation docs (command not verified)",
        ),
        (
            "open-code-review",
            "ocr",
            "independent code review",
            "See open-code-review upstream installation docs (command not verified)",
        ),
        (
            "ai-memory (BOD-336)",
            "ai-memory",
            "shared agent memory",
            "Not available yet; BOD-336 integration pending",
        ),
    ):
        tui.status(name, "found" if shutil.which(binary) else "missing", "Enables: " + capability)
        tui.console.print(Text("Version/outdated: not observed. Install: " + command))
        _ask(tui, reader, "Confirm manual recommendation? [y/N]")
        notes.append(name + ": manual; no install executed.")


def _warm_cache(
    tui: TerminalUI, reader: Callable[[], str | None] | None, gateway: str, notes: list[str]
) -> None:
    del gateway
    choice = _choice(tui, reader, "5 / Warm-cache daemon", "not checked", "off", ["off", "on"])
    tui.console.print(
        Text("Enables: background health cache. Service installer: not available yet.")
    )
    tui.console.print(Text("Manual command: verdict prove-at-rest daemon --allow-live-probe"))
    tui.console.print(
        Text("User-service templates: deploy/systemd/verdict-health-cache.{service,timer}")
    )
    tui.console.print(Text("Live probes may spend credits. No daemon is started by this wizard."))
    notes.append(
        "Warm-cache: not available yet; use the manual command/templates above. No service installed."
        if choice == "on"
        else "Warm-cache: unchanged; no enable/disable operation requested."
    )
