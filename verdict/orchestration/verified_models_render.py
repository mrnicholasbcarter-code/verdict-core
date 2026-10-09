"""Pure presentation of sanitized verified-model pages and refresh plans.

The input boundary is ``VerifiedModelsView.to_dict()`` plus sanitized refresh
metadata. This module does not load evidence, infer health, select routes, or
run probes. Page rows are already filtered and sorted by the projection.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

_MAX_PAGE_ROWS = 200
_STATUSES = (
    "VERIFIED",
    "STALE",
    "FAILED",
    "UNAVAILABLE",
    "UNVERIFIED",
    "INVENTORY_ONLY",
    "EXCLUDED",
)
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_SPEND_NOTE = (
    "SUBSCRIPTION and METERED probes may consume quota; FREE can also have provider "
    "limits; UNKNOWN may incur charges; currency estimate unavailable"
)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _display(value: object) -> str:
    """Format a sanitized scalar without interpreting markup or terminal codes."""
    if value is None or value == "":
        return "unknown"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, str):
        return _CONTROL_CHARACTERS.sub(lambda match: f"\\x{ord(match[0]):02x}", value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return str(value)
    # Never dump mappings, objects, or their repr: those are not display fields.
    return "unknown"


def _count(value: object) -> str:
    """Progress and counts accept only recorded non-negative integer values."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return str(value)
    return "unknown"


def _elapsed(value: object) -> str:
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    ):
        return f"{value:.1f}s"
    return "unknown"


