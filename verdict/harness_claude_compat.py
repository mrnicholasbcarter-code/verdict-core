"""Pure, read-only Claude Code model compatibility reports (BOD-294 U5).

Claude Code's native Anthropic Messages transport is not implemented by Verdict.
This module reports that limitation for each exact selected id. It may also report
that the existing OpenAI-compatible side path is configured, but never claims that
Claude selected or tested any exact model. All discovery, status, projection,
digests, and time are supplied by the caller; this module performs no I/O.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from verdict.harness_claude import DiscoverReport, StatusReport

SCHEMA = "verdict.claude-model-compat/v1"
_NEEDS_OWNER = "BOD-102"
_SUPPORTED_HEALTH_STATUSES = frozenset(
    {"VERIFIED", "STALE", "FAILED", "UNAVAILABLE", "UNVERIFIED", "INVENTORY_ONLY", "EXCLUDED"}
)


def build_claude_compat_report(
    discover: DiscoverReport,
    status: StatusReport,
    start_digest: str | None,
    end_digest: str | None,
    requested_mode: str,
    exact_selected_ids: Sequence[str],
    projection_rows: Sequence[Mapping[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    """Build a stable, side-effect-free report from supplied local snapshots.

    ``start_digest`` and ``end_digest`` are SHA-256 digests of Claude settings
    bytes captured at report start and before presentation. A mismatch blocks
    side-path readiness. Projection rows must have the ``to_dict()`` shape of
    ``verdict.verified-models/v1``. Exact route-id equality is the only mapping:
    this function does not infer aliases, classify inventory as proof, or probe
    any selected id.

    The discovery/status dataclasses expose only safe readiness facts. Secret
    values, paths, raw settings and token environment names are never serialized.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    generated_at = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    config_changed = start_digest != end_digest
    config_digest_known = bool(start_digest) and bool(end_digest)

    installed = discover.installed is True
    settings_present = discover.config_exists is True and status.config_exists is True
    raw_credentials = getattr(status, "token_env_set", None)
    credentials_present = raw_credentials if isinstance(raw_credentials, bool) else None
    side_path_env_configured = discover.pointing_at_verdict is True
    gate_hook_present = discover.gate_hook_present is True and status.gate_hook_present is True

    prerequisite_reasons: list[str] = []
    if not installed:
        prerequisite_reasons.append("binary_missing")
    if not settings_present:
        prerequisite_reasons.append("settings_missing")
    if credentials_present is False:
        prerequisite_reasons.append("credentials_missing")
    elif credentials_present is None:
        prerequisite_reasons.append("credentials_unknown")
    if not side_path_env_configured:
        prerequisite_reasons.append("side_path_env_missing")
    if not gate_hook_present:
        prerequisite_reasons.append("gate_hook_missing")
    if not config_digest_known:
        prerequisite_reasons.append("config_digest_missing")

    configured = not prerequisite_reasons
    if config_changed:
        side_path_compatibility = "blocked"
        top_reasons = [*prerequisite_reasons, "config_changed"]
    elif configured:
        side_path_compatibility = "configured_unproven"
        top_reasons = []
    else:
        side_path_compatibility = "not_configured"
        top_reasons = [*prerequisite_reasons, "side_path_not_configured"]

    by_id: dict[str, Mapping[str, Any]] = {}
    ambiguous_ids: set[str] = set()
    for candidate_row in projection_rows:
        route_id = candidate_row.get("route_id")
        if not isinstance(route_id, str):
            continue
        if route_id in by_id:
            ambiguous_ids.add(route_id)
        else:
            by_id[route_id] = candidate_row

    rows: list[dict[str, Any]] = []
    for exact_id in exact_selected_ids:
        matched_row = by_id.get(exact_id)
        selected_projection_row: Mapping[str, Any] | None = (
            None if exact_id in ambiguous_ids else matched_row
        )
        raw_health_status = (
            selected_projection_row.get("status") if selected_projection_row is not None else None
        )
        health_status = (
            raw_health_status
            if isinstance(raw_health_status, str)
            and raw_health_status in _SUPPORTED_HEALTH_STATUSES
            else "UNVERIFIED"
        )
        provider = (
            selected_projection_row.get("provider") if selected_projection_row is not None else None
        )
        if not isinstance(provider, str):
            provider = None

        row_reasons = ["native_messages_proxy_missing", "selected_id_unproven"]
        if selected_projection_row is None:
            row_reasons.append("unsupported_identity")
        if health_status != "VERIFIED":
            row_reasons.append("stale_or_unverified")
        row_reasons.extend(prerequisite_reasons)
        if side_path_compatibility == "not_configured":
            row_reasons.append("side_path_not_configured")
        if config_changed:
            row_reasons.append("config_changed")

        side_path_reasons = ["selected_id_unproven", *prerequisite_reasons]
        if side_path_compatibility == "not_configured":
            side_path_reasons.append("side_path_not_configured")
        if config_changed:
            side_path_reasons.append("config_changed")

        rows.append(
            {
                "route_id": exact_id,
                "provider": provider,
                "health_status": health_status,
                "checked_at": (
                    selected_projection_row.get("checked_at") if selected_projection_row else None
                ),
                "fresh_until": (
                    selected_projection_row.get("fresh_until") if selected_projection_row else None
                ),
                "expires_at": (
                    selected_projection_row.get("expires_at") if selected_projection_row else None
                ),
                "native": {
                    "transport": "anthropic-messages",
                    "endpoint": "/v1/messages",
                    "compatibility": "unsupported",
                    "disposition": "NEEDS_OWNER",
                    "owner": _NEEDS_OWNER,
                    "exact_selection_tested": False,
                    "selectable": False,
                    "reasons": ["native_messages_proxy_missing"],
                },
                "side_path": {
                    "transport": "openai-compatible",
                    "endpoint": "/v1/chat/completions",
                    "configured": side_path_compatibility == "configured_unproven",
                    "compatibility": side_path_compatibility,
                    "exact_selection_tested": False,
                    "selectable": False,
                    "reasons": side_path_reasons,
                },
                "reasons": row_reasons,
            }
        )

    return {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "harness": "claude",
        "requested_mode": requested_mode,
        "exact_selected_ids": list(exact_selected_ids),
        "selected_ids": list(exact_selected_ids),
        "installed": installed,
        "settings_present": settings_present,
        "side_path_env_configured": side_path_env_configured,
        "gate_hook_present": gate_hook_present,
        "credentials_present": credentials_present,
        "config_digest": start_digest,
        "config_changed": config_changed,
        "rows": rows,
        "reasons": top_reasons,
        "apply_available": False,
        "needs_owner": [_NEEDS_OWNER],
    }


__all__ = ["SCHEMA", "build_claude_compat_report"]
