"""Shared contract for verdict.discovery. Every module imports its types from here.

Ported from freescan/model.py (D1, BOD-342).  Field names and status values
are intentionally compatible with freescan so the two registries can be compared.

Standard library only: low layers and the cheap ``import verdict.discovery`` path
import this, so it must not import verdict modules or third-party packages.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- auth -----------------------------------------------------------------
# A route's ``auth`` is one of:
#   "none"              send no Authorization header (keyless public endpoint)
#   "literal:<value>"   send "Bearer <value>" (e.g. "literal:public")
#   "env:<NAME>"        send "Bearer $NAME"; the key itself is never stored
AUTH_NONE = "none"

# --- probe statuses ---------------------------------------------------------
UP = "up"  # answered with usable content
NO_KEY = "no_key"  # needs env:<NAME> and it is unset; NOT probed
AUTH_ERROR = "auth_error"  # 401/403: the credential was rejected
RATE_LIMITED = "rate_limited"  # 429 or an explicit quota/rate message
DOWN = "down"  # other 4xx/5xx: model gone, unsupported, server error
TIMEOUT = "timeout"  # no complete answer inside the time budget
GARBAGE = "garbage"  # HTTP 200 but no usable answer (empty, error body, HTML)
ERROR = "error"  # network/DNS/TLS failure before any HTTP status

STATUSES = (UP, NO_KEY, AUTH_ERROR, RATE_LIMITED, DOWN, TIMEOUT, GARBAGE, ERROR)


@dataclass(frozen=True)
class Route:
    """One exact way to reach one model: provider + model id + endpoint + auth."""

    provider: str  # stable provider key, e.g. "kilo", "pollinations", "omniroute"
    model: str  # exact model id sent in the request body
    base_url: str  # OpenAI-compatible base; probe POSTs {base_url}/chat/completions
    auth: str = AUTH_NONE  # see the auth section above
    source: str = "static"  # where this route came from: "static", "catalog", "omniroute"
    context: int | None = None  # context window in tokens, when known
    upstream: str | None = None  # for gateway routes: the provider behind the gateway
    notes: str | None = None  # short human note, e.g. "200 req/hour per IP"
    headers: dict[str, str] = field(default_factory=dict, compare=False, hash=False)
    # What using this route costs, as information (never a filter):
    # "free", "subscription", "priced" or "unknown".  None = not assessed.
    billing: str | None = field(default=None, compare=False, hash=False)

    @property
    def key(self) -> str:
        """Stable identity used to join probe results."""
        return f"{self.provider}/{self.model}"


@dataclass
class ProbeResult:
    route: Route
    status: str  # one of STATUSES
    latency_ms: int | None = None  # wall time to the complete answer
    http_code: int | None = None
    detail: str | None = None  # short reason, at most ~200 chars, never a secret
    retry_after_s: float | None = None
    answered_model: str | None = None  # "model" field of the response, when present
    tool: str | None = None  # tool-call check (toolprobe.py), when run
