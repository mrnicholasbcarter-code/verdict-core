"""verdict-memory — reusable lifecycle/memory capability package.

Experimental API (0.1.0a1). Schema family: lifecycle-event/v1.
"""

from verdict_memory.contracts import (
    ALLOWED_EVENT_KINDS,
    IdempotencyConflictError,
    CANONICAL_EVENT_KINDS,
    EVENT_IDENTITY_KNOWN_FIELDS,
    LIFECYCLE_EVENT_KNOWN_FIELDS,
    CAPABILITY_STATUS_PARTIAL,
    CAPABILITY_STATUS_SUPPORTED,
    CAPABILITY_STATUS_UNKNOWN,
    CAPABILITY_STATUS_UNSUPPORTED,
    CONTRACT_API_VERSION,
    EVENT_KIND_ALIASES,
    LIFECYCLE_EVENT_SCHEMA,
    CaptureDecision,
    LifecycleEvent,
    MemoryHit,
    ProviderErrorCode,
    ProviderHealth,
    ProviderResultStatus,
    RecallQuery,
    RecallResult,
    canonicalize,
    digest_bytes,
    event_digest,
    loads_lifecycle_event,
    redact_secrets,
    source_idempotency_key,
)
from verdict_memory.fake import FakeMemoryProvider
from verdict_memory.policy import MemoryCapturePolicy
from verdict_memory.provider import MemoryProvider

__version__ = "0.1.0a1"
__all__ = [
    "ALLOWED_EVENT_KINDS",
    "IdempotencyConflictError",
    "CANONICAL_EVENT_KINDS",
    "EVENT_IDENTITY_KNOWN_FIELDS",
    "LIFECYCLE_EVENT_KNOWN_FIELDS",
    "CAPABILITY_STATUS_PARTIAL",
    "CAPABILITY_STATUS_SUPPORTED",
    "CAPABILITY_STATUS_UNKNOWN",
    "CAPABILITY_STATUS_UNSUPPORTED",
    "CONTRACT_API_VERSION",
    "EVENT_KIND_ALIASES",
    "LIFECYCLE_EVENT_SCHEMA",
    "CaptureDecision",
    "FakeMemoryProvider",
    "LifecycleEvent",
    "MemoryCapturePolicy",
    "MemoryHit",
    "MemoryProvider",
    "ProviderErrorCode",
    "ProviderHealth",
    "ProviderResultStatus",
    "RecallQuery",
    "RecallResult",
    "canonicalize",
    "digest_bytes",
    "event_digest",
    "loads_lifecycle_event",
    "redact_secrets",
    "source_idempotency_key",
    "__version__",
]
