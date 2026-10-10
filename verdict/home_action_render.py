"""Human action reports for the home prompt; payloads stay at the JSON boundary."""
from collections.abc import Mapping
from typing import Any

from rich.text import Text

from verdict.contracts import redact_contract_secrets
from verdict.terminal_ui import TerminalUI, clean

_INTERNAL = {"schema", "schema_version", "plan_digest", "plan_id", "decision_digest"}


def render_doctor(tui: TerminalUI, data: Mapping[str, Any]) -> None:
    """Use the same capability, finding and summary renderers as CLI doctor."""
    tui.header("Doctor")
    tui.doctor(data.get("capability_bootstrap", {}))
    for row in data.get("sections", []):
        label, state, detail = row["label"], row["state"], row.get("detail", "")
        if state == "header":
            tui.header(label)
        elif state == "section":
            tui.section(label)
            if detail:
                tui.console.print(Text(f"  {clean(detail)}"))
        else:
            tui.doctor_finding(label, state, detail)
    for warning in data.get("warnings", []):
        tui.doctor_finding("WARNING", "warning", warning)
    tui.doctor_summary(data.get("issues", []), data.get("repaired", []))
    if data.get("issues"):
        tui.panel("Next step", "Follow the repair hints above, then run verdict doctor.", tone="WARNING")


def _facts(value: Any, *, depth: int = 0) -> list[str]:
    """Bounded tree of human labels; never stringify a container."""
    if depth > 5:
        return ["More detail available with --json."]
    if isinstance(value, Mapping):
        lines = []
        for key, item in list(value.items())[:30]:
            if str(key) in _INTERNAL or str(key).endswith("_digest"):
                continue
            label = clean(str(key).replace("_", " "))
            children = _facts(item, depth=depth + 1)
            if isinstance(item, (Mapping, list, tuple)):
                lines.append(label + ":")
                lines.extend("  " + line for line in children)
            else:
                lines.append(label + ": " + children[0])
        return lines or ["No details."]
    if isinstance(value, (list, tuple)):
        lines = []
        for item in value[:30]:
            lines.extend("- " + line for line in _facts(item, depth=depth + 1))
        if len(value) > 30:
            lines.append(f"{len(value) - 30} more items; use --json for all details.")
        return lines or ["None."]
    return ["not set" if value is None else clean(str(value))[:300]]


def render_generic(tui: TerminalUI, ok: bool, data: Any, *, action: str = "Action") -> None:
    """Readable fallback with a title, grouped facts and an explicit next step."""
    title = action.replace(".", " / ").replace("_", " ").capitalize()
    tui.header(title if ok else title + " — needs attention")
    safe = redact_contract_secrets(data)
    if isinstance(safe, Mapping) and isinstance(safe.get("text"), str):
        tui.console.print(Text(clean(safe["text"])))
    else:
        tui.section("Key facts")
        tui.console.print(Text("\n".join(_facts(safe))))
    next_step = safe.get("next") if isinstance(safe, Mapping) else None
    if not isinstance(next_step, str):
        next_step = "Use /help for another action or --json for full details." if ok else "Review the finding above. Run /doctor for repair hints."
    tui.section("Next step")
    tui.console.print(Text(clean(next_step)))


def render_setup_plan(tui: TerminalUI, data: Mapping[str, Any]) -> None:
    tui.header("Setup plan")
    config = data.get("config", {})
    tui.status("Configuration", "found" if config.get("exists") else "missing", str(config.get("path", "")))
    for action in data.get("actions", []):
        tui.status(str(action.get("target", "Setup")), "planned", str(action.get("description", "")))
    tui.section("Next step")
    tui.console.print(Text("Run /setup for guided choices. Changes require confirmation and backups."))