def _page_rows(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Use only supplied page rows, with a defensive hard limit (not first 50)."""
    rows = _sequence(payload.get("rows"))
    return [row for row in rows[:_MAX_PAGE_ROWS] if isinstance(row, Mapping)]


def _refresh_metadata(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    refresh = payload.get("refresh")
    if isinstance(refresh, Mapping):
        return refresh
    # Also accept the metadata inline for small presentation-only callers.
    if any(key in payload for key in ("outcome", "last_known", "cap_reason")):
        return payload
    return {}


def _counts_line(label: str, counts: object) -> str:
    values = _mapping(counts)
    return (
        label + ": " + "  ".join(f"{status}={_count(values.get(status))}" for status in _STATUSES)
    )


def _filters_line(filters: object) -> str:
    values = _mapping(filters)
    parts = [
        f"{key}={_display(values[key])}"
        for key in ("status", "provider", "search")
        if values.get(key) is not None and values.get(key) != ""
    ]
    return "filters: " + ("  ".join(parts) if parts else "none")


def _refresh_lines(refresh: Mapping[str, Any]) -> list[str]:
    if not refresh:
        return []
    outcome = _display(refresh.get("outcome"))
    lines: list[str] = []
    if refresh.get("outcome") == "cancelled":
        lines.append(
            "CANCELLED / LAST-KNOWN: showing last-known evidence and any completed "
            "results; unfinished routes were not verified."
        )
    elif refresh.get("last_known") is True:
        lines.append(
            f"LAST-KNOWN: refresh outcome={outcome}; showing recorded evidence "
            "and any completed results; untested routes have no new proof."
        )
    if refresh.get("outcome") == "auto_refresh_disabled":
        lines.append("Automatic refresh disabled; showing the recorded snapshot only.")
    elif refresh.get("outcome") == "reused_fresh":
        lines.append("Fresh evidence reused; no refresh probes needed.")
    fields = [f"outcome={outcome}"]
    for key, label in (
        ("probed", "probed"),
        ("healthy", "healthy"),
        ("verified", "verified"),
        ("alive", "alive"),
        ("failed", "failed"),
        ("unavailable", "unavailable"),
        ("requests_made", "requests"),
    ):
        if key in refresh:
            fields.append(f"{label}={_count(refresh[key])}")
    if "elapsed_seconds" in refresh:
        fields.append(f"elapsed={_elapsed(refresh['elapsed_seconds'])}")
    if "complete" in refresh:
        fields.append(f"complete={_display(refresh['complete'])}")
    lines.append("refresh: " + "  ".join(fields))
    if refresh.get("cap_reason"):
        lines.append(
            f"Refresh cap/skip reason: {_display(refresh['cap_reason'])}; "
            "unrefreshed routes keep their recorded status."
        )
    return lines


def _summary_lines(payload: Mapping[str, Any], shown: int) -> list[str]:
    lines = ["verified models", f"generated_at={_display(payload.get('generated_at'))}"]
    lines.extend(_refresh_lines(_refresh_metadata(payload)))
    lines.append(_counts_line("counts (full inventory)", payload.get("counts_by_status")))
    lines.append(_counts_line("counts (filtered)", payload.get("filtered_counts_by_status")))
    lines.append(
        f"showing {shown}/{_count(payload.get('filtered_count'))} filtered rows "
        f"(total {_count(payload.get('total_count'))}; "
        f"page {_count(payload.get('page'))}/{_count(payload.get('page_count'))}, "
        f"size {_count(payload.get('page_size'))})"
    )
    lines.append(_filters_line(payload.get("filters")))
    return lines


def _proof_details(row: Mapping[str, Any]) -> str:
    # These are recorded facts. In particular, catalog tool support and agentic
    # hints are not substituted for the explicit coding_ok proof flag.
    parts = [f"coding_ok={_display(row.get('coding_ok'))}"]
    if row.get("status") == "VERIFIED" and row.get("coding_ok") is False:
        parts.append("chat verified; tools unverified")
    for key in ("probe_class", "identity", "freshness", "checked_at", "last_success_at"):
        if row.get(key) is not None:
            parts.append(f"{key}={_display(row[key])}")
    return "  ".join(parts)


def _row_notes(row: Mapping[str, Any], refresh: Mapping[str, Any]) -> list[str]:
    parts: list[str] = []
    for key in ("reason", "restriction", "failure_category", "http_status", "cooldown_until"):
        if row.get(key) is not None and row.get(key) != "":
            parts.append(f"{key}={_display(row[key])}")
    restrictions = _sequence(row.get("restrictions"))
    secondary = [
        _display(value)
        for value in restrictions
        if isinstance(value, str) and value and value != row.get("restriction")
    ]
    if secondary:
        parts.append("restrictions=" + ", ".join(secondary))
    if "refreshable" in row:
        parts.append(f"refreshable={_display(row['refreshable'])}")
    route_id = row.get("route_id")
    outcome = (
        _mapping(_mapping(refresh.get("route_outcomes")).get(route_id))
        if isinstance(route_id, str)
        else {}
    )
    job_reason = outcome.get("refresh_reason")
    row_reason = row.get("refresh_reason")
    if job_reason is not None and job_reason != "":
        parts.append(f"refresh_reason={_display(job_reason)}")
    if row_reason is not None and row_reason != "" and row_reason != job_reason:
        label = "eligibility_refresh_reason" if job_reason else "refresh_reason"
        parts.append(f"{label}={_display(row_reason)}")
    return parts


_SOURCE_ERROR_KEY_PREFIX = re.compile(r"^[A-Za-z0-9_ ]+\[[^\]]*\]:\s*")
_MAX_SOURCE_ERROR_GROUPS = 5


def _grouped_source_error_lines(payload: Mapping[str, Any]) -> list[str]:
    """Bound potentially thousands of source_errors to a handful of display
    lines. Identical messages (per-key prefix such as ``health_cache[id]: ``
    stripped) are grouped and counted; the full scrubbed list is unchanged in
    the JSON contract (``payload["source_errors"]``), only this rendering is
    bounded."""
    errors = [
        _display(value)
        for value in _sequence(payload.get("source_errors"))
        if isinstance(value, str)
    ]
    if not errors:
        return []
    counts: dict[str, int] = {}
    order: list[str] = []
    for text in errors:
        key = _SOURCE_ERROR_KEY_PREFIX.sub("", text)
        if key not in counts:
            order.append(key)
        counts[key] = counts.get(key, 0) + 1
    lines = [f"source_errors ({len(errors)} total, {len(order)} distinct):"]
    shown = order[:_MAX_SOURCE_ERROR_GROUPS]
    lines.extend(f"  {key} x{counts[key]}" for key in shown)
    remaining = len(order) - len(shown)
    if remaining > 0:
        lines.append(f"  {remaining} more; use --json for all")
    return lines


def _footer_lines(payload: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    supplied = len(_sequence(payload.get("rows")))
    if supplied > _MAX_PAGE_ROWS:
        lines.append(
            f"Display safety cap: {_MAX_PAGE_ROWS} supplied page rows shown; "
            f"{supplied - _MAX_PAGE_ROWS} extra rows not rendered."
        )
    lines.extend(_grouped_source_error_lines(payload))
    return lines


def render_verified_plain(payload: Mapping[str, Any]) -> str:
    """Render a sanitized, pre-paged view as plain text (no ANSI or markup)."""
    rows = _page_rows(payload)
    refresh = _refresh_metadata(payload)
    lines = _summary_lines(payload, len(rows))
    lines.append("")
    if not rows:
        lines.append("No rows on this page.")
    for row in rows:
        lines.append(
            f"{_display(row.get('status'))}  {_display(row.get('route_id'))}  "
            f"provider={_display(row.get('provider'))}  "
            f"capacity_class={_display(row.get('capacity_class'))}"
        )
        lines.append("  proof: " + _proof_details(row))
        notes = _row_notes(row, refresh)
        if notes:
            lines.append("  " + "  ".join(notes))
    lines.extend(_footer_lines(payload))
    return "\n".join(lines) + "\n"


def render_verified_table(payload: Mapping[str, Any]) -> RenderableType:
    """Render only the supplied page, using literal Text for all dynamic data."""
    rows = _page_rows(payload)
    refresh = _refresh_metadata(payload)
    blocks: list[RenderableType] = [Text(line) for line in _summary_lines(payload, len(rows))]
    if rows:
        table = Table(header_style="bold", expand=False, show_lines=False)
        for title in (
            "status",
            "route id",
            "provider",
            "coding proof",
            "capacity",
            "reason / refresh",
        ):
            table.add_column(Text(title), overflow="fold")
        for row in rows:
            table.add_row(
                Text(_display(row.get("status"))),
                Text(_display(row.get("route_id"))),
                Text(_display(row.get("provider"))),
                Text(_proof_details(row)),
                Text(_display(row.get("capacity_class"))),
                Text("\n".join(_row_notes(row, refresh)) or "none recorded"),
            )
        blocks.append(table)
    else:
        blocks.append(Text("No rows on this page."))
    blocks.extend(Text(line) for line in _footer_lines(payload))
    return Group(*blocks)


def _progress_field(event: Any, key: str) -> object:
    if isinstance(event, Mapping):
        return event.get(key)
    return getattr(event, key, None)


def format_verified_progress(event: Any) -> str:
    """Return one live line from numeric progress fields, never raw reasons.

    Accepts the coordinator's ProgressEvent or its mapping representation.
    There is no newline so a consumer can replace the current live line.
    """
    probed = _count(_progress_field(event, "probed"))
    total = _count(_progress_field(event, "total"))
    verified = _progress_field(event, "verified")
    alive = _progress_field(event, "alive")
    if alive is None:
        alive = 0
    healthy = (
        str(verified + alive)
        if type(verified) is int and verified >= 0 and type(alive) is int and alive >= 0
        else "unknown"
    )
    failed = _count(_progress_field(event, "failed"))
    unavailable = _count(_progress_field(event, "unavailable"))
    requests = _count(_progress_field(event, "requests_made"))
    reserved = _count(_progress_field(event, "requests_reserved"))
    elapsed = _elapsed(_progress_field(event, "elapsed_seconds"))
    return (
        f"{probed}/{total} probed  healthy={healthy} failed={failed} "
        f"unavailable={unavailable}  requests={requests} reserved={reserved}  elapsed={elapsed}"
    )


def render_refresh_plan(plan: Mapping[str, Any]) -> str:
    """Disclose the exact manual plan without executing it or granting consent."""
    routes = [route for route in _sequence(plan.get("routes")) if isinstance(route, Mapping)]
    needed_ids = _sequence(plan.get("needed_ids"))
    caps = _mapping(plan.get("caps"))
    lines = [
        "Manual model refresh plan (not executed)",
        f"consumer={_display(plan.get('consumer'))}  "
        f"gateway_origin={_display(plan.get('gateway_origin'))}",
        f"candidate_count={_count(plan.get('candidate_count'))}  "
        f"selected_count={_count(plan.get('selected_count'))}  "
        f"omitted_count={_count(plan.get('omitted_count'))}",
        "caps: "
        + "  ".join(
            f"{key}={_display(caps.get(key))}"
            for key in ("max_routes", "max_requests", "wall_seconds", "concurrency")
        ),
        f"estimated_requests={_count(plan.get('estimated_requests'))}",
        f"requires_confirmation={_display(plan.get('requires_confirmation'))}  "
        f"force={_display(plan.get('force'))}  "
        f"include_metered={_display(plan.get('include_metered'))}",
        _filters_line(plan.get("filters")) + f"  page={_display(plan.get('page'))}",
        f"needed_ids ({len(needed_ids)}):",
    ]
    lines.extend(f"  {_display(route_id)}" for route_id in needed_ids)
    if not needed_ids:
        lines.append("  none")
    lines.append(f"Selected routes ({len(routes)}):")
    for index, route in enumerate(routes, start=1):
        lines.append(
            f"  {index}. {_display(route.get('route_id'))}  "
            f"provider={_display(route.get('provider'))}  "
            f"capacity_class={_display(route.get('capacity_class'))}  "
            f"probe_kind={_display(route.get('probe_kind'))}"
        )
    if not routes:
        lines.append("  none")
    lines.extend(
        [
            "Spend note: " + _display(plan.get("spend_note") or _SPEND_NOTE),
            f"created_at={_display(plan.get('created_at'))}  "
            f"valid_until={_display(plan.get('valid_until'))}",
            f"evidence_generation={_display(plan.get('evidence_generation'))}",
            f"plan_digest={_display(plan.get('plan_digest'))}",
            "This is a plan, not a probe result. Explicit confirmation is required before execution.",
        ]
    )
    return "\n".join(lines) + "\n"


__all__ = [
    "format_verified_progress",
    "render_refresh_plan",
    "render_verified_plain",
    "render_verified_table",
]
