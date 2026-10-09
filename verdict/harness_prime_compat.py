"""Read-only Prime 0.9.8 exact-id compatibility overlay.

Health comes from ``VerifiedModelRow.to_dict()`` shaped input. Registry visibility,
transport readiness and interactive scope are independent of launch admission.
No helper reads files, executes credential commands, calls a provider or syncs the
registry. The caller supplies discovered release/binary and credential-presence
facts, and an endpoint/evidence binding it has independently validated.

Unknown releases fail closed. The supported resolver checks literal ids first,
then case-insensitive names/substrings, and finally effort suffixes. We refuse
competing bindings rather than depend on registry order or fuzzy fallback.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from verdict.admission import canonical_route_id

SUPPORTED_RELEASES = frozenset({"0.9.8"})
SUPPORTED_APIS = frozenset({"openai-completions", "openai-responses"})
_STATUSES = frozenset(
    {"VERIFIED", "STALE", "FAILED", "UNAVAILABLE", "UNVERIFIED", "INVENTORY_ONLY", "EXCLUDED"}
)
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,255}\Z")


@dataclass(frozen=True)
class PrimeCompatibilityContext:
    """Sanitized discovery and independently checked credential/source facts.

    ``agent_dir`` must be the resolved operator directory, not a worker mirror.
    ``binary_digest`` binds the discovered binary/support facts. Credentials are
    presence booleans only; use per-id overrides when effective model auth differs.
    ``gateway_endpoint`` is the recognized evidence gateway, not a guessed port.
    """

    agent_dir: Path
    installed: bool
    release: str | None
    binary_digest: str
    gateway_endpoint: str | None
    evidence_source: str | None
    credentials_present: bool | None
    credential_overrides: tuple[tuple[str, bool | None], ...] = ()
    project_scope_override: bool = False
    path_recognized: bool = True

    def credential_presence(self, route_id: str) -> bool | None:
        return dict(self.credential_overrides).get(route_id, self.credentials_present)


@dataclass(frozen=True)
class PrimeBinding:
    """A unique literal binding, retained unknown, or unsafe resolution."""

    token: str
    registry_provider: str | None
    prime_id: str | None
    disposition: str
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class PrimeSelectionRow:
    """Health metadata plus separate visibility/compatibility, never launch proof."""

    route_id: str
    provider: str
    status: str
    coding_ok: bool
    checked_at: str | None
    fresh_until: str | None
    expires_at: str | None
    cooldown_until: str | None
    cooldown_scope: str | None
    identity: str | None
    registry_provider: str
    prime_id: str
    visible: bool
    execution: str
    reasons: tuple[str, ...]
    selectable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "provider": self.provider,
            "status": self.status,
            "coding_ok": self.coding_ok,
            "checked_at": self.checked_at,
            "fresh_until": self.fresh_until,
            "expires_at": self.expires_at,
            "cooldown_until": self.cooldown_until,
            "cooldown_scope": self.cooldown_scope,
            "identity": self.identity,
            "registry_provider": self.registry_provider,
            "prime_id": self.prime_id,
            "visible": self.visible,
            "execution": self.execution,
            "reasons": list(self.reasons),
            "selectable": self.selectable,
        }


def aware_time(value: datetime) -> datetime:
    """Reject an implicit local/naive clock rather than extend freshness."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone_aware_time_required")
    return value


def timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return aware_time(result)
    except ValueError:
        return None


def exact_token(value: object) -> bool:
    """Accept display-safe literal ids; never patterns or control characters."""
    return (
        isinstance(value, str)
        and _TOKEN.fullmatch(value) is not None
        and not value.startswith(("--", "sk-"))
        and not value.lower().startswith(("auto/", "bearer"))
        and "//" not in value
    )


