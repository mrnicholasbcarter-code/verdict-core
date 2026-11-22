"""``verdict discovery connections`` — read-only listing of connection billing facts.

Shows each active OmniRoute connection alongside its billing classification from
the snapshot + operator overlay, the deciding fact, and drift status.

No model calls and no writes.  Useful for auditing what the classifier sees.
"""

from __future__ import annotations

from typing import Any

from verdict.connection_billing import (
    BILLING_KEYLESS,
    BILLING_METERED,
    BILLING_SUBSCRIPTION,
    BillingFact,
    _load_snapshot,
    check_drift,
    lookup,
    snapshot_meta,
)


def _capacity_from_fact(fact: BillingFact | None) -> str:
    """Human-readable capacity class name for a billing fact."""
    if fact is None:
        return "unknown"
    if fact.billing == BILLING_SUBSCRIPTION:
        return "subscription"
    if fact.billing == BILLING_METERED:
        return "metered"
    if fact.billing == BILLING_KEYLESS:
        return "free (keyless)"
    return "unknown"


def run_connections_action() -> dict[str, Any]:
    """Collect connection billing facts and return a structured result dict.

    Lists every (provider, authType) pair from the billing catalog snapshot and
    the operator overlay.  No live gateway call; no model calls; no writes.
    """
    # Build from snapshot entries (and overlay entries via lookup()).
    snapshot_index, _meta = _load_snapshot()

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None]] = set()
    for prov, auth_key in sorted(snapshot_index):
        if (prov, auth_key) in seen:
            continue
        seen.add((prov, auth_key))

        # lookup() will return an overlay entry if one exists, otherwise snapshot.
        fact = lookup(prov, auth_key)
        rows.append(
            {
                "provider": prov,
                "authType": auth_key,
                "billing": fact.billing if fact else None,
                "overage": fact.overage if fact else None,
                "source": fact.source if fact else None,
                "capacity": _capacity_from_fact(fact),
            }
        )

    drift = check_drift()
    meta = snapshot_meta()
    return {
        "schema": "verdict.discovery.connections/v1",
        "snapshot_version": meta.get("omniroute_version"),
        "snapshot_sha256": meta.get("source_sha256"),
        "drift_status": drift.get("status"),
        "connections": rows,
    }


def render_connections_plain(result: dict[str, Any]) -> str:
    """Render the connections result as human-readable text."""
    lines: list[str] = []
    version = result.get("snapshot_version") or "?"
    drift = result.get("drift_status") or "?"
    lines.append(f"Snapshot: OmniRoute {version}  drift={drift}")
    lines.append("")
    rows = result.get("connections") or []
    if not rows:
        lines.append("(no connection data — start OmniRoute and re-run)")
        return "\n".join(lines)

    # Header
    lines.append(
        f"{'Provider':<22} {'AuthType':<10} {'Billing':<14} {'Overage':<16} {'Source':<10}"
    )
    lines.append("-" * 76)
    for row in sorted(rows, key=lambda r: r.get("provider", "")):
        prov = str(row.get("provider") or "")[:21]
        auth = str(row.get("authType") or "")[:9]
        billing = str(row.get("billing") or "—")[:13]
        overage = str(row.get("overage") or "—")[:15]
        source = str(row.get("source") or "—")[:9]
        lines.append(f"{prov:<22} {auth:<10} {billing:<14} {overage:<16} {source:<10}")
    return "\n".join(lines)
