"""verdict-memory — reusable lifecycle/memory capability package.

Experimental API (0.1.0a1). Schema family: lifecycle-event/v1.
"""

from verdict_memory.contracts import (
    CAPABILITY_STATUS_PARTIAL,
    CAPABILITY_STATUS_SUPPORTED,
    CAPABILITY_STATUS_UNKNOWN,
    CAPABILITY_STATUS_UNSUPPORTED,
    CONTRACT_API_VERSION,
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
)
from verdict_memory.fake import FakeMemoryProvider
from verdict_memory.policy import MemoryCapturePolicy
from verdict_memory.provider import MemoryProvider

__version__ = "0.1.0a1"
__all__ = [
    "CAPABILITY_STATUS_PARTIAL",
    "CAPABILITY_STATUS_SUPPORTED",
    "CAPABILITY_STATUS_UNKNOWN",
    "CAPABILITY_STATUS_UNSUPPORTED",
    "CONTRACT_API_VERSION",
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
    "__version__",
]