def _entries(models_doc: Mapping[str, Any]) -> tuple[tuple[str, str, str, Mapping[str, Any]], ...]:
    providers = models_doc.get("providers")
    if not isinstance(providers, Mapping):
        raise ValueError("registry_invalid")
    entries: list[tuple[str, str, str, Mapping[str, Any]]] = []
    for provider, config in providers.items():
        if not exact_token(provider) or not isinstance(config, Mapping):
            raise ValueError("registry_invalid")
        rows = config.get("models")
        if not isinstance(rows, list):
            raise ValueError("registry_invalid")
        for row in rows:
            if not isinstance(row, Mapping) or not exact_token(row.get("id")):
                raise ValueError("registry_invalid")
            rid = str(row["id"])
            name = row.get("name", rid)
            if not isinstance(name, str):
                raise ValueError("registry_invalid")
            entries.append((str(provider), rid, name, row))
    return tuple(entries)


def bind_prime_token(token: str, models_doc: Mapping[str, Any]) -> PrimeBinding:
    """Classify current scope without expanding globs/names/effort suffixes.

    Unknown literal tokens survive only if the supported resolver cannot bind
    them through a name, case-insensitive substring or effort fallback.
    """
    return _bind(token, _entries(models_doc))


def _bind(token: str, entries: tuple[tuple[str, str, str, Mapping[str, Any]], ...]) -> PrimeBinding:
    if not exact_token(token):
        return PrimeBinding("[unsafe entry]", None, None, "unsafe", ("unsafe_scope_token",))
    lower = token.casefold()
    matches = [e for e in entries if lower in (e[1].casefold(), f"{e[0]}/{e[1]}".casefold())]
    if len(matches) > 1:
        return PrimeBinding(token, None, None, "unsafe", ("identity_unsupported",))
    if matches:
        entry = matches[0]
        competitors = [e for e in entries if e is not entry and lower in e[2].casefold()]
        if competitors:
            return PrimeBinding(token, None, None, "unsafe", ("identity_unsupported",))
        if token not in (entry[1], f"{entry[0]}/{entry[1]}"):
            return PrimeBinding(token, None, None, "unsafe", ("identity_unsupported",))
        # A literal colon id is supported when it has no alternate base binding.
        if ":" in token:
            base = token.rsplit(":", 1)[0].casefold()
            if any(base in (e[1].casefold(), f"{e[0]}/{e[1]}".casefold()) for e in entries):
                return PrimeBinding(token, None, None, "unsafe", ("identity_unsupported",))
        return PrimeBinding(token, entry[0], entry[1], "bound")
    fuzzy = any(lower in e[1].casefold() or lower in e[2].casefold() for e in entries)
    if ":" in token:
        base = token.rsplit(":", 1)[0].casefold()
        fuzzy = fuzzy or any(base in e[1].casefold() or base in e[2].casefold() for e in entries)
    if fuzzy:
        return PrimeBinding(token, None, None, "unsafe", ("identity_unsupported",))
    return PrimeBinding(token, None, None, "retained_unknown", ("retained_unknown_not_verified",))


