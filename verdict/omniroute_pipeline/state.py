"""Pure per-model state machine (spec v6.1 section 3). No I/O, no OmniRoute calls."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum

from verdict.omniroute_pipeline.probes import ProbeOutcome

STATE_FLAG_ENV = "VERDICT_PIPELINE_STATE"


def state_enabled() -> bool:
    return os.getenv(STATE_FLAG_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


class ModelState(Enum):
    UNSEEN = "unseen"
    CATALOGED = "cataloged"
    POLICY_REJECTED = "policy_rejected"
    UNVERIFIED_FREE = "unverified_free"
    SHUTDOWN = "shutdown"
    NEEDS_SYNC = "needs_sync"
    NEEDS_ACCOUNT = "needs_account"
    COOLING = "cooling"
    CHAT_OK = "chat_ok"
    TOOL_OK = "tool_ok"
    CTX_MEASURED = "ctx_measured"
    RANKED = "ranked"
    ELIGIBLE = "eligible"
    DEGRADED = "degraded"
    FAILED = "failed"


NO_PROBE_STATES = frozenset(
    {
        ModelState.SHUTDOWN,
        ModelState.UNVERIFIED_FREE,
        ModelState.POLICY_REJECTED,
        ModelState.NEEDS_ACCOUNT,
        ModelState.NEEDS_SYNC,
        ModelState.COOLING,
    }
)

_PROGRESS = {
    ModelState.CATALOGED: ModelState.CHAT_OK,
    ModelState.CHAT_OK: ModelState.TOOL_OK,
    ModelState.TOOL_OK: ModelState.CTX_MEASURED,
    ModelState.CTX_MEASURED: ModelState.RANKED,
    ModelState.RANKED: ModelState.ELIGIBLE,
    ModelState.DEGRADED: ModelState.ELIGIBLE,
}


@dataclass
class ProviderBlocklist:
    """Providers dropped for billing fakes; only the controller clears an entry."""

    entries: dict[str, str] = field(default_factory=dict)

    def block(self, provider: str, reason: str) -> None:
        self.entries.setdefault(provider, reason)

    def is_blocked(self, provider: str) -> bool:
        return provider in self.entries

    def reason(self, provider: str) -> str | None:
        return self.entries.get(provider)


def should_probe(state: ModelState) -> bool:
    return state not in NO_PROBE_STATES


def transition(
    state: ModelState,
    *,
    http_status: int | None = None,
    error_code: str | None = None,
    probe: ProbeOutcome | None = None,
    route: str | None = None,
    blocklist: ProviderBlocklist | None = None,
) -> ModelState:
    """Next state for one observation. FAIL_FAKE also blocklists the route's provider."""
    if probe is ProbeOutcome.FAIL_FAKE:
        if blocklist is not None and route:
            blocklist.block(route.split("/", 1)[0], "fail_fake")
        return ModelState.NEEDS_ACCOUNT
    if http_status in (401, 402) or error_code == "undecryptable":
        return ModelState.NEEDS_ACCOUNT
    if http_status == 410 or error_code == "model_shutdown":
        return ModelState.SHUTDOWN
    if http_status == 404 or error_code == "catalog_stale":
        return ModelState.NEEDS_SYNC
    if http_status == 429:
        return ModelState.COOLING
    if probe is ProbeOutcome.PASS:
        return _PROGRESS.get(state, state)
    if probe is ProbeOutcome.FAIL:
        if state is ModelState.ELIGIBLE:
            return ModelState.DEGRADED
        if state is ModelState.DEGRADED:
            return ModelState.FAILED
        return ModelState.FAILED if state in _PROGRESS else state
    return state