def _endpoint(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            return None
        return value.rstrip("/")
    except ValueError:
        return None


def prime_selection_rows(
    models_doc: Mapping[str, Any],
    projection_rows: Sequence[Mapping[str, Any]],
    *,
    context: PrimeCompatibilityContext,
    now: datetime,
) -> tuple[PrimeSelectionRow, ...]:
    """Add compatibility to supplied projection rows without reclassifying health."""
    aware_time(now)
    entries = _entries(models_doc)
    providers = models_doc["providers"]
    omni = providers.get("omniroute", {})
    output: list[PrimeSelectionRow] = []
    for raw in projection_rows:
        raw_id = raw.get("route_id")
        if not exact_token(raw_id):
            continue
        rid = canonical_route_id(str(raw_id))
        if not exact_token(rid):
            continue
        binding = _bind(rid, entries)
        exact = [e for e in entries if e[0] == "omniroute" and e[1] == rid]
        visible = bool(exact)
        reasons: list[str] = []
        if not visible:
            reasons.extend(("not visible to Prime", "separate_sync_required"))
        if binding.disposition != "bound" or binding.registry_provider != "omniroute":
            reasons.append("identity_unsupported")
        status = raw.get("status")
        status = status if isinstance(status, str) and status in _STATUSES else "UNVERIFIED"
        if status != "VERIFIED":
            reasons.append(f"health_{status.lower()}")
        checked = timestamp(raw.get("checked_at"))
        fresh = timestamp(raw.get("fresh_until"))
        expiry = timestamp(raw.get("expires_at"))
        if checked is None or fresh is None or expiry is None:
            reasons.append("missing_proof_timestamps")
        elif checked > now or fresh <= checked or expiry < fresh:
            reasons.append("invalid_proof_timestamps")
        elif fresh <= now or expiry <= now:
            reasons.append("proof_expired")
        identity = raw.get("identity")
        if identity != "verified":
            reasons.append("identity_unverified")
        cooldown = timestamp(raw.get("cooldown_until"))
        if raw.get("cooldown_until") is not None and cooldown is None:
            reasons.append("invalid_cooldown")
        elif cooldown is not None and cooldown > now:
            reasons.append("active_cooldown")
        if raw.get("restriction") or raw.get("restrictions"):
            reasons.append("active_restriction")
        readiness: list[str] = []
        if not context.installed:
            readiness.append("binary_missing")
        if context.release not in SUPPORTED_RELEASES or not context.binary_digest:
            readiness.append("release_support_unknown")
        if not context.path_recognized:
            readiness.append("effective_path_unrecognized")
        presence = context.credential_presence(rid)
        if presence is not True:
            readiness.append("credentials_missing" if presence is False else "credentials_unknown")
        model = exact[0][3] if len(exact) == 1 else {}
        if ("apiKey" in model or "headers" in model) and rid not in dict(
            context.credential_overrides
        ):
            readiness.append("model_credentials_unknown")
        api = model.get("api", omni.get("api"))
        if not isinstance(api, str) or api not in SUPPORTED_APIS:
            readiness.append("unsupported_api")
        endpoint = model.get(
            "baseUrl", model.get("baseURL", omni.get("baseUrl", omni.get("baseURL")))
        )
        expected = _endpoint(context.gateway_endpoint)
        if expected is None or _endpoint(endpoint) != expected:
            readiness.append("gateway_endpoint_unbound")
        if context.evidence_source is None or raw.get("evidence_source") != context.evidence_source:
            readiness.append("evidence_source_unbound")
        if binding.disposition != "bound" or binding.registry_provider != "omniroute":
            readiness.append("identity_unsupported")
        execution = "compatible_not_launch_confirmed" if not readiness else "blocked"
        if readiness and all(
            r in {"credentials_unknown", "release_support_unknown"} for r in readiness
        ):
            execution = "unknown"
        reasons.extend(readiness)
        output.append(
            PrimeSelectionRow(
                route_id=rid,
                provider=str(raw.get("provider"))
                if exact_token(raw.get("provider"))
                else "unknown",
                status=status,
                coding_ok=raw.get("coding_ok") is True,
                checked_at=checked.isoformat() if checked else None,
                fresh_until=fresh.isoformat() if fresh else None,
                expires_at=expiry.isoformat() if expiry else None,
                cooldown_until=cooldown.isoformat() if cooldown else None,
                cooldown_scope=raw.get("cooldown_scope")
                if isinstance(raw.get("cooldown_scope"), str)
                and raw.get("cooldown_scope") in {"route", "provider", "account", "gateway"}
                else None,
                identity=identity
                if isinstance(identity, str)
                and identity in {"verified", "unverified", "unsupported", "opaque"}
                else None,
                registry_provider="omniroute",
                prime_id=rid,
                visible=visible,
                execution=execution,
                reasons=tuple(dict.fromkeys(reasons)),
                selectable=not reasons,
            )
        )
    return tuple(output)
